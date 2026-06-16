#!/usr/bin/env bash
# Strix/make.sh — baut das 1.0.7-.deb-Paket aus DIESEM Worktree.
# Self-contained: braucht keinen Top-Level-make.sh. Ergebnis liegt im
# Strix-Verzeichnis als strix_<VERSION>_amd64.deb.
#
#   bash make.sh                  # nutzt Cargo-Version aus Cargo.toml
#   VERSION=1.0.7 bash make.sh    # explizit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Rust auf grosse Platte (root-Partition / ist nur 20G, fast voll)
export RUSTUP_HOME="${RUSTUP_HOME:-/home/david/Schreibtisch/Platte/rust/rustup}"
export CARGO_HOME="${CARGO_HOME:-/home/david/Schreibtisch/Platte/rust/cargo}"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-/home/david/Schreibtisch/Platte/rust/strix-target}"
export PATH="$CARGO_HOME/bin:$PATH"

# Version aus Cargo.toml lesen wenn nicht explizit gesetzt
VERSION="${VERSION:-$(awk -F'"' '/^version = / {print $2; exit}' Cargo.toml)}"
ARCH="amd64"
PKG="strix_${VERSION}_${ARCH}"
PKGDIR="$SCRIPT_DIR/.build/$PKG"

echo "╔══════════════════════════════════════════╗"
echo "║   Strix — Debian-Build         ║"
echo "╚══════════════════════════════════════════╝"
echo "Version : $VERSION"
echo "Output  : $SCRIPT_DIR/$PKG.deb"
echo ""

# ── 0. Altes .deb archivieren ──────────────────────────────────────────────
OLD_RELEASES="$SCRIPT_DIR/old_releases"
if [ -f "$SCRIPT_DIR/$PKG.deb" ]; then
    mkdir -p "$OLD_RELEASES"
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    ARCHIVE_NAME="strix_${VERSION}_${TIMESTAMP}_amd64.deb"
    cp "$SCRIPT_DIR/$PKG.deb" "$OLD_RELEASES/$ARCHIVE_NAME"
    echo "[0/5] Archiviert: $ARCHIVE_NAME"
    echo "       → old_releases/ ($(ls "$OLD_RELEASES" | wc -l) Dateien)"
else
    echo "[0/5] Kein altes .deb zum Archivieren."
fi

# ── 1. musl-Build (statisch, kein GLIBC-Drift)
TARGET="x86_64-unknown-linux-musl"

# Voraussetzungen pruefen + ggf. installieren
if ! rustup target list --installed 2>/dev/null | grep -qx "$TARGET"; then
    echo "[prep] rustup target add $TARGET ..."
    rustup target add "$TARGET"
fi
if ! command -v musl-gcc >/dev/null 2>&1; then
    echo "[prep] musl-tools fehlt — installiere via apt ..."
    sudo apt-get update -qq
    sudo apt-get install -y musl-tools
fi

echo "[1/4] cargo build --release --target $TARGET ..."
cargo build --release --target "$TARGET" 2>&1 | tail -3

BINARY="$CARGO_TARGET_DIR/$TARGET/release/strix"
if [ ! -s "$BINARY" ]; then
    BINARY="$CARGO_TARGET_DIR/release/strix"
fi
if [ ! -s "$BINARY" ]; then
    echo "FEHLER: kein Binary unter $CARGO_TARGET_DIR/$TARGET/release/strix" >&2
    echo "  (musl-Build ggf. fehlgeschlagen — 0-Byte-Datei erkannt)" >&2
    exit 1
fi

# ── 2. Paket-Struktur
echo "[2/4] Paket-Struktur erstellen ..."
rm -rf "$PKGDIR"
install -d "$PKGDIR/DEBIAN" "$PKGDIR/usr/bin" "$PKGDIR/usr/lib/strix" "$PKGDIR/etc/strix" "$PKGDIR/etc/strix/profiles"

