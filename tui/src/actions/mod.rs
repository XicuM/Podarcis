//! Background work.
//!
//! The rule is that no action ever runs on the UI thread. `qmd query` measures
//! around thirty seconds on a real checkout and a `git pull` is unbounded, so a
//! job runs on its own thread and reports back as an event, keeping the app
//! interactive while it runs.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::Sender;

use crate::event::AppEvent;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

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
}

#[derive(Debug, Default)]
pub struct JobRunner {
    pub running: Vec<Running>,
}

/// What a native (in-process) job reports back, once it finishes.
pub struct NativeOutcome {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl JobRunner {
    /// Run a Rust closure on its own thread and report the result back as a
    /// `JobResult`, exactly as a shelled-out subprocess would have.
    /// `args` is cosmetic — `on_job_done` dispatches on it, so callers should
    /// pass whatever a shelled-out equivalent would have used.
    pub fn spawn_native<F>(
        &mut self,
        label: impl Into<String>,
        args: Vec<String>,
        f: F,
        tx: Sender<AppEvent>,
    ) -> u64
    where
        F: FnOnce() -> NativeOutcome + Send + 'static,
    {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let label = label.into();
        self.running.push(Running { id, label: label.clone() });
        std::thread::spawn(move || {
            let outcome = f();
            let result = JobResult {
                id,
                label,
                args,
                code: outcome.code,
                stdout: outcome.stdout,
                stderr: outcome.stderr,
            };
            let _ = tx.send(AppEvent::JobDone(result));
        });
        id
    }

    pub fn finish(&mut self, id: u64) {
        self.running.retain(|j| j.id != id);
    }

    pub fn is_busy(&self) -> bool {
        !self.running.is_empty()
    }

    pub fn labels(&self) -> Vec<&str> {
        self.running.iter().map(|j| j.label.as_str()).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn runner_tracks_and_clears_running_jobs() {
        let mut runner = JobRunner::default();
        runner.running.push(Running { id: 3, label: "lint".into() });
        assert!(runner.is_busy());
        runner.finish(3);
        assert!(!runner.is_busy());
    }

    #[test]
    fn spawn_native_reports_the_outcome_and_clears_from_running() {
        let (tx, rx) = std::sync::mpsc::channel();
        let mut runner = JobRunner::default();
        let id = runner.spawn_native(
            "test job",
            vec!["repo".into(), "sync".into()],
            || NativeOutcome { code: 0, stdout: "{\"ok\": true}".into(), stderr: String::new() },
            tx,
        );
        assert!(runner.is_busy());
        assert_eq!(runner.labels(), vec!["test job"]);

        let event = rx.recv_timeout(std::time::Duration::from_secs(2)).unwrap();
        let AppEvent::JobDone(result) = event else { panic!("expected JobDone") };
        assert_eq!(result.id, id);
        assert!(result.ok());
        assert_eq!(result.args, vec!["repo".to_string(), "sync".to_string()]);
        runner.finish(result.id);
        assert!(!runner.is_busy());
    }
}
