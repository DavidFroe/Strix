//! `/model` picker modal: pick a DeepSeek model and a thinking-effort tier
//! and apply both at once (#39).
//!
//! Two side-by-side panes — Models on the left, Thinking effort on the
//! right. Tab swaps focus, ↑/↓ moves within the focused pane, Enter applies
//! both and closes the modal, Esc cancels.
//!
//! The effort pane intentionally only exposes `Off / High / Max`. Per
//! DeepSeek's [Thinking Mode docs](https://api-docs.deepseek.com/guides/reasoning_model),
//! `low`/`medium` are silently mapped to `high` server-side and `xhigh` is
//! mapped to `max`, so surfacing them as separate choices would be misleading.
//! The legacy variants remain valid in `~/.deepseek/settings.toml` for
//! back-compat — the picker just doesn't offer them.
//!
//! On apply we emit a [`ViewEvent::ModelPickerApplied`] with the resolved
//! model id and effort tier; the UI handler updates `App` state, persists
//! the choice via `Settings`, and forwards `Op::SetModel` so the running
//! engine picks up the change without a restart.

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    buffer::Buffer,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph, Widget},
};

use crate::models::OwltrailModel;
use crate::palette;
use crate::tui::app::{App, ReasoningEffort};
use crate::tui::views::{ModalKind, ModalView, ViewAction, ViewEvent};

/// Hard-coded fallback used when the adapter at `/v1/models` was unreachable
/// at boot. Power users can still type `/model <id>` for anything else.
const FALLBACK_PICKER_MODELS: &[(&str, &str)] = &[
    ("auto", "select per turn"),
    ("deepseek-v4-pro", "flagship"),
    ("deepseek-v4-flash", "fast / cheap"),
];

#[derive(Debug, Clone)]
struct PickerModel {
    id: String,
    label: String,
    hint: String,
    available: bool,
}

fn fallback_picker_models() -> Vec<PickerModel> {
    FALLBACK_PICKER_MODELS
        .iter()
        .map(|(id, hint)| PickerModel {
            id: (*id).to_string(),
            label: crate::models::display_model_name(id),
            hint: (*hint).to_string(),
            available: true,
        })
        .collect()
}

fn picker_models_from_adapter(models: &[OwltrailModel]) -> Vec<PickerModel> {
    let mut rows: Vec<PickerModel> = Vec::with_capacity(models.len() + 1);
    rows.push(PickerModel {
        id: "auto".to_string(),
        label: "auto".to_string(),
        hint: "select per turn".to_string(),
        available: true,
    });
    let mut converted: Vec<PickerModel> = models
        .iter()
        .map(|m| {
            let available = m.is_available();
            let hint = if available {
                String::new()
            } else if m.status.is_empty() {
                "unavailable".to_string()
            } else {
                m.status.clone()
            };
            PickerModel {
                id: m.id.clone(),
                label: m.display_label(),
                hint,
                available,
            }
        })
        .collect();
    // Available first, then the rest. Keep original order within each
    // partition so the adapter's ordering survives where it matters.
    converted.sort_by(|a, b| b.available.cmp(&a.available));
    rows.extend(converted);
    rows
}

/// Thinking-effort rows shown in the picker, in the order DeepSeek
/// behaviorally distinguishes them.
const PICKER_EFFORTS: &[ReasoningEffort] = &[
    ReasoningEffort::Auto,
    ReasoningEffort::Off,
    ReasoningEffort::High,
    ReasoningEffort::Max,
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Pane {
    Model,
    Effort,
}

/// Which logical model slot the picker is editing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PickerTarget {
    /// Main interactive agent (`app.model`).
    Main,
    /// Sub-agent / background tasks (`app.subagent_model`).
    Subagent,
}

pub struct ModelPickerView {
    initial_model: String,
    initial_effort: ReasoningEffort,
    /// Working selection (separate from the initial values so we can offer a
    /// clean Esc-to-cancel without mutating App state).
    selected_model_idx: usize,
    selected_effort_idx: usize,
    focus: Pane,
    /// True when the active model is one we don't list — we still show it
    /// so the picker doesn't quietly forget the user's chosen IDs.
    show_custom_model_row: bool,
    /// Concrete rows to render — built from `app.available_models` when the
    /// adapter responded, else the hard-coded fallback.
    rows: Vec<PickerModel>,
    /// Which logical model the picker is editing.
    target: PickerTarget,
}

