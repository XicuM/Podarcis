//! Multi-project management and XDG-compliant project resolution.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::Result;
use serde_yaml_ng::Value as Yaml;

/// Resolve Podarcis XDG configuration directory (~/.config/podarcis).
pub fn xdg_config_dir() -> PathBuf {
    if let Some(base) = std::env::var_os("XDG_CONFIG_HOME") {
        if !base.is_empty() {
            return PathBuf::from(base).join("podarcis");
        }
    }
    match std::env::var_os("HOME") {
        Some(home) => PathBuf::from(home).join(".config").join("podarcis"),
        None => PathBuf::from(".config").join("podarcis"),
    }
}

/// Resolve Podarcis XDG data directory (~/.local/share/podarcis).
pub fn xdg_data_dir() -> PathBuf {
    if let Some(base) = std::env::var_os("XDG_DATA_HOME") {
        if !base.is_empty() {
            return PathBuf::from(base).join("podarcis");
        }
    }
    match std::env::var_os("HOME") {
        Some(home) => PathBuf::from(home).join(".local").join("share").join("podarcis"),
        None => PathBuf::from(".local").join("share").join("podarcis"),
    }
}

/// Resolve Podarcis XDG cache directory (~/.cache/podarcis).
pub fn xdg_cache_dir() -> PathBuf {
    if let Some(base) = std::env::var_os("XDG_CACHE_HOME") {
        if !base.is_empty() {
            return PathBuf::from(base).join("podarcis");
        }
    }
    match std::env::var_os("HOME") {
        Some(home) => PathBuf::from(home).join(".cache").join("podarcis"),
        None => PathBuf::from(".cache").join("podarcis"),
    }
}

/// Default directory containing user research projects (~/.local/share/podarcis/projects).
pub fn projects_dir() -> PathBuf {
    xdg_data_dir().join("projects")
}

/// Path to global configuration file (~/.config/podarcis/config.yaml).
pub fn global_config_path() -> PathBuf {
    xdg_config_dir().join("config.yaml")
}

/// Check if a directory is a valid Podarcis project root.
pub fn is_project_root(path: &Path) -> bool {
    if !path.is_dir() {
        return false;
    }
    if path.join("podarcis.yaml").is_file() {
        return true;
    }
    if path.join(".podarcis").join("config.yaml").is_file() {
        return true;
    }
    if path.join("wiki").is_dir() && path.join("workspace").is_dir() {
        return true;
    }
    false
}

/// An individual research project workspace.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Project {
    pub name: String,
    pub root: PathBuf,
}

impl Project {
    pub fn new(name: impl Into<String>, root: impl Into<PathBuf>) -> Self {
        Self {
            name: name.into(),
            root: root.into(),
        }
    }

    pub fn wiki(&self) -> PathBuf {
        self.root.join("wiki")
    }

    pub fn workspace(&self) -> PathBuf {
        self.root.join("workspace")
    }

    pub fn sources(&self) -> PathBuf {
        self.root.join("sources")
    }

    pub fn config_path(&self) -> PathBuf {
        let p = self.root.join("podarcis.yaml");
        if p.is_file() {
            p
        } else {
            self.root.join(".podarcis").join("config.yaml")
        }
    }

    pub fn exists(&self) -> bool {
        self.root.is_dir()
    }

    /// Return the standard 3 collections (wiki, workspace, sources) for this project.
    pub fn collections(&self) -> Vec<(&'static str, PathBuf)> {
        [("wiki", self.wiki()), ("workspace", self.workspace()), ("sources", self.sources())]
            .into_iter()
            .filter(|(_, p)| p.is_dir())
            .collect()
    }
}

#[derive(Clone, Debug, Default)]
pub struct ProjectEntry {
    pub path: PathBuf,
    pub description: String,
}

/// Global registry of projects loaded from `~/.config/podarcis/config.yaml`.
#[derive(Clone, Debug, Default)]
pub struct ProjectRegistry {
    pub active: String,
    pub projects: HashMap<String, ProjectEntry>,
}

