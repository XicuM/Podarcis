//! Checkout discovery and `.podarcis/config.yaml`.
//!
//! Reads are lenient (a missing or malformed file just yields defaults), since
//! the Python engine is the one that actually validates this file. Writes are
//! narrow: `rewrite_block` replaces exactly one top-level block as text
//! (`tui:`, `repositories:`) and leaves every other byte alone — see its own
//! doc comment for why a full YAML round-trip would be wrong.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde_yaml_ng::Value;

use crate::theme::Flavor;

/// A checkout is `AGENTS.md` + `.podarcis/config.yaml`, or a `podarcis.yaml` project,
/// or a directory with `wiki/` + `workspace/`. Mirrors `podarcis.root.is_wiki_root`.
pub fn is_root(path: &Path) -> bool {
    (path.join("AGENTS.md").is_file() && path.join(".podarcis").join("config.yaml").is_file())
        || path.join("podarcis.yaml").is_file()
        || (path.join("wiki").is_dir() && path.join("workspace").is_dir())
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
        if is_root(&path) {
            return Ok(path);
        }
        // Check if pinned is a registered project name
        let reg = crate::project::ProjectRegistry::load();
        let name = pinned.to_string_lossy();
        if let Some(entry) = reg.projects.get(name.as_ref()) {
            if is_root(&entry.path) {
                return Ok(entry.path.clone());
            }
        }
        bail!(
            "not a Podarcis checkout or project at {} (no AGENTS.md + .podarcis/config.yaml or podarcis.yaml). Pass --root.",
            path.display()
        );
    }

    let start = absolutize(cwd, cwd);
    let mut probe: Option<&Path> = Some(&start);
    while let Some(dir) = probe {
        if is_root(dir) {
            return Ok(dir.to_path_buf());
        }
        probe = dir.parent();
    }

    // Fall back to active project in global registry
    let reg = crate::project::ProjectRegistry::load();
    if let Ok(proj) = reg.resolve(None, cwd, env_root) {
        if is_root(&proj.root) {
            return Ok(proj.root);
        }
    }

    bail!("not a Podarcis checkout (no AGENTS.md + .podarcis/config.yaml or podarcis.yaml). Pass --root.")
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
    pub inspector_height: u16,
    pub sidebar_open: bool,
    pub tree_open: bool,
    /// Why semantic search is unavailable, phrased as the fix, or `None` when
    /// it works. We say this rather than spawning a search that will fail.
    pub qmd_off_reason: Option<&'static str>,
    /// `repositories:` map from collection name to git URL, `local`, or `gdrive`.
    pub repositories: HashMap<String, String>,
    /// `oneliners:` splash lines shown in the status bar's right corner.
    pub oneliners: Vec<String>,
}

impl Config {
    pub fn load(root: &Path) -> Self {
        Self::load_with_herdr_theme(root, read_herdr_theme())
    }

    pub fn load_with_herdr_theme(root: &Path, herdr_flavor: Option<Flavor>) -> Self {
        Self::load_parts(root, herdr_flavor, crate::search::qmd_on_path())
    }