install -m 755 "$BINARY" "$PKGDIR/usr/lib/strix/strix-tui"
install -m 644 "$SCRIPT_DIR/owltrail.py"             "$PKGDIR/usr/lib/strix/"
install -m 644 "$SCRIPT_DIR/owltrail_adapter.py"     "$PKGDIR/usr/lib/strix/"
install -m 644 "$SCRIPT_DIR/owltrail.conf"           "$PKGDIR/etc/strix/owltrail.conf"
install -m 644 "$SCRIPT_DIR/strix.conf"          "$PKGDIR/etc/strix/strix.conf"
install -m 644 "$SCRIPT_DIR/strix_agent_system.md"      "$PKGDIR/etc/strix/strix_agent_system.md"
install -m 644 "$SCRIPT_DIR/strix_start_prompt.md"      "$PKGDIR/etc/strix/strix_start_prompt.md"
install -m 644 "$SCRIPT_DIR/strix_quote_adjectives.csv" "$PKGDIR/etc/strix/strix_quote_adjectives.csv"
install -m 644 "$SCRIPT_DIR/strix_prompts.csv"          "$PKGDIR/etc/strix/strix_prompts.csv"
install -m 644 "$SCRIPT_DIR/strix_tooltips.csv"         "$PKGDIR/etc/strix/strix_tooltips.csv"
install -m 644 "$SCRIPT_DIR/profiles/webUIAgent.md"     "$PKGDIR/etc/strix/profiles/webUIAgent.md"
install -m 644 "$SCRIPT_DIR/profiles/webUIMaster.md"    "$PKGDIR/etc/strix/profiles/webUIMaster.md"
ln -sf /etc/strix/owltrail.conf "$PKGDIR/usr/lib/strix/owltrail.conf"

# Wrapper-Script
cat > "$PKGDIR/usr/bin/strix" << 'WRAPPER'
#!/usr/bin/env bash
LIBDIR=/usr/lib/strix
CONF=/etc/strix/owltrail.conf
LOG="${XDG_RUNTIME_DIR:-/tmp}/strix-owltrail.log"

# ── Port aus owltrail.conf lesen ───────────────────────────────────────────
_read_conf_port() {
    python3 -c "
import json, sys
try:
    cfg = json.load(open('$CONF'))
    print(cfg.get('listen_port', 8081))
except Exception:
    print(8081)
" 2>/dev/null || echo 8081
}
PORT=$(_read_conf_port)

