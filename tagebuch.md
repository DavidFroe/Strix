# tagebuch.md — Strix Session-Log

> Was wirklich passiert ist, in umgekehrter chronologischer Reihenfolge (jüngstes oben).
> Für Pläne / TODOs siehe `spec.md`.

## 2026-05-22 — make.sh Fix: Toolchain + 0-Byte-Binary

- **Ursache**: Rust-Toolchain war nach rsync korrupt (shared libraries "file too short").
  `rustup toolchain list` → "no installed toolchains", `rustc --version` → linker-Fehler.
- **Fix**: Toolchain neu installiert (`rustup toolchain remove stable && install stable`).
  Allerdings: `serde_derive` 1.0.228 braucht Rust ≥ 1.96 — stable 1.95.0 zu alt.
  → Default auf `nightly` (1.97.0, 2026-05-21) gesetzt.
- **make.sh gehärtet**: `[ ! -f "$BINARY" ]` → `[ ! -s "$BINARY" ]` damit 0-Byte-Dateien
  erkannt werden statt ein leeres Paket zu produzieren (bisher: 29 KB .deb mit 0-Byte-Binary).
- `bash make.sh` → `strix_0.1.0_amd64.deb` = 13 MB (Binary: 50 MB, static-pie musl).
- **Wrapper-Robustness**: `_ensure_owltrail()` zeigt jetzt bei Fehlern den Log-Tail an
  (statt nur "Adapter nicht startbar"). `Depends: python3` statt `Recommends` im control.
- **Binary-Fehlermeldung**: Liest Log-Pfad aus `OWLTRAIL_LOG`-Env-Var (wie vom Wrapper gesetzt),
  Fallback `/tmp/strix-owltrail.log`. Vorher hartkodiert `/tmp/owltrail-adapter.log` (falsch).
- **Wrapper `_adapter_ok()`**: HTTP-200-Check auf `/v1/models` statt blindem TCP-Connect.
  `touch $LOG` vor Adapter-Start → Log existiert auch bei sofortigem Crash.
  Timeout 8s (40×0.2s), bei Fehler wird Log-Tail angezeigt.
- **Adapter**: `setup_logging` VOR `load_conf` → Config-Fehler werden geloggt statt zu verschwinden.
  Fataler Fehler bei ungültiger owltrail.conf schreibt auf stderr + return 1.
- `musl-gcc` war schon installiert, `x86_64-unknown-linux-musl` Target für nightly installiert.

## 2026-05-18 — Info-Panel + Tall-Sidebar-Layout

- `crates/tui/src/tui/sidebar.rs`: neues `render_sidebar_info` Panel. Zeigt Workspace-Pfad (Home → `~`), Projektname (H1 aus CLAUDE.md/spec.md/README, sonst Folder-Name, sonst "unnamed") und 3 File-Status (CLAUDE.md/spec.md/tagebuch.md mit ✓/– und Command-Hint dahinter). Hardcoded auf 9 Zeilen Höhe damit's nicht ausufert.
- `render_sidebar_auto`: Panel::Info am Anfang der visible-Liste eingefügt; constraints-Berechnung umgebaut auf Info=Length(9) + Plan und übrige = anteiliger Rest.
- `crates/tui/src/tui/ui.rs`: **Tall-Sidebar-Layout**. Sidebar bekommt jetzt eine Rect über die volle Body+Preview+Composer-Höhe, statt nur Body-Höhe. Composer und Preview werden in der Breite gekürzt damit kein Overlap. file-tree-Pane bleibt im chat_outer (linker Spalte). Resize-Edge-Cases sollten weiter funktionieren (Min-Width-Checks unverändert).
- Adapter-Stale-Bug (siehe vorigen Tagebuch-Eintrag) fix: root `owltrail.conf` mit Stuck/Dev synchronisiert (IDs 251/250/241/239), alter Adapter (PID 123405, vom 16. Mai) gekillt; nächster start.sh spawnt frischen Adapter mit korrekter Conf.
- Native-Binary nach `metadata_spans`-CWD-Fix nachgebaut (war im musl-Build aber nicht in target/release nach dem Vorher-Commit).

## 2026-05-17 — Vibecoding-Slash-Commands + Header-CWD-Fix

- `crates/tui/src/commands/context_files.rs` neu: `/spec` `/plan` `/tagebuch [all]` `/claude` `/context`. Read-only ins Scrollback, kein Schreiben. Datei-Lookup nur in `app.workspace/<name>.md`, kein Tree-Walk.
- `commands/mod.rs`: 3 neue CommandInfo + Dispatch-Arms. Description teilt sich `CmdContextDescription` — minimal-invasiv, keine neuen MessageIds.
- `commands/mod.rs`: `/context` ist jetzt die Vibecoding-Übersicht (mit Existenz-Status + offenen Spec-TODOs + letztem Tagebuch-Datum). Alter Context-Window-Befehl bleibt unter `/ctx` erreichbar.
- `tui/app.rs`: `read_propeller_conf_tagebuch_entries()` (Default 3, gelesen aus `propeller.conf.tagebuch_entries`).
- `propeller.conf`: Key `tagebuch_entries: 3` ergänzt.
- `widgets/header.rs metadata_spans`: CWD-Bug gefixt — bei gesetztem `sub_model` wird jetzt `<workspace> · M:<main> S:<sub>` gerendert statt nur das Modell-Paar. Falls zu lang → Fallback aufs Paar (alte Verhalten).
- `CLAUDE.md`: Key-Files-Tabelle + Slash-Commands-Zeile erweitert.

## 2026-05-17 — Initial Stuck-Split

- Worktree `Strix` aus `checkpoint/1.0.6-pre-workflow` gebaut (commit `8f005031`)
- Defaults auf Agent + DeepSeek-Pro/Flash gesetzt → Commit `91b645e6`
- `start.sh`, `make.sh`, `claude.sh`, `CLAUDE.md` als self-contained-Scaffolding hinzugefügt → `35507a2a`
- Splash-Refusal-Härtung + Soft-Fail + reasoning_effort-Fix aus dev cherry-picked → `666b08c6`
- Preset-Liste reordert (DeepSeek vorn, Grok dazu, Local-Qwen ans Ende), `propeller_agent_system.md` mit "Handle, rede nicht"-Direktive verstärkt → `1b03be69`
- Voller Workspace-Pfad im Header (Home → `~`), Sudo-Preauth in `start.sh` portiert, `spec.md` + `tagebuch.md` angelegt

## Konvention

- Datums-Header pro Tag (`## YYYY-MM-DD — kurzer Titel`)
- Bullet-Liste: Was gemacht + ggf. Commit-Hash
- Inhaltliche Begründung gehört in die Commit-Message, nicht hierher
- Sessions wo nichts committet wurde → trotzdem ein Eintrag wenn Erkenntnisse gewonnen wurden ("Debug: 35B antwortet immer mit Refusal wenn …")