    /// `qmd_present` is injected so the tests are not at the mercy of whatever
    /// happens to be installed on the machine running them.
    fn load_parts(root: &Path, herdr_flavor: Option<Flavor>, qmd_present: bool) -> Self {
        let pod_yaml = root.join("podarcis.yaml");
        let cfg_yaml = root.join(".podarcis").join("config.yaml");
        let doc_path = if pod_yaml.is_file() { pod_yaml } else { cfg_yaml };
        let doc = std::fs::read_to_string(&doc_path)
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
                .or(herdr_flavor)
                .unwrap_or_default(),
            tree_width: clamp_width(tui.and_then(|t| t.get("tree_width")), 30, 16, 80),
            sidebar_width: clamp_width(tui.and_then(|t| t.get("sidebar_width")), 42, 20, 120),
            inspector_height: clamp_width(tui.and_then(|t| t.get("inspector_height")), 10, 4, 40),
            sidebar_open: tui.and_then(|t| t.get("sidebar")).and_then(Value::as_bool).unwrap_or(true),
            tree_open: tui.and_then(|t| t.get("tree")).and_then(Value::as_bool).unwrap_or(true),
            qmd_off_reason: match doc.get("engines").and_then(|e| e.get("qmd")).and_then(Value::as_bool) {
                Some(true) => None,
                Some(false) => Some("semantic search is off (engines.qmd: false in .podarcis/config.yaml)"),
                // An absent key is not a decision. Ask the filesystem instead
                // of reporting a config line the user never wrote.
                None if qmd_present => None,
                None => Some(
                    "semantic search needs the qmd binary on PATH — install it, or set engines.qmd: true in .podarcis/config.yaml",
                ),
            },
            repositories: doc
                .get("repositories")
                .and_then(Value::as_mapping)
                .map(|m| {
                    m.iter()
                        .filter_map(|(k, v)| {
                            Some((k.as_str()?.to_string(), v.as_str()?.to_string()))
                        })
                        .collect()
                })
                .unwrap_or_default(),
            oneliners: doc
                .get("oneliners")
                .and_then(Value::as_sequence)
                .map(|seq| {
                    seq.iter()
                        .filter_map(Value::as_str)
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default(),
        }
    }

    /// Can a semantic search actually run?
    pub fn qmd_enabled(&self) -> bool {
        self.qmd_off_reason.is_none()
    }

    /// A random splash line from `oneliners:`, or `None` when none are set.
    pub fn oneline(&self) -> Option<String> {
        if self.oneliners.is_empty() {
            return None;
        }
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.subsec_nanos()) as usize;
        Some(self.oneliners[nanos % self.oneliners.len()].clone())
    }

    pub fn repo_url(&self, name: &str) -> Option<&str> {
        self.repositories.get(name).map(String::as_str)
    }

    /// The three content repositories, in the order they are shown.
    pub fn collections(&self) -> Vec<(&'static str, PathBuf)> {
        [("wiki", "wiki"), ("workspace", "workspace"), ("sources", "sources")]
            .into_iter()
            .map(|(label, dir)| (label, self.root.join(dir)))
            .filter(|(_, path)| path.is_dir())
            .collect()
    }

    /// Persist the current sidebar and tree widths to `.podarcis/config.yaml`.
    /// Persist the settings the app itself can change: pane geometry and the
    /// theme.
    ///
    /// This is the one place the front-end writes `config.yaml`, and it rewrites
    /// exactly the `tui:` block as text, leaving every other byte of the file
    /// alone. Round-tripping the document through a YAML serializer instead
    /// looks tidier and is wrong: it re-quotes values the Python engine cares
    /// about — `last_run: '2026-09-06T11:01:50+00:00'` comes back unquoted and
    /// PyYAML then loads a `datetime`, which `podarcis status --json` cannot
    /// serialize.
    pub fn save_tui(&self) -> Result<()> {
        let block = format!(
            "tui:\n  tree_width: {}\n  sidebar_width: {}\n  inspector_height: {}\n  theme: {}\n",
            self.tree_width,
            self.sidebar_width,
            self.inspector_height,
            self.flavor.as_str(),
        );
        self.rewrite_block("tui", &block)
    }

    /// Persist `repositories:` — set locally by the repo-config overlay
    /// (mirrors `repos.py::set_repo_url`, previously reached only through
    /// `podarcis config repo`). Same text-preserving rewrite as `save_tui`.
    pub fn save_repositories(&self) -> Result<()> {
        let mut names: Vec<&String> = self.repositories.keys().collect();
        names.sort();
        let mut block = String::from("repositories:\n");
        for name in names {
            block.push_str(&format!("  {name}: {}\n", self.repositories[name]));
        }
        self.rewrite_block("repositories", &block)
    }

    /// Read-modify-write `config.yaml`, replacing exactly one top-level block
    /// as text. This is the one place the front-end writes `config.yaml`,
    /// and it never round-trips the whole document through a YAML
    /// serializer — that would re-quote values the Python engine cares
    /// about (e.g. `last_run: '2026-09-06T11:01:50+00:00'` comes back
    /// unquoted and PyYAML then loads a `datetime`, which
    /// `podarcis status --json` cannot serialize).
    fn rewrite_block(&self, key: &str, block: &str) -> Result<()> {
        let dir = self.root.join(".podarcis");
        std::fs::create_dir_all(&dir)?;
        let path = dir.join("config.yaml");
        let current = std::fs::read_to_string(&path).unwrap_or_default();
        std::fs::write(&path, with_block(&current, key, block))?;
        Ok(())
    }
}

