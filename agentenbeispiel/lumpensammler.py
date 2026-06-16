#!/usr/bin/env python3
"""
Lumpensammler — Bewerbungsteam-Agent

Sucht via Perplexity nach passenden Stellenangeboten, liest die
tatsächlichen Ausschreibungs-URLs und extrahiert strukturierte Daten.

Ablauf:
  1. Perplexity-Suche (Query aus Profil, gecacht)
  2. URLs aus Ergebnis extrahieren
  3. Jede URL einzeln lesen → strukturierte Daten extrahieren
  4. Bei ausreichender Qualität: DB-Eintrag + Ausschreibung.md

Aufruf:
  python3 lumpensammler.py              # poll (Standard)
  python3 lumpensammler.py poll         # dasselbe
  python3 lumpensammler.py init         # einmalige Suche
  python3 lumpensammler.py init --seed "TEXT"
"""

import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Pfade ─────────────────────────────────────────────────────────────────────
LUMPS_DIR   = Path(__file__).parent.resolve()
PARENT_DIR  = LUMPS_DIR.parent
sys.path.insert(0, str(LUMPS_DIR))

# ── Imports ───────────────────────────────────────────────────────────────────
from rich.console  import Console
from rich.panel    import Panel
from rich.rule     import Rule

from tools.llm_client import LLMClient
from tools.datenbank  import (
    add_entry, check_exists, check_url_exists, get_stellen_by_firma,
    get_next_ref, get_row, get_all_rows, get_unprocessed, fill_slot,
    mark_lumpensammler_done, clear_review, _update_row, STATUS_NEU,
    reserve_ref,
)

# ── Dateipfade ────────────────────────────────────────────────────────────────
CONF_FILE       = LUMPS_DIR  / "lumpensammler.conf"
AGENT_MD        = LUMPS_DIR  / "Agent.md"
SOLE_MD         = LUMPS_DIR  / "Sole.md"
BEHAVIOR_MD     = LUMPS_DIR  / "Behavior.md"
KOMPETENZEN_MD  = PARENT_DIR / "Kompetenzen.md"
LEBENSLAUF_MD   = PARENT_DIR / "Lebenslauf.md"
OWLTRAIL_CONF   = PARENT_DIR / "owltrail.conf"
BEWERBUNGEN_DIR = PARENT_DIR / "Bewerbungen"
LOGS_DIR        = LUMPS_DIR  / "logs"
QUERY_CACHE     = LUMPS_DIR  / "query_cache.json"

DATENBANK_XLS   = None  # Legacy-Param, DB läuft via owAPI

_is_pipe = not sys.stdout.isatty()
console = Console(no_color=_is_pipe, highlight=False)


# ─────────────────────────────── Logging ─────────────────────────────────────

def setup_logging(conf: dict) -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    log_cfg  = conf.get("log", {})
    enabled  = log_cfg.get("enabled", True)
    level    = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    logger   = logging.getLogger("lumpensammler")
    logger.setLevel(level)
    if enabled and not logger.handlers:
        logfile = LOGS_DIR / f"lumpensammler_{datetime.now():%Y-%m-%d}.log"
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
        logger.addHandler(fh)
    return logger


# ─────────────────────────────── Konfiguration ────────────────────────────────

def load_conf() -> dict:
    defaults: dict = {
        "models": {
            "profile":       "207",
            "query_builder": "207",
            "perplexity":    "115",
        },
        "search": {
            "max_results_per_run": 5,
            "poll_interval_sec":   120,
            "pause_between_urls_sec": 3,
        },
        "log": {"enabled": True, "level": "INFO"},
        "suchkriterien": {
            "standort_modus": "orte",
            "orte": [{"name": "Kassel", "radius_km": 50}],
            "land": "",
            "verbund": "",
            "level": {
                "kompetenz": 0.6,
                "gehalt_von": 4000,
                "gehalt_bis": 7000,
                "anstellung": "festanstellung",
            },
            "blacklist_domains": [],
        },
    }
    if CONF_FILE.exists():
        user = json.loads(CONF_FILE.read_text())
        for k, v in user.items():
            if isinstance(v, dict) and k in defaults:
                defaults[k].update(v)
            else:
                defaults[k] = v
    return defaults


def get_config() -> dict:
    return json.loads(CONF_FILE.read_text(encoding="utf-8")) if CONF_FILE.exists() else {}