_check_deps() {
    local missing=()
    command -v python3 >/dev/null 2>&1 || missing+=(python3)
    [ -f /etc/ssl/certs/ca-certificates.crt ] || missing+=(ca-certificates)
    [ ${#missing[@]} -eq 0 ] && return 0
    echo "strix: fehlende Pakete: ${missing[*]} — installiere..."
    local APT="sudo apt-get"; [ "$(id -u)" = "0" ] && APT="apt-get"
    $APT update -qq 2>/dev/null || true
    $APT install -y "${missing[@]}" || { echo "strix: FEHLER Installation"; exit 1; }
}

# Prüft ob der Adapter auf PORT läuft UND /v1/models HTTP 200 liefert.
_adapter_ok() {
    python3 -c "
import urllib.request, sys
try:
    req = urllib.request.Request('http://127.0.0.1:$PORT/v1/models',
                                  headers={'Accept': 'application/json'})
    resp = urllib.request.urlopen(req, timeout=2)
    # Adapter antwortet mit JSON. Alles andere ist ein Fremd-Dienst.
    ct = resp.headers.get('Content-Type', '')
    if 'application/json' not in ct:
        sys.exit(2)  # Fremd-Dienst
    sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
"
}

_ensure_owltrail() {
    if _adapter_ok; then
        return 0  # Adapter läuft bereits
    fi

    # Prüfen ob Port durch Fremd-Dienst belegt ist
    local port_status
    port_status=$(python3 -c "
import urllib.request, sys
try:
    req = urllib.request.Request('http://127.0.0.1:$PORT/', headers={'Accept': 'text/html'})
    resp = urllib.request.urlopen(req, timeout=2)
    ct = resp.headers.get('Content-Type', '')
    body = resp.read(512).decode('utf-8', errors='replace')
    # Falls JSON → owltrail-Adapter (der antwortet nur auf /v1/ Pfade)
    if 'application/json' in ct:
        sys.exit(0)
    # HTML → Fremd-Dienst
    title = ''
    import re
    m = re.search(r'<title>(.*?)</title>', body, re.I)
    if m:
        title = m.group(1).strip()
    print(f'Port $PORT ist belegt durch: {title or \"Unbekannter Web-Dienst\"}')
    print()
    print('Lösungen:')
    print('  1. Anderen Dienst stoppen, der Port $PORT nutzt')
    print('  2. oder: listen_port in /etc/strix/owltrail.conf ändern (z.B. 8082)')
    sys.exit(1)
except Exception:
    sys.exit(0)
" 2>/dev/null)
    if [ $? -ne 0 ] && [ -n "$port_status" ]; then
        echo "$port_status" >&2
        exit 1
    fi

    # Log-Datei vorab anlegen
    touch "$LOG" 2>/dev/null || true

    python3 "$LIBDIR/owltrail_adapter.py" --port $PORT --conf "$CONF" --log "$LOG" >> "$LOG" 2>&1 &
    local ADAPTER_PID=$!

    # Warte bis zu 8s auf HTTP 200 vom Adapter
    for _ in $(seq 1 40); do
        sleep 0.2
        if _adapter_ok; then
            return 0
        fi
    done

    # Fehlerdiagnose
    echo "strix: Adapter auf Port $PORT antwortet nicht (HTTP 200 mit JSON erwartet)." >&2
    if [ -s "$LOG" ]; then
        echo "strix: Letzte 20 Zeilen aus $LOG:" >&2
        tail -20 "$LOG" >&2
    else
        echo "strix: Log $LOG ist leer." >&2
        echo "strix: Prüfe: python3 $LIBDIR/owltrail_adapter.py --port $PORT --conf $CONF" >&2
    fi
    kill "$ADAPTER_PID" 2>/dev/null || true
    exit 1
}

_watchdog() {
    while kill -0 "${TUI_PID:-0}" 2>/dev/null; do
        if ! _adapter_ok; then
            touch "$LOG" 2>/dev/null || true
            python3 "$LIBDIR/owltrail_adapter.py" --port $PORT --conf "$CONF" --log "$LOG" >> "$LOG" 2>&1 &
        fi
        sleep 2
    done
}

case "${1:-}" in --help|-h|--version|-V) exec "$LIBDIR/strix-tui" "$@" ;; esac

_check_deps
_ensure_owltrail

export DEEPSEEK_BASE_URL="http://127.0.0.1:$PORT/v1"
export DEEPSEEK_API_KEY="sk-strix"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-v4-pro}"
export DEEPSEEK_FORCE_HTTP1=1
export OWLTRAIL_ADAPTER_PATH="$LIBDIR/owltrail_adapter.py"
export OWLTRAIL_PORT="$PORT"
export OWLTRAIL_LOG="$LOG"

AUTO_APPROVE="$(python3 -c "
import json
try: print(json.load(open('$CONF')).get('_strix', {}).get('auto_approve', False))
except Exception: print(False)
" 2>/dev/null || echo False)"

EXTRA_FLAGS=""
[ "$AUTO_APPROVE" = "True" ] && EXTRA_FLAGS="--yolo"

"$LIBDIR/strix-tui" $EXTRA_FLAGS "$@" &
TUI_PID=$!
_watchdog &
wait $TUI_PID
WRAPPER
chmod 755 "$PKGDIR/usr/bin/strix"

# ── strix-server Wrapper (Web-UI-Server)
cat > "$PKGDIR/usr/bin/strix-server" << 'SERVER'
#!/usr/bin/env bash
# strix-server — startet den Strix-HTTP-Server inkl. eingebettetem Web-UI.
# Workspace = cwd, Port walks-up ab 7878. Druckt URL im Banner.
# Default: bindet auf 0.0.0.0 (LAN/VPN-erreichbar).
#
#   strix-server                                # Default-Profile 'web'
#   strix-server --profile webUIAgent           # Worker-Agent-Entwicklung
#   strix-server --profile webUIMaster          # Master/Orchestrator-Entwicklung
#   HOST=127.0.0.1 strix-server                 # nur lokal
#   PORT=8000 strix-server                      # Wunsch-Port (walks up)
#   PROFILE=web strix-server                    # Env-Var-Alternative
#   PROFILE='' strix-server                     # ohne Profile starten
#   MODEL=ollama-qwen3.6-27b strix-server       # Default-Modell
set -u

# CLI-Flags parsen (--profile, --host, --port, --model). Alles andere wird
# unverändert an strix-tui durchgereicht.
EXTRA_ARGS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --profile)        PROFILE="${2:-}"; shift 2 ;;
        --profile=*)      PROFILE="${1#--profile=}"; shift ;;
        --host)           HOST="${2:-}"; shift 2 ;;
        --host=*)         HOST="${1#--host=}"; shift ;;
        --port)           PORT="${2:-}"; shift 2 ;;
        --port=*)         PORT="${1#--port=}"; shift ;;
        --model)          MODEL="${2:-}"; shift 2 ;;
        --model=*)        MODEL="${1#--model=}"; shift ;;
        --help|-h)
            sed -n '2,/^set -u/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) EXTRA_ARGS+=("$1"); shift ;;
    esac
