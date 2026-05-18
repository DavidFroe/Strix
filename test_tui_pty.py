#!/usr/bin/env python3
"""Drive the TUI via PTY + pyte, mimicking the tmux test from SIGNAL.md."""
import os
import pty
import select
import signal
import sys
import time

import pyte

COLS, ROWS = 220, 50
START_WAIT = 8.0      # SIGNAL.md: sleep 8 nach TUI-Start
ANSWER_WAIT = 30.0    # SIGNAL.md: sleep 15; we wait longer & poll
PROMPT = "Antworte nur mit dem Wort: TUIWORKS"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.ByteStream(screen)


def render():
    return "\n".join(line.rstrip() for line in screen.display)


def drain(fd, until_ts):
    """Read whatever is available from fd until until_ts (epoch seconds)."""
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


def main():
    os.environ["TERM"] = "xterm-256color"
    os.environ["COLUMNS"] = str(COLS)
    os.environ["LINES"] = str(ROWS)

    pid, fd = pty.fork()
    if pid == 0:
        # Child
        os.execvp("bash", ["bash", "start.sh"])
        os._exit(127)

    # Set the PTY size so the TUI renders at our virtual size
    import fcntl
    import struct
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    try:
        # Phase 1: warm up
        drain(fd, time.time() + START_WAIT)
        before = render()
        with open("/tmp/tui_before.txt", "w") as f:
            f.write(before)
        print("=== TUI-Zustand nach Start ===")
        print("\n".join(before.splitlines()[-20:]))

        # Phase 2: send prompt (without Enter), let the paste-burst window
        # close, then send Enter as a separate keypress so the TUI treats
        # it as a real submit, not a trailing newline of a paste burst.
        os.write(fd, PROMPT.encode())
        drain(fd, time.time() + 1.5)
        os.write(fd, b"\r")
        # Poll for answer up to ANSWER_WAIT seconds, but keep draining
        deadline = time.time() + ANSWER_WAIT
        while time.time() < deadline:
            drain(fd, time.time() + 1.0)
            current = render()
            if "TUIWORKS" in current and PROMPT not in current.split("TUIWORKS")[-1]:
                # Found the answer (not just the echo of our own prompt)
                pass

        after = render()
        with open("/tmp/tui_after.txt", "w") as f:
            f.write(after)
        print("=== TUI-Zustand nach Antwort ===")
        print(after)

        # Auswertung — nur Treffer ausserhalb der Prompt-Zeile zählen
        lines = after.splitlines()
        answer_hits = [
            ln for ln in lines
            if "TUIWORKS" in ln and PROMPT not in ln
        ]
        net_err = any(
            ("network error" in ln.lower() or "request failed" in ln.lower())
            for ln in lines
        )

        if answer_hits:
            print("ERGEBNIS: TUI-TEST BESTANDEN — 'TUIWORKS' im Bildschirm gefunden")
            print(f"  Treffer-Zeile(n): {answer_hits}")
            rc = 0
        elif net_err:
            print("ERGEBNIS: TUI-TEST FEHLGESCHLAGEN — Network error sichtbar")
            rc = 1
        else:
            print("ERGEBNIS: UNKLAR — weder Antwort noch Fehler erkannt")
            rc = 2
        return rc
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
