//! Engine-side platform logic for the `podarcis` CLI.
//!
//! Everything `podarcis status` / `config` / `repo` needs that has no home in
//! the front-end-only modules: config-file access with the legacy `state.yaml`
//! fold-in, component discovery (MCP modules, skills, personas), external-skill
//! executable resolution, jobs, and repository status.
//!
//! Writes go through `config::with_block` — text-preserving single-block
//! rewrites. Round-tripping the document through a YAML serializer re-quotes
//! values the Python engine cares about: `last_run: '…'` comes back unquoted
//! and PyYAML then loads a `datetime`, which `podarcis status --json` cannot
//! serialize. Never round-trip `config.yaml` here.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::Result;
use serde_json::{json, Value};
use serde_yaml_ng::Value as Yaml;

use crate::config::{self, Config};

fn read_yaml(path: &Path) -> Yaml {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_yaml_ng::from_str::<Yaml>(&raw).ok())
        .unwrap_or(Yaml::Null)
}

/// `.podarcis/config.yaml` — there is no second config file.
fn config_path(root: &Path) -> PathBuf {
    root.join(".podarcis").join("config.yaml")
}

/// Fold a legacy `.podarcis/state.yaml` into `config.yaml`, once per process.
/// state.yaml's merged values win; the file is then removed.
fn migrate_legacy_state(root: &Path) {
    use std::sync::OnceLock;
    static MIGRATED: OnceLock<Vec<PathBuf>> = OnceLock::new();
    let migrated = MIGRATED.get_or_init(Vec::new);
    if migrated.contains(&root.to_path_buf()) {
        return;
    }
    let path = config_path(root);
    let state = root.join(".podarcis").join("state.yaml");
    if !state.exists() {
        return;
    }
    let Ok(st_raw) = std::fs::read_to_string(&state) else { return };
    let Ok(Yaml::Mapping(st)) = serde_yaml_ng::from_str::<Yaml>(&st_raw) else { return };
    let mut base = match read_yaml(&path) {
        Yaml::Mapping(map) => map,
        _ => serde_yaml_ng::Mapping::new(),
    };
    for (k, v) in st.iter() {
        base.insert(k.clone(), v.clone());
    }
    std::fs::create_dir_all(path.parent().unwrap()).ok();
    let doc = serde_yaml_ng::to_string(&Yaml::Mapping(base)).unwrap_or_default();
    let _ = std::fs::write(&path, doc);
    let _ = std::fs::remove_file(&state);
}

/// Load `config.yaml` (migrating `state.yaml` first) as a plain YAML value.
pub fn load_config(root: &Path) -> Yaml {
    migrate_legacy_state(root);
    read_yaml(&config_path(root))
}

/// A nested string value, or `default` when missing or non-string.
pub fn get_str(root: &Path, default: &str, keys: &[&str]) -> String {
    let mut val = load_config(root);
    for k in keys {
        let Some(next) = val.get(*k) else { return default.to_string() };
        val = next.clone();
    }
    match val {
        Yaml::String(s) if !s.is_empty() => s,
        _ => default.to_string(),
    }
}

/// Read-modify-write one top-level block of `config.yaml` as text.
fn write_block(root: &Path, key: &str, block: &str) -> Result<()> {
    migrate_legacy_state(root);
    let dir = root.join(".podarcis");
    std::fs::create_dir_all(&dir)?;
    let path = config_path(root);
    let current = std::fs::read_to_string(&path).unwrap_or_default();
    std::fs::write(&path, config::with_block(&current, key, block))?;
    Ok(())
}

pub fn frontend(root: &Path) -> String {
    get_str(root, "none", &["frontend"])
}

pub fn set_frontend(root: &Path, name: &str) -> Result<()> {
    write_block(root, "frontend", &format!("frontend: {name}\n"))
}

