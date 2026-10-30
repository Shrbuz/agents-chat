import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .store import TERMINAL, compact


class Client:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()

    def call(self, method, **params):
        try:
            endpoint = json.loads((self.state_dir / "endpoint.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("broker_not_running: start the broker first") from None
        url = urlparse(endpoint["url"])
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path or url.username or url.query or url.fragment:
            raise RuntimeError("invalid_local_endpoint")
        req = urllib.request.Request(endpoint["url"] + "/call", data=compact({"method": method, "params": params}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + endpoint["token"]})
        try:
            # Never send local requests or the local bearer through a configured HTTP proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=60) as response:
                return json.load(response)["result"]
        except urllib.error.HTTPError as exc:
            with exc:
                message = json.load(exc).get("error", "request_failed")
            raise RuntimeError(message) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise RuntimeError("broker_unreachable_or_request_timeout; retry sends only with the SAME idempotency key") from None

    def wait(self, task_id, timeout=300):
        deadline = time.monotonic() + timeout
        while True:
            result = self.call("wait", task_id=task_id, seconds=max(0, min(45, deadline - time.monotonic())))
            if result["state"] in TERMINAL or time.monotonic() >= deadline:
                return result
