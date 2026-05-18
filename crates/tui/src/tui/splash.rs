use ratatui::{buffer::Buffer, layout::Rect, style::{Color, Style}, widgets::{Clear, Widget}};
use std::time::{Duration, Instant};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const BUILD_NUMBER_STR: &str = env!("STRIX_BUILD_NUMBER");

const SPLASH_DURATION: Duration = Duration::from_secs(3);
/// Minimum time the splash is shown before a keypress can dismiss it.
const SPLASH_MIN_MS: u128 = 600;

// ASCII "STRIX" — ANSI Shadow Stil mit Pyramiden-Strichen links + rechts:
// dünn → mittel → dick (heavy) zur Mitte → mittel → dünn. Sci-fi-Auflockerung,
// nicht plumpe Strich-Wiederholung. Width = 52 cols (4+4 + 36 + 4+4).
const LOGO_LINES: &[&str] = &[
    "─       ███████╗████████╗██████╗ ██╗██╗  ██╗       ─",
    "──      ██╔════╝╚══██╔══╝██╔══██╗██║╚██╗██╔╝      ──",
    "━━━━    ███████╗   ██║   ██████╔╝██║ ╚███╔╝    ━━━━",
    "━━━━    ╚════██║   ██║   ██╔══██╗██║ ██╔██╗    ━━━━",
    "──      ███████║   ██║   ██║  ██║██║██╔╝ ██╗      ──",
    "─       ╚══════╝   ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝  ╚═╝       ─",
];

// Owl pixel-art (Strix-Mascot). Klare Augen (◉) + Schnabel (▼).
// Compact 7-row design um in den verfügbaren Splash-Slot zu passen.
const OWL_LINES: &[&str] = &[
    "     ▄▄█▀███▀█▄▄     ",
    "    █ ◉ █ █ ◉ █    ",
    "    ███▀███▀███    ",
    "    ████ ▼ ████    ",
    "    ╚███▄▄▄███╝    ",
    "     ╚███████╝     ",
    "      ▀▀▀▀▀      ",
];

// ── Propeller geometry ────────────────────────────────────────────────────────
// Terminal chars are ≈2× taller than wide. To make the rotation look circular
// rather than elliptic we use:
//   horizontal blade radius  rx = 9 columns
//   vertical   blade radius  ry = 4 rows  (≈ rx / 2.25)
// That gives equal "display-distance" to the blade tip at every angle, so a
// blade sweeping 120° of rotation travels the same arc length on-screen
// regardless of whether it's horizontal or vertical.
const PROP_RX: f64 = 9.0;
const PROP_RY: f64 = 4.0;
// Bounding box occupied by the propeller in the layout:
const PROP_BOX_W: u16 = 19; // 2 * 9 + 1  (centre col plus ±9 cols)
const PROP_BOX_H: u16 = 9;  // 2 * 4 + 1  (centre row plus ±4 rows)

// Rotation: one full turn in 2 s → clear wind-turbine pace, easy to follow.
const ROT_PERIOD_MS: f64 = 2000.0;

/// Incremental update sent from the inference background thread.
#[derive(Debug)]
pub enum InferenceUpdate {
    /// New attempt begins. `phase_label` is shown verbatim under the propeller
    /// (e.g. "35B-MoE quote" or "27B-Dense reasoning=max"). `attempt` is
    /// 1-indexed. The splash uses this to drive the per-phase timer reset.
    AttemptStarted { phase_label: String, attempt: u32 },
    /// Flash model finished — quote ready, comment still pending.
    QuoteReady(String, String),
    /// Pro/reasoning model + optional correction pass finished.
    /// `corrected_quote`/`corrected_attribution` override the QuoteReady values
    /// when a spelling/grammar correction step improved them.
    CommentReady {
        greeting: String,
        corrected_quote: Option<String>,
        corrected_attribution: Option<String>,
    },
    /// A stage failed.
    Failed(String),
}