/// Enable or disable an MCP tool module under the `mcp_modules:` override
/// section. Mirrors `components.set_mcp_server_status`.
pub fn set_mcp_enabled(root: &Path, name: &str, enabled: bool) -> Result<()> {
    let clean = name.strip_suffix("-mcp").unwrap_or(name);
    let cfg = load_config(root);
    let section = cfg.get("mcp_modules").cloned().unwrap_or(Yaml::Null);
    let mut entries: Vec<String> = section
        .as_mapping()
        .map(|m| m.keys().filter_map(|k| k.as_str().map(str::to_string)).collect())
        .unwrap_or_default();
    if !entries.iter().any(|k| k == clean) {
        entries.push(clean.to_string());
    }
    entries.sort();
    let mut block = String::from("mcp_modules:\n");
    for key in entries {
        block.push_str(&format!("  {key}: {{enabled: {enabled}}}\n"));
    }
    write_block(root, "mcp_modules", &block)
}

#[cfg(test)]
fn repos_map(root: &Path) -> HashMap<String, String> {
    Config::load(root).repositories
}

/// Persist a repository URL under `repositories:`. Mirrors `repos.set_repo_url`.
pub fn set_repo_url_config(root: &Path, name: &str, url: &str) -> Result<()> {
    let mut repos = Config::load(root).repositories;
    if url.trim().is_empty() {
        repos.remove(name);
    } else {
        repos.insert(name.to_string(), url.trim().to_string());
    }
    let mut names: Vec<&String> = repos.keys().collect();
    names.sort();
    let mut block = String::from("repositories:\n");
    for n in names {
        block.push_str(&format!("  {n}: {}\n", repos[n]));
    }
    write_block(root, "repositories", &block)
}

// ------------------------------------------------------------ component files

fn read_dir_names(dir: &Path) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = std::fs::read_dir(dir)
        .map(|rd| rd.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect())
        .unwrap_or_default();
    out.sort();
    out
}

fn count_tokens(text: &str) -> i64 {
    if text.is_empty() { 0 } else { (text.len() as i64 / 4).max(1) }
}

