//! `podarcis` — the Rust CLI for the Podarcis engine.
//!
//! Owns the whole command tree. Core commands (status, config, repo, lint,
//! diagnose, test, wiki, frontend) run natively in Rust on the shared library;
//! the Python-bound families (job timers, research/ingest HTTP+PDF, install/
//! uninstall venv lifecycle) dispatch to the engine's remaining Python CLI
//! (`python -m podarcis.cli`) until ported.

use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::Result;
use clap::{Args, Parser, Subcommand};
use podarcis::config::{self, Config};
use podarcis::diagnose;
use podarcis::platform;
use podarcis::vault::index::Index;
use podarcis::vault::lint;
use podarcis::vault::page;
use serde_json::{json, Map, Value};

#[derive(Parser, Debug)]
#[command(
    name = "podarcis",
    disable_version_flag = true,
    about = "Podarcis OKF v0.2 research agent engine & configuration tool"
)]
struct Cli {
    /// Print the engine version and exit.
    #[arg(short, long)]
    version: bool,
    /// Launch the interactive configuration menu.
    #[arg(short, long)]
    interactive: bool,
    /// Target project name or directory path.
    #[arg(short, long)]
    project: Option<String>,
    /// Podarcis checkout root (AGENTS.md + .podarcis/config.yaml or podarcis.yaml).
    #[arg(long)]
    root: Option<PathBuf>,
    #[command(subcommand)]
    command: Option<Cmd>,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Manage research projects (workspaces with wiki, workspace, and sources).
    Project {
        #[command(subcommand)]
        action: Option<ProjectAction>,
    },
    /// Display status of MCP tool modules, skills, agents, jobs, and repos.
    Status {
        /// Output status as JSON.
        #[arg(short, long)]
        json: bool,
    },
    /// Configure components, frontend, and repositories.
    Config {
        #[command(subcommand)]
        action: Option<ConfigAction>,
    },
    /// Manage and synchronize workspace repositories.
    Repo {
        #[command(subcommand)]
        action: Option<RepoAction>,
    },
    /// Run the link integrity check.
    Lint {
        /// Output findings as JSON.
        #[arg(long)]
        json: bool,
        /// Apply safe auto-fixes (frontmatter quoting) via the engine checker.
        #[arg(long)]
        fix: bool,
        /// Path to lint; defaults to the checkout root.
        #[arg(trailing_var_arg = true)]
        rest: Vec<String>,
    },
    /// Display and resolve platform pain points.
    Diagnose(DiagnoseArgs),
    /// Run the pytest suite, then the front-end crate's tests.
    Test {
        /// Skip the front-end crate tests.
        #[arg(long)]
        python_only: bool,
        /// Pass-through arguments for pytest.
        #[arg(trailing_var_arg = true)]
        rest: Vec<String>,
    },
    /// Browse, search, and edit the wiki (Ratatui frontend).
    Wiki {
        #[command(subcommand)]
        action: Option<WikiAction>,
        /// Page to open.
        #[arg(trailing_var_arg = true)]
        rest: Vec<String>,
    },
    /// Open the configured frontend tool.
    Frontend,
    /// Clean Python build artifacts and cache files.
    Clean,
    /// Remove global symlink, virtualenv, and build artefacts.
    Uninstall {
        #[arg(short, long)]
        yes: bool,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        purge: bool,
    },
    /// Delegate remaining engine surface to the Python CLI.
    Job {
        #[arg(trailing_var_arg = true)]
        rest: Vec<String>,
    },
    Research {
        #[arg(trailing_var_arg = true)]
        rest: Vec<String>,
    },
    Ingest {
        #[arg(trailing_var_arg = true)]
        rest: Vec<String>,
    },
    Install {
        #[arg(trailing_var_arg = true)]
        rest: Vec<String>,
    },
}

#[derive(Args, Debug)]
struct DiagnoseArgs {
    /// Output issues as JSON.
    #[arg(long)]
    json: bool,
    /// Resolve every unresolved pain point.
    #[arg(long)]
    clear: bool,
    /// Mark a specific pain point ID as resolved.
    #[arg(long)]
    resolve: Option<String>,
    /// Resolve every unresolved pain point in a category.
    #[arg(long)]
    resolve_category: Option<String>,
    /// Parse and log pain points for a transcript file.
    #[arg(long)]
    log_session: Option<String>,
}

#[derive(Subcommand, Debug)]
enum ConfigAction {
    /// List status of components and repositories.
    List {
        #[arg(short, long)]
        json: bool,
    },
    /// Enable an MCP tool module.
    Enable { name: String },
    /// Disable an MCP tool module.
    Disable { name: String },
    /// Configure repository remotes or local paths.
    #[command(name = "repo")]
    ConfigureRepo(RepoCfgArgs),
    /// Set the frontend tool (tui, vscode, obsidian, none).
    Frontend { frontend_name: String },
    /// Launch the interactive configuration menu.
    Interactive,
}

#[derive(Subcommand, Debug)]
enum RepoAction {
    /// Display Git and sync status across all workspace repositories.
    Status {
        #[arg(short, long)]
        json: bool,
    },
    /// Synchronize workspace repositories (pull remotes, init locals).
    Sync,
    /// Lint-gated per-repo commit of dirty workspace repositories.
    Commit {
        #[arg(short, long, default_value = "chore: wiki commit")]
        message: String,
    },
    /// Configure repository remotes or local paths.
    #[command(name = "config")]
    ConfigureRepo(RepoCfgArgs),
    /// Push local commits to configured remotes (delegated to the engine).
    Push(PushArgs),
}