impl ProjectRegistry {
    pub fn load() -> Self {
        let mut reg = Self::load_from(&global_config_path());
        // Also discover any subdirectories in projects_dir()
        let p_dir = projects_dir();
        if let Ok(entries) = std::fs::read_dir(&p_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    let name = entry.file_name().to_string_lossy().to_string();
                    if !reg.projects.contains_key(&name) && is_project_root(&path) {
                        reg.projects.insert(
                            name,
                            ProjectEntry {
                                path,
                                description: String::new(),
                            },
                        );
                    }
                }
            }
        }
        reg
    }

    pub fn load_from(cfg_path: &Path) -> Self {
        let mut reg = Self {
            active: "default".to_string(),
            projects: HashMap::new(),
        };

        if let Ok(raw) = std::fs::read_to_string(cfg_path) {
            if let Ok(Yaml::Mapping(map)) = serde_yaml_ng::from_str::<Yaml>(&raw) {
                if let Some(Yaml::String(active)) = map.get(Yaml::String("active_project".to_string())) {
                    reg.active = active.clone();
                }
                if let Some(Yaml::Mapping(projs)) = map.get(Yaml::String("projects".to_string())) {
                    for (k, v) in projs {
                        if let Yaml::String(name) = k {
                            match v {
                                Yaml::String(p) => {
                                    reg.projects.insert(
                                        name.clone(),
                                        ProjectEntry {
                                            path: PathBuf::from(p),
                                            description: String::new(),
                                        },
                                    );
                                }
                                Yaml::Mapping(entry) => {
                                    let path_str = entry
                                        .get(Yaml::String("path".to_string()))
                                        .and_then(Yaml::as_str)
                                        .unwrap_or("");
                                    let desc = entry
                                        .get(Yaml::String("description".to_string()))
                                        .and_then(Yaml::as_str)
                                        .unwrap_or("");
                                    if !path_str.is_empty() {
                                        reg.projects.insert(
                                            name.clone(),
                                            ProjectEntry {
                                                path: PathBuf::from(path_str),
                                                description: desc.to_string(),
                                            },
                                        );
                                    }
                                }
                                _ => {}
                            }
                        }
                    }
                }
            }
        }

        reg
    }

    pub fn save(&self) -> Result<()> {
        Self::save_to(self, &global_config_path())
    }

    pub fn save_to(&self, cfg_path: &Path) -> Result<()> {
        let parent = cfg_path.parent().unwrap_or_else(|| Path::new("."));
        std::fs::create_dir_all(parent)?;

        let current = std::fs::read_to_string(cfg_path).unwrap_or_default();
        let mut block = format!("active_project: {}\nprojects:\n", self.active);
        let mut sorted_names: Vec<&String> = self.projects.keys().collect();
        sorted_names.sort();
        for name in sorted_names {
            let entry = &self.projects[name];
            block.push_str(&format!(
                "  {name}:\n    path: {}\n    description: {}\n",
                entry.path.display(),
                entry.description
            ));
        }

        let updated = crate::config::with_block(&current, "active_project", &format!("active_project: {}\n", self.active));
        let updated = crate::config::with_block(&updated, "projects", &block[block.find("projects:").unwrap()..]);
        std::fs::write(cfg_path, updated)?;
        Ok(())
    }

    /// Resolve project based on explicit flag, env var, CWD walk, or active project.
    pub fn resolve(
        &self,
        explicit: Option<&str>,
        cwd: &Path,
        env_proj: Option<&str>,
    ) -> Result<Project> {
        // 1. Explicit parameter
        if let Some(target) = explicit.filter(|s| !s.trim().is_empty()) {
            if let Some(entry) = self.projects.get(target) {
                return Ok(Project::new(target, &entry.path));
            }
            let p = absolutize(Path::new(target), cwd);
            let name = p.file_name().unwrap_or_default().to_string_lossy().to_string();
            return Ok(Project::new(name, p));
        }

        // 2. Environment variable
        if let Some(target) = env_proj.filter(|s| !s.trim().is_empty()) {
            if let Some(entry) = self.projects.get(target) {
                return Ok(Project::new(target, &entry.path));
            }
            let p = absolutize(Path::new(target), cwd);
            let name = p.file_name().unwrap_or_default().to_string_lossy().to_string();
            return Ok(Project::new(name, p));
        }

        // 3. CWD walk
        let start = absolutize(cwd, cwd);
        let mut probe: Option<&Path> = Some(&start);
        while let Some(dir) = probe {
            if is_project_root(dir) {
                for (name, entry) in &self.projects {
                    if entry.path == dir {
                        return Ok(Project::new(name.clone(), dir.to_path_buf()));
                    }
                }
                let name = dir.file_name().unwrap_or_default().to_string_lossy().to_string();
                return Ok(Project::new(name, dir.to_path_buf()));
            }
            probe = dir.parent();
        }

        // 4. Active project from registry
        if let Some(entry) = self.projects.get(&self.active) {
            if entry.path.is_dir() {
                return Ok(Project::new(self.active.clone(), &entry.path));
            }
        }

        // 5. Fallback: default project in XDG data projects directory
        let def_path = projects_dir().join("default");
        Ok(Project::new("default", def_path))
    }
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

