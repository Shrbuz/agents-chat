#!/bin/sh
# Agents Chat installer for macOS and Linux.
#
# Installs one canonical copy under $PREFIX, then links that single copy into
# each existing client skill directory (Codex, Qoder, DSH, Claude Code). An
# upgrade replaces the canonical copy, so every client sees the new version
# without a second install.
#
# No tray application and no desktop-chat binding on these platforms: the hub
# runs as a background service and roles execute as CLI tasks.
set -eu

PREFIX="${AGENTS_CHAT_PREFIX:-$HOME/.local/opt/agents-chat}"
SKILL_NAME="agents-chat-desktop"
SKILL_DIRS="${AGENTS_CHAT_SKILL_DIRS:-$HOME/.codex/skills $HOME/.qoder/skills $HOME/.agents/skills $HOME/.claude/skills}"
DRY=0
SERVICE=1
UNINSTALL=0
SRC=$(cd "$(dirname "$0")" && pwd)

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

  --prefix DIR      Install to DIR (default: ~/.local/opt/agents-chat)
  --no-service      Do not install the background service
  --dry-run         Print what would happen, change nothing
  --uninstall       Remove this install and the symlinks it created
  -h, --help        Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --no-service) SERVICE=0; shift ;;
        --dry-run) DRY=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
run() {
    if [ "$DRY" -eq 1 ]; then say "DRY: $*"; else "$@"; fi
}

# Refuse to operate on obviously wrong prefixes; this script deletes only
# inside PREFIX and never touches a client directory that holds real files.
case "$PREFIX" in
    ""|"/"|"$HOME") echo "refusing unsafe prefix: $PREFIX" >&2; exit 2 ;;
esac

if [ "$UNINSTALL" -eq 1 ]; then
    say "uninstalling from $PREFIX"
    for dir in $SKILL_DIRS; do
        target="$dir/$SKILL_NAME"
        if [ -L "$target" ]; then
            current=$(readlink "$target")
            case "$current" in
                "$PREFIX"/*) say "removing link $target"; run rm -f "$target" ;;
                *) say "leaving foreign link $target -> $current" ;;
            esac
        elif [ -e "$target" ]; then
            say "leaving real directory $target (not ours to delete)"
        fi
    done
    plist="$HOME/Library/LaunchAgents/com.agents-chat.hub.plist"
    if [ -f "$plist" ]; then
        say "removing launch agent"
        command -v launchctl >/dev/null 2>&1 && run launchctl unload "$plist" || true
        run rm -f "$plist"
    fi
    if [ -d "$PREFIX" ]; then
        say "removing $PREFIX"
        run rm -rf "$PREFIX"
    fi
    say "done"
    exit 0
fi

# -- prerequisites -----------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required; install it first (macOS: xcode-select --install)" >&2
    exit 1
fi
PY=$(command -v python3)
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "python3 >= 3.11 is required, found: $("$PY" -V 2>&1)" >&2
    exit 1
fi
if [ ! -f "$SRC/bridge.py" ]; then
    echo "run this script from an extracted Agents Chat package (bridge.py not found)" >&2
    exit 1
fi

say "Agents Chat -> $PREFIX"
say "python      -> $PY"

# -- files -------------------------------------------------------------------
run mkdir -p "$PREFIX"
say "copying application files"
if [ "$DRY" -eq 1 ]; then
    say "DRY: copy bridge/, bridge.py, VERSION, config.example.json, tool-skill/"
else
    ( cd "$SRC" && tar cf - --exclude=__pycache__ bridge bridge.py VERSION config.example.json tool-skill ) \
        | tar xf - -C "$PREFIX"
fi

if [ ! -f "$PREFIX/config.local.json" ]; then
    say "creating config.local.json from the example"
    run cp "$PREFIX/config.example.json" "$PREFIX/config.local.json"
fi

# -- one canonical skill, linked into every existing client ------------------
SKILL_SRC="$PREFIX/tool-skill"
SKILL="$PREFIX/skill"
say "preparing canonical skill copy"
run rm -rf "$SKILL"
run mkdir -p "$SKILL"
if [ "$DRY" -eq 0 ]; then
    cp "$SKILL_SRC/SKILL.md" "$SKILL/SKILL.md"
    [ -d "$SKILL_SRC/references" ] && cp -R "$SKILL_SRC/references" "$SKILL/references"
    [ -d "$SKILL_SRC/scripts" ] && cp -R "$SKILL_SRC/scripts" "$SKILL/scripts"
    printf '{"python":"%s","source":"path","written":"%s"}\n' "$PY" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$SKILL/runtime.json"
fi

for dir in $SKILL_DIRS; do
    [ -d "$dir" ] || continue
    target="$dir/$SKILL_NAME"
    if [ -L "$target" ]; then
        current=$(readlink "$target")
        if [ "$current" = "$SKILL" ]; then
            say "already linked: $target"
        else
            say "skipped (link points elsewhere): $target -> $current"
        fi
    elif [ -e "$target" ]; then
        say "skipped (real directory exists, left untouched): $target"
    else
        say "linking $target -> $SKILL"
        run ln -s "$SKILL" "$target"
        # Some shells on Windows resolve ln -s to a copy; a copy would silently
        # defeat the single-copy upgrade model, so refuse to pretend it worked.
        if [ "$DRY" -eq 0 ] && [ ! -L "$target" ]; then
            say "WARNING: $target is not a symlink; this environment copied instead of linking"
            say "         upgrades will not reach this client; reinstall the skill manually"
        fi
    fi
done

# -- background service ------------------------------------------------------
if [ "$SERVICE" -eq 1 ]; then
    if [ "$(uname -s)" = "Darwin" ]; then
        plist="$HOME/Library/LaunchAgents/com.agents-chat.hub.plist"
        say "installing launch agent: $plist"
        if [ "$DRY" -eq 1 ]; then
            say "DRY: write $plist"
        else
            mkdir -p "$HOME/Library/LaunchAgents"
            cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agents-chat.hub</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string>
    <string>$PREFIX/bridge.py</string>
    <string>serve</string>
    <string>--config</string><string>$PREFIX/config.local.json</string>
    <string>--state</string><string>$PREFIX/.bridge</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$PREFIX/.bridge/hub.log</string>
</dict></plist>
EOF
            launchctl unload "$plist" 2>/dev/null || true
            launchctl load -w "$plist"
        fi
    else
        say "Linux: create a systemd user unit if you want the hub to start at login:"
        say "  $PY $PREFIX/bridge.py serve --config $PREFIX/config.local.json --state $PREFIX/.bridge"
    fi
fi

say ""
say "installed. Next steps:"
say "  1. $PY $PREFIX/bridge.py status"
say "  2. in your project chat: 用 agents-chat-desktop 技能把这个项目接成多角色协作"
say ""
say "Skills are linked, so a later upgrade only replaces $SKILL."