#[derive(Args, Debug)]
struct PushArgs {
    /// Commit uncommitted local changes before pushing.
    #[arg(short, long)]
    commit: bool,
    /// Lint-gate the push.
    #[arg(long)]
    audit: bool,
    /// Commit message.
    #[arg(short, long, default_value = "chore: sync workspace changes")]
    message: String,
}

#[derive(Args, Debug)]
struct RepoCfgArgs {
    /// Repository name (wiki, workspace, sources, …).
    repo_name: Option<String>,
    /// Remote Git URL.
    #[arg(long)]
    url: Option<String>,
    /// Local directory path.
    #[arg(long)]
    path: Option<String>,
    /// Set the repository to local-only (no remote).
    #[arg(long)]
    local: bool,
}

#[derive(Subcommand, Debug)]
enum ProjectAction {
    /// List all registered research projects.
    List {
        #[arg(short, long)]
        json: bool,
    },
    /// Display the currently active project.
    Current,
    /// Switch the active project.
    Switch {
        name: String,
    },
    /// Create a new project workspace.
    New(ProjectNewArgs),
    /// Register an existing directory as a project.
    Add(ProjectAddArgs),
    /// Unregister a project.
    Remove {
        name: String,
        #[arg(long)]
        purge: bool,
    },
    /// Migrate the current checkout's wiki/workspace/sources to ~/.local/share/podarcis/projects/<name>.
    Migrate {
        #[arg(default_value = "default")]
        name: String,
    },
}

#[derive(Args, Debug)]
struct ProjectNewArgs {
    /// Project name.
    name: String,
    /// Optional directory path (defaults to ~/.local/share/podarcis/projects/<name>).
    #[arg(long)]
    path: Option<PathBuf>,
    /// Project description.
    #[arg(long, default_value = "")]
    description: String,
    /// Remote Git URL for wiki.
    #[arg(long, default_value = "")]
    wiki_remote: String,
    /// Remote Git URL for workspace.
    #[arg(long, default_value = "")]
    workspace_remote: String,
    /// Remote Git URL for sources.
    #[arg(long, default_value = "")]
    sources_remote: String,
    /// Sources backend: local or gdrive.
    #[arg(long, default_value = "local")]
    sources_backend: String,
}

#[derive(Args, Debug)]
struct ProjectAddArgs {
    /// Directory path of the existing project.
    path: PathBuf,
    /// Optional project name (defaults to directory name).
    #[arg(long)]
    name: Option<String>,
    /// Project description.
    #[arg(long, default_value = "")]
    description: String,
}

#[derive(Subcommand, Debug)]
enum WikiAction {
    /// Open the wiki frontend, optionally at a page.
    Open {
        /// Page to open.
        path: Option<String>,
    },
    /// Compile the wiki frontend.
    Build {
        /// Build the debug profile.
        #[arg(long)]
        debug: bool,
    },
}

fn main() {
    let cli = Cli::parse();
    let code = match run(cli) {
        Ok(code) => code,
        Err(err) => {
            eprintln!("Error: {err}");
            1
        }
    };
    std::process::exit(code);
}

