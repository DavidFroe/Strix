from dataclasses import dataclass, field
from pathlib import Path
import os, sys


@dataclass
class BridgeConfig:
    # --- Chrome/CDP ---
    chrome_exe: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    cdp_addr: str = "127.0.0.1"
    cdp_port: int = 9333
    chrome_user_data_dir: str = str(Path(os.getenv("LOCALAPPDATA") or ".") / "ChromeDebugUserData")
    chat_url: str = "https://chatgpt.com/"

    wait_cdp_seconds: int = 10

    # --- Chat-Features ---
    use_temporary_chat: bool = False
    use_web_search: bool = True

    # --- Strategien ---
    dom_first: bool = True
    mouse_fallback_enable: bool = True

    # --- Schnelle Standard-Wartezeiten ---
    mouse_move_duration: float = 0.10
    mouse_after_plus_sleep: float = 0.18
    mouse_after_hover_sleep: float = 0.18
    mouse_after_click_sleep: float = 0.18

    mouse_mode: str = "maximized"
    mouse_send_f11: bool = False

    # --- Fixpunkte (absolute Koordinaten, unverändert) ---
    mouse_composer_xy: tuple = (1000, 536)
    mouse_plus_xy: tuple = (630, 530)
    mouse_more_hover_xy: tuple = (768, 768)
    mouse_web_xy_regular: tuple = (1000, 780)
    mouse_web_xy_tempchat: tuple = (777, 644)

    # Maximiert (spiegeln der obigen Werte)
    mouse_composer_xy_max: tuple = (1000, 536)
    mouse_plus_xy_max: tuple = (730, 530)
    mouse_more_hover_xy_max: tuple = (769, 769)
    mouse_web_xy_regular_max: tuple = (1000, 769)

    # Alternativpunkte (ohne Rechnen)
    more_hover_alternates: tuple = ((768,764),(768,764),(768,764),(768,764),(768,764))
    web_regular_alternates: tuple = ((1000,780),(990,780),(1010,780),(1000,770),(1000,790))
    plus_alternates: tuple = ((630,530),(620,530),(640,530),(630,520),(630,540))
    temp_web_alternates: tuple = ((777,644),(767,644),(787,644),(777,634),(777,654))

    hover_backend = "playwright"

    more_hover_offset = (0, 0)  # 66px rechts, 17px hoch

    # --- Playwright Timeouts (aggressiv kurz) ---
    timeout_default_ms: int = 350
    timeout_nav_ms: int = 6000
    timeout_short_ms: int = 300
    json_total_timeout_s: int = 75
    json_settle_s: float = 0.3
    hover_backend = "os"         # "os" = PyAutoGUI bewegt die echte Maus (empfohlen für Hover-Menüs)
    hover_dwell_more = 0.35      # Verweilzeit über "More"
    mouse_move_duration = 0.12   # smooth
    
    # --- FAST INPUT (großer Boost) ---
    # Für lange Prompts NICHT tippen, sondern direkt ins DOM setzen
    fast_insert_text: bool = True
    keyboard_insert_threshold: int = 120  # bis zu dieser Länge darf getippt werden

    # --- ownAPI Defaults ---
    project_name: str = "Bewerbungs_Log"
    worksheet: str = "Anwalt Log"
    worksheet_gid: int = 268410648
    sheet_url: str = "https://docs.google.com/spreadsheets/d/1Wa8kL5kiDuw-0XYqF2iuFqkItg7dhFNydBELwtJBKlI/edit?usp=sharing"

    status_path: str = "status.json"
    update_path: str = "update.json"
    memory_path: str = "memory.json"

    engine_cmd: str = field(default_factory=lambda: f"{sys.executable} gpt_spreadsheet_interface.py --debug")

    max_rounds: int = 6
    repeat_on_error: bool = True
