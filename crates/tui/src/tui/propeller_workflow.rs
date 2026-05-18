//! Propeller Discovery→Plan workflow.
//!
//! When a new chat starts in `AppMode::Propeller`, the workflow runs in two phases:
//!
//! 1. **Discovery** (sub_model, fast — 35B-a3b): the model asks the user clarifying
//!    questions until it has enough context. Must ask **at least 3** questions.
//!    Signals readiness by emitting `<plan-ready/>` in its response.
//!
//! 2. **Planning** (main_model, deep — 27B-dense + reasoning_effort=max): the model
//!    receives the full discovery transcript plus a planning system prompt and
//!    produces a structured project plan (goals, work-items, test-bench requirements,
//!    risks).
//!
//! Phases 3–6 (orchestration / subagent dispatch / testbench.py generation / test
//! run) are explicitly out of scope for the first iteration.

/// Signal token the discovery model emits to hand off to the planning phase.
/// Chosen to be unlikely in normal prose and easy to detect with a substring scan.
pub const PLAN_READY_SIGNAL: &str = "<plan-ready/>";

/// Minimum number of assistant questions before the signal is accepted. Lower than
/// this, even an emitted signal is ignored — we want the discovery phase to actually
/// probe the user, not skip straight to planning.
pub const MIN_DISCOVERY_QUESTIONS: u32 = 3;

/// Current phase of the propeller workflow. `Free` means the workflow has completed
/// (plan delivered) and the chat behaves as a regular Propeller-mode agent session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PropellerPhase {
    Discovery,
    Planning,
    Free,
}

impl PropellerPhase {
    pub fn label(self) -> &'static str {
        match self {
            PropellerPhase::Discovery => "discovery",
            PropellerPhase::Planning => "planning",
            PropellerPhase::Free => "free",
        }
    }
}

/// System prompt for the Discovery phase. Drives the 35B to ask clarifying questions
/// and emit the signal token when it has enough context.
pub const DISCOVERY_SYSTEM_PROMPT: &str = "\
Du bist die DISCOVERY-Phase eines zweistufigen Agenten-Workflows.
Deine Aufgabe in dieser Phase ist NICHT die Aufgabe selbst zu lösen, sondern die \
Anforderung des Users durch gezielte Rückfragen zu klären, bis die Spezifikation \
ausreichend für die Planungsphase ist.

REGELN:
1. Stelle ZUERST mindestens DREI Rückfragen, eine pro Antwort. Sammle iterativ \
   Kontext: Was genau soll gebaut werden? Welche Constraints (Sprache, Library, \
   Plattform, Performance)? Welches Erfolgskriterium? Wer benutzt das Ergebnis?
2. Wenn Du nach drei Runden noch substantielle Lücken siehst, frage weiter.
3. Wenn der Erfolgsfall sauber definiert ist (Eingabe, Verhalten, Akzeptanz-\
   Kriterien) und Du keine offenen Fragen mehr hast, antworte mit einer kurzen \
   Zusammenfassung der gesammelten Anforderungen und beende deine Nachricht mit \
   genau diesem Token in einer eigenen Zeile:

       <plan-ready/>

4. Schreibe NIEMALS Code in dieser Phase. KEIN Lösungsvorschlag, KEINE \
   Implementations-Vorschau. Nur Klärung und Zusammenfassung.
5. Stil: präzise, knapp, ein Gedanke pro Frage. Auf Deutsch antworten falls der \
   User Deutsch schreibt.";

/// System prompt for the Planning phase. Receives the discovery transcript and
/// produces a structured plan. Uses the 27B with `reasoning_effort=max`.
pub const PLANNING_SYSTEM_PROMPT: &str = "\
Du bist die PLANUNGS-Phase eines zweistufigen Agenten-Workflows. Die DISCOVERY-\
Phase hat soeben die Anforderungen mit dem User geklärt. Du erhältst den \
vollständigen Discovery-Verlauf als Kontext.

Deine einzige Aufgabe: Daraus einen strukturierten, ausführbaren Plan erstellen.

FORMAT (Markdown, exakt diese Sektionen, in dieser Reihenfolge):

# Projektplan

## 1. Ziel
Ein einziger Absatz, was am Ende existieren muss. Keine Vermutungen — nur was \
in der Discovery bestätigt wurde.

## 2. Arbeitspakete
Nummerierte Liste konkreter Tasks. Pro Task:
- **Was:** ein Satz.
- **Komplexität:** *einfach* (kann an Sub-Agent 35B delegiert werden) oder *anspruchsvoll* (bleibt beim 27B-Hauptagent).
- **Abhängigkeit:** Task-Nummern die zuerst fertig sein müssen, oder *keine*.

## 3. Test-Strategie
Beschreibe was `testbench.py` prüfen soll — eine eigenständige Python-Datei \
die nach erfolgreicher Implementierung alle Arbeitspakete unabhängig verifiziert. \
Liste pro Arbeitspaket einen Akzeptanz-Check (Eingabe → erwartete Ausgabe).

## 4. Risiken
2–4 Bullet-Points: was kann schief gehen, was ist unklar geblieben.

WICHTIG: Schreibe KEINEN Code, KEINE testbench.py — nur den Plan. Die \
Implementierungsphase folgt separat.";

/// Returns true when the assistant message contains the `<plan-ready/>` signal.
/// The check is case-insensitive and tolerates surrounding whitespace.
#[must_use]
pub fn has_plan_ready_signal(text: &str) -> bool {
    text.to_lowercase().contains(PLAN_READY_SIGNAL)
}

/// Strips the `<plan-ready/>` signal from a transcript so it isn't shown to the
/// user when the message is displayed. Keeps the surrounding text intact.
#[must_use]
pub fn strip_plan_ready_signal(text: &str) -> String {
    // Substring removal of the literal token; tolerant of case differences by
    // lower-casing for the match but reconstructing from the original.
    let lower = text.to_lowercase();
    let needle = PLAN_READY_SIGNAL;
    let Some(idx) = lower.find(needle) else {
        return text.to_string();
    };
    let mut out = String::with_capacity(text.len());
    out.push_str(&text[..idx]);
    out.push_str(&text[idx + needle.len()..]);
    out.trim_end().to_string()
}

/// True when the assistant has asked enough questions to satisfy the discovery
/// minimum. Counts trailing `?` runs across all assistant messages — a single
/// message containing three questions counts as three.
#[must_use]
pub fn discovery_questions_satisfied(assistant_messages: &[&str]) -> bool {
    let count: usize = assistant_messages
        .iter()
        .map(|m| m.chars().filter(|c| *c == '?').count())
        .sum();
    count >= MIN_DISCOVERY_QUESTIONS as usize
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signal_detection_is_case_insensitive() {
        assert!(has_plan_ready_signal("alles klar.\n<plan-ready/>"));
        assert!(has_plan_ready_signal("ALLES KLAR.\n<PLAN-READY/>"));
        assert!(!has_plan_ready_signal("plan ready"));
    }

    #[test]
    fn strip_removes_signal_and_trailing_whitespace() {
        let cleaned = strip_plan_ready_signal("Zusammenfassung: foo.\n<plan-ready/>\n");
        assert_eq!(cleaned, "Zusammenfassung: foo.");
    }

    #[test]
    fn three_questions_satisfied() {
        assert!(discovery_questions_satisfied(&[
            "Was genau? Welche Sprache?",
            "Wer ist Zielgruppe?",
        ]));
    }

    #[test]
    fn two_questions_not_satisfied() {
        assert!(!discovery_questions_satisfied(&[
            "Was genau?",
            "Welche Sprache?",
        ]));
    }
}