/// Port of `components.count_mcp_tokens`: tool handlers, registered names, and
/// docstrings, weighted like the engine.
fn count_mcp_tokens(content: &str) -> i64 {
    let tool_re = regex::Regex::new(
        r"@(?:mcp|app)\.tool\(.*?\)\s*(?:async\s+)?def\s+([a-zA-Z0-9_]+)\((.*?)\):",
    )
    .unwrap();
    let name_re = regex::Regex::new(r#"name=[\x22']([a-zA-Z0-9_]+)[\x22']"#).unwrap();
    let doc_re = regex::Regex::new(r#""""(.*?)"""|'''(.*?)'''"#).unwrap();
    let mut pieces = Vec::new();
    for cap in tool_re.captures_iter(content) {
        pieces.push(format!("tool: {} ({})", &cap[1], cap.get(2).map_or("", |m| m.as_str()).trim()));
    }
    for cap in name_re.captures_iter(content) {
        pieces.push(format!("tool: {}", &cap[1]));
    }
    for cap in doc_re.captures_iter(content) {
        let text = cap.get(1).or_else(|| cap.get(2)).map_or("", |m| m.as_str()).trim();
        if !text.is_empty() {
            pieces.push(text.to_string());
        }
    }
    let joined = pieces.join("\n");
    if joined.trim().is_empty() {
        (count_tokens(content) * 35 / 100).max(100)
    } else {
        count_tokens(&joined)
    }
}

fn frontmatter_enabled(content: &str, flags: &[&str]) -> bool {
    if !content.trim_start().starts_with("---") {
        return true;
    }
    let Some(parts) = content.trim_start().strip_prefix("---").and_then(|c| c.split_once("---")) else {
        return true;
    };
    for line in parts.0.lines() {
        let clean = line.trim().to_ascii_lowercase();
        for flag in flags {
            if clean.contains(flag) && clean.contains("true") {
                return false;
            }
        }
    }
    true
}

// ---------------------------------------------------------------- discovery

#[derive(Debug)]
pub struct McpInfo {
    pub dir_name: String,
    pub tokens: i64,
}

#[derive(Debug)]
pub struct SkillInfo {
    pub enabled: bool,
    pub tokens: i64,
}

#[derive(Debug)]
pub struct AgentInfo {
    pub enabled: bool,
    pub tokens: i64,
}

#[derive(Debug, Default)]
pub struct Components {
    pub mcp: BTreeMap<String, McpInfo>,
    pub skills: BTreeMap<String, SkillInfo>,
    pub agents: BTreeMap<String, AgentInfo>,
}

/// Discover the on-disk MCP modules, skills, and personas.
/// Mirrors `components.discover_components` (minus the mtime token cache: an
/// `Object of type datetime is not JSON serializable`-style optimization that
/// recomputes deterministically anyway).
pub fn discover_components(root: &Path) -> Components {
    let mut out = Components::default();

    let mcp_dir = root.join(".agents").join("mcp");
    for d in read_dir_names(&mcp_dir) {
        let server = d.join("server.py");
        if !server.is_file() {
            continue;
        }
        let content = std::fs::read_to_string(&server).unwrap_or_default();
        let key = if d.file_name().unwrap().to_string_lossy().ends_with("-mcp") {
            d.file_name().unwrap().to_string_lossy().into_owned()
        } else {
            format!("{}-mcp", d.file_name().unwrap().to_string_lossy())
        };
        out.mcp.insert(
            key,
            McpInfo { dir_name: d.file_name().unwrap().to_string_lossy().into_owned(), tokens: count_mcp_tokens(&content) },
        );
    }

    let skills_dir = root.join(".apm").join("skills");
    for d in read_dir_names(&skills_dir) {
        let file = d.join("SKILL.md");
        let content = std::fs::read_to_string(&file).unwrap_or_default();
        let name = d.file_name().unwrap().to_string_lossy().into_owned();
        out.skills.insert(
            name,
            SkillInfo { enabled: frontmatter_enabled(&content, &["disable-model-invocation:", "user-invocable:"]), tokens: count_tokens(&content) },
        );
    }

    let agents_dir = root.join(".apm").join("agents");
    if let Ok(rd) = std::fs::read_dir(&agents_dir) {
        let mut files: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("agent.md"))
            .collect();
        files.sort();
        for f in files {
            let content = std::fs::read_to_string(&f).unwrap_or_default();
            let name = f.file_stem().unwrap().to_string_lossy().replace(".agent", "");
            out.agents.insert(
                name,
                AgentInfo { enabled: frontmatter_enabled(&content, &["disable-model-invocation:", "user-invocable:", "disabled:"]), tokens: count_tokens(&content) },
            );
        }
    }
    out
}

/// Active MCP module identifiers, both bare and `-mcp` suffixed. Mirrors the
/// gateway router rule: whatever is on disk is active unless `mcp_modules:`
/// explicitly disables it.
pub fn enabled_mcp_servers(root: &Path, components: &Components) -> Vec<String> {
    let section = load_config(root).get("mcp_modules").cloned().unwrap_or(Yaml::Null);
    let mut out = Vec::new();
    for (name, _) in &components.mcp {
        let bare = name.strip_suffix("-mcp").unwrap_or(name);
        let enabled = match section.get(bare) {
            Some(Yaml::Bool(b)) => *b,
            Some(Yaml::Mapping(m)) => match m.get(Yaml::String("enabled".into())) {
                Some(Yaml::Bool(b)) => *b,
                _ => true,
            },
            _ => true,
        };
        if enabled {
            for alias in [bare.to_string(), name.clone()] {
                out.push(alias);
            }
        }
    }
    out
}

// ----------------------------------------------------------- external skills

fn declared_scripts(dir: &Path) -> Vec<String> {
    let mut names = Vec::new();
    let pyproject = dir.join("pyproject.toml");
    if pyproject.is_file() {
        if let Ok(raw) = std::fs::read_to_string(&pyproject) {
            if let Ok(doc) = raw.parse::<toml::Table>() {
                if let Some(scripts) = doc.get("project").and_then(|p| p.get("scripts")).and_then(|s| s.as_table()) {
                    names.extend(scripts.keys().cloned());
                }
            }
        }
    }
    let package = dir.join("package.json");
    if package.is_file() {
        if let Ok(raw) = std::fs::read_to_string(&package) {
            if let Ok(v) = serde_json::from_str::<Value>(&raw) {
                if let Some(bin) = v.get("bin") {
                    if bin.as_str().is_some() {
                        names.push(
                            v.get("name")
                                .and_then(Value::as_str)
                                .map(str::to_string)
                                .unwrap_or_else(|| dir.file_name().unwrap().to_string_lossy().into_owned()),
                        );
                    } else if let Some(map) = bin.as_object() {
                        names.extend(map.keys().cloned());
                    }
                }
            }
        }
    }
    names.sort();
    names.dedup();
    names
}

fn resolve_executable(root: &Path, name: &str) -> String {
    let venv = root.join(".venv").join("bin").join(name);
    if venv.is_file() {
        return venv.to_string_lossy().into_owned();
    }
    if let Some(path) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&path) {
            let probe = dir.join(name);
            if probe.is_file() {
                return probe.to_string_lossy().into_owned();
            }
        }
    }
    String::new()
}