fn run(cli: Cli) -> Result<i32> {
    if cli.version {
        println!("podarcis {}", env!("CARGO_PKG_VERSION"));
        return Ok(0);
    }

    let root = find_root(&cli)?;
    if cli.interactive {
        return exec_python_cli(&root, &["interactive"]);
    }

    match cli.command {
        None => open_frontend(&root),
        Some(Cmd::Project { action }) => match action {
            None | Some(ProjectAction::List { json: false }) => cmd_project_list(false),
            Some(ProjectAction::List { json: true }) => cmd_project_list(true),
            Some(ProjectAction::Current) => cmd_project_current(),
            Some(ProjectAction::Switch { name }) => cmd_project_switch(&name),
            Some(ProjectAction::New(args)) => cmd_project_new(args),
            Some(ProjectAction::Add(args)) => cmd_project_add(args),
            Some(ProjectAction::Remove { name, purge }) => cmd_project_remove(&name, purge),
            Some(ProjectAction::Migrate { name }) => cmd_project_migrate(&root, &name),
        },
        Some(Cmd::Status { json }) => cmd_status(&root, json),
        Some(Cmd::Config { action }) => match action {
            None | Some(ConfigAction::Interactive) => exec_python_cli(&root, &["interactive"]),
            Some(ConfigAction::List { json }) => cmd_status(&root, json),
            Some(ConfigAction::Enable { name }) => cmd_config_set_status(&root, &name, true),
            Some(ConfigAction::Disable { name }) => cmd_config_set_status(&root, &name, false),
            Some(ConfigAction::ConfigureRepo(args)) => cmd_config_repo(&root, args),
            Some(ConfigAction::Frontend { frontend_name }) => cmd_config_frontend(&root, &frontend_name),
        },
        Some(Cmd::Repo { action }) => match action {
            None | Some(RepoAction::Status { json: false }) => cmd_repo_status(&root, false),
            Some(RepoAction::Status { json: true }) => cmd_repo_status(&root, true),
            Some(RepoAction::Sync) => cmd_repo_sync(&root),
            Some(RepoAction::Commit { message }) => cmd_repo_commit(&root, &message),
            Some(RepoAction::ConfigureRepo(args)) => cmd_config_repo(&root, args),
            Some(RepoAction::Push(args)) => {
                let mut rest = vec!["repo".to_string(), "push".to_string()];
                if args.commit {
                    rest.push("--commit".to_string());
                }
                if args.audit {
                    rest.push("--audit".to_string());
                }
                rest.push(format!("--message={}", args.message));
                exec_python_cli(&root, &rest)
            }
        },
        Some(Cmd::Lint { json, fix, rest }) => cmd_lint(&root, json, fix, rest),
        Some(Cmd::Diagnose(args)) => cmd_diagnose(&root, args),
        Some(Cmd::Test { python_only, rest }) => cmd_test(&root, python_only, rest),
        Some(Cmd::Wiki { action, rest }) => match action {
            None => run_wiki(&root, rest.first().map(String::as_str)),
            Some(WikiAction::Open { path }) => run_wiki(&root, path.as_deref()),
            Some(WikiAction::Build { debug }) => cmd_wiki_build(&root, debug),
        },
        Some(Cmd::Frontend) => open_frontend(&root),
        Some(Cmd::Clean) => cmd_clean(&root),
        Some(Cmd::Uninstall { yes, dry_run, purge }) => {
            let mut argv = vec!["uninstall".to_string()];
            if yes { argv.push("--yes".to_string()); }
            if dry_run { argv.push("--dry-run".to_string()); }
            if purge { argv.push("--purge".to_string()); }
            exec_python_cli(&root, &argv)
        }
        Some(Cmd::Job { rest }) => pass_through(&root, "job", &rest),
        Some(Cmd::Research { rest }) => pass_through(&root, "research", &rest),
        Some(Cmd::Ingest { rest }) => pass_through(&root, "ingest", &rest),
        Some(Cmd::Install { rest }) => pass_through(&root, "install", &rest),
    }
}

/// Delegate one engine subcommand family, preserving each argv token as a
/// separate argument for the Python parser.
fn pass_through(root: &Path, subcommand: &str, rest: &[String]) -> Result<i32> {
    let argv = std::iter::once(subcommand)
        .chain(rest.iter().map(String::as_str))
        .collect::<Vec<_>>();
    exec_python_cli(root, &argv)
}

fn find_root(cli: &Cli) -> Result<PathBuf> {
    let cwd = std::env::current_dir()?;
    let explicit = cli.project.as_deref().or(cli.root.as_ref().and_then(|p| p.to_str()));
    let env_proj = std::env::var("PODARCIS_PROJECT").ok();
    let env_root = std::env::var("PODARCIS_ROOT").ok();
    let env = env_proj.as_deref().or(env_root.as_deref());

    let reg = podarcis::project::ProjectRegistry::load();
    if let Ok(proj) = reg.resolve(explicit, &cwd, env) {
        if proj.exists() {
            return Ok(proj.root);
        }
    }
    config::find_root(cli.root.as_deref(), &cwd, env_root.as_deref())
}

fn which(bin: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path).map(|d| d.join(bin)).find(|p| p.is_file())
    })
}

fn python_bin(root: &Path) -> String {
    if let Ok(val) = std::env::var("PODARCIS_PYTHON") {
        if !val.trim().is_empty() && Path::new(&val).is_file() {
            return val;
        }
    }
    if let Ok(venv) = std::env::var("VIRTUAL_ENV") {
        let candidate = Path::new(&venv).join("bin").join("python");
        if candidate.is_file() {
            return candidate.to_string_lossy().into_owned();
        }
    }
    let app_venv = podarcis::project::xdg_data_dir().join("venv").join("bin").join("python");
    if app_venv.is_file() {
        return app_venv.to_string_lossy().into_owned();
    }
    let venv = root.join(".venv").join("bin").join("python");
    if venv.is_file() {
        venv.to_string_lossy().into_owned()
    } else {
        which("python3").map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|| "python3".into())
    }
}

/// Internal engine surface that is still Python: jobs, research, ingest,
/// interactive menu, install, uninstall. Dispatches with the same argv the
/// user typed (minus the subcommand), keeping the Python parser the authority
/// for exactly those families.
fn exec_python_cli<S: AsRef<str>>(root: &Path, args: &[S]) -> Result<i32> {
    let py = python_bin(root);
    let mut cmd = Command::new(&py);
    cmd.args(["-m", "podarcis.cli"]).args(args.iter().map(AsRef::as_ref));
    let status = cmd.status()?;
    Ok(status.code().unwrap_or(1))
}

// ----------------------------------------------------------------- project

