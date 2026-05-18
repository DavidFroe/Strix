#!/usr/bin/env python3
"""Drive the TUI via PTY + pyte, send THREE prompts in the same session
without restarting the TUI. Verifies the reqwest-pool fix.
"""
import os
import pty
import select
import signal
import sys
import time

import pyte

COLS, ROWS = 220, 50
START_WAIT = 8.0
ANSWER_WAIT = 45.0   # generous — Pro reasoning + first cold connect

PROMPTS = [
    ("Antworte nur mit dem Wort: ALPHA",   "ALPHA"),
    ("Antworte nur mit dem Wort: BRAVO",   "BRAVO"),
    ("Antworte nur mit dem Wort: CHARLIE", "CHARLIE"),
]

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


def send_prompt(fd, prompt, marker, deadline_after):
    """Type prompt, wait paste-burst, hit Enter, poll for marker.
    Returns (success: bool, network_error: bool, snippet: str)."""
    os.write(fd, prompt.encode())
    drain(fd, time.time() + 1.5)
    os.write(fd, b"\r")
    deadline = time.time() + deadline_after
    seen = False
    while time.time() < deadline:
        drain(fd, time.time() + 0.5)
        current = render()
        # Marker must appear in a line that is NOT just the user-prompt echo
        for ln in current.splitlines():
            if marker in ln and prompt not in ln:
                seen = True
                break
        if seen:
            break
    after = render()
    lines = after.splitlines()
    net_err = any(
        ("network error" in ln.lower() or "request failed" in ln.lower())
        for ln in lines
    )
    # Capture last 25 lines for diagnostics
    snippet = "\n".join(lines[-25:])
    return seen, net_err, snippet


def main():
    os.environ["TERM"] = "xterm-256color"
    os.environ["COLUMNS"] = str(COLS)
    os.environ["LINES"] = str(ROWS)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("bash", ["bash", "start.sh"])
        os._exit(127)

    import fcntl
    import struct
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    results = []
    try:
        drain(fd, time.time() + START_WAIT)
        before = render()
        with open("/tmp/tui_3x_start.txt", "w") as f:
            f.write(before)
        print("=== TUI nach Start (letzte 20 Zeilen) ===")
        print("\n".join(before.splitlines()[-20:]))
        print()

        for i, (prompt, marker) in enumerate(PROMPTS, 1):
            print(f"=== Prompt {i}/{len(PROMPTS)}: '{prompt}' (warte auf '{marker}') ===")
            ok, net_err, snippet = send_prompt(fd, prompt, marker, ANSWER_WAIT)
            results.append((i, prompt, marker, ok, net_err))
            with open(f"/tmp/tui_3x_after_{i}.txt", "w") as f:
                f.write(snippet)
            status = "OK" if ok else ("NETWORK_ERROR" if net_err else "TIMEOUT")
            print(f"  -> {status}")
            print(f"  letzte Zeilen:\n{snippet}\n")
            # Small pause so the next prompt visibly comes after the answer
            time.sleep(1.5)

        print("=== Zusammenfassung ===")
        all_ok = True
        for i, prompt, marker, ok, net_err in results:
            tag = "PASS" if ok else ("NETERR" if net_err else "TIMEOUT")
            print(f"  Prompt {i} ('{marker}'): {tag}")
            if not ok:
                all_ok = False

        if all_ok:
            print("ERGEBNIS: 3x-TEST BESTANDEN")
            return 0
        else:
            print("ERGEBNIS: 3x-TEST FEHLGESCHLAGEN")
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
