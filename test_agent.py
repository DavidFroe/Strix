#!/usr/bin/env python3
"""
Propeller Development Test Agent
===================================
Startet den Propeller-Entwicklungsstand (target/release/propeller via start.sh),
führt automatisierte UI-Checks durch und erstellt einen Testbericht.

Verwendung:
    python3 test_agent.py              # normaler Lauf
    python3 test_agent.py --verbose    # zeigt vollen Bildschirm-Dump

Rückgabewert:
    0 → alle Tests grün
    1 → mindestens ein Test fehlgeschlagen

Entwicklungs-Chain: Dieses Skript wird von Claude / Sub-Agenten ausgeführt.
Neue Features in der TUI müssen hier bestehen bevor sie als "fertig" gelten.
"""

import argparse
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

try:
    import pyte
except ImportError:
    print("FEHLER: pyte nicht installiert. Bitte: pip install pyte")
    sys.exit(2)

# ── Konstanten ────────────────────────────────────────────────────────────────
COLS, ROWS  = 200, 50
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
START_SH    = os.path.join(SCRIPT_DIR, "start.sh")
REPORT_FILE = os.path.join(SCRIPT_DIR, "test_report.md")

# Wartezeiten (Sekunden)
SPLASH_WAIT     = 5.0   # Splash-Erscheinen abwarten
MAIN_UI_WAIT    = 8.0   # Hauptoberfläche nach Splash
KEYPRESS_WAIT   = 1.0   # Nach Tastendruck warten
CYCLE_WAIT      = 1.5   # Nach Ctrl+N warten
ANSWER_WAIT     = 35.0  # Auf Modellantwort warten

# ── Screen-Hilfsfunktionen ────────────────────────────────────────────────────
screen = pyte.Screen(COLS, ROWS)
stream = pyte.ByteStream(screen)


def render() -> str:
    return "\n".join(line.rstrip() for line in screen.display)


def drain(fd: int, seconds: float):
    deadline = time.time() + seconds
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        r, _, _ = select.select([fd], [], [], min(remaining, 0.1))
        if not r:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        stream.feed(chunk)


def screen_contains(text: str, case_sensitive: bool = False) -> bool:
    rendered = render()
    if case_sensitive:
        return text in rendered
    return text.lower() in rendered.lower()


def find_in_screen(text: str, case_sensitive: bool = False) -> Optional[tuple[int, int]]:
    """Gibt (Zeile, Spalte) des ersten Vorkommens zurück, oder None."""
    rendered = render()
    for li, line in enumerate(rendered.splitlines()):
        haystack = line if case_sensitive else line.lower()
        needle   = text  if case_sensitive else text.lower()
        col = haystack.find(needle)
        if col != -1:
            return (li, col)
    return None


def screenshot_lines(last_n: int = 30) -> list[str]:
    return [ln.rstrip() for ln in screen.display[-last_n:]]


# ── Testergebnis ──────────────────────────────────────────────────────────────
@dataclass
class TestResult:
    name:    str
    passed:  bool
    detail:  str = ""
    snippet: str = ""   # relevanter Bildschirmausschnitt

@dataclass
class TestSuite:
    results: list[TestResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def add(self, name: str, passed: bool, detail: str = "", snippet: str = ""):
        self.results.append(TestResult(name, passed, detail, snippet))
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}  {name}")
        if detail:
            print(f"         {detail}")

    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    def summary(self) -> str:
        passed = sum(1 for r in self.results if r.passed)
        total  = len(self.results)
        return f"{passed}/{total} Tests bestanden"


# ── Testfälle ─────────────────────────────────────────────────────────────────

def check_splash(suite: TestSuite, fd: int, verbose: bool):
    """Splash-Screen erscheint mit PROPELLER-Logo."""
    drain(fd, SPLASH_WAIT)
    snap = render()

    # Splash zeigt PROPELLER (Logo in Box-Drawing-Blöcken → mindestens
    # die ASCII-Buchstaben P R O P E L L E R müssen irgendwo stehen)
    has_logo = (
        screen_contains("PROPELLER") or
        # Box-Drawing-Logo: suche charakteristischen Teilstring
        "██" in snap or
        "╔" in snap
    )
    snippet = "\n".join(snap.splitlines()[:15])
    suite.add(
        "Splash-Screen erscheint",
        has_logo,
        "PROPELLER-Logo oder Block-Zeichen gefunden" if has_logo
        else "Weder PROPELLER noch Box-Zeichnung im Startbild",
        snippet,
    )

    # "Press any key" Hinweis (erscheint nach 600 ms)
    drain(fd, 1.0)
    has_hint = screen_contains("press any key") or screen_contains("any key")
    suite.add(
        "Splash: 'Press any key' Hinweis",
        has_hint,
        "'Press any key' nach 600 ms sichtbar" if has_hint
        else "Kein Tastaturhinweis im Splash gefunden",
    )


