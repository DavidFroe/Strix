# json_utils.py
import json, re
from typing import Tuple, Any, Optional

JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S | re.I)

def extract_first_json(text: str) -> Tuple[bool, Optional[Any], Optional[str]]:
    if not isinstance(text, str):
        return False, None, "no_text"
    m = JSON_BLOCK.search(text)
    blob = m.group(1) if m else None
    if not blob:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b != -1 and b > a:
            blob = text[a:b+1]
    if not blob:
        return False, None, "no_json_found"
    try:
        return True, json.loads(blob), None
    except Exception as e:
        return False, None, f"json_parse_error: {e}"