fn cmd_project_list(as_json: bool) -> Result<i32> {
    let reg = podarcis::project::ProjectRegistry::load();
    if as_json {
        let mut list = Vec::new();
        let mut names: Vec<&String> = reg.projects.keys().collect();
        names.sort();
        for name in names {
            let entry = &reg.projects[name];
            list.push(json!({
                "name": name,
                "path": entry.path.to_string_lossy(),
                "description": entry.description,
                "active": *name == reg.active,
                "exists": entry.path.is_dir(),
            }));
        }
        println!("{}", serde_json::to_string_pretty(&list)?);
        return Ok(0);
    }

    println!("Podarcis Research Projects:\n");
    let mut names: Vec<&String> = reg.projects.keys().collect();
    names.sort();
    if names.is_empty() {
        println!("  No projects registered yet.");
    } else {
        for name in names {
            let entry = &reg.projects[name];
            let is_active = *name == reg.active;
            let mark = if is_active { "*" } else { " " };
            let status = if entry.path.is_dir() { "ready" } else { "missing" };
            println!(
                "  {mark} {:<16} [{status:<7}]  {:<35}  {}",
                name,
                entry.path.display(),
                entry.description
            );
        }
    }
    println!("\nActive project:     {}", reg.active);
    println!("Projects directory: {}\n", podarcis::project::projects_dir().display());
    Ok(0)
}

fn cmd_project_current() -> Result<i32> {
    let reg = podarcis::project::ProjectRegistry::load();
    if let Some(entry) = reg.projects.get(&reg.active) {
        println!("{} ({})", reg.active, entry.path.display());
    } else {
        println!("{} (path not registered)", reg.active);
    }
    Ok(0)
}

fn cmd_project_switch(name: &str) -> Result<i32> {
    let mut reg = podarcis::project::ProjectRegistry::load();
    let Some(entry) = reg.projects.get(name).cloned() else {
        eprintln!("Error: project \"{name}\" is not registered. Run `podarcis project list` to see available projects.");
        return Ok(1);
    };
    reg.active = name.to_string();
    reg.save()?;
    let _ = podarcis::herdr::space::ensure_project_space(name, &entry.path);
    println!("✓ Switched active project to \"{name}\".");
    Ok(0)
}

fn cmd_project_new(args: ProjectNewArgs) -> Result<i32> {
    let proj = podarcis::project::create_project(
        &args.name,
        args.path.as_deref(),
        &args.description,
        &args.wiki_remote,
        &args.workspace_remote,
        &args.sources_remote,
        &args.sources_backend,
    )?;
    let _ = podarcis::herdr::space::create_workspace_if_server_running(&proj.name, &proj.root);
    println!("✓ Initialized project \"{}\" at {}", proj.name, proj.root.display());
    println!("  • wiki:      {}", proj.wiki().display());
    println!("  • workspace: {}", proj.workspace().display());
    println!("  • sources:   {}", proj.sources().display());
    println!("  • config:    {}", proj.config_path().display());
    Ok(0)
}

fn cmd_project_add(args: ProjectAddArgs) -> Result<i32> {
    let path = args.path.canonicalize().unwrap_or(args.path);
    if !path.is_dir() {
        eprintln!("Error: directory does not exist: {}", path.display());
        return Ok(1);
    }
    let name = args.name.unwrap_or_else(|| path.file_name().unwrap_or_default().to_string_lossy().to_string());
    let mut reg = podarcis::project::ProjectRegistry::load();
    reg.projects.insert(
        name.clone(),
        podarcis::project::ProjectEntry {
            path: path.clone(),
            description: args.description,
        },
    );
    if reg.active.is_empty() || reg.active == "default" {
        reg.active = name.clone();
    }
    reg.save()?;
    let _ = podarcis::herdr::space::create_workspace_if_server_running(&name, &path);
    println!("✓ Registered project \"{name}\" at {}", path.display());
    Ok(0)
}

fn cmd_project_remove(name: &str, purge: bool) -> Result<i32> {
    let mut reg = podarcis::project::ProjectRegistry::load();
    let Some(entry) = reg.projects.remove(name) else {
        eprintln!("Error: project \"{name}\" not found in registry.");
        return Ok(1);
    };
    if reg.active == name {
        reg.active = reg.projects.keys().next().cloned().unwrap_or_else(|| "default".to_string());
    }
    reg.save()?;
    if let Ok(workspaces) = podarcis::herdr::space::list_workspaces() {
        if let Some(ws) = workspaces.iter().find(|w| w.label == name) {
            let _ = podarcis::herdr::space::close_workspace(&ws.id);
        }
    }
    if purge && entry.path.is_dir() {
        std::fs::remove_dir_all(&entry.path)?;
        println!("✓ Removed project \"{name}\" and deleted {}", entry.path.display());
    } else {
        println!("✓ Unregistered project \"{name}\" (files kept at {})", entry.path.display());
    }
    Ok(0)
}

fn cmd_project_migrate(root: &Path, name: &str) -> Result<i32> {
    println!("Migrating workspace ({}) to project \"{name}\" in XDG user folder...", root.display());
    let target = podarcis::project::projects_dir().join(name);
    std::fs::create_dir_all(&target)?;

    for folder in &["wiki", "workspace", "sources"] {
        let src = root.join(folder);
        let dst = target.join(folder);
        if src.is_dir() && !dst.exists() {
            println!("  Moving {} → {}", src.display(), dst.display());
            if let Err(_) = std::fs::rename(&src, &dst) {
                copy_dir_all(&src, &dst)?;
                let _ = std::fs::remove_dir_all(&src);
            }
        }
    }

    let src_cfg = root.join(".podarcis").join("config.yaml");
    if src_cfg.is_file() {
        let _ = std::fs::copy(&src_cfg, target.join("podarcis.yaml"));
    }

    let mut reg = podarcis::project::ProjectRegistry::load();
    reg.active = name.to_string();
    reg.projects.insert(
        name.to_string(),
        podarcis::project::ProjectEntry {
            path: target.clone(),
            description: "Migrated workspace".to_string(),
        },
    );
    reg.save()?;
    println!("✓ Migration complete. Project \"{name}\" is now active at {}", target.display());
    Ok(0)
}