/// Skills APM deployed that this repo does not author, mapped to their
/// executables. Mirrors `components.external_skills`.
pub fn external_skills(root: &Path) -> BTreeMap<String, BTreeMap<String, String>> {
    let authored: Vec<String> = read_dir_names(&root.join(".apm").join("skills"))
        .into_iter()
        .filter_map(|d| d.file_name().map(|n| n.to_string_lossy().into_owned()))
        .collect();
    let mut found: BTreeMap<String, BTreeMap<String, String>> = BTreeMap::new();
    for deploy in [root.join(".agents").join("skills"), root.join(".claude").join("skills")] {
        for d in read_dir_names(&deploy) {
            let name = d.file_name().unwrap().to_string_lossy().into_owned();
            if authored.contains(&name) || found.contains_key(&name) {
                continue;
            }
let exes: BTreeMap<String, String> = declared_scripts(&d)
            .into_iter()
            .map(|n| (n.clone(), resolve_executable(root, &n)))
            .collect();
        found.insert(name, exes);
        }
    }
    found
}

// ---------------------------------------------------------------- repository

/// Port of `repos.get_repo_status`: git state per configured repo.
pub fn repo_status(root: &Path, repos: &HashMap<String, String>) -> Vec<Value> {
    let mut out = Vec::new();
    for name in crate::vault::git::repo_names(repos) {
        let dir = root.join(&name);
        let url = repos.get(&name).cloned().unwrap_or_default();
        let mut info = json!({
            "repo": name,
            "path": dir.to_string_lossy(),
            "url": if url.is_empty() { "local" } else { url.as_str() },
            "type": "git",
            "exists": dir.is_dir(),
            "is_git": dir.join(".git").exists(),
            "branch": Value::Null,
            "clean": true,
            "ahead": 0,
            "behind": 0,
            "changes": 0,
            "status": "ready",
        });
        if url == "gdrive" {
            info["status"] = json!("gdrive_managed");
            out.push(info);
            continue;
        }
        if !info["exists"].as_bool().unwrap_or(false) || !info["is_git"].as_bool().unwrap_or(false) {
            info["status"] = json!("missing");
            out.push(info);
            continue;
        }
        let git = |args: &[&str]| {
            Command::new("git").args(args).current_dir(&dir).output()
        };
        if let Ok(o) = git(&["branch", "--show-current"]) {
            let branch = String::from_utf8_lossy(&o.stdout).trim().to_string();
            info["branch"] = json!(if branch.is_empty() { "HEAD" } else { branch.as_str() });
        }
        let changes = git(&["status", "--porcelain"])
            .map(|o| String::from_utf8_lossy(&o.stdout).lines().filter(|l| !l.trim().is_empty()).count())
            .unwrap_or(0);
        info["changes"] = json!(changes);
        info["clean"] = json!(changes == 0);
        let (mut ahead, mut behind) = (0, 0);
        if let Ok(o) = git(&["rev-list", "--left-right", "--count", "HEAD...@{u}"]) {
            if o.status.success() {
                let stdout = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if !stdout.is_empty() {
                let parts: Vec<&str> = stdout.split_whitespace().collect();
                if parts.len() == 2 {
                    ahead = parts[0].parse().unwrap_or(0);
                    behind = parts[1].parse().unwrap_or(0);
                }
            }
            }
        }
        info["ahead"] = json!(ahead);
        info["behind"] = json!(behind);
        info["status"] = if !info["clean"].as_bool().unwrap_or(true) {
            json!("modified")
        } else if ahead > 0 {
            json!("ahead")
        } else if behind > 0 {
            json!("behind")
        } else {
            json!("synced")
        };
        out.push(info);
    }
    out
}

