//! Git status tracking for the content tree.
//!
//! Queries the repository (and any decoupled collection repositories) for
//! modified, added, untracked, deleted, renamed, or conflicted files, and maps
//! each path and its parent directories to a `GitStatus` token.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GitStatus {
    Modified,
    Untracked,
    Added,
    Deleted,
    Renamed,
    Conflict,
}

impl GitStatus {
    /// Priority order for directory aggregation:
    /// Conflict > Modified > Added > Untracked > Renamed > Deleted.
    pub fn priority(self) -> u8 {
        match self {
            Self::Conflict => 6,
            Self::Modified => 5,
            Self::Added => 4,
            Self::Untracked => 3,
            Self::Renamed => 2,
            Self::Deleted => 1,
        }
    }

    pub fn merge(self, other: Self) -> Self {
        if self.priority() >= other.priority() {
            self
        } else {
            other
        }
    }

    pub fn glyph(self) -> &'static str {
        match self {
            Self::Modified => "M",
            Self::Untracked => "?",
            Self::Added => "A",
            Self::Deleted => "D",
            Self::Renamed => "R",
            Self::Conflict => "!",
        }
    }
}

/// Whether a collection folder is itself a git checkout (with its current branch name),
/// tracked by a parent repo, or not in git at all.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TrackState {
    /// Nested `.git` (submodule or its own repository) with active branch or commit.
    Repo(String),
    /// Paths under this folder are in a parent repository's index.
    Tracked,
    /// No git, ignored, or nothing in the index.
    Untracked,
}

impl TrackState {
    pub fn branch(&self) -> Option<&str> {
        match self {
            Self::Repo(branch) => Some(branch.as_str()),
            Self::Tracked | Self::Untracked => None,
        }
    }

    pub fn label(&self) -> Option<&str> {
        self.branch()
    }

    pub fn tracked(&self) -> bool {
        !matches!(self, Self::Untracked)
    }
}

/// Retrieve the active branch name for a git repository directory,
/// falling back to a short commit SHA or "HEAD".
pub fn git_branch(dir: &Path) -> Option<String> {
    if let Ok(output) = Command::new("git")
        .args(["branch", "--show-current"])
        .current_dir(dir)
        .output()
    {
        if output.status.success() {
            let branch = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !branch.is_empty() {
                return Some(branch);
            }
        }
    }
    if let Ok(output) = Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .current_dir(dir)
        .output()
    {
        if output.status.success() {
            let sha = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !sha.is_empty() {
                return Some(sha);
            }
        }
    }
    None
}

/// Is `path` tracked by git (own repo or parent index)?
pub fn track_state(path: &Path, root: &Path) -> TrackState {
    if path.join(".git").exists() {
        let branch = git_branch(path).unwrap_or_else(|| "HEAD".to_string());
        return TrackState::Repo(branch);
    }
    let repo = git_toplevel(path).or_else(|| git_toplevel(root));
    let Some(repo) = repo else {
        return TrackState::Untracked;
    };
    let rel = path.strip_prefix(&repo).unwrap_or(path);
    let rel_str = rel.to_string_lossy();
    let Ok(output) = Command::new("git")
        .args(["ls-files", "--", rel_str.as_ref()])
        .current_dir(&repo)
        .output()
    else {
        return TrackState::Untracked;
    };
    if output.status.success() && !output.stdout.is_empty() {
        TrackState::Tracked
    } else {
        TrackState::Untracked
    }
}

fn git_toplevel(dir: &Path) -> Option<PathBuf> {
    let output = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .current_dir(dir)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() {
        None
    } else {
        Some(PathBuf::from(text))
    }
}

#[derive(Clone, Debug, Default)]
pub struct GitMap {
    pub files: HashMap<PathBuf, GitStatus>,
    pub dirs: HashMap<PathBuf, GitStatus>,
}

impl GitMap {
    pub fn status_for(&self, path: &Path) -> Option<GitStatus> {
        self.files.get(path).copied().or_else(|| self.dirs.get(path).copied())
    }

    /// Scan `root` and any collection sub-repositories for git status.
    pub fn scan(root: &Path, collections: &[PathBuf]) -> Self {
        let mut map = Self::default();
        let mut repos = Vec::new();

        if root.join(".git").exists() {
            repos.push(root.to_path_buf());
        }

        for col in collections {
            if col.join(".git").exists() && !repos.contains(col) {
                repos.push(col.clone());
            }
        }

        for repo in repos {
            map.scan_repo(&repo, root);
        }

        map
    }

