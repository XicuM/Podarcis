//! The linter. There is one, and this is it.
//!
//! It began as a port of `check_links.py`, which lived in two byte-identical
//! copies and was policed against this file by a `podarcis-tui --lint` differ.
//! All three are gone: the port was proven equal to the scripts over the whole
//! corpus (690 pages, identical findings), so keeping a second implementation
//! only bought a second thing to keep in step. `podarcis lint`, `wiki_lint`,
//! the agent-job autonomy gate and the front-end's commit gate all arrive
//! here now.
//!
//! Two rules look wrong and are not, so they are documented rather than
//! improved: a footnote may be satisfied by an in-body `[^id]:` definition as
//! well as by a `sources[].id`, and the word count is a whitespace split over
//! the whole file, frontmatter included.
//!
//! One behaviour was deliberately *not* carried over. The scripts deleted
//! every flat `.md` file in any directory named `recipes` that had
//! `bowls`/`lunches`/`dinners` subdirectories — a one-off migration left armed
//! inside a read-only audit, with no `--fix` guard and no mention in the
//! report. An audit does not delete the thing it audits.

use std::path::Path;

use serde_yaml_ng::Value;

use super::page::{KNOWN_TYPES, MAX_WORDS};

/// Folder bloat limit, counting subdirectories and non-index files.
pub const MAX_DIR_ENTRIES: usize = 15;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Finding {
    pub code: &'static str,
    pub detail: String,
    /// 0-based line in the file, where the finding anchors to one.
    pub line: Option<usize>,
}

/// Every code the linter emits, and how loudly to show it. A code that blocks
/// the commit gate is an error; the rest are warnings.
pub const CODES: [(&str, Severity); 9] = [
    ("broken_link", Severity::Error),
    ("missing_footnote", Severity::Error),
    ("unmatched_source", Severity::Error),
    ("missing_frontmatter", Severity::Error),
    ("yaml_error", Severity::Error),
    ("unused_footnote", Severity::Warn),
    ("positional_footnote", Severity::Warn),
    ("page_length", Severity::Warn),
    ("bloated_directory", Severity::Warn),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    Clean,
    Warn,
    Error,
}

pub fn severity(code: &str) -> Severity {
    CODES
        .iter()
        .find(|(name, _)| *name == code)
        .map(|(_, severity)| *severity)
        .unwrap_or(Severity::Warn)
}

/// Is this one of the codes declared in [`CODES`]?
///
/// [`CODES`] drives severity, the gutter and the JSON payload, so a finding
/// whose code is missing from it is invisible to all three. Asserted by the
/// tests rather than enforced at the call site: it is a property of this file
/// being internally consistent, not a runtime condition.
pub fn is_known(code: &str) -> bool {
    CODES.iter().any(|(name, _)| *name == code)
}

/// `raw.md` is extracted paper text, not an authored page.
pub fn is_skipped(path: &Path) -> bool {
    path.file_name().and_then(|n| n.to_str()) == Some("raw.md")
}

fn components(path: &Path) -> Vec<String> {
    path.components().map(|c| c.as_os_str().to_string_lossy().to_string()).collect()
}

fn in_okf_scope(path: &Path) -> bool {
    components(path).iter().any(|c| matches!(c.as_str(), "wiki" | "workspace" | "user"))
}

fn counts_words(path: &Path) -> bool {
    components(path).iter().any(|c| matches!(c.as_str(), "wiki" | "user"))
}

fn is_index(path: &Path) -> bool {
    matches!(path.file_name().and_then(|n| n.to_str()), Some("index.md") | Some("_index.md"))
}

/// Lint one file's contents. `path` is used for scope rules and link resolution.
pub fn check(content: &str, path: &Path) -> Vec<Finding> {
    let mut out = Vec::new();
    let stripped = strip_code(content);
    let mut source_ids: Vec<String> = Vec::new();

    if !is_index(path) && in_okf_scope(path) {
        frontmatter(content, &mut out, &mut source_ids);
    }

    links(&stripped, path, content, &mut out);
    footnotes(&stripped, content, &source_ids, &mut out);

    if counts_words(path) {
        let words = content.split_whitespace().count();
        if words > MAX_WORDS {
            out.push(Finding { code: "page_length", detail: words.to_string(), line: None });
        }
    }
    out
}