/// Result of the startup inference test shown during the splash screen.
#[derive(Debug, Clone)]
pub enum InferenceState {
    /// Request not yet complete.
    Pending,
    /// Quote delivered; waiting for Pro model comment (spinner shown).
    QuoteReady {
        quote: String,
        attribution: String,
    },
    /// Both models answered successfully.
    Success {
        /// The quote line, e.g. `"Ich kam, sah und siegte"`
        quote: String,
        /// Attribution, e.g. `"Julius Caesar"`
        attribution: String,
        /// One-liner greeting from the Pro model.
        greeting: String,
    },
    /// Request failed — reason shown in red.
    Failed(String),
}

pub struct SplashScreen {
    shown_at: Instant,
    dismissed: bool,
    /// Moment when Success state was reached (for auto-dismiss).
    success_at: Option<Instant>,
    /// State of the startup model-test request.
    pub inference: InferenceState,
    /// Label of the currently-running attempt (e.g. "35B-MoE quote").
    /// `None` before the first AttemptStarted update arrives.
    attempt_label: Option<String>,
    /// 1-indexed attempt counter for the current phase.
    attempt_n: u32,
    /// Wall-clock start of the current attempt — drives the live seconds counter.
    attempt_started_at: Option<Instant>,
    /// Moment when the comment phase failed *after* a quote was already shown.
    /// Keeps the splash visible briefly with an error marker so the user can see
    /// the 27B test was attempted, before auto-dismissing into the TUI.
    comment_failed_at: Option<Instant>,
}

impl SplashScreen {
    pub fn new() -> Self {
        Self {
            shown_at: Instant::now(),
            dismissed: false,
            success_at: None,
            attempt_label: None,
            attempt_n: 0,
            attempt_started_at: None,
            comment_failed_at: None,
            inference: InferenceState::Pending,
        }
    }

    /// Dismiss the splash early (keypress).
    /// Pending: quote not yet ready — keypress ignored, must wait.
    /// QuoteReady: user skips comment wait → TUI opens with "kein Kommentar".
    /// Success: already auto-dismissed; keypress is a no-op.
    /// Failed: dismiss after min guard.
    pub fn dismiss(&mut self) {
        match &self.inference {
            InferenceState::Pending => {
                // Phase 1: quote not yet ready — user cannot skip yet.
            }
            InferenceState::QuoteReady { .. } | InferenceState::Success { .. } => {
                self.dismissed = true;
            }
            InferenceState::Failed(_) => {
                if self.shown_at.elapsed().as_millis() >= SPLASH_MIN_MS {
                    self.dismissed = true;
                }
            }
        }
    }

    pub fn is_done(&self) -> bool {
        if self.dismissed { return true; }
        let elapsed = self.shown_at.elapsed();
        match &self.inference {
            // Phase 1: wait for quote (hard max 240 s — covers 35B cold-start from disk).
            InferenceState::Pending => elapsed >= Duration::from_secs(240),
            // Phase 2+3: wait for comment+correction (hard max 300 s — 27B cold-start + reasoning).
            // Normal exit is via CommentReady which sets dismissed=true immediately.
            // If the comment phase failed, keep the splash visible at least 8 s after
            // the failure so the user actually sees the 27B-test was attempted and
            // what went wrong (timer + error label remain on screen).
            InferenceState::QuoteReady { .. } => {
                if let Some(failed_at) = self.comment_failed_at {
                    failed_at.elapsed() >= Duration::from_secs(8)
                } else {
                    elapsed >= Duration::from_secs(300)
                }
            }
            // Success is set by CommentReady which also sets dismissed=true — never rendered.
            InferenceState::Success { .. } => true,
            // Failed: stay open briefly so the user can read the error.
            InferenceState::Failed(_) => elapsed >= SPLASH_DURATION,
        }
    }

    /// True when the splash has been dismissed (by keypress or auto-dismiss).
    pub fn is_dismissed(&self) -> bool {
        self.dismissed
    }

    /// True when inference failed — used by the main loop to decide whether to abort startup.
    pub fn inference_failed(&self) -> bool {
        matches!(&self.inference, InferenceState::Failed(_))
    }

