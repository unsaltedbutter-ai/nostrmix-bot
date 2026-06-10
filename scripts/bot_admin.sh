#!/usr/bin/env bash
#
# bot_admin.sh — install / remove / inspect the nostrmix-bot macOS LaunchAgent.
#
# The bot runs as `python src/main.py` from the project root with the project's
# venv (./venv) as the interpreter. It installs its own SIGINT/SIGTERM handlers
# and shuts down gracefully, so launchd's normal unload (SIGTERM) is clean.
# All stdout/stderr (the bot logs via Python's logging module) is captured to
# ./logs/.
#
# This is a *user* LaunchAgent in the `gui/<uid>` domain. On a headless Mac
# Mini reached over SSH it only runs at boot if auto-login is enabled for this
# account (System Settings → Users & Groups → Automatically log in as …).
#
# Usage:
#   ./scripts/bot_admin.sh --install     # create logs/, write plist, load + start
#   ./scripts/bot_admin.sh --remove      # stop + unload + delete plist
#   ./scripts/bot_admin.sh --status      # is it loaded? running? recent log lines
#   ./scripts/bot_admin.sh --restart     # kickstart (reload) the running bot
#   ./scripts/bot_admin.sh --logs        # tail -f the live logs
#
set -euo pipefail

# --- Resolve paths (works on any machine; nothing is hardcoded) --------------
# ROOT is the project root — the parent of this script's directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

LABEL="ai.unsaltedbutter.nostrmix-bot"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"
SERVICE="${DOMAIN}/${LABEL}"

PYTHON="${ROOT}/venv/bin/python"
MAIN="${ROOT}/src/main.py"
LOG_DIR="${ROOT}/logs"
OUT_LOG="${LOG_DIR}/nostrmix-bot.out.log"
ERR_LOG="${LOG_DIR}/nostrmix-bot.err.log"

# --- Pretty output helpers ---------------------------------------------------
info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# --- Preconditions ------------------------------------------------------------
preflight_install() {
    [[ -x "${PYTHON}" ]] || die "venv interpreter not found at ${PYTHON}
       Create it first:  cd ${ROOT} && python3.12 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    [[ -f "${MAIN}" ]] || die "entry point not found at ${MAIN}"
    if [[ ! -f "${ROOT}/nostrmix-bot.env" ]]; then
        warn "nostrmix-bot.env not found in ${ROOT}"
        warn "the bot will fail to start without it (copy nostrmix-bot.env.example and fill it in)."
    fi
}

ensure_logs() {
    if [[ ! -d "${LOG_DIR}" ]]; then
        mkdir -p "${LOG_DIR}"
        ok "created ${LOG_DIR}"
    fi
}

# --- plist generation ---------------------------------------------------------
write_plist() {
    mkdir -p "$(dirname "${PLIST}")"
    cat > "${PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${MAIN}</string>
    </array>

    <!-- Run from the project root so nostrmix-bot.env and bot.db resolve. -->
    <key>WorkingDirectory</key>
    <string>${ROOT}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <!-- Flush stdout/stderr immediately so logs are live-tailable. -->
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>PATH</key>
        <string>${ROOT}/venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <!-- Start when the agent is loaded (and at login/boot). -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart on crash, but stay down on a clean stop / remove. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <!-- Don't hammer relaunch if it crash-loops (seconds between attempts). -->
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>${OUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${ERR_LOG}</string>
</dict>
</plist>
PLIST
    ok "wrote ${PLIST}"
}

# --- Commands -----------------------------------------------------------------
cmd_install() {
    info "Installing ${LABEL}"
    preflight_install
    ensure_logs
    write_plist

    # Clear any stale registration so bootstrap doesn't fail with EALREADY.
    launchctl bootout "${SERVICE}" 2>/dev/null || true

    info "Loading agent into ${DOMAIN}"
    launchctl bootstrap "${DOMAIN}" "${PLIST}" \
        || die "launchctl bootstrap failed.
       On a headless Mac Mini this needs an active GUI session for uid $(id -u).
       Enable auto-login for this account, or run from a logged-in session."
    launchctl enable "${SERVICE}" 2>/dev/null || true
    # RunAtLoad starts it; kickstart guarantees it's up even if it was loaded before.
    launchctl kickstart "${SERVICE}" 2>/dev/null || true

    ok "installed and started"
    echo
    cmd_status
}

cmd_remove() {
    info "Removing ${LABEL}"
    if launchctl bootout "${SERVICE}" 2>/dev/null; then
        ok "stopped and unloaded"
    else
        warn "service was not loaded (nothing to stop)"
    fi
    if [[ -f "${PLIST}" ]]; then
        rm -f "${PLIST}"
        ok "deleted ${PLIST}"
    else
        warn "no plist at ${PLIST}"
    fi
    info "logs left in place at ${LOG_DIR} (delete manually if you want them gone)"
}

cmd_status() {
    info "Status of ${LABEL}"

    if [[ -f "${PLIST}" ]]; then
        ok "plist present: ${PLIST}"
    else
        warn "plist NOT installed (${PLIST})"
    fi

    if launchctl print "${SERVICE}" >/tmp/.nostrmix_status 2>/dev/null; then
        local state pid last
        state="$(awk -F'= ' '/^[[:space:]]*state =/ {print $2; exit}' /tmp/.nostrmix_status)"
        pid="$(awk -F'= ' '/^[[:space:]]*pid =/ {print $2; exit}' /tmp/.nostrmix_status)"
        last="$(awk -F'= ' '/last exit code =/ {print $2; exit}' /tmp/.nostrmix_status)"
        ok "loaded in ${DOMAIN}"
        [[ -n "${state:-}" ]] && echo "     state: ${state}"
        [[ -n "${pid:-}"   ]] && echo "     pid:   ${pid}"
        [[ -n "${last:-}"  ]] && echo "     last exit code: ${last}"
        rm -f /tmp/.nostrmix_status
    else
        warn "not loaded in ${DOMAIN}"
        rm -f /tmp/.nostrmix_status 2>/dev/null || true
    fi

    echo
    if [[ -f "${ERR_LOG}" ]]; then
        info "last 15 lines of ${ERR_LOG}:"
        tail -n 15 "${ERR_LOG}" || true
    else
        warn "no log yet at ${ERR_LOG}"
    fi
}

cmd_restart() {
    info "Restarting ${LABEL}"
    [[ -f "${PLIST}" ]] || die "not installed — run --install first"
    launchctl kickstart -k "${SERVICE}" \
        || die "kickstart failed (is it loaded? try --status, or --install)"
    ok "restarted"
}

cmd_logs() {
    [[ -f "${OUT_LOG}" || -f "${ERR_LOG}" ]] || die "no logs yet at ${LOG_DIR}"
    info "tailing ${LOG_DIR} (Ctrl-C to stop)"
    touch "${OUT_LOG}" "${ERR_LOG}"
    tail -f "${OUT_LOG}" "${ERR_LOG}"
}

# --- Arg dispatch -------------------------------------------------------------
[[ $# -eq 0 ]] && usage 1

case "${1:-}" in
    --install)  cmd_install ;;
    --remove)   cmd_remove ;;
    --status)   cmd_status ;;
    --restart)  cmd_restart ;;
    --logs)     cmd_logs ;;
    -h|--help)  usage 0 ;;
    *)          die "unknown option: $1  (try --help)" ;;
esac
