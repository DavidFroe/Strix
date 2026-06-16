# webbridge/ki_layer.py — SCHLANKE VERSION (mit Sticky-Websearch & Same-Chat-Retry)
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any
import time

from playwright.sync_api import sync_playwright

from .config import BridgeConfig
from .web_adapter import (
    start_chrome_debug,
    connect_chrome,
    pick_or_open_chat_page,
    ensure_composer_or_recover,
    on_login_page,
    dismiss_memory_modal,
    # Für Retry/Diagnose:
    detect_ui_error,
    click_try_again,
    send_prompt,
    wait_for_json_codeblock,
    enable_web_search,
    web_search_is_active,
)
from .flows import send_prompt_with_websearch


SYSTEM_HEADER = (
    "SYSTEMROLLE: OADS2 ↔ owAPI-Controller (DE)\n"
    "Du agierst als deterministischer JSON-Orchestrator zwischen Applikation (OADS2), WebBridge (Browser) und ownAPI (Sheets).\n"
    "- Antworte IMMER mit GENAU EINEM gültigen JSON-Codeblock (```json … ```), ohne Freitext.\n"
)

def _compose(role: str, user_prompt: str) -> str:
    return f"{SYSTEM_HEADER}\n[Rolle]: {role}\n[Prompt]: {user_prompt}"


@dataclass
class KIResult:
    ok: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timings: Optional[Dict[str, int]] = None


