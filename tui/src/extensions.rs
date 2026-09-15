//! Browsing and managing apm ([microsoft.github.io/apm](https://microsoft.github.io/apm))
//! dependencies — third-party skills and MCP servers declared in `apm.yml` —
//! from inside the TUI instead of a terminal.
//!
//! Every mutating action (install/update/uninstall) shells out to the real
//! `apm` binary, exactly as `search::run_qmd` shells out to `qmd`: this module
//! never reimplements apm's own dependency resolution. Only the browse list is
//! read directly from `apm.yml` and `apm.lock.yaml` (both YAML, lenient like
//! `Config::load` — a missing or malformed file just yields an empty list),
//! because apm's own list commands (`apm deps list`, `apm mcp list`) print
//! Rich-rendered tables with no machine-readable output worth parsing.

use std::path::Path;
use std::process::Command;

use serde_yaml_ng::Value;

/// Is an `apm` binary reachable at all?
pub fn apm_on_path() -> bool {
    crate::herdr::pty::which("apm").is_some()
}

#[derive(Clone, Debug)]
pub struct ExtensionItem {
    pub name: String,
    /// From `apm.lock.yaml`'s `package_type` (e.g. `claude_skill`), or
    /// `"not installed"` for a dependency declared in `apm.yml` but not yet
    /// resolved into the lockfile.
    pub package_type: String,
    /// Lockfile `host` (e.g. `github`, `gitlab`), or the raw `git:` URL for an
    /// unresolved dependency.
    pub source: String,
    pub version: String,
    /// The manifest identifier to pass to `apm update`/`apm uninstall` — the
    /// lockfile's `name`, or the raw `git:` URL for a declared-but-unlocked one.
    pub key: String,
}

/// Read `apm.yml` and `apm.lock.yaml` and join them into one browsable list:
/// every locked dependency, plus anything declared but not yet installed.
pub fn load(root: &Path) -> Vec<ExtensionItem> {
    let lock = read_yaml(&root.join("apm.lock.yaml"));
    let locked: Vec<&Value> = lock.get("dependencies").and_then(Value::as_sequence).into_iter().flatten().collect();
    let mut items: Vec<ExtensionItem> = locked
        .iter()
        .map(|dep| ExtensionItem {
            name: str_field(dep, "name"),
            package_type: str_field(dep, "package_type"),
            source: str_field(dep, "host"),
            version: str_field(dep, "version"),
            key: str_field(dep, "name"),
        })
        .collect();

    let manifest = read_yaml(&root.join("apm.yml"));
    for section in ["apm", "mcp"] {
        let Some(deps) = manifest.get("dependencies").and_then(|d| d.get(section)).and_then(Value::as_sequence)
        else {
            continue;
        };
        for dep in deps {
            let git = dep.get("git").and_then(Value::as_str).unwrap_or_default();
            if git.is_empty() {
                continue;
            }
            let alias = dep.get("alias").and_then(Value::as_str);
            let git_lower = git.to_lowercase();
            // A lockfile `repo_url` (lowercased by apm; `materialization_repo_url`
            // keeps the original case) is a fragment of the declaring `git:` URL
            // — that substring check is what apm's own resolver keys entries
            // by, so it survives an alias the manifest gives the dependency.
            let already_locked = locked.iter().any(|dep| {
                let repo_url = dep.get("repo_url").and_then(Value::as_str).unwrap_or_default();
                let materialized =
                    dep.get("materialization_repo_url").and_then(Value::as_str).unwrap_or_default();
                (!repo_url.is_empty() && git_lower.contains(&repo_url.to_lowercase()))
                    || (!materialized.is_empty() && git.contains(materialized))
                    || (alias.is_some() && dep.get("name").and_then(Value::as_str) == alias)
            });
            if already_locked {
                continue;
            }
            let name = alias.map(str::to_string).unwrap_or_else(|| repo_name(git));
            items.push(ExtensionItem {
                name,
                package_type: "not installed".into(),
                source: git.to_string(),
                version: "-".into(),
                key: git.to_string(),
            });
        }
    }
    items
}

/// Run `apm <args>` in `root`, capturing its exit code, stdout and stderr —
/// the shape `NativeOutcome` expects. Never fails silently: a missing binary
/// or spawn error becomes a non-zero exit with the reason in stderr.
pub fn run(root: &Path, args: &[&str]) -> (i32, String, String) {
    match Command::new("apm").args(args).current_dir(root).output() {
        Ok(out) => (
            out.status.code().unwrap_or(1),
            String::from_utf8_lossy(&out.stdout).into_owned(),
            String::from_utf8_lossy(&out.stderr).into_owned(),
        ),
        Err(err) => (1, String::new(), format!("could not run apm: {err}")),
    }
}

fn str_field(v: &Value, key: &str) -> String {
    v.get(key).and_then(Value::as_str).unwrap_or_default().to_string()
}

fn read_yaml(path: &Path) -> Value {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_yaml_ng::from_str::<Value>(&raw).ok())
        .unwrap_or(Value::Null)
}

/// The repo's own name from a git URL — `menumaker` from
/// `.../XicuM/menumaker.git#v1.0.0` — for a declared dependency with no alias
/// and no lockfile entry yet. A trailing `#<ref>` (apm's pin syntax) and
/// `.git` are both stripped before taking the last path segment.
fn repo_name(git: &str) -> String {
    let without_ref = git.split('#').next().unwrap_or(git);
    let trimmed = without_ref.trim_end_matches(".git").trim_end_matches('/');
    trimmed.rsplit('/').next().unwrap_or(trimmed).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repo_name_strips_the_git_suffix_and_a_pinned_ref() {
        assert_eq!(repo_name("https://github.com/XicuM/market-skill.git"), "market-skill");
        assert_eq!(repo_name("ssh://git@gitlab-internal.bsc.es/esantigo/overleaf-suggest"), "overleaf-suggest");
        assert_eq!(repo_name("https://github.com/XicuM/menumaker.git#v1.0.0"), "menumaker");
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("podarcis-extensions-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn load_is_lenient_about_missing_files() {
        let dir = scratch("empty");
        assert!(load(&dir).is_empty());
    }

    #[test]
    fn load_joins_locked_and_declared_only_dependencies() {
        let dir = scratch("join");
        std::fs::write(
            dir.join("apm.yml"),
            "dependencies:\n  apm:\n    - git: https://github.com/XicuM/market-skill.git\n    - git: https://github.com/XicuM/menumaker.git#v1.0.0\n  mcp: []\n",
        )
        .unwrap();
        std::fs::write(
            dir.join("apm.lock.yaml"),
            "dependencies:\n  - repo_url: XicuM/market-skill\n    name: market-skill\n    host: github\n    version: '1.0.0'\n    package_type: claude_skill\n",
        )
        .unwrap();
        let items = load(&dir);
        assert_eq!(items.len(), 2);
        assert!(items.iter().any(|i| i.name == "market-skill" && i.package_type == "claude_skill"));
        assert!(items.iter().any(|i| i.name == "menumaker" && i.package_type == "not installed"));
    }
}