    /// Live status line under the propeller: "<label>... · #N · Xs" mit
    /// 3-Punkt-Animation am Label-Ende (war zwischendurch verloren gegangen).
    /// `None` wenn kein Attempt läuft (zwischen Phasen / fertig).
    fn attempt_status_line(&self) -> Option<String> {
        let label = self.attempt_label.as_ref()?;
        let started = self.attempt_started_at?;
        let elapsed = started.elapsed();
        let secs = elapsed.as_secs();
        let dots = match (elapsed.as_millis() / 500) % 3 {
            0 => ".  ",
            1 => ".. ",
            _ => "...",
        };
        Some(format!("{label}{dots} · #{n} · {secs}s", n = self.attempt_n))
    }

    /// Apply an incremental update from the inference thread.
    pub fn apply_update(&mut self, update: InferenceUpdate) {
        match update {
            InferenceUpdate::AttemptStarted { phase_label, attempt } => {
                self.attempt_label = Some(phase_label);
                self.attempt_n = attempt;
                self.attempt_started_at = Some(Instant::now());
            }
            InferenceUpdate::QuoteReady(q, a) => {
                self.inference = InferenceState::QuoteReady { quote: q, attribution: a };
                // Comment phase will issue its own AttemptStarted; reset counter so
                // the timer doesn't briefly show the quote phase's elapsed time.
                self.attempt_label = None;
                self.attempt_n = 0;
                self.attempt_started_at = None;
            }
            InferenceUpdate::CommentReady { greeting, corrected_quote, corrected_attribution } => {
                if let InferenceState::QuoteReady { quote, attribution } = &self.inference {
                    self.inference = InferenceState::Success {
                        quote:       corrected_quote.unwrap_or_else(|| quote.clone()),
                        attribution: corrected_attribution.unwrap_or_else(|| attribution.clone()),
                        greeting,
                    };
                    self.success_at = Some(Instant::now());
                    // Immediately dismiss — comment is never shown in the splash itself,
                    // only in the TUI after opening.
                    self.dismissed = true;
                }
            }
            InferenceUpdate::Failed(e) => {
                // Soft-fail when a quote already arrived: keep the splash in
                // QuoteReady state so the user still sees the 27B test was attempted,
                // mark the attempt as failed in the timer line, and arm the 8 s
                // dismiss timer in is_done(). The full error is on stderr / in the
                // start log for the backend dev.
                if matches!(self.inference, InferenceState::QuoteReady { .. }) {
                    let short = e
                        .lines()
                        .find(|l| !l.trim().is_empty() && !l.starts_with('='))
                        .unwrap_or("Comment failed")
                        .trim()
                        .chars()
                        .take(80)
                        .collect::<String>();
                    self.attempt_label = Some(format!("\u{26a0} 27B-Test failed: {short}"));
                    // Freeze the timer at the failure moment by leaving attempt_started_at
                    // untouched — it keeps showing the duration the test ran for.
                    self.comment_failed_at = Some(Instant::now());
                } else {
                    self.inference = InferenceState::Failed(e);
                }
            }
        }
    }