/// Fenced blocks then inline spans, in that order — two passes, because a
/// fence may contain backticks that are not an inline span.
fn strip_code(content: &str) -> String {
    let mut out = String::with_capacity(content.len());
    let mut rest = content;
    while let Some(start) = rest.find("```") {
        out.push_str(&rest[..start]);
        match rest[start + 3..].find("```") {
            Some(end) => rest = &rest[start + 3 + end + 3..],
            None => {
                rest = "";
                break;
            }
        }
    }
    out.push_str(rest);

    // Inline spans: a single run of backticks with no newline inside.
    let mut result = String::with_capacity(out.len());
    let mut rest = out.as_str();
    while let Some(start) = rest.find('`') {
        let after = &rest[start + 1..];
        let end = after.find('`').filter(|e| !after[..*e].contains('\n') && *e > 0);
        match end {
            Some(end) => {
                result.push_str(&rest[..start]);
                rest = &after[end + 1..];
            }
            None => break,
        }
    }
    result.push_str(rest);
    result
}

/// Split the leading `---` block. Returns the YAML and the line it starts on.
fn split_frontmatter(content: &str) -> Option<(String, usize)> {
    let trimmed = content.trim_start_matches([' ', '\t', '\n', '\r']);
    let skipped = content.len() - trimmed.len();
    let rest = trimmed.strip_prefix("---")?;
    let rest = rest.trim_start_matches([' ', '\t', '\r']).strip_prefix('\n')?;
    let end = rest.find("\n---")?;
    let start_line = content[..skipped].matches('\n').count();
    Some((rest[..end].to_string(), start_line + 1))
}

fn frontmatter(content: &str, out: &mut Vec<Finding>, source_ids: &mut Vec<String>) {
    let Some((yaml, first_line)) = split_frontmatter(content) else {
        out.push(Finding {
            code: "missing_frontmatter",
            detail: "Entire YAML frontmatter block is missing".into(),
            line: Some(0),
        });
        return;
    };

    let doc: Value = match serde_yaml_ng::from_str(&yaml) {
        Ok(value) => value,
        // An unquoted colon is repaired before giving up, and only reported as
        // a YAML error if the repair also fails. A real page reads
        // `rationale: Antifragile: Things That Gain from Disorder`, and calling
        // that an error would be a false positive the gate does not raise.
        Err(err) => match quote_unquoted_colons(&yaml).and_then(|fixed| serde_yaml_ng::from_str(&fixed).ok()) {
            Some(value) => value,
            None => {
                out.push(Finding {
                    code: "yaml_error",
                    detail: format!("Invalid YAML frontmatter: {err}"),
                    line: Some(first_line),
                });
                return;
            }
        },
    };

    let Some(map) = doc.as_mapping() else {
        if !doc.is_null() {
            out.push(Finding {
                code: "yaml_error",
                detail: "Frontmatter YAML must be a key-value mapping".into(),
                line: Some(first_line),
            });
        } else {
            // An empty block parses as null; treat it as `{}`, so the message
            // names the missing fields rather than the empty block.
            out.push(Finding {
                code: "missing_frontmatter",
                detail: "Missing required fields: type, category, rationale".into(),
                line: Some(first_line),
            });
        }
        return;
    };

    let has = |key: &str| map.contains_key(Value::String(key.to_string()));
    let missing: Vec<&str> = ["type", "category", "rationale"]
        .into_iter()
        .filter(|key| !has(key))
        .collect();
    if !missing.is_empty() {
        out.push(Finding {
            code: "missing_frontmatter",
            detail: format!("Missing required fields: {}", missing.join(", ")),
            line: Some(first_line),
        });
    }

    let kind = doc
        .get("type")
        .map(scalar_text)
        .unwrap_or_default()
        .to_lowercase();
    if !kind.is_empty() && !KNOWN_TYPES.contains(&kind.as_str()) {
        let mut known: Vec<&str> = KNOWN_TYPES.to_vec();
        known.sort_unstable();
        out.push(Finding {
            code: "yaml_error",
            detail: format!("Unknown OKF document type '{kind}' (expected one of: {})", known.join(", ")),
            line: Some(first_line),
        });
    }

    match doc.get("sources") {
        Some(Value::Sequence(items)) => {
            for item in items {
                match item {
                    Value::Mapping(_) => {
                        if let Some(id) = item.get("id") {
                            source_ids.push(scalar_text(id));
                        }
                    }
                    Value::String(s) => source_ids.push(s.clone()),
                    _ => {}
                }
            }
        }
        Some(Value::Null) | None => {}
        Some(_) => out.push(Finding {
            code: "yaml_error",
            detail: "'sources' field in frontmatter must be a list".into(),
            line: Some(first_line),
        }),
    }
}

