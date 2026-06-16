# Behavior — Verhaltensregeln

## Recherche
- Perplexity-Queries sind präzise und auf das Profil zugeschnitten
- Seed-Vorgaben des Nutzers werden eingehalten und in den Query eingebaut
- Ping-Pong maximal 3 Runden — danach wird das Ergebnis so genommen wie es ist
- Wenn eine Stelle nach 3 Runden immer noch keine URL hat, wird sie als "url_fehlt" markiert

## Datenbankpflege
- Vor dem Anlegen immer Duplikatcheck (Firma + Titel, case-insensitive)
- Referenznummern lückenlos aufsteigend (0001, 0002, ...)
- Status bei Neuanlage: "neu"
- Lumpensammler_OK=False bei manuell angelegten Zeilen → Poll verarbeitet diese

## Ausschreibung.md
- Pflichtfelder: Stelle, Unternehmen, Standort, Referenz, Datum
- Optionale Felder: Kontakt, URL, Aufgaben, Anforderungen, Angebot
- Fehlt ein Pflichtfeld weil Perplexity es nicht liefern konnte → "(nicht ermittelt)" eintragen

## Fehlerverhalten
- LLM nicht erreichbar → Fehlermeldung + Abbruch
- Perplexity nicht erreichbar → Warnung + Retry nach 10s, max 2 Versuche
- XLSX-Fehler → Fehlermeldung + kein Schreiben (lieber sicher)

## Was ich NICHT tue
- Stellenanzeigen erfinden oder halluzinieren
- Eine Stelle anlegen wenn sie kein einziges valides Metadatum hat
- Dateien außerhalb von Bewerbungen/ und dem eigenen Verzeichnis anlegen
- Den Maintainer-Teil erledigen (Kompetenzen.md anpassen ist nicht meine Aufgabe)
