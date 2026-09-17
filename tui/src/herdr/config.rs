//! Provisioning the embedded herdr's session configuration.
//!
//! The template is the canonical Podarcis herdr config — byte-for-byte the one
//! PodarcisNest bakes into its user containers — with the theme substituted so
//! the sidebar matches the app it is embedded in. It is written on every launch
//! rather than merged into, because merging is how the old Python front-end
//! ended up appending keybindings to a file it did not own.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use crate::theme::Flavor;

pub const SESSION: &str = "podarcis";

const TEMPLATE: &str = include_str!("../../assets/herdr-session.toml");

/// `~/.config/herdr/sessions/podarcis/`, honouring `$XDG_CONFIG_HOME`.
pub fn session_dir() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))?;
    Some(base.join("herdr").join("sessions").join(SESSION))
}

/// The state home the embedded herdr client runs under.
///
/// herdr keeps the saved-SSH-machine catalog and the machine currently selected
/// in `$XDG_STATE_HOME/herdr/client/` — one pair of files shared by every herdr
/// client on the box, not one per session. Picking a machine in any other herdr
/// window therefore retargets this pane at its next launch, and because our
/// session config hides the sidebar there is no way to select Local again from
/// inside the pane. A state home of our own leaves the embedded client with an
/// empty catalog, so it always runs Local — the only coherent choice, since the
/// app reads `wiki/` off this disk and drives the pane over a local socket.
pub fn client_state_home() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local").join("state")))?;
    Some(base.join("podarcis").join("herdr-state"))
}

/// The environment that pins a herdr invocation to the local machine.
pub fn local_env() -> Option<(&'static str, PathBuf)> {
    client_state_home().map(|dir| ("XDG_STATE_HOME", dir))
}

/// A standard Command configured to target the podarcis session, on this machine.
pub fn herdr_cmd() -> std::process::Command {
    let mut cmd = std::process::Command::new(super::pty::herdr_binary());
    cmd.args(["--session", SESSION]);
    if let Some((key, dir)) = local_env() {
        cmd.env(key, dir);
    }
    if let Some(path) = session_dir().map(|d| d.join("config.toml")) {
        if path.exists() {
            cmd.env("HERDR_CONFIG_PATH", path);
        }
    }
    cmd
}

/// Trailing comment that marks a `[theme.custom]` key as generated.
pub const GENERATED_MARK: &str = "podarcis-tui";

pub fn render(flavor: Flavor) -> String {
    render_with_existing(flavor, "")
}

fn render_with_existing(flavor: Flavor, existing: &str) -> String {
    let custom = format_custom_block(flavor, &parse_custom(existing));
    TEMPLATE
        .replace("@THEME@", flavor.herdr_name())
        .replace("@THEME_CUSTOM@", &custom)
}

/// Write the session config, returning the path. Only rewrites when the content
/// actually differs, so a running herdr is not asked to reload for nothing.
pub fn provision(flavor: Flavor) -> Result<PathBuf> {
    let dir = session_dir().context("no HOME or XDG_CONFIG_HOME to place the herdr config in")?;
    write_into(&dir, flavor)
}

pub fn write_into(dir: &Path, flavor: Flavor) -> Result<PathBuf> {
    let path = dir.join("config.toml");
    let existing = std::fs::read_to_string(&path).unwrap_or_default();
    let wanted = render_with_existing(flavor, &existing);
    if existing == wanted {
        return Ok(path);
    }
    std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    std::fs::write(&path, wanted).with_context(|| format!("writing {}", path.display()))?;
    Ok(path)
}

struct ParsedCustom {
    user: Vec<(String, String)>,
    generated: Vec<(String, String)>,
}

fn parse_custom(src: &str) -> ParsedCustom {
    let mut user = Vec::new();
    let mut generated = Vec::new();
    let mut in_custom = false;
    for line in src.lines() {
        let trimmed = line.trim();
        if trimmed == "[theme.custom]" {
            in_custom = true;
            continue;
        }
        if trimmed.starts_with('[') {
            in_custom = false;
            continue;
        }
        if !in_custom {
            continue;
        }
        let Some(assign) = parse_assignment(trimmed) else {
            continue;
        };
        if assign.generated {
            generated.push((assign.key, assign.value));
        } else if !assign.commented {
            user.push((assign.key, assign.rhs));
        }
    }
    ParsedCustom { user, generated }
}

