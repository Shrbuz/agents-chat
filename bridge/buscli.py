"""`agents-chat bus ...` — the file-bus protocol as first-class CLI commands.

These verbs operate on the project's shared bus directory (the same files the
bundled ``agent-chat`` skill reads), so a session can request, claim, report and
verify work without calling an external script.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .bus import Bus


def _project_option(parser):
    parser.add_argument("--project", dest="project_local", default=argparse.SUPPRESS,
                        help="Project ID that owns the bus (may also be given before the subcommand).")


def _role_session(parser):
    parser.add_argument("--role", required=True)
    parser.add_argument("--session", required=True)


def _token(parser):
    _role_session(parser)
    parser.add_argument("--token", required=True)


def add_commands(sub):
    bus = sub.add_parser("bus", help="File-bus protocol: request/claim/result/verify")
    bus_sub = bus.add_subparsers(dest="bus_command", required=True)

    request = bus_sub.add_parser("request", help="Send a request with acceptance criteria")
    _project_option(request)
    request.add_argument("--from", dest="sender_role", required=True)
    request.add_argument("--to", dest="target_role", required=True)
    request.add_argument("--session", required=True)
    request.add_argument("--title", required=True)
    request.add_argument("--body", required=True)
    request.add_argument("--acceptance", action="append", default=[])
    request.add_argument("--priority", default="P2", choices=("P0", "P1", "P2", "P3"))
    request.add_argument("--due-at")
    request.add_argument("--data-file", help="Extra JSON merged into the request data")

    claim = bus_sub.add_parser("claim", help="Claim a task and receive a lease token")
    _project_option(claim)
    _role_session(claim)
    claim.add_argument("task_id")
    claim.add_argument("--lease-seconds", type=int)

    for name in ("renew", "progress", "blocked"):
        step = bus_sub.add_parser(name)
        _project_option(step)
        _token(step)
        step.add_argument("task_id")
        if name != "renew":
            step.add_argument("--body", required=True)

    release = bus_sub.add_parser("release", help="Hand a claimed task back to the pool")
    _project_option(release)
    _token(release)
    release.add_argument("task_id")
    release.add_argument("--body", required=True, help="What you did and why you are handing it back")

    reassign = bus_sub.add_parser("reassign", help="Requester pulls a claimed task back to the pool")
    _project_option(reassign)
    reassign.add_argument("task_id")
    reassign.add_argument("--role", required=True)
    reassign.add_argument("--session", required=True)
    reassign.add_argument("--body", required=True, help="Why the task is being pulled back")

    result = bus_sub.add_parser("result", help="Report a result with evidence")
    _project_option(result)
    _token(result)
    result.add_argument("task_id")
    result.add_argument("--body", required=True)
    result.add_argument("--evidence", action="append", default=[])
    result.add_argument("--data-file")

    verify = bus_sub.add_parser("verify", help="Accept or reject the reported result")
    _project_option(verify)
    _role_session(verify)
    verify.add_argument("task_id")
    verify.add_argument("--body", required=True)
    verify.add_argument("--outcome", required=True, choices=("pass", "fail"))

    cancel = bus_sub.add_parser("cancel")
    _project_option(cancel)
    _role_session(cancel)
    cancel.add_argument("task_id")
    cancel.add_argument("--body", required=True)

    inbox = bus_sub.add_parser("inbox", help="Unread events for one role and session")
    _project_option(inbox)
    _role_session(inbox)
    inbox.add_argument("--limit", type=int, default=50)

    mark = bus_sub.add_parser("mark-read", help="Mark messages read for this session")
    _project_option(mark)
    _role_session(mark)
    mark.add_argument("message_id", nargs="+")

    status = bus_sub.add_parser("status", help="Open tasks on the bus")
    _project_option(status)
    status.add_argument("--role")
    status.add_argument("--all", action="store_true", help="Include closed and cancelled tasks")

    show = bus_sub.add_parser("show", help="One task with its full event chain")
    _project_option(show)
    show.add_argument("task_id")

    paths = bus_sub.add_parser("paths", help="Where the bus and mailboxes live")
    _project_option(paths)

    validate = bus_sub.add_parser("validate", help="List messages the bus had to skip or repair")
    _project_option(validate)

    repair = bus_sub.add_parser("repair", help="Move unreadable messages aside so the bus reads cleanly")
    _project_option(repair)
    repair.add_argument("--apply", action="store_true",
                        help="actually move the files (default: only report what would move)")


def bus_root(config_path, project):
    """Resolve the shared bus directory declared by a project entry."""
    from .config import read_config
    if not project:
        raise ValueError("project_required: pass --project or run inside a project entry")
    resolved = read_config(config_path)
    entry = resolved.get("projects", {}).get(project)
    if not entry:
        raise ValueError("unknown_project: " + str(project))
    chat = entry.get("agent_chat") or {}
    root = chat.get("bus")
    if not root:
        raise ValueError("project has no agent_chat bus; run setup or link it with project add --bus")
    return root


def _merge_data_file(data, path):
    if not path:
        return data
    extra = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(extra, dict):
        raise ValueError("--data-file must contain a JSON object")
    merged = dict(extra)
    merged.update(data)
    return merged


def _summary(outcome):
    event, task = outcome["event"], outcome["task"]
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "sequence": event["sequence"],
        "message_id": event["message_id"],
        "duplicate": outcome["duplicate"],
        "claim_token": (task.get("claim") or {}).get("token"),
    }


def run(args, root):
    """Execute one bus command against the shared directory at `root`."""
    bus = Bus(root)
    command = args.bus_command
    if command == "request":
        data = _merge_data_file({
            "title": args.title, "body": args.body,
            "acceptance": list(args.acceptance), "priority": args.priority,
        }, args.data_file)
        if args.due_at:
            data["due_at"] = args.due_at
        return _summary(bus.submit("request", args.sender_role, args.session,
                                   recipient=args.target_role, data=data))
    if command == "claim":
        return _summary(bus.submit("claim", args.role, args.session, task_id=args.task_id,
                                   lease_seconds=getattr(args, "lease_seconds", None)))
    if command == "renew":
        return _summary(bus.submit("renew", args.role, args.session, task_id=args.task_id,
                                   token=args.token))
    if command == "release":
        return _summary(bus.submit("release", args.role, args.session, task_id=args.task_id,
                                   token=args.token, data={"body": args.body}))
    if command == "reassign":
        return _summary(bus.submit("reassign", args.role, args.session, task_id=args.task_id,
                                   data={"body": args.body}))
    if command in ("progress", "blocked"):
        return _summary(bus.submit(command, args.role, args.session, task_id=args.task_id,
                                   token=args.token, data={"body": args.body}))
    if command == "result":
        data = _merge_data_file({"body": args.body, "evidence": list(args.evidence)}, args.data_file)
        return _summary(bus.submit("result", args.role, args.session, task_id=args.task_id,
                                   token=args.token, data=data))
    if command == "verify":
        return _summary(bus.submit("verify", args.role, args.session, task_id=args.task_id,
                                   data={"outcome": args.outcome, "body": args.body}))
    if command == "cancel":
        return _summary(bus.submit("cancel", args.role, args.session, task_id=args.task_id,
                                   data={"body": args.body}))
    if command == "inbox":
        return bus.inbox(args.role, args.session, limit=args.limit)
    if command == "mark-read":
        return bus.mark_read(args.role, args.session, args.message_id)
    if command == "status":
        return bus.status(role=args.role, include_closed=args.all)
    if command == "show":
        return bus.show(args.task_id)
    if command == "paths":
        return bus.paths()
    if command == "validate":
        bus.load()
        return {"ok": not bus.problems, "problems": bus.problems}
    if command == "repair":
        return _repair(bus, apply=args.apply)
    raise ValueError("unknown_bus_command: " + str(command))


def _repair(bus, apply=False):
    """Quarantine files the loader had to skip, so nothing is hidden or deleted.

    Keeping the files (rather than deleting them) matters: a bad message is still
    evidence of what a client tried to write.
    """
    import shutil
    import time as _time
    bus.load()
    skipped = [problem for problem in bus.problems if problem["kind"] == "skipped" and problem["file"]]
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    target_dir = bus.root / ("_quarantine-" + stamp)
    moved = []
    for problem in skipped:
        source = Path(problem["file"])
        if not source.is_file():
            continue
        if apply:
            target_dir.mkdir(parents=True, exist_ok=True)
            destination = target_dir / (source.parent.name + "_" + source.name)
            shutil.move(str(source), str(destination))
            moved.append(str(destination))
    return {"applied": bool(apply), "quarantined": moved,
            "would_move": len(skipped) if not apply else len(moved),
            "remaining": [problem for problem in bus.problems if problem["kind"] != "skipped"]}


