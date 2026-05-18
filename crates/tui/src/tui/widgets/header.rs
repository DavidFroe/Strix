//! Header bar widget displaying mode, workspace/model context, and session status.

use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Widget},
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::palette;
use crate::tui::app::AppMode;

use super::Renderable;

const CONTEXT_WARNING_THRESHOLD_PERCENT: f64 = 85.0;
const CONTEXT_CRITICAL_THRESHOLD_PERCENT: f64 = 95.0;
const CONTEXT_SIGNAL_WIDTH: usize = 4;

/// Data required to render the header bar.
pub struct HeaderData<'a> {
    pub model: &'a str,
    /// Optional sub-agent / background model label. When `Some`, the header
    /// renders `M:<main>  S:<sub>` (or a compact variant) instead of the
    /// single-model layout.
    pub subagent_model: Option<&'a str>,
    pub workspace_name: &'a str,
    pub mode: AppMode,
    pub is_streaming: bool,
    pub background: ratatui::style::Color,
    /// Total tokens used in this session (cumulative, for display).
    pub total_tokens: u32,
    /// Context window size for the model (if known).
    pub context_window: Option<u32>,
    /// Accumulated session cost in the active display currency.
    pub session_cost: f64,
    /// Active context input tokens used for context utilization. Callers should
    /// pass a sanitized live-context estimate, not cumulative API usage.
    pub last_prompt_tokens: Option<u32>,
    /// Short label for the current reasoning-effort tier (e.g. "max", "high",
    /// "off"). Rendered as a chip when space allows.
    pub reasoning_effort_label: Option<&'a str>,
    /// Short label for the active provider (e.g. "NIM"). When `None` (the
    /// default-DeepSeek case), no provider chip is rendered. Surfaces the
    /// fact that requests are going somewhere other than DeepSeek's API so
    /// it's visible at a glance after a `/provider nvidia-nim`.
    pub provider_label: Option<&'a str>,
}

impl<'a> HeaderData<'a> {
    /// Create header data from common app fields.
    #[must_use]
    pub fn new(
        mode: AppMode,
        model: &'a str,
        workspace_name: &'a str,
        is_streaming: bool,
        background: ratatui::style::Color,
    ) -> Self {
        Self {
            model,
            subagent_model: None,
            workspace_name,
            mode,
            is_streaming,
            background,
            total_tokens: 0,
            context_window: None,
            session_cost: 0.0,
            last_prompt_tokens: None,
            reasoning_effort_label: None,
            provider_label: None,
        }
    }

    /// Attach a sub-agent model label so the header surfaces both the main
    /// and the sub-agent model in the metadata cluster.
    #[must_use]
    pub fn with_subagent_model(mut self, label: Option<&'a str>) -> Self {
        self.subagent_model = label;
        self
    }

    /// Attach a short reasoning-effort label for the header chip.
    #[must_use]
    pub fn with_reasoning_effort(mut self, label: Option<&'a str>) -> Self {
        self.reasoning_effort_label = label;
        self
    }

    /// Attach a short provider label for the header chip. Pass `None` when on
    /// the default DeepSeek provider so the chip is hidden.
    #[must_use]
    pub fn with_provider(mut self, label: Option<&'a str>) -> Self {
        self.provider_label = label;
        self
    }

    /// Set token/cost fields.
    #[must_use]
    pub fn with_usage(
        mut self,
        total_tokens: u32,
        context_window: Option<u32>,
        session_cost: f64,
        active_context_input_tokens: Option<u32>,
    ) -> Self {
        self.total_tokens = total_tokens;
        self.context_window = context_window;
        self.session_cost = session_cost;
        self.last_prompt_tokens = active_context_input_tokens;
        self
    }
}

/// Header bar widget (1 line height).
pub struct HeaderWidget<'a> {
    data: HeaderData<'a>,
}

impl<'a> HeaderWidget<'a> {
    #[must_use]
    pub fn new(data: HeaderData<'a>) -> Self {
        Self { data }
    }