def update_config(updates: dict):
    conf = get_config()
    if "suchkriterien" in updates:
        conf["suchkriterien"] = updates["suchkriterien"]
    CONF_FILE.write_text(
        json.dumps(conf, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    if QUERY_CACHE.exists():
        QUERY_CACHE.unlink()


def read_file(path: Path, fallback: str = "") -> str:
    return path.read_text(encoding="utf-8") if path.exists() else fallback


def build_system_prompt() -> str:
    parts = []
    for f, label in [(AGENT_MD, "Identität"), (SOLE_MD, "Zweck"), (BEHAVIOR_MD, "Verhalten")]:
        if f.exists():
            parts.append(f"## {label}\n\n{f.read_text(encoding='utf-8')}")
    return "\n\n---\n\n".join(parts) if parts else "Du bist der Lumpensammler im Bewerbungsteam."


def trunc(text: str, max_chars: int = 8_000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[… {len(text) - max_chars} Zeichen gekürzt …]"


# ─────────────────────────────── TUI-Helfer ───────────────────────────────────

def banner():
    console.print()
    console.print(Panel.fit(
        "[bold magenta]◆  Lumpensammler[/bold magenta]\n"
        "[dim]Bewerbungsteam · Stellenrecherche · v2.0[/dim]",
        border_style="magenta",
        padding=(1, 4),
    ))
    console.print()


def step(n: int, title: str):
    console.print()
    console.print(Rule(
        f"[bold yellow] Schritt {n}: {title} [/bold yellow]",
        style="yellow dim",
    ))
    console.print()


def ok(msg: str):   console.print(f"  [green]✓[/green]  {msg}")
def info(msg: str): console.print(f"  [cyan]→[/cyan]  {msg}")
def warn(msg: str): console.print(f"  [yellow]⚠[/yellow]  {msg}")
def err(msg: str):  console.print(f"  [red]✗[/red]  {msg}")


# ─────────────────────────────── JSON-Extraktion ──────────────────────────────

def extract_json(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        return json.loads(m.group(1))
    for i, c in enumerate(text):
        if c in ("{", "["):
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                pass
    raise ValueError("Kein JSON im LLM-Output gefunden")


# ─────────────────────────── Perplexity-Bereinigung ───────────────────────────

_BACKEND_ARTIFACTS = re.compile(
    r"^\[User\]:.*?\n\[Assistant\]:\s*"
    r"(?:\d+ Schritte? abgeschlossen\s*)?"
    r"(?:#START_RESPONSE\s*)?",
    re.DOTALL | re.IGNORECASE,
)

def _clean_perplexity(text: str) -> str:
    text = _BACKEND_ARTIFACTS.sub("", text).strip()
    if text.endswith("endmarkierung:"):
        text = text[: text.rfind("endmarkierung:")].strip()
    return text


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE 1: Query-Cache — Profil + Suchquery einmal berechnen, wiederverwenden
# ═══════════════════════════════════════════════════════════════════════════════

def _get_profile_mtime() -> float:
    mtimes = []
    for p in (KOMPETENZEN_MD, LEBENSLAUF_MD):
        if p.exists():
            mtimes.append(p.stat().st_mtime)
    return max(mtimes) if mtimes else 0.0


_search_cycle = 0
_profile_cache: str | None = None
_query_pool: list[str] = []


def _suchkriterien_hash(conf: dict) -> str:
    sk = conf.get("suchkriterien", {})
    return hashlib.md5(json.dumps(sk, sort_keys=True).encode()).hexdigest()


def _load_query_cache(conf: dict) -> dict | None:
    if not QUERY_CACHE.exists():
        return None
    try:
        cache = json.loads(QUERY_CACHE.read_text(encoding="utf-8"))
        if cache.get("profile_mtime", 0) < _get_profile_mtime():
            return None
        if cache.get("suchkriterien_hash") != _suchkriterien_hash(conf):
            return None
        return cache
    except Exception:
        pass
    return None


def _save_query_cache(profile_summary: str, conf: dict):
    QUERY_CACHE.write_text(json.dumps({
        "profile_summary": profile_summary,
        "profile_mtime": _get_profile_mtime(),
        "suchkriterien_hash": _suchkriterien_hash(conf),
        "created": datetime.now().isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_profile(llm: LLMClient, sys_prompt: str,
                    conf: dict, log: logging.Logger) -> str:
    """Return cached profile summary; compute once."""
    global _profile_cache
    if _profile_cache:
        return _profile_cache

    cached = _load_query_cache(conf)
    if cached and cached.get("profile_summary"):
        _profile_cache = cached["profile_summary"]
        ok(f"Profil aus Cache")
        return _profile_cache

    step(1, "Profil laden")
    kompetenzen = read_file(KOMPETENZEN_MD)
    lebenslauf  = read_file(LEBENSLAUF_MD)

    info("LLM analysiert Profil …")
    _profile_cache = llm.chat([
        {"role": "system", "content": sys_prompt},
        {"role": "user",   "content": (
            "Fasse das Bewerberprofil in 3–5 Stichwortzeilen zusammen "
            "(Berufsbezeichnung, Kernkompetenzen, Branchenerfahrung, Standort).\n\n"
            f"Kompetenzen:\n{trunc(kompetenzen, 3000)}\n\n"
            f"Lebenslauf:\n{trunc(lebenslauf, 2000)}\n\n"
            "Gib NUR die Stichwortzeilen aus."
        )},
    ], step="profile", temperature=0.3)
    _save_query_cache(_profile_cache, conf)
    ok("Profilanalyse fertig")
    return _profile_cache


_KOMPETENZ_MAP = [
    (0.0, "Ausbildung, Praktikum, Werkstudent"),
    (0.2, "Berufseinsteiger, Junior"),
    (0.4, "Fachkraft, Techniker"),
    (0.6, "Ingenieur, Entwickler, Spezialist"),
    (0.8, "Senior Engineer, Teamlead, Projektleiter"),
    (1.0, "Direktor, Professor, VP Engineering"),
]

_ANSTELLUNG_MAP = {
    "festanstellung": "Festanstellung",
    "befristet": "befristete Anstellung",
    "freiberuflich": "Freelance / freiberuflich",
    "alle": "alle Anstellungsarten",
}

_VERBUND_REGIONEN = {
    "EU": "Europäische Union",
    "USA": "USA",
    "BRICS": "BRICS-Staaten (Brasilien, Russland, Indien, China, Südafrika)",
    "Asien": "Asien",
    "Ostblock": "Osteuropa",
    "Naher Osten": "Naher Osten",
    "Lateinamerika": "Lateinamerika",
}


def _build_suchkriterien_prompt(conf: dict) -> str:
    sk = conf.get("suchkriterien", {})
    parts = []

    modus = sk.get("standort_modus", "orte")
    if modus == "orte":
        orte = sk.get("orte", [{"name": "Kassel", "radius_km": 50}])
        if orte:
            ort = random.choice(orte)
            radius = int(ort.get("radius_km", 50) * random.uniform(0.8, 1.2))
            parts.append(f"Region: im Umkreis von {radius} km um {ort['name']}")
    elif modus == "land":
        land = sk.get("land", "Deutschland")
        if land:
            parts.append(f"Region: in {land}")
    elif modus == "verbund":
        verbund = sk.get("verbund", "EU")
        label = _VERBUND_REGIONEN.get(verbund, verbund)
        parts.append(f"Region: {label}")
    elif modus == "weltweit":
        parts.append("Region: weltweit, keine Ortsbeschränkung")

    level = sk.get("level", {})
    kompetenz = level.get("kompetenz", 0.6)
    nearest = min(_KOMPETENZ_MAP, key=lambda x: abs(x[0] - kompetenz))
    parts.append(f"Stellenlevel: {nearest[1]}")

    gehalt_von = level.get("gehalt_von", 4000)
    gehalt_bis = level.get("gehalt_bis", 7000)
    parts.append(
        f"Gehaltsrahmen: ca. {gehalt_von}–{gehalt_bis} EUR brutto/Monat "
        f"(nur Richtwert, Stellen ohne Gehaltsangabe trotzdem einbeziehen)"
    )

    anstellung = level.get("anstellung", "festanstellung")
    if anstellung != "alle":
        parts.append(f"Anstellungsart: {_ANSTELLUNG_MAP.get(anstellung, anstellung)}")

    return "\n".join(parts)


def generate_query(llm: LLMClient, sys_prompt: str,
                   known_firms: list[str], cycle: int,
                   seed: str | None, conf: dict,
                   log: logging.Logger) -> str:
    """Generate a NEW varied search query each cycle."""
    profile = _ensure_profile(llm, sys_prompt, conf, log)
    sk_prompt = _build_suchkriterien_prompt(conf)

    known_hint = ""
    if known_firms:
        known_hint = (
            f"\n\nBei diesen Firmen wurden bereits Stellen gefunden: "
            f"{', '.join(known_firms[:20])}. "
            f"Suche bevorzugt bei ANDEREN Firmen, aber wenn diese Firmen "
            f"weitere passende Stellen haben, zeige sie trotzdem."
        )

    seed_hint = f"\n\nNutzer-Einschränkung: {seed}" if seed else ""

    info("LLM generiert neue Suchanfrage …")
    query = llm.chat([
        {"role": "system", "content": sys_prompt},
        {"role": "user",   "content": (
            f"Erstelle eine Perplexity-Suchanfrage (max. 500 Zeichen, auf Deutsch).\n\n"
            f"Ziel: Stellenangebote finden, die zum folgenden Bewerber passen.\n\n"
            f"Bewerber-Profil:\n{profile}\n\n"
            f"Suchkriterien:\n{sk_prompt}\n\n"
            f"Dies ist Suchdurchlauf Nr. {cycle + 1}. "
            f"Variiere die Suchbegriffe! Nutze andere Berufsbezeichnungen, "
            f"Synonyme, andere Branchen als in vorherigen Suchen. "
            f"Zum Beispiel: mal 'Embedded Entwickler', mal 'Systemingenieur', "
            f"mal 'Hardware Engineer', mal 'FPGA Entwickler'."
            f"{known_hint}{seed_hint}\n\n"
            f"Wichtig:\n"
            f"- Formuliere so, dass Perplexity KONKRETE Stellenausschreibungen findet\n"
            f"- Frage nach direkten Links zu einzelnen Stellen auf Firmenwebseiten "
            f"(z.B. firma.com/karriere/stelle-123), NICHT nach Jobportal-Suchseiten "
            f"(stepstone.de/jobs/, indeed.com/q-, linkedin.com/jobs/ Listings)\n"
            f"- Nenne konkrete Firmennamen oder Karriereseiten statt Jobportale\n"
            f"- Verwende KEINE Suchoperatoren wie -Indeed oder -Stepstone, "
            f"das funktioniert bei Perplexity nicht\n\n"
            f"Gib NUR den Suchtext aus."
        )},
    ], step="query_builder", temperature=0.9)
    ok(f"Query #{cycle + 1} fertig")
    console.print(Panel(f"[dim]{query}[/dim]",
                        title=f"[dim]→ Perplexity #{cycle + 1}[/dim]",
                        border_style="dim"))
    log.info(f"Query #{cycle + 1}: {query}")
    return query


def ensure_query(llm: LLMClient, sys_prompt: str, seed: str | None,
                 conf: dict, log: logging.Logger) -> str:
    """Legacy wrapper for init mode."""
    return generate_query(llm, sys_prompt, [], 0, seed, conf, log)


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE 2: Perplexity-Suche
# ═══════════════════════════════════════════════════════════════════════════════

MIN_PERPLEXITY_CHARS = 200

def perplexity_search(llm: LLMClient, query: str,
                      log: logging.Logger) -> str:
    """Run Perplexity web search with retry."""
    step(2, "Perplexity-Recherche")
    info("Web-Recherche läuft …")

    enriched_query = (
        f"{query}\n\n"
        f"WICHTIG: Gib direkte Links zu einzelnen Stellenausschreibungen, "
        f"NICHT zu Suchseiten oder Listing-Seiten von Jobportalen "
        f"(keine stepstone.de/jobs/, indeed.com/q-, linkedin.com/jobs/ etc.). "
        f"Bevorzuge Links direkt von Firmenwebseiten oder Karriereportalen."
    )

    for attempt in range(1, 3):
        try:
            raw = llm.chat(
                [{"role": "user", "content": enriched_query}],
                step="perplexity",
                temperature=0.1,
            )
            result = _clean_perplexity(raw)

            console.print(Panel(
                f"[dim]Länge: {len(result)} Zeichen[/dim]\n\n{result[:3000]}",
                title=f"[cyan]Perplexity (Versuch {attempt})[/cyan]",
                border_style="cyan" if len(result) >= MIN_PERPLEXITY_CHARS else "red",
            ))

            if len(result) < MIN_PERPLEXITY_CHARS:
                warn(f"Antwort zu kurz ({len(result)} Zeichen)")
                log.warning(f"Perplexity Versuch {attempt}: zu kurz ({len(result)})")
                if attempt < 2:
                    info("Warte 10s und versuche erneut …")
                    time.sleep(10)
                continue

            ok(f"Perplexity geantwortet ({len(result)} Zeichen)")
            log.info(f"Perplexity OK: {len(result)} Zeichen")
            return result

        except Exception as e:
            warn(f"Perplexity-Fehler (Versuch {attempt}): {e}")
            log.warning(f"Perplexity Versuch {attempt} fehlgeschlagen: {e}")
            if attempt < 2:
                info("Warte 10s …")
                time.sleep(10)

    raise RuntimeError("Perplexity kein verwertbares Ergebnis (2 Versuche)")


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE 3: URLs extrahieren + einzeln lesen
# ═══════════════════════════════════════════════════════════════════════════════

_SKIP_DOMAINS = {
    "wikipedia.org", "google.com", "youtube.com", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "reddit.com", "tiktok.com",
    "germantechjobs.de", "jobtensor.com", "devjobs.de",
    "glassdoor.de", "arbeitsagentur.de", "kununu.com",
    "wlw.de", "ingenieurcenter.de",
    "xing.com", "hannovermesse.de",
}

_LISTING_RE = re.compile(
    r"indeed\.com/(?:q-|jobs\?)"
    r"|stepstone\.de/jobs/"
    r"|linkedin\.com/jobs/(?!view/)"
    r"|monster\.de/jobs/"
    r"|karriere\.de/jobs/"
    r"|jobs\.heise\.de/.+/jobs/"
    r"|kimeta\.de/",
    re.IGNORECASE,
)

def extract_urls(text: str, log: logging.Logger | None = None,
                 blacklist_domains: list[str] | None = None) -> list[str]:
    """Extract unique, relevant URLs from Perplexity response."""
    skip = _SKIP_DOMAINS | set(blacklist_domains or [])
    raw_urls = re.findall(r'https?://[^\s\)>\]"\']+', text)
    seen: set[str] = set()
    result: list[str] = []
    skipped: list[str] = []
    for url in raw_urls:
        url = url.rstrip(".,;:)")
        if url in seen:
            continue
        seen.add(url)
        if any(d in url.lower() for d in skip):
            skipped.append(url)
            continue
        if _LISTING_RE.search(url):
            skipped.append(url)
            continue
        result.append(url)
    if log and skipped:
        log.info(f"URLs gefiltert (Listing/Skip): {skipped}")
    return result


_POSTING_SCHEMA = """{
  "valid": true,
  "firma": "Firmenname",
  "titel": "Stellenbezeichnung",
  "standort": "Stadt",
  "region": "Bundesland oder Region",
  "firma_adresse": "Straße Nr, PLZ Ort",
  "beschreibung": "Vollständige Stellenbeschreibung",
  "aufgaben": ["Aufgabe 1", "Aufgabe 2"],
  "anforderungen_muss": ["Muss-Anforderung 1"],
  "anforderungen_soll": ["Soll-Anforderung 1"],
  "angebot": ["Benefit 1"],
  "datum_ausschreibung": "YYYY-MM-DD oder leer",
  "bewerbungsfrist": "YYYY-MM-DD oder leer",
  "kontakt_name": "Ansprechpartner",
  "kontakt_email": "bewerbung@firma.de",
  "kontakt_telefon": "+49 ...",
  "url": "https://..."
}"""


_MIN_PAGE_CHARS = 100

try:
    import cloudscraper as _cloudscraper
except ImportError:
    _cloudscraper = None

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

_pw_browser = None

def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)[:15000]


def _fetch_requests(url: str, timeout: int = 20) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return _html_to_text(resp.text)
    except Exception:
        return None


def _fetch_cloudscraper(url: str) -> str | None:
    if not _cloudscraper:
        return None
    try:
        s = _cloudscraper.create_scraper()
        resp = s.get(url, timeout=20)
        resp.raise_for_status()
        return _html_to_text(resp.text)
    except Exception:
        return None


def _fetch_playwright(url: str) -> str | None:
    if not _HAS_PLAYWRIGHT:
        return None
    import threading
    if threading.current_thread() is not threading.main_thread():
        return None
    global _pw_browser
    try:
        if not _pw_browser:
            pw = _sync_playwright().__enter__()
            _pw_browser = pw.chromium.launch(headless=True)
        page = _pw_browser.new_page()
        try:
            page.goto(url, timeout=25000, wait_until="networkidle")
            html = page.content()
            return _html_to_text(html)
        finally:
            page.close()
    except Exception:
        return None


def _fetch_page_text(url: str, timeout: int = 20) -> str | None:
    """Fetch URL with 3-tier fallback: requests → cloudscraper → playwright."""
    text = _fetch_requests(url, timeout)
    if text and len(text) >= _MIN_PAGE_CHARS:
        return text

    text = _fetch_cloudscraper(url)
    if text and len(text) >= _MIN_PAGE_CHARS:
        return text

    text = _fetch_playwright(url)
    if text and len(text) >= _MIN_PAGE_CHARS:
        return text

    return None


def read_job_posting(llm: LLMClient, url: str,
                     log: logging.Logger) -> dict | None:
    """Fetch URL directly, then use model 200 to extract structured JSON."""
    page_text = _fetch_page_text(url)

    if not page_text or len(page_text) < 100:
        log.info(f"URL nicht lesbar oder zu wenig Inhalt: {url}")
        return None

    extract_prompt = (
        f"Extrahiere aus dem folgenden Stellenausschreibungstext "
        f"alle Informationen als JSON.\n\n"
        f"Text der Stellenausschreibung:\n"
        f"---\n{page_text}\n---\n\n"
        f"JSON-Schema:\n{_POSTING_SCHEMA}\n\n"
        f"Regeln:\n"
        f"- NUR tatsächlich im Text vorhandene Informationen extrahieren\n"
        f"- Nichts erfinden oder halluzinieren\n"
        f"- Felder die nicht ermittelbar sind: leeren String setzen\n"
        f"- Datumsformat: YYYY-MM-DD\n"
        f"- beschreibung: den vollständigen Stellentext übernehmen, nicht kürzen\n"
        f"- Falls der Text keine Stellenausschreibung enthält: "
        f'  {{"valid": false}} zurückgeben\n'
        f"- Gib NUR das JSON aus, keine Erklärungen"
    )

    try:
        raw = llm.chat(
            [{"role": "user", "content": extract_prompt}],
            step="profile",
            temperature=0.1,
        )
        data = extract_json(raw)

        if isinstance(data, list):
            valid = [d for d in data if isinstance(d, dict) and d.get("valid", True)]
            if not valid:
                log.info(f"URL keine Stellenausschreibung (Liste): {url}")
                return None
            for d in valid:
                d["url"] = d.get("url", "") or url
            log.info(f"URL gelesen ({len(valid)} Stellen): {url}")
            return valid

        if not data.get("valid", True):
            log.info(f"URL keine Stellenausschreibung: {url}")
            return None

        data["url"] = data.get("url", "") or url
        log.info(f"URL gelesen: {url} → {data.get('firma', '?')} – {data.get('titel', '?')}")
        return data

    except Exception as e:
        log.warning(f"URL-Extraktion fehlgeschlagen: {url}: {e}")
        return None


def validate_posting(data: dict) -> bool:
    """Check minimum data quality: firma + titel + (contact OR description)."""
    if not data:
        return False
    if not data.get("firma") or not data.get("titel"):
        return False
    has_contact = any([
        data.get("kontakt_email"),
        data.get("kontakt_telefon"),
        data.get("kontakt_name"),
    ])
    has_description = len(data.get("beschreibung", "")) > 50
    return has_contact or has_description


# ═══════════════════════════════════════════════════════════════════════════════
#  Ausschreibung.md + Tagebuch aus echten Daten
# ═══════════════════════════════════════════════════════════════════════════════

def build_ausschreibung_md(ref: str, data: dict) -> str:
    """Build Ausschreibung.md from real extracted data (no LLM fabrication)."""
    lines = [
        f"# {data.get('titel', 'Stellenausschreibung')}",
        "",
        f"**Unternehmen:** {data.get('firma', '—')}",
        f"**Standort:** {data.get('standort', '—')}",
        f"**Referenz:** {ref}",
    ]
    if data.get("region"):
        lines.append(f"**Region:** {data['region']}")
    if data.get("firma_adresse"):
        lines.append(f"**Adresse:** {data['firma_adresse']}")
    if data.get("datum_ausschreibung"):
        lines.append(f"**Veröffentlicht:** {data['datum_ausschreibung']}")
    if data.get("bewerbungsfrist"):
        lines.append(f"**Bewerbungsfrist:** {data['bewerbungsfrist']}")
    if data.get("kontakt_name"):
        lines.append(f"**Ansprechpartner:** {data['kontakt_name']}")
    if data.get("kontakt_email"):
        lines.append(f"**E-Mail:** {data['kontakt_email']}")
    if data.get("kontakt_telefon"):
        lines.append(f"**Telefon:** {data['kontakt_telefon']}")
    if data.get("url"):
        lines.append(f"**URL:** {data['url']}")

    lines += ["", "---", ""]

    if data.get("beschreibung"):
        lines += [data["beschreibung"], ""]

    if data.get("aufgaben"):
        lines += ["## Aufgaben", ""]
        for a in data["aufgaben"]:
            lines.append(f"- {a}")
        lines.append("")

    if data.get("anforderungen_muss"):
        lines += ["## Anforderungen", "", "**Muss:**", ""]
        for a in data["anforderungen_muss"]:
            lines.append(f"- {a}")
        lines.append("")

    if data.get("anforderungen_soll"):
        lines += ["**Soll:**", ""]
        for a in data["anforderungen_soll"]:
            lines.append(f"- {a}")
        lines.append("")

    if data.get("angebot"):
        lines += ["## Wir bieten", ""]
        for a in data["angebot"]:
            lines.append(f"- {a}")
        lines.append("")

    lines += ["---", "", f"*Quelle: {data.get('url', 'Perplexity-Recherche')}*", ""]
    return "\n".join(lines)


def make_tagebuch(ref: str, data: dict, xlsx_path: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Tagebuch"
    headers = ["Datum", "Ereignis", "Status", "Notiz"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font      = Font(bold=True, color="FFFFFF")
        cell.fill      = PatternFill("solid", fgColor="1A237E")
        cell.alignment = Alignment(horizontal="center")
    for col, w in zip("ABCD", [14, 40, 20, 50]):
        ws.column_dimensions[col].width = w
    ws.append([
        datetime.now(),
        "Stelle gefunden / angelegt",
        STATUS_NEU,
        f"{data.get('firma', '')} – {data.get('titel', '')}",
    ])
    wb.save(str(xlsx_path))


# ═══════════════════════════════════════════════════════════════════════════════
#  DB-Commit: Posting speichern (DB + Dateisystem)
# ═══════════════════════════════════════════════════════════════════════════════

def commit_posting(ref: str, data: dict, is_new_row: bool,
                   log: logging.Logger, keine_url: bool = False) -> bool:
    """Save a validated posting to DB + filesystem."""
    reserve_ref(ref)

    if is_new_row:
        add_entry(DATENBANK_XLS, {"ref": ref})

    app_dir = BEWERBUNGEN_DIR / ref
    app_dir.mkdir(parents=True, exist_ok=True)

    ausschr_md = build_ausschreibung_md(ref, data)
    (app_dir / "Ausschreibung.md").write_text(ausschr_md, encoding="utf-8")
    ok(f"[{ref}] Ausschreibung.md geschrieben ({len(ausschr_md)} Zeichen)")

    pfad = f"Bewerbungen/{ref}/Ausschreibung.md"
    url_value = data.get("url", "") or pfad if keine_url else data.get("url", "")

    updates = {
        "Stelle":               data.get("titel", ""),
        "Unternehmen":          data.get("firma", ""),
        "Datum":                datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Standort":             data.get("standort", ""),
        "URL":                  url_value,
        "Status":               STATUS_NEU,
        "Zustaendigkeit":       "LPS",
        "Datum_Ausschreibung":  data.get("datum_ausschreibung", ""),
        "Bewerbungsfrist":      data.get("bewerbungsfrist", ""),
        "Kontakt_Email":        data.get("kontakt_email", ""),
        "Kontakt_Telefon":      data.get("kontakt_telefon", ""),
        "Kontakt_Post":         data.get("firma_adresse", ""),
    }
    if keine_url:
        updates["Keine_URL"] = True

    _update_row(ref, updates)

    tagebuch_path = app_dir / "Tagebuch.xlsx"
    if not tagebuch_path.exists():
        make_tagebuch(ref, data, tagebuch_path)

    mark_lumpensammler_done(DATENBANK_XLS, ref, ausschreibung_pfad=pfad)
    url_hint = "(keine URL)" if keine_url else f"(URL: {data.get('url', '?')})"
    ok(f"[{ref}] {data.get('firma', '?')} – {data.get('titel', '?')}  → UBW")
    log.info(f"[{ref}] Committed: {data.get('firma')} – {data.get('titel')} {url_hint}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  Kern-Workflow: URLs durchgehen, Slots füllen
# ═══════════════════════════════════════════════════════════════════════════════

def fill_from_search(llm: LLMClient, search_result: str,
                     empty_refs: list[str], conf: dict,
                     log: logging.Logger) -> list[str]:
    """
    Extract URLs from Perplexity search result, read URLs in parallel,
    and fill empty slots with valid postings.
    Returns list of filled refs.
    """
    step(3, "URLs lesen & Daten extrahieren")

    blacklist = conf.get("suchkriterien", {}).get("blacklist_domains", [])
    urls = extract_urls(search_result, log, blacklist_domains=blacklist)
    if not urls:
        warn("Keine URLs in Perplexity-Ergebnis gefunden")
        log.warning("Keine URLs extrahiert")
        return []

    info(f"{len(urls)} URL(s) gefunden")
    for i, url in enumerate(urls, 1):
        console.print(f"    [dim]{i}. {url[:80]}[/dim]")
    log.info(f"{len(urls)} URLs extrahiert: {urls}")

    max_per_run = conf.get("search", {}).get("max_results_per_run", 15)
    parallel    = conf.get("search", {}).get("parallel_reads", 3)
    refs_pool   = list(empty_refs)
    filled: list[str] = []

    candidate_urls = [u for u in urls if not check_url_exists(DATENBANK_XLS, u)]
    skipped = len(urls) - len(candidate_urls)
    if skipped:
        info(f"{skipped} URL(s) bereits bekannt, übersprungen")

    if not candidate_urls:
        warn("Alle URLs bereits in DB")
        return []

    info(f"Lese {len(candidate_urls)} URL(s) parallel ({parallel} Threads) …")

    results: list[tuple[str, dict | None]] = []

    def _read_url(url: str) -> tuple[str, dict | None]:
        return (url, read_job_posting(llm, url, log))

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(_read_url, u): u for u in candidate_urls}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                url = futures[future]
                log.warning(f"URL-Read Fehler: {url}: {e}")
                results.append((url, None))

    failed_urls = [url for url, data in results if not data]
    if failed_urls and _HAS_PLAYWRIGHT:
        info(f"{len(failed_urls)} URL(s) im Playwright-Nachgang …")
        for url in failed_urls[:5]:
            data = read_job_posting(llm, url, log)
            if data:
                results = [(u, d) if u != url else (url, data) for u, d in results]

    ok(f"{len(results)} URL(s) gelesen, prüfe Ergebnisse …")

    for url, data in results:
        if len(filled) >= max_per_run:
            info(f"Maximum ({max_per_run}) erreicht, stoppe")
            break

        if not data:
            continue

        postings = data if isinstance(data, list) else [data]

        for posting in postings:
            if len(filled) >= max_per_run:
                break

            if not validate_posting(posting):
                warn(f"Unvollständig: {posting.get('firma', '?')} – {posting.get('titel', '?')}")
                continue

            firma = posting.get("firma", "")
            titel = posting.get("titel", "")

            if check_exists(DATENBANK_XLS, firma, titel):
                info(f"Duplikat (Firma+Titel): {firma} – {titel}")
                continue

            if refs_pool:
                ref = refs_pool.pop(0)
                is_new = False
            else:
                ref = get_next_ref(DATENBANK_XLS)
                is_new = True

            print(f"\n  ═══ [{ref}] {firma[:30]} — {titel[:40]} ═══", flush=True)

            commit_posting(ref, posting, is_new, log)
            filled.append(ref)

    return filled


# ═══════════════════════════════════════════════════════════════════════════════
#  Teildaten: Zeile hat Firma/Stelle aber fehlt Ausschreibung.md
# ═══════════════════════════════════════════════════════════════════════════════

def _review_search(llm: LLMClient, ref: str, row: dict, review: str,
                    log: logging.Logger) -> dict | None:
    """Gezielt per Perplexity nachrecherchieren basierend auf Überwacher-Review."""
    firma = row.get("Unternehmen", "")
    stelle = row.get("Stelle", "")
    standort = row.get("Standort", "") or "Deutschland"
    url = str(row.get("URL", "")).strip()

    query = (
        f"Stellenanzeige: {stelle} bei {firma}, Standort {standort}.\n"
        f"Folgende Informationen fehlen oder sind mangelhaft: {review}\n\n"
        f"Finde die fehlenden Informationen — insbesondere:\n"
        f"- Ansprechpartner (Name, E-Mail, Telefon)\n"
        f"- Firmenadresse / Postanschrift\n"
        f"- Bewerbungsfrist und Datum der Ausschreibung\n"
        f"- Direkte URL zur Stellenausschreibung\n\n"
        f"Suche auf der Karriereseite von {firma}, auf LinkedIn, XING "
        f"und in Branchenverzeichnissen."
    )

    info(f"[{ref}] Review-Nachrecherche via Perplexity …")
    log.info(f"[{ref}] Review-Suche: {review}")

    try:
        raw = llm.chat(
            [{"role": "user", "content": query}],
            step="perplexity",
            temperature=0.1,
        )
        raw = _clean_perplexity(raw)
    except Exception as e:
        err(f"[{ref}] Perplexity Review-Suche fehlgeschlagen: {e}")
        log.error(f"[{ref}] Review-Suche Fehler: {e}")
        return None

    extract_prompt = (
        f"Extrahiere aus dem folgenden Recherchergebnis alle verfügbaren Kontakt- "
        f"und Stelleninformationen als JSON.\n\n"
        f"Kontext: Es geht um die Stelle '{stelle}' bei '{firma}'.\n"
        f"Fehlende/bemängelte Infos laut Review: {review}\n\n"
        f"Recherchergebnis:\n---\n{raw}\n---\n\n"
        f"JSON-Schema:\n{_POSTING_SCHEMA}\n\n"
        f"Regeln:\n"
        f"- NUR tatsächlich im Text gefundene Informationen\n"
        f"- Leere Strings für nicht gefundene Felder\n"
        f"- Gib NUR das JSON aus"
    )

    try:
        result = llm.chat(
            [{"role": "user", "content": extract_prompt}],
            step="profile",
            temperature=0.1,
        )
        return extract_json(result)
    except Exception as e:
        log.warning(f"[{ref}] Review-Extraktion fehlgeschlagen: {e}")
        return None


def _process_urlless(llm: LLMClient, ref: str, row: dict,
                     log: logging.Logger) -> bool:
    """Verarbeite einen Eintrag ohne Web-URL (Keine_URL=TRUE).
    Rohdaten aus Ausschreibung_Roh.md oder Quelle_David.md extrahieren."""
    app_dir = BEWERBUNGEN_DIR / ref
    ausschr_md = app_dir / "Ausschreibung.md"

    if ausschr_md.exists() and ausschr_md.stat().st_size > 200:
        mark_lumpensammler_done(DATENBANK_XLS, ref,
                                ausschreibung_pfad=f"Bewerbungen/{ref}/Ausschreibung.md")
        ok(f"[{ref}] URL-los, Ausschreibung.md vorhanden — LOK=TRUE")
        log.info(f"[{ref}] URL-los, bereits komplett")
        return True

    raw_text = None
    for candidate in ("Ausschreibung_Roh.md", "Quelle_David.md"):
        src = app_dir / candidate
        if src.exists():
            raw_text = src.read_text(encoding="utf-8")
            info(f"[{ref}] Roh-Text aus {candidate} ({len(raw_text)} Zeichen)")
            log.info(f"[{ref}] URL-los, Quelle: {candidate}")
            break

    if not raw_text or len(raw_text) < 50:
        firma = row.get("Unternehmen", "")
        stelle = row.get("Stelle", "")
        if firma and stelle:
            info(f"[{ref}] Kein Roh-Text, suche via Perplexity …")
            query = (
                f"Stellenanzeige: {stelle} bei {firma} "
                f"Standort {row.get('Standort') or 'Deutschland'}. "
                f"Gib alle verfügbaren Informationen: Aufgaben, Anforderungen, "
                f"Ansprechpartner, Kontaktdaten, Bewerbungsfrist."
            )
            try:
                resp = llm.chat(
                    [{"role": "user", "content": query}],
                    step="perplexity", temperature=0.1,
                )
                raw_text = _clean_perplexity(resp)
            except Exception as e:
                err(f"[{ref}] Perplexity fehlgeschlagen: {e}")
                log.error(f"[{ref}] URL-los Perplexity: {e}")
                return False

    if not raw_text or len(raw_text) < 50:
        warn(f"[{ref}] URL-los, kein verwertbarer Roh-Text")
        log.warning(f"[{ref}] URL-los, kein Text verfügbar")
        return False

    extract_prompt = (
        f"Extrahiere aus dem folgenden Text alle Stelleninformationen als JSON.\n\n"
        f"Text:\n---\n{trunc(raw_text, 12000)}\n---\n\n"
        f"JSON-Schema:\n{_POSTING_SCHEMA}\n\n"
        f"Regeln:\n"
        f"- NUR tatsächlich im Text vorhandene Informationen\n"
        f"- Nichts erfinden oder halluzinieren\n"
        f"- Leere Strings für nicht ermittelbare Felder\n"
        f"- beschreibung: den vollständigen Stellentext übernehmen\n"
        f"- Gib NUR das JSON aus"
    )

    try:
        result = llm.chat(
            [{"role": "user", "content": extract_prompt}],
            step="profile", temperature=0.1,
        )
        data = extract_json(result)
    except Exception as e:
        err(f"[{ref}] JSON-Extraktion fehlgeschlagen: {e}")
        log.error(f"[{ref}] URL-los Extraktion: {e}")
        return False

    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict) and d.get("valid", True)), None)
    if not data or not data.get("valid", True):
        warn(f"[{ref}] Text enthält keine valide Stellenausschreibung")
        return False

    if not data.get("firma"):
        data["firma"] = row.get("Unternehmen", "")
    if not data.get("titel"):
        data["titel"] = row.get("Stelle", "")
    if not data.get("standort"):
        data["standort"] = row.get("Standort", "")

    commit_posting(ref, data, is_new_row=False, log=log, keine_url=True)
    return True


def process_partial_entry(llm: LLMClient, ref: str,
                          conf: dict, log: logging.Logger) -> bool:
    """Process a DB row with some data but LOK=FALSE."""
    row = get_row(DATENBANK_XLS, ref)
    if not row:
        log.debug(f"[{ref}] get_row=None, überspringe")
        return False

    has_url    = bool(str(row.get("URL", "")).strip())
    has_firma  = bool(str(row.get("Unternehmen", "")).strip())
    has_stelle = bool(str(row.get("Stelle", "")).strip())
    review     = str(row.get("Review", "")).strip()
    keine_url  = str(row.get("Keine_URL", "")).strip().upper() == "TRUE"

    firma_str  = str(row.get("Unternehmen", "")).strip()[:30]
    stelle_str = str(row.get("Stelle", "")).strip()[:40]
    label = f"{firma_str} — {stelle_str}" if firma_str else "?"
    print(f"\n  ═══ [{ref}] {label} ═══", flush=True)

    if review:
        info(f"[{ref}] Review vom Überwacher: {review[:80]}")
        log.info(f"[{ref}] Review: {review}")

    app_dir    = BEWERBUNGEN_DIR / ref
    ausschr_md = app_dir / "Ausschreibung.md"

    # URL-loser Eintrag → eigener Verarbeitungspfad
    if keine_url:
        info(f"[{ref}] Keine_URL=TRUE — URL-loser Modus")
        return _process_urlless(llm, ref, row, log)

    # Already complete and no review → just mark as done
    if (app_dir.exists() and ausschr_md.exists()
            and has_url and has_firma and has_stelle and not review):
        mark_lumpensammler_done(DATENBANK_XLS, ref,
                                ausschreibung_pfad=f"Bewerbungen/{ref}/Ausschreibung.md")
        ok(f"[{ref}] Dateien vorhanden — LOK=TRUE")
        log.info(f"[{ref}] Bereits komplett, markiert")
        return True

    # Review vorhanden → gezielt nachrecherchieren
    if review and has_firma and has_stelle:
        supplement = _review_search(llm, ref, row, review, log)
        if supplement and isinstance(supplement, dict):
            data = _load_existing_data(ref, row)
            _merge_supplement(data, supplement)
            commit_posting(ref, data, is_new_row=False, log=log)
            ok(f"[{ref}] Review abgearbeitet, Daten ergänzt")
            log.info(f"[{ref}] Review erfolgreich verarbeitet")
            return True
        else:
            warn(f"[{ref}] Review-Nachrecherche ergab keine neuen Daten")
            log.warning(f"[{ref}] Review-Suche ohne Ergebnis")

    # Has URL → read it directly
    url = str(row.get("URL", "")).strip()
    if url:
        info(f"[{ref}] Lese URL: {url[:70]}")
        data = read_job_posting(llm, url, log)
    else:
        # No URL → search for the specific company/position
        query = (
            f"Stellenanzeige: {row.get('Stelle', '')} bei {row.get('Unternehmen', '')} "
            f"Standort {row.get('Standort') or 'Deutschland'}. "
            "Finde die genaue Stellenausschreibung mit Aufgaben, Anforderungen, "
            "Kontaktdaten, Bewerbungslink, Datum der Ausschreibung und Bewerbungsfrist."
        )
        info(f"[{ref}] Suche via Perplexity …")
        log.info(f"[{ref}] Perplexity-Suche: {query}")

        try:
            raw = llm.chat(
                [{"role": "user", "content": query}],
                step="perplexity",
                temperature=0.1,
            )
            raw = _clean_perplexity(raw)
        except Exception as e:
            err(f"[{ref}] Perplexity-Fehler: {e}")
            log.error(f"[{ref}] Perplexity fehlgeschlagen: {e}")
            return False

        # Try to find a URL in the response and read it
        blacklist = conf.get("suchkriterien", {}).get("blacklist_domains", [])
        found_urls = extract_urls(raw, log, blacklist_domains=blacklist)
        data = None
        for candidate_url in found_urls[:3]:
            data = read_job_posting(llm, candidate_url, log)
            if data and validate_posting(data):
                break
            data = None

        if not data:
            try:
                parsed = extract_json(raw)
                if isinstance(parsed, list):
                    data = next((d for d in parsed if isinstance(d, dict) and validate_posting(d)), None)
                else:
                    data = parsed
            except ValueError:
                pass

    if not data:
        warn(f"[{ref}] Keine verwertbaren Daten gefunden")
        log.warning(f"[{ref}] Keine Daten extrahiert")
        return False

    # Merge with existing row data (don't overwrite known fields)
    if not data.get("firma") and has_firma:
        data["firma"] = row.get("Unternehmen", "")
    if not data.get("titel") and has_stelle:
        data["titel"] = row.get("Stelle", "")
    if not data.get("standort") and row.get("Standort"):
        data["standort"] = row.get("Standort", "")
    if not data.get("url") and has_url:
        data["url"] = row.get("URL", "")

    # Commit (row already exists, not new)
    commit_posting(ref, data, is_new_row=False, log=log)
    return True


def _load_existing_data(ref: str, row: dict) -> dict:
    """Lade bestehende Daten aus Ausschreibung.md und DB-Zeile zusammen."""
    data = {
        "firma": row.get("Unternehmen", ""),
        "titel": row.get("Stelle", ""),
        "standort": row.get("Standort", ""),
        "url": row.get("URL", ""),
        "kontakt_name": row.get("Kontakt", ""),
        "kontakt_email": row.get("Kontakt_Email", ""),
        "kontakt_telefon": row.get("Kontakt_Telefon", ""),
        "firma_adresse": row.get("Kontakt_Post", ""),
        "datum_ausschreibung": row.get("Datum_Ausschreibung", ""),
        "bewerbungsfrist": row.get("Bewerbungsfrist", ""),
    }

    ausschr_path = BEWERBUNGEN_DIR / ref / "Ausschreibung.md"
    if ausschr_path.exists():
        md = ausschr_path.read_text(encoding="utf-8")
        if "## Aufgaben" in md:
            data.setdefault("beschreibung", md)

    return data


def _merge_supplement(base: dict, supplement: dict):
    """Ergänze base mit nicht-leeren Feldern aus supplement (überschreibt keine vorhandenen)."""
    for key, val in supplement.items():
        if not val or key == "valid":
            continue
        existing = str(base.get(key, "")).strip()
        if not existing:
            base[key] = val


# ═══════════════════════════════════════════════════════════════════════════════
#  Init-Modus: einmalige Suche
# ═══════════════════════════════════════════════════════════════════════════════

def run_init(llm: LLMClient, conf: dict, sys_prompt: str,
             seed: str | None, log: logging.Logger, auto: bool = False):
    """One-shot search: find postings and create entries."""
    total = 0

    while True:
        try:
            query  = ensure_query(llm, sys_prompt, seed, conf, log)
            raw    = perplexity_search(llm, query, log)
            filled = fill_from_search(llm, raw, [], conf, log)
            total += len(filled)

            console.print()
            if filled:
                ok(f"Dieser Lauf: {len(filled)} Stelle(n) angelegt (Gesamt: {total})")
            else:
                info("Keine neuen Stellen gefunden")

            if auto:
                break

            console.print()
            from rich.prompt import Confirm
            if not Confirm.ask("Nochmal suchen?", default=False):
                break

            from rich.prompt import Prompt
            new_seed = Prompt.ask("Neuer Suchfokus (Enter = gleicher)", default=seed or "")
            if new_seed.strip():
                seed = new_seed.strip()
                # Invalidate cache for new seed
                if QUERY_CACHE.exists():
                    QUERY_CACHE.unlink()

        except KeyboardInterrupt:
            console.print("\n[yellow]Suche unterbrochen.[/yellow]")
            break
        except Exception as e:
            err(f"Fehler: {e}")
            log.error(f"Init-Fehler: {e}", exc_info=True)
            break

    console.print()
    console.print(Panel.fit(
        f"[bold green]✓  Suche abgeschlossen![/bold green]\n\n"
        f"[dim]Neue Stellen:[/dim]  {total}\n"
        f"[dim]Bewerbungen:[/dim]  {BEWERBUNGEN_DIR}",
        border_style="green",
        padding=(1, 2),
    ))
    log.info(f"Init fertig: {total} Stellen angelegt")


# ═══════════════════════════════════════════════════════════════════════════════
#  Poll-Modus: Datenbank kontinuierlich überwachen
# ═══════════════════════════════════════════════════════════════════════════════

def poll_loop(llm: LLMClient, sys_prompt: str, conf: dict,
              log: logging.Logger):
    global _search_cycle
    interval = conf.get("search", {}).get("poll_interval_sec", 120)

    console.print()
    console.print(Panel(
        f"[bold cyan]Polling-Modus aktiv[/bold cyan]\n"
        f"[dim]Prüfe Google Sheet alle {interval}s auf LOK=FALSE …\n"
        f"Abbruch mit [bold]Ctrl+C[/bold][/dim]",
        border_style="cyan",
        padding=(1, 2),
    ))
    log.info(f"Poll-Modus gestartet (Intervall: {interval}s)")

    while True:
        try:
            pending = get_unprocessed(DATENBANK_XLS)
            for r in pending:
                reserve_ref(r)

            if not pending:
                new_slots = []
                for _ in range(5):
                    ref = add_entry(DATENBANK_XLS)
                    new_slots.append(ref)
                info(f"Keine LOK=FALSE — {len(new_slots)} neue Slots angelegt ({new_slots[0]}–{new_slots[-1]})")
                log.info(f"Neue Slots angelegt: {new_slots}")
                pending = new_slots

            # Classify: empty slots vs partial entries
            empty_refs   = []
            partial_refs = []
            for ref in pending:
                row = get_row(DATENBANK_XLS, ref)
                if not row:
                    continue
                has_stelle = bool(str(row.get("Stelle", "")).strip())
                has_firma  = bool(str(row.get("Unternehmen", "")).strip())
                if not has_stelle and not has_firma:
                    empty_refs.append(ref)
                else:
                    partial_refs.append(ref)

            info(f"LOK=FALSE: {len(empty_refs)} leere Slots, {len(partial_refs)} Teildaten")
            log.info(f"Poll: {len(empty_refs)} leer, {len(partial_refs)} partial")

            # Empty slots: batch-fill with LLM-generated varied query
            if empty_refs:
                try:
                    all_rows = get_all_rows(DATENBANK_XLS)
                    known_firms = list({
                        str(r.get("Unternehmen", "")).strip()
                        for r in all_rows
                        if str(r.get("Unternehmen", "")).strip()
                    })
                    query = generate_query(
                        llm, sys_prompt, known_firms,
                        _search_cycle, None, conf, log,
                    )
                    _search_cycle += 1
                    raw = perplexity_search(llm, query, log)
                    filled = fill_from_search(llm, raw, empty_refs, conf, log)
                    if filled:
                        ok(f"{len(filled)} neue Stelle(n) eingetragen")
                    time.sleep(5)
                except Exception as e:
                    err(f"Slot-Füllung fehlgeschlagen: {e}")
                    log.error(f"fill_from_search Fehler: {e}", exc_info=True)

            # Partial entries: process individually
            for ref in partial_refs[:10]:
                try:
                    process_partial_entry(llm, ref, conf, log)
                except Exception as e:
                    err(f"[{ref}] Fehler: {e}")
                    log.error(f"[{ref}] Partial-Fehler: {e}", exc_info=True)
                time.sleep(3)

            if not empty_refs and not partial_refs:
                console.print(f"\n  [dim]Alles bearbeitet — warte {interval}s …[/dim]")
                time.sleep(interval)

        except KeyboardInterrupt:
            console.print("\n[yellow]Polling gestoppt.[/yellow]")
            log.info("Poll gestoppt (KeyboardInterrupt)")
            break


# ─────────────────────────────── Einstiegspunkt ───────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Lumpensammler — Stellenrecherche-Agent v2",
    )
    parser.add_argument("--seed", default=None,
                        help='Suchfokus, z.B. --seed "Embedded-Stellen in Kassel"')
    parser.add_argument("--auto", action="store_true",
                        help="Einen Lauf durchführen und beenden (für Hintergrundbetrieb)")
    args = parser.parse_args()

    banner()

    conf       = load_conf()
    log        = setup_logging(conf)
    sys_prompt = build_system_prompt()

    log.info(f"Lumpensammler v2 gestartet. Seed: {args.seed}")

    if not OWLTRAIL_CONF.exists():
        err(f"owltrail.conf nicht gefunden: {OWLTRAIL_CONF}")
        sys.exit(1)

    info("Verbinde mit QuiteQue …")
    try:
        llm = LLMClient(OWLTRAIL_CONF, conf)
        ok("LLM-Verbindung hergestellt")
        log.info("LLM OK")
    except Exception as e:
        err(f"LLM-Verbindung fehlgeschlagen: {e}")
        log.error(f"LLM init: {e}")
        sys.exit(1)

    BEWERBUNGEN_DIR.mkdir(exist_ok=True)

    if args.seed:
        run_init(llm, conf, sys_prompt, args.seed, log, auto=args.auto)
    poll_loop(llm, sys_prompt, conf, log)


if __name__ == "__main__":
    main()
