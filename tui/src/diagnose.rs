//! Platform pain-point log (`/podarcis diagnose`).
//!
//! Port of `diagnose_session.py`. Records live in
//! `.podarcis/diagnostics/pain_points.jsonl`, one JSON object per line. A
//! malformed line raises rather than being skipped: resolving rewrites the
//! whole file from the read list, so swallowing a bad line would delete it.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde_json::Value;

pub fn log_file(root: &Path) -> PathBuf {
    root.join(".podarcis").join("diagnostics").join("pain_points.jsonl")
}

/// Every pain-point record, oldest first.
pub fn read_all(root: &Path) -> Result<Vec<Value>> {
    let raw = std::fs::read_to_string(log_file(root)).unwrap_or_default();
    let mut out = Vec::new();
    for line in raw.lines() {
        if line.trim().is_empty() {
            continue;
        }
        out.push(serde_json::from_str(line).with_context(|| format!("malformed pain-point line: {line}"))?);
    }
    Ok(out)
}

/// Unresolved pain points, oldest first.
pub fn active(root: &Path) -> Result<Vec<Value>> {
    if !log_file(root).exists() {
        return Ok(Vec::new());
    }
    Ok(read_all(root)
        .context("reading pain points")?
        .into_iter()
        .filter(|r| r.get("resolved").and_then(Value::as_bool) != Some(true))
        .collect())
}

fn utc_now() -> String {
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = ts.as_secs();
    let days = secs / 86_400;
    let rem = secs % 86_400;
    let (y, d) = {
        let mut y = 1970_i64;
        let mut d = days;
        loop {
            let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
            let len = if leap { 366 } else { 365 };
            if d < len {
                break;
            }
            d -= len;
            y += 1;
        }
        (y, d)
    };
    let month_lens = if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 {
        [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    } else {
        [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    };
    let mut month = 0;
    let mut day = d;
    for (i, len) in month_lens.iter().enumerate() {
        if day < *len {
            month = i + 1;
            break;
        }
        day -= *len;
    }
    let (h, m) = (rem / 3600, (rem % 3600) / 60);
    let s = rem % 60;
    format!("{y:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}.{:03}+00:00", ts.subsec_millis())
}

/// Mark pain points resolved by id, by category, or all of them, writing the
/// whole file back. Returns the ids actually resolved.
pub fn resolve(root: &Path, ids: &[String], category: &str, sweep: bool) -> Result<Vec<String>> {
    let path = log_file(root);
    if !path.exists() {
        return Ok(Vec::new());
    }
    let mut records = read_all(root).context("reading pain points")?;
    let now = utc_now();
    let mut resolved = Vec::new();
    for record in records.iter_mut() {
        if record.get("resolved").and_then(Value::as_bool) == Some(true) {
            continue;
        }
        let matches = if !ids.is_empty() {
            ids.iter().any(|id| record.get("id").and_then(Value::as_str) == Some(id.as_str()))
        } else if !category.is_empty() {
            record.get("category").and_then(Value::as_str) == Some(category)
        } else if sweep {
            true
        } else {
            false
        };
        if !matches {
            continue;
        }
        let obj = record.as_object_mut().context("pain-point record is not an object")?;
        obj.insert("resolved".into(), Value::Bool(true));
        obj.insert("resolved_at".into(), Value::String(now.clone()));
        if let Some(id) = record.get("id").and_then(Value::as_str) {
            resolved.push(id.to_string());
        }
    }
    if !resolved.is_empty() {
        let mut out = String::new();
        for record in &records {
            out.push_str(&serde_json::to_string(record)?);
            out.push('\n');
        }
        std::fs::write(&path, out).context("writing pain points")?;
    }
    Ok(resolved)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("podarcis-diag-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir.join(".podarcis").join("diagnostics")).unwrap();
        dir
    }

    #[test]
    fn active_lists_only_unresolved() {
        let root = root("active");
        let mut raw = String::new();
        raw.push_str(&serde_json::json!({"id": "a", "resolved": false}).to_string());
        raw.push('\n');
        raw.push_str(&serde_json::json!({"id": "b", "resolved": true}).to_string());
        raw.push('\n');
        std::fs::write(log_file(&root), raw).unwrap();
        let all = active(&root).unwrap();
        assert_eq!(all.len(), 1);
        assert_eq!(all[0]["id"], "a");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn resolve_marks_by_id_and_persists() {
        let root = root("resolve_by_id");
        let mut raw = String::new();
        raw.push_str(&serde_json::json!({"id": "x", "category": "lint", "resolved": false}).to_string());
        raw.push('\n');
        raw.push_str(&serde_json::json!({"id": "y", "category": "net", "resolved": false}).to_string());
        raw.push('\n');
        std::fs::write(log_file(&root), raw).unwrap();
        let done = resolve(&root, &["x".to_string()], "", false).unwrap();
        assert_eq!(done, vec!["x"]);
        let recs = read_all(&root).unwrap();
        assert_eq!(recs[0]["resolved"], true);
        assert_eq!(recs[1]["resolved"], false);
        assert!(recs[0].get("resolved_at").is_some());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn resolve_by_category_and_sweep() {
        let root = root("category_and_sweep");
        let mut raw = String::new();
        raw.push_str(&serde_json::json!({"id": "1", "category": "lint", "resolved": false}).to_string());
        raw.push('\n');
        raw.push_str(&serde_json::json!({"id": "2", "category": "net", "resolved": false}).to_string());
        raw.push('\n');
        std::fs::write(log_file(&root), raw).unwrap();
        assert_eq!(resolve(&root, &[], "net", false).unwrap(), vec!["2"]);
        assert_eq!(resolve(&root, &[], "", true).unwrap(), vec!["1"]);
        assert!(active(&root).unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }
}