    fn mode_color(mode: AppMode) -> Color {
        match mode {
            AppMode::Chat => palette::MODE_CHAT,
            AppMode::Plan => palette::MODE_PLAN,
            AppMode::Agent => palette::MODE_AGENT,
            AppMode::Shell => palette::MODE_SHELL,
            AppMode::Yolo => palette::MODE_YOLO,
            AppMode::Propeller => palette::MODE_PROPELLER,
        }
    }

    fn mode_name(mode: AppMode) -> &'static str {
        // Strix v<ver> wird IMMER vorangestellt damit die Identität in jedem
        // Modus sichtbar ist. AppMode::Propeller bleibt im Code-Enum (alter
        // Übermoodus mit auto-approve + max-reasoning) — User-facing heißt
        // er jetzt "Auto" (siehe auch footer.rs).
        match mode {
            AppMode::Chat => concat!("Strix v", env!("CARGO_PKG_VERSION"), "  Chat"),
            AppMode::Plan => concat!("Strix v", env!("CARGO_PKG_VERSION"), "  Plan"),
            AppMode::Agent => concat!("Strix v", env!("CARGO_PKG_VERSION"), "  Agent"),
            AppMode::Shell => concat!("Strix v", env!("CARGO_PKG_VERSION"), "  Shell"),
            AppMode::Yolo => concat!("Strix v", env!("CARGO_PKG_VERSION"), "  Yolo"),
            AppMode::Propeller => concat!("Strix v", env!("CARGO_PKG_VERSION"), "  Auto"),
        }
    }

    fn span_width(spans: &[Span<'_>]) -> usize {
        spans.iter().map(|span| span.content.width()).sum()
    }

    fn truncate_to_width(text: &str, max_width: usize) -> String {
        const ELLIPSIS: &str = "...";
        let ellipsis_width = ELLIPSIS.width();

        if text.width() <= max_width {
            return text.to_string();
        }
        if max_width == 0 {
            return String::new();
        }
        if max_width <= ellipsis_width {
            return ".".repeat(max_width);
        }

        let mut truncated = String::new();
        let mut width = 0;
        for ch in text.chars() {
            let ch_width = ch.width().unwrap_or(0);
            if width + ch_width + ellipsis_width > max_width {
                break;
            }
            truncated.push(ch);
            width += ch_width;
        }
        truncated.push_str(ELLIPSIS);
        truncated
    }

    fn context_percent(&self) -> Option<f64> {
        let used = f64::from(self.data.last_prompt_tokens?);
        let max = f64::from(self.data.context_window?);
        if max <= 0.0 {
            return None;
        }
        Some((used / max * 100.0).clamp(0.0, 100.0))
    }

    fn context_color(percent: f64) -> Color {
        if percent >= CONTEXT_CRITICAL_THRESHOLD_PERCENT {
            palette::STATUS_ERROR
        } else if percent >= CONTEXT_WARNING_THRESHOLD_PERCENT {
            palette::STATUS_WARNING
        } else {
            palette::accent_light()
        }
    }

    fn context_signal_spans(&self, show_percent: bool) -> Vec<Span<'static>> {
        let Some(percent) = self.context_percent() else {
            return Vec::new();
        };

        let color = Self::context_color(percent);
        let filled = ((percent / 100.0) * CONTEXT_SIGNAL_WIDTH as f64)
            .ceil()
            .clamp(0.0, CONTEXT_SIGNAL_WIDTH as f64) as usize;
        let empty = CONTEXT_SIGNAL_WIDTH.saturating_sub(filled);

        let mut spans = Vec::new();
        if show_percent {
            spans.push(Span::styled(
                format!("{percent:.0}%"),
                Style::default().fg(color),
            ));
            spans.push(Span::raw(" "));
        }
        spans.push(Span::styled("▰".repeat(filled), Style::default().fg(color)));
        spans.push(Span::styled(
            "▱".repeat(empty),
            Style::default().fg(palette::accent_border()),
        ));
        spans
    }

    fn context_percent_spans(&self) -> Vec<Span<'static>> {
        let Some(percent) = self.context_percent() else {
            return Vec::new();
        };

        vec![Span::styled(
            format!("{percent:.0}%"),
            Style::default().fg(Self::context_color(percent)),
        )]
    }

    fn provider_chip_spans(&self) -> Vec<Span<'static>> {
        let Some(label) = self.data.provider_label else {
            return Vec::new();
        };
        let trimmed = label.trim();
        if trimmed.is_empty() {
            return Vec::new();
        }
        vec![Span::styled(
            trimmed.to_string(),
            Style::default()
                .fg(palette::accent_light())
                .add_modifier(Modifier::BOLD),
        )]
    }

    fn effort_chip_spans(&self, include_prefix: bool) -> Vec<Span<'static>> {
        let Some(label) = self.data.reasoning_effort_label else {
            return Vec::new();
        };
        let trimmed = label.trim();
        if trimmed.is_empty() {
            return Vec::new();
        }
        let is_off = trimmed.eq_ignore_ascii_case("off");
        let color = if is_off {
            palette::TEXT_HINT
        } else {
            palette::accent_light()
        };
        let body = if !include_prefix {
            trimmed.to_string()
        } else if trimmed.eq_ignore_ascii_case("max") || trimmed.eq_ignore_ascii_case("maximum") {
            format!("\u{1F300} {trimmed}")
        } else {
            format!("\u{00B7} {trimmed}")
        };
        vec![Span::styled(body, Style::default().fg(color))]
    }

    fn status_variant(
        &self,
        show_stream_label: bool,
        show_percent: bool,
        show_signal: bool,
    ) -> Vec<Span<'static>> {
        let mut spans = Vec::new();

        let provider_spans = self.provider_chip_spans();
        let has_provider = !provider_spans.is_empty();
        if has_provider {
            spans.extend(provider_spans);
        }

        let effort_spans = self.effort_chip_spans(true);
        let has_effort = !effort_spans.is_empty();
        if has_effort {
            if has_provider {
                spans.push(Span::raw("  "));
            }
            spans.extend(effort_spans);
        }

        if self.data.is_streaming {
            if has_effort || has_provider {
                spans.push(Span::raw("  "));
            }
            spans.push(Span::styled(
                "●",
                Style::default()
                    .fg(palette::accent_light())
                    .add_modifier(Modifier::BOLD),
            ));
            if show_stream_label {
                spans.push(Span::raw(" "));
                spans.push(Span::styled(
                    "Live",
                    Style::default().fg(palette::TEXT_SOFT),
                ));
            }
        }

        let context_spans = if show_signal {
            self.context_signal_spans(show_percent)
        } else if show_percent {
            self.context_percent_spans()
        } else {
            Vec::new()
        };
        if !context_spans.is_empty() {
            if !spans.is_empty() {
                spans.push(Span::raw("  "));
            }
            spans.extend(context_spans);
        }

        spans
    }

    fn right_spans(&self, max_width: usize) -> Vec<Span<'static>> {
        let candidates = [
            self.status_variant(true, true, true),
            self.status_variant(false, true, true),
            self.status_variant(false, true, false),
            self.status_variant(false, false, true),
        ];

        candidates
            .into_iter()
            .find(|spans| Self::span_width(spans) <= max_width)
            .unwrap_or_default()
    }

    fn model_pair_spans(&self, max_width: usize) -> Vec<Span<'static>> {
        let main = self.data.model.trim();
        let sub_raw = self.data.subagent_model.unwrap_or("").trim();
        let has_sub = !sub_raw.is_empty() && !sub_raw.eq_ignore_ascii_case(main);

        if !has_sub {
            if main.is_empty() {
                return Vec::new();
            }
            return vec![Span::styled(
                Self::truncate_to_width(main, max_width),
                Style::default().fg(palette::TEXT_HINT),
            )];
        }

        // Long form: `M:<main>  S:<sub>` — kept compact (no spaces around
        // the colon) so the existing context-signal cluster on the right
        // still fits on typical terminal widths.
        let prefix_long = ("M:", "  S:");
        let long = format!(
            "{prefix}{main}{sep}{sub}",
            prefix = prefix_long.0,
            main = main,
            sep = prefix_long.1,
            sub = sub_raw
        );
        if long.width() <= max_width {
            return vec![
                Span::styled("M:", Style::default().fg(palette::TEXT_HINT)),
                Span::styled(main.to_string(), Style::default().fg(palette::TEXT_HINT)),
                Span::raw("  "),
                Span::styled("S:", Style::default().fg(palette::TEXT_HINT)),
                Span::styled(sub_raw.to_string(), Style::default().fg(palette::TEXT_HINT)),
            ];
        }

        // Tight: budget the two model labels proportionally to their widths.
        // Reserves 2 chars each for the `M:` and `S:` prefixes and 2 for the
        // double-space separator.
        let chrome_width = 2 + 2 + 2; // M:, S:, spacer
        let content_budget = max_width.saturating_sub(chrome_width);
        if content_budget < 6 {
            // Too tight for both — just show the main label as a fallback.
            return vec![Span::styled(
                Self::truncate_to_width(main, max_width),
                Style::default().fg(palette::TEXT_HINT),
            )];
        }
        let main_w = main.width().max(1);
        let sub_w = sub_raw.width().max(1);
        let total = main_w + sub_w;
        let proportional_main =
            ((content_budget as f64 * main_w as f64) / total as f64).round() as usize;
        let main_budget = proportional_main.clamp(3, content_budget.saturating_sub(3));
        let sub_budget = content_budget.saturating_sub(main_budget);

        vec![
            Span::styled("M:", Style::default().fg(palette::TEXT_HINT)),
            Span::styled(
                Self::truncate_to_width(main, main_budget),
                Style::default().fg(palette::TEXT_HINT),
            ),
            Span::raw("  "),
            Span::styled("S:", Style::default().fg(palette::TEXT_HINT)),
            Span::styled(
                Self::truncate_to_width(sub_raw, sub_budget),
                Style::default().fg(palette::TEXT_HINT),
            ),
        ]
    }

    fn metadata_spans(&self, max_width: usize) -> Vec<Span<'static>> {
        // Strix: pwd/workspace wird im Info-Kasten angezeigt — im Header doppelt
        // sich das. Außerdem war die Anzeige modus-abhängig inkonsistent
        // (manche Mode-Kombinationen zeigten sie, andere nicht). Komplett raus.
        let workspace = "";
        let model = self.data.model.trim();
        let sub = self
            .data
            .subagent_model
            .map(str::trim)
            .filter(|s| !s.is_empty() && !s.eq_ignore_ascii_case(model));

        if max_width < 4 || (workspace.is_empty() && model.is_empty()) {
            return Vec::new();
        }

        // When a sub-agent model is configured, render workspace ALONGSIDE the
        // M/S model pair if there's room (`<ws> · M:<main> S:<sub>`). Falls
        // back to model-pair-only when the workspace path is too long to fit.
        // Keeps the CWD visible — earlier versions hid it entirely whenever
        // a sub-model was set, which made it easy to forget where propeller
        // was running.
        if sub.is_some() {
            let pair = self.model_pair_spans(max_width);
            let pair_width: usize = pair.iter().map(|s| s.content.width()).sum();
            let separator_width = 3; // " · "
            if !workspace.is_empty()
                && pair_width > 0
                && workspace.width() + separator_width + pair_width <= max_width
            {
                let mut spans = vec![
                    Span::styled(
                        workspace.to_string(),
                        Style::default().fg(palette::TEXT_SECONDARY),
                    ),
                    Span::styled(" · ", Style::default().fg(palette::TEXT_HINT)),
                ];
                spans.extend(pair);
                return spans;
            }
            return pair;
        }

        if workspace.is_empty() {
            return vec![Span::styled(
                Self::truncate_to_width(model, max_width),
                Style::default().fg(palette::TEXT_HINT),
            )];
        }

        if model.is_empty() || max_width < 12 {
            return vec![Span::styled(
                Self::truncate_to_width(workspace, max_width),
                Style::default().fg(palette::TEXT_SECONDARY),
            )];
        }

        let separator_width = 3; // " · "
        if workspace.width() + separator_width + model.width() <= max_width {
            return vec![
                Span::styled(
                    workspace.to_string(),
                    Style::default().fg(palette::TEXT_SECONDARY),
                ),
                Span::styled(" · ", Style::default().fg(palette::TEXT_HINT)),
                Span::styled(model.to_string(), Style::default().fg(palette::TEXT_HINT)),
            ];
        }

        let content_width = max_width.saturating_sub(separator_width);
        if content_width < 9 {
            return vec![Span::styled(
                Self::truncate_to_width(workspace, max_width),
                Style::default().fg(palette::TEXT_SECONDARY),
            )];
        }

        let workspace_width = workspace.width();
        let model_width = model.width();
        let total_width = workspace_width + model_width;
        let min_workspace = 4;
        let min_model = 4;

        let proportional_workspace =
            ((content_width as f64 * workspace_width as f64) / total_width as f64).round() as usize;
        let workspace_budget =
            proportional_workspace.clamp(min_workspace, content_width.saturating_sub(min_model));
        let model_budget = content_width.saturating_sub(workspace_budget);

        vec![
            Span::styled(
                Self::truncate_to_width(workspace, workspace_budget),
                Style::default().fg(palette::TEXT_SECONDARY),
            ),
            Span::styled(" · ", Style::default().fg(palette::TEXT_HINT)),
            Span::styled(
                Self::truncate_to_width(model, model_budget),
                Style::default().fg(palette::TEXT_HINT),
            ),
        ]
    }

    fn left_spans(&self, max_width: usize) -> Vec<Span<'static>> {
        if max_width == 0 {
            return Vec::new();
        }

        let mode_label = Self::mode_name(self.data.mode);
        let mode_style = Style::default()
            .fg(Self::mode_color(self.data.mode))
            .add_modifier(Modifier::BOLD);

        if max_width < mode_label.width() {
            let fallback = self
                .data
                .mode
                .label()
                .chars()
                .next()
                .unwrap_or('?')
                .to_string();
            return vec![Span::styled(fallback, mode_style)];
        }

        let mut spans = vec![Span::styled(mode_label.to_string(), mode_style)];
        let metadata_width = max_width
            .saturating_sub(mode_label.width())
            .saturating_sub(2);
        let metadata = if metadata_width >= 4 {
            self.metadata_spans(metadata_width)
        } else {
            Vec::new()
        };

        if !metadata.is_empty() {
            spans.push(Span::raw("  "));
            spans.extend(metadata);
        }

        spans
    }
}

