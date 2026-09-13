//! Checkout discovery and `.podarcis/config.yaml`.
//!
//! The Python engine owns the config file; we only read it. Nothing here writes
//! to `config.yaml` — a preference changed in the app is written back through
//! `podarcis config`, so there stays exactly one writer.

use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde_yaml_ng::Value;

use crate::theme::Flavor;

/// A checkout is `AGENTS.md` + `.podarcis/config.yaml` — never the engine
/// package on its own. Mirrors `podarcis.root.is_wiki_root`.
pub fn is_root(path: &Path) -> bool {
    path.join("AGENTS.md").is_file() && path.join(".podarcis").join("config.yaml").is_file()
}

/// Resolve the checkout: explicit `--root`, then `$PODARCIS_ROOT`, then walk up
/// from `cwd`. A pinned path that is not a checkout is an error, with no walk
/// fallback — the user asked for that specific path.
pub fn find_root(explicit: Option<&Path>, cwd: &Path, env_root: Option<&str>) -> Result<PathBuf> {
    let pinned = explicit
        .map(PathBuf::from)
        .or_else(|| env_root.filter(|s| !s.trim().is_empty()).map(PathBuf::from));
    if let Some(pinned) = pinned {
        let path = absolutize(&pinned, cwd);
        if !is_root(&path) {
            bail!(
                "not a Podarcis checkout at {} (no AGENTS.md + .podarcis/config.yaml). Pass --root.",
                path.display()
            );
        }
        return Ok(path);
    }

    let start = absolutize(cwd, cwd);
    let mut probe: Option<&Path> = Some(&start);
    while let Some(dir) = probe {
        if is_root(dir) {
            return Ok(dir.to_path_buf());
        }
        probe = dir.parent();
    }
    bail!("not a Podarcis checkout (no AGENTS.md + .podarcis/config.yaml). Pass --root.")
}

fn absolutize(path: &Path, cwd: &Path) -> PathBuf {
    let expanded = if let Ok(rest) = path.strip_prefix("~") {
        match std::env::var_os("HOME") {
            Some(home) => PathBuf::from(home).join(rest),
            None => path.to_path_buf(),
        }
    } else {
        path.to_path_buf()
    };
    let joined = if expanded.is_absolute() { expanded } else { cwd.join(expanded) };
    joined.canonicalize().unwrap_or(joined)
}

/// The slice of `config.yaml` this front-end cares about.
#[derive(Clone, Debug)]
pub struct Config {
    pub root: PathBuf,
    pub flavor: Flavor,
    pub tree_width: u16,
    pub sidebar_width: u16,
    pub sidebar_open: bool,
    pub tree_open: bool,
    /// `engines.qmd` — false means semantic search is off and we say so rather
    /// than spawning a search that will fail.
    pub qmd_enabled: bool,
}

impl Config {
    pub fn load(root: &Path) -> Self {
        let doc = std::fs::read_to_string(root.join(".podarcis").join("config.yaml"))
            .ok()
            .and_then(|raw| serde_yaml_ng::from_str::<Value>(&raw).ok())
            .unwrap_or(Value::Null);

        let tui = doc.get("tui");
        Self {
            root: root.to_path_buf(),
            flavor: tui
                .and_then(|t| t.get("theme"))
                .and_then(Value::as_str)
                .and_then(Flavor::parse)
                .unwrap_or_default(),
            tree_width: clamp_width(tui.and_then(|t| t.get("tree_width")), 30, 16, 80),
            sidebar_width: clamp_width(tui.and_then(|t| t.get("sidebar_width")), 42, 20, 120),
            sidebar_open: tui.and_then(|t| t.get("sidebar")).and_then(Value::as_bool).unwrap_or(true),
            tree_open: tui.and_then(|t| t.get("tree")).and_then(Value::as_bool).unwrap_or(true),
            qmd_enabled: doc
                .get("engines")
                .and_then(|e| e.get("qmd"))
                .and_then(Value::as_bool)
                .unwrap_or(false),
        }
    }

    /// The three content repositories, in the order they are shown.
    pub fn collections(&self) -> Vec<(&'static str, PathBuf)> {
        [("wiki", "wiki"), ("workspace", "workspace"), ("sources", "sources")]
            .into_iter()
            .map(|(label, dir)| (label, self.root.join(dir)))
            .filter(|(_, path)| path.is_dir())
            .collect()
    }


