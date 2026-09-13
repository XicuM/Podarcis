//! Keep herdr tab labels in sync with live agent status.
//!
//! Same rule as herdr-companion: `🟡 grok`, `🔴 claude`, `shell` when there is
//! no agent. Polls `herdr api snapshot` and calls `herdr tab rename` when the
//! label is stale. Best-effort — a missed rename is retried on the next tick.

use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;

const INTERVAL: Duration = Duration::from_secs(2);

pub fn spawn(cwd: &Path) -> Arc<AtomicBool> {
    let stop = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&stop);
    let root = cwd.to_path_buf();
    std::thread::spawn(move || {
        while !flag.load(Ordering::Relaxed) {
            let _ = sync_once(&root);
            std::thread::sleep(INTERVAL);
        }
    });
    stop
}

pub fn status_emoji(status: &str) -> &'static str {
    match status {
        "working" => "🟡",
        "blocked" => "🔴",
        "done" => "🟢",
        "idle" => "⚪",
        _ => "",
    }
}

pub fn desired_label(agent: Option<&str>, status: &str) -> String {
    let name = agent.filter(|s| !s.is_empty()).unwrap_or("shell");
    let emoji = status_emoji(status);
    if emoji.is_empty() {
        name.to_string()
    } else {
        format!("{emoji} {name}")
    }
}

fn sync_once(root: &Path) -> Result<(), ()> {
    let snap = snapshot()?;
    for (tab_id, wanted) in planned_renames(&snap, root) {
        rename_tab(&tab_id, &wanted);
    }
    Ok(())
}

/// Tabs whose live label does not match `emoji + agent`, scoped to `root`.
///
/// Matches herdr-companion's `buildConversations`: both `agent` and `status`
/// come from the *same* pane object (falling back to the tab only for
/// status). The snapshot's top-level `agents` array is a separate, only
/// eventually-consistent view of the same data — pulling the name from
/// there while the status comes from `pane`/`tab` let the two desync.
pub fn planned_renames(snap: &Value, root: &Path) -> Vec<(String, String)> {
    let Some(tabs) = snap.get("tabs").and_then(Value::as_array) else {
        return Vec::new();
    };
    let panes = snap.get("panes").and_then(Value::as_array);
    let mut out = Vec::new();

    for tab in tabs {
        let Some(tab_id) = tab.get("tab_id").and_then(Value::as_str) else {
            continue;
        };
        // No pane means we cannot confirm this tab belongs to `root` — skip
        // rather than falling through the scope filter unchecked.
        let Some(pane) = panes.and_then(|ps| {
            ps.iter().find(|p| p.get("tab_id").and_then(Value::as_str) == Some(tab_id))
        }) else {
            continue;
        };
        let cwd = pane
            .get("cwd")
            .or_else(|| pane.get("foreground_cwd"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if !cwd.is_empty() && Path::new(cwd) != root {
            continue;
        }
        let agent = pane.get("agent").and_then(Value::as_str);
        let status = pane
            .get("agent_status")
            .and_then(Value::as_str)
            .or_else(|| tab.get("agent_status").and_then(Value::as_str))
            .unwrap_or("unknown");
        let wanted = desired_label(agent, status);
        let current = tab.get("label").and_then(Value::as_str).unwrap_or("");
        if current != wanted {
            out.push((tab_id.to_string(), wanted));
        }
    }
    out
}

fn snapshot() -> Result<Value, ()> {
    let out = herdr_cmd()
        .args(["api", "snapshot"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .map_err(|_| ())?;
    if !out.status.success() {
        return Err(());
    }
    let v: Value = serde_json::from_slice(&out.stdout).map_err(|_| ())?;
    v.get("result")
        .and_then(|r| r.get("snapshot"))
        .cloned()
        .or_else(|| v.get("snapshot").cloned())
        .ok_or(())
}

fn rename_tab(tab_id: &str, label: &str) {
    let _ = herdr_cmd()
        .args(["tab", "rename", tab_id, label])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn herdr_cmd() -> Command {
    let mut cmd = Command::new(super::pty::herdr_binary());
    cmd.args(["--session", super::config::SESSION]);
    if let Some(path) = super::config::session_dir().map(|d| d.join("config.toml")) {
        if path.exists() {
            cmd.env("HERDR_CONFIG_PATH", path);
        }
    }
    cmd
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn working_agent_gets_the_yellow_dot() {
        assert_eq!(desired_label(Some("grok"), "working"), "🟡 grok");
        assert_eq!(desired_label(Some("claude"), "blocked"), "🔴 claude");
        assert_eq!(desired_label(Some("codex"), "done"), "🟢 codex");
        assert_eq!(desired_label(Some("opencode"), "idle"), "⚪ opencode");
    }

    #[test]
    fn unknown_status_is_just_the_name() {
        assert_eq!(desired_label(Some("grok"), "unknown"), "grok");
        assert_eq!(desired_label(None, "unknown"), "shell");
        assert_eq!(desired_label(Some(""), "working"), "🟡 shell");
    }

    #[test]
    fn snapshot_renames_stale_tabs_in_the_wiki_root() {
        let snap = serde_json::json!({
            "tabs": [
                {"tab_id": "w1:t1", "label": "1", "agent_status": "working"},
                {"tab_id": "w1:t2", "label": "🟡 grok", "agent_status": "working"},
                {"tab_id": "w2:t1", "label": "other", "agent_status": "idle"}
            ],
            "panes": [
                {"tab_id": "w1:t1", "agent": "grok", "agent_status": "working", "cwd": "/wiki"},
                {"tab_id": "w1:t2", "agent": "grok", "agent_status": "working", "cwd": "/wiki"},
                {"tab_id": "w2:t1", "agent": "claude", "agent_status": "idle", "cwd": "/elsewhere"}
            ]
        });
        let planned = planned_renames(&snap, Path::new("/wiki"));
        assert_eq!(planned, vec![("w1:t1".into(), "🟡 grok".into())]);
    }

    #[test]
    fn a_tab_with_no_matching_pane_is_skipped_rather_than_renamed() {
        // No pane means we cannot confirm the tab belongs to `root` — the
        // scope filter must not be bypassed just because there is nothing to
        // check it against.
        let snap = serde_json::json!({
            "tabs": [{"tab_id": "w1:t1", "label": "1", "agent_status": "working"}],
            "panes": []
        });
        assert_eq!(planned_renames(&snap, Path::new("/wiki")), Vec::<(String, String)>::new());
    }

    #[test]
    fn agent_name_and_status_are_read_from_the_same_pane_not_the_agents_list() {
        // The top-level `agents` array is a separate, only eventually
        // consistent view of the same data. If it were consulted for the
        // name while `pane`/`tab` still supply the status, a stale `agents`
        // entry could pair the wrong name with the current status.
        let snap = serde_json::json!({
            "tabs": [{"tab_id": "w1:t1", "label": "1", "agent_status": "working"}],
            "panes": [{"tab_id": "w1:t1", "cwd": "/wiki", "agent_status": "working"}],
            "agents": [{"tab_id": "w1:t1", "agent": "stale-agent", "agent_status": "done"}]
        });
        assert_eq!(
            planned_renames(&snap, Path::new("/wiki")),
            vec![("w1:t1".into(), "🟡 shell".into())]
        );
    }
}