def dismiss_splash_and_wait(fd: int):
    """Splash mit Leertaste schließen, dann auf Haupt-UI warten."""
    time.sleep(0.8)          # mindestens 600 ms Mindestanzeigezeit
    os.write(fd, b" ")       # beliebige Taste
    drain(fd, MAIN_UI_WAIT)


def check_propeller_mode(suite: TestSuite, verbose: bool):
    """Footer / Header zeigt 'propeller' als aktiven Modus."""
    snap = render()
    found = find_in_screen("propeller")
    suite.add(
        "Default-Modus: propeller",
        found is not None,
        f"'propeller' in Zeile {found[0]+1}" if found
        else "'propeller' nicht im UI gefunden – prüfe ~/.config/deepseek/settings.toml",
        "\n".join(snap.splitlines()[-5:]),
    )


def check_max_effort(suite: TestSuite, verbose: bool):
    """Header-Chip zeigt 'max' als Reasoning-Effort."""
    snap = render()
    # Header ist typischerweise in den ersten 3 Zeilen
    header_lines = "\n".join(snap.splitlines()[:4])
    found_max    = "max" in header_lines.lower()
    # Fallback: irgendwo im sichtbaren Bereich
    if not found_max:
        found_max = screen_contains("max") and not screen_contains("max_input")
    suite.add(
        "Reasoning-Effort: max",
        found_max,
        "Effort-Chip zeigt 'max' im Header" if found_max
        else "'max' nicht im Header – prüfe ~/.deepseek/config.toml: reasoning_effort = \"max\"",
        header_lines,
    )


def check_mode_cycling(suite: TestSuite, fd: int):
    """Ctrl+N cycelt Plan → Agent → Yolo → Propeller."""
    start_snap = render()
    modes_seen = []

    for _ in range(4):
        os.write(fd, b"\x0e")   # Ctrl+N
        drain(fd, CYCLE_WAIT)
        snap = render()
        for m in ("plan", "agent", "yolo", "propeller"):
            if m in snap.lower() and m not in modes_seen:
                modes_seen.append(m)

    all_modes = {"plan", "agent", "yolo", "propeller"}
    missing   = all_modes - set(modes_seen)
    suite.add(
        "Ctrl+N: Mode-Cycling (alle 4 Modi)",
        len(missing) == 0,
        f"Modi gesehen: {', '.join(sorted(modes_seen))}" +
        (f" | FEHLEN: {', '.join(sorted(missing))}" if missing else ""),
    )

    # Zurück zu Propeller (noch 0–3 mal Ctrl+N drücken bis wir es sehen)
    for _ in range(4):
        if screen_contains("propeller"):
            break
        os.write(fd, b"\x0e")
        drain(fd, CYCLE_WAIT)


def check_no_approval_prompt(suite: TestSuite, fd: int, verbose: bool):
    """Im Propeller-Modus: kein Bestätigungs-Dialog nach einer Aufgabe."""
    task = "Führe `echo PROPELLER_TEST_OK` aus"
    os.write(fd, task.encode())
    drain(fd, 0.5)
    os.write(fd, b"\r")
    drain(fd, ANSWER_WAIT)

    snap = render()
    # Propeller-Modus: keine "Approve / Deny"-Zeile sichtbar
    approval_showing = any(
        kw in snap.lower()
        for kw in ("approve", "deny", "allow", "zulassen", "ablehnen", "genehmig")
    )
    test_ran = "propeller_test_ok" in snap.lower()

    suite.add(
        "Kein Bestätigungs-Dialog (auto-approve)",
        not approval_showing,
        "Kein Approve/Deny-Dialog aufgetaucht" if not approval_showing
        else "WARNUNG: Bestätigungs-Dialog erschienen – auto-approve nicht aktiv",
        "\n".join(snap.splitlines()[-8:]),
    )
    suite.add(
        "Shell-Befehl wurde ausgeführt",
        test_ran,
        "PROPELLER_TEST_OK im Output gefunden" if test_ran
        else "Shell-Befehl wurde nicht ausgeführt (Timeout oder Fehler)",
    )