/// Replace (or append) the top-level `<key>:` block in a config document.
///
/// `pub` so the CLI can write engine settings the same way without ever
/// round-tripping the document through a YAML serializer — see `rewrite_block`
/// for why that would corrupt `last_run:` timestamps.
pub fn with_block(current: &str, key: &str, block: &str) -> String {
    let mut out = String::with_capacity(current.len() + block.len());
    let mut lines = current.lines().peekable();
    let mut replaced = false;
    while let Some(line) = lines.next() {
        let is_target_key = line.trim_end() == format!("{key}:")
            || line.starts_with(&format!("{key}: "))
            || line.starts_with(&format!("{key}:\t"));
        if !is_target_key {
            out.push_str(line);
            out.push('\n');
            continue;
        }
        // Swallow the old block: its own line plus every indented or blank
        // line under it, up to the next top-level key.
        out.push_str(block);
        replaced = true;
        while let Some(next) = lines.peek() {
            if next.starts_with([' ', '\t']) || next.trim().is_empty() {
                lines.next();
            } else {
                break;
            }
        }
    }
    if !replaced {
        if !out.is_empty() && !out.ends_with('\n') {
            out.push('\n');
        }
        out.push_str(block);
    }
    out
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

/// Read the active theme configured in herdr's user config (`~/.config/herdr/config.toml`).
pub fn read_herdr_theme() -> Option<Flavor> {
    let path = herdr_user_config_path()?;
    let content = std::fs::read_to_string(path).ok()?;
    parse_herdr_theme(&content)
}

/// Locate herdr's user configuration, respecting `$HERDR_CONFIG_PATH` and `$XDG_CONFIG_HOME`.
/// Ignores paths pointing into the embedded `podarcis` session directory so we don't
/// read back our own provisioned session config.
pub fn herdr_user_config_path() -> Option<PathBuf> {
    if let Some(env_path) = std::env::var_os("HERDR_CONFIG_PATH").map(PathBuf::from) {
        if env_path.is_file() && !env_path.to_string_lossy().contains("sessions/podarcis") {
            return Some(env_path);
        }
    }
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))?;
    let candidate = base.join("herdr").join("config.toml");
    candidate.is_file().then_some(candidate)
}

