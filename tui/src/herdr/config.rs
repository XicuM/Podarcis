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

pub fn render(flavor: Flavor) -> String {
    TEMPLATE.replace("@THEME@", flavor.herdr_name())
}

/// Write the session config, returning the path. Only rewrites when the content
/// actually differs, so a running herdr is not asked to reload for nothing.
pub fn provision(flavor: Flavor) -> Result<PathBuf> {
    let dir = session_dir().context("no HOME or XDG_CONFIG_HOME to place the herdr config in")?;
    write_into(&dir, flavor)
}

pub fn write_into(dir: &Path, flavor: Flavor) -> Result<PathBuf> {
    let path = dir.join("config.toml");
    let wanted = render(flavor);
    if std::fs::read_to_string(&path).ok().as_deref() == Some(wanted.as_str()) {
        return Ok(path);
    }
    std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    std::fs::write(&path, wanted).with_context(|| format!("writing {}", path.display()))?;
    Ok(path)
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
        }
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
    fn session_dir_follows_xdg_config_home() {
        // Read whatever the environment says; the point is the shape, not the value.
        let dir = session_dir().expect("HOME is always set in the test environment");
        assert!(dir.ends_with("herdr/sessions/podarcis"), "{}", dir.display());
    }
}
