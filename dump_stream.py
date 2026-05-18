#!/usr/bin/env python3
"""Dumpt rohe SSE-Frames für deepseek-v4-pro Streaming, um das Chunk-Format
zu identifizieren. Schreibt nach stream_dump.txt."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import owltrail
import owltrail_adapter


def main() -> int:
    owltrail_adapter.install_model_mapping_patch()
    port = 8182
    proxy = owltrail.OwlTrailProxy(conf_file=os.path.join(HERE, "owltrail.conf"))
    proxy.start(port=port)
    base = f"http://127.0.0.1:{port}/v1"

    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            break
        except OSError:
            time.sleep(0.1)

    out = open(os.path.join(HERE, "stream_dump.txt"), "w", encoding="utf-8")

    def dump(name: str, model: str, prompt: str, stream: bool):
        out.write(f"\n\n========== {name} (model={model}, stream={stream}) ==========\n")
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
            "max_tokens": 64,
            "temperature": 0.0,
        }).encode()
        req = urllib.request.Request(
            base + "/chat/completions",
            data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer sk-owltrail",
                "Accept": "text/event-stream",
            },
        )
        try:
            r = urllib.request.urlopen(req, timeout=60)
        except Exception as e:
            out.write(f"REQUEST ERROR: {type(e).__name__}: {e}\n")
            return
        for i, raw in enumerate(r):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                out.write(f"  [{i:03d}] <empty>\n")
                continue
            out.write(f"  [{i:03d}] {line}\n")
            if i > 200:
                out.write("  ... abbruch nach 200 zeilen ...\n")
                break

    try:
        dump("PRO_STREAM_TRUE",  "deepseek-v4-pro",   "Zähle laut von 1 bis 5 — nur die Zahlen.", stream=True)
        dump("FLASH_STREAM_TRUE", "deepseek-v4-flash", "Zähle laut von 1 bis 5 — nur die Zahlen.", stream=True)
        dump("PRO_LONGER_PROMPT", "deepseek-v4-pro",
             "Bitte schreibe ein kurzes Hallo-Welt in Python (3 Zeilen).", stream=True)
    finally:
        out.close()
        proxy.stop()

    print(f"Gedumpt nach {os.path.join(HERE, 'stream_dump.txt')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