/// Quote a single-line scalar that itself contains a colon, which is the one
/// YAML mistake a page author makes often enough to repair automatically —
/// `rationale: Antifragile: Things That Gain`. `None` when nothing changed.
fn quote_unquoted_colons(yaml: &str) -> Option<String> {
    let mut changed = false;
    let fixed: Vec<String> = yaml
        .lines()
        .map(|line| {
            if line.trim_start().starts_with('#') {
                return line.to_string();
            }
            let Some((prefix, value)) = split_key(line) else { return line.to_string() };
            let first = value.chars().next();
            if matches!(first, None | Some('"') | Some('\'')) || first.is_some_and(char::is_whitespace) {
                return line.to_string();
            }
            // Only a value that itself contains a colon needs quoting.
            if !value.contains(':') {
                return line.to_string();
            }
            changed = true;
            format!("{prefix}\"{}\"", value.replace('"', "\\\""))
        })
        .collect();
    changed.then(|| fixed.join("\n"))
}

/// `^(\s*[\w_-]+\s*:\s*)(rest)$` — the key part and the value part.
fn split_key(line: &str) -> Option<(String, &str)> {
    let indent = line.len() - line.trim_start().len();
    let rest = &line[indent..];
    let colon = rest.find(':')?;
    let key = &rest[..colon];
    if key.is_empty() || !key.chars().all(|c| c.is_alphanumeric() || c == '_' || c == '-') {
        return None;
    }
    let after = &rest[colon + 1..];
    let spaces = after.len() - after.trim_start_matches([' ', '\t']).len();
    let value = &after[spaces..];
    Some((line[..indent + colon + 1 + spaces].to_string(), value))
}

fn scalar_text(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        Value::Bool(b) => b.to_string(),
        _ => String::new(),
    }
}

/// `\[[^\]]+\]\(([^)]+)\)`, hand-rolled.
pub fn markdown_links(content: &str) -> Vec<String> {
    let bytes = content.as_bytes();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] != b'[' {
            i += 1;
            continue;
        }
        let after_bracket = &content[i + 1..];
        let Some(text_len) = after_bracket.find(']') else { break };
        if text_len == 0 || after_bracket.as_bytes().get(text_len + 1) != Some(&b'(') {
            i += 1;
            continue;
        }
        let target_start = i + 1 + text_len + 2;
        let Some(target_len) = content[target_start..].find(')') else { break };
        out.push(content[target_start..target_start + target_len].to_string());
        i = target_start + target_len + 1;
    }
    out
}

fn links(stripped: &str, path: &Path, raw: &str, out: &mut Vec<Finding>) {
    let dir = path.parent().unwrap_or(Path::new("."));
    for link in markdown_links(stripped) {
        if link.starts_with("http://")
            || link.starts_with("https://")
            || link.starts_with('#')
            || link.starts_with("mailto:")
            || link.starts_with("gdrive:")
        {
            continue;
        }
        let clean = link.split(['#', '?']).next().unwrap_or("");
        if clean.is_empty() {
            continue;
        }
        let target = super::links::normalize(&dir.join(clean));
        if !target.exists() {
            out.push(Finding {
                code: "broken_link",
                detail: format!("{link} -> {}", target.display()),
                line: line_of(raw, &format!("]({link})")),
            });
        }
    }
}

/// `^\s*\[\^id\]:` — a definition. `\s*` spans newlines,
/// which only ever widens what counts as a definition, never narrows it.
fn footnote_defs(content: &str) -> Vec<String> {
    content
        .lines()
        .filter_map(|line| {
            let trimmed = line.trim_start();
            let rest = trimmed.strip_prefix("[^")?;
            let end = rest.find("]:")?;
            let label = &rest[..end];
            valid_label(label).then(|| label.to_string())
        })
        .collect()
}

