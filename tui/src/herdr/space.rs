//! Project space (workspace) management in Herdr.
//!
//! Each Podarcis research project maps to its own space (workspace) in the
//! canonical `podarcis` Herdr session. Within the embedded TUI pane, Herdr's
//! spaces sidebar is hidden because the Podarcis TUI itself is scoped to the
//! active project. When opening or switching projects, Podarcis ensures the
//! Herdr space for that project exists and is focused.

use std::path::Path;
use std::process::Stdio;

use anyhow::Result;
use serde_json::Value;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkspaceInfo {
    pub id: String,
    pub label: String,
    pub focused: bool,
    pub pane_count: usize,
    pub tab_count: usize,
}

/// Parse workspaces array from Herdr's JSON response.
pub fn parse_workspaces(val: &Value) -> Vec<WorkspaceInfo> {
    if val.get("error").is_some() {
        return Vec::new();
    }

    let Some(workspaces) = val
        .get("result")
        .and_then(|r| r.get("workspaces"))
        .and_then(Value::as_array)
    else {
        return Vec::new();
    };

    let mut list = Vec::new();
    for ws in workspaces {
        let Some(id) = ws.get("workspace_id").and_then(Value::as_str) else { continue };
        let label = ws.get("label").and_then(Value::as_str).unwrap_or("");
        let focused = ws.get("focused").and_then(Value::as_bool).unwrap_or(false);
        let pane_count = ws.get("pane_count").and_then(Value::as_u64).unwrap_or(0) as usize;
        let tab_count = ws.get("tab_count").and_then(Value::as_u64).unwrap_or(0) as usize;
        list.push(WorkspaceInfo {
            id: id.to_string(),
            label: label.to_string(),
            focused,
            pane_count,
            tab_count,
        });
    }
    list
}

