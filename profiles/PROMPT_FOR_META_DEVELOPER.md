# Brief für den Agentensystem-Entwickler

> Paste dies in den Onboarding-Prompt / die Architektur-Doku deines
> Meta-Agenten (der die Agentensysteme designt/baut). Er weiß damit, dass
> Strix jetzt zwei eingebaute Profile mitbringt, die zum Bau einzelner
> Agenten innerhalb eines Agentensystems gedacht sind.

---

## Neue Komponente: `strix-server` mit Entwickler-Profilen

Auf Hosts mit installiertem `strix` (Debian-Paket, ab Version 0.1.0)
stehen zwei eingebaute Profile für die **Entwicklung einzelner Agenten
innerhalb eines Agentensystems** zur Verfügung. `strix-server` startet
einen lokalen Web-UI-Server, der diese Profile kennt:

```bash
# Worker-Agent (z.B. einen "Sammler", "Reviewer", "Mailer") entwickeln
cd /pfad/zum/agent
strix-server --profile webUIAgent

# Master/Orchestrator (Excel-Tabelle abarbeiten, Worker dispatchen) entwickeln
cd /pfad/zum/master
strix-server --profile webUIMaster
```

Default-Host ist `0.0.0.0` → der Server ist sofort über VPN/LAN
erreichbar. Banner druckt alle URLs beim Start.

### Was diese Profile dem Strix-Agenten mitgeben

Beide Profile laden `/etc/strix/profiles/<name>.md` als
**system-instructions**. Inhalt:

| Aspekt          | webUIAgent                                    | webUIMaster                            |
|----------------|-----------------------------------------------|----------------------------------------|
| Rolle          | Entwickler eines **Worker-Agenten**          | Entwickler des **Orchestrators**       |
| Skelett-Vorlage| `Agent.md` + `Behavior.md` + `Sole.md` + `<name>.py` + `<name>.conf` + `tools/llm_client.py` + `tools/datenbank.py` + `logs/` | `Master.md` + `Behavior.md` + `Sole.md` + `<master>.py` + `<master>.conf` + `tools/` + `logs/` |
| Pattern        | Subscribes to Tabellen-Zeilen mit `<name>_OK=False`, arbeitet, setzt `<name>_OK=True` | Polling-Schleife, prüft `Master_OK=True`, dispatched an Worker via `<Agent>_OK=False`, idempotent |
| owAPI-Wissen   | ja — Endpunkt, Actions, Auto-Reset-Konvention | ja — selbe Doku, aber Fokus auf list+update statt write |
| owltrail-Wissen| ja — Modell-IDs (20 Haiku, 115 Perplexity, 120 PropellerA, 251 DSv4-Pro …) | ja — aber Master soll Modelle eher sparsam nutzen |
| Default-Modell | DeepSeek v4 Pro (251) — Code mit Reasoning   | DeepSeek v4 Pro (251)                  |
| Shell+yolo     | ja (auto-approve), kein Klick-Dialog im Browser | ja                                     |

### Was Strix in diesem Profil tut

- Bei **leerem cwd**: schlägt das Skelett vor (analog Lumpensammler-Beispiel)
- Bei **gefülltem cwd**: liest erst `Agent.md`/`Master.md`, danach Code
- Kennt die Modell-IDs **numerisch** (keine selbsterfundenen Aliase)
- Logging im Lumpensammler-Stil (`logs/<name>_YYYY-MM-DD.log`)
- Verwendet `chat_template_kwargs={"enable_thinking": false}` bei PropellerA
  für Tool-Use
- Timeouts ≥ 120s in generiertem Code (Cold-Starts)

### Was Strix in diesem Profil NICHT macht

- Backend-Ports direkt anrufen (11434 Ollama, 8210 PropellerA)
- Eigene Modell-Aliase erfinden
- Master selbst Worker-Arbeit erledigen lassen (Anti-Pattern)
- Race-Conditions zwischen mehreren Mastern erlauben

### Persistenz im Workdir

Während der Entwicklung schreibt der Strix-Server in das Workdir:
- `verlauf.db` — SQLite mit kompletter Conversation (Threads/Turns/Items/Events)
- `verlauf.md` — menschenlesbarer Markdown-Mirror

Auswertung CLI:
```bash
strix verlauf list                     # Threads dieses Workdirs
strix verlauf export --pretty out.json # voller Dump für Archiv
```

### Vom Meta-System aus aufrufen

Wenn dein Meta-Agent einen neuen Agenten anlegen will:

```bash
# 1. Workdir vorbereiten
mkdir -p /pfad/zu/neuem-agent
cd /pfad/zu/neuem-agent

# 2. Strix-Server hochfahren (Profile wählt Doku + Defaults)
strix-server --profile webUIAgent --port 7878 &

# 3. URL ausgeben (steht im Banner) → an User schicken
#    → User entwickelt im Browser am Agenten
#    → wenn fertig: pkill -9 -f "strix-tui.*7878"
```

Für Multi-Agent-Bauten kann der Meta-Agent mehrere strix-server parallel
starten — der Port walkt automatisch hoch (7878 → 7879 → 7880 …), Workdirs
bleiben isoliert.

### Verfügbare Modelle (über owltrail)

| ID   | Modell           | Eignung                              |
|-----:|------------------|--------------------------------------|
| 20   | Claude Haiku     | Schnell+günstig, Profile/Queries     |
| 21   | Claude Sonnet    | Allround                             |
| 22   | Claude Opus      | Teuer, max Qualität                  |
| 120  | PropellerA Qwen3.6-27B | Lokal, Vision+Tools+Thinking, 97K |
| 115  | Perplexity (PMC) | **Web-Suche** (60–180s!)              |
| 250  | DeepSeek v4 Flash| Günstig                              |
| 251  | DeepSeek v4 Pro  | +Thinking                             |
| 500  | Gemini 3 Flash   | Vision/Video                          |
| 239–242 | Ollama Qwen 35B MoE | Lokal                            |
| 246  | Ollama llama3.1 8B | Klein+lokal                         |
| "free" | Auto-Pool       | Wählt billigstes verfügbar           |

Live: `GET http://localhost:8082/v1/models` (owltrail-Proxy).

### Spezialfälle

- **PropellerA (120) + Tools**: `chat_template_kwargs={"enable_thinking": false}` setzen, sonst frisst Thinking das Token-Budget
- **Ollama (200er)**: Cold-Start 20–90s → Timeout ≥ 120s
- **Perplexity (115)**: 60–180s normal, Heartbeat-SSE
- **Free-Pool**: Waterfall bis 5 Versuche

### Zusammenfassung für deinen Meta-Agenten

Wenn du einen Agentensystem-Agenten ("Lumpensammler", "Maintainer",
"Anschreiber", "Master" usw.) entwickeln lassen willst:

1. Lege das Workdir an (leer oder mit existierendem Code).
2. Starte `strix-server --profile webUIAgent` (Worker) oder
   `--profile webUIMaster` (Orchestrator).
3. Öffne die URL aus dem Banner im Browser.
4. Strix kennt durch das Profil bereits: owAPI-Schnittstelle,
   owltrail-Modelle, Skelett-Konvention, Verhalten.
5. Konversation wird in `verlauf.db` + `verlauf.md` mitgeschrieben.

Du kannst beide Profile in `/etc/strix/profiles/{webUIAgent,webUIMaster}.md`
nachlesen / editieren (Änderungen wirken sofort, kein Rebuild nötig).
