# commands.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any

@dataclass
class StartConversation:
    role: str
    prompt: str
    model: str = "gpt5-webseite"
    params: Dict[str, Any] = None            # z.B. {"websearch": True}

@dataclass
class ContinueConversation:
    prompt: str

@dataclass
class EndConversation:
    pass

@dataclass
class KIResponse:
    ok: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    debug: Optional[Dict[str, Any]] = None
    def to_dict(self): return asdict(self)
