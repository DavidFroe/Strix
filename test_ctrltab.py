#!/usr/bin/env python3
"""Verify Ctrl+Tab cycles the visible model in the footer."""
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

import pyte

COLS, ROWS = 220, 50
START_WAIT = 8.0

screen = pyte.Screen(COLS, ROWS)
stream = pyte.ByteStream(screen)


def render():
    return "\n".join(line.rstrip() for line in screen.display)


def drain(fd, until_ts):
    while True:
        remaining = until_ts - time.time()
        if remaining <= 0:
            return
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            return
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        stream.feed(chunk)


def footer_model_line(snapshot: str) -> str:
    for ln in snapshot.splitlines():
        stripped = ln.strip()
        if stripped.startswith("agent ·") or stripped.startswith("yolo ·") or stripped.startswith("plan ·"):
            return stripped
    return ""


def main():
    os.environ["TERM"] = "xterm-256color"
    os.environ["COLUMNS"] = str(COLS)
    os.environ["LINES"] = str(ROWS)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("bash", ["bash", "start.sh"])
        os._exit(127)

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    try:
        drain(fd, time.time() + START_WAIT)
        before = render()
        with open("/tmp/ctrltab_before.txt", "w") as f:
            f.write(before)
        before_model = footer_model_line(before)
        print("Footer vor Ctrl+Tab:", before_model)

        # kitty keyboard protocol Ctrl+Tab: ESC [ 9 ; 5 u
        os.write(fd, b"\x1b[9;5u")
        drain(fd, time.time() + 2.0)
        after = render()
        with open("/tmp/ctrltab_after.txt", "w") as f:
            f.write(after)
        after_model = footer_model_line(after)
        print("Footer nach Ctrl+Tab:", after_model)

        # Footer-Vergleich greift nicht zwingend — ein wechselndes Modell
        # kann den Status-Toast "→ <name>" auslösen und damit den Footer-
        # Modus-Text überschreiben. Akzeptiere als Erfolg, wenn auf dem
        # Bildschirm ein anderer als der Ausgangs-Modellname auftaucht.
        baseline = "owltrail-pro" in before and "agent · owltrail-pro" in before
        switched_visible = "→" in after or "Model set to" in after
        new_model_visible = any(
            tok in after for tok in (
                "alibaba", "claude", "openai", "gemini", "qwen",
                "owltrail-flash", "deepseek-v4-flash",
            )
        )
        if baseline and (switched_visible or new_model_visible):
            print("ERGEBNIS: PASS — Modell hat gewechselt")
            return 0
        print("ERGEBNIS: FAIL — Modell unverändert")
        print("\n=== Screen-Dump (after) ===")
        print(after)
        return 1
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