    pub fn render(&self, area: Rect, buf: &mut Buffer) {
        // ── 1. Solid black background ─────────────────────────────────────────
        Clear.render(area, buf);
        let black = Style::default().fg(Color::Black).bg(Color::Black);
        for y in area.top()..area.bottom() {
            for x in area.left()..area.right() {
                if let Some(cell) = buf.cell_mut((x, y)) {
                    cell.set_char(' ').set_style(black);
                }
            }
        }

        let elapsed_ms = self.shown_at.elapsed().as_millis();

        // ── 2. Layout ─────────────────────────────────────────────────────────
        let logo_w = LOGO_LINES.iter().map(|l| l.width()).max().unwrap_or(0) as u16;
        let logo_h = LOGO_LINES.len() as u16;

        // Single label — never shifts position.
        let press_key = "[ Press any key ]";
        let pk_w_layout = press_key.width() as u16;

        // content_w is driven by fixed-size elements; text wraps TO this width.
        let owl_w = OWL_LINES.iter().map(|l| l.width()).max().unwrap_or(0) as u16;
        let content_w = logo_w.max(owl_w).max(pk_w_layout);

        // Retrieve raw text, then word-wrap the quote to content_w (max 4 lines).
        let (quote_line, attrib_line, base_greeting) = parse_quote_response(&self.inference, elapsed_ms);
        // Override the greeting/spinner line with the live attempt timer when one
        // is running — gives the user something to watch instead of a vague "denkt
        // darüber nach...". Falls back to the base greeting when between attempts.
        let greeting_line = self.attempt_status_line().or(base_greeting);
        let quote_wrapped: Vec<String> = quote_line.as_deref()
            .map(|q| word_wrap(q, content_w as usize))
            .unwrap_or_default()
            .into_iter()
            .take(4)
            .collect();

        // Dynamic content height: logo + blank + propeller + blank + text block + press_key.
        let q_rows  = quote_wrapped.len().max(if quote_line.is_some() { 1 } else { 0 }) as u16;
        let a_rows  = if attrib_line.is_some()   { 1u16 } else { 0 };
        let g_rows  = if greeting_line.is_some()  { 1u16 } else { 0 };
        // text block = quote + attrib + blank + greeting + blank (before press_key)
        let text_h  = q_rows + a_rows + 1 + g_rows + 1;
        let owl_h = OWL_LINES.len() as u16;
        let content_h = logo_h + 1 + owl_h + 1 + text_h + 1;

        let ox = area.x + area.width .saturating_sub(content_w) / 2;
        let oy = area.y + area.height.saturating_sub(content_h) / 2;
        let mut row = oy;

        // ── 3. PROPELLER logo — left→right blue gradient ──────────────────────
        for (li, logo_line) in LOGO_LINES.iter().enumerate() {
            if row >= area.bottom() { break; }
            // Write glyphs first (gives correct multi-byte rendering)
            buf.set_string(ox, row, logo_line,
                Style::default().fg(Color::White).bg(Color::Black));
            // Then recolour each cell with the gradient
            let lw = logo_line.width() as u16;
            let mut cx = ox;
            for ch in logo_line.chars() {
                let cw = UnicodeWidthChar::width(ch).unwrap_or(1) as u16;
                if cx >= area.right() { break; }
                let h = cx.saturating_sub(ox) as f64 / lw.max(1) as f64;
                let v = li as f64 / logo_h.max(1) as f64;
                let t = (0.75 * h + 0.25 * (1.0 - v)).clamp(0.0, 1.0);
                if let Some(cell) = buf.cell_mut((cx, row)) {
                    cell.set_fg(logo_gradient(t)).set_bg(Color::Black);
                }
                cx += cw;
            }
            row += 1;
        }

        // Version string — right-aligned under logo, in blank row before propeller
        let build_n: u64 = BUILD_NUMBER_STR.parse().unwrap_or(0);
        let ver_str = format!("v{VERSION}  Build #{build_n:03}");
        if row < area.bottom() {
            let vx = ox + content_w.saturating_sub(ver_str.len() as u16);
            buf.set_string(vx, row, &ver_str,
                Style::default().fg(Color::Indexed(240)).bg(Color::Black));
        }
        row += 1; // blank between logo and propeller

        // ── 4. Strix-Eule (statisches Pixel-Art) ──────────────────────────────
        // Die Eule blinzelt sanft (rechtes Auge alle paar Sekunden geschlossen)
        // — das ist die einzige Animation, kein Spinning mehr wie beim Propeller.
        let blink = (elapsed_ms / 100) % 30 == 0 && (elapsed_ms / 100) % 60 < 31;
        let _ = blink; // reserviert für spätere Animation; statisch reicht erstmal
        for (li, owl_line) in OWL_LINES.iter().enumerate() {
            if row >= area.bottom() { break; }
            let lw = owl_line.width() as u16;
            let ox_owl = ox + content_w.saturating_sub(lw) / 2;
            buf.set_string(ox_owl, row, owl_line,
                Style::default().fg(Color::White).bg(Color::Black));
            // Recolour pro Cell mit Theme-Gradient
            let mut cx = ox_owl;
            for ch in owl_line.chars() {
                let cw = UnicodeWidthChar::width(ch).unwrap_or(1) as u16;
                if cx >= area.right() { break; }
                let v = li as f64 / OWL_LINES.len().max(1) as f64;
                let color = if ch == '\u{25cf}' {
                    // Schnabel-Mittelpunkt: Akzent-Glow
                    crate::palette::accent_light()
                } else {
                    logo_gradient(0.4 + 0.4 * v)
                };
                if let Some(cell) = buf.cell_mut((cx, row)) {
                    cell.set_fg(color).set_bg(Color::Black);
                }
                cx += cw;
            }
            row += 1;
        }
        row += 1; // blank between owl and quote

        // ── 5. Quote (word-wrapped, up to 4 rows) + attribution + greeting ───
        let quote_color = match &self.inference {
            InferenceState::Failed(_)        => Color::Rgb(255, 80, 80),
            InferenceState::Pending          => Color::Indexed(244),
            InferenceState::QuoteReady { .. }
            | InferenceState::Success { .. } => Color::Rgb(220, 220, 180),
        };
        for qline in &quote_wrapped {
            if row >= area.bottom() { break; }
            let qw = qline.width() as u16;
            let qx = ox + content_w.saturating_sub(qw) / 2;
            buf.set_string(qx, row, qline, Style::default().fg(quote_color).bg(Color::Black));
            row += 1;
        }
        // If nothing to render (Pending with no quote yet), advance 1 row anyway.
        if quote_wrapped.is_empty() && quote_line.is_none() {
            row += 1;
        }

        // Attribution on its own row.
        if let Some(a) = &attrib_line {
            if row < area.bottom() {
                let aw = a.width() as u16;
                let ax = ox + content_w.saturating_sub(aw) / 2;
                buf.set_string(ax, row, a,
                    Style::default().fg(Color::Indexed(67)).bg(Color::Black));
            }
        }
        row += 1;
        row += 1; // blank between attribution and greeting/spinner

        // Greeting / spinner.
        if let Some(g) = &greeting_line {
            if row < area.bottom() {
                let gw = g.width() as u16;
                let gx = ox + content_w.saturating_sub(gw) / 2;
                let color = if matches!(&self.inference, InferenceState::QuoteReady { .. }) {
                    let pulse = (elapsed_ms % 900) as f64 / 900.0;
                    let alpha = (pulse * std::f64::consts::TAU).sin() * 0.5 + 0.5;
                    let bright = (100.0 + alpha * 80.0) as u8;
                    Color::Rgb(bright / 2, bright, bright)
                } else {
                    Color::Rgb(100, 210, 180)
                };
                buf.set_string(gx, row, g, Style::default().fg(color));
            }
        }
        row += 1;
        row += 1; // blank before press-key hint

        // ── 6. "Press any key" hint — only when waiting for comment or on failure ──
        // Pending: propeller-only, no hint needed.
        // QuoteReady: user can skip the comment wait.
        // Success: auto-dismisses, no hint needed.
        // Failed: user must press a key to continue.
        let show_press_key = matches!(&self.inference,
            InferenceState::QuoteReady { .. } | InferenceState::Failed(_));
        if row < area.bottom() {
            let can_dismiss = show_press_key && self.shown_at.elapsed().as_millis() >= SPLASH_MIN_MS;
            if can_dismiss {
                let pk_w = press_key.width() as u16;
                let px = ox + content_w.saturating_sub(pk_w) / 2;
                // Pulse: visible ~70% of the time using a 1.2 s cycle
                let pulse_t = (self.shown_at.elapsed().as_millis() % 1200) as f64 / 1200.0;
                let alpha = (pulse_t * std::f64::consts::TAU).sin() * 0.5 + 0.5;
                let brightness = (80.0 + alpha * 100.0) as u8;
                buf.set_string(px, row, press_key,
                    Style::default()
                        .fg(Color::Rgb(brightness, brightness, brightness + 40))
                        .bg(Color::Black));
            }
        }
    }
}