// ---------------------------------------------------------------------- jobs

fn yaml_to_string(v: &Yaml) -> String {
    match v {
        Yaml::String(s) => s.clone(),
        Yaml::Bool(b) => b.to_string(),
        Yaml::Number(n) => n.to_string(),
        Yaml::Null => String::new(),
        other => serde_yaml_ng::to_string(other).unwrap_or_default().trim().to_string(),
    }
}

/// Discovered jobs, merged with runtime state from `config.yaml`'s `jobs:`
/// section. Every value is stringified so a `last_run` timestamp can never
/// become a Python `datetime` (the JSON-serialization bug this port fixes).
pub fn jobs(root: &Path) -> BTreeMap<String, Value> {
    let mut out = BTreeMap::new();
    let state = load_config(root).get("jobs").cloned().unwrap_or(Yaml::Null);
    let jdir = root.join(".agents").join("jobs");
    let mut files: Vec<PathBuf> = std::fs::read_dir(&jdir)
        .map(|rd| rd.flatten().map(|e| e.path()).filter(|p| p.extension().and_then(|e| e.to_str()) == Some("yaml")).collect())
        .unwrap_or_default();
    files.sort();
    for file in files {
        let data = read_yaml(&file);
        let Yaml::Mapping(map) = data else { continue };
        let name = map
            .get(Yaml::String("name".into()))
            .map(yaml_to_string)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| file.file_stem().unwrap().to_string_lossy().into_owned());
        let job_state = state.get(&name).cloned().unwrap_or(Yaml::Null);
        let st = |key: &str, default: &str| {
            match job_state.get(key) {
                Some(v) => yaml_to_string(v),
                None => yaml_to_string(&map.get(Yaml::String(key.into())).cloned().unwrap_or(Yaml::String(default.to_string()))),
            }
        };
        out.insert(
            name.clone(),
            json!({
                "enabled": st("enabled", "True") == "True",
                "schedule": st("schedule", "daily"),
                "description": map.get(Yaml::String("description".into())).map(yaml_to_string).unwrap_or_else(|| "Podarcis job".into()),
                "last_run": st("last_run", ""),
                "last_status": st("last_status", ""),
            }),
        );
    }
    out
}

// ------------------------------------------------------------ frontend binary

/// Resolve the front-end binary: `$PODARCIS_TUI_BIN`, then the crate's release
/// build, then debug, then `$PATH`. Mirrors `wiki.find_binary`.
pub fn find_frontend_binary(root: &Path) -> Option<PathBuf> {
    if let Ok(override_bin) = std::env::var("PODARCIS_TUI_BIN") {
        let path = PathBuf::from(override_bin.trim());
        if !override_bin.trim().is_empty() && path.is_file() {
            return Some(path);
        }
    }
    for profile in ["release", "debug"] {
        let candidate = root.join("tui").join("target").join(profile).join("podarcis-tui");
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path).map(|d| d.join("podarcis-tui")).find(|p| p.is_file())
    })
}

/// `podarcis-tui --version` output, last whitespace token. Mirrors `wiki.version`.
pub fn frontend_version(binary: &Path) -> Option<String> {
    let o = Command::new(binary).arg("--version").output().ok()?;
    if !o.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&o.stdout);
    text.split_whitespace().last().map(|s| s.to_string())
}

// -------------------------------------------------------------------- status

