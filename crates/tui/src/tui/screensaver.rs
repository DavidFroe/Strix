use std::time::{Duration, Instant};

use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Clear, Paragraph};

const LOGO: &str = "\u{2699}  P R O P E L L E R";

const LOGO_COLORS: &[Color] = &[
    Color::Rgb(0, 210, 255),
    Color::Rgb(80, 255, 180),
    Color::Rgb(255, 200, 50),
    Color::Rgb(200, 80, 255),
    Color::Rgb(255, 80, 130),
    Color::Rgb(60, 255, 130),
];

// All particle colors are bright enough to read on a black background.
const PARTICLE_COLORS: &[Color] = &[
    Color::Rgb(0, 200, 255),   // cyan
    Color::Rgb(60, 240, 160),  // teal-green
    Color::Rgb(255, 165, 30),  // orange
    Color::Rgb(180, 80, 255),  // purple
    Color::Rgb(255, 80, 140),  // pink
    Color::Rgb(120, 255, 80),  // lime
];

const PARTICLE_LIFETIME: Duration = Duration::from_secs(18);
const TARGET_PARTICLES: usize = 4;

pub struct Particle {
    text: String,
    x: f64,
    y: f64,
    vx: f64,
    vy: f64,
    color_idx: usize,
    born: Instant,
}

pub struct Screensaver {
    pub particles: Vec<Particle>,
    logo_x: f64,
    logo_y: f64,
    logo_vx: f64,
    logo_vy: f64,
    logo_color_idx: usize,
    last_tick: Instant,
    quote_pool: Vec<String>,
    seed: u64,
}

impl Screensaver {
    pub fn new(quote_pool: Vec<String>) -> Self {
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(12345);
        Self {
            particles: Vec::new(),
            logo_x: 4.0,
            logo_y: 4.0,
            logo_vx: 14.0,
            logo_vy: 6.0,
            logo_color_idx: 0,
            last_tick: Instant::now(),
            quote_pool,
            seed,
        }
    }

    fn xorshift(&mut self) -> u64 {
        self.seed ^= self.seed << 13;
        self.seed ^= self.seed >> 7;
        self.seed ^= self.seed << 17;
        self.seed
    }

    fn rand_f(&mut self, min: f64, max: f64) -> f64 {
        let r = self.xorshift();
        min + (r as f64 / u64::MAX as f64) * (max - min)
    }

    fn pick_quote(&mut self) -> String {
        if self.quote_pool.is_empty() {
            return "Propellerastisch".to_string();
        }
        let idx = self.xorshift() as usize % self.quote_pool.len();
        self.quote_pool[idx].clone()
    }

    fn spawn_particle(&mut self, w: u16, h: u16) {
        let fw = w as f64;
        let fh = h as f64;
        let edge = self.xorshift() % 4;
        let (x, y, vx, vy) = match edge {
            0 => (self.rand_f(0.0, fw), 0.0,        self.rand_f(-3.0, 3.0), self.rand_f(1.5, 3.5)),
            1 => (self.rand_f(0.0, fw), fh - 1.0,   self.rand_f(-3.0, 3.0), self.rand_f(-3.5, -1.5)),
            2 => (0.0,                  self.rand_f(0.0, fh), self.rand_f(1.5, 4.0), self.rand_f(-2.0, 2.0)),
            _ => (fw,                   self.rand_f(0.0, fh), self.rand_f(-4.0, -1.5), self.rand_f(-2.0, 2.0)),
        };
        let color_idx = self.xorshift() as usize % PARTICLE_COLORS.len();
        let text = self.pick_quote();
        self.particles.push(Particle { text, x, y, vx, vy, color_idx, born: Instant::now() });
    }

    pub fn tick(&mut self, w: u16, h: u16) {
        let dt = self.last_tick.elapsed().as_secs_f64().min(0.1);
        self.last_tick = Instant::now();

        let fw = w as f64;
        let fh = h as f64;
        let logo_char_w = LOGO.chars().count() as f64;

        // ── DVD-bounce the logo ───────────────────────────────────────────
        self.logo_x += self.logo_vx * dt;
        self.logo_y += self.logo_vy * dt;

        let mut bounced = false;
        if self.logo_x <= 0.0 {
            self.logo_x = 0.0;
            self.logo_vx = self.logo_vx.abs();
            bounced = true;
        } else if self.logo_x + logo_char_w >= fw {
            self.logo_x = (fw - logo_char_w).max(0.0);
            self.logo_vx = -self.logo_vx.abs();
            bounced = true;
        }
        if self.logo_y <= 0.0 {
            self.logo_y = 0.0;
            self.logo_vy = self.logo_vy.abs();
            bounced = true;
        } else if self.logo_y >= fh - 1.0 {
            self.logo_y = (fh - 2.0).max(0.0);
            self.logo_vy = -self.logo_vy.abs();
            bounced = true;
        }
        if bounced {
            self.logo_color_idx = (self.logo_color_idx + 1) % LOGO_COLORS.len();
        }

        // ── Particle lifecycle ────────────────────────────────────────────
        let now = Instant::now();
        self.particles.retain(|p| {
            now.duration_since(p.born) < PARTICLE_LIFETIME
                && p.x > -80.0 && p.x < fw + 80.0
                && p.y > -5.0  && p.y < fh + 5.0
        });
        for p in &mut self.particles {
            p.x += p.vx * dt;
            p.y += p.vy * dt;
        }
        while self.particles.len() < TARGET_PARTICLES {
            self.spawn_particle(w, h);
        }
    }

    pub fn render(&self, frame: &mut Frame) {
        let area = frame.area();
        // Use the terminal's own default background — no explicit fill.
        frame.render_widget(Clear, area);

        // ── Particles ─────────────────────────────────────────────────────
        for p in &self.particles {
            let col = p.x as i32;
            let row = p.y as i32;
            if row < 0 || row >= area.height as i32 || col < 0 {
                continue;
            }
            let col = col as u16;
            let row = row as u16;
            let max_w = area.width.saturating_sub(col);
            if max_w == 0 {
                continue;
            }
            let display: String = p.text.chars()
                .scan(0u16, |used, ch| {
                    let cw = unicode_width::UnicodeWidthChar::width(ch).unwrap_or(1) as u16;
                    if *used + cw > max_w { return None; }
                    *used += cw;
                    Some(ch)
                })
                .collect();
            if display.is_empty() { continue; }
            let color = PARTICLE_COLORS[p.color_idx % PARTICLE_COLORS.len()];
            let rect = Rect::new(col, row, max_w.min(display.len() as u16 + 2), 1);
            frame.render_widget(
                Paragraph::new(Text::from(Line::from(Span::styled(
                    display,
                    Style::default().fg(color),
                )))),
                rect,
            );
        }

        // ── Bouncing logo ─────────────────────────────────────────────────
        let col = self.logo_x as i32;
        let row = self.logo_y as i32;
        if col >= 0 && row >= 0 && row < area.height as i32 {
            let col = col as u16;
            let row = row as u16;
            let max_w = area.width.saturating_sub(col);
            if max_w > 0 {
                let color = LOGO_COLORS[self.logo_color_idx % LOGO_COLORS.len()];
                let logo_w = (LOGO.chars().count() as u16).min(max_w);
                let rect = Rect::new(col, row, logo_w, 1);
                frame.render_widget(
                    Paragraph::new(Text::from(Line::from(Span::styled(
                        LOGO,
                        Style::default().fg(color).add_modifier(Modifier::BOLD),
                    )))),
                    rect,
                );
            }
        }
    }
}
