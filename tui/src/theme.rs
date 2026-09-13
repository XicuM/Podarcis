//! Catppuccin palettes mapped onto semantic tokens.
//!
//! Every colour in the app comes from here. Widgets never name a palette colour
//! directly, so re-flavouring is a single `Theme::new` call and the embedded
//! herdr pane can be handed the matching flavour name verbatim.

use catppuccin::PALETTE;
use ratatui::style::{Color, Modifier, Style};

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Flavor {
    #[default]
    Latte,
    Frappe,
    Macchiato,
    Mocha,
}

impl Flavor {
    /// Accepts both the bare flavour (`latte`) and herdr's spelling
    /// (`catppuccin-latte`), which is what lands in `config.yaml`.
    pub fn parse(name: &str) -> Option<Self> {
        let key = name.trim().to_ascii_lowercase();
        let key = key.strip_prefix("catppuccin-").unwrap_or(&key);
        match key {
            "latte" | "light" => Some(Self::Latte),
            "frappe" | "frappé" => Some(Self::Frappe),
            "macchiato" => Some(Self::Macchiato),
            "mocha" | "dark" => Some(Self::Mocha),
            _ => None,
        }
    }

    pub const ALL: [Flavor; 4] = [Flavor::Latte, Flavor::Frappe, Flavor::Macchiato, Flavor::Mocha];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Latte => "latte",
            Self::Frappe => "frappe",
            Self::Macchiato => "macchiato",
            Self::Mocha => "mocha",
        }
    }

    /// The `[theme] name` value the embedded herdr expects.
    pub fn herdr_name(self) -> String {
        format!("catppuccin-{}", self.as_str())
    }

    pub fn next(self) -> Self {
        let i = Self::ALL.iter().position(|f| *f == self).unwrap_or(0);
        Self::ALL[(i + 1) % Self::ALL.len()]
    }
}

/// Semantic colour tokens. Named by role, never by hue.
#[derive(Clone, Copy, Debug)]
pub struct Theme {
    pub flavor: Flavor,
    /// Page background.
    pub bg: Color,
    /// Slightly raised background: focused pane, selected row.
    pub surface: Color,
    /// Further raised: popups, the command palette.
    pub overlay: Color,
    /// Primary body text.
    pub text: Color,
    /// De-emphasised text: paths, counts, hints.
    pub subtext: Color,
    /// Barely-there text: inactive borders, gutters.
    pub faint: Color,
    /// The one accent colour. Focus, selection, headings.
    pub accent: Color,
    /// Secondary accent for links and cross references.
    pub link: Color,
    pub ok: Color,
    pub warn: Color,
    pub err: Color,
    /// Literals, code spans, footnote labels.
    pub literal: Color,
}

impl Default for Theme {
    fn default() -> Self {
        Self::new(Flavor::default())
    }
}

impl Theme {
    pub fn new(flavor: Flavor) -> Self {
        let c = match flavor {
            Flavor::Latte => &PALETTE.latte.colors,
            Flavor::Frappe => &PALETTE.frappe.colors,
            Flavor::Macchiato => &PALETTE.macchiato.colors,
            Flavor::Mocha => &PALETTE.mocha.colors,
        };
        Self {
            flavor,
            bg: c.base.into(),
            surface: c.surface0.into(),
            overlay: c.mantle.into(),
            text: c.text.into(),
            subtext: c.subtext0.into(),
            faint: c.overlay0.into(),
            accent: c.blue.into(),
            link: c.sapphire.into(),
            ok: c.green.into(),
            warn: c.yellow.into(),
            err: c.red.into(),
            literal: c.peach.into(),
        }
    }

    pub fn base(&self) -> Style {
        Style::default().fg(self.text).bg(self.bg)
    }

    pub fn dim(&self) -> Style {
        Style::default().fg(self.subtext)
    }

    pub fn faint_style(&self) -> Style {
        Style::default().fg(self.faint)
    }

    /// Border of a pane, brightened when it holds focus.
    pub fn border(&self, focused: bool) -> Style {
        if focused {
            Style::default().fg(self.accent)
        } else {
            Style::default().fg(self.faint)
        }
    }

    /// Pane title, brightened when it holds focus.
    pub fn title(&self, focused: bool) -> Style {
        if focused {
            Style::default().fg(self.accent).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(self.subtext)
        }
    }

    /// Selected row. Only the focused pane gets the accent wash; an unfocused
    /// pane keeps a muted selection so you can still see where you left off.
    pub fn selection(&self, focused: bool) -> Style {
        if focused {
            Style::default().fg(self.bg).bg(self.accent).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(self.text).bg(self.surface)
        }
    }

    pub fn heading(&self, level: u8) -> Style {
        let s = Style::default().add_modifier(Modifier::BOLD);
        match level {
            1 => s.fg(self.accent),
            2 => s.fg(self.link),
            _ => s.fg(self.text),
        }
    }

    pub fn status_of(&self, status: &str) -> Color {
        match status {
            "verified" | "stable" | "done" | "ok" => self.ok,
            "draft" | "wip" | "in_progress" | "working" => self.warn,
            "failed" | "blocked" | "stub" => self.err,
            _ => self.subtext,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_both_spellings() {
        assert_eq!(Flavor::parse("latte"), Some(Flavor::Latte));
        assert_eq!(Flavor::parse("catppuccin-mocha"), Some(Flavor::Mocha));
        assert_eq!(Flavor::parse("  Frappe "), Some(Flavor::Frappe));
        assert_eq!(Flavor::parse("dracula"), None);
    }

    #[test]
    fn herdr_name_round_trips() {
        for f in Flavor::ALL {
            assert_eq!(Flavor::parse(&f.herdr_name()), Some(f));
        }
    }

    #[test]
    fn default_is_latte_to_match_podarcisnest() {
        assert_eq!(Theme::default().flavor, Flavor::Latte);
    }

    #[test]
    fn cycling_visits_every_flavor() {
        let mut f = Flavor::Latte;
        let mut seen = vec![f];
        for _ in 0..3 {
            f = f.next();
            seen.push(f);
        }
        assert_eq!(seen, Flavor::ALL.to_vec());
        assert_eq!(f.next(), Flavor::Latte);
    }
}