struct Assign {
    key: String,
    value: String,
    rhs: String,
    generated: bool,
    commented: bool,
}

fn parse_assignment(trimmed: &str) -> Option<Assign> {
    let generated = trimmed.contains(&format!("# {GENERATED_MARK}"));
    let commented = trimmed.starts_with('#');
    let work = if commented {
        trimmed.trim_start_matches('#').trim()
    } else {
        trimmed
    };
    if work.starts_with('#') {
        return None;
    }
    let (key, rest) = work.split_once('=')?;
    let key = key.trim();
    if key.is_empty()
        || !key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_')
    {
        return None;
    }
    let rhs = rest.trim();
    let value = toml_string_value(rhs)?;
    Some(Assign {
        key: key.to_string(),
        value,
        rhs: rhs.to_string(),
        generated,
        commented,
    })
}

fn toml_string_value(rhs: &str) -> Option<String> {
    let rhs = rhs.trim();
    if let Some(s) = rhs.strip_prefix('"') {
        let end = s.find('"')?;
        return Some(s[..end].to_string());
    }
    let end = rhs.find(" #").unwrap_or(rhs.len());
    let v = rhs[..end].trim();
    (!v.is_empty()).then(|| v.to_string())
}

fn format_custom_block(flavor: Flavor, parsed: &ParsedCustom) -> String {
    let user_keys: std::collections::HashSet<&str> =
        parsed.user.iter().map(|(k, _)| k.as_str()).collect();
    let mut body = String::new();
    if let Some(tokens) = flavor.herdr_custom_tokens() {
        for (key, value) in tokens {
            if user_keys.contains(key) {
                continue;
            }
            body.push_str(&format!("{key} = \"{value}\"  # {GENERATED_MARK}\n"));
        }
    } else {
        for (key, value) in &parsed.generated {
            if user_keys.contains(key.as_str()) {
                continue;
            }
            body.push_str(&format!("# {key} = \"{value}\"  # {GENERATED_MARK}\n"));
        }
    }
    for (key, rhs) in &parsed.user {
        body.push_str(&format!("{key} = {rhs}\n"));
    }
    if body.is_empty() {
        return String::new();
    }
    format!(
        "\n# House palette tokens. Lines ending in `# {GENERATED_MARK}` are rewritten\n\
         # when you pick a flavour in the TUI. Drop that marker (or change the\n\
         # value and the marker) and the key is yours — we never overwrite it.\n\
         [theme.custom]\n{body}"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn template_carries_the_settings_the_sidebar_depends_on() {
        let rendered = render(Flavor::Latte);
        // Without nesting, herdr refuses to start inside our pane at all.
        assert!(rendered.contains("allow_nested = true"));
        // A second navigation sidebar inside ours would be nonsense.
        assert!(rendered.contains("sidebar_collapsed_mode = \"hidden\""));
        // The pane is narrow by design; the mobile layout must not kick in.
        assert!(rendered.contains("mobile_width_threshold = 0"));
        // ctrl+space is herdr's prefix, so the app must never claim it.
        assert!(rendered.contains("prefix = \"ctrl+space\""));
    }

    #[test]
    fn theme_is_substituted_for_every_flavor() {
        for flavor in Flavor::ALL {
            let rendered = render(flavor);
            assert!(rendered.contains(&format!("name = \"{}\"", flavor.herdr_name())));
            assert!(!rendered.contains("@THEME@"));
            assert!(!rendered.contains("@THEME_CUSTOM@"));
        }
    }

    #[test]
    fn house_flavour_writes_live_custom_tokens() {
        let rendered = render(Flavor::Podarcis);
        assert!(rendered.contains("name = \"catppuccin-latte\""));
        assert!(rendered.contains("[theme.custom]"));
        assert!(rendered.contains(&format!("# {GENERATED_MARK}")));
        let accent = crate::theme::Theme::new(Flavor::Podarcis).accent;
        let ratatui::style::Color::Rgb(r, g, b) = accent else {
            panic!("house accent is rgb");
        };
        assert!(rendered.contains(&format!("accent = \"#{r:02x}{g:02x}{b:02x}\"  # {GENERATED_MARK}")));
        let live_custom = rendered
            .lines()
            .filter(|l| l.starts_with("accent = "))
            .count();
        assert_eq!(live_custom, 1);
    }

    #[test]
    fn borrowed_flavour_has_no_live_generated_custom() {
        let rendered = render(Flavor::Nord);
        assert!(!rendered.contains("[theme.custom]"));
        assert!(!rendered.lines().any(|l| l.starts_with("accent = ")));
    }

    #[test]
    fn user_custom_key_wins_over_generated() {
        let existing = concat!(
            "[theme.custom]\n",
            "accent = \"#ff00aa\"  # mine\n",
            "panel_bg = \"#faf7f0\"  # podarcis-tui\n",
        );
        let rendered = render_with_existing(Flavor::Podarcis, existing);
        assert!(rendered.contains("accent = \"#ff00aa\"  # mine"));
        assert!(!rendered.contains(&format!("accent = \"#2f7d4f\"  # {GENERATED_MARK}")));
        assert!(rendered.contains(&format!("panel_bg = \"#faf7f0\"  # {GENERATED_MARK}")));
    }

    #[test]
    fn leaving_house_comments_generated_keys() {
        let house = render(Flavor::Podarcis);
        let nord = render_with_existing(Flavor::Nord, &house);
        assert!(nord.contains("name = \"nord\""));
        assert!(nord.contains(&format!("# accent = \"#2f7d4f\"  # {GENERATED_MARK}")));
        assert!(!nord.lines().any(|l| l.starts_with("accent = ")));
    }

    #[test]
    fn returning_to_house_uncomments_generated_and_keeps_user() {
        let existing = concat!(
            "[theme]\n",
            "name = \"nord\"\n",
            "\n",
            "[theme.custom]\n",
            "# accent = \"#2f7d4f\"  # podarcis-tui\n",
            "# panel_bg = \"#faf7f0\"  # podarcis-tui\n",
            "accent = \"#ff79c6\"\n",
        );
        let house = render_with_existing(Flavor::Podarcis, existing);
        assert!(house.contains("accent = \"#ff79c6\""));
        assert!(!house.contains(&format!("accent = \"#2f7d4f\"  # {GENERATED_MARK}")));
        assert!(house.contains(&format!("panel_bg = \"#faf7f0\"  # {GENERATED_MARK}")));
        assert!(house.lines().any(|l| l.starts_with("accent = ")));
    }

    #[test]
    fn writes_once_and_then_leaves_the_file_alone() {
        let dir = std::env::temp_dir().join(format!("podarcis-herdr-cfg-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let path = write_into(&dir, Flavor::Latte).unwrap();
        let first = std::fs::metadata(&path).unwrap().modified().unwrap();

        std::thread::sleep(std::time::Duration::from_millis(10));
        write_into(&dir, Flavor::Latte).unwrap();
        assert_eq!(std::fs::metadata(&path).unwrap().modified().unwrap(), first);

        write_into(&dir, Flavor::Mocha).unwrap();
        assert!(std::fs::read_to_string(&path).unwrap().contains("catppuccin"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_embedded_client_runs_under_a_state_home_of_its_own() {
        // herdr's machine catalog and selection live in $XDG_STATE_HOME/herdr/client.
        // Sharing the user's would let a machine picked in another herdr window
        // retarget this pane, which has no sidebar to switch it back.
        let ours = client_state_home().expect("HOME is always set in the test environment");
        assert!(ours.ends_with("podarcis/herdr-state"), "{}", ours.display());

        let cmd = herdr_cmd();
        let state: Vec<_> = cmd
            .get_envs()
            .filter(|(k, _)| *k == std::ffi::OsStr::new("XDG_STATE_HOME"))
            .collect();
        assert_eq!(state.len(), 1, "every herdr invocation is pinned to the local machine");
        assert_eq!(state[0].1, Some(ours.as_os_str()));
    }

    #[test]
    fn session_dir_follows_xdg_config_home() {
        // Read whatever the environment says; the point is the shape, not the value.
        let dir = session_dir().expect("HOME is always set in the test environment");
        assert!(dir.ends_with("herdr/sessions/podarcis"), "{}", dir.display());
    }
}