impl ModelPickerView {
    #[must_use]
    pub fn new(app: &App) -> Self {
        Self::new_with_target(app, PickerTarget::Main)
    }

    /// Build a picker that edits the sub-agent model instead of the main one.
    /// Selecting `auto` clears the override so sub-agents inherit the main
    /// model again.
    #[must_use]
    pub fn new_for_subagent(app: &App) -> Self {
        Self::new_with_target(app, PickerTarget::Subagent)
    }

    fn new_with_target(app: &App, target: PickerTarget) -> Self {
        let hide_deepseek_models = crate::config::provider_passes_model_through(app.api_provider);
        let initial_model = match target {
            PickerTarget::Main => {
                if app.auto_model {
                    "auto".to_string()
                } else {
                    app.model.clone()
                }
            }
            // For the sub-agent slot we treat "no override" as the
            // synthetic `auto` row so the picker still has a sensible
            // landing spot and the user can clear the override.
            PickerTarget::Subagent => app
                .subagent_model
                .clone()
                .unwrap_or_else(|| "auto".to_string()),
        };
        // On pass-through providers, only show "auto" and the custom row.
        let rows: Vec<PickerModel> = if hide_deepseek_models {
            vec![PickerModel {
                id: "auto".to_string(),
                label: "auto".to_string(),
                hint: "select per turn".to_string(),
                available: true,
            }]
        } else if app.available_models.is_empty() {
            fallback_picker_models()
        } else {
            picker_models_from_adapter(&app.available_models)
        };
        // Match the active model to an adapter row. The TUI canonicalises
        // `app.model` to `deepseek-v4-pro`, while the adapter advertises the
        // same model as `deepseek-deepseek-v4-pro` (provider-prefixed). An
        // exact-equality lookup misses that case and lands the cursor on
        // the off-screen "custom" row — pressing Enter then re-applies the
        // same model, which is the source of the "Enter tut nix" bug.
        // Fall back to suffix-matching (`-<needle>`) so the canonical name
        // resolves to the prefixed adapter row.
        let needle = initial_model.trim().to_ascii_lowercase();
        let mut selected_model_idx = rows
            .iter()
            .position(|r| r.id.eq_ignore_ascii_case(&needle));
        if selected_model_idx.is_none() && !needle.is_empty() {
            let suffix = format!("-{needle}");
            selected_model_idx = rows
                .iter()
                .position(|r| r.id.to_ascii_lowercase().ends_with(&suffix));
        }
        let show_custom_model_row = selected_model_idx.is_none();
        if show_custom_model_row {
            selected_model_idx = Some(rows.len());
        }
        let selected_model_idx = selected_model_idx.unwrap_or(0);

        let initial_effort = app.reasoning_effort;
        // Map low/medium → high, xhigh → max for picker purposes.
        let normalized = match initial_effort {
            ReasoningEffort::Low | ReasoningEffort::Medium => ReasoningEffort::High,
            other => other,
        };
        let selected_effort_idx = PICKER_EFFORTS
            .iter()
            .position(|e| *e == normalized)
            .unwrap_or(2); // default to High if somehow unknown

        Self {
            initial_model,
            initial_effort,
            selected_model_idx,
            selected_effort_idx,
            focus: Pane::Model,
            show_custom_model_row,
            rows,
            target,
        }
    }

    fn model_row_count(&self) -> usize {
        self.rows.len() + if self.show_custom_model_row { 1 } else { 0 }
    }

    /// Resolve the currently highlighted model row to a model id. If the
    /// custom row is selected we return the original model from the App so
    /// "Apply" doesn't blow away an unrecognised id.
    fn resolved_model(&self) -> String {
        if self.show_custom_model_row && self.selected_model_idx == self.rows.len() {
            self.initial_model.clone()
        } else if self.selected_model_idx < self.rows.len() {
            self.rows[self.selected_model_idx].id.clone()
        } else {
            self.initial_model.clone()
        }
    }

    fn resolved_effort(&self) -> ReasoningEffort {
        if self.resolved_model().trim().eq_ignore_ascii_case("auto") {
            return ReasoningEffort::Auto;
        }
        PICKER_EFFORTS[self.selected_effort_idx]
    }