done

LIBDIR=/usr/lib/strix
CONF=/etc/strix/owltrail.conf
LOG="${XDG_RUNTIME_DIR:-/tmp}/strix-owltrail.log"
OWLTRAIL_PORT="${OWLTRAIL_PORT:-8081}"
OWLTRAIL_URL="http://127.0.0.1:$OWLTRAIL_PORT/v1/models"

_port_listening() {
    if command -v ss >/dev/null 2>&1; then
        ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
    else
        curl -sS -m 1 -o /dev/null "http://127.0.0.1:$1/health" 2>/dev/null
    fi
}

# owltrail-Adapter (port 8081) sicherstellen
if ! curl -sS -o /dev/null --connect-timeout 1 "$OWLTRAIL_URL" 2>/dev/null; then
    python3 "$LIBDIR/owltrail_adapter.py" --port "$OWLTRAIL_PORT" --conf "$CONF" --log "$LOG" \
        >> "$LOG" 2>&1 &
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 0.3
        curl -sS -o /dev/null --connect-timeout 1 "$OWLTRAIL_URL" 2>/dev/null && break
    done
fi

export DEEPSEEK_BASE_URL="http://127.0.0.1:$OWLTRAIL_PORT/v1"
export DEEPSEEK_API_KEY="sk-strix"
export DEEPSEEK_MODEL="${MODEL:-${DEEPSEEK_MODEL:-deepseek-v4-pro}}"
export DEEPSEEK_FORCE_HTTP1=1
export OLLAMA_BASE_URL="http://127.0.0.1:$OWLTRAIL_PORT/v1"
export OPENAI_BASE_URL="http://127.0.0.1:$OWLTRAIL_PORT/v1"

# Default: bind 0.0.0.0 (LAN/VPN-erreichbar). Wer es nur lokal will:
# HOST=127.0.0.1 strix-server
HOST="${HOST:-0.0.0.0}"
START_PORT="${PORT:-7878}"
PORT="$START_PORT"
while _port_listening "$PORT"; do
    PORT=$((PORT + 1))
    if [ "$PORT" -gt 8200 ]; then
        echo "strix-server: kein freier Port ab $START_PORT bis 8200." >&2
        exit 3
    fi
