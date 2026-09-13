//! Background work: everything that shells out to the Python engine.
//!
//! The rule is that no action ever runs on the UI thread. `qmd query` measures
//! around thirty seconds on a real checkout and a `git pull` is unbounded, so a
//! job is spawned, its output streamed back as events, and the app stays
//! interactive while it runs.

use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::Sender;

use crate::event::AppEvent;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Debug)]
pub struct JobSpec {
    /// Shown in the status bar while the job runs.
    pub label: String,
    pub args: Vec<String>,
    /// Collect stdout for the caller instead of streaming it as progress.
    pub capture: bool,
}

impl JobSpec {
    pub fn new(label: impl Into<String>, args: &[&str]) -> Self {
        Self {
            label: label.into(),
            args: args.iter().map(|s| s.to_string()).collect(),
            capture: false,
        }
    }

    pub fn capturing(label: impl Into<String>, args: &[&str]) -> Self {
        Self { capture: true, ..Self::new(label, args) }
    }
}

#[derive(Clone, Debug)]
pub struct JobResult {
    pub id: u64,
    pub label: String,
    pub args: Vec<String>,
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl JobResult {
    pub fn ok(&self) -> bool {
        self.code == 0
    }

    /// Parse stdout as JSON. Engine commands print a rich banner before their
    /// payload in some modes, so we start from the first `{` or `[`.
    pub fn json(&self) -> Option<serde_json::Value> {
        let start = self.stdout.find(['{', '['])?;
        serde_json::from_str(&self.stdout[start..]).ok()
    }
}

#[derive(Debug)]
pub struct Running {
    pub id: u64,
    pub label: String,
    pub last_line: String,
}

#[derive(Debug, Default)]
pub struct JobRunner {
    pub running: Vec<Running>,
}

impl JobRunner {
    /// Spawn `podarcis <args>` in the checkout. Returns the job id.
    pub fn spawn(&mut self, cli: &Path, root: &Path, spec: JobSpec, tx: Sender<AppEvent>) -> u64 {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        self.running.push(Running { id, label: spec.label.clone(), last_line: String::new() });

        let cli = cli.to_path_buf();
        let root = root.to_path_buf();
        std::thread::spawn(move || {
            let result = run(id, &cli, &root, &spec, &tx);
            let _ = tx.send(AppEvent::JobDone(result));
        });
        id
    }

    pub fn finish(&mut self, id: u64) {
        self.running.retain(|j| j.id != id);
    }

    pub fn note(&mut self, id: u64, line: String) {
        if let Some(job) = self.running.iter_mut().find(|j| j.id == id) {
            job.last_line = line;
        }
    }

    pub fn is_busy(&self) -> bool {
        !self.running.is_empty()
    }

    pub fn labels(&self) -> Vec<&str> {
        self.running.iter().map(|j| j.label.as_str()).collect()
    }
}

fn run(id: u64, cli: &Path, root: &Path, spec: &JobSpec, tx: &Sender<AppEvent>) -> JobResult {
    let mut child = match Command::new(cli)
        .args(&spec.args)
        .current_dir(root)
        .env("PODARCIS_ROOT", root)
        // The engine's rich output is for a human terminal; ours is a pane.
        .env("NO_COLOR", "1")
        .env("TERM", "dumb")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(err) => {
            return JobResult {
                id,
                label: spec.label.clone(),
                args: spec.args.clone(),
                code: -1,
                stdout: String::new(),
                stderr: format!("could not run {}: {err}", cli.display()),
            }
        }
    };

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    let err_handle = std::thread::spawn(move || {
        let mut buf = String::new();
        if let Some(stderr) = stderr {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                buf.push_str(&line);
                buf.push('\n');
            }
        }
        buf
    });

    let mut collected = String::new();
    if let Some(stdout) = stdout {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if !spec.capture && !line.trim().is_empty() {
                let _ = tx.send(AppEvent::JobLine(id, line.trim().to_string()));
            }
            collected.push_str(&line);
            collected.push('\n');
        }
    }

    let code = child.wait().ok().and_then(|s| s.code()).unwrap_or(-1);
    JobResult {
        id,
        label: spec.label.clone(),
        args: spec.args.clone(),
        code,
        stdout: collected,
        stderr: err_handle.join().unwrap_or_default(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn result(stdout: &str, code: i32) -> JobResult {
        JobResult {
            id: 1,
            label: "t".into(),
            args: vec![],
            code,
            stdout: stdout.into(),
            stderr: String::new(),
        }
    }

    #[test]
    fn json_skips_a_banner_printed_before_the_payload() {
        let r = result("Podarcis 10.6.0\n{\"ok\": true}\n", 0);
        assert_eq!(r.json().unwrap()["ok"], serde_json::json!(true));
    }

    #[test]
    fn json_handles_a_top_level_array() {
        let r = result("[{\"repo\": \"wiki\"}]", 0);
        assert_eq!(r.json().unwrap()[0]["repo"], serde_json::json!("wiki"));
    }

    #[test]
    fn json_is_none_when_there_is_no_payload() {
        assert!(result("no json here", 0).json().is_none());
        assert!(result("{not valid", 0).json().is_none());
    }

    #[test]
    fn exit_code_decides_success_not_the_output() {
        assert!(result("", 0).ok());
        assert!(!result("{\"ok\": true}", 1).ok());
    }

    #[test]
    fn a_missing_binary_is_reported_not_panicked() {
        let (tx, rx) = std::sync::mpsc::channel();
        let spec = JobSpec::new("t", &["status"]);
        let r = run(1, &PathBuf::from("/nonexistent/podarcis"), &std::env::temp_dir(), &spec, &tx);
        assert_eq!(r.code, -1);
        assert!(r.stderr.contains("could not run"));
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn streams_progress_lines_then_reports_the_result() {
        let (tx, rx) = std::sync::mpsc::channel();
        let spec = JobSpec::new("echo", &["-e", "one\\ntwo"]);
        let r = run(7, &PathBuf::from("/bin/echo"), &std::env::temp_dir(), &spec, &tx);
        assert_eq!(r.code, 0);
        let lines: Vec<String> = rx
            .try_iter()
            .filter_map(|e| match e {
                AppEvent::JobLine(id, line) => {
                    assert_eq!(id, 7);
                    Some(line)
                }
                _ => None,
            })
            .collect();
        assert_eq!(lines, vec!["one", "two"]);
    }

    #[test]
    fn capturing_jobs_do_not_stream() {
        let (tx, rx) = std::sync::mpsc::channel();
        let spec = JobSpec::capturing("echo", &["hello"]);
        let r = run(1, &PathBuf::from("/bin/echo"), &std::env::temp_dir(), &spec, &tx);
        assert_eq!(r.stdout.trim(), "hello");
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn runner_tracks_and_clears_running_jobs() {
        let mut runner = JobRunner::default();
        runner.running.push(Running { id: 3, label: "lint".into(), last_line: String::new() });
        assert!(runner.is_busy());
        runner.note(3, "checking".into());
        assert_eq!(runner.running[0].last_line, "checking");
        runner.finish(3);
        assert!(!runner.is_busy());
    }
}