    fn move_up(&mut self) {
        match self.focus {
            Pane::Model => {
                if self.selected_model_idx > 0 {
                    self.selected_model_idx -= 1;
                }
            }
            Pane::Effort => {
                if self.selected_effort_idx > 0 {
                    self.selected_effort_idx -= 1;
                }
            }
        }
    }

    fn move_down(&mut self) {
        match self.focus {
            Pane::Model => {
                let max = self.model_row_count().saturating_sub(1);
                if self.selected_model_idx < max {
                    self.selected_model_idx += 1;
                }
            }
            Pane::Effort => {
                let max = PICKER_EFFORTS.len().saturating_sub(1);
                if self.selected_effort_idx < max {
                    self.selected_effort_idx += 1;
                }
            }
        }
    }

    fn toggle_focus(&mut self) {
        self.focus = match self.focus {
            Pane::Model => Pane::Effort,
            Pane::Effort => Pane::Model,
        };
    }

    fn build_event(&self) -> ViewEvent {
        match self.target {
            PickerTarget::Main => ViewEvent::ModelPickerApplied {
                model: self.resolved_model(),
                effort: self.resolved_effort(),
                previous_model: self.initial_model.clone(),
                previous_effort: self.initial_effort,
            },
            PickerTarget::Subagent => {
                let resolved = self.resolved_model();
                let model = if resolved.trim().eq_ignore_ascii_case("auto") {
                    None
                } else {
                    Some(resolved)
                };
                let previous_model = if self.initial_model.trim().eq_ignore_ascii_case("auto") {
                    None
                } else {
                    Some(self.initial_model.clone())
                };
                ViewEvent::SubmodelPickerApplied {
                    model,
                    previous_model,
                }
            }
        }
    }

    fn render_pane(
        &self,
        area: Rect,
        buf: &mut Buffer,
        title: &str,
        rows: Vec<(String, String, bool)>,
        selected: usize,
        focused: bool,
    ) {
        let border_style = if focused {
            Style::default().fg(palette::accent_light())
        } else {
            Style::default().fg(palette::accent_border())
        };
        let block = Block::default()
            .title(Line::from(Span::styled(
                format!(" {title} "),
                Style::default().fg(palette::TEXT_PRIMARY).bold(),
            )))
            .borders(Borders::ALL)
            .border_style(border_style)
            .style(Style::default().bg(palette::SURFACE_PANEL));
        let inner = block.inner(area);
        block.render(area, buf);

        let visible_h = inner.height as usize;
        // Scroll the list so the selected item stays within the visible window.
        let scroll_top = selected.saturating_sub(visible_h.saturating_sub(1));
        let end = (scroll_top + visible_h).min(rows.len());
        let visible_rows = &rows[scroll_top..end];
        let local_selected = selected.saturating_sub(scroll_top);

        let mut lines = Vec::with_capacity(visible_rows.len());
        for (idx, (label, hint, available)) in visible_rows.iter().enumerate() {
            let is_selected = idx == local_selected;
            let marker = if is_selected { "▸" } else { " " };
            let base_fg = if *available {
                palette::TEXT_PRIMARY
            } else {
                palette::TEXT_MUTED
            };
            let label_style = if is_selected {
                let mut s = Style::default()
                    .fg(palette::SELECTION_TEXT)
                    .bg(palette::accent_selection())
                    .add_modifier(Modifier::BOLD);
                if !*available {
                    s = s.add_modifier(Modifier::DIM);
                }
                s
            } else {
                let mut s = Style::default().fg(base_fg).bg(palette::SURFACE_PANEL);
                if !*available {
                    s = s.add_modifier(Modifier::DIM);
                }
                s
            };
            let hint_style = if is_selected {
                Style::default()
                    .fg(palette::SELECTION_TEXT)
                    .bg(palette::accent_selection())
            } else {
                Style::default().fg(palette::TEXT_MUTED).bg(palette::SURFACE_PANEL)
            };
            let mut spans = vec![
                Span::raw(" "),
                Span::styled(marker, label_style),
                Span::raw(" "),
                Span::styled(label.clone(), label_style),
            ];
            if !hint.is_empty() {
                spans.push(Span::raw("  "));
                spans.push(Span::styled(format!("({hint})"), hint_style));
            }
            lines.push(Line::from(spans));
        }
        // Scroll indicator suffix in the title if list is clipped.
        let _has_more_above = scroll_top > 0;
        let _has_more_below = end < rows.len();

        Paragraph::new(lines)
            .style(Style::default().bg(palette::SURFACE_PANEL))
            .render(inner, buf);
    }
}

