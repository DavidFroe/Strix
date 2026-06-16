# Strix Dev-Umgebung auf 11.0.0.12

Dieser Ordner enthält den **kompletten Strix-Source-Tree** + alle Dev-Tools.
Hier kannst du Code ändern, lokal bauen und sofort testen — parallel zum
`apt`-installierten Strix das systemweit unter `/usr/bin/strix` läuft.

## Erstes Mal: Rust installieren

```bash
curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source $HOME/.cargo/env
rustup target add x86_64-unknown-linux-musl   # nur wenn du .deb bauen willst
```

Dauert ~3 Min. Brauchst du nur einmal.

## Starten

```bash
cd ~/strix-dev
bash start.sh       # baut + startet das lokale Binary (target/release/strix)
```

Erster Start dauert ~5 Min (cargo Release-Build). Danach inkrementell schnell.

## Was wo liegt

| Pfad                          | Zweck                                            |
|-------------------------------|--------------------------------------------------|
| `crates/tui/src/`             | Rust-Source — hier passieren Code-Änderungen     |
| `Cargo.toml` + `crates/*/`    | Workspace-Manifest + alle Sub-Crates             |
| `start.sh`                    | Adapter + Lokal-Binary starten                   |
| `make.sh`                     | Public-.deb bauen (`strix_0.1.0_amd64.deb`)      |
| `claude.sh`                   | Claude-Code-Session in diesem Dir mit handoff    |
| `owltrail.conf`               | Token + Modell-Mapping (lokale Kopie)            |
| `owltrail_adapter.py`         | OpenAI-kompatibler Adapter (Port 8081)           |
| `strix.conf`                  | Presets + Default-Modell                         |
| `strix_prompts.csv`           | Composer-Placeholder-Pool                        |
| `strix_tooltips.csv`          | Rotierende Tooltips                              |
| `.deepseek/handoff.md`        | Session-Übergabe — zuerst lesen!                 |

## Dev vs. installiertes Strix

| Wo gestartet                  | Was läuft                                        |
|-------------------------------|--------------------------------------------------|
| `cd ~/strix-dev && bash start.sh` | Lokal-gebautes Binary aus `target/release/strix` |
| `strix` (irgendwo)            | System-Strix aus `/usr/bin/strix` (apt-installiert) |

## Workflow

1. Code ändern in `crates/tui/src/...`
2. `bash start.sh` (rebaut wenn nötig, dann TUI)
3. Wenn zufrieden: `bash make.sh` → neues `strix_*.deb`
4. `sudo dpkg -i strix_*.deb` → System-Strix updaten
5. Optional: `bash git_up.sh "kommit-message"` → commit + push zu GitHub

## Git

```bash
git status               # owltrail.conf, *.deb, target/ sind gitignored
git log --oneline | head # 9 commits aktuell
git remote -v            # → git@github.com:DavidFroe/Strix.git
```

## Probleme?

- Build-Fehler → `cargo clean && cargo build --release --bin strix`
- Adapter startet nicht → `tail -f /tmp/strix-owltrail.log`
- Flackern → andere Terminal-Emulator probieren (kitty empfohlen)
