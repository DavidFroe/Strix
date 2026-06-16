"""LLM-Client: direkte Verbindung zu QuiteQue via owltrail.conf."""

import json, re, time
from pathlib import Path
from openai import OpenAI


def _strip_think(text: str) -> str:
    """Entfernt <think>…</think> Blöcke von Thinking-Modellen (z.B. QwQ, DeepSeek-R1)."""
    return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()


class LLMClient:
    def __init__(self, owltrail_conf: Path, agent_conf: dict):
        oc = json.loads(owltrail_conf.read_text())
        self.models  = agent_conf.get("models", {})
        token = oc.get("token", "sk-no-key-required")
        self._client = OpenAI(
            base_url=f"http://{oc['server_ip']}:{oc['quiteque_port']}/v1",
            api_key=token,
            default_headers={
                "X-OwlTrail-User": oc.get("username", ""),
                "X-User-ID":       oc.get("username", ""),
            },
            timeout=1800,
        )

    def chat(self, messages: list[dict], step: str = "profile",
             temperature: float = 0.7) -> str:
        model_id = str(self.models.get(step, "207"))
        t0 = time.time()
        resp = self._client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=temperature,
        )
        dt = time.time() - t0
        content = _strip_think(resp.choices[0].message.content)
        tok = resp.usage
        tok_info = f", {tok.prompt_tokens}→{tok.completion_tokens} tok" if tok else ""
        print(f"  ⏱  LLM [{model_id}/{step}] {dt:.1f}s{tok_info}, {len(content)} Zeichen", flush=True)
        return content
