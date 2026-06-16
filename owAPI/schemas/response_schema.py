response_schema.pyfrom pydantic import BaseModel
from typing import List, Optional, Dict, Any

class WebFinding(BaseModel):
    title: str
    url: str
    notes: str

class NextAction(BaseModel):
    open_new_chat: bool = False
    recommend_web_search: bool = False
    recommend_temporary_chat: bool = False

class Hints(BaseModel):
    target_row: Optional[int] = None
    target_header: Optional[str] = None
    alt_urls: List[str] = []

class ResponseData(BaseModel):
    decision: str  # expected to be one of "finish", "produce_update_json", "ask_followup", "retry"
    message: str
    web_findings: List[WebFinding] = []
    update_json: Dict[str, Any] = {}
    next_action: NextAction = NextAction()
    hints: Hints = Hints()

class RawInfo(BaseModel):
    text_excerpt: str = ""
    json_blocks_found: int = 0

class EventEntry(BaseModel):
    t: str
    e: str

class ResponseModel(BaseModel):
    protocol_version: str
    status: str
    data: Optional[ResponseData] = None
    raw: RawInfo
    events: List[EventEntry] = []
    errors: List[str] = []