done

# Profile (Default 'web')
PROFILE="${PROFILE-web}"
PROFILE_LABEL="${PROFILE:-(none)}"

# URL-Zeilen — bei 0.0.0.0 alle IPs auflisten
if [ "$HOST" = "0.0.0.0" ]; then
    IPS="$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')"
    [ -z "$IPS" ] && IPS="$(hostname -I 2>/dev/null || echo localhost)"
    URL_LINES="║   URL       : http://localhost:$PORT/ui"
    for ip in $IPS; do
        URL_LINES="$URL_LINES
║              : http://$ip:$PORT/ui"
    done
else
    URL_LINES="║   URL       : http://$HOST:$PORT/ui"
fi

cat >&2 <<EOF
╔══════════════════════════════════════════════════════════╗
║   Strix Web UI
$URL_LINES
║   API       : http://${HOST}:$PORT/v1
║   Workspace : $(pwd)
║   Profile   : $PROFILE_LABEL
║   Model     : $DEEPSEEK_MODEL
║   Stop      : Ctrl-C
╚══════════════════════════════════════════════════════════╝
EOF

PROFILE_ARGS=()
[ -n "$PROFILE" ] && PROFILE_ARGS=(--profile "$PROFILE")
exec "$LIBDIR/strix-tui" "${PROFILE_ARGS[@]}" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" serve --http --host "$HOST" --port "$PORT" --insecure
SERVER
chmod 755 "$PKGDIR/usr/bin/strix-server"

# ── 3. control
echo "[3/4] control-Datei ..."
SIZE=$(du -sk "$PKGDIR/usr" "$PKGDIR/etc" 2>/dev/null | awk '{s+=$1} END {print s}')
cat > "$PKGDIR/DEBIAN/control" << EOF
Package: strix
Version: $VERSION
Architecture: $ARCH
Maintainer: David Froe <davidmg0815@googlemail.com>
Installed-Size: $SIZE
Depends: python3 (>= 3.8)
Section: utils
Priority: optional
Homepage: https://github.com/DavidFroe/Strix
Description: Strix — Terminal-KI-Assistent mit OwlTrail-Backend
 Alltagstauglicher Terminal-KI-Assistent (Fork von deepseek-tui) mit
 owltrail-Routing für DeepSeek/Claude/Grok/Gemini/GPT/Qwen.
 Default-Mode: Agent, Default-Model: deepseek-v4-pro.
 Tab/Shift+Tab cyclet: Chat → Plan → Agent → Shell → Yolo → Auto.
 .
 Konfiguration: /etc/strix/owltrail.conf  +  /etc/strix/strix.conf
EOF

cat > "$PKGDIR/DEBIAN/postinst" << 'EOF'
#!/bin/bash
chmod 644 /etc/strix/owltrail.conf 2>/dev/null || true
exit 0
EOF
chmod 755 "$PKGDIR/DEBIAN/postinst"

cat > "$PKGDIR/DEBIAN/prerm" << 'EOF'
#!/bin/bash
pkill -f "owltrail_adapter.py" 2>/dev/null || true
exit 0
EOF
chmod 755 "$PKGDIR/DEBIAN/prerm"

# ── 4. .deb bauen
echo "[4/4] dpkg-deb --build ..."
dpkg-deb --build --root-owner-group "$PKGDIR" "$SCRIPT_DIR/${PKG}.deb"

echo ""
echo "✅ Fertig: $SCRIPT_DIR/${PKG}.deb"
echo ""
echo "Installieren:   sudo apt install ./${PKG}.deb"
echo "Deploy 11.0.0.12:  bash ../test_server.sh 11.0.0.12  (nimmt das jüngste .deb im Workspace-Root)"