/// `podarcis status --json` payload. Every value is JSON-safe by construction.
pub fn status(root: &Path) -> Value {
    let cfg = Config::load(root);
    let components = discover_components(root);
    let enabled = enabled_mcp_servers(root, &components);
    let external = external_skills(root);
    let binary = find_frontend_binary(root);

    let mcp_servers: BTreeMap<String, Value> = components
        .mcp
        .iter()
        .map(|(key, info)| {
            let is_on = enabled.iter().any(|e| e == key);
            (
                key.clone(),
                json!({ "enabled": is_on, "tokens": info.tokens, "dir_name": info.dir_name }),
            )
        })
        .collect();
    let skills: BTreeMap<String, Value> = components
        .skills
        .iter()
        .map(|(k, v)| (k.clone(), json!({ "enabled": v.enabled, "tokens": v.tokens })))
        .collect();
    let agents: BTreeMap<String, Value> = components
        .agents
        .iter()
        .map(|(k, v)| (k.clone(), json!({ "enabled": v.enabled, "tokens": v.tokens })))
        .collect();
    let external_skills: BTreeMap<String, Value> = external
        .iter()
        .map(|(k, exes)| {
            let exes_v: BTreeMap<String, Value> =
                exes.iter().map(|(n, p)| (n.clone(), json!(p))).collect();
            (k.clone(), json!({ "executables": exes_v, "ok": exes.values().all(|p| !p.is_empty()) }))
        })
        .collect();
    let repositories: BTreeMap<String, Value> = cfg
        .repositories
        .keys()
        .cloned()
        .chain(crate::vault::git::DEFAULT_REPO_NAMES.iter().map(|s| s.to_string()))
        .map(|name| {
            let url = cfg.repositories.get(&name).cloned().unwrap_or_default();
            (name, json!({ "remote_url": url, "is_local_only": url.is_empty() }))
        })
        .collect();

    json!({
        "frontend": {
            "name": frontend(root),
            "binary": binary.as_ref().map(|b| b.to_string_lossy().into_owned()).unwrap_or_default(),
            "version": binary.as_ref().and_then(|b| frontend_version(b)).unwrap_or_default(),
            "built": binary.is_some(),
        },
        "mcp_servers": mcp_servers,
        "skills": skills,
        "agents": agents,
        "external_skills": external_skills,
        "jobs": jobs(root),
        "repositories": repositories,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("podarcis-platform-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir.join(".podarcis")).unwrap();
        dir
    }

    fn deploy(root: &Path, name: &str, pyproject: Option<&str>, package_json: Option<&str>) {
        let d = root.join(".agents").join("skills").join(name);
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("SKILL.md"), format!("---\nname: {name}\n---\n")).unwrap();
        if let Some(p) = pyproject {
            std::fs::write(d.join("pyproject.toml"), p).unwrap();
        }
        if let Some(pj) = package_json {
            std::fs::write(d.join("package.json"), pj).unwrap();
        }
    }

    #[test]
    fn external_skills_excludes_authored() {
        let root = root("authored");
        std::fs::create_dir_all(root.join(".apm").join("skills").join("mine")).unwrap();
        deploy(&root, "mine", None, None);
        deploy(&root, "theirs", None, None);
        assert_eq!(external_skills(&root).keys().next().map(String::as_str), Some("theirs"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn external_skills_parses_python_scripts_and_venv_preference() {
        let root = root("venv");
        let venv = root.join(".venv").join("bin");
        std::fs::create_dir_all(&venv).unwrap();
        std::fs::write(venv.join("thing"), "").unwrap();
        deploy(
            &root,
            "tool",
            Some("[project]\nname = \"tool\"\n[project.scripts]\nthing = \"tool.cli:main\"\nother = \"tool.cli:other\"\n"),
            None,
        );
        let exes = &external_skills(&root)["tool"];
        assert_eq!(exes.len(), 2);
        assert_eq!(exes["thing"], venv.join("thing").to_string_lossy());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn external_skills_string_bin_names_after_package() {
        let root = root("binstr");
        deploy(&root, "node-tool", None, Some(r#"{"name": "node-tool", "bin": "dist/index.js"}"#));
        let exes = &external_skills(&root)["node-tool"];
        assert!(exes.contains_key("node-tool"));
        assert!(!exes.contains_key("dist/index.js"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn missing_executable_resolves_to_empty() {
        let root = root("missing");
        deploy(&root, "tool", Some("[project]\n[project.scripts]\ndefinitely-not-on-this-system = \"x\"\n"), None);
        assert_eq!(&external_skills(&root)["tool"]["definitely-not-on-this-system"], "");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn state_migration_folds_state_winning() {
        let root = root("migrate");
        std::fs::write(
            root.join(".podarcis").join("config.yaml"),
            "sources_backend: local\napis:\n  k: v\nfrontend: none\n",
        )
        .unwrap();
        std::fs::write(root.join(".podarcis").join("state.yaml"), "frontend: tui\n").unwrap();
        assert_eq!(get_str(&root, "", &["frontend"]), "tui");
        assert!(!root.join(".podarcis").join("state.yaml").exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn set_frontend_rewrites_one_block_only() {
        let root = root("frontend");
        std::fs::write(
            root.join(".podarcis").join("config.yaml"),
            "sources_backend: local\nfrontend: none\nrepositories:\n  wiki: https://example.com\n",
        )
        .unwrap();
        set_frontend(&root, "obsidian").unwrap();
        let raw = std::fs::read_to_string(root.join(".podarcis").join("config.yaml")).unwrap();
        assert!(raw.contains("frontend: obsidian"));
        assert!(raw.contains("repositories:\n  wiki: https://example.com"), "{raw}");
        assert!(raw.contains("sources_backend: local"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn set_repo_url_config_persists_and_removes_empty() {
        let root = root("repourl");
        std::fs::write(root.join(".podarcis").join("config.yaml"), "frontend: none\n").unwrap();
        set_repo_url_config(&root, "wiki", "https://github.com/example/wiki.git").unwrap();
        assert_eq!(Config::load(&root).repositories["wiki"], "https://github.com/example/wiki.git");
        set_repo_url_config(&root, "wiki", "").unwrap();
        assert!(!Config::load(&root).repositories.contains_key("wiki"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn set_mcp_enabled_writes_override_and_gates_status() {
        let root = root("mcpon");
        std::fs::create_dir_all(root.join(".agents").join("mcp").join("drives")).unwrap();
        std::fs::write(root.join(".agents").join("mcp").join("drives").join("server.py"), "").unwrap();
        std::fs::write(root.join(".podarcis").join("config.yaml"), "frontend: none\n").unwrap();
        let components = discover_components(&root);
        assert!(enabled_mcp_servers(&root, &components).contains(&"drives-mcp".to_string()));
        set_mcp_enabled(&root, "drives-mcp", false).unwrap();
        assert!(!enabled_mcp_servers(&root, &components).contains(&"drives-mcp".to_string()));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn jobs_stringifies_timestamp_last_run() {
        let root = root("jobs");
        std::fs::create_dir_all(root.join(".agents").join("jobs")).unwrap();
        std::fs::write(
            root.join(".agents").join("jobs").join("audit.yaml"),
            "name: audit_wiki\nschedule: daily\ndescription: Audit\n",
        )
        .unwrap();
        std::fs::write(
            root.join(".podarcis").join("config.yaml"),
            "jobs:\n  audit_wiki:\n    last_run: 2026-09-06T11:01:50+00:00\n",
        )
        .unwrap();
        let jobs = jobs(&root);
        assert_eq!(jobs["audit_wiki"]["last_run"], "2026-09-06T11:01:50+00:00");
        assert!(serde_json::to_string(&jobs).is_ok(), "timestamp must stay JSON-serializable");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn status_payload_has_required_keys() {
        let root = root("status");
        std::fs::create_dir_all(root.join(".apm").join("skills").join("s")).unwrap();
        std::fs::write(root.join(".apm").join("skills").join("s").join("SKILL.md"), "---\ndescription: x\n---\n").unwrap();
        std::fs::create_dir_all(root.join(".agents").join("mcp").join("w")).unwrap();
        std::fs::write(root.join(".agents").join("mcp").join("w").join("server.py"), "").unwrap();
        std::fs::write(root.join(".podarcis").join("config.yaml"), "frontend: none\n").unwrap();
        let payload = status(&root);
        assert!(payload.get("mcp_servers").is_some());
        assert!(payload.get("skills").is_some());
        assert!(payload.get("agents").is_some());
        assert!(payload.get("external_skills").is_some());
        assert!(payload.get("repositories").is_some());
        assert!(serde_json::to_string(&payload).is_ok());
        let _ = std::fs::remove_dir_all(&root);
    }
}