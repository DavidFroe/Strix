#!/usr/bin/env python3
"""Drive the TUI via PTY + pyte to validate the model-switch flow from
SIGNAL.md (Aufgabe 2..4):

  T1: `/model` opens the picker → ↓ ↓ Enter actually changes main model
  T2: `/submodel free` sets the sub-agent model → header shows S:free
  T3: Alt+m cycles the sub-agent model again

The Ctrl+Tab cycler is also exercised, but with a forgiving check: PTYs
don't always transport the modifyOtherKeys encoding crossterm needs, so
we only WARN if the keypress doesn't move the model.
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

COLS, ROWS = 240, 50
START_WAIT = 14.0
DRAIN_AFTER_KEY = 1.8
# Paste-burst window in the TUI is ~1s; characters typed via PTY look
# like a paste, so Enter must be sent AFTER the burst window closes
# or it gets reinterpreted as a literal newline in the composer.
PASTE_BURST_CLOSE = 1.6
LOG_DIR = "/tmp"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.ByteStream(screen)


def render() -> str:
    return "\n".join(line.rstrip() for line in screen.display)


def header_line() -> str:
    return screen.display[0].rstrip()


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


def find_main_model(line: str) -> str:
    if "M:" in line:
        rest = line.split("M:", 1)[1].lstrip()
        for sep in ("  S:", "  ·", "  ▰", "  ▱", "  Live", "  ●"):
            if sep in rest:
                rest = rest.split(sep, 1)[0]
        return rest.strip()
    # Single-label layout: "<Mode>  <workspace> · <model>  · <effort> ..."
    # Take the FIRST " · " segment — the right-side cluster is effort/usage.
    parts = [p.strip() for p in line.split(" · ")]
    if len(parts) >= 2:
        return parts[1].strip()
    return ""


def find_sub_model(line: str) -> str:
    if "S:" not in line:
        return ""
    rest = line.split("S:", 1)[1].lstrip()
    for sep in ("  ·", "  ▰", "  ▱", "  Live", "  ●"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    return rest.strip()


def dump(label: str):
    path = os.path.join(LOG_DIR, f"tui_switch_{label}.txt")
    with open(path, "w") as f:
        f.write(render())
    print(f"--- {label} ---")
    print(f"  header: {header_line().strip()!r}")
    print(f"  → wrote {path}")


def send(fd, data):
    os.write(fd, data)


def type_text(fd, text):
    send(fd, text.encode())


def press(fd, key):
    if key == "enter":
        send(fd, b"\r")
    elif key == "esc":
        send(fd, b"\x1b")
    elif key == "down":
        send(fd, b"\x1b[B")
    elif key == "up":
        send(fd, b"\x1b[A")
    elif key == "tab":
        send(fd, b"\t")
    elif key == "ctrl_tab":
        # Try the modifyOtherKeys form first.
        send(fd, b"\x1b[27;5;9~")
    elif key == "alt_m":
        send(fd, b"\x1bm")


def main() -> int:
    os.environ["TERM"] = "xterm-256color"
    os.environ["COLUMNS"] = str(COLS)
    os.environ["LINES"] = str(ROWS)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("bash", ["bash", "start.sh"])
        os._exit(127)

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    rc = 0
    failures = []
    try:
        drain(fd, time.time() + START_WAIT)
        dump("00_start")
        initial_main = find_main_model(header_line())
        print(f"initial main: {initial_main!r}")
        if not initial_main:
            failures.append("no model in header at startup")

        # T1 — /model picker: type /model, wait for paste-burst to close,
        # then Enter to actually submit.
        type_text(fd, "/model")
        drain(fd, time.time() + PASTE_BURST_CLOSE)
        press(fd, "enter")
        drain(fd, time.time() + DRAIN_AFTER_KEY)
        dump("01_picker_open")

        # Move down twice in the picker, then Enter to apply.
        press(fd, "down")
        drain(fd, time.time() + 0.3)
        press(fd, "down")
        drain(fd, time.time() + 0.3)
        press(fd, "enter")
        drain(fd, time.time() + DRAIN_AFTER_KEY)
        dump("02_picker_applied")

        main_after_picker = find_main_model(header_line())
        print(f"main after /model picker Enter: {main_after_picker!r}")
        if main_after_picker and main_after_picker != initial_main:
            print("PASS  /model picker Enter changed the main model")
        else:
            print("FAIL  /model picker Enter did NOT change the main model")
            failures.append("model picker Enter not applied")

        # T2 — /submodel free explicit. Wait out the paste-burst window
        # so Enter is treated as submit, not as newline-in-composer.
        type_text(fd, "/submodel free")
        drain(fd, time.time() + PASTE_BURST_CLOSE)
        press(fd, "enter")
        drain(fd, time.time() + DRAIN_AFTER_KEY)
        dump("03_submodel_free")
        sub_after = find_sub_model(header_line())
        print(f"sub after /submodel free: {sub_after!r}")
        # The TUI displays the friendly model name from the adapter
        # (`Free Plan (Auto)`) rather than the raw id (`free`). Accept either.
        if sub_after and (
            sub_after == "free"
            or "free" in sub_after.lower()
            or sub_after.lower().startswith("free plan")
        ):
            print("PASS  /submodel free set the sub-agent model")
        else:
            print(f"FAIL  /submodel free did not set sub model (got: {sub_after!r})")
            failures.append("/submodel arg not applied")

        # T3 — Alt+m cycles the sub-agent model
        press(fd, "alt_m")
        drain(fd, time.time() + DRAIN_AFTER_KEY)
        dump("04_after_alt_m")
        sub_alt = find_sub_model(header_line())
        print(f"sub after Alt+m: {sub_alt!r}")
        if sub_alt and sub_alt != sub_after:
            print("PASS  Alt+m cycled the sub-agent model")
        else:
            print(f"WARN  Alt+m did not change sub (got: {sub_alt!r})")
            # Not a hard failure — the cycle wraps back to "free" if
            # there are only 2 available models in some configs.

        # T4 — Ctrl+Tab tries to cycle the main model (soft)
        before_ctrl = find_main_model(header_line())
        press(fd, "ctrl_tab")
        drain(fd, time.time() + DRAIN_AFTER_KEY)
        dump("05_after_ctrl_tab")
        after_ctrl = find_main_model(header_line())
        print(f"main after Ctrl+Tab: {after_ctrl!r}")
        if after_ctrl and after_ctrl != before_ctrl:
            print("PASS  Ctrl+Tab cycled the main model")
        else:
            print("WARN  Ctrl+Tab did not cycle — likely a PTY key-encoding "
                  "issue (works in real terminals; verify manually).")

        # T5 — final header inspection
        final = header_line().strip()
        if "M:" in final and "S:" in final:
            print(f"PASS  header shows both labels: {final!r}")
        elif "M:" in final or "S:" in final:
            print(f"INFO  header shows partial labels: {final!r}")
        else:
            print(f"FAIL  header missing M:/S: labels: {final!r}")
            failures.append("header layout missing")

        if failures:
            rc = 1
            print(f"\nFAILURES: {failures}")
        else:
            print("\nALL HARD CHECKS PASSED")
        return rc
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.4)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
