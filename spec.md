# spec.md — Geplante Features für Strix

> Diese Datei lebt im Arbeitsverzeichnis und ist die **Source of Truth** für die nächste Iteration.
> Der Agent (35B/27B/Claude/DeepSeek) liest sie zu Session-Beginn, der User editiert sie zwischen den Sessions.

## Aktuell offen

- [ ] Sudo-Preauth-Toggle in propeller.conf statt owltrail.conf (sinnvoller Platz)
- [ ] Recurring-Task-Skill: "alle X Minuten Y prüfen" als nativer Propeller-Befehl
- [ ] Working-Dir-Pfad im Header wird hin und wieder zu lang → smartere Kürzung (`.../dir`)
- [ ] Owltrail-Adapter als systemd user-Service im .deb-postinst statt Wrapper-Spawn
- [ ] Optional: Header-Chip rechts mit `[CLAUDE✓ spec✓4 tageb 2026-05-17]` — derzeit nur per `/context`

## Ideen für später

- [ ] `/loop <intervall> <task>` Slash-Command der intern auf automation_manager.rs aufbaut
- [ ] Persistente Plan-Liste in `~/.deepseek/plans/<workspace-hash>.md` damit Plans Sessions überleben
- [ ] Switch-Hotkey zum Wechseln zwischen Stuck und Dev-Build aus dem laufenden TUI heraus
- [ ] Status-Banner mit aktuell aktivem .deb (installierte vs. lokale Version)

## Fertig

- [x] Splash-Logo theme-aware
- [x] Version-Display im Splash + Header
- [x] Quote-Refusal-Detection v2 (KI-Assistent / "Anfrage erfüllen" / "nicht in der Lage" / …)
- [x] Splash-Comment-Phase Soft-Fail mit 8s Anzeige
- [x] `reasoning_effort: "none"` als JSON-Feld statt `/no_think`-Prefix
- [x] Cold-Start-Timeouts (240s/300s)
- [x] Synchronized-Output-Mode (DECSET 2026)
- [x] Default-Mode Agent, Default-Model deepseek-v4-pro
- [x] Voller Workspace-Pfad im Header (~/Schreibtisch/… statt nur Foldername)
- [x] Sudo-Preauth beim Stuck-Start (konfigurierbar via owltrail.conf `_propeller.sudo_preauth`)
- [x] `start.sh`, `make.sh`, `claude.sh`, `CLAUDE.md` direkt in Strix/
- [x] Preset-Liste neu sortiert (DeepSeek vorn, Local-Qwen hinten)
- [x] `bash claude.sh` Banner zeigt nächsten spec-TODO + letzten Tagebuch-Eintrag
- [x] CWD im Header bleibt sichtbar auch wenn sub_model gesetzt ist (vorher hidden hinter M/S-Pair)
- [x] Slash-Commands `/spec` (Alias `/plan`), `/tagebuch [all]`, `/claude`, `/context` für Vibecoding-File-Anzeige
- [x] `propeller.conf` Key `tagebuch_entries` (Default 3) konfiguriert wie viele Einträge `/tagebuch` ohne `all` zeigt
- [x] Info-Panel rechts (Workspace + Projektname + CLAUDE/spec/tagebuch-Status mit Command-Hints)
- [x] Tall-Sidebar-Layout: Sidebar erstreckt sich über volle Höhe (auch unter dem Composer); Composer/Preview sind links gekürzt damit rechts mehr Platz für Info/Plan/Tasks bleibt

## Konventionen

- Jeder Feature-Punkt der durch ist wandert nach **Fertig** (statt gelöscht zu werden — Audit-Trail)
- `tagebuch.md` enthält *was passiert ist*, `spec.md` enthält *was geplant ist*
- Beim Commit: kurze Referenz auf den `spec.md`-Punkt in der Commit-Message
