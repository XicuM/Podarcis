//! Theme palettes mapped onto semantic tokens.
//!
//! Every colour in the app comes from here. Widgets never name a palette colour
//! directly, so re-flavouring is a single `Theme::new` call and the embedded
//! herdr pane can be handed the matching flavour name verbatim.

use catppuccin::PALETTE;
use ratatui::style::{Color, Modifier, Style};

const fn rgb(hex: u32) -> Color {
    Color::Rgb(
        ((hex >> 16) & 0xff) as u8,
        ((hex >> 8) & 0xff) as u8,
        (hex & 0xff) as u8,
    )
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Flavor {
    #[default]
    Latte,
    Frappe,
    Macchiato,
    Mocha,
    TokyoNight,
    TokyoNightDay,
    Dracula,
    Nord,
    Gruvbox,
    GruvboxLight,
    OneDark,
    OneLight,
    Solarized,
    SolarizedLight,
    Kanagawa,
    KanagawaLotus,
    RosePine,
    RosePineDawn,
    Vesper,
    Terminal,
}

impl Flavor {
    /// Accepts both the bare flavour (`latte`, `nord`, `tokyo-night`) and herdr's
    /// spelling (`catppuccin-latte`, `catppuccin`), as well as common aliases.
    pub fn parse(name: &str) -> Option<Self> {
        let raw = name.trim().to_ascii_lowercase();
        let key = raw.strip_prefix("catppuccin-").unwrap_or(&raw);
        match key {
            "latte" | "catppuccin-latte" | "light" => Some(Self::Latte),
            "frappe" | "catppuccin-frappe" | "frappé" => Some(Self::Frappe),
            "macchiato" | "catppuccin-macchiato" => Some(Self::Macchiato),
            "mocha" | "catppuccin-mocha" | "catppuccin" | "dark" => Some(Self::Mocha),
            "tokyo-night" | "tokyonight" | "tokyo_night" => Some(Self::TokyoNight),
            "tokyo-night-day" | "tokyonight-day" | "tokyo_night_day" | "tokyo-night-light" => {
                Some(Self::TokyoNightDay)
            }
            "dracula" => Some(Self::Dracula),
            "nord" => Some(Self::Nord),
            "gruvbox" | "gruvbox-dark" | "gruvbox_dark" => Some(Self::Gruvbox),
            "gruvbox-light" | "gruvbox_light" => Some(Self::GruvboxLight),
            "one-dark" | "onedark" | "one_dark" => Some(Self::OneDark),
            "one-light" | "onelight" | "one_light" => Some(Self::OneLight),
            "solarized" | "solarized-dark" | "solarized_dark" => Some(Self::Solarized),
            "solarized-light" | "solarized_light" => Some(Self::SolarizedLight),
            "kanagawa" | "kanagawa-wave" | "kanagawa_wave" => Some(Self::Kanagawa),
            "kanagawa-lotus" | "kanagawa_lotus" => Some(Self::KanagawaLotus),
            "rose-pine" | "rosepine" | "rose_pine" => Some(Self::RosePine),
            "rose-pine-dawn" | "rosepinedawn" | "rose_pine_dawn" => Some(Self::RosePineDawn),
            "vesper" => Some(Self::Vesper),
            "terminal" => Some(Self::Terminal),
            _ => None,
        }
    }

    pub const ALL: [Flavor; 20] = [
        Flavor::Latte,
        Flavor::Frappe,
        Flavor::Macchiato,
        Flavor::Mocha,
        Flavor::TokyoNight,
        Flavor::TokyoNightDay,
        Flavor::Dracula,
        Flavor::Nord,
        Flavor::Gruvbox,
        Flavor::GruvboxLight,
        Flavor::OneDark,
        Flavor::OneLight,
        Flavor::Solarized,
        Flavor::SolarizedLight,
        Flavor::Kanagawa,
        Flavor::KanagawaLotus,
        Flavor::RosePine,
        Flavor::RosePineDawn,
        Flavor::Vesper,
        Flavor::Terminal,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Latte => "latte",
            Self::Frappe => "frappe",
            Self::Macchiato => "macchiato",
            Self::Mocha => "mocha",
            Self::TokyoNight => "tokyo-night",
            Self::TokyoNightDay => "tokyo-night-day",
            Self::Dracula => "dracula",
            Self::Nord => "nord",
            Self::Gruvbox => "gruvbox",
            Self::GruvboxLight => "gruvbox-light",
            Self::OneDark => "one-dark",
            Self::OneLight => "one-light",
            Self::Solarized => "solarized",
            Self::SolarizedLight => "solarized-light",
            Self::Kanagawa => "kanagawa",
            Self::KanagawaLotus => "kanagawa-lotus",
            Self::RosePine => "rose-pine",
            Self::RosePineDawn => "rose-pine-dawn",
            Self::Vesper => "vesper",
            Self::Terminal => "terminal",
        }
    }

    #[allow(dead_code)]
    pub fn display_name(self) -> &'static str {
        match self {
            Self::Latte => "catppuccin latte",
            Self::Frappe => "catppuccin frappe",
            Self::Macchiato => "catppuccin macchiato",
            Self::Mocha => "catppuccin mocha",
            Self::TokyoNight => "tokyo night",
            Self::TokyoNightDay => "tokyo night day",
            Self::Dracula => "dracula",
            Self::Nord => "nord",
            Self::Gruvbox => "gruvbox",
            Self::GruvboxLight => "gruvbox light",
            Self::OneDark => "one dark",
            Self::OneLight => "one light",
            Self::Solarized => "solarized dark",
            Self::SolarizedLight => "solarized light",
            Self::Kanagawa => "kanagawa",
            Self::KanagawaLotus => "kanagawa lotus",
            Self::RosePine => "rose pine",
            Self::RosePineDawn => "rose pine dawn",
            Self::Vesper => "vesper",
            Self::Terminal => "terminal",
        }
    }

    /// The `[theme] name` value the embedded herdr expects.
    pub fn herdr_name(self) -> &'static str {
        match self {
            Self::Latte => "catppuccin-latte",
            // Herdr only ships catppuccin-latte and catppuccin (mocha) for Catppuccin;
            // mapping frappe and macchiato to "catppuccin" avoids an unknown theme warning.
            Self::Frappe => "catppuccin",
            Self::Macchiato => "catppuccin",
            Self::Mocha => "catppuccin",
            Self::TokyoNight => "tokyo-night",
            Self::TokyoNightDay => "tokyo-night-day",
            Self::Dracula => "dracula",
            Self::Nord => "nord",
            Self::Gruvbox => "gruvbox",
            Self::GruvboxLight => "gruvbox-light",
            Self::OneDark => "one-dark",
            Self::OneLight => "one-light",
            Self::Solarized => "solarized",
            Self::SolarizedLight => "solarized-light",
            Self::Kanagawa => "kanagawa",
            Self::KanagawaLotus => "kanagawa-lotus",
            Self::RosePine => "rose-pine",
            Self::RosePineDawn => "rose-pine-dawn",
            Self::Vesper => "vesper",
            Self::Terminal => "terminal",
        }
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
        match flavor {
            Flavor::Latte => {
                let c = &PALETTE.latte.colors;
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
            Flavor::Frappe => {
                let c = &PALETTE.frappe.colors;
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
            Flavor::Macchiato => {
                let c = &PALETTE.macchiato.colors;
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
            Flavor::Mocha => {
                let c = &PALETTE.mocha.colors;
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
            Flavor::TokyoNight => Self {
                flavor,
                bg: rgb(0x1a1b26),
                surface: rgb(0x24283b),
                overlay: rgb(0x1f2335),
                text: rgb(0xc0caf5),
                subtext: rgb(0xa9b1d6),
                faint: rgb(0x565f89),
                accent: rgb(0x7aa2f7),
                link: rgb(0x7dcfff),
                ok: rgb(0x9ece6a),
                warn: rgb(0xe0af68),
                err: rgb(0xf7768e),
                literal: rgb(0xff9e64),
            },
            Flavor::TokyoNightDay => Self {
                flavor,
                bg: rgb(0xe1e2e7),
                surface: rgb(0xc4c8da),
                overlay: rgb(0xd2d3da),
                text: rgb(0x3760bf),
                subtext: rgb(0x6172b0),
                faint: rgb(0x8990b3),
                accent: rgb(0x2e7de9),
                link: rgb(0x2e7de9),
                ok: rgb(0x587539),
                warn: rgb(0x8c6c3e),
                err: rgb(0xf52a65),
                literal: rgb(0xb15c00),
            },
            Flavor::Dracula => Self {
                flavor,
                bg: rgb(0x282a36),
                surface: rgb(0x44475a),
                overlay: rgb(0x373c52),
                text: rgb(0xf8f8f2),
                subtext: rgb(0xd2d2dc),
                faint: rgb(0x6272a4),
                accent: rgb(0xbd93f9),
                link: rgb(0x8be9fd),
                ok: rgb(0x50fa7b),
                warn: rgb(0xf1fa8c),
                err: rgb(0xff5555),
                literal: rgb(0xffb86c),
            },
            Flavor::Nord => Self {
                flavor,
                bg: rgb(0x2e3440),
                surface: rgb(0x3b4252),
                overlay: rgb(0x434c5e),
                text: rgb(0xeceff4),
                subtext: rgb(0xd8dee9),
                faint: rgb(0x4c566a),
                accent: rgb(0x88c0d0),
                link: rgb(0x81a1c1),
                ok: rgb(0xa3be8c),
                warn: rgb(0xebcb8b),
                err: rgb(0xbf616a),
                literal: rgb(0xd08770),
            },
            Flavor::Gruvbox => Self {
                flavor,
                bg: rgb(0x282828),
                surface: rgb(0x3c3836),
                overlay: rgb(0x323130),
                text: rgb(0xebdbb2),
                subtext: rgb(0xd5c4a1),
                faint: rgb(0x928374),
                accent: rgb(0xfe8019),
                link: rgb(0x83a598),
                ok: rgb(0xb8bb26),
                warn: rgb(0xfabd2f),
                err: rgb(0xfb4934),
                literal: rgb(0xd3869b),
            },
            Flavor::GruvboxLight => Self {
                flavor,
                bg: rgb(0xfbf1c7),
                surface: rgb(0xebdbb2),
                overlay: rgb(0xf2e5bc),
                text: rgb(0x3c3836),
                subtext: rgb(0x504945),
                faint: rgb(0x928374),
                accent: rgb(0xaf3a03),
                link: rgb(0x076678),
                ok: rgb(0x79740e),
                warn: rgb(0xb57614),
                err: rgb(0x9d0006),
                literal: rgb(0x8f3f71),
            },
            Flavor::OneDark => Self {
                flavor,
                bg: rgb(0x282c34),
                surface: rgb(0x2c313a),
                overlay: rgb(0x313640),
                text: rgb(0xabb2bf),
                subtext: rgb(0x969ca8),
                faint: rgb(0x5c6370),
                accent: rgb(0x61afef),
                link: rgb(0x56b6c2),
                ok: rgb(0x98c379),
                warn: rgb(0xe5c07b),
                err: rgb(0xe06c75),
                literal: rgb(0xd19a66),
            },
            Flavor::OneLight => Self {
                flavor,
                bg: rgb(0xfafafa),
                surface: rgb(0xf0f0f1),
                overlay: rgb(0xe5e5e6),
                text: rgb(0x383a42),
                subtext: rgb(0x686b77),
                faint: rgb(0xa0a1a7),
                accent: rgb(0x4078f2),
                link: rgb(0x0184bc),
                ok: rgb(0x50a14f),
                warn: rgb(0xc18401),
                err: rgb(0xe45649),
                literal: rgb(0x986801),
            },
            Flavor::Solarized => Self {
                flavor,
                bg: rgb(0x002b36),
                surface: rgb(0x073642),
                overlay: rgb(0x00212b),
                text: rgb(0x839496),
                subtext: rgb(0x93a1a1),
                faint: rgb(0x586e75),
                accent: rgb(0x268bd2),
                link: rgb(0x2aa198),
                ok: rgb(0x859900),
                warn: rgb(0xb58900),
                err: rgb(0xdc322f),
                literal: rgb(0xcb4b16),
            },
            Flavor::SolarizedLight => Self {
                flavor,
                bg: rgb(0xfdf6e3),
                surface: rgb(0xeee8d5),
                overlay: rgb(0xc9dcdf),
                text: rgb(0x657b83),
                subtext: rgb(0x586e75),
                faint: rgb(0x93a1a1),
                accent: rgb(0x268bd2),
                link: rgb(0x2aa198),
                ok: rgb(0x859900),
                warn: rgb(0xb58900),
                err: rgb(0xdc322f),
                literal: rgb(0xcb4b16),
            },
            Flavor::Kanagawa => Self {
                flavor,
                bg: rgb(0x1f1f28),
                surface: rgb(0x2a2a37),
                overlay: rgb(0x363646),
                text: rgb(0xdcd7ba),
                subtext: rgb(0xc8c3aa),
                faint: rgb(0x727169),
                accent: rgb(0x7e9cd8),
                link: rgb(0x7aa89f),
                ok: rgb(0x76946a),
                warn: rgb(0xc0a36e),
                err: rgb(0xc34043),
                literal: rgb(0xffa066),
            },
            Flavor::KanagawaLotus => Self {
                flavor,
                bg: rgb(0xf2ecbc),
                surface: rgb(0xd5cea3),
                overlay: rgb(0xdcd5ac),
                text: rgb(0x545464),
                subtext: rgb(0x43436c),
                faint: rgb(0x8a8980),
                accent: rgb(0x4d699b),
                link: rgb(0x4d699b),
                ok: rgb(0x6f894e),
                warn: rgb(0x77713f),
                err: rgb(0xc84053),
                literal: rgb(0xcc6d00),
            },
            Flavor::RosePine => Self {
                flavor,
                bg: rgb(0x191724),
                surface: rgb(0x26233a),
                overlay: rgb(0x1f1d2e),
                text: rgb(0xe0def4),
                subtext: rgb(0xc8c5dc),
                faint: rgb(0x6e6a86),
                accent: rgb(0xc4a7e7),
                link: rgb(0x31748f),
                ok: rgb(0x31748f),
                warn: rgb(0xf6c177),
                err: rgb(0xeb6f92),
                literal: rgb(0xebbcba),
            },
            Flavor::RosePineDawn => Self {
                flavor,
                bg: rgb(0xfaf4ed),
                surface: rgb(0xf2e9e1),
                overlay: rgb(0xe3d9cf),
                text: rgb(0x464261),
                subtext: rgb(0x797593),
                faint: rgb(0x9893a5),
                accent: rgb(0xd7827e),
                link: rgb(0x286983),
                ok: rgb(0x286983),
                warn: rgb(0xea9d34),
                err: rgb(0xb4637a),
                literal: rgb(0xd7827e),
            },
            Flavor::Vesper => Self {
                flavor,
                bg: rgb(0x101010),
                surface: rgb(0x232323),
                overlay: rgb(0x282828),
                text: rgb(0xffffff),
                subtext: rgb(0xa0a0a0),
                faint: rgb(0x5c5c5c),
                accent: rgb(0xffd1a8),
                link: rgb(0xa0a0a0),
                ok: rgb(0x99ffe4),
                warn: rgb(0xffd1a8),
                err: rgb(0xff8080),
                literal: rgb(0xff9955),
            },
            Flavor::Terminal => Self {
                flavor,
                bg: Color::Reset,
                surface: Color::DarkGray,
                overlay: Color::Black,
                text: Color::Reset,
                subtext: Color::Gray,
                faint: Color::DarkGray,
                accent: Color::Cyan,
                link: Color::Blue,
                ok: Color::Green,
                warn: Color::Yellow,
                err: Color::Red,
                literal: Color::Magenta,
            },
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
            let fg = if self.bg == Color::Reset { Color::Black } else { self.bg };
            Style::default().fg(fg).bg(self.accent).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(self.text).bg(self.surface)
        }
    }

    /// Emphasized prose. `BOLD` alone is one font away from invisible: a
    /// terminal whose regular and bold faces look alike renders `**text**` as
    /// body text. Pushing the colour to the page's contrast pole — black on a
    /// light background, white on a dark one — keeps the emphasis legible no
    /// matter how thin the bold face is. The `Terminal` flavor inherits its
    /// palette, so there is no known pole and bold stays a pure `BOLD`.
    pub fn bold(&self) -> Style {
        let base = Style::default().add_modifier(Modifier::BOLD);
        match self.bold_color() {
            Some(fg) => base.fg(fg),
            None => base,
        }
    }

    fn bold_color(&self) -> Option<Color> {
        let Color::Rgb(r, g, b) = self.bg else {
            return None;
        };
        let perceived = 0.299 * r as f32 + 0.587 * g as f32 + 0.114 * b as f32;
        Some(if perceived > 128.0 { Color::Black } else { Color::White })
    }

    /// Headings step down through the palette rather than all landing on
    /// `text`: past h2 the only cue a terminal has left is colour, so h3 and
    /// h4+ have to differ from body prose and from each other.
    pub fn heading(&self, level: u8) -> Style {
        let s = Style::default().add_modifier(Modifier::BOLD);
        match level {
            1 => s.fg(self.accent),
            2 => s.fg(self.link),
            // h3 is where a heading first shares body text colour, so it is
            // the level that needs the bold contrast pole.
            3 => s.fg(self.bold_color().unwrap_or(self.text)),
            _ => s.fg(self.subtext).add_modifier(Modifier::ITALIC),
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
    fn parses_spellings_and_aliases() {
        assert_eq!(Flavor::parse("latte"), Some(Flavor::Latte));
        assert_eq!(Flavor::parse("catppuccin-mocha"), Some(Flavor::Mocha));
        assert_eq!(Flavor::parse("  Frappe "), Some(Flavor::Frappe));
        assert_eq!(Flavor::parse("dracula"), Some(Flavor::Dracula));
        assert_eq!(Flavor::parse("tokyo-night"), Some(Flavor::TokyoNight));
        assert_eq!(Flavor::parse("nord"), Some(Flavor::Nord));
        assert_eq!(Flavor::parse("gruvbox-light"), Some(Flavor::GruvboxLight));
        assert_eq!(Flavor::parse("terminal"), Some(Flavor::Terminal));
        assert_eq!(Flavor::parse("nonexistent-theme-xyz"), None);
    }

    #[test]
    fn herdr_name_parses_back() {
        for f in Flavor::ALL {
            assert!(
                Flavor::parse(f.herdr_name()).is_some(),
                "herdr_name {} for {:?} must be parseable",
                f.herdr_name(),
                f
            );
        }
    }

    #[test]
    fn as_str_round_trips_for_every_flavor() {
        for f in Flavor::ALL {
            assert_eq!(Flavor::parse(f.as_str()), Some(f), "flavor {:?} must round trip via as_str", f);
        }
    }

    #[test]
    fn default_is_latte_to_match_podarcisnest() {
        assert_eq!(Theme::default().flavor, Flavor::Latte);
    }

    #[test]
    fn every_flavor_is_listed_once_and_round_trips_through_parse() {
        let mut seen: Vec<&str> = Vec::new();
        for flavor in Flavor::ALL {
            let name = flavor.as_str();
            assert!(!seen.contains(&name), "{name} is listed twice");
            seen.push(name);
            assert_eq!(Flavor::parse(name), Some(flavor));
            assert!(!flavor.display_name().is_empty());
        }
        assert_eq!(seen.len(), Flavor::ALL.len());
    }
}