    fn scan_repo(&mut self, repo: &Path, root: &Path) {
        let Ok(output) = Command::new("git")
            .args(["-c", "core.quotePath=false", "status", "--porcelain=v1", "-uall"])
            .current_dir(repo)
            .output()
        else {
            return;
        };

        if !output.status.success() {
            return;
        }

        let text = String::from_utf8_lossy(&output.stdout);
        for line in text.lines() {
            if line.len() < 3 {
                continue;
            }
            let bytes = line.as_bytes();
            let x = bytes[0] as char;
            let y = bytes[1] as char;
            let rest = &line[3..];

            let status = parse_status(x, y);

            let rel_file = if let Some((_, new_target)) = rest.split_once(" -> ") {
                new_target.trim().trim_matches('"')
            } else {
                rest.trim().trim_matches('"')
            };

            let abs_path = repo.join(rel_file);

            self.files.insert(abs_path.clone(), status);

            let mut cur = abs_path.parent();
            while let Some(parent) = cur {
                self.dirs
                    .entry(parent.to_path_buf())
                    .and_modify(|s| *s = s.merge(status))
                    .or_insert(status);
                if parent == repo || parent == root {
                    break;
                }
                cur = parent.parent();
            }
        }
    }
}

pub fn parse_status(x: char, y: char) -> GitStatus {
    if x == '?' && y == '?' {
        GitStatus::Untracked
    } else if x == 'U' || y == 'U' || (x == 'A' && y == 'A') || (x == 'D' && y == 'D') {
        GitStatus::Conflict
    } else if x == 'M' || y == 'M' || x == 'T' || y == 'T' {
        GitStatus::Modified
    } else if x == 'A' {
        GitStatus::Added
    } else if x == 'D' || y == 'D' {
        GitStatus::Deleted
    } else if x == 'R' || y == 'R' {
        GitStatus::Renamed
    } else {
        GitStatus::Modified
    }
}

// --------------------------------------------------------------- repo sync

/// The three default collections, plus any extra names configured in
/// `repositories:`. Mirrors `repos.py::get_repo_names`.
pub const DEFAULT_REPO_NAMES: [&str; 3] = ["sources", "wiki", "workspace"];

pub fn repo_names(repositories: &HashMap<String, String>) -> Vec<String> {
    let mut names: Vec<String> = DEFAULT_REPO_NAMES.iter().map(|s| s.to_string()).collect();
    for k in repositories.keys() {
        if !names.contains(k) {
            names.push(k.clone());
        }
    }
    names
}

fn run_git(dir: &Path, args: &[&str]) -> std::io::Result<Output> {
    Command::new("git").args(args).current_dir(dir).output()
}

fn ok_output(out: &std::io::Result<Output>) -> bool {
    matches!(out, Ok(o) if o.status.success())
}

fn stderr_of(out: std::io::Result<Output>) -> String {
    match out {
        Ok(o) => {
            let text = if !o.stderr.is_empty() { &o.stderr } else { &o.stdout };
            String::from_utf8_lossy(text).trim().to_string()
        }
        Err(err) => err.to_string(),
    }
}

/// Set a local fallback identity if nothing — local, global, or system — is
/// configured, so the initial commit below never silently fails on a machine
/// or CI runner with no git identity at all.
fn ensure_identity(dir: &Path) {
    let has_name = run_git(dir, &["config", "user.name"]).map(|o| o.status.success()).unwrap_or(false);
    if !has_name {
        let _ = run_git(dir, &["config", "user.name", "Podarcis"]);
        let _ = run_git(dir, &["config", "user.email", "podarcis@localhost"]);
    }
}

/// `git init` + an initial commit, if the directory is not already a repo.
/// Mirrors `repos.py::ensure_local_git_repo`.
pub fn ensure_local_repo(root: &Path, name: &str) {
    let dir = root.join(name);
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    if dir.join(".git").exists() {
        return;
    }
    let _ = run_git(&dir, &["init"]);
    ensure_identity(&dir);
    let readme = dir.join("index.md");
    if !readme.exists() {
        let mut title = name.to_string();
        if let Some(c) = title.get_mut(0..1) {
            c.make_ascii_uppercase();
        }
        let _ = std::fs::write(&readme, format!("# {title}\n\nOKF v0.2 Knowledge Base\n"));
    }
    let _ = run_git(&dir, &["add", "-A"]);
    let _ = run_git(&dir, &["commit", "-m", &format!("chore: initialize {name} repository")]);
}