fn valid_label(label: &str) -> bool {
    !label.is_empty() && label.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// References, with definition lines removed first.
fn footnote_refs(content: &str) -> Vec<String> {
    let body: String = content
        .lines()
        .filter(|line| {
            let trimmed = line.trim_start();
            !trimmed
                .strip_prefix("[^")
                .and_then(|rest| rest.find("]:").map(|end| valid_label(&rest[..end])))
                .unwrap_or(false)
        })
        .collect::<Vec<_>>()
        .join("\n");

    let mut out = Vec::new();
    let mut rest = body.as_str();
    while let Some(start) = rest.find("[^") {
        let after = &rest[start + 2..];
        match after.find(']') {
            Some(end) if valid_label(&after[..end]) => {
                out.push(after[..end].to_string());
                rest = &after[end + 1..];
            }
            _ => rest = after,
        }
    }
    out
}

fn footnotes(stripped: &str, raw: &str, source_ids: &[String], out: &mut Vec<Finding>) {
    let defs = footnote_defs(stripped);
    let refs = footnote_refs(stripped);

    for label in dedup(refs.iter().filter(|r| !defs.contains(r))) {
        out.push(Finding {
            code: "missing_footnote",
            detail: label.clone(),
            line: line_of(raw, &format!("[^{label}]")),
        });
    }
    for label in dedup(defs.iter().filter(|d| !refs.contains(d))) {
        out.push(Finding {
            code: "unused_footnote",
            detail: label.clone(),
            line: line_of(raw, &format!("[^{label}]:")),
        });
    }
    for label in dedup(refs.iter().filter(|r| !source_ids.contains(r) && !defs.contains(r))) {
        out.push(Finding {
            code: "unmatched_source",
            detail: label.clone(),
            line: line_of(raw, &format!("[^{label}]")),
        });
    }
    let mut positional: Vec<String> =
        dedup(refs.iter().filter(|r| r.chars().all(|c| c.is_ascii_digit())));
    positional.sort();
    for label in positional {
        out.push(Finding {
            code: "positional_footnote",
            detail: label.clone(),
            line: line_of(raw, &format!("[^{label}]")),
        });
    }
}

fn dedup<'a>(iter: impl Iterator<Item = &'a String>) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for item in iter {
        if !out.contains(item) {
            out.push(item.clone());
        }
    }
    out
}

fn line_of(content: &str, needle: &str) -> Option<usize> {
    content.lines().position(|line| line.contains(needle))
}

/// The directory-bloat rule: directories and non-index files.
pub fn bloat(entries: usize) -> Option<Finding> {
    (entries > MAX_DIR_ENTRIES).then(|| Finding {
        code: "bloated_directory",
        detail: entries.to_string(),
        line: None,
    })
}

/// Validate a standalone `.yaml`/`.yml` file, the way `check_yaml_file` did.
///
/// Config and job declarations are linted too: a `.podarcis/config.yaml` that
/// stopped parsing is the kind of breakage that otherwise surfaces as a
/// puzzling default three commands later.
pub fn check_yaml(content: &str) -> Vec<Finding> {
    match serde_yaml_ng::from_str::<Value>(content) {
        Ok(_) => Vec::new(),
        Err(err) => {
            // serde_yaml_ng renders `at line N, column M` into its Display,
            // where PyYAML put it in a separate `problem_mark`. Both end up in
            // `detail`, so the text differs while the code does not.
            let line = err.location().map(|loc| loc.line().saturating_sub(1));
            vec![Finding { code: "yaml_error", detail: format!("Invalid YAML syntax: {err}"), line }]
        }
    }
}