    /// The `podarcis` shim next to the checkout, falling back to `$PATH`.
    pub fn cli(&self) -> PathBuf {
        let shim = self.root.join("podarcis");
        if shim.is_file() {
            shim
        } else {
            PathBuf::from("podarcis")
        }
    }
}

fn clamp_width(value: Option<&Value>, default: u16, min: u16, max: u16) -> u16 {
    value
        .and_then(Value::as_u64)
        .map(|n| n.clamp(min as u64, max as u64) as u16)
        .unwrap_or(default)
}

/// Read `version` out of the repo's `pyproject.toml` so the status bar can show
/// the engine version without paying for a Python start-up.
pub fn engine_version(root: &Path) -> Option<String> {
    let raw = std::fs::read_to_string(root.join("pyproject.toml")).ok()?;
    raw.lines()
        .map(str::trim)
        .find_map(|line| line.strip_prefix("version"))
        .and_then(|rest| rest.trim().strip_prefix('='))
        .map(|rest| rest.trim().trim_matches('"').trim_matches('\'').to_string())
}

/// Resolve a path the user typed on the command line into a file to open.
pub fn resolve_open_path(arg: &str, root: &Path, cwd: &Path) -> Result<PathBuf> {
    let candidate = PathBuf::from(arg);
    for base in [cwd.to_path_buf(), root.to_path_buf()] {
        let probe = if candidate.is_absolute() { candidate.clone() } else { base.join(&candidate) };
        if probe.exists() {
            return probe.canonicalize().context("resolving path");
        }
    }
    bail!("no such file: {arg}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("podarcis-tui-test-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "# agents\n").unwrap();
        std::fs::write(dir.join(".podarcis").join("config.yaml"), "engines:\n  qmd: true\n").unwrap();
        dir
    }

    #[test]
    fn root_needs_both_markers() {
        let dir = scratch("markers");
        assert!(is_root(&dir));
        std::fs::remove_file(dir.join("AGENTS.md")).unwrap();
        assert!(!is_root(&dir));
    }

    #[test]
    fn walks_up_from_a_nested_cwd() {
        let dir = scratch("walk");
        let nested = dir.join("wiki").join("health");
        std::fs::create_dir_all(&nested).unwrap();
        let found = find_root(None, &nested, None).unwrap();
        assert_eq!(found.canonicalize().unwrap(), dir.canonicalize().unwrap());
    }

    #[test]
    fn pinned_non_checkout_errors_without_falling_back_to_the_walk() {
        let dir = scratch("pinned");
        let nested = dir.join("wiki");
        std::fs::create_dir_all(&nested).unwrap();
        // `nested` is inside a real checkout, so a walk would have succeeded.
        let err = find_root(Some(&nested), &nested, None).unwrap_err();
        assert!(err.to_string().contains("not a Podarcis checkout"));
    }

    #[test]
    fn env_root_is_honoured_when_no_explicit_flag() {
        let dir = scratch("env");
        let found = find_root(None, Path::new("/"), Some(dir.to_str().unwrap())).unwrap();
        assert_eq!(found.canonicalize().unwrap(), dir.canonicalize().unwrap());
    }

    #[test]
    fn blank_env_root_is_ignored() {
        let dir = scratch("blank-env");
        let found = find_root(None, &dir, Some("   ")).unwrap();
        assert_eq!(found.canonicalize().unwrap(), dir.canonicalize().unwrap());
    }

    #[test]
    fn config_defaults_to_latte_and_reads_overrides() {
        let dir = scratch("config");
        let cfg = Config::load(&dir);
        assert_eq!(cfg.flavor, Flavor::Latte);
        assert!(cfg.qmd_enabled);
        assert_eq!(cfg.tree_width, 30);

        std::fs::write(
            dir.join(".podarcis").join("config.yaml"),
            "engines:\n  qmd: false\ntui:\n  theme: catppuccin-mocha\n  tree_width: 9999\n  sidebar: false\n",
        )
        .unwrap();
        let cfg = Config::load(&dir);
        assert_eq!(cfg.flavor, Flavor::Mocha);
        assert!(!cfg.qmd_enabled);
        assert_eq!(cfg.tree_width, 80, "widths are clamped, not trusted");
        assert!(!cfg.sidebar_open);
    }

    #[test]
    fn missing_config_never_panics() {
        let dir = std::env::temp_dir().join(format!("podarcis-empty-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let cfg = Config::load(&dir);
        assert_eq!(cfg.flavor, Flavor::Latte);
        assert!(!cfg.qmd_enabled);
    }
}