/// Extract `name = "..."` under the `[theme]` section in a Herdr config file.
pub fn parse_herdr_theme(content: &str) -> Option<Flavor> {
    let mut in_theme = false;
    for line in content.lines() {
        let line = line.trim();
        if line.starts_with('#') || line.is_empty() {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            in_theme = line == "[theme]";
            continue;
        }
        if in_theme {
            if let Some(rest) = line.strip_prefix("name") {
                let rest = rest.trim();
                if let Some(val) = rest.strip_prefix('=') {
                    let val = val.trim().trim_matches('"').trim_matches('\'');
                    if let Some(flavor) = Flavor::parse(val) {
                        return Some(flavor);
                    }
                }
            }
        }
    }
    None
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
        let cfg = Config::load_with_herdr_theme(&dir, None);
        assert_eq!(cfg.flavor, Flavor::Latte);
        assert!(cfg.qmd_enabled());
        assert_eq!(cfg.tree_width, 30);

        std::fs::write(
            dir.join(".podarcis").join("config.yaml"),
            "engines:\n  qmd: false\ntui:\n  theme: catppuccin-mocha\n  tree_width: 9999\n  sidebar: false\n",
        )
        .unwrap();
        let cfg = Config::load_with_herdr_theme(&dir, None);
        assert_eq!(cfg.flavor, Flavor::Mocha);
        assert!(!cfg.qmd_enabled());
        assert_eq!(cfg.tree_width, 80, "widths are clamped, not trusted");
        assert!(!cfg.sidebar_open);
        assert!(cfg.repositories.is_empty());

        std::fs::write(
            dir.join(".podarcis").join("config.yaml"),
            "repositories:\n  wiki: git@example.com:w.git\n  sources: local\n",
        )
        .unwrap();
        let cfg = Config::load_with_herdr_theme(&dir, None);
        assert_eq!(cfg.repo_url("wiki"), Some("git@example.com:w.git"));
        assert_eq!(cfg.repo_url("sources"), Some("local"));
    }

    #[test]
    fn herdr_theme_syncs_and_respects_wiki_override() {
        let dir = scratch("herdr-sync");
        // No tui.theme in config.yaml -> inherits herdr's theme
        let cfg = Config::load_with_herdr_theme(&dir, Some(Flavor::TokyoNight));
        assert_eq!(cfg.flavor, Flavor::TokyoNight);

        // Explicit tui.theme in config.yaml overrides herdr's theme
        std::fs::write(
            dir.join(".podarcis").join("config.yaml"),
            "tui:\n  theme: dracula\n",
        )
        .unwrap();
        let cfg = Config::load_with_herdr_theme(&dir, Some(Flavor::TokyoNight));
        assert_eq!(cfg.flavor, Flavor::Dracula);
    }

    #[test]
    fn oneliner_loads_from_config_and_picks_one() {
        let dir = scratch("oneliners");
        std::fs::write(
            dir.join(".podarcis").join("config.yaml"),
            "oneliners:\n  - first one\n  - second one\n",
        )
        .unwrap();
        let cfg = Config::load(&dir);
        let line = cfg.oneline().unwrap();
        assert!(line == "first one" || line == "second one");

        std::fs::write(dir.join(".podarcis").join("config.yaml"), "engines:\n  qmd: true\n").unwrap();
        let cfg = Config::load(&dir);
        assert!(cfg.oneline().is_none());
    }

    #[test]
    fn parses_herdr_theme_from_toml() {
        let toml1 = "[theme]\nname = \"tokyo-night\"\nauto_switch = false\n";
        assert_eq!(parse_herdr_theme(toml1), Some(Flavor::TokyoNight));

        let toml2 = "[theme]\n# name = \"nord\"\nname = 'gruvbox'\n";
        assert_eq!(parse_herdr_theme(toml2), Some(Flavor::Gruvbox));

        let toml3 = "[other]\nname = \"dracula\"\n";
        assert_eq!(parse_herdr_theme(toml3), None);

        let toml4 = "[theme]\nname = \"terminal\"\n";
        assert_eq!(parse_herdr_theme(toml4), Some(Flavor::Terminal));
    }

    #[test]
    fn saving_the_tui_block_leaves_every_other_line_byte_for_byte() {
        // The regression this guards: a YAML round-trip unquoted
        // `last_run: '2026-…+00:00'`, PyYAML then read a datetime, and
        // `podarcis status --json` stopped working.
        let dir = scratch("save-tui");
        let original = concat!(
            "repositories:\n  wiki: git@example.com:w.git\n",
            "jobs:\n  audit_wiki:\n    last_run: '2026-09-06T11:01:50.186301+00:00'\n",
            "engines:\n  qmd: true\n",
        );
        std::fs::write(dir.join(".podarcis").join("config.yaml"), original).unwrap();

        let mut cfg = Config::load(&dir);
        cfg.tree_width = 31;
        cfg.flavor = Flavor::Mocha;
        cfg.save_tui().unwrap();

        let after = std::fs::read_to_string(dir.join(".podarcis").join("config.yaml")).unwrap();
        assert!(after.contains("last_run: '2026-09-06T11:01:50.186301+00:00'"), "{after}");
        assert!(after.contains("  wiki: git@example.com:w.git"));
        assert!(after.contains("tui:\n  tree_width: 31"));
        assert!(after.contains("  theme: mocha"));

        // And it round-trips: loading gives back what was saved.
        let reloaded = Config::load(&dir);
        assert_eq!(reloaded.tree_width, 31);
        assert_eq!(reloaded.flavor, Flavor::Mocha);
    }

    #[test]
    fn saving_twice_does_not_stack_up_tui_blocks() {
        let dir = scratch("save-twice");
        let mut cfg = Config::load(&dir);
        cfg.sidebar_width = 44;
        cfg.save_tui().unwrap();
        cfg.sidebar_width = 55;
        cfg.save_tui().unwrap();

        let after = std::fs::read_to_string(dir.join(".podarcis").join("config.yaml")).unwrap();
        assert_eq!(after.matches("tui:").count(), 1, "{after}");
        assert!(after.contains("sidebar_width: 55"));
        assert!(!after.contains("sidebar_width: 44"));
    }

    #[test]
    fn a_tui_block_in_the_middle_is_replaced_in_place() {
        let dir = scratch("save-middle");
        std::fs::write(
            dir.join(".podarcis").join("config.yaml"),
            "frontend: tui\ntui:\n  tree_width: 10\n\n  sidebar_width: 10\nengines:\n  qmd: true\n",
        )
        .unwrap();
        let mut cfg = Config::load(&dir);
        cfg.tree_width = 40;
        cfg.save_tui().unwrap();

        let after = std::fs::read_to_string(dir.join(".podarcis").join("config.yaml")).unwrap();
        assert!(after.starts_with("frontend: tui\n"));
        assert!(after.contains("engines:\n  qmd: true"), "the key after it survives: {after}");
        assert!(after.contains("tree_width: 40"));
        assert!(!after.contains("tree_width: 10"));
    }

    #[test]
    fn missing_config_never_panics() {
        let dir = std::env::temp_dir().join(format!("podarcis-empty-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let cfg = Config::load_with_herdr_theme(&dir, None);
        assert_eq!(cfg.flavor, Flavor::Latte);
    }

    #[test]
    fn an_absent_engines_qmd_follows_the_binary_and_explains_itself() {
        let dir = std::env::temp_dir().join(format!("podarcis-noqmd-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();

        // No key, no binary: the reason names the binary, not a config line the
        // user never wrote.
        let cfg = Config::load_parts(&dir, None, false);
        assert!(!cfg.qmd_enabled());
        assert!(cfg.qmd_off_reason.unwrap().contains("qmd binary on PATH"));

        // No key, binary present: nothing to enable.
        assert!(Config::load_parts(&dir, None, true).qmd_enabled());

        // An explicit `false` is a decision, and is reported as one even when
        // the binary is right there.
        std::fs::write(dir.join("podarcis.yaml"), "engines:\n  qmd: false\n").unwrap();
        let cfg = Config::load_parts(&dir, None, true);
        assert!(!cfg.qmd_enabled());
        assert!(cfg.qmd_off_reason.unwrap().contains("engines.qmd: false"));

        // An explicit `true` wins over a missing binary: the run then fails
        // with qmd's own error, which is accurate.
        std::fs::write(dir.join("podarcis.yaml"), "engines:\n  qmd: true\n").unwrap();
        assert!(Config::load_parts(&dir, None, false).qmd_enabled());
        std::fs::remove_file(dir.join("podarcis.yaml")).unwrap();
    }

    #[test]
    fn save_repositories_persists_and_preserves_other_keys() {
        let dir = scratch("save-repos");
        std::fs::write(
            dir.join(".podarcis").join("config.yaml"),
            "repositories:\n  wiki: old-url\n\
             jobs:\n  audit_wiki:\n    last_run: '2026-09-06T11:01:50.186301+00:00'\n\
             engines:\n  qmd: true\n",
        )
        .unwrap();

        let mut cfg = Config::load(&dir);
        cfg.repositories.insert("wiki".into(), "git@example.com:w.git".into());
        cfg.repositories.insert("sources".into(), "gdrive".into());
        cfg.save_repositories().unwrap();

        let after = std::fs::read_to_string(dir.join(".podarcis").join("config.yaml")).unwrap();
        assert!(after.contains("last_run: '2026-09-06T11:01:50.186301+00:00'"), "{after}");
        assert!(after.contains("  wiki: git@example.com:w.git"));
        assert!(after.contains("  sources: gdrive"));
        assert!(!after.contains("old-url"));

        let reloaded = Config::load(&dir);
        assert_eq!(reloaded.repo_url("wiki"), Some("git@example.com:w.git"));
        assert_eq!(reloaded.repo_url("sources"), Some("gdrive"));
        assert!(reloaded.qmd_enabled());
    }

    #[test]
    fn save_widths_persists_to_yaml_and_preserves_other_keys() {
        let dir = scratch("save-widths");
        let mut cfg = Config::load_with_herdr_theme(&dir, None);
        assert_eq!(cfg.tree_width, 30);
        cfg.tree_width = 48;
        cfg.sidebar_width = 64;
        cfg.save_tui().unwrap();

        let reloaded = Config::load_with_herdr_theme(&dir, None);
        assert_eq!(reloaded.tree_width, 48);
        assert_eq!(reloaded.sidebar_width, 64);
        assert!(reloaded.qmd_enabled(), "engines.qmd should be preserved");
    }
}