/// Point `origin` at `url`, adding it if the repo has none yet. No-op if
/// already correct. Mirrors `repos.py::set_repo_url`'s remote update.
pub fn set_remote(dir: &Path, url: &str) {
    let current = run_git(dir, &["remote", "get-url", "origin"])
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();
    if current.is_empty() {
        let _ = run_git(dir, &["remote", "add", "origin", url]);
    } else if current != url {
        let _ = run_git(dir, &["remote", "set-url", "origin", url]);
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct RepoSyncResult {
    pub repo: String,
    pub status: &'static str,
    pub message: String,
}

/// Clone missing repos, pull existing ones, skip `gdrive`-managed ones.
/// Mirrors `repos.py::sync_repos_full` (minus the gdrive ingestion job, which
/// stays Python — `gdrive_sync.py` is an unimplemented stub anyway).
pub fn sync_repos(root: &Path, repositories: &HashMap<String, String>) -> Vec<RepoSyncResult> {
    let mut out = Vec::new();
    for name in repo_names(repositories) {
        let url = repositories.get(&name).cloned().unwrap_or_default();
        let dir = root.join(&name);

        if url == "gdrive" {
            out.push(RepoSyncResult {
                repo: name,
                status: "ok",
                message: "managed via Google Drive (no local repo)".into(),
            });
            continue;
        }

        if !dir.join(".git").is_dir() {
            if !url.is_empty() && ok_output(&run_git(root, &["clone", &url, &dir.to_string_lossy()])) {
                out.push(RepoSyncResult { repo: name, status: "ok", message: "cloned".into() });
            } else {
                ensure_local_repo(root, &name);
                out.push(RepoSyncResult {
                    repo: name,
                    status: "ok",
                    message: "initialised local repository".into(),
                });
            }
            continue;
        }

        if url.is_empty() || url == "local" {
            out.push(RepoSyncResult { repo: name, status: "ok", message: "local repository ready".into() });
            continue;
        }

        set_remote(&dir, &url);
        let pull = run_git(&dir, &["pull", "--rebase", "origin", "HEAD"]);
        if ok_output(&pull) {
            out.push(RepoSyncResult { repo: name, status: "ok", message: "synced with remote origin".into() });
        } else {
            out.push(RepoSyncResult { repo: name, status: "error", message: stderr_of(pull) });
        }
    }
    out
}

pub struct CommitOutcome {
    pub ok: bool,
    /// Which repos actually got a commit. Mirrors `audit_and_commit`'s return
    /// dict shape; not surfaced by the TUI today (the toast just shows
    /// `message`) but kept distinct from it for callers — and tests — that
    /// want the list without parsing prose.
    #[allow(dead_code)]
    pub committed: Vec<String>,
    pub message: String,
}

/// Lint-gate, then `git add -A` + commit every dirty configured repo.
/// Mirrors `audit.py::audit_and_commit`; the lint gate itself is the caller's
/// job (an `Index` scan, not a subprocess) — see `vault::lint::to_json_payload`.
pub fn commit_dirty(
    root: &Path,
    repositories: &HashMap<String, String>,
    lint_ok: bool,
    message: &str,
) -> CommitOutcome {
    if !lint_ok {
        return CommitOutcome {
            ok: false,
            committed: vec![],
            message: "lint gate failed: fix errors before committing".into(),
        };
    }

    let mut done = Vec::new();
    let mut errors = Vec::new();
    for name in repo_names(repositories) {
        let dir = root.join(&name);
        if !dir.join(".git").is_dir() {
            continue;
        }
        let status = run_git(&dir, &["status", "--porcelain"]);
        let dirty = matches!(&status, Ok(o) if !o.stdout.is_empty());
        if !dirty {
            continue;
        }
        let add = run_git(&dir, &["add", "-A"]);
        if !ok_output(&add) {
            errors.push(format!("{name} add: {}", stderr_of(add)));
            continue;
        }
        let commit = run_git(&dir, &["commit", "-m", message]);
        if ok_output(&commit) {
            done.push(name);
        } else {
            errors.push(format!("{name}: {}", stderr_of(commit)));
        }
    }

    if done.is_empty() && errors.is_empty() {
        return CommitOutcome { ok: true, committed: vec![], message: "nothing to commit".into() };
    }
    if !errors.is_empty() {
        return CommitOutcome { ok: false, committed: done, message: errors.join("\n") };
    }
    CommitOutcome { ok: true, message: format!("committed {}", done.join(", ")), committed: done }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_status_variants() {
        assert_eq!(parse_status('?', '?'), GitStatus::Untracked);
        assert_eq!(parse_status(' ', 'M'), GitStatus::Modified);
        assert_eq!(parse_status('M', ' '), GitStatus::Modified);
        assert_eq!(parse_status('M', 'M'), GitStatus::Modified);
        assert_eq!(parse_status('A', ' '), GitStatus::Added);
        assert_eq!(parse_status(' ', 'D'), GitStatus::Deleted);
        assert_eq!(parse_status('R', ' '), GitStatus::Renamed);
        assert_eq!(parse_status('U', 'U'), GitStatus::Conflict);
    }

    #[test]
    fn track_state_own_repo() {
        let dir = std::env::temp_dir().join(format!("podarcis-track-repo-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        Command::new("git").args(["init"]).current_dir(&dir).output().unwrap();
        let state = track_state(&dir, &dir);
        assert!(matches!(state, TrackState::Repo(_)));
        assert!(state.branch().is_some());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn track_state_named_branch() {
        let dir = std::env::temp_dir().join(format!("podarcis-track-named-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        Command::new("git").args(["init"]).current_dir(&dir).output().unwrap();
        Command::new("git").args(["checkout", "-b", "my-feature"]).current_dir(&dir).output().unwrap();
        let state = track_state(&dir, &dir);
        assert_eq!(state, TrackState::Repo("my-feature".to_string()));
        assert_eq!(state.branch(), Some("my-feature"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn track_state_nested_folder() {
        let dir = std::env::temp_dir().join(format!("podarcis-track-nested-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let wiki = dir.join("wiki");
        std::fs::create_dir_all(&wiki).unwrap();
        Command::new("git").args(["init"]).current_dir(&dir).output().unwrap();
        Command::new("git").args(["config", "user.name", "test"]).current_dir(&dir).output().unwrap();
        Command::new("git").args(["config", "user.email", "test@example.com"]).current_dir(&dir).output().unwrap();
        std::fs::write(wiki.join("a.md"), "x").unwrap();
        Command::new("git").args(["add", "wiki/a.md"]).current_dir(&dir).output().unwrap();
        Command::new("git").args(["commit", "-m", "init"]).current_dir(&dir).output().unwrap();
        assert_eq!(track_state(&wiki, &dir), TrackState::Tracked);
        assert_eq!(track_state(&wiki, &dir).branch(), None);

        let sources = dir.join("sources");
        std::fs::create_dir_all(&sources).unwrap();
        std::fs::write(sources.join("raw.md"), "x").unwrap();
        assert_eq!(track_state(&sources, &dir), TrackState::Untracked);
        assert_eq!(track_state(&sources, &dir).branch(), None);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn priority_merging() {
        assert_eq!(GitStatus::Modified.merge(GitStatus::Untracked), GitStatus::Modified);
        assert_eq!(GitStatus::Untracked.merge(GitStatus::Modified), GitStatus::Modified);
        assert_eq!(GitStatus::Conflict.merge(GitStatus::Modified), GitStatus::Conflict);
    }

    #[test]
    fn git_scan_in_temp_repo() {
        let dir = std::env::temp_dir().join(format!("podarcis-git-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("sub")).unwrap();

        // init git repo
        Command::new("git").args(["init"]).current_dir(&dir).output().unwrap();
        Command::new("git").args(["config", "user.name", "test"]).current_dir(&dir).output().unwrap();
        Command::new("git").args(["config", "user.email", "test@example.com"]).current_dir(&dir).output().unwrap();

        let f1 = dir.join("tracked.md");
        std::fs::write(&f1, "hello").unwrap();
        Command::new("git").args(["add", "tracked.md"]).current_dir(&dir).output().unwrap();
        Command::new("git").args(["commit", "-m", "init"]).current_dir(&dir).output().unwrap();

        // Modify tracked.md
        std::fs::write(&f1, "hello world").unwrap();

        // Create untracked in sub
        let f2 = dir.join("sub/untracked.md");
        std::fs::write(&f2, "new").unwrap();

        let map = GitMap::scan(&dir, &[]);
        assert_eq!(map.status_for(&f1), Some(GitStatus::Modified));
        assert_eq!(map.status_for(&f2), Some(GitStatus::Untracked));
        assert_eq!(map.status_for(&dir.join("sub")), Some(GitStatus::Untracked));
        assert_eq!(map.status_for(&dir), Some(GitStatus::Modified));

        let _ = std::fs::remove_dir_all(&dir);
    }

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("podarcis-git-sync-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn configure_identity(dir: &Path) {
        Command::new("git").args(["config", "user.name", "test"]).current_dir(dir).output().unwrap();
        Command::new("git").args(["config", "user.email", "test@example.com"]).current_dir(dir).output().unwrap();
    }

    #[test]
    fn ensure_local_repo_inits_and_makes_an_initial_commit() {
        let root = temp_dir("ensure-local");
        ensure_local_repo(&root, "wiki");
        let wiki = root.join("wiki");
        assert!(wiki.join(".git").is_dir());
        assert!(wiki.join("index.md").is_file());
        // Idempotent: calling again does not error or duplicate commits.
        ensure_local_repo(&root, "wiki");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn sync_repos_skips_gdrive_and_inits_missing_local() {
        let root = temp_dir("sync-basic");
        let mut repos = HashMap::new();
        repos.insert("sources".to_string(), "gdrive".to_string());
        let results = sync_repos(&root, &repos);

        let by_name = |n: &str| results.iter().find(|r| r.repo == n).unwrap();
        assert_eq!(by_name("sources").status, "ok");
        assert!(by_name("sources").message.contains("Google Drive"));
        assert!(root.join("wiki").join(".git").is_dir(), "missing repo gets initialised locally");
        assert!(root.join("workspace").join(".git").is_dir());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn sync_repos_clones_a_local_remote_then_pulls_it() {
        let remote = temp_dir("sync-remote");
        Command::new("git").args(["init", "--bare"]).current_dir(&remote).output().unwrap();

        // Seed the bare remote with one commit via a throwaway working copy.
        let seed = temp_dir("sync-seed");
        Command::new("git").args(["clone", remote.to_str().unwrap(), "."]).current_dir(&seed).output().unwrap();
        configure_identity(&seed);
        std::fs::write(seed.join("index.md"), "# Wiki\n").unwrap();
        Command::new("git").args(["add", "-A"]).current_dir(&seed).output().unwrap();
        Command::new("git").args(["commit", "-m", "seed"]).current_dir(&seed).output().unwrap();
        Command::new("git").args(["push", "origin", "HEAD:master"]).current_dir(&seed).output().unwrap();

        let root = temp_dir("sync-clone");
        let mut repos = HashMap::new();
        repos.insert("wiki".to_string(), remote.to_str().unwrap().to_string());
        let results = sync_repos(&root, &repos);
        let wiki = results.iter().find(|r| r.repo == "wiki").unwrap();
        assert_eq!(wiki.status, "ok", "{:?}", wiki.message);
        assert!(root.join("wiki").join("index.md").is_file());

        // Second sync pulls instead of cloning again.
        let results = sync_repos(&root, &repos);
        let wiki = results.iter().find(|r| r.repo == "wiki").unwrap();
        assert_eq!(wiki.status, "ok", "{:?}", wiki.message);

        for d in [&remote, &seed, &root] {
            let _ = std::fs::remove_dir_all(d);
        }
    }

    #[test]
    fn commit_dirty_refuses_when_the_lint_gate_is_red() {
        let root = temp_dir("commit-gate");
        let repos = HashMap::new();
        let outcome = commit_dirty(&root, &repos, false, "chore: sync");
        assert!(!outcome.ok);
        assert!(outcome.committed.is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn commit_dirty_commits_only_dirty_repos() {
        let root = temp_dir("commit-dirty");
        ensure_local_repo(&root, "wiki");
        ensure_local_repo(&root, "workspace");
        configure_identity(&root.join("wiki"));
        configure_identity(&root.join("workspace"));
        std::fs::write(root.join("wiki").join("new.md"), "hello").unwrap();

        let mut repos = HashMap::new();
        repos.insert("wiki".to_string(), "local".to_string());
        repos.insert("workspace".to_string(), "local".to_string());
        let outcome = commit_dirty(&root, &repos, true, "chore: sync");

        assert!(outcome.ok, "{}", outcome.message);
        assert_eq!(outcome.committed, vec!["wiki".to_string()], "workspace had nothing to commit");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn set_remote_adds_then_updates_origin() {
        let root = temp_dir("set-remote");
        ensure_local_repo(&root, "wiki");
        let wiki = root.join("wiki");
        set_remote(&wiki, "https://example.com/a.git");
        let out = Command::new("git").args(["remote", "get-url", "origin"]).current_dir(&wiki).output().unwrap();
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "https://example.com/a.git");

        set_remote(&wiki, "https://example.com/b.git");
        let out = Command::new("git").args(["remote", "get-url", "origin"]).current_dir(&wiki).output().unwrap();
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "https://example.com/b.git");
        let _ = std::fs::remove_dir_all(&root);
    }
}
