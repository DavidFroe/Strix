#!/usr/bin/env python3
"""Verify the TUI starts directly without any onboarding screen.

Spawns `bash start.sh` in a PTY, waits 6s, captures the rendered screen,
and looks for onboarding artifacts (Welcome, language picker, trust
dialog, tips screen) vs. composer artifacts (the input gutter).
"""
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

COLS, ROWS = 200, 50
WARM_WAIT = 7.0

ONBOARDING_MARKERS = [
    # Welcome screen
    "A focused terminal workspace",
    "Press Enter to continue",
    "You'll add an API key",
    # Language screen
    "Select your language",
    "Sprache w",
    # Trust screen
    "Trust this workspace",
    "Verzeichnis vertrauen",
    "Do you trust",
    # Step indicator only appears inside onboarding panel
    "Step 1/",
    "Step 2/",
    "Step 3/",
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
        drain(fd, time.time() + WARM_WAIT)
        rendered = render()
        with open("/tmp/tui_no_onboarding.txt", "w") as f:
            f.write(rendered)

        hits = [m for m in ONBOARDING_MARKERS if m in rendered]
        if hits:
            print("FAIL — onboarding markers visible:")
            for h in hits:
                print(f"  - {h!r}")
            print("\n--- screen ---")
            print(rendered)
            return 1

        # Look for the composer gutter — a typical sign the TUI is ready.
        if "▎" in rendered or "│" in rendered or "Type" in rendered or ">" in rendered:
            print("PASS — TUI rendered, no onboarding screens.")
            print("\n--- screen (top 25 lines) ---")
            print("\n".join(rendered.splitlines()[:25]))
            return 0

        print("UNKNOWN — neither onboarding nor obvious composer, dumping screen:")
        print(rendered)
        return 2
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.3)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