class OADS2KI:
    def __init__(self, cfg: Optional[BridgeConfig] = None, *, verbose: bool = True):
        self.cfg = cfg or BridgeConfig()
        self._pw = None
        self._browser = None
        self._page = None
        self._active = False
        self._t_total = int(getattr(self.cfg, "json_total_timeout_s", 75))
        self._t_settle = float(getattr(self.cfg, "json_settle_s", 0.6))
        self.verbose = bool(verbose)
        self._t0 = time.perf_counter()

        # --- Sticky-Websearch-Status (pro Thread) ---
        self._ws_sticky: bool = False
        self._ws_thread_id: Optional[str] = None

    # ---------------- intern: logging / timing ----------------
    def _since(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def _dbg(self, msg: str):
        if self.verbose:
            print(f"[ki t+{self._since():05d}ms] {msg}", flush=True)

    # ---------------- intern: Thread/Sticky helpers -----------
    def _current_thread_id(self) -> Optional[str]:
        try:
            u = (self._page.url or "")
            if "/c/" in u:
                return u.split("/c/")[1].split("?")[0].split("#")[0]
        except Exception:
            pass
        return None

    def _reset_sticky_ws(self):
        self._ws_sticky = False
        self._ws_thread_id = None

    def _ensure_websearch_once(self) -> bool:
        """Aktiviere Websearch nur einmal pro Chat-Thread (Sticky)."""
        tid = self._current_thread_id()
        if self._ws_sticky and tid and tid == self._ws_thread_id:
            return True
        # Falls schon aktiv (z. B. nach Reload): nur merken, nicht klicken.
        if web_search_is_active(self._page):
            self._ws_sticky = True
            self._ws_thread_id = tid
            return True
        ok = enable_web_search(self._page, self.cfg)
        if ok:
            self._ws_sticky = True
            self._ws_thread_id = tid
        return ok

    # ---------------- lifecycle ----------------
    def open(self):
        if self._pw:
            return
        self._dbg("Starte Playwright …")
        self._pw = sync_playwright().start()
        try:
            try:
                self._dbg(f"Verbinde zu Chrome CDP {self.cfg.cdp_addr}:{self.cfg.cdp_port} …")
                self._browser = connect_chrome(self._pw, self.cfg)
                self._dbg("CDP-Verbindung steht.")
            except Exception as e:
                self._dbg(f"CDP noch nicht erreichbar → starte Chrome … ({e})")
                start_chrome_debug(self.cfg)
                time.sleep(0.4)
                self._browser = connect_chrome(self._pw, self.cfg)
                self._dbg("CDP-Verbindung steht (nach Start).")

            self._dbg(f"Öffne/selektiere Chat-Seite: {self.cfg.chat_url}")
            self._page = pick_or_open_chat_page(self._browser, self.cfg.chat_url)

            # Memory-Modal direkt nach dem Öffnen wegklicken
            try:
                dismiss_memory_modal(self._page, self.cfg)
            except Exception:
                pass

            if on_login_page(self._page):
                raise RuntimeError("Login nötig – bitte im Chrome anmelden.")
            if not ensure_composer_or_recover(self._page, self.cfg, new_chat=True):
                raise RuntimeError("Composer nicht sichtbar.")
            self._dbg("Composer ist sichtbar/aktiv.")
            self._dbg(f"URL nach open(): {self._page.url}")
            self._reset_sticky_ws()  # frischer Kontext
        except Exception:
            self.close()
            raise

    def close(self):
        try:
            if self._browser:
                self._dbg("Schließe Browser …")
                with self._browser.expect_event("disconnected", timeout=1000):
                    self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._dbg("Stoppe Playwright …")
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._page = None
        self._active = False
        self._reset_sticky_ws()

    def __enter__(self): self.open(); return self
    def __exit__(self, *a): self.close()

    # ---------------- intern: Recovery-Sequenz ----------------
    def _same_chat_retry(self, original_prompt: str) -> Optional[Dict[str, Any]]:
        """
        1) 'Try again' klicken, wenn vorhanden
        2) Workaround-Prompt senden
        3) Dann Original-Prompt erneut senden (im SELBEN Chat)
        """
        self._dbg("Same-Chat-Retry: prüfe Fehlerbanner …")
        try:
            err = detect_ui_error(self._page)
        except Exception:
            err = None

        if err:
            self._dbg(f"Same-Chat-Retry: UI-Error erkannt → '{err}'")
            try:
                if click_try_again(self._page):
                    self._dbg("Same-Chat-Retry: 'Try again' geklickt.")
                    data = wait_for_json_codeblock(self._page, total_timeout_s=18, settle_s=self._t_settle)
                    if data:
                        return data
            except Exception:
                pass

        # Schritt 2: Workaround-Text (keine neue Konversation!)
        wk = "Please generate a JSON file with the information of the following prompt:"
        try:
            send_prompt(self._page, self.cfg, wk, auto_submit=True)
            wait_for_json_codeblock(self._page, total_timeout_s=8, settle_s=0.4)  # Antwort egal – nur Beruhigung
        except Exception:
            pass

        # Schritt 3: Original erneut
        try:
            send_prompt(self._page, self.cfg, original_prompt, auto_submit=True)
            data = wait_for_json_codeblock(self._page, total_timeout_s=60, settle_s=self._t_settle)
            return data
        except Exception:
            return None

    def _new_chat_retry(self, original_prompt: str) -> Optional[Dict[str, Any]]:
        """Neuen Chat öffnen, Sticky zurücksetzen, Websearch einmal aktivieren, Prompt erneut."""
        self._dbg("New-Chat-Retry: öffne neuen Chat …")
        try:
            ensure_composer_or_recover(self._page, self.cfg, new_chat=True)
        except Exception:
            return None
        self._reset_sticky_ws()
        self._ensure_websearch_once()
        try:
            send_prompt(self._page, self.cfg, original_prompt, auto_submit=True)
            return wait_for_json_codeblock(self._page, total_timeout_s=60, settle_s=self._t_settle)
        except Exception:
            return None

    # ---------------- high-level ----------------
    def start_conversation(self, role: str, prompt: str, *, websearch: bool = True, temp_chat: bool = False) -> KIResult:
        timings: Dict[str, int] = {}
        try:
            self.open()
            self._dbg("== Start Conversation ==")
            full = _compose(role, prompt)
            self._dbg(f"Prompt-Preview: {full[:180]}{'…' if len(full) > 180 else ''}")

            # Websuche nur einmal pro Thread aktivieren (Sticky)
            if websearch:
                self._ensure_websearch_once()

            t0 = time.perf_counter()
            data = send_prompt_with_websearch(
                self._page, self.cfg, full,
                activation_latch_secs=8.0,
                json_total_timeout_s=self._t_total,
                json_settle_s=self._t_settle,
                debug=self.verbose,
                startover_cb=self.startover,
            )
            if not data:
                # Same-Chat-Retry ohne neuen Chat
                data = self._same_chat_retry(full)
            if not data:
                # Neuer Chat probieren
                data = self._new_chat_retry(full)
            if not data:
                # Harter Neustart
                self._dbg("Start: fallback startover() …")
                if self.startover():
                    self._ensure_websearch_once()
                    send_prompt(self._page, self.cfg, full, auto_submit=True)
                    data = wait_for_json_codeblock(self._page, total_timeout_s=60, settle_s=self._t_settle)

            timings["total_ms"] = int((time.perf_counter() - t0) * 1000)
            if not data:
                return KIResult(False, error="Keine gültige JSON-Antwort", timings=timings)

            self._active = True
            return KIResult(True, data=data, timings=timings)
        except Exception as e:
            return KIResult(False, error=str(e), timings=timings)

    def continue_conversation(self, prompt: str) -> KIResult:
        if not self._active:
            return KIResult(False, error="Kein aktives Gespräch – erst start_conversation() aufrufen.")
        timings: Dict[str, int] = {}
        try:
            self._dbg("== Continue Conversation ==")

            # Sticky-Websearch im selben Thread nicht erneut anfassen
            self._ensure_websearch_once()

            t0 = time.perf_counter()
            data = send_prompt_with_websearch(
                self._page, self.cfg, prompt,
                activation_latch_secs=8.0,
                json_total_timeout_s=self._t_total,
                json_settle_s=self._t_settle,
                debug=self.verbose,
                startover_cb=self.startover,
            )
            if not data:
                data = self._same_chat_retry(prompt)
            if not data:
                data = self._new_chat_retry(prompt)
            if not data:
                self._dbg("Continue: fallback startover() …")
                if self.startover():
                    self._ensure_websearch_once()
                    send_prompt(self._page, self.cfg, prompt, auto_submit=True)
                    data = wait_for_json_codeblock(self._page, total_timeout_s=60, settle_s=self._t_settle)

            timings["total_ms"] = int((time.perf_counter() - t0) * 1000)
            if not data:
                return KIResult(False, error="Keine gültige JSON-Antwort", timings=timings)
            return KIResult(True, data=data, timings=timings)
        except Exception as e:
            return KIResult(False, error=str(e), timings=timings)

    def end_conversation(self) -> None:
        self._dbg("== End Conversation ==")
        self._active = False
        # Beim Abschluss Sticky-Zustand zurücksetzen (neuer Thread => neue Aktivierung erlaubt)
        self._reset_sticky_ws()

    def startover(self) -> bool:
        """Harter Neustart (Browser/Playwright neu) und Wiederöffnung."""
        try:
            self.close()
            time.sleep(0.4)
            self.open()
            # nach Neustart: Sticky zurücksetzen & bei Bedarf einmal aktivieren
            self._reset_sticky_ws()
            return self._page is not None
        except Exception:
            return False
