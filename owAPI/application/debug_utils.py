# application/debug_utils.py
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional

def _shorten(s: str, n: int = 80) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"

def print_status_summary(status: Dict[str, Any], label: str = "") -> None:
    """Kompakte Übersicht der status.json:
    - Anzahl results, pro Action: status, rows_preview vorhanden?, matches, header-Länge.
    - Diagnostics (erste 10).
    - Falls vorhanden: erste Trefferzeile (row + 3 Spaltenbeispiele).
    - tabs[title=headers] fürs schnelle Header-Debugging.
    """
    print(f"\n[DEBUG] ===== status.json summary {('(' + label + ')') if label else ''} =====", flush=True)
    if not isinstance(status, dict):
        print("[DEBUG] status ist kein dict.", flush=True)
        return

    results = status.get("results") or []
    print(f"[DEBUG] results: {len(results)}", flush=True)

    first_preview = None
    first_headers: Optional[List[str]] = None

    for idx, res in enumerate(results):
        action = res.get("action")
        st = res.get("status")
        meta = res.get("meta") or {}
        headers = meta.get("headers") or []
        rows_preview = meta.get("rows_preview") or []
        matches = meta.get("matches")
        hlen = len(headers)
        rplen = len(rows_preview)
        print(f"[DEBUG]  #{idx:02d} action={action!r} status={st!r} headers={hlen} rows_preview={rplen} matches={matches}", flush=True)
        if rplen and first_preview is None:
            first_preview = rows_preview[0]
            first_headers = headers

    diags = status.get("diagnostics") or []
    if diags:
        print(f"[DEBUG] diagnostics: {len(diags)} (zeige max 10)", flush=True)
        for d in diags[:10]:
            lvl = d.get("level")
            code = d.get("code")
            msg = _shorten(d.get("message", ""))
            print(f"[DEBUG]  - {lvl}/{code}: {msg}", flush=True)
    else:
        print("[DEBUG] diagnostics: none", flush=True)

    tabs = status.get("tabs") or []
    if tabs:
        print("[DEBUG] tabs+headers:", flush=True)
        for t in tabs:
            title = t.get("title")
            headers = t.get("headers") or []
            print(f"[DEBUG]  - {title!r}: {len(headers)} Header -> {_shorten(', '.join(map(str, headers)), 160)}", flush=True)

    if first_preview:
        rowno = first_preview.get("row")
        row_vals = first_preview.get("row_values") or []
        print(f"[DEBUG] erster rows_preview → row={rowno}", flush=True)
        if first_headers:
            samples = []
            for i, h in enumerate(first_headers[:3]):
                v = row_vals[i] if i < len(row_vals) else ""
                samples.append(f"{h!r}={_shorten(v)}")
            print("[DEBUG]  Beispielwerte:", ", ".join(samples), flush=True)
    else:
        print("[DEBUG] kein rows_preview in den Ergebnissen gefunden.", flush=True)
    print("[DEBUG] ===== end summary =====\n", flush=True)
