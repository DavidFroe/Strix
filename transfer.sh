#!/usr/bin/env bash
# Strix/transfer.sh — baut ZWEI Debian-Pakete:
#
#   1. strix_<version>_amd64.deb           — Standard-Paket (siehe make.sh)
#                                             ohne persönliche Tokens; das
#                                             gleiche Paket entsteht aus dem
#                                             öffentlichen Repo + Configurator.
#   2. strix-transfer_<version>_amd64.deb  — Personal-Profil: deine aktuelle
#                                             owltrail.conf MIT Token, deine
#                                             persönlichen Dev-Tools (start.sh
#                                             claude.sh git_up.sh transfer.sh),
#                                             spec.md / tagebuch.md.
#                                             Wird auf einem neuen Debian-System
#                                             nach dem Strix-Standard-Install
#                                             installiert und ersetzt die
#                                             Defaults durch deine Configs.
#
# Beide Pakete landen im Strix-Dir. transfer.deb wird NICHT in Git committet
# (siehe .gitignore: *.deb).
#
# Auf dem Ziel-System:
#   sudo apt install ./strix_<version>_amd64.deb           # erst Standard
#   sudo apt install ./strix-transfer_<version>_amd64.deb  # dann Personal
#   bash claude.sh   # → ~/strix-dev/ enthält jetzt deine Dev-Toolchain
#
# Verwendung:
#   bash transfer.sh                # baut beide Pakete
#   VERSION=1.0.1 bash transfer.sh  # explizite Version

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="${VERSION:-$(awk -F'"' '/^version = / {print $2; exit}' Cargo.toml)}"
ARCH="amd64"
PKG_TRANSFER="strix-transfer_${VERSION}_${ARCH}"
PKGDIR_TRANSFER="$SCRIPT_DIR/.build/$PKG_TRANSFER"

echo "╔══════════════════════════════════════════╗"
echo "║   Strix — Transfer-Build (Personal)      ║"
echo "╚══════════════════════════════════════════╝"
echo "Version : $VERSION"
echo ""

# ── 1. Standard-Paket bauen (delegiert an make.sh)
echo "[1/2] Standard-Paket via make.sh ..."
bash "$SCRIPT_DIR/make.sh"

# ── 2. Transfer-Paket bauen — personal-config Overlay
echo ""
echo "[2/2] Transfer-Paket (personal config) bauen ..."

if [ ! -f "$SCRIPT_DIR/owltrail.conf" ]; then
    echo "FEHLER: owltrail.conf existiert nicht — nichts zum Transfer-Verpacken." >&2
    echo "        Kopiere owltrail.conf.example zu owltrail.conf, fülle Token ein." >&2
    exit 1
fi

rm -rf "$PKGDIR_TRANSFER"
install -d "$PKGDIR_TRANSFER/DEBIAN" \
           "$PKGDIR_TRANSFER/etc/strix" \
           "$PKGDIR_TRANSFER/usr/share/strix/dev"

# Personal Backend-Config (Token + IDs)
install -m 600 "$SCRIPT_DIR/owltrail.conf" "$PKGDIR_TRANSFER/etc/strix/owltrail.conf"

# Dev-Tooling: die Scripts dieser Vibecoding-Umgebung
for f in start.sh make.sh claude.sh git_up.sh transfer.sh spec.md tagebuch.md CLAUDE.md; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        install -m 644 "$SCRIPT_DIR/$f" "$PKGDIR_TRANSFER/usr/share/strix/dev/"
        chmod +x "$PKGDIR_TRANSFER/usr/share/strix/dev/$f" 2>/dev/null || true
    fi
done

# Optionale Dotfiles (.env, etc.) — falls vorhanden
[ -f "$SCRIPT_DIR/.env" ] && install -m 600 "$SCRIPT_DIR/.env" \
    "$PKGDIR_TRANSFER/usr/share/strix/dev/.env" || true

# Größe + Abhängigkeit auf das Standard-Strix-Paket gleicher Version
SIZE=$(du -sk "$PKGDIR_TRANSFER/etc" "$PKGDIR_TRANSFER/usr" 2>/dev/null | awk '{s+=$1} END {print s}')

cat > "$PKGDIR_TRANSFER/DEBIAN/control" << EOF
Package: strix-transfer
Version: $VERSION
Architecture: $ARCH
Maintainer: David <davidmg0815@googlemail.com>
Installed-Size: $SIZE
Depends: strix (= $VERSION)
Section: utils
Priority: optional
Description: Strix Vibecoding Personal-Profil
 Persönliche Strix-Config (owltrail.conf mit Token, Dev-Tooling,
 spec.md, tagebuch.md). Installiert die Backend-Routing-Config
 nach /etc/strix/ und legt die Dev-Toolchain unter
 /usr/share/strix/dev/ ab. Auf einem neuen System nach dem
 Standard-Strix-Paket installieren.
 .
 NICHT für öffentliche Distribution gedacht — enthält API-Tokens.
EOF

cat > "$PKGDIR_TRANSFER/DEBIAN/postinst" << 'POST'
#!/bin/bash
# Symlink für schnellen Zugriff auf die Dev-Toolchain im User-Home
SUDO_USER_HOME=""
if [ -n "${SUDO_USER:-}" ]; then
    SUDO_USER_HOME="$(eval echo "~$SUDO_USER")"
fi
if [ -n "$SUDO_USER_HOME" ] && [ -d "$SUDO_USER_HOME" ]; then
    TARGET="$SUDO_USER_HOME/strix-dev"
    if [ ! -e "$TARGET" ]; then
        ln -sf /usr/share/strix/dev "$TARGET"
        chown -h "$SUDO_USER:" "$TARGET" 2>/dev/null || true
    fi
fi
chmod 600 /etc/strix/owltrail.conf 2>/dev/null || true
exit 0
POST
chmod 755 "$PKGDIR_TRANSFER/DEBIAN/postinst"

dpkg-deb --build --root-owner-group "$PKGDIR_TRANSFER" "$SCRIPT_DIR/${PKG_TRANSFER}.deb"

echo ""
echo "✅ Fertig:"
echo "   $SCRIPT_DIR/strix_${VERSION}_amd64.deb           (Standard, public-safe)"
echo "   $SCRIPT_DIR/${PKG_TRANSFER}.deb  (Personal, MIT Tokens)"
echo ""
echo "Transfer auf neues System:"
echo "  scp $SCRIPT_DIR/strix_*.deb $SCRIPT_DIR/strix-transfer_*.deb user@ziel:/tmp/"
echo "  ssh user@ziel"
echo "    sudo apt install /tmp/strix_${VERSION}_amd64.deb"
echo "    sudo apt install /tmp/${PKG_TRANSFER}.deb"
echo "    ~/strix-dev/start.sh"
