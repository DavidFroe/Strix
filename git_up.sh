#!/usr/bin/env bash
# Strix/git_up.sh — committet den aktuellen Stand und pusht zu origin/main.
# Verwendung:
#   bash git_up.sh                       # Default-Message "wip: <timestamp>"
#   bash git_up.sh "kommit-message"      # explizite Message
#   bash git_up.sh -m "kommit-message"   # mit -m flag (Kompatibilität)
#   bash git_up.sh --no-push             # nur committen, nicht pushen
#
# Es wird `git add -A` gemacht — gitignored bleibt gitignored.
# owltrail.conf, *.deb, .build/ etc. sind in .gitignore und kommen NICHT mit.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NO_PUSH=""
MSG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --no-push) NO_PUSH=1; shift ;;
        -m|--message) MSG="$2"; shift 2 ;;
        *) MSG="$1"; shift ;;
    esac
done

if [ -z "$MSG" ]; then
    MSG="wip: $(date +'%Y-%m-%d %H:%M')"
fi

echo "╔══════════════════════════════════════════╗"
echo "║   Strix — git_up                         ║"
echo "╚══════════════════════════════════════════╝"
echo "  Branch  : $(git rev-parse --abbrev-ref HEAD)"
echo "  Message : $MSG"
[ -n "$NO_PUSH" ] && echo "  Push    : skipped (--no-push)"
echo ""

# Verhindere committen sensibler Files (owltrail.conf sollte gitignored sein,
# aber doppelt-prüfen schadet nicht).
if git status --short | awk '{print $2}' | grep -qE '^owltrail\.conf$|^\.env$|sudo_password'; then
    echo "FEHLER: sensible Files erkannt im Stage. Abbruch." >&2
    git status --short | grep -E 'owltrail\.conf|\.env' >&2
    exit 2
fi

git add -A

if git diff --cached --quiet; then
    echo "Nichts zu committen — Working-Tree ist sauber."
    if [ -z "$NO_PUSH" ] && [ "$(git rev-list @{u}..HEAD 2>/dev/null | wc -l)" -gt 0 ]; then
        echo "Aber lokale Commits voraus — push trotzdem."
    else
        exit 0
    fi
else
    git commit -m "$MSG" || { echo "FEHLER: commit failed" >&2; exit 1; }
fi

if [ -n "$NO_PUSH" ]; then
    echo "✓ committed (kein push)"
    exit 0
fi

if ! git remote get-url origin >/dev/null 2>&1; then
    echo "Hinweis: kein origin-Remote konfiguriert; übersprungen."
    exit 0
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo ""
echo "Pushing → origin/$BRANCH ..."
git push -u origin "$BRANCH"
echo "✓ done"
