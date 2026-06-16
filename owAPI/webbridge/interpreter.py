# interpreter.py
from __future__ import annotations
from typing import Dict, Any

from commands import StartConversation, ContinueConversation, EndConversation, KIResponse
from json_utils import extract_first_json
from web_adapter import IWebAdapter

class CommandInterpreter:
    def __init__(self, wb: IWebAdapter):
        self.wb = wb
        self.active = False
        self.conv_id = 0

    # -------- Start --------
    def do_start(self, req: StartConversation) -> KIResponse:
        if self.active:
            return KIResponse(ok=False, error="conversation_active")

        # if not chromeconnection → starteChrome()
        if not self.wb.ensure_connection():
            return KIResponse(ok=False, error="cannot_connect_browser")
        self.wb.wait(100)

        # startNewChat()
        if not self.wb.open_new_chat():
            return KIResponse(ok=False, error="open_new_chat_failed")
        self.wb.wait(100)

        # activate_websearch()
        params = req.params or {}
        if params.get("websearch", True):
            if not self.wb.enable_web_search():
                # optional nicht abbrechen
                pass
        self.wb.wait(100)

        # focus composer
        if not self.wb.focus_composer():
            return KIResponse(ok=False, error="focus_composer_failed")
        self.wb.wait(10)

        # insertPrompt()
        composed = (
            "[ROLE]\n" + req.role + 
            "\n\n[USER]\n" + req.prompt +
            "\n\n[FORMAT]\nAntworte NUR als gültiger JSON-Codeblock (```json ... ```)."
        )
        if not self.wb.insert_prompt(composed):
            return KIResponse(ok=False, error="insert_prompt_failed")
        self.wb.wait(10)

        # send + wait_for_ki()
        if not self.wb.send_prompt():
            return KIResponse(ok=False, error="send_prompt_failed")
        if not self.wb.wait_for_model():
            return KIResponse(ok=False, error="model_timeout")

        # get_response()
        raw = self.wb.read_last_assistant_text() or ""
        ok, obj, err = extract_first_json(raw)
        if not ok:
            self.active = True
            self.conv_id += 1
            return KIResponse(ok=False, error=f"invalid_json: {err}", debug={"raw_text": raw, "conv_id": self.conv_id})

        # Erfolg
        self.active = True
        self.conv_id += 1
        return KIResponse(ok=True, data={"json": obj, "raw_text": raw}, debug={"conv_id": self.conv_id})

    # -------- Continue --------
    def do_continue(self, req: ContinueConversation) -> KIResponse:
        if not self.active:
            return KIResponse(ok=False, error="no_active_conversation")

        if not self.wb.focus_composer():
            return KIResponse(ok=False, error="focus_composer_failed")
        self.wb.wait(10)
        if not self.wb.insert_prompt(req.prompt):
            return KIResponse(ok=False, error="insert_prompt_failed")
        self.wb.wait(10)
        if not self.wb.send_prompt():
            return KIResponse(ok=False, error="send_prompt_failed")
        if not self.wb.wait_for_model():
            return KIResponse(ok=False, error="model_timeout")

        raw = self.wb.read_last_assistant_text() or ""
        ok, obj, err = extract_first_json(raw)
        if not ok:
            return KIResponse(ok=False, error=f"invalid_json: {err}", debug={"raw_text": raw, "conv_id": self.conv_id})
        return KIResponse(ok=True, data={"json": obj, "raw_text": raw}, debug={"conv_id": self.conv_id})

    # -------- End --------
    def do_end(self, req: EndConversation) -> KIResponse:
        if not self.active:
            return KIResponse(ok=False, error="no_active_conversation")
        self.active = False
        return KIResponse(ok=True, data={"ended": True}, debug={"conv_id": self.conv_id})