/// Query `herdr --session podarcis workspace list`.
/// Returns Ok(workspaces), or Ok(vec![]) if Herdr is missing or server is not running.
pub fn list_workspaces() -> Result<Vec<WorkspaceInfo>> {
    let mut cmd = super::config::herdr_cmd();
    cmd.args(["workspace", "list"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let output = match cmd.output() {
        Ok(out) => out,
        Err(_) => return Ok(Vec::new()),
    };

    if !output.status.success() {
        return Ok(Vec::new());
    }

    let val: Value = match serde_json::from_slice(&output.stdout) {
        Ok(v) => v,
        Err(_) => return Ok(Vec::new()),
    };

    Ok(parse_workspaces(&val))
}

/// Focus a workspace in the Herdr session.
pub fn focus_workspace(workspace_id: &str) -> Result<()> {
    let mut cmd = super::config::herdr_cmd();
    cmd.args(["workspace", "focus", workspace_id])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let _ = cmd.status();
    Ok(())
}

/// Rename a workspace in the Herdr session.
pub fn rename_workspace(workspace_id: &str, new_label: &str) -> Result<()> {
    let mut cmd = super::config::herdr_cmd();
    cmd.args(["workspace", "rename", workspace_id, new_label])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let _ = cmd.status();
    Ok(())
}

/// Extract workspace ID from workspace create JSON response.
pub fn parse_created_workspace_id(val: &Value) -> Option<String> {
    val.get("result")
        .and_then(|r| r.get("workspace"))
        .and_then(|w| w.get("workspace_id"))
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// Create a new workspace in the Herdr session for a project.
pub fn create_workspace(label: &str, cwd: &Path, focus: bool) -> Result<Option<String>> {
    let mut cmd = super::config::herdr_cmd();
    cmd.args(["workspace", "create", "--label", label, "--cwd"])
        .arg(cwd)
        .arg(if focus { "--focus" } else { "--no-focus" })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let output = match cmd.output() {
        Ok(out) => out,
        Err(_) => return Ok(None),
    };

    if !output.status.success() {
        return Ok(None);
    }

    let val: Value = match serde_json::from_slice(&output.stdout) {
        Ok(v) => v,
        Err(_) => return Ok(None),
    };

    Ok(parse_created_workspace_id(&val))
}

/// Close a workspace in the Herdr session.
pub fn close_workspace(workspace_id: &str) -> Result<()> {
    let mut cmd = super::config::herdr_cmd();
    cmd.args(["workspace", "close", workspace_id])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let _ = cmd.status();
    Ok(())
}

/// Ensure that a workspace exists for the project and is focused.
///
/// If a workspace with label == `project_name` exists, focus it.
/// If only a default/unnamed workspace (e.g. `~`) exists, rename and focus it.
/// Otherwise, create a new workspace with label `project_name` and cwd `project_root`.
pub fn ensure_project_space(project_name: &str, project_root: &Path) -> Result<Option<String>> {
    let workspaces = list_workspaces()?;
    if workspaces.is_empty() {
        // Herdr server is not running yet; client spawn will start it.
        return Ok(None);
    }

    // 1. Existing workspace with exact label
    if let Some(existing) = workspaces.iter().find(|w| w.label == project_name) {
        if !existing.focused {
            let _ = focus_workspace(&existing.id);
        }
        return Ok(Some(existing.id.clone()));
    }

    // 2. Check if any workspace with label `~` or `default` has panes in project_root
    for ws in &workspaces {
        if ws.label == "~" || ws.label == "default" {
            let mut cmd = super::config::herdr_cmd();
            cmd.args(["pane", "list", "--workspace", &ws.id])
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            if let Ok(out) = cmd.output() {
                if let Ok(val) = serde_json::from_slice::<Value>(&out.stdout) {
                    if let Some(panes) = val.get("result").and_then(|r| r.get("panes")).and_then(Value::as_array) {
                        let matches_root = panes.iter().any(|p| {
                            p.get("cwd")
                                .or_else(|| p.get("foreground_cwd"))
                                .and_then(Value::as_str)
                                .map(|c| Path::new(c) == project_root || Path::new(c).starts_with(project_root))
                                .unwrap_or(false)
                        });
                        if matches_root {
                            let _ = rename_workspace(&ws.id, project_name);
                            if !ws.focused {
                                let _ = focus_workspace(&ws.id);
                            }
                            return Ok(Some(ws.id.clone()));
                        }
                    }
                }
            }
        }
    }

    // 3. If there's an initial default workspace that is the only one in the session
    if workspaces.len() == 1 {
        let first = &workspaces[0];
        let dir_name = project_root.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if first.label == "~" || first.label == "default" || first.label == dir_name {
            let _ = rename_workspace(&first.id, project_name);
            if !first.focused {
                let _ = focus_workspace(&first.id);
            }
            return Ok(Some(first.id.clone()));
        }
    }

    // 4. Otherwise, create a new workspace for this project and focus it
    create_workspace(project_name, project_root, true)
}

/// Pre-create a workspace for a project without focusing it (used during project creation).
pub fn create_workspace_if_server_running(project_name: &str, project_root: &Path) -> Result<Option<String>> {
    let workspaces = list_workspaces()?;
    if workspaces.is_empty() {
        return Ok(None);
    }
    if let Some(existing) = workspaces.iter().find(|w| w.label == project_name) {
        return Ok(Some(existing.id.clone()));
    }
    create_workspace(project_name, project_root, false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_workspaces_extracts_fields() {
        let json_data = serde_json::json!({
            "id": "cli:workspace:list",
            "result": {
                "type": "workspace_list",
                "workspaces": [
                    {
                        "workspace_id": "w1",
                        "label": "current-dev",
                        "focused": true,
                        "pane_count": 3,
                        "tab_count": 2
                    },
                    {
                        "workspace_id": "w2",
                        "label": "longevity",
                        "focused": false,
                        "pane_count": 1,
                        "tab_count": 1
                    }
                ]
            }
        });
        let list = parse_workspaces(&json_data);
        assert_eq!(list.len(), 2);
        assert_eq!(list[0].id, "w1");
        assert_eq!(list[0].label, "current-dev");
        assert!(list[0].focused);
        assert_eq!(list[1].id, "w2");
        assert_eq!(list[1].label, "longevity");
        assert!(!list[1].focused);
    }

    #[test]
    fn parse_workspaces_handles_server_not_running() {
        let json_err = serde_json::json!({
            "id": "cli:workspace:list",
            "error": {
                "code": "server_not_running",
                "message": "no herdr server is running"
            }
        });
        let list = parse_workspaces(&json_err);
        assert!(list.is_empty());
    }

    #[test]
    fn parse_created_workspace_id_extracts_id() {
        let json_create = serde_json::json!({
            "id": "cli:workspace:create",
            "result": {
                "type": "workspace_created",
                "workspace": {
                    "workspace_id": "w8",
                    "label": "macro"
                }
            }
        });
        assert_eq!(parse_created_workspace_id(&json_create), Some("w8".to_string()));
    }
}
