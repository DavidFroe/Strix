# Plan: make.sh Fix — .deb-Build reparieren

## Ziel
make.sh erstellt wieder ein funktionierendes .deb-Paket (≥ 40 MB) das auf anderen Systemen installierbar ist.

## Schritte
1. ✅ rustup default toolchain setzen (nightly, da serde_derive 1.0.228 nightly braucht)
2. ✅ musl-Target installieren
3. ✅ make.sh fixen: 0-Byte-Binaries erkennen mit `[ -s ]` statt `[ -f ]`
4. ✅ Sauberen musl-Build durchführen (3m 05s, 50 MB Binary)
5. ✅ .deb gebaut: 13 MB, Binary 50 MB statisch gelinkt

## Risiken
- Toolchain war korrupt (rsync-Übernahme) → fixed durch Neuinstallation
- Cargo.lock könnte veraltete/inkompatible Versionen enthalten → nightly fixt das temporär

## Erfolgskriterium
`strix_0.1.0_amd64.deb` ≥ 40 MB, installierbar via `sudo apt install`, Strix startet.