/// Create a new project workspace on disk and register it.
pub fn create_project(
    name: &str,
    path: Option<&Path>,
    description: &str,
    wiki_remote: &str,
    workspace_remote: &str,
    sources_remote: &str,
    sources_backend: &str,
) -> Result<Project> {
    let root = match path {
        Some(p) => p.to_path_buf(),
        None => projects_dir().join(name),
    };

    std::fs::create_dir_all(&root)?;
    let wiki = root.join("wiki");
    let workspace = root.join("workspace");
    let sources = root.join("sources");

    for (dir, title) in [
        (&wiki, "Wiki Knowledge Base"),
        (&workspace, "Workspace Deliverables"),
        (&sources, "Raw Sources & Literature"),
    ] {
        std::fs::create_dir_all(dir)?;
        let idx = dir.join("_index.md");
        if !idx.exists() {
            let content = format!("# {title}\n\nOKF v0.2 Knowledge Base for {name}.\n");
            let _ = std::fs::write(&idx, content);
        }
        if !dir.join(".git").exists() {
            let _ = std::process::Command::new("git").arg("init").current_dir(dir).output();
        }
    }

    std::fs::create_dir_all(sources.join("literature"))?;

    let s_remote = if sources_remote.is_empty() {
        if sources_backend == "gdrive" { "gdrive" } else { "local" }
    } else {
        sources_remote
    };

    let cfg_content = format!(
        "name: {name}\ndescription: {}\nrepositories:\n  wiki: {}\n  workspace: {}\n  sources: {}\nsources_backend: {}\n",
        if description.is_empty() { format!("{name} research project") } else { description.to_string() },
        if wiki_remote.is_empty() { "local" } else { wiki_remote },
        if workspace_remote.is_empty() { "local" } else { workspace_remote },
        s_remote,
        if sources_backend.is_empty() { "local" } else { sources_backend }
    );
    std::fs::write(root.join("podarcis.yaml"), cfg_content)?;

    let mut reg = ProjectRegistry::load();
    reg.active = name.to_string();
    reg.projects.insert(
        name.to_string(),
        ProjectEntry {
            path: root.clone(),
            description: description.to_string(),
        },
    );
    reg.save()?;

    Ok(Project::new(name, root))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn project_directories_derived() {
        let p = Project::new("test", "/tmp/test");
        assert_eq!(p.wiki(), PathBuf::from("/tmp/test/wiki"));
        assert_eq!(p.workspace(), PathBuf::from("/tmp/test/workspace"));
        assert_eq!(p.sources(), PathBuf::from("/tmp/test/sources"));
    }

    #[test]
    fn registry_save_and_load_round_trip() {
        let temp = std::env::temp_dir().join(format!("podarcis-reg-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&temp);
        std::fs::create_dir_all(&temp).unwrap();

        let cfg_path = temp.join("config.yaml");
        let mut reg = ProjectRegistry {
            active: "bio".to_string(),
            projects: HashMap::new(),
        };
        reg.projects.insert(
            "bio".to_string(),
            ProjectEntry {
                path: PathBuf::from("/tmp/bio"),
                description: "Biology research".to_string(),
            },
        );
        reg.projects.insert(
            "macro".to_string(),
            ProjectEntry {
                path: PathBuf::from("/tmp/macro"),
                description: "Macro economy".to_string(),
            },
        );
        reg.save_to(&cfg_path).unwrap();

        let reloaded = ProjectRegistry::load_from(&cfg_path);
        assert_eq!(reloaded.active, "bio");
        assert_eq!(reloaded.projects.len(), 2);
        assert_eq!(reloaded.projects["bio"].path, PathBuf::from("/tmp/bio"));
        assert_eq!(reloaded.projects["macro"].description, "Macro economy");
    }

    #[test]
    fn project_resolution_priority() {
        let temp = std::env::temp_dir().join(format!("podarcis-res-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&temp);
        std::fs::create_dir_all(&temp).unwrap();

        let p1_dir = temp.join("p1");
        let p2_dir = temp.join("p2");
        std::fs::create_dir_all(p1_dir.join("wiki")).unwrap();
        std::fs::create_dir_all(p1_dir.join("workspace")).unwrap();
        std::fs::create_dir_all(p2_dir.join("wiki")).unwrap();
        std::fs::create_dir_all(p2_dir.join("workspace")).unwrap();

        let mut reg = ProjectRegistry {
            active: "p1".to_string(),
            projects: HashMap::new(),
        };
        reg.projects.insert("p1".to_string(), ProjectEntry { path: p1_dir.clone(), description: "".into() });
        reg.projects.insert("p2".to_string(), ProjectEntry { path: p2_dir.clone(), description: "".into() });

        // 1. Fallback to active when CWD is not in a project
        let empty_dir = temp.join("empty");
        std::fs::create_dir_all(&empty_dir).unwrap();
        let res1 = reg.resolve(None, &empty_dir, None).unwrap();
        assert_eq!(res1.name, "p1");

        // 2. CWD walk beats active
        let res2 = reg.resolve(None, &p2_dir.join("wiki"), None).unwrap();
        assert_eq!(res2.name, "p2");

        // 3. Env var beats CWD
        let res3 = reg.resolve(None, &p2_dir.join("wiki"), Some("p1")).unwrap();
        assert_eq!(res3.name, "p1");

        // 4. Explicit beats all
        let res4 = reg.resolve(Some("p2"), &p1_dir, Some("p1")).unwrap();
        assert_eq!(res4.name, "p2");
    }
}