// ── Word-wrap helper ─────────────────────────────────────────────────────────

/// Wraps `text` to `max_w` terminal columns (Unicode-aware).
/// Returns one string per visual line, each ≤ `max_w` columns wide.
fn word_wrap(text: &str, max_w: usize) -> Vec<String> {
    if max_w == 0 { return vec![text.to_string()]; }
    let mut lines: Vec<String> = Vec::new();
    let mut current = String::new();
    let mut current_w = 0usize;

    for word in text.split_whitespace() {
        let word_w = word.width();
        if current_w == 0 {
            current.push_str(word);
            current_w = word_w;
        } else if current_w + 1 + word_w <= max_w {
            current.push(' ');
            current.push_str(word);
            current_w += 1 + word_w;
        } else {
            lines.push(std::mem::take(&mut current));
            current.push_str(word);
            current_w = word_w;
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    if lines.is_empty() && !text.is_empty() {
        lines.push(text.to_string());
    }
    lines
}

// ── Quote parser ─────────────────────────────────────────────────────────────

/// Returns (quote_line, attribution_line, greeting_or_spinner_line).
fn parse_quote_response(state: &InferenceState, elapsed_ms: u128) -> (Option<String>, Option<String>, Option<String>) {
    match state {
        InferenceState::Pending => {
            // Phase 1: propeller only — no text until quote is ready.
            (None, None, None)
        }
        InferenceState::QuoteReady { quote, attribution } => {
            let quote_str  = format!("\u{201e}{quote}\u{201c}");
            let attrib_str = format!("\u{2014} {attribution}");
            let dots = match (elapsed_ms / 500) % 3 {
                0 => ".  ",
                1 => ".. ",
                _ => "...",
            };
            let spinner = format!("Strix denkt dar\u{fc}ber nach{dots}");
            (Some(quote_str), Some(attrib_str), Some(spinner))
        }
        InferenceState::Failed(err) => {
            let short = err.lines().next().unwrap_or(err).trim();
            let msg = format!("\u{26a0}  Modell-Inferenz fehlgeschlagen: {short}");
            (Some(msg), Some("Tipp: strix model main <modelname>  |  strix models".to_string()), None)
        }
        InferenceState::Success { quote, attribution, greeting } => {
            let quote_str  = format!("\u{201e}{quote}\u{201c}");
            let attrib_str = format!("\u{2014} {attribution}");
            (Some(quote_str), Some(attrib_str), Some(greeting.clone()))
        }
    }
}

// ── Propeller renderer ────────────────────────────────────────────────────────

/// Render a mathematically correct 3-bladed propeller.
///
/// * `cx`, `cy`        — absolute centre position in the buffer.
/// * `rx`, `ry`        — horizontal / vertical blade radius (columns / rows).
/// * `angle_deg`       — current rotation angle in degrees.  0° means the
///   first blade points right; add –90° offset to start pointing up.
/// * `clip`            — buffer area to clip against.
fn render_propeller(
    buf:       &mut Buffer,
    cx:        u16,
    cy:        u16,
    rx:        f64,
    ry:        f64,
    angle_deg: f64,
    clip:      Rect,
) {
    // Draw the three blades from hub outward.
    // Each blade is at a multiple of 120° from `angle_deg`.
    for blade in 0..3_u32 {
        let a_deg = angle_deg + blade as f64 * 120.0;
        let a     = a_deg.to_radians();

        // Tip position in terminal cell coordinates.
        let tip_x = (cx as f64 + rx * a.cos()).round() as i32;
        let tip_y = (cy as f64 + ry * a.sin()).round() as i32;

        // Reference "display length" for this blade so that t=1 at the tip.
        let tdx = (tip_x - cx as i32) as f64;
        let tdy = (tip_y - cy as i32) as f64;
        // Account for aspect ratio when computing "display distance".
        let max_display = display_dist(tdx, tdy).max(1.0);

        for (px, py) in bresenham(cx as i32, cy as i32, tip_x, tip_y) {
            if px < 0 || py < 0 { continue; }
            let ux = px as u16;
            let uy = py as u16;
            if ux < clip.left() || ux >= clip.right()
                || uy < clip.top()  || uy >= clip.bottom() {
                continue;
            }

            // t = 0 at hub, 1 at tip — use display-corrected distance.
            let dx = (px - cx as i32) as f64;
            let dy = (py - cy as i32) as f64;
            let t  = (display_dist(dx, dy) / max_display).clamp(0.0, 1.0);

            // Character: solid near hub, fading toward tip.
            let ch = if t < 0.28 { '\u{2588}' }       // █
                else if t < 0.55 { '\u{2593}' }        // ▓
                else if t < 0.80 { '\u{2592}' }        // ▒
                else             { '\u{2591}' };        // ░

            // Colour: dark-blue at hub → bright-cyan at tip.
            let color = blade_color(t);

            if let Some(cell) = buf.cell_mut((ux, uy)) {
                cell.set_char(ch).set_fg(color).set_bg(Color::Black);
            }
        }
    }

    // Hub marker — drawn last so it always sits on top of all three blades.
    if cx >= clip.left() && cx < clip.right()
        && cy >= clip.top()  && cy < clip.bottom() {
        if let Some(cell) = buf.cell_mut((cx, cy)) {
            cell.set_char('\u{25cf}')           // ●
                .set_fg(Color::White)
                .set_bg(Color::Black);
        }
    }
}

/// "Display-space" distance between (0,0) and (dx, dy) in cell coordinates,
/// corrected for the ≈2:1 terminal character aspect ratio.
#[inline]
fn display_dist(dx: f64, dy: f64) -> f64 {
    // Each row is ≈2.25× the height of a column's width on screen.
    (dx * dx + (dy * 2.25) * (dy * 2.25)).sqrt()
}

/// Bresenham integer line — fills every cell between (x0,y0) and (x1,y1).
fn bresenham(x0: i32, y0: i32, x1: i32, y1: i32) -> Vec<(i32, i32)> {
    let mut pts = Vec::new();
    let dx = (x1 - x0).abs();
    let dy = (y1 - y0).abs();
    let sx: i32 = if x0 < x1 {  1 } else { -1 };
    let sy: i32 = if y0 < y1 {  1 } else { -1 };
    let mut err = dx - dy;
    let (mut x, mut y) = (x0, y0);
    loop {
        pts.push((x, y));
        if x == x1 && y == y1 { break; }
        let e2 = 2 * err;
        if e2 > -dy { err -= dy; x += sx; }
        if e2 <  dx { err += dx; y += sy; }
    }
    pts
}

// ── Colour helpers ────────────────────────────────────────────────────────────

/// Extract (r, g, b) from a `Color::Rgb` value; falls back to blue.
fn rgb_of(c: Color) -> (u8, u8, u8) {
    match c {
        Color::Rgb(r, g, b) => (r, g, b),
        _ => (53, 120, 229),
    }
}

/// Blade colour: dark at hub (t=0) → theme accent light at tip (t=1).
fn blade_color(t: f64) -> Color {
    let t = t.clamp(0.0, 1.0);
    let (pr, pg, pb) = rgb_of(crate::palette::accent());
    let (lr, lg, lb) = rgb_of(crate::palette::accent_light());
    Color::Rgb(
        lerp(lerp(pr as f64 * 0.12, pr as f64 * 0.7, t), lr as f64, t) as u8,
        lerp(lerp(pg as f64 * 0.12, pg as f64 * 0.7, t), lg as f64, t) as u8,
        lerp(lerp(pb as f64 * 0.12, pb as f64 * 0.7, t), lb as f64, t) as u8,
    )
}

/// Logo gradient: dark left/top → theme accent → theme accent-light right/bottom.
fn logo_gradient(t: f64) -> Color {
    let t = t.clamp(0.0, 1.0);
    let (pr, pg, pb) = rgb_of(crate::palette::accent());
    let (lr, lg, lb) = rgb_of(crate::palette::accent_light());
    let (r, g, b) = if t < 0.5 {
        let s = t * 2.0;
        (
            lerp(pr as f64 * 0.08, pr as f64 * 0.7, s),
            lerp(pg as f64 * 0.08, pg as f64 * 0.7, s),
            lerp(pb as f64 * 0.08, pb as f64 * 0.7, s),
        )
    } else {
        let s = (t - 0.5) * 2.0;
        (
            lerp(pr as f64 * 0.7, lr as f64, s),
            lerp(pg as f64 * 0.7, lg as f64, s),
            lerp(pb as f64 * 0.7, lb as f64, s),
        )
    };
    Color::Rgb(r as u8, g as u8, b as u8)
}

#[inline]
fn lerp(a: f64, b: f64, t: f64) -> f64 {
    a + (b - a) * t.clamp(0.0, 1.0)
}
