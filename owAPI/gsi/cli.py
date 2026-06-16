from __future__ import annotations
import argparse

def parse_args():
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--project", type=str, help="Projektname (optional – kann auch aus update.json kommen)")
    grp.add_argument("--new", type=str, help="Neues Projekt anlegen")
    ap.add_argument("--url", type=str, help="URL für Projekt (bei --new oder --set-url)")
    ap.add_argument("--desc", type=str, help="Beschreibung für Projekt (bei --new oder --set-desc)")
    ap.add_argument("--set-url", type=str, help="URL für bestehendes Projekt setzen (Name)")
    ap.add_argument("--set-desc", type=str, help="Beschreibung für bestehendes Projekt setzen (Name)")
    ap.add_argument("--delete", type=str, help="Projekt löschen")
    ap.add_argument("--rename", type=str, help="Projekt umbenennen: aktueller Name")
    ap.add_argument("--to", type=str, help="Neuer Name für --rename")
    ap.add_argument("--list-projects", action="store_true", help="Alle Projekte auflisten")
    ap.add_argument("--show", type=str, help="Ein Projekt anzeigen")
    ap.add_argument("--force-project", action="store_true", help="Projekt-URL erzwingen, auch wenn update.json eine andere enthält")
    ap.add_argument("--debug", action="store_true", help="Verbose Debug-Logs")
    ap.add_argument("--log-file", type=str, help="Log in Datei schreiben")
    return ap.parse_args()