impl ModalView for ModelPickerView {
    fn kind(&self) -> ModalKind {
        match self.target {
            PickerTarget::Main => ModalKind::ModelPicker,
            PickerTarget::Subagent => ModalKind::SubmodelPicker,
        }
    }

    fn as_any_mut(&mut self) -> &mut dyn std::any::Any {
        self
    }

    fn handle_key(&mut self, key: KeyEvent) -> ViewAction {
        match key.code {
            KeyCode::Esc => ViewAction::Close,
            KeyCode::Enter => ViewAction::EmitAndClose(self.build_event()),
            KeyCode::Up => {
                self.move_up();
                ViewAction::None
            }
            KeyCode::Down => {
                self.move_down();
                ViewAction::None
            }
            KeyCode::Tab | KeyCode::Right | KeyCode::Left | KeyCode::BackTab => {
                self.toggle_focus();
                ViewAction::None
            }
            _ => ViewAction::None,
        }
    }

    fn render(&self, area: Rect, buf: &mut Buffer) {
        let popup_width = 76.min(area.width.saturating_sub(4)).max(50);
        let popup_height = 28.min(area.height.saturating_sub(4)).max(12);
        let popup_area = Rect {
            x: area.x + (area.width.saturating_sub(popup_width)) / 2,
            y: area.y + (area.height.saturating_sub(popup_height)) / 2,
            width: popup_width,
            height: popup_height,
        };

        Clear.render(popup_area, buf);

        let title_text = match self.target {
            PickerTarget::Main => " ⚙ Model & Thinking ",
            PickerTarget::Subagent => " ⚙ Sub-agent Model ",
        };
        // Build footer hint depending on which slot we're editing.
        let subagent_hint: Vec<Span> = match self.target {
            PickerTarget::Main => vec![
                Span::styled(" /submodel ", Style::default().fg(palette::TEXT_MUTED)),
                Span::raw("sub-agent  "),
            ],
            PickerTarget::Subagent => vec![
                Span::styled(" /model ", Style::default().fg(palette::TEXT_MUTED)),
                Span::raw("main  "),
            ],
        };
        let mut footer_spans = subagent_hint;
        footer_spans.extend([
            Span::styled(" ↑↓ ", Style::default().fg(palette::TEXT_MUTED)),
            Span::raw("scroll "),
            Span::styled(" Tab ", Style::default().fg(palette::TEXT_MUTED)),
            Span::raw("switch "),
            Span::styled(" Enter ", Style::default().fg(palette::TEXT_MUTED)),
            Span::raw("apply "),
            Span::styled(" Esc ", Style::default().fg(palette::TEXT_MUTED)),
            Span::raw("cancel "),
        ]);

        // Outer chrome with title + footer hint.
        let outer = Block::default()
            .title(Line::from(Span::styled(
                title_text,
                Style::default()
                    .fg(palette::accent_light())
                    .add_modifier(Modifier::BOLD),
            )))
            .title_bottom(Line::from(footer_spans))
            .borders(Borders::ALL)
            .border_style(Style::default().fg(palette::accent_border()))
            .style(Style::default().bg(palette::SURFACE_ELEVATED));
        let inner = outer.inner(popup_area);
        outer.render(popup_area, buf);

        let columns = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Percentage(60), Constraint::Percentage(40)])
            .split(inner);

        let mut model_rows: Vec<(String, String, bool)> = self
            .rows
            .iter()
            .map(|row| (row.label.clone(), row.hint.clone(), row.available))
            .collect();
        if self.show_custom_model_row {
            model_rows.push((
                crate::models::display_model_name(&self.initial_model),
                "current (custom)".to_string(),
                true,
            ));
        }
        self.render_pane(
            columns[0],
            buf,
            "Model",
            model_rows,
            self.selected_model_idx,
            self.focus == Pane::Model,
        );

        let effort_rows: Vec<(String, String, bool)> = PICKER_EFFORTS
            .iter()
            .map(|effort| {
                let label = effort.short_label().to_string();
                let hint = match effort {
                    ReasoningEffort::Auto => "auto-select per turn".to_string(),
                    ReasoningEffort::Off => "thinking disabled".to_string(),
                    ReasoningEffort::High => "thinking enabled (default)".to_string(),
                    ReasoningEffort::Max => "thinking enabled, max effort".to_string(),
                    _ => String::new(),
                };
                (label, hint, true)
            })
            .collect();
        self.render_pane(
            columns[1],
            buf,
            "Thinking",
            effort_rows,
            self.selected_effort_idx,
            self.focus == Pane::Effort,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::tui::app::{App, TuiOptions};
    use std::path::PathBuf;

    fn create_test_app() -> App {
        let options = TuiOptions {
            model: "deepseek-v4-pro".to_string(),
            workspace: PathBuf::from("."),
            config_path: None,
            config_profile: None,
            allow_shell: false,
            use_alt_screen: true,
            use_mouse_capture: false,
            use_bracketed_paste: true,
            max_subagents: 1,
            skills_dir: PathBuf::from("."),
            memory_path: PathBuf::from("memory.md"),
            notes_path: PathBuf::from("notes.txt"),
            mcp_config_path: PathBuf::from("mcp.json"),
            use_memory: false,
            start_in_agent_mode: true,
            skip_onboarding: true,
            yolo: false,
            resume_session_id: None,
            initial_input: None,
        };
        let mut app = App::new(options, &Config::default());
        // App::new merges in `~/.config/deepseek/settings.toml` /
        // `Application Support/deepseek/settings.toml`, which can override
        // the model and effort with whatever the developer happens to have
        // saved. Pin both back to known values so the picker tests below
        // exercise the picker logic, not the user's environment.
        app.model = "deepseek-v4-pro".to_string();
        app.reasoning_effort = ReasoningEffort::Max;
        app
    }

    #[test]
    fn picker_initial_selection_matches_app_state() {
        let mut app = create_test_app();
        app.model = "deepseek-v4-flash".to_string();
        app.reasoning_effort = ReasoningEffort::Max;
        let view = ModelPickerView::new(&app);
        assert_eq!(view.resolved_model(), "deepseek-v4-flash");
        assert_eq!(view.resolved_effort(), ReasoningEffort::Max);
    }

    #[test]
    fn picker_initial_selection_matches_auto_state() {
        let mut app = create_test_app();
        app.model = "auto".to_string();
        app.auto_model = true;
        app.reasoning_effort = ReasoningEffort::Auto;

        let view = ModelPickerView::new(&app);

        assert_eq!(view.resolved_model(), "auto");
        assert_eq!(view.resolved_effort(), ReasoningEffort::Auto);
    }

    #[test]
    fn picker_auto_model_forces_auto_effort_on_apply() {
        let mut app = create_test_app();
        app.model = "auto".to_string();
        app.auto_model = true;
        app.reasoning_effort = ReasoningEffort::Off;

        let mut view = ModelPickerView::new(&app);
        view.selected_model_idx = 0;
        view.selected_effort_idx = PICKER_EFFORTS
            .iter()
            .position(|effort| *effort == ReasoningEffort::Max)
            .expect("max effort row");

        assert_eq!(view.resolved_model(), "auto");
        assert_eq!(view.resolved_effort(), ReasoningEffort::Auto);
    }

    #[test]
    fn picker_normalizes_low_medium_to_high() {
        let mut app = create_test_app();
        app.reasoning_effort = ReasoningEffort::Medium;
        let view = ModelPickerView::new(&app);
        assert_eq!(
            view.resolved_effort(),
            ReasoningEffort::High,
            "medium should map to high in the picker"
        );
    }

    #[test]
    fn picker_exposes_auto_and_distinct_thinking_tiers() {
        let model_labels: Vec<_> = FALLBACK_PICKER_MODELS.iter().map(|(id, _)| *id).collect();
        assert_eq!(
            model_labels,
            vec!["auto", "deepseek-v4-pro", "deepseek-v4-flash"]
        );

        let effort_labels: Vec<_> = PICKER_EFFORTS
            .iter()
            .map(|effort| effort.as_setting())
            .collect();
        assert_eq!(effort_labels, vec!["auto", "off", "high", "max"]);
    }

    #[test]
    fn picker_matches_prefixed_adapter_id_via_suffix() {
        // The OwlTrail adapter advertises DeepSeek models with a redundant
        // `deepseek-` owner prefix (`deepseek-deepseek-v4-pro`), while
        // `app.model` stays canonical (`deepseek-v4-pro`). The picker must
        // still resolve the canonical name to the prefixed adapter row so
        // the cursor lands on the active model — without this, the cursor
        // defaulted to the off-screen "custom" row and Enter became a no-op.
        let mut app = create_test_app();
        app.model = "deepseek-v4-pro".to_string();
        app.available_models = vec![crate::models::OwltrailModel {
            id: "deepseek-deepseek-v4-pro".to_string(),
            name: "DeepSeek V4 Pro".to_string(),
            context_window: Some(1_048_576),
            status: "OK".to_string(),
            triage_available: true,
        }];
        let view = ModelPickerView::new(&app);
        assert!(
            !view.show_custom_model_row,
            "active model should match an adapter row, not fall back to the custom row"
        );
        assert_eq!(view.resolved_model(), "deepseek-deepseek-v4-pro");
    }

    #[test]
    fn picker_preserves_unknown_model_via_custom_row() {
        let mut app = create_test_app();
        app.model = "deepseek-v4-pro-2026-04-XX".to_string();
        let view = ModelPickerView::new(&app);
        assert!(view.show_custom_model_row);
        assert_eq!(view.resolved_model(), "deepseek-v4-pro-2026-04-XX");
    }

    #[test]
    fn arrow_keys_move_within_focused_pane() {
        let app = create_test_app();
        let mut view = ModelPickerView::new(&app);
        // Default focus is Model; move down then up.
        let initial = view.selected_model_idx;
        view.handle_key(KeyEvent::new(
            KeyCode::Down,
            crossterm::event::KeyModifiers::NONE,
        ));
        assert_eq!(view.selected_model_idx, initial + 1);
        view.handle_key(KeyEvent::new(
            KeyCode::Up,
            crossterm::event::KeyModifiers::NONE,
        ));
        assert_eq!(view.selected_model_idx, initial);
    }

    #[test]
    fn tab_switches_focus_and_arrow_now_moves_effort() {
        let mut app = create_test_app();
        // Default is Max; pin to Off so the Down arrow has
        // somewhere to go.
        app.reasoning_effort = ReasoningEffort::Off;
        let mut view = ModelPickerView::new(&app);
        let initial_effort_idx = view.selected_effort_idx;
        view.handle_key(KeyEvent::new(
            KeyCode::Tab,
            crossterm::event::KeyModifiers::NONE,
        ));
        assert_eq!(view.focus, Pane::Effort);
        view.handle_key(KeyEvent::new(
            KeyCode::Down,
            crossterm::event::KeyModifiers::NONE,
        ));
        assert!(view.selected_effort_idx > initial_effort_idx);
    }

    #[test]
    fn enter_emits_apply_event_with_selection() {
        let mut app = create_test_app();
        app.reasoning_effort = ReasoningEffort::High;
        let mut view = ModelPickerView::new(&app);
        view.handle_key(KeyEvent::new(
            KeyCode::Tab,
            crossterm::event::KeyModifiers::NONE,
        ));
        view.handle_key(KeyEvent::new(
            KeyCode::Down,
            crossterm::event::KeyModifiers::NONE,
        ));
        let action = view.handle_key(KeyEvent::new(
            KeyCode::Enter,
            crossterm::event::KeyModifiers::NONE,
        ));
        match action {
            ViewAction::EmitAndClose(ViewEvent::ModelPickerApplied {
                model,
                effort,
                previous_effort,
                ..
            }) => {
                assert_eq!(model, "deepseek-v4-pro");
                assert_eq!(effort, ReasoningEffort::Max);
                assert_eq!(previous_effort, ReasoningEffort::High);
            }
            other => panic!("expected ModelPickerApplied EmitAndClose, got {other:?}"),
        }
    }

    #[test]
    fn esc_closes_without_emitting() {
        let app = create_test_app();
        let mut view = ModelPickerView::new(&app);
        let action = view.handle_key(KeyEvent::new(
            KeyCode::Esc,
            crossterm::event::KeyModifiers::NONE,
        ));
        assert!(matches!(action, ViewAction::Close));
    }

    #[test]
    fn picker_only_exposes_auto_off_high_max() {
        let labels: Vec<&str> = PICKER_EFFORTS
            .iter()
            .map(|effort| effort.short_label())
            .collect();
        assert_eq!(labels, vec!["auto", "off", "high", "max"]);
    }
}
