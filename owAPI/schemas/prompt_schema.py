from pydantic import BaseModel, Field
from typing import Optional, List, Any, Dict

class ConversationSettings(BaseModel):
    new_chat: bool
    temporary_chat: bool
    enable_web_search: bool

class UISettings(BaseModel):
    dom_first: bool = True
    absolute: Dict[str, List[int]]

class WaitSettings(BaseModel):
    expect_json_codeblock: bool = True
    max_wait_s: int = 120
    settle_s: float = 0.6

class PromptModel(BaseModel):
    protocol_version: str = Field(..., regex=r'^\d+\.\d+\.\d+$')
    conversation: ConversationSettings
    ui: UISettings
    wait: WaitSettings
    message: str
    meta: Dict[str, Any] = {}

    class Config:
        extra = "ignore"