/// Repair a frontmatter block whose only fault is an unquoted scalar
/// containing a colon, returning the whole file's new contents.
///
/// This is the entire `--fix` surface, and deliberately so: it is the one
/// repair that cannot change what a page means, because the value it quotes
/// was already meant as text. `None` when there is nothing to fix, or when
/// quoting does not make the block parse — a file that is broken some other
/// way is reported, never rewritten on a guess.
pub fn fix_frontmatter(content: &str) -> Option<String> {
    let (yaml, _) = split_frontmatter(content)?;
    if serde_yaml_ng::from_str::<Value>(&yaml).is_ok() {
        return None;
    }
    let fixed = quote_unquoted_colons(&yaml)?;
    serde_yaml_ng::from_str::<Value>(&fixed).ok()?;
    let start = content.find(&yaml)?;
    let mut out = String::with_capacity(content.len() + 16);
    out.push_str(&content[..start]);
    out.push_str(&fixed);
    out.push_str(&content[start + yaml.len()..]);
    Some(out)
}

/// The `podarcis lint --json` object, built from an already-built `Index`
/// rather than a fresh walk — every page's findings are already sitting there,
/// kept fresh by the file watcher.
pub fn to_json_payload(index: &super::index::Index) -> serde_json::Value {
    let mut files = serde_json::Map::new();
    for entry in &index.entries {
        if entry.findings.is_empty() {
            continue;
        }
        let issues: Vec<serde_json::Value> = entry
            .findings
            .iter()
            .map(|f| serde_json::json!({"code": f.code, "detail": f.detail}))
            .collect();
        files.insert(entry.rel.clone(), serde_json::Value::Array(issues));
    }
    for (rel, finding) in &index.dir_findings {
        let entry = files.entry(rel.clone()).or_insert_with(|| serde_json::Value::Array(vec![]));
        if let serde_json::Value::Array(arr) = entry {
            arr.push(serde_json::json!({"code": finding.code, "detail": finding.detail}));
        }
    }
    serde_json::json!({
        "ok": files.is_empty(),
        "root": index.root.display().to_string(),
        "files": files,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn codes(findings: &[Finding]) -> Vec<&str> {
        findings.iter().map(|f| f.code).collect()
    }

    fn wiki(name: &str) -> PathBuf {
        PathBuf::from("/r/wiki").join(name)
    }

    const OK: &str = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\n# T\n";

    #[test]
    fn a_well_formed_page_has_no_findings() {
        assert!(check(OK, &wiki("a.md")).is_empty());
    }

    #[test]
    fn an_in_body_definition_satisfies_a_footnote() {
        // This is the rule that matters most: the real wiki cites this way, and
        // getting it wrong would flag hundreds of clean pages.
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\nClaim[^lin_2023].\n\n[^lin_2023]: Lin et al.\n";
        assert!(check(raw, &wiki("a.md")).is_empty(), "{:?}", check(raw, &wiki("a.md")));
    }

    #[test]
    fn a_frontmatter_source_id_also_satisfies_a_footnote() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\nsources:\n  - id: lin_2023\n---\nClaim[^lin_2023].\n";
        assert_eq!(codes(&check(raw, &wiki("a.md"))), vec!["missing_footnote"]);
    }

    #[test]
    fn a_footnote_with_neither_is_both_missing_and_unmatched() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\nClaim[^nope].\n";
        assert_eq!(codes(&check(raw, &wiki("a.md"))), vec!["missing_footnote", "unmatched_source"]);
    }

    #[test]
    fn an_unreferenced_definition_is_unused() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\nNo citations.\n\n[^orphan]: Someone\n";
        assert_eq!(codes(&check(raw, &wiki("a.md"))), vec!["unused_footnote"]);
    }

    #[test]
    fn positional_footnotes_are_reported_in_order() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\nA[^2] B[^1].\n\n[^1]: x\n[^2]: y\n";
        let findings = check(raw, &wiki("a.md"));
        let positional: Vec<&str> = findings
            .iter()
            .filter(|f| f.code == "positional_footnote")
            .map(|f| f.detail.as_str())
            .collect();
        assert_eq!(positional, vec!["1", "2"], "sorted, not in order of appearance");
    }

    #[test]
    fn missing_frontmatter_names_the_fields_that_are_missing() {
        let findings = check("# No frontmatter\n", &wiki("a.md"));
        assert_eq!(findings[0].code, "missing_frontmatter");
        assert_eq!(findings[0].detail, "Entire YAML frontmatter block is missing");

        let findings = check("---\ntitle: T\n---\nbody\n", &wiki("a.md"));
        assert_eq!(findings[0].detail, "Missing required fields: type, category, rationale");
    }

    #[test]
    fn an_unknown_type_is_a_yaml_error_not_a_missing_field() {
        let raw = "---\ntitle: T\ntype: nonsense\ncategory: c\nrationale: r\n---\nbody\n";
        let findings = check(raw, &wiki("a.md"));
        assert_eq!(codes(&findings), vec!["yaml_error"]);
        assert!(findings[0].detail.contains("Unknown OKF document type 'nonsense'"));
        assert!(findings[0].detail.contains("concept"));
    }

    #[test]
    fn an_unquoted_colon_in_a_value_is_repaired_not_reported() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: Antifragile: Things That Gain from Disorder\n---\nbody\n";
        assert!(check(raw, &wiki("a.md")).is_empty(), "{:?}", check(raw, &wiki("a.md")));
    }

    // ---- ported from the deleted test_yaml_checker.py / test_check_links.py

    #[test]
    fn a_valid_standalone_yaml_file_has_no_findings() {
        assert!(check_yaml("name: Podarcis\nversion: 1.0\n").is_empty());
    }

    #[test]
    fn an_invalid_standalone_yaml_file_is_a_yaml_error() {
        let findings = check_yaml("name: Podarcis\n  version: : 1.0\n");
        assert_eq!(codes(&findings), ["yaml_error"]);
        assert!(findings[0].detail.starts_with("Invalid YAML syntax"), "{:?}", findings[0]);
    }

    #[test]
    fn fix_quotes_a_value_containing_a_colon_and_leaves_the_body_alone() {
        let raw = "---\ntitle: Protocol: State Transitions\ntype: protocol\ncategory: psychology\nrationale: test\n---\nBody text\n";
        let fixed = fix_frontmatter(raw).expect("repairable");
        assert!(fixed.contains("title: \"Protocol: State Transitions\""), "{fixed}");
        assert!(fixed.ends_with("---\nBody text\n"), "{fixed}");
    }

    #[test]
    fn fix_is_a_no_op_on_frontmatter_that_already_parses() {
        let raw = "---\ntitle: \"Test Note\"\ntype: concept\ncategory: test\nrationale: text\n---\nBody\n";
        assert_eq!(fix_frontmatter(raw), None);
        // And so idempotent: fixing twice cannot drift.
        let once = "---\ntitle: A: B\ntype: concept\ncategory: c\nrationale: r\n---\nb\n";
        let fixed = fix_frontmatter(once).expect("repairable");
        assert_eq!(fix_frontmatter(&fixed), None);
    }

    #[test]
    fn fix_refuses_yaml_that_quoting_cannot_save() {
        // Broken some other way: reported by `check`, never rewritten on a guess.
        let raw = "---\ntitle: T\n  bad indent\n    worse: [unclosed\n---\nbody\n";
        assert_eq!(fix_frontmatter(raw), None);
        assert!(check(raw, &wiki("a.md")).iter().any(|f| f.code == "yaml_error"));
    }

    #[test]
    fn fix_leaves_a_file_with_no_frontmatter_untouched() {
        assert_eq!(fix_frontmatter("Just a body, no frontmatter.\n"), None);
    }

    #[test]
    fn yaml_that_no_repair_can_save_is_still_reported() {
        let raw = "---\ntitle: T\n  bad indent\n    worse: [unclosed\n---\nbody\n";
        assert!(check(raw, &wiki("a.md")).iter().any(|f| f.code == "yaml_error"));
    }

    #[test]
    fn already_quoted_values_are_left_alone() {
        assert!(quote_unquoted_colons("title: \"A: B\"\n").is_none());
        assert!(quote_unquoted_colons("title: plain\n").is_none());
        assert_eq!(quote_unquoted_colons("title: A: B").unwrap(), "title: \"A: B\"");
    }

    #[test]
    fn sources_must_be_a_list() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\nsources: just_one\n---\nbody\n";
        assert_eq!(codes(&check(raw, &wiki("a.md"))), vec!["yaml_error"]);
    }

    #[test]
    fn index_pages_and_non_okf_paths_are_exempt_from_frontmatter() {
        assert!(check("# Index\n", &wiki("_index.md")).is_empty());
        assert!(check("raw text\n", Path::new("/r/sources/lit/a/metadata.md")).is_empty());
    }

    #[test]
    fn raw_extracted_text_is_skipped_entirely() {
        assert!(is_skipped(Path::new("/r/sources/lit/a/raw.md")));
        assert!(!is_skipped(Path::new("/r/sources/lit/a/metadata.md")));
    }

    #[test]
    fn the_word_count_is_a_whitespace_split_of_the_whole_file() {
        // Frontmatter and code count too. MAX_WORDS was calibrated against
        // counts taken this way, so counting "better" would silently move the
        // threshold rather than improve it.
        let body = "word ".repeat(MAX_WORDS);
        let raw = format!("---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\n{body}");
        let findings = check(&raw, &wiki("a.md"));
        assert_eq!(codes(&findings), vec!["page_length"]);
        assert!(findings[0].detail.parse::<usize>().unwrap() > MAX_WORDS);
    }

    #[test]
    fn workspace_pages_need_frontmatter_but_are_not_length_capped() {
        let body = "word ".repeat(MAX_WORDS + 100);
        let raw = format!("---\ntitle: T\ntype: protocol\ncategory: c\nrationale: r\n---\n{body}");
        assert!(check(&raw, Path::new("/r/workspace/p.md")).is_empty());
    }

    #[test]
    fn code_is_stripped_before_links_and_footnotes_are_seen() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\n```\n[x](nope.md) [^ghost]\n```\n\nInline `[y](nope.md)` too.\n";
        assert!(check(raw, &wiki("a.md")).is_empty());
    }

    #[test]
    fn external_and_anchor_links_are_never_broken() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\n[a](https://x.dev) [b](#s) [c](mailto:x@y.z) [d](gdrive:abc)\n";
        assert!(check(raw, &wiki("a.md")).is_empty());
    }

    #[test]
    fn a_broken_link_reports_the_resolved_target() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\nSee [G](../gone.md).\n";
        let findings = check(raw, &wiki("a.md"));
        assert_eq!(codes(&findings), vec!["broken_link"]);
        assert_eq!(findings[0].detail, "../gone.md -> /r/gone.md");
        assert_eq!(findings[0].line, Some(6));
    }

    #[test]
    fn link_anchors_are_stripped_before_resolving() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\n[G](../gone.md#section)\n";
        assert_eq!(check(raw, &wiki("a.md"))[0].detail, "../gone.md#section -> /r/gone.md");
    }

    #[test]
    fn the_link_regex_stops_where_a_markdown_link_stops() {
        assert_eq!(markdown_links("[a](x.md) [b](y.md)"), vec!["x.md", "y.md"]);
        assert_eq!(markdown_links("[]()"), Vec::<String>::new(), "empty text does not match");
        assert_eq!(markdown_links("[a](b(1).md)"), vec!["b(1"], "stops at the first closing paren");
        assert!(markdown_links("[^footnote]").is_empty());
    }

    #[test]
    fn every_emitted_code_is_declared_in_codes() {
        let raw = "---\ntitle: T\ntype: bogus\n---\n[a](gone.md) [^x] [^1]\n\n[^orphan]: y\n";
        for finding in check(raw, &wiki("a.md")) {
            assert!(is_known(finding.code), "{} is missing from CODES", finding.code);
        }
    }

    #[test]
    fn repeated_problems_are_reported_once() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\n[^x] [^x] [^x]\n";
        let missing: Vec<&Finding> = check(raw, &wiki("a.md")).iter().filter(|f| f.code == "missing_footnote").cloned().collect::<Vec<_>>().leak().iter().collect();
        assert_eq!(missing.len(), 1);
    }

    #[test]
    fn severity_separates_gate_blockers_from_advice() {
        assert_eq!(severity("broken_link"), Severity::Error);
        assert_eq!(severity("page_length"), Severity::Warn);
        assert_eq!(severity("something_new"), Severity::Warn, "an unknown code is not silent");
    }

    #[test]
    fn directory_bloat_starts_above_the_limit() {
        assert!(bloat(MAX_DIR_ENTRIES).is_none());
        assert_eq!(bloat(MAX_DIR_ENTRIES + 1).unwrap().detail, (MAX_DIR_ENTRIES + 1).to_string());
    }
}
