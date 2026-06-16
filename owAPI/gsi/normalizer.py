# gsi/normalizer.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
import logging

LOG = logging.getLogger("gpt_sheet")

def _ops_to_actions(payload: Dict[str, Any]) -> None:
    """
    Kompatibilität: erlaubt Top-Level 'ops': [{ op: 'append'|'append_rows'|..., worksheet?, values?/rows? }, ...]
    → wandelt nach 'actions' um und mappt 'op' → 'type'.
    """
    if not isinstance(payload, dict):
        return
    if isinstance(payload.get("ops"), list) and not payload.get("actions"):
        actions = []
        for op in payload["ops"]:
            if not isinstance(op, dict):
                continue
            act = dict(op)
            if "type" not in act and "op" in act:
                act["type"] = act.pop("op")
            actions.append(act)
        payload["actions"] = actions
        LOG.debug("ops → actions konvertiert (%d)", len(actions))
        # Hinweis in Diagnostics macht die Engine; hier nur Log.

def _apply_worksheet_alias(payload: Dict[str, Any]) -> None:
    """
    Alias-Unterstützung: top-level 'worksheet' als Ersatz für sheet.worksheet
    """
    if not isinstance(payload, dict):
        return
    if "worksheet" in payload:
        sheet = payload.get("sheet") or {}
        if not isinstance(sheet, dict):
            sheet = {}
        if "worksheet" not in sheet:
            sheet["worksheet"] = payload["worksheet"]
            payload["sheet"] = sheet

def coerce_incoming_payload(payload: Dict[str, Any],
                            headers: Optional[List[str]],
                            diagnostics: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    - wendet Backwards-Compat-Transformationen an
    - kann kleinere Korrekturen durchführen
    """
    if not isinstance(payload, dict):
        return {}

    before_actions = len(payload.get("actions") or [])
    _ops_to_actions(payload)
    after_actions = len(payload.get("actions") or [])
    if before_actions == 0 and after_actions > 0:
        diagnostics.append({"level": "info", "code": "ops_to_actions", "message": f"{after_actions} Einträge aus 'ops' übernommen; 1x 'op'→'type'."})

    _apply_worksheet_alias(payload)

    # keine weiteren Zwangsumformungen – 'append_rows' bleibt als Batch-Aktion stehen
    # (der Engine-Dispatch expandiert diese später in einzelne 'append' Calls)

    return payload
