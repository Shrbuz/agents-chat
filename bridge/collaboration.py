"""Shared collaboration-mode policy.

The transport path (direct hub queue or durable file bus) and the amount of
coordination ceremony are separate decisions.  These profiles keep that
choice explicit and consistent across setup, prompts, queue tasks and bus
tasks.
"""

MODES = ("fast", "standard", "strict")
DEFAULT_MODE = "standard"

POLICIES = {
    "fast": {
        "route": "queue",
        "claim_required": False,
        "verify_required": False,
        "independent_verification": False,
        "coordination_budget_seconds": 0,
        "guidance": (
            "Small, local, low-risk work stays with one owner. Complete it in one "
            "bounded turn; do not create child tasks or a second acceptance loop."
        ),
    },
    "standard": {
        "route": "bus",
        "claim_required": False,
        "verify_required": True,
        "independent_verification": False,
        "coordination_budget_seconds": 120,
        "guidance": (
            "Keep one owner and hand off only a real dependency. Reuse one task for "
            "rework; an immediate result may be reported without a separate claim."
        ),
    },
    "strict": {
        "route": "bus",
        "claim_required": True,
        "verify_required": True,
        "independent_verification": True,
        "coordination_budget_seconds": 900,
        "guidance": (
            "Use the full leased task lifecycle and independent evidence review for "
            "release, production, security, data, payment or other high-risk work."
        ),
    },
}


def collaboration_mode(value=None):
    mode = value or DEFAULT_MODE
    if mode not in MODES:
        raise ValueError("collaboration_mode_must_be_fast_standard_or_strict")
    return mode


def policy(value=None):
    return POLICIES[collaboration_mode(value)]


def project_mode(config, project):
    entry = (config.get("projects") or {}).get(project) or {}
    return collaboration_mode(entry.get("collaboration_mode"))
