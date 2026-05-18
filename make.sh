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

# ── 1. musl-Build (statisch, kein GLIBC-Drift)
echo "[1/4] cargo build --release --target x86_64-unknown-linux-musl ..."
TARGET="x86_64-unknown-linux-musl"
cargo build --release --target "$TARGET" 2>&1 | tail -3

BINARY="$SCRIPT_DIR/target/$TARGET/release/strix"
if [ ! -f "$BINARY" ]; then
    BINARY="$SCRIPT_DIR/target/release/strix"
fi
[ ! -f "$BINARY" ] && { echo "FEHLER: kein Binary unter $BINARY"; exit 1; }

# ── 2. Paket-Struktur
echo "[2/4] Paket-Struktur erstellen ..."
rm -rf "$PKGDIR"
install -d "$PKGDIR/DEBIAN" "$PKGDIR/usr/bin" "$PKGDIR/usr/lib/strix" "$PKGDIR/etc/strix"

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
ln -sf /etc/strix/owltrail.conf "$PKGDIR/usr/lib/strix/owltrail.conf"

# Wrapper-Script
cat > "$PKGDIR/usr/bin/strix" << 'WRAPPER'
#!/usr/bin/env bash
LIBDIR=/usr/lib/strix
CONF=/etc/strix/owltrail.conf
LOG="${XDG_RUNTIME_DIR:-/tmp}/strix-owltrail.log"
PORT=8081

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

_port_open() {
    python3 -c "import socket; s=socket.create_connection(('127.0.0.1',$PORT),0.3); s.close()" 2>/dev/null
}

_ensure_owltrail() {
    if ! _port_open; then
        python3 "$LIBDIR/owltrail_adapter.py" --port $PORT --conf "$CONF" >> "$LOG" 2>&1 &
        for _ in $(seq 1 20); do sleep 0.25; _port_open && return 0; done
        echo "strix: Adapter nicht startbar. Log: $LOG" >&2
        exit 1
    fi
}

_watchdog() {
    while kill -0 "${TUI_PID:-0}" 2>/dev/null; do
        if ! _port_open; then
            python3 "$LIBDIR/owltrail_adapter.py" --port $PORT --conf "$CONF" >> "$LOG" 2>&1 &
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

# ── 3. control
echo "[3/4] control-Datei ..."
SIZE=$(du -sk "$PKGDIR/usr" "$PKGDIR/etc" 2>/dev/null | awk '{s+=$1} END {print s}')
cat > "$PKGDIR/DEBIAN/control" << EOF
Package: strix
Version: $VERSION
Architecture: $ARCH
Maintainer: David Froe <davidmg0815@googlemail.com>
Installed-Size: $SIZE
Recommends: python3 (>= 3.8)
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