fn copy_dir_all(src: &Path, dst: &Path) -> Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        if ty.is_dir() {
            copy_dir_all(&entry.path(), &dst.join(entry.file_name()))?;
        } else {
            std::fs::copy(entry.path(), dst.join(entry.file_name()))?;
        }
    }
    Ok(())
}

// ------------------------------------------------------------------ status

fn cmd_status(root: &Path, as_json: bool) -> Result<i32> {
    let payload = platform::status(root);
    if as_json {
        println!("{}", serde_json::to_string_pretty(&payload)?);
        return Ok(0);
    }
    println!("Podarcis Configuration Status\n");
    let live_tokens: i64 = payload["mcp_servers"]
        .as_object()
        .map(|m| m.values().filter(|v| v["enabled"] == true).map(|v| v["tokens"].as_i64().unwrap_or(0)).sum())
        .unwrap_or(0);
    println!("MCP tool modules: {live_tokens} tokens per session");
    for (k, v) in payload["mcp_servers"].as_object().unwrap_or(&Map::new()) {
        let state = if v["enabled"].as_bool().unwrap_or(false) { "enabled" } else { "disabled" };
        println!("  • {k:<20} [{state}] ({} tokens)", v["tokens"].as_i64().unwrap_or(0));
    }
    for (label, section) in [("Personas", "agents"), ("Skills", "skills")] {
        println!("\n{label}: loaded on demand");
        for (k, v) in payload[section].as_object().unwrap_or(&Map::new()) {
            println!("  • {k:<20} ({} tokens when invoked)", v["tokens"].as_i64().unwrap_or(0));
        }
    }
    if let Some(ext) = payload["external_skills"].as_object() {
        if !ext.is_empty() {
            println!("\nExternal skills: installed via `apm install`");
            for (k, v) in ext {
                let detail: Vec<String> = v["executables"]
                    .as_object()
                    .map(|m| {
                        m.iter()
                            .map(|(name, path)| {
                                if path.as_str().unwrap_or("").is_empty() {
                                    format!("{name} MISSING")
                                } else {
                                    name.clone()
                                }
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                println!("  • {k:<20} {}", detail.join(", "));
            }
            if ext.values().any(|v| v["ok"].as_bool() == Some(false)) {
                println!("  Run `apm lifecycle trust` then `apm install` to install missing CLIs.");
            }
        }
    }
    println!("\nJobs:");
    for (k, v) in payload["jobs"].as_object().unwrap_or(&Map::new()) {
        let state = if v["enabled"].as_bool().unwrap_or(false) { "enabled" } else { "disabled" };
        let sched = v["schedule"].as_str().unwrap_or("");
        println!("  • {k:<20} [{state}] ({sched})");
    }
    println!("\nFrontend:");
    let fe = &payload["frontend"];
    let name = fe["name"].as_str().unwrap_or("none");
    if fe["built"].as_bool().unwrap_or(false) {
        println!("  • {name:<20} ✓ {}  {}", fe["version"].as_str().unwrap_or(""), fe["binary"].as_str().unwrap_or(""));
    } else {
        println!("  • {name:<20} not built  (run `podarcis wiki build`)");
    }
    println!("\nRepositories:");
    for (k, v) in payload["repositories"].as_object().unwrap_or(&Map::new()) {
        let url = v["remote_url"].as_str().unwrap_or("");
        let url = if url.is_empty() { "local-only" } else { url };
        println!("  • {k:<20} {url}");
    }
    Ok(0)
}

fn cmd_config_set_status(root: &Path, name: &str, enable: bool) -> Result<i32> {
    let components = platform::discover_components(root);
    if !components.mcp.contains_key(name) {
        let kind = if components.skills.contains_key(name) {
            "skill"
        } else if components.agents.contains_key(name) {
            "persona"
        } else {
            "none"
        };
        if kind != "none" {
            eprintln!(
                "Error: \"{name}\" is a {kind}, not a tool module, and is not toggleable. \
                 To retire it, set `disabled: true` in its own frontmatter."
            );
            return Ok(1);
        }
        eprintln!(
            "Error: tool module \"{name}\" not found. Available: {}",
            components.mcp.keys().cloned().collect::<Vec<_>>().join(", ")
        );
        return Ok(1);
    }
    platform::set_mcp_enabled(root, name, enable)?;
    println!("{} tool module \"{name}\".", if enable { "✓ Enabled" } else { "Disabled" });
    Ok(0)
}

fn cmd_config_repo(root: &Path, args: RepoCfgArgs) -> Result<i32> {
    use podarcis::vault::git;
    let cfg = Config::load(root);
    let known = git::repo_names(&cfg.repositories);

    let Some(repo_name) = args.repo_name else {
        println!("Configured Podarcis Repositories:\n");
        for name in &known {
            let url = cfg.repositories.get(name).cloned().unwrap_or_default();
            println!("  • {name:<15} {}", if url.is_empty() { "local-only" } else { &url });
        }
        return Ok(0);
    };

    let target = args.url.clone().or(args.path.clone()).map(|s| s.trim().to_string()).unwrap_or_default();
    if args.local {
        platform::set_repo_url_config(root, &repo_name, "")?;
        git::ensure_local_repo(root, &repo_name);
        println!("✓ Set {repo_name} to local-only.");
    } else if args.url.is_some() || args.path.is_some() {
        platform::set_repo_url_config(root, &repo_name, &target)?;
        git::ensure_local_repo(root, &repo_name);
        if target.is_empty() {
            println!("✓ Set {repo_name} to local-only.");
        } else {
            println!("✓ Set remote/path for {repo_name} to {target}");
        }
    } else {
        let url = cfg.repositories.get(&repo_name).cloned().unwrap_or_default();
        println!("Repository \"{repo_name}\": {}", if url.is_empty() { "local-only" } else { url.as_str() });
    }
    Ok(0)
}

fn cmd_config_frontend(root: &Path, name: &str) -> Result<i32> {
    let name = name.to_lowercase();
    platform::set_frontend(root, &name)?;
    if name == "vscode" {
        ensure_vscode_config(root);
    }
    println!("✓ Frontend set to {name}.");
    Ok(0)
}

/// Copy `.podarcis/templates/vscode/*` into `.vscode/` when missing.
fn ensure_vscode_config(root: &Path) {
    let template = root.join(".podarcis").join("templates").join("vscode");
    if !template.is_dir() {
        return;
    }
    let target = root.join(".vscode");
    let _ = std::fs::create_dir_all(&target);
    for entry in std::fs::read_dir(&template).into_iter().flatten().flatten() {
        let src = entry.path();
        let dst = target.join(entry.file_name());
        if !dst.exists() {
            let _ = std::fs::copy(&src, &dst);
        }
    }
}

// ------------------------------------------------------------------- repo

fn cmd_repo_status(root: &Path, as_json: bool) -> Result<i32> {
    let repos = Config::load(root).repositories;
    let rows = platform::repo_status(root, &repos);
    if as_json {
        println!("{}", serde_json::to_string_pretty(&Value::Array(rows))?);
        return Ok(0);
    }
    println!("{}", format_status_table(&rows));
    Ok(0)
}

fn format_status_table(rows: &[Value]) -> String {
    let mut out = String::new();
    for row in rows {
        let name = row["repo"].as_str().unwrap_or("");
        let status = row["status"].as_str().unwrap_or("");
        let branch = row["branch"].as_str().unwrap_or("-");
        let changes = row["changes"].as_i64().unwrap_or(0);
        let ahead = row["ahead"].as_i64().unwrap_or(0);
        let behind = row["behind"].as_i64().unwrap_or(0);
        let url = row["url"].as_str().unwrap_or("local");
        out.push_str(&format!(
            "{name:<12} {status:<12} branch {branch:<12} {changes:+} changes  a{ahead}/b{behind}  {url}\n"
        ));
    }
    out
}

fn cmd_repo_sync(root: &Path) -> Result<i32> {
    let repos = Config::load(root).repositories;
    let results = podarcis::vault::git::sync_repos(root, &repos);
    for r in &results {
        let sym = if r.status == "ok" { "✓" } else { "✗" };
        println!("  {sym} {:<12} {}", r.repo, r.message);
    }
    Ok(0)
}

fn cmd_repo_commit(root: &Path, message: &str) -> Result<i32> {
    let cfg = Config::load(root);
    let collections: Vec<PathBuf> = cfg.collections().into_iter().map(|(_, p)| p).collect();
    let index = Index::build(root, &collections);
    let findings_present = |i: &Index| {
        !i.entries.iter().all(|e| e.findings.is_empty()) || !i.dir_findings.is_empty()
    };
    let outcome = podarcis::vault::git::commit_dirty(
        root,
        &cfg.repositories,
        !findings_present(&index),
        message,
    );
    if !outcome.ok {
        eprintln!("Audit gate failed; nothing committed. {}", outcome.message);
        return Ok(1);
    }
    if outcome.committed.is_empty() {
        println!("{}", outcome.message);
    } else {
        println!("✓ Committed {}", outcome.committed.join(", "));
    }
    Ok(0)
}

// ------------------------------------------------------------------- lint

/// Lint the parts of the OKF collections the target covers, using the same
/// `Index` the front-end's own `--lint` port runs (itself diff-gated against
/// the engine's `check_links.run_audit`). Payload matches
/// `audit.to_json_payload`: root-relative keys, `{ok, root, files}`.
fn lint_payload(root: &Path, target: &Path) -> Result<Value> {
    let cfg = Config::load(root);
    let collections: Vec<PathBuf> = cfg.collections().into_iter().map(|(_, p)| p).collect();
    let mut walk_roots: Vec<PathBuf> = Vec::new();
    for collection in &collections {
        if target.starts_with(collection) || collection.starts_with(target) {
            let sub = if collection.starts_with(target) {
                collection.clone()
            } else {
                target.to_path_buf()
            };
            if !walk_roots.contains(&sub) {
                walk_roots.push(sub);
            }
        }
    }
    let index = if walk_roots.is_empty() {
        return raw_walk_payload(root, target);
    } else {
        Index::build(root, &walk_roots)
    };
    let mut files: Map<String, Value> = Map::new();
    for entry in &index.entries {
        if entry.findings.is_empty() {
            continue;
        }
        let arr: Vec<Value> = entry
            .findings
            .iter()
            .map(|f| json!({"code": f.code, "detail": f.detail}))
            .collect();
        files.insert(entry.rel.clone(), Value::Array(arr));
    }
    for (rel, finding) in &index.dir_findings {
        files.insert(
            rel.clone(),
            Value::Array(vec![json!({"code": finding.code, "detail": finding.detail})]),
        );
    }
    Ok(json!({
        "ok": files.is_empty(),
        "root": root.to_string_lossy(),
        "files": files,
    }))
}

/// Non-collection target (e.g. `podarcis lint tui/`): bare link/footnote/
/// frontmatter lint of the subtree. Directory-bloat only applies inside the
/// wiki/workspace collections, so it is skipped here.
fn raw_walk_payload(root: &Path, target: &Path) -> Result<Value> {
    let mut files: Map<String, Value> = Map::new();
    for entry in walk_documents(target, root) {
        let findings = lint::check(&entry.raw, &entry.path);
        if !findings.is_empty() {
            let arr: Vec<Value> = findings
                .iter()
                .map(|f| json!({"code": f.code, "detail": f.detail}))
                .collect();
            files.insert(entry.rel, Value::Array(arr));
        }
    }
    Ok(json!({
        "ok": files.is_empty(),
        "root": root.to_string_lossy(),
        "files": files,
    }))
}

struct DocEntry {
    rel: String,
    path: PathBuf,
    raw: String,
}

fn walk_documents(target: &Path, base: &Path) -> Vec<DocEntry> {
    let mut out = Vec::new();
    let mut stack = vec![target.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            if path.is_dir() {
                if !matches!(name.as_str(), ".git" | ".venv" | ".obsidian" | "__pycache__" | "node_modules" | "tmp" | "target") {
                    stack.push(path);
                }
            } else if name != "raw.md" && name.ends_with(".md") {
                if let Ok(raw) = std::fs::read_to_string(&path) {
                    out.push(DocEntry { rel: page::rel_path(&path, base), path, raw });
                }
            }
        }
    }
    out
}

fn cmd_lint(root: &Path, as_json: bool, fix: bool, rest: Vec<String>) -> Result<i32> {
    let target = if rest.is_empty() {
        root.to_path_buf()
    } else {
        let first = std::path::Path::new(&rest[0]);
        if first.is_absolute() { first.to_path_buf() } else { root.join(first) }
    };

    if fix {
        // Auto-fixes (frontmatter quoting) only exist in the engine checker.
        let checker = root.join(".agents").join("mcp").join("wiki").join("check_links.py");
        if !checker.is_file() {
            eprintln!("Error: engine checker not found at {}", checker.display());
            return Ok(1);
        }
        let py = python_bin(root);
        let status = Command::new(py).arg(&checker).arg("--fix").arg(&target).status()?;
        return Ok(status.code().unwrap_or(1));
    }

    let payload = lint_payload(root, &target)?;
    if as_json {
        println!("{}", serde_json::to_string_pretty(&payload)?);
        return Ok(if payload["ok"].as_bool().unwrap_or(false) { 0 } else { 1 });
    }

    let files = payload["files"].as_object().cloned().unwrap_or_default();
    if files.is_empty() {
        println!("Audit passed: No issues found.");
        Ok(0)
    } else {
        for (rel, findings) in &files {
            println!("\n--- {rel} ---");
            if let Some(arr) = findings.as_array() {
                for f in arr {
                    println!("  - {}: {}", f["code"].as_str().unwrap_or("issue"), f["detail"].as_str().unwrap_or(""));
                }
            }
        }
        println!();
        Ok(1)
    }
}

// ---------------------------------------------------------------- diagnose

fn cmd_diagnose(root: &Path, args: DiagnoseArgs) -> Result<i32> {
    if let Some(transcript) = args.log_session {
        let script = root
            .join(".apm")
            .join("skills")
            .join("self-improvement")
            .join("scripts")
            .join("diagnose_session.py");
        if !script.is_file() {
            eprintln!("Error: diagnose_session.py script not found.");
            return Ok(1);
        }
        let py = python_bin(root);
        let mut cmd = Command::new(py);
        cmd.arg(&script).arg("--transcript").arg(&transcript);
        if args.json {
            cmd.arg("--json");
        }
        let status = cmd.status()?;
        return Ok(status.code().unwrap_or(0));
    }

    if args.resolve.is_some() || args.resolve_category.is_some() || args.clear {
        let ids = args.resolve.iter().cloned().collect::<Vec<_>>();
        let resolved = diagnose::resolve(root, &ids, args.resolve_category.as_deref().unwrap_or(""), args.clear)?;
        if resolved.is_empty() {
            println!("No unresolved pain points matched.");
            return Ok(1);
        }
        println!("✓ Resolved {} pain point(s): {}", resolved.len(), resolved.join(", "));
        return Ok(0);
    }

    let issues = diagnose::active(root)?;
    if args.json {
        println!("{}", serde_json::to_string_pretty(&Value::Array(issues))?);
        return Ok(0);
    }
    if issues.is_empty() {
        println!("No active platform pain points logged in .podarcis/diagnostics/");
    } else {
        println!("Current Platform Pain Points ({} active):", issues.len());
        for (idx, issue) in issues.iter().enumerate() {
            let sev = issue.get("severity").and_then(Value::as_str).unwrap_or("medium");
            let cat = issue.get("category").and_then(Value::as_str).unwrap_or("issue");
            let summ = issue.get("summary").and_then(Value::as_str).unwrap_or("");
            let ts = issue.get("timestamp").and_then(Value::as_str).unwrap_or("");
            println!("{}. [{}] [{}] {} ({})", idx + 1, sev.to_uppercase(), cat, summ, ts);
        }
    }
    Ok(0)
}

// -------------------------------------------------------------------- test

fn cmd_test(root: &Path, python_only: bool, rest: Vec<String>) -> Result<i32> {
    let pytest = {
        let venv = root.join(".venv").join("bin").join("pytest");
        if venv.is_file() {
            venv.to_string_lossy().into_owned()
        } else {
            "pytest".to_string()
        }
    };
    let py_code = Command::new(&pytest).args(&rest).status()?.code().unwrap_or(1);
    if python_only {
        return Ok(py_code);
    }
    let manifest = root.join("tui").join("Cargo.toml");
    if !manifest.is_file() {
        return Ok(py_code);
    }
    let Some(cargo) = which("cargo") else {
        println!("cargo not found — skipping the frontend tests.");
        return Ok(py_code);
    };
    println!("\npodarcis-tui");
    let rust = Command::new(cargo).args(["test", "--manifest-path"]).arg(&manifest).arg("--quiet").status()?.code().unwrap_or(1);
    Ok(if py_code != 0 { py_code } else { rust })
}

// -------------------------------------------------------------------- wiki

fn cmd_wiki_build(root: &Path, debug: bool) -> Result<i32> {
    let manifest = root.join("tui").join("Cargo.toml");
    if !manifest.is_file() {
        eprintln!("No crate at {}; skipping frontend build.", manifest.display());
        return Ok(1);
    }
    let Some(cargo) = which("cargo") else {
        eprintln!("cargo not found — the wiki frontend will not be built.");
        return Ok(1);
    };
    let mut cmd = Command::new(cargo);
    cmd.arg("build").arg("--manifest-path").arg(&manifest);
    if !debug {
        cmd.arg("--release");
    }
    let ok = cmd.status()?.success();
    if !ok {
        return Ok(1);
    }
    if let Some(binary) = platform::find_frontend_binary(root) {
        println!("✓ Built {}", binary.display());
    }
    Ok(0)
}

fn run_wiki(root: &Path, path: Option<&str>) -> Result<i32> {
    let mut binary = platform::find_frontend_binary(root);
    if binary.is_none() && which("cargo").is_some() {
        println!("Building the wiki frontend (first run)…");
        if !Command::new(which("cargo").unwrap())
            .args(["build", "--release", "--manifest-path"])
            .arg(root.join("tui").join("Cargo.toml"))
            .status()?
            .success()
        {
            eprintln!("Error: podarcis-tui not found. Install Rust from https://rustup.rs.");
            return Ok(1);
        }
        binary = platform::find_frontend_binary(root);
    }
    let Some(binary) = binary else {
        eprintln!("Error: podarcis-tui not found. Build it with `podarcis wiki build`.");
        return Ok(1);
    };
    let mut cmd = Command::new(&binary);
    cmd.arg("--root").arg(root);
    if let Some(path) = path {
        cmd.arg(path);
    }
    let status = cmd.status()?;
    Ok(status.code().unwrap_or(1))
}

// ---------------------------------------------------------------- frontend

fn open_frontend(root: &Path) -> Result<i32> {
    let name = platform::frontend(root);
    match name.as_str() {
        "tui" => run_wiki(root, None),
        "none" => {
            println!("Frontend set to none — nothing to open.");
            Ok(0)
        }
        "obsidian" => {
            let uri = format!("obsidian://open?path={}", root.display());
            let status = Command::new("obsidian").arg(uri).status()?;
            Ok(status.code().unwrap_or(0))
        }
        "vscode" => {
            ensure_vscode_config(root);
            let status = Command::new("code").arg(root).status()?;
            Ok(status.code().unwrap_or(0))
        }
        other => {
            let status = Command::new(other).arg(root).status()?;
            Ok(status.code().unwrap_or(0))
        }
    }
}

// ------------------------------------------------------------------- clean

fn cmd_clean(root: &Path) -> Result<i32> {
    let mut count = 0usize;
    let mut stack = vec![root.to_path_buf()];
    let mut pycache = Vec::new();
    let mut pyc = Vec::new();
    let mut egg = Vec::new();
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            if path.is_dir() {
                if name == "__pycache__" {
                    pycache.push(path);
                } else if name == ".pytest_cache" {
                    pycache.push(path);
                } else if name.ends_with(".egg-info") {
                    egg.push(path);
                } else {
                    stack.push(path);
                }
            } else if name.ends_with(".pyc") {
                pyc.push(path);
            }
        }
    }
    for d in pycache {
        if std::fs::remove_dir_all(&d).is_ok() {
            count += 1;
        }
    }
    for d in egg {
        if std::fs::remove_dir_all(&d).is_ok() {
            count += 1;
        }
    }
    for f in pyc {
        if std::fs::remove_file(&f).is_ok() {
            count += 1;
        }
    }
    println!("✓ Cleaned {count} build artifacts and cache directories.");
    Ok(0)
}