# ── Berichtserstellung ────────────────────────────────────────────────────────

def write_report(suite: TestSuite, verbose: bool):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    icon = "🟢" if suite.all_passed() else "🔴"
    lines = [
        f"# Propeller Test-Agent — Bericht",
        f"",
        f"**Ausgeführt:** {suite.started_at}  |  **Abgeschlossen:** {ts}",
        f"**Ergebnis:** {icon} {suite.summary()}",
        f"",
        f"---",
        f"",
        f"## Test-Ergebnisse",
        f"",
    ]
    for r in suite.results:
        status = "✅ PASS" if r.passed else "❌ FAIL"
        lines.append(f"### {status} — {r.name}")
        if r.detail:
            lines.append(f"_{r.detail}_")
        if r.snippet and verbose:
            lines.append(f"")
            lines.append(f"```")
            lines.append(r.snippet[:800])
            lines.append(f"```")
        lines.append("")

    lines += [
        "---",
        "",
        "## Entwicklungsregel",
        "",
        "> Dieses Skript muss grünes Licht geben bevor ein Propeller-Feature",
        "> als fertig gilt. Ausführen: `python3 test_agent.py`",
        "",
    ]

    report = "\n".join(lines)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    return report


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Propeller TUI Test Agent")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Bildschirm-Dumps in Bericht einfügen")
    parser.add_argument("--no-report", action="store_true",
                        help="Kein test_report.md schreiben")
    args = parser.parse_args()

    if not os.path.isfile(START_SH):
        print(f"FEHLER: start.sh nicht gefunden unter {START_SH}")
        sys.exit(2)

    suite = TestSuite()
    print(f"\n{'='*60}")
    print(f"  Propeller Test-Agent — {suite.started_at}")
    print(f"{'='*60}")
    print(f"  Binary: {SCRIPT_DIR}/target/release/propeller")
    print(f"  Screen: {COLS}×{ROWS}")
    print()

    # ── TUI starten ───────────────────────────────────────────────────────────
    env = os.environ.copy()
    env.update({
        "TERM":    "xterm-256color",
        "COLUMNS": str(COLS),
        "LINES":   str(ROWS),
        "LANG":    env.get("LANG", "de_DE.UTF-8"),
    })

    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(SCRIPT_DIR)
        os.execvpe("bash", ["bash", "start.sh"], env)
        os._exit(127)

    # PTY-Fenstergröße setzen
    fcntl.ioctl(fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", ROWS, COLS, 0, 0))

    print("  [Starte TUI…]")
    try:
        # ── Tests ausführen ───────────────────────────────────────────────────
        print("\n── Splash-Screen ───────────────────────────────────────")
        check_splash(suite, fd, args.verbose)

        print("\n── Hauptoberfläche laden ───────────────────────────────")
        dismiss_splash_and_wait(fd)

        snap_after_splash = render()
        if args.verbose:
            print("\n".join(snap_after_splash.splitlines()[:6]))

        print("\n── Modus & Effort ──────────────────────────────────────")
        check_propeller_mode(suite, args.verbose)
        check_max_effort(suite, args.verbose)

        print("\n── Mode-Cycling ────────────────────────────────────────")
        check_mode_cycling(suite, fd)

        print("\n── Auto-Approve / Shell-Ausführung ─────────────────────")
        check_no_approval_prompt(suite, fd, args.verbose)

    finally:
        # ── TUI sauber beenden ────────────────────────────────────────────────
        try:
            os.write(fd, b"\x03")   # Ctrl+C
            time.sleep(0.3)
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    # ── Abschlussbericht ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    icon = "🟢" if suite.all_passed() else "🔴"
    print(f"  {icon}  {suite.summary()}")
    print(f"{'='*60}")

    if not args.no_report:
        write_report(suite, args.verbose)
        print(f"\n  Bericht geschrieben: {REPORT_FILE}")

    # Abschließend vollen Bildschirm-Dump sichern
    with open(os.path.join(SCRIPT_DIR, "test_screen_dump.txt"), "w") as f:
        f.write(render())

    sys.exit(0 if suite.all_passed() else 1)


if __name__ == "__main__":
    main()
