#!/usr/bin/env bash
# Strix/start.sh — startet das lokal gebaute Stable-1.0.7-Binary mit
# owltrail-Adapter. Lokales Dev-Pendant zum installierten `propeller`-Wrapper.
#
# Verwendet das Native-Build (`target/release/strix`) aus DIESEM Worktree
# damit der allerletzte cargo-build sofort live ist, ohne dpkg-Install-Schleife.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG="$SCRIPT_DIR/owltrail.log"
PORT="${OWLTRAIL_PORT:-8081}"
HOST="127.0.0.1"
URL="http://$HOST:$PORT/v1/models"

BIN="$SCRIPT_DIR/target/release/strix"
if [ ! -x "$BIN" ]; then
    BIN="$SCRIPT_DIR/target/x86_64-unknown-linux-musl/release/strix"
fi
if [ ! -x "$BIN" ]; then
    echo "[stuck/start.sh] FEHLER: kein gebautes propeller-Binary gefunden." >&2
    echo "[stuck/start.sh] Erst bauen:  cd $SCRIPT_DIR && cargo build --release" >&2
    exit 2
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "[stuck/start.sh] FEHLER: curl wird benötigt." >&2
    exit 2
fi

# ── owltrail-Adapter — systemd-Service nutzen falls aktiv, sonst selbst starten
USE_SYSTEMD_ADAPTER=""
if systemctl --user is-active owltrail-adapter >/dev/null 2>&1; then
    OWLTRAIL_PID=""
    USE_SYSTEMD_ADAPTER=1
elif curl -sS -o /dev/null --connect-timeout 1 "$URL" 2>/dev/null; then
    OLD_PIDS="$(pgrep -f owltrail_adapter.py 2>/dev/null || true)"
    if [ -n "$OLD_PIDS" ]; then
        # shellcheck disable=SC2086
        kill -TERM $OLD_PIDS 2>/dev/null || true
        for _ in 1 2 3 4 5 6; do
            sleep 0.3
            curl -sS -o /dev/null --connect-timeout 1 "$URL" 2>/dev/null || break
        done
    fi
fi

if [ -z "$USE_SYSTEMD_ADAPTER" ]; then
    python3 "$SCRIPT_DIR/owltrail_adapter.py" --port "$PORT" >> "$LOG" 2>&1 &
    OWLTRAIL_PID=$!
fi

_cleanup() {
    [ -n "${OWLTRAIL_PID:-}" ] && kill -TERM "$OWLTRAIL_PID" 2>/dev/null || true
}
trap _cleanup EXIT INT TERM HUP

# Auf Adapter warten (TCP + HTTP 200)
end=$(( $(date +%s) + 10 ))
while [ "$(date +%s)" -lt "$end" ]; do
    if [ "$(curl -sS -o /dev/null -w '%{http_code}' -m 1 "$URL" 2>/dev/null || echo 000)" = "200" ]; then
        break
    fi
    sleep 0.25
done

# Env-Vars für den Stable-Build
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-sk-owltrail}"
export DEEPSEEK_BASE_URL="http://$HOST:$PORT/v1"
export DEEPSEEK_PROVIDER="${DEEPSEEK_PROVIDER:-deepseek}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-v4-pro}"
export DEEPSEEK_FORCE_HTTP1=1
export OLLAMA_BASE_URL="http://$HOST:$PORT/v1"
export OPENAI_BASE_URL="http://$HOST:$PORT/v1"
export OWLTRAIL_ADAPTER_PATH="$SCRIPT_DIR/owltrail_adapter.py"
export OWLTRAIL_PORT="$PORT"
export OWLTRAIL_LOG="$LOG"

# _propeller-Konfig aus owltrail.conf lesen
_read_conf() {
    python3 -c "
import json
try:
    p = json.load(open('$SCRIPT_DIR/owltrail.conf')).get('_strix', {})
    print(p.get('$1', '$2'))
except Exception:
    print('$2')
" 2>/dev/null || echo "$2"
}

AUTO_APPROVE="$(_read_conf auto_approve true)"
SUDO_PREAUTH="$(_read_conf sudo_preauth true)"
SUDO_PW="$(_read_conf sudo_password '')"

# ── Sudo-Vorauthentifizierung — vor der TUI, solange wir ein echtes Terminal haben.
# Cached ~15 Minuten. Macht propeller-Sessions mit Shell-Tool-Use deutlich
# komfortabler (keine sudo-Prompts mitten in der TUI).
_ASKPASS_SCRIPT=""
if [ -n "$SUDO_PW" ]; then
    _ASKPASS_SCRIPT="$(mktemp /tmp/propeller_askpass.XXXXXX)"
    printf '#!/bin/sh\nprintf "%%s\\n" "%s"\n' "$SUDO_PW" > "$_ASKPASS_SCRIPT"
    chmod 700 "$_ASKPASS_SCRIPT"
    export SUDO_ASKPASS="$_ASKPASS_SCRIPT"
    sudo -A -v 2>/dev/null || true
elif [ "$SUDO_PREAUTH" = "True" ] || [ "$SUDO_PREAUTH" = "true" ] || [ "$SUDO_PREAUTH" = "1" ]; then
    if sudo -n true 2>/dev/null; then
        : # sudo bereits authentifiziert
    else
        printf '\n[stuck] Sudo-Vorauthentifizierung (cached ~15 min, Shell-Tool wird flüssig)\n'
        sudo -v || printf '[stuck] Sudo-Auth fehlgeschlagen — sudo-Befehle fragen ggf. nach\n'
    fi
fi

_cleanup_askpass() {
    [ -n "$_ASKPASS_SCRIPT" ] && rm -f "$_ASKPASS_SCRIPT" 2>/dev/null || true
}
# Existing trap erweitern
_cleanup_orig() { :; }
declare -f _cleanup >/dev/null && _cleanup_orig() { _cleanup; }
_cleanup() {
    _cleanup_orig
    _cleanup_askpass
}
trap _cleanup EXIT INT TERM HUP

EXTRA_FLAGS=""
if [ "$AUTO_APPROVE" = "True" ] || [ "$AUTO_APPROVE" = "true" ] || [ "$AUTO_APPROVE" = "1" ]; then
    EXTRA_FLAGS="--yolo"
fi

# shellcheck disable=SC2086
"$BIN" $EXTRA_FLAGS "$@"
