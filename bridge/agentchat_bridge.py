"""Wake role sessions for events written by the user's agent-chat skill.

The file bus remains the source of truth (claim/lease/verify semantics live in
that skill).  This bridge only turns a new event into one bounded wakeup task
for the role session, with no extra model polling.
"""
import json
from pathlib import Path
import sys
import threading
import time

from .bus import Bus, parse_time, utc_now

DELIVER = {"request", "result", "blocked", "cancel", "release", "reassign"}
WAKEUP_SENDER = "agent-chat"
# A failed delivery is worth retrying, but an offline connector must not grow
# an unbounded queue of identical wakeups.
MAX_WAKEUP_ATTEMPTS = 5


def _helper_for(project, root):
    configured = (project.get("agent_chat") or {}).get("helper")
    candidates = [Path(configured) if configured else None,
                  Path.home() / ".agents/skills/agent-chat/scripts/agent_chat.py",
                  Path(root) / "tools/agent-chat/scripts/agent_chat.py",
                  Path(root) / ".agents/skills/agent-chat/scripts/agent_chat.py"]
    return next((path for path in candidates if path and path.is_file()), None)


def _event_path(bus, role_directory, filename):
    return bus / role_directory / filename


def _wake_roles(chat, resolved):
    """Restrict hub wakeups to the roles that actually run through the hub.

    A DSH host plugin polls the same bus by itself; if the hub also wakes that
    role it would dispatch the same task twice. ``agent_chat.wake_roles`` names
    the roles the hub owns, and everything else is left to other clients.
    """
    listed = chat.get("wake_roles")
    if isinstance(listed, list) and listed:
        return {name: role for name, role in resolved.items() if name in listed}
    return resolved


