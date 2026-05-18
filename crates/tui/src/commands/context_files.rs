//! Context-file commands: read-only Anzeige der drei Vibecoding-MDs
//! (CLAUDE.md, spec.md, tagebuch.md) im Arbeitsverzeichnis.
//!
//! Diese Commands schreiben nichts — sie spiegeln den Inhalt nur ins
//! Chat-Scrollback, damit der User nicht vergisst dass diese Dateien
//! existieren und gepflegt werden sollten.
//!
//! Datei-Lookup: ausschließlich `app.workspace/<name>.md`. Kein Tree-Walk,
//! kein Parent-Hochlaufen. Fehlt die Datei → freundliche Meldung mit Hinweis.

use std::fs;

use crate::tui::app::App;

use super::CommandResult;

const MAX_RENDER_BYTES: usize = 64 * 1024; // ≈ 64 KB ist genug für jede sinnvolle Vibecoding-MD

/// `/spec` und Alias `/plan` — zeigt spec.md komplett.
pub fn spec(app: &App) -> CommandResult {
    render_file(app, "spec.md")
}

/// `/claude` — zeigt CLAUDE.md komplett.
pub fn claude(app: &App) -> CommandResult {
    render_file(app, "CLAUDE.md")
}

/// `/tagebuch [all]` — zeigt entweder die letzten N Einträge (Default 3 aus
/// propeller.conf `tagebuch_entries`) oder die komplette Datei wenn `all`
/// als Argument übergeben wird.
pub fn tagebuch(app: &App, arg: Option<&str>) -> CommandResult {
    let path = app.workspace.join("tagebuch.md");
    let content = match fs::read_to_string(&path) {
        Ok(c) => c,
        Err(_) => {
            return CommandResult::message(format!(
                "tagebuch.md fehlt unter {} — leg sie an um den Session-Log zu pflegen.",
                path.display()
            ));
        }
    };

    let show_all = arg.map(|s| s.trim().eq_ignore_ascii_case("all")).unwrap_or(false);
    let body = if show_all {
        content
    } else {
        let n = crate::tui::app::read_strix_conf_tagebuch_entries();
        last_n_dated_sections(&content, n)
    };

    let bounded = if body.len() > MAX_RENDER_BYTES {
        format!("{}\n\n[… abgeschnitten, Datei zu lang]\n", &body[..MAX_RENDER_BYTES])
    } else {
        body
    };
    CommandResult::message(format!("📓 tagebuch.md\n\n{bounded}"))
}

/// `/context` — Übersicht: welche Vibecoding-Files im Arbeitsverzeichnis liegen,
/// mit Existenz-Status, Pfaden und der Liste der zugehörigen Slash-Commands.
pub fn context(app: &App) -> CommandResult {
    let ws = &app.workspace;
    let claude_path = ws.join("CLAUDE.md");
    let spec_path = ws.join("spec.md");
    let tagebuch_path = ws.join("tagebuch.md");

    let claude_status = file_indicator(&claude_path);
    let (spec_status, spec_open) = spec_status(&spec_path);
    let (tagebuch_status, tagebuch_last) = tagebuch_status(&tagebuch_path);

    let mut out = String::new();
    out.push_str("📋 Vibecoding-Kontext\n\n");
    out.push_str(&format!("  Arbeitsverzeichnis: {}\n\n", ws.display()));
    out.push_str(&format!("  CLAUDE.md     {claude_status}\n"));
    out.push_str(&format!("                 {}\n", claude_path.display()));
    out.push_str(&format!("  spec.md       {spec_status}"));
    if let Some(n) = spec_open {
        out.push_str(&format!("  ({n} offen)"));
    }
    out.push('\n');
    out.push_str(&format!("                 {}\n", spec_path.display()));
    out.push_str(&format!("  tagebuch.md   {tagebuch_status}"));
    if let Some(date) = tagebuch_last {
        out.push_str(&format!("  (letzter: {date})"));
    }
    out.push('\n');
    out.push_str(&format!("                 {}\n", tagebuch_path.display()));
    out.push_str("\n  Commands:\n");
    out.push_str("    /claude            CLAUDE.md anzeigen\n");
    out.push_str("    /spec  | /plan     spec.md anzeigen\n");
    out.push_str("    /tagebuch [all]    Tagebuch (letzte N | komplett)\n");
    out.push_str("    /context           diese Übersicht\n");
    CommandResult::message(out)
}

// ── Helpers ────────────────────────────────────────────────────────────

fn render_file(app: &App, name: &str) -> CommandResult {
    let path = app.workspace.join(name);
    match fs::read_to_string(&path) {
        Ok(content) => {
            let bounded = if content.len() > MAX_RENDER_BYTES {
                format!("{}\n\n[… abgeschnitten, Datei zu lang]\n", &content[..MAX_RENDER_BYTES])
            } else {
                content
            };
            CommandResult::message(format!("📄 {name}\n\n{bounded}"))
        }
        Err(_) => CommandResult::message(format!(
            "{name} fehlt unter {} — leg sie an um den Vibecoding-Flow zu nutzen.",
            path.display()
        )),
    }
}

fn file_indicator(path: &std::path::Path) -> &'static str {
    if path.exists() { "✓" } else { "–" }
}

/// Returns (indicator, count of unchecked `- [ ]` items)
fn spec_status(path: &std::path::Path) -> (&'static str, Option<usize>) {
    match fs::read_to_string(path) {
        Ok(content) => {
            let open = content.lines().filter(|l| l.trim_start().starts_with("- [ ]")).count();
            ("✓", Some(open))
        }
        Err(_) => ("–", None),
    }
}

/// Returns (indicator, last `## YYYY-...` heading text)
fn tagebuch_status(path: &std::path::Path) -> (&'static str, Option<String>) {
    match fs::read_to_string(path) {
        Ok(content) => {
            let last = content
                .lines()
                .find(|l| l.starts_with("## "))
                .map(|l| l.trim_start_matches("## ").trim().to_string());
            ("✓", last)
        }
        Err(_) => ("–", None),
    }
}

/// Returns the last `n` `## ...` dated sections from tagebuch.md, joined.
/// Sections are split on lines starting with `## `. If there are fewer than `n`,
/// returns all of them.
fn last_n_dated_sections(content: &str, n: usize) -> String {
    if n == 0 {
        return String::new();
    }
    let mut sections: Vec<String> = Vec::new();
    let mut current = String::new();
    let mut in_section = false;
    for line in content.lines() {
        if line.starts_with("## ") {
            if in_section && !current.trim().is_empty() {
                sections.push(std::mem::take(&mut current));
            }
            in_section = true;
            current.push_str(line);
            current.push('\n');
        } else if in_section {
            current.push_str(line);
            current.push('\n');
        }
    }
    if in_section && !current.trim().is_empty() {
        sections.push(current);
    }
    if sections.len() <= n {
        return sections.join("\n");
    }
    sections[..n].join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn last_n_grabs_first_n_dated_sections() {
        let md = "# header\n\n## 2026-05-17 — heute\n- a\n\n## 2026-05-16 — gestern\n- b\n\n## 2026-05-15 — vorgestern\n- c\n";
        let two = last_n_dated_sections(md, 2);
        assert!(two.contains("2026-05-17"));
        assert!(two.contains("2026-05-16"));
        assert!(!two.contains("2026-05-15"));
    }

    #[test]
    fn last_n_zero_returns_empty() {
        assert_eq!(last_n_dated_sections("## foo\n- a\n", 0), "");
    }
}
