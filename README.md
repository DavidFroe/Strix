# Strix 🦉

> Terminal-UI für OpenAI-kompatible LLM-Endpoints. Multi-Provider, Vibecoding-tauglich, mit owltrail-Adapter für nahtloses Modell-Routing.

Strix ist ein eigenständiger Terminal-Agent: chatten, Tools nutzen, Plans pflegen, alles in der Konsole. Funktioniert gegen jede OpenAI-kompatible API — DeepSeek, Anthropic Claude, OpenAI, xAI Grok, Google Gemini, Mistral, lokale Ollama-Modelle, vLLM/SGLang/LM Studio …

## Features

- **Multi-Provider** — `/model claude opus` · `/model deepseek-v4-pro` · `/model gemini-2.5-flash` · `/model ollama-qwen3.6-27b` … alles über einen OpenAI-kompatiblen Endpoint
- **Modes** — Tab schaltet zwischen `Agent`, `Yolo`, `Plan`, `Übermoodus` (auto-approve + max reasoning)
- **Vibecoding-Workflow** — Slash-Commands `/spec` `/tagebuch` `/claude` `/context` zur Anzeige von Workspace-MDs direkt in der TUI
- **Tall-Sidebar** — Info / Plan / Tasks / Todos / Agents permanent rechts sichtbar, mit Workspace-Path und File-Status-Indikatoren
- **Live-Streaming** — Reasoning- und Content-Blöcke in Echtzeit; pro-Turn-Model-Routing
- **Owl-Splash** — pixelart-Eule beim Start, Modell-Test-Phase mit Live-Timer

## Quick-Start

```bash
# 1. Build
cargo build --release

# 2. Config — owltrail.conf.example als Vorlage:
cp owltrail.conf.example owltrail.conf
$EDITOR owltrail.conf        # Token + Modell-IDs eintragen

# 3. Run
bash start.sh                # spawnt owltrail-Adapter (Port 8081) + TUI
```

Alternativ direkt gegen einen OpenAI-kompatiblen Endpoint ohne owltrail:

```bash
export DEEPSEEK_API_KEY="sk-…"
export DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"
./target/release/strix
```

## Konfiguration

| Datei | Zweck |
|---|---|
| `strix.conf` | Default-Mode, Default-Modell, Theme, `tagebuch_entries`, Presets |
| `owltrail.conf` | Backend-Routing (Token, Modell-IDs) — **NICHT in git committen** |
| `strix_agent_system.md` | System-Prompt für den Übermoodus |
| `strix_start_prompt.md` | Splash-Zitat-Prompt-Template |
| `strix_quote_adjectives.csv` | Adjektive-Pool für Splash-Zitat-Variation |
| `strix_prompts.csv` | Composer-Placeholder-Pool |

## Slash-Commands

```
/model [name]            Modell wechseln
/models                  alle verfügbaren Modelle listen
/spec  | /plan           spec.md (Vibecoding-Plan) anzeigen
/tagebuch [all]          tagebuch.md (Session-Log) anzeigen
/claude                  CLAUDE.md (Dev-Kontext) anzeigen
/context                 Übersicht: Workspace + welche MDs vorhanden + Commands
/ctx                     Context-Window-Stats (Tokens / Cache)
/help [cmd]              alle Commands oder Detail
```

## Build .deb (Debian/Ubuntu)

```bash
bash make.sh                          # → strix_<VERSION>_amd64.deb
sudo apt install ./strix_*.deb        # installiert /usr/bin/strix
```

## Dev-Toolchain (im Strix-Verzeichnis)

| Script | Zweck |
|---|---|
| `start.sh` | Strix lokal starten (spawnt owltrail-Adapter + TUI) |
| `make.sh` | Standard-Debian-Paket bauen (`strix_<v>_amd64.deb`) |
| `claude.sh` | Claude-Code-Session in diesem Verzeichnis öffnen mit `CLAUDE.md`-Kontext |
| `git_up.sh` | `git add -A && git commit && git push` mit Sicherheits-Check gegen Secret-Leaks |
| `transfer.sh` | Beide Pakete bauen: Standard + Personal-Profil (`strix-transfer_<v>_amd64.deb`) für Transfer auf neues Debian-System |
| `spec.md` | Vibecoding-TODO-Liste (in der TUI: `/spec`) |
| `tagebuch.md` | Session-Log (in der TUI: `/tagebuch`) |
| `CLAUDE.md` | Dev-Kontext für eine frische Claude-Session |

## Lizenz

MIT. Forked aus [deepseek-tui](https://github.com/Hmbown/DeepSeek-TUI).

## Status

**1.0.0** — Initial Public Release. Siehe [`spec.md`](spec.md) für TODOs und [`tagebuch.md`](tagebuch.md) für Änderungs-Log.