class AgentChatBridge:
    def __init__(self, state_dir, config, store, notify):
        self.state = Path(state_dir)
        self.config = config
        self.store = store
        self.notify = notify
        self.stop = threading.Event()
        self.thread = None
        self.projects = self._prepare()
        self.started_at = None
        self.last_scan_at = None
        self.wakeups = 0
        self.cursor_resets = 0
        self.last_cursor_reset = None
        self.project_errors = {}
        self.project_success = {}

    def _prepare(self):
        prepared = []
        for pid, project in self.config.get("projects", {}).items():
            chat = project.get("agent_chat")
            if not chat or not chat.get("enabled", True):
                continue
            bus = Path(chat["bus"])
            manifest_path = bus / "project.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            bus_roles = manifest.get("roles", {})
            if not isinstance(bus_roles, dict):
                continue
            mapping = chat.get("roles") or {}
            if not mapping:
                mapping = {name: name for name in bus_roles if name in project["roles"]}
            resolved = {}
            for bus_role, local_role in mapping.items():
                directory = bus_roles.get(bus_role)
                address = project["roles"].get(local_role)
                if not directory or not address:
                    continue
                resolved[bus_role] = {"directory": directory, "address": address,
                                      "local_role": local_role}
            prepared.append({"id": pid, "root": project["root"], "bus": bus,
                             "helper": _helper_for(project, project["root"]),
                             "roles": _wake_roles(chat, resolved),
                             "project_id": manifest.get("project_id", pid)})
        return prepared

    def status(self):
        return [{"id": p["id"], "bus": str(p["bus"]), "roles": len(p["roles"]),
                 "helper": str(p["helper"]) if p["helper"] else None,
                 "open_tasks": len(self.store.bus_projection(p["id"])),
                 "thread_alive": bool(self.thread and self.thread.is_alive()),
                 "last_error": (self.project_errors.get(p["id"]) or {}).get("error")}
                for p in self.projects]

    def health(self):
        """Wakeup-chain health, separate from adapter worker liveness.

        ``workers_alive`` only counts adapter workers; a dead or failing
        agent-chat watcher used to look perfectly healthy.
        """
        alive = bool(self.thread and self.thread.is_alive())
        projects = []
        degraded = bool(self.projects) and not alive
        for project in self.projects:
            pid = project["id"]
            error = self.project_errors.get(pid)
            success = self.project_success.get(pid, 0)
            project_degraded = bool(error and error.get("at", 0) > success)
            degraded = degraded or project_degraded
            projects.append({
                "id": pid,
                "thread_alive": alive,
                "degraded": project_degraded,
                "last_success_at": success or None,
                "last_error": (error or {}).get("error"),
                "last_error_at": (error or {}).get("at"),
            })
        fatal = self.project_errors.get("*")
        return {
            "started": self.thread is not None,
            "thread_alive": alive,
            "ok": (not self.projects) or (alive and not degraded),
            "degraded": degraded,
            "started_at": self.started_at,
            "last_scan_at": self.last_scan_at,
            "last_error": (fatal or {}).get("error"),
            "wakeups": self.wakeups,
            "cursor_resets": self.cursor_resets,
            "last_cursor_reset": self.last_cursor_reset,
            "projects": projects,
        }

    def _record_error(self, project_id, exc):
        self.project_errors[project_id] = {"at": time.time(), "error": str(exc)[:300]}
        try:
            sys.stderr.write("agents-chat: agent-chat wakeup error [" + project_id + "]: "
                             + str(exc)[:300] + "\n")
            sys.stderr.flush()
        except OSError:
            pass

    def _record_success(self, project_id):
        self.project_success[project_id] = time.time()

    def _record_cursor_reset(self, project_id, bus_role, old, new):
        self.cursor_resets += 1
        self.last_cursor_reset = {"project": project_id, "role": bus_role,
                                  "old": old, "new": new, "at": time.time()}
        try:
            sys.stderr.write("agents-chat: resetting stale bus cursor for " + project_id + "/"
                             + bus_role + ": " + str(old) + " -> " + str(new) + "\n")
            sys.stderr.flush()
        except OSError:
            pass

    def projection(self, project=None, include_closed=False):
        """Read-only task view derived from the bus log."""
        return self.store.bus_projection(project, include_closed=include_closed)

    def start(self):
        if not self.projects:
            return
        self.started_at = time.time()
        self._prime_cursors()
        # Priming skips on-disk history, which is correct for delivered events.
        # A request that is still pending and has no wakeup task, however, must
        # not stay invisible just because it predates this hub process.
        for project in self.projects:
            try:
                self._reconcile_pending(project)
                self._record_success(project["id"])
            except Exception as exc:
                self._record_error(project["id"], exc)
                continue
        self.thread = threading.Thread(target=self._loop, name="agent-chat-bridge", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _key(self, project, bus_role):
        return project + ":" + bus_role

    def _prime_cursors(self):
        """Skip history, but never let a stale high-water mark hide a rebuilt bus.

        A cursor is a sequence filename.  If someone rebuilds/replaces the bus,
        its sequences restart at 1 while the stored cursor may still be at 579;
        every new event then compares smaller and is silently skipped forever.
        """
        for project in self.projects:
            raw = self.store.bus_cursor(project["id"])
            stored = self._cursors(project["id"], project["roles"].keys())
            latest = {}
            for bus_role, role in project["roles"].items():
                names = [path.name for path in project["bus"].glob(role["directory"] + "/*.json")]
                latest[bus_role] = max(names) if names else ""
            if not raw:
                self.store.set_bus_cursor(project["id"], json.dumps(latest, sort_keys=True))
                continue
            updated = dict(stored)
            for bus_role, cursor in stored.items():
                newest = latest.get(bus_role, "")
                if cursor and (not newest or cursor > newest):
                    updated[bus_role] = newest
                    self._record_cursor_reset(project["id"], bus_role, cursor, newest)
            if updated != stored:
                self.store.set_bus_cursor(project["id"], json.dumps(updated, sort_keys=True))

    def _cursors(self, project_id, bus_roles):
        """Per-role cursors; an old single-string cursor seeds every role."""
        raw = self.store.bus_cursor(project_id)
        try:
            stored = json.loads(raw) if raw else {}
        except ValueError:
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        if raw and not stored:
            # Legacy format: one filename for the whole project.
            stored = {role: raw for role in bus_roles}
        return {role: stored.get(role, "") for role in bus_roles}

    def _set_cursor(self, project_id, bus_role, name):
        raw = self.store.bus_cursor(project_id)
        try:
            stored = json.loads(raw) if raw else {}
        except ValueError:
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored[bus_role] = name
        self.store.set_bus_cursor(project_id, json.dumps(stored, sort_keys=True))

    def _loop(self):
        ticks = 0
        try:
            while not self.stop.wait(1.5):
                ticks += 1
                for project in self.projects:
                    project_id = project["id"]
                    try:
                        self._scan(project)
                        self._record_success(project_id)
                    except Exception as exc:
                        # A malformed or concurrently rewritten event must not
                        # kill the watcher, but it must not be invisible either.
                        self._record_error(project_id, exc)
                        continue
                    if ticks % 40 == 0:
                        try:
                            self._reconcile_pending(project)
                            self._record_success(project_id)
                        except Exception as exc:
                            self._record_error(project_id, exc)
                        try:
                            self._renew_leases(project)
                        except Exception as exc:
                            self._record_error(project_id, exc)
                self.last_scan_at = time.time()
        except BaseException as exc:
            # Do not let the watcher die silently; health() exposes this as a
            # dead/degraded wakeup thread instead of looking like worker health.
            self._record_error("*", exc)
            return

    def _renew_leases(self, project):
        """Keep a desktop session's claim alive while that session is still there.

        Only roles the hub actually delivers to are touched: their session heartbeats,
        so the hub knows the agent is present rather than crashed. A role driven by a
        bare CLI has no such signal, and renewing it blindly would turn the lease back
        into a deadlock.
        """
        bus = Bus(project["bus"])
        _, tasks = bus.load()
        now = utc_now()
        for task in tasks.values():
            claim = task.get("claim")
            if not claim or task["status"] not in ("in_progress", "blocked"):
                continue
            role = project["roles"].get(task["assignee"])
            if not role or not self._desktop_session_online(project, role):
                continue
            lease = claim.get("lease_seconds") or 1800
            remaining = (parse_time(claim["expires_at"]) - now).total_seconds()
            if remaining > lease / 2:
                continue
            try:
                bus.submit("renew", task["assignee"], claim["session"],
                           task_id=task["task_id"], token=claim["token"])
            except ValueError:
                # Someone else took over, or the task moved on: nothing to keep alive.
                continue

    def _desktop_session_online(self, project, role):
        sessions = self.store.list_sessions(project=project["id"], role=role["local_role"])
        return any(item["id"].startswith("desktop:") and item["online"] for item in sessions)

    def _scan(self, project):
        project_id = project["id"]
        cursors = self._cursors(project_id, project["roles"].keys())
        reset = False
        for bus_role, role in project["roles"].items():
            cursor = cursors[bus_role]
            directory = project["bus"] / role["directory"]
            paths = sorted(directory.glob("*.json"))
            newest = paths[-1].name if paths else ""
            if cursor and (not newest or cursor > newest):
                # The bus was rebuilt/replaced and its sequence restarted.
                # Do not replay the old files; checkpoint at the new tail and
                # let reconcile wake the latest open events.
                self._set_cursor(project_id, bus_role, newest)
                self._record_cursor_reset(project_id, bus_role, cursor, newest)
                cursor = newest
                cursors[bus_role] = newest
                reset = True
            for path in paths:
                if path.name <= cursor:
                    continue
                try:
                    event = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self._set_cursor(project_id, bus_role, path.name)
                    cursor = path.name
                    continue
                action = self._action(project_id, bus_role, event)
                if action is False:
                    self._set_cursor(project_id, bus_role, path.name)
                    cursor = path.name
                    continue
                # Do not block the wakeup on unrelated work for the role.  A
                # long-running desktop task used to make the cursor sit here
                # forever, so a new request never produced its agent-chat wakeup.
                self._enqueue_wakeup(project, bus_role, role, event, action)
                self._set_cursor(project_id, bus_role, path.name)
                cursor = path.name
                self.notify()
        if reset:
            self._reconcile_pending(project)
            self._record_success(project_id)
        self._sync_projection(project)

    def _enqueue_wakeup(self, project, bus_role, role, event, action):
        base = "agentchat:" + str(event.get("message_id") or event.get("task_id"))
        existing = self.store.latest_task_by_key_prefix(WAKEUP_SENDER, base)
        if existing is not None and existing["state"] in ("queued", "running", "succeeded"):
            return existing
        attempts = self.store.count_tasks_by_key_prefix(WAKEUP_SENDER, base)
        if attempts >= MAX_WAKEUP_ATTEMPTS:
            return existing
        key = base if existing is None else base + ":retry:" + str(time.time_ns())
        prompt = self._prompt(project, bus_role, role, None, event, action)
        payload = {"prompt": prompt, "timeout": 1800, "delivery_timeout": 30,
                   "context": [], "reply_mode": "mailbox", "queue_ttl": 3600,
                   "notify": False, "kind": "agent_chat_wakeup",
                   "interrupt_on_timeout": False}
        task_id, duplicate = self.store.enqueue(WAKEUP_SENDER, role["address"], key, payload)
        if not duplicate:
            self.wakeups += 1
        return self.store.get(task_id)

    def _reconcile_pending(self, project):
        """Wake a task whose latest bus event asks for action but has no wakeup.

        The cursor keeps normal scanning cheap; this safety pass makes a missed
        event (hub restart, a wakeup blocked behind another task, a crash between
        the cursor write and enqueue) recoverable without replaying old history.
        """
        try:
            events, tasks = Bus(project["bus"]).load()
        except (OSError, ValueError):
            return
        latest = {}
        for event in events:
            latest[event["task_id"]] = event
        cursors = self._cursors(project["id"], project["roles"].keys())
        for task_id, event in latest.items():
            task = tasks.get(task_id)
            if not task:
                continue
            bus_role = event.get("to")
            role = project["roles"].get(bus_role)
            if not role:
                continue
            action = self._reconcile_action(event, task)
            if not action:
                continue
            self._enqueue_wakeup(project, bus_role, role, event, action)
            name = f"{event.get('sequence', 0):08d}_{event.get('message_id', '')}.json"
            if name > cursors.get(bus_role, ""):
                self._set_cursor(project["id"], bus_role, name)
                cursors[bus_role] = name
            self.notify()

    @staticmethod
    def _reconcile_action(event, task):
        """Only the latest event matters; only open states deserve a wakeup."""
        kind = event.get("type")
        status = task.get("status")
        data = event.get("data") or {}
        if kind == "request" and status == "pending":
            return "request"
        if kind == "claim" and status == "in_progress":
            return "claim"
        if kind == "result" and status == "resolved":
            return "result"
        if kind == "blocked" and status == "blocked":
            return "blocked"
        if kind == "release" and status == "pending":
            return "release"
        if kind == "reassign" and status == "pending":
            return "reassign"
        if kind == "verify" and data.get("outcome") == "pass" and status == "closed":
            return "verify_pass"
        if kind == "verify" and data.get("outcome") == "fail" and status == "pending":
            return "verify_fail"
        return False

    def _sync_projection(self, project):
        """Mirror replayed task state into the store; the bus remains authoritative."""
        try:
            _, tasks = Bus(project["bus"]).load()
        except (OSError, ValueError):
            # A damaged or partially written bus must never stop wakeups.
            return
        self.store.sync_bus(project["id"], tasks)

    def _action(self, project_id, bus_role, event):
        """Decide whether an event deserves a wakeup at all.

        The requester is told when its work is claimed or finished; the assignee
        is told on request, verify pass and verify fail.  A cancellation of work
        nobody claimed stays on disk only.
        """
        if not isinstance(event, dict):
            return False
        if event.get("to") != bus_role:
            return False
        if event.get("from") == bus_role:
            return False
        kind = event.get("type")
        if kind == "verify":
            return ("verify_pass" if (event.get("data") or {}).get("outcome") == "pass"
                    else "verify_fail")
        if kind == "claim":
            return "claim"
        if kind == "cancel":
            # Stop someone who is working; an unclaimed cancel is just a note.
            return kind if self._task_status(project_id, event.get("task_id")) in ("in_progress", "blocked") else False
        if kind in DELIVER:
            return kind
        return False

    def _task_status(self, project_id, task_id):
        for row in self.store.bus_projection(project_id):
            if row.get("task_id") == task_id:
                return row.get("status")
        return None

    def _prompt(self, project, bus_role, role, path, event, action):
        """Keep the wakeup short: a pointer plus a runnable command.

        The event already sits in the role's inbox, so repeating its title and
        acceptance only burns tokens. The session name is filled in from the
        role's bound chat when we know it, so nothing has to be guessed.
        """
        helper = project["helper"]
        session = self._bus_session(project, bus_role) or f"{bus_role}-1"
        # The action depends on the event: only a request wants a claim, and only
        # a result wants a verify. Saying "领取" for a result is simply wrong.
        hint = {
            "request": "请用 claim 领取并处理，完成后用 result 回传结果。",
            "claim": "对方已领取任务，查看即可。",
            "result": "查看即可；需要确认时用 verify 通过或驳回。",
            "verify_pass": "验收已通过，查看即可。",
            "verify_fail": "这是被驳回的验收，请处理问题后重新 result。",
            "blocked": "对方报告了阻塞，查看后决定是继续还是 reassign。",
            "cancel": "任务已取消，查看即可。",
            "release": "任务已被交还，等你或其他人重新领取。",
            "reassign": "任务已被发起方收回，请停止手上的工作。",
        }.get(action, "去收件箱查看。")
        head = f"Agents Chat 收件箱有你的新消息（{action}，task {str(event.get('task_id'))[:8]}）。{hint}"
        if not helper:
            return head + "\n用项目内的 agent-chat helper 执行 inbox/claim/result/verify。"
        return (head + "\n"
                f'python "{helper}" --project "{project["root"]}" inbox '
                f'--role {bus_role} --session {session}')

    def _bus_session(self, project, bus_role):
        """The session name this role already uses on the bus, if it has one."""
        role = project["roles"].get(bus_role) or {}
        local_role = role.get("local_role") or bus_role
        try:
            sessions = self.store.list_sessions(project=project["id"], role=local_role)
        except Exception:
            return None
        for item in sessions:
            if item["id"].startswith("desktop:"):
                return item["id"].split(":", 1)[1]
        return None