impl Renderable for HeaderWidget<'_> {
    fn render(&self, area: Rect, buf: &mut Buffer) {
        if area.height == 0 || area.width == 0 {
            return;
        }

        let available = area.width as usize;
        let right_budget = available.saturating_sub(6);
        let right_spans = self.right_spans(right_budget);
        let right_width = Self::span_width(&right_spans);
        let spacer_min = usize::from(right_width > 0);
        let left_budget = available.saturating_sub(right_width + spacer_min);
        let left_spans = self.left_spans(left_budget);
        let left_width = Self::span_width(&left_spans);
        let spacer_width = available.saturating_sub(left_width + right_width);

        let mut spans = left_spans;
        if spacer_width > 0 {
            spans.push(Span::raw(" ".repeat(spacer_width)));
        }
        spans.extend(right_spans);

        let line = Line::from(spans);
        let paragraph = Paragraph::new(line).style(Style::default().bg(self.data.background));
        paragraph.render(area, buf);
    }

    fn desired_height(&self, _width: u16) -> u16 {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::{HeaderData, HeaderWidget, Renderable};
    use crate::palette;
    use crate::tui::app::AppMode;
    use ratatui::{buffer::Buffer, layout::Rect};

    fn render_header(data: HeaderData<'_>, width: u16) -> String {
        let widget = HeaderWidget::new(data);
        let area = Rect::new(0, 0, width, 1);
        let mut buf = Buffer::empty(area);
        widget.render(area, &mut buf);

        (0..width).map(|x| buf[(x, 0)].symbol()).collect::<String>()
    }

    #[test]
    fn wide_header_shows_plain_mode_and_single_metadata_cluster() {
        let rendered = render_header(
            HeaderData::new(
                AppMode::Agent,
                "deepseek-v4-pro",
                "deepseek-tui",
                false,
                palette::DEEPSEEK_INK,
            ),
            72,
        );

        assert!(rendered.contains("Agent"));
        assert!(rendered.contains("deepseek-tui"));
        assert!(rendered.contains("deepseek-v4-pro"));
        assert!(!rendered.contains("Plan"));
        assert!(!rendered.contains("Yolo"));
    }

    #[test]
    fn streaming_header_integrates_live_state_with_context_signal() {
        let rendered = render_header(
            HeaderData::new(
                AppMode::Plan,
                "deepseek-v4-pro",
                "workspace",
                true,
                palette::DEEPSEEK_INK,
            )
            .with_usage(42_000, Some(128_000), 0.0, Some(48_000)),
            72,
        );

        assert!(rendered.contains("Live"));
        assert!(rendered.contains("38%"));
        assert!(rendered.contains("▰"));
    }

    #[test]
    fn narrow_header_keeps_context_percent_visible() {
        let rendered = render_header(
            HeaderData::new(AppMode::Agent, "", "", true, palette::DEEPSEEK_INK).with_usage(
                0,
                Some(128_000),
                0.0,
                Some(48_000),
            ),
            14,
        );

        assert!(rendered.contains('%'));
    }

    #[test]
    fn narrow_header_falls_back_to_mode_without_rendering_all_modes() {
        let rendered = render_header(
            HeaderData::new(
                AppMode::Yolo,
                "deepseek-v4-flash",
                "repo",
                true,
                palette::DEEPSEEK_INK,
            )
            .with_usage(1_000, Some(10_000), 0.0, Some(4_000)),
            8,
        );

        assert!(rendered.trim_start().starts_with('Y'));
        assert!(!rendered.contains("Plan"));
        assert!(!rendered.contains("Agent"));
    }

    #[test]
    fn header_hides_context_signal_when_usage_snapshot_is_missing() {
        let rendered = render_header(
            HeaderData::new(
                AppMode::Agent,
                "deepseek-v4-flash",
                "repo",
                false,
                palette::DEEPSEEK_INK,
            ),
            48,
        );

        assert!(!rendered.contains('%'));
        assert!(!rendered.contains("▰"));
    }

    #[test]
    fn header_caps_context_signal_at_hundred_percent() {
        let rendered = render_header(
            HeaderData::new(
                AppMode::Agent,
                "deepseek-v4-flash",
                "repo",
                false,
                palette::DEEPSEEK_INK,
            )
            .with_usage(1_000, Some(128_000), 0.0, Some(320_000)),
            48,
        );

        assert!(rendered.contains("100%"));
        assert!(!rendered.contains("250%"));
    }

    #[test]
    fn header_shows_provider_chip_when_set() {
        let rendered = render_header(
            HeaderData::new(
                AppMode::Agent,
                "deepseek-ai/deepseek-v4-flash",
                "deepseek-tui",
                false,
                palette::DEEPSEEK_INK,
            )
            .with_provider(Some("NIM")),
            72,
        );
        assert!(
            rendered.contains("NIM"),
            "expected NIM chip in header, got: {rendered}"
        );
    }

    #[test]
    fn header_hides_provider_chip_when_default_deepseek() {
        let rendered = render_header(
            HeaderData::new(
                AppMode::Agent,
                "deepseek-v4-pro",
                "deepseek-tui",
                false,
                palette::DEEPSEEK_INK,
            ),
            72,
        );
        // Sanity: no `NIM` text leaks in when provider is None.
        assert!(!rendered.contains("NIM"));
    }
}
