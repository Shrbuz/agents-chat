"""Wake role sessions for events written by the user's agent-chat skill.

The file bus remains the source of truth (claim/lease/verify semantics live in
that skill).  This bridge only turns a new event into one bounded wakeup task
for the role session, with no extra model polling.
"""
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

from .bus import Bus, parse_time, utc_now

DELIVER = {"request", "result", "blocked", "cancel", "release", "reassign"}
WAKEUP_SENDER = "agent-chat"
def _helper_for(project, root):
    configured = (project.get("agent_chat") or {}).get("helper")
    candidates = [Path(configured) if configured else None,
                  Path.home() / ".agents/skills/agents-chat-desktop/scripts/agent_chat.py",
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
    def __init__(self, state_dir, config, store, notify, transport_online=None):
        self.state = Path(state_dir)
        self.config = config
        self.store = store
        self.notify = notify
        self.transport_online = transport_online
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
        wakeups = self.store.wakeup_stats()
        degraded = degraded or bool(wakeups["needs_attention"])
        return {
            "started": self.thread is not None,
            "thread_alive": alive,
            "ok": not degraded and ((not self.projects) or alive),
            "degraded": degraded,
            "started_at": self.started_at,
            "last_scan_at": self.last_scan_at,
            "last_error": (fatal or {}).get("error"),
            "wakeups": self.wakeups,
            "pending_wakeups": wakeups["pending"],
            "pending_result_wakeups": wakeups["pending_results"],
            "needs_attention": wakeups["needs_attention"],
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
        # Upgrades may inherit an old message-id keyed wakeup plus one task
        # created by the new composite-key code before the first restart.  Keep
        # exactly one active delivery without touching any role session.
        self.store.collapse_active_wakeup_duplicates()
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
        if self.transport_online is not None:
            try:
                if not self.transport_online(role["address"]):
                    return False
            except Exception:
                return False
        sessions = self.store.list_sessions(project=project["id"], role=role["local_role"])
        return any(item["id"].startswith("desktop:") and item["online"] for item in sessions)

    def _scan(self, project):
        project_id = project["id"]
        events, tasks = Bus(project["bus"]).load()
        self._discard_terminal_wakeups(project, events, tasks)
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
            pending = []
            for path in paths:
                if path.name <= cursor:
                    continue
                try:
                    event = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self._set_cursor(project_id, bus_role, path.name)
                    cursor = path.name
                    continue
                pending.append(event)
            selected = self._select_actions(pending, tasks, bus_role, include_cancel=True)
            if selected:
                # Reserve/deliver the whole scan as one summary.  The cursor is
                # advanced only after the idempotency records and queue entry
                # commit, so a crash cannot silently lose a notification.
                self._enqueue_wakeup_batch(project, bus_role, role, selected)
                self.notify()
            if paths and paths[-1].name > cursor:
                self._set_cursor(project_id, bus_role, paths[-1].name)
                cursors[bus_role] = paths[-1].name
        if reset:
            self._reconcile_pending(project)
            self._record_success(project_id)
        self._sync_projection(project, tasks)

    def _enqueue_wakeup(self, project, bus_role, role, event, action):
        return self._enqueue_wakeup_batch(project, bus_role, role, [(event, action)])

    def _delivery_key(self, project, event, session):
        """Stable identity independent from the generated queue task id."""
        return "|".join((
            str(project.get("project_id") or project["id"]),
            str(event.get("task_id") or ""),
            str(event.get("message_id") or ""),
            str(event.get("sequence") or ""),
            str(session),
        ))

    def _enqueue_wakeup_batch(self, project, bus_role, role, selected):
        session = self._bus_session(project, bus_role) or ("unbound:" + role["address"])
        pairs = [(event, action) for event, action in selected
                 if event.get("message_id") and event.get("task_id")]
        keys = [self._delivery_key(project, event, session) for event, _ in pairs]
        existing = self.store.wakeup_tasks_for_keys(keys)
        # Adopt v0.7's message-id keyed task instead of creating a second
        # wakeup during an in-place upgrade.  From this point onward the durable
        # composite identity owns retries for that same task id.
        for (event, _), delivery_key in zip(pairs, keys):
            if delivery_key in existing:
                continue
            legacy_key = "agentchat:" + str(event.get("message_id") or "")
            legacy = self.store.task_by_key(WAKEUP_SENDER, legacy_key)
            if legacy is None or legacy.get("target") != role["address"]:
                continue
            self.store.bind_wakeup_keys(legacy["id"], [delivery_key])
            existing[delivery_key] = legacy
        unseen = [(event, action, key) for (event, action), key in zip(pairs, keys)
                  if key not in existing]

        # A failed delivery for an event that is still actionable keeps its
        # original task id and is retried; no new wakeup identity is minted.
        retried = set()
        for item in existing.values():
            if item["id"] in retried or item["state"] in ("queued", "running", "succeeded"):
                continue
            self.store.requeue_wakeup(item["id"], {
                "error": (item.get("result") or {}).get("error") or "wakeup_not_delivered",
                "summary": "Wakeup is still required and will be retried.",
                "retryable": True, "needs_attention": True, "unnotified": True,
            })
            retried.add(item["id"])
            self.wakeups += 1
        if not unseen:
            item = next(iter(existing.values()), None)
            return self.store.get(item["id"]) if item else None

        fresh = [(event, action) for event, action, _ in unseen]
        fresh_keys = [key for _, _, key in unseen]
        digest = hashlib.sha256("\0".join(sorted(fresh_keys)).encode("utf-8")).hexdigest()
        key = "agentchat-batch:" + digest
        prompt = self._prompt_batch(project, bus_role, role, fresh)
        bus_events = [{
            "project_id": project.get("project_id") or project["id"],
            "task_id": event.get("task_id"), "message_id": event.get("message_id"),
            "sequence": event.get("sequence"), "type": event.get("type"),
            "to": event.get("to"), "action": action,
        } for event, action in fresh]
        payload = {"prompt": prompt, "timeout": 1800, "delivery_timeout": 30,
                   "context": [], "reply_mode": "mailbox", "queue_ttl": 86400,
                   "notify": False, "kind": "agent_chat_wakeup",
                   "event_type": fresh[0][0].get("type") if len(fresh) == 1 else "batch",
                   "event_types": sorted({event.get("type") for event, _ in fresh}),
                   "bus_event_id": fresh[0][0].get("message_id") if len(fresh) == 1 else None,
                   "bus_events": bus_events,
                   "project_id": project["id"],
                   "task_ids": sorted({event.get("task_id") for event, _ in fresh}),
                   "recipient_session": session, "dedupe_keys": fresh_keys,
                   "interrupt_on_timeout": False}
        task_id, duplicate = self.store.enqueue_wakeup(
            WAKEUP_SENDER, role["address"], key, payload, fresh_keys)
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
        self._discard_terminal_wakeups(project, events, tasks)
        cursors = self._cursors(project["id"], project["roles"].keys())
        for bus_role, role in project["roles"].items():
            # Reconciliation repairs missed *actionable* work.  A historical
            # cancellation is already terminal and must not be replayed after
            # every fresh install/database rebuild; live scans still deliver a
            # newly appended cancel immediately.
            selected = self._select_actions(events, tasks, bus_role, include_cancel=False)
            if not selected:
                continue
            self._enqueue_wakeup_batch(project, bus_role, role, selected)
            newest = max(
                f"{event.get('sequence', 0):08d}_{event.get('message_id', '')}.json"
                for event, _ in selected)
            if newest > cursors.get(bus_role, ""):
                self._set_cursor(project["id"], bus_role, newest)
                cursors[bus_role] = newest
            self.notify()

    @staticmethod
    def _terminal_verified(task):
        return bool(task and (task.get("status") == "closed" or
                    (task.get("verification") or {}).get("outcome") == "pass"))

    def _discard_terminal_wakeups(self, project, events, tasks):
        terminal = {task_id for task_id, task in tasks.items()
                    if self._terminal_verified(task)}
        if not terminal:
            return []
        event_ids = {event.get("message_id") for event in events
                     if event.get("task_id") in terminal and event.get("message_id")}
        return self.store.discard_bus_wakeups(project["id"], terminal, event_ids)

    def _select_actions(self, events, tasks, bus_role, include_cancel=False):
        """Choose at most one current action per task for one recipient role."""
        selected = {}
        for event in events:
            if event.get("to") != bus_role:
                continue
            task = tasks.get(event.get("task_id"))
            action = self._reconcile_action(event, task)
            if (not action and include_cancel and event.get("type") == "cancel"
                    and task and task.get("status") == "cancelled"):
                action = "cancel"
            if not action:
                continue
            previous = selected.get(event["task_id"])
            if previous is None or event.get("sequence", 0) > previous[0].get("sequence", 0):
                selected[event["task_id"]] = (event, action)
        return sorted(selected.values(), key=lambda pair: pair[0].get("sequence", 0))

    @staticmethod
    def _reconcile_action(event, task):
        """Only the latest event matters; only open states deserve a wakeup."""
        if not task or AgentChatBridge._terminal_verified(task):
            return False
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
        if kind == "verify" and data.get("outcome") == "fail" and status == "pending":
            return "verify_fail"
        return False

    def _sync_projection(self, project, tasks=None):
        """Mirror replayed task state into the store; the bus remains authoritative."""
        try:
            if tasks is None:
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
        project = next((item for item in self.projects if item["id"] == project_id), None)
        if not project:
            return False
        try:
            _, tasks = Bus(project["bus"]).load()
        except (OSError, ValueError):
            return False
        return self._reconcile_action(event, tasks.get(event.get("task_id")))

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

    def _prompt_batch(self, project, bus_role, role, selected):
        """One concise inbox pointer for all actionable events in this scan."""
        if len(selected) == 1:
            event, action = selected[0]
            return self._prompt(project, bus_role, role, None, event, action)
        tasks = []
        for event, action in selected:
            tasks.append(f"{str(event.get('task_id'))[:8]}({action})")
        summary = "、".join(tasks[:8])
        if len(tasks) > 8:
            summary += f"，另 {len(tasks) - 8} 项"
        session = self._bus_session(project, bus_role) or f"{bus_role}-1"
        head = (f"Agents Chat 收件箱本轮有 {len(selected)} 条需要处理的消息，"
                f"已合并为一条摘要：{summary}。请先读取 inbox，并按任务当前状态处理；"
                "已关闭任务无需回复。")
        helper = project["helper"]
        if not helper:
            return head + "\n用项目内的 agent-chat helper 执行 inbox/claim/result/verify。"
        return (head + "\n"
                f'python "{helper}" --project "{project["root"]}" inbox '
                f'--role {bus_role} --session {session}')

    def prepare_wakeup(self, queue_task):
        """Drop or trim a wakeup immediately before transport delivery.

        This closes the race where a result wakeup was queued and the requester
        verified the task before the desktop worker reached it.  It deliberately
        changes only the advisory queue task; role/session lifecycle state is
        never touched.
        """
        payload = queue_task.get("payload") or {}
        if payload.get("kind") != "agent_chat_wakeup":
            return queue_task
        project_id = payload.get("project_id")
        candidates = ([item for item in self.projects if item["id"] == project_id]
                      if project_id else list(self.projects))
        for project in candidates:
            try:
                events, tasks = Bus(project["bus"]).load()
            except (OSError, ValueError):
                continue
            described = list(payload.get("bus_events") or [])
            if described:
                active = []
                for item in described:
                    task = tasks.get(item.get("task_id"))
                    event = next((value for value in events
                                  if value.get("message_id") == item.get("message_id")), item)
                    action = self._reconcile_action(event, task)
                    if (not action and event.get("type") == "cancel" and task
                            and task.get("status") == "cancelled"):
                        action = "cancel"
                    if action:
                        active.append({**item, "action": action})
                if not active:
                    return None
                if len(active) != len(described):
                    role_pair = next(
                        ((name, role) for name, role in project["roles"].items()
                         if role["address"] == queue_task.get("target")), None)
                    if role_pair:
                        bus_role, role = role_pair
                        selected = []
                        for item in active:
                            event = next((value for value in events
                                          if value.get("message_id") == item.get("message_id")), item)
                            selected.append((event, item.get("action") or item.get("type") or "message"))
                        queue_task = dict(queue_task)
                        queue_task["payload"] = dict(payload)
                        queue_task["payload"]["bus_events"] = active
                        queue_task["payload"]["task_ids"] = sorted(
                            {item.get("task_id") for item in active})
                        queue_task["payload"]["prompt"] = self._prompt_batch(
                            project, bus_role, role, selected)
                return queue_task

            # Backward compatibility for already queued v0.7 wakeups.  Their
            # payload only carried a bus event id, so resolve it against each
            # project and discard it when that event's task has since closed.
            legacy_id = payload.get("bus_event_id")
            if legacy_id:
                event = next((value for value in events
                              if value.get("message_id") == legacy_id or
                              (not value.get("message_id") and value.get("task_id") == legacy_id)), None)
                if event is not None:
                    task = tasks.get(event.get("task_id"))
                    action = self._reconcile_action(event, task)
                    if (not action and event.get("type") == "cancel" and task
                            and task.get("status") == "cancelled"):
                        action = "cancel"
                    return queue_task if action else None
        return queue_task

    def _bus_session(self, project, bus_role):
        """The session name this role already uses on the bus, if it has one."""
        role = project["roles"].get(bus_role) or {}
        local_role = role.get("local_role") or bus_role
        try:
            sessions = self.store.list_sessions(project=project["id"], role=local_role)
        except Exception:
            return None
        sessions.sort(key=lambda item: (not item.get("online", False),
                                        item.get("retired", 0),
                                        -item.get("last_seen", 0)))
        for item in sessions:
            if item["id"].startswith("desktop:") and not item.get("retired"):
                return item["id"].split(":", 1)[1]
        return None






