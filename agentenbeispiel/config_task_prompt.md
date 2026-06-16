# Aufgabe: Suchkriterien-Konfiguration für den Lumpensammler

## Kontext
Das Webpanel (main.py, Port 8000) bekommt ein Suchkriterien-Panel. Der Nutzer soll Standort, Radius, Stellenlevel und Gehaltsrahmen steuern können. Diese Werte fließen in den Perplexity-Suchquery ein.

## Neue Einstellungen in `lumpensammler.conf`

```json
"suchkriterien": {
  "standort_modus": "orte",
  "orte": [
    {"name": "Kassel", "radius_km": 50},
    {"name": "Göttingen", "radius_km": 30}
  ],
  "land": "",
  "verbund": "",
  "level": {
    "kompetenz": 0.6,
    "gehalt_von": 4000,
    "gehalt_bis": 7000,
    "anstellung": "festanstellung"
  },
  "blacklist_domains": [
    "freelancermap.de",
    "green-energy-jobs.net"
  ]
}
```

### Standort-Modus (Radio-Buttons im Webpanel)
Vier Modi, gegenseitig exklusiv:

1. **`"orte"`** — 1 bis 3 konkrete Orte mit Umkreis
   - Jeder Ort: Freitext-Name + Schieberegler Radius (10–200 km)
   - Standard: `[{"name": "Kassel", "radius_km": 50}]`
   - Im Perplexity-Prompt: randomisierte Ortsangabe aus der Liste, z.B. "im Umkreis von 50km um Kassel" oder "Region Göttingen"

2. **`"land"`** — Ein bestimmtes Land (Freitext)
   - Beispiel: `"land": "Deutschland"`, `"land": "Österreich"`
   - Wenn aktiv: `orte` wird ignoriert (im Webpanel ausgegraut)

3. **`"verbund"`** — Staatenverbund (Dropdown, kein Freitext)
   - Werte: `"EU"`, `"USA"`, `"BRICS"`, `"Asien"`, `"Ostblock"`, `"Naher Osten"`, `"Lateinamerika"`
   - Wenn aktiv: `orte` und `land` ausgegraut

4. **`"weltweit"`** — Keine Ortsbeschränkung
   - Wenn aktiv: alles andere ausgegraut

### Stellenlevel (Schieberegler)
Zwei unabhängige Slider:

**Kompetenz-Level** (0.0 bis 1.0):
| Wert | Bedeutung | Perplexity-Prompt |
|------|-----------|-------------------|
| 0.0 | Azubi/Praktikant | "Ausbildung, Praktikum, Werkstudent" |
| 0.2 | Junior | "Berufseinsteiger, Junior" |
| 0.4 | Fachkraft | "Fachkraft, Techniker" |
| 0.6 | Ingenieur (Standard) | "Ingenieur, Entwickler, Spezialist" |
| 0.8 | Senior/Lead | "Senior Engineer, Teamlead, Projektleiter" |
| 1.0 | Professor/Direktor | "Direktor, Professor, VP Engineering" |

**Gehaltsrahmen** (Monatsbrutto, EUR):
- `gehalt_von`: Untergrenze (Standard: 4000)
- `gehalt_bis`: Obergrenze (Standard: 7000)
- Schieberegler-Range: 300 bis 30000 EUR/Monat
- **WICHTIG:** Dies ist nur ein Richtwert für die Suche. Viele Ausschreibungen nennen kein Gehalt — das darf Stellen nicht ausschließen! Der Überwacher schätzt das Gehalt nachträglich per LLM.

**Anstellungsart** (Dropdown):
- `"festanstellung"` (Standard)
- `"befristet"`
- `"freiberuflich"`
- `"alle"`

### Blacklist-Domains
Liste von Domains deren URLs der Lumpensammler ignorieren soll. Editierbar als Textfeld (eine Domain pro Zeile). Standard:
```
freelancermap.de
green-energy-jobs.net
```

## Umsetzung

### In `lumpensammler.py`
1. `suchkriterien` aus Config lesen
2. In `ensure_query()` bzw. `build_query()`: Die Suchkriterien in den Perplexity-Prompt einbauen
3. **Orts-Randomisierung**: Bei Modus `"orte"` pro Suchquery zufällig einen der Orte wählen und den Radius variieren (±20%)
4. **Blacklist**: In `extract_urls()` oder `read_job_posting()`: URLs von blacklisted Domains überspringen
5. **Level/Gehalt**: In den Perplexity-Prompt als Richtwert einfließen lassen

### Query-Cache Invalidierung
Wenn sich `suchkriterien` ändern, muss `query_cache.json` invalidiert werden. Entweder:
- Hash der suchkriterien im Cache speichern und vergleichen
- Oder einfach: Config-mtime prüfen (wie bei Kompetenzen.md)

### Config-API für Webpanel
```python
def get_config() -> dict:
    return json.loads(Path("lumpensammler.conf").read_text())

def update_config(updates: dict):
    conf = get_config()
    if "suchkriterien" in updates:
        conf["suchkriterien"] = updates["suchkriterien"]
    Path("lumpensammler.conf").write_text(json.dumps(conf, indent=2, ensure_ascii=False))
    # Cache invalidieren bei Suchkriterien-Änderung
    cache = Path("query_cache.json")
    if cache.exists():
        cache.unlink()
```

## Querverweise
- **Überwacher**: Prüft nachträglich ob gefundene Stellen zum Level/Gehalt passen — die Lumpensammler-Suche ist der Breitenfilter, der Überwacher der Qualitätsfilter
- **Maintainer**: `Kompetenzen.md` beeinflusst den Query unabhängig von den Suchkriterien
- **Webpanel**: Zeigt alle Suchkriterien-Einstellungen + den aktuellen generierten Query aus `query_cache.json`

Starte damit, `lumpensammler.py` und `lumpensammler.conf` zu lesen. Dann bau die Suchkriterien-Integration in den Perplexity-Prompt ein.
