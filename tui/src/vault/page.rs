//! A single markdown page: OKF frontmatter, body, and the derived facts the
//! reader, inspector and editor all need.
//!
//! Frontmatter is split by hand rather than with a crate because the editor
//! needs the exact line offset where the body starts — a lint finding reported
//! at file line 42 has to land on body line 42, not on parse line 42.

use std::path::{Path, PathBuf};

use serde_yaml_ng::Value;

/// The nine `type:` values the linter accepts (`wiki_check_links.KNOWN_OKF_TYPES`).
pub const KNOWN_TYPES: [&str; 9] = [
    "concept", "protocol", "entity", "overview", "synthesis", "guide", "meta", "recipe", "journal",
];

/// `wiki_check_links.MAX_WORDS` — pages over this are flagged.
pub const MAX_WORDS: usize = 1500;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SourceRef {
    pub id: String,
    pub resource: Option<String>,
    pub title: Option<String>,
    pub author: Option<String>,
    pub year: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Generated {
    pub by: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub at: Option<String>,
}

#[derive(Clone, Debug, Default)]
pub struct Okf {
    pub title: Option<String>,
    /// The `type:` key. `type` is a keyword in Rust.
    pub kind: Option<String>,
    pub description: Option<String>,
    pub category: Option<String>,
    pub rationale: Option<String>,
    pub status: Option<String>,
    pub generated: Generated,
    pub sources: Vec<SourceRef>,
    pub related: Vec<String>,
    pub present: bool,
}

#[derive(Clone, Debug)]
pub struct Page {
    pub path: PathBuf,
    /// Path relative to the checkout root, with `/` separators.
    pub rel: String,
    pub okf: Okf,
    /// 0-based line on which the body starts in the file. Frontmatter takes the lines
    /// before it, so `body_line(n) == file_line(n + body_start)`.
    pub body_start: usize,
    pub body: String,
    /// Words in the body, for the reader's header.
    pub words: usize,
    /// Words as the engine counts them: the whole file, split on whitespace.
    pub raw_words: usize,
    pub is_index: bool,
}

impl Page {
    pub fn load(path: &Path, root: &Path) -> std::io::Result<Self> {
        let raw = std::fs::read_to_string(path)?;
        Ok(Self::parse(&raw, path, root))
    }

    pub fn parse(raw: &str, path: &Path, root: &Path) -> Self {
        let (okf, body_start) = split_frontmatter(raw);
        let body: String = raw.lines().skip(body_start).collect::<Vec<_>>().join("\n");
        let is_index = matches!(
            path.file_name().and_then(|n| n.to_str()),
            Some("_index.md") | Some("index.md")
        );
        let rel = rel_path(path, root);
        let title = okf.title.clone().or_else(|| first_heading(&body)).unwrap_or_else(|| {
            path.file_stem().and_then(|s| s.to_str()).unwrap_or("untitled").replace('_', " ")
        });
        let mut okf = okf;
        okf.title = Some(title);
        Self {
            words: count_words(&body),
            raw_words: raw.split_whitespace().count(),
            path: path.to_path_buf(),
            rel,
            okf,
            body_start,
            body,
            is_index,
        }
    }

    pub fn title(&self) -> &str {
        self.okf.title.as_deref().unwrap_or("untitled")
    }

    /// Frontmatter is required for non-index pages under `wiki/`, `workspace/`
    /// or `user/` — the same scope the linter enforces.
    pub fn needs_frontmatter(&self) -> bool {
        !self.is_index
            && self
                .rel
                .split('/')
                .next()
                .is_some_and(|first| ["wiki", "workspace", "user"].contains(&first))
    }

    /// Over the linter's 1500-word cap. Mirrors the engine: the count is a
    /// whitespace split of the whole file, frontmatter included.
    pub fn over_word_limit(&self) -> bool {
        self.needs_frontmatter() && self.raw_words > MAX_WORDS
    }

    /// `[^id]: text` definitions, as `(id, a short description)`.
    ///
    /// Most pages in this wiki carry their citations here rather than in the
    /// frontmatter `sources:` list; the linter accepts either, so both the
    /// inspector and the editor's completion read both.
    pub fn footnote_definitions(&self) -> Vec<(String, String)> {
        self.body
            .lines()
            .filter_map(|line| {
                let rest = line.trim_start().strip_prefix("[^")?;
                let end = rest.find("]:")?;
                let label = rest[..end].to_string();
                if label.is_empty() {
                    return None;
                }
                let text = rest[end + 2..].trim();
                // Definitions here are markdown links; the link text reads
                // better than the URL.
                let text = text
                    .strip_prefix('[')
                    .and_then(|t| t.split_once(']').map(|(name, _)| name))
                    .unwrap_or(text);
                Some((label, text.chars().take(80).collect()))
            })
            .collect()
    }

    /// Footnote references in the body, in order of first appearance.
    pub fn footnote_refs(&self) -> Vec<String> {
        let mut seen: Vec<String> = Vec::new();
        for label in scan_footnote_refs(&self.body) {
            if !seen.contains(&label) {
                seen.push(label);
            }
        }
        seen
    }

}

/// Split a leading `---` YAML block. Returns the parsed frontmatter and the
/// 0-based line the body starts on.
fn split_frontmatter(raw: &str) -> (Okf, usize) {
    let mut lines = raw.lines();
    if lines.next().map(str::trim_end) != Some("---") {
        return (Okf::default(), 0);
    }
    let mut block = String::new();
    let mut consumed = 1usize;
    for line in lines {
        consumed += 1;
        if line.trim_end() == "---" {
            let mut okf = parse_okf(&block);
            okf.present = true;
            return (okf, consumed);
        }
        block.push_str(line);
        block.push('\n');
    }
    // Unterminated block: treat the whole file as body rather than swallowing it.
    (Okf::default(), 0)
}

fn parse_okf(block: &str) -> Okf {
    let doc: Value = match serde_yaml_ng::from_str(block) {
        Ok(v) => v,
        // A broken block is reported by the linter, which parses the file
        // itself; here it simply means there are no facts to show.
        Err(_) => return Okf::default(),
    };
    let get = |key: &str| doc.get(key).and_then(as_text);
    Okf {
        title: get("title"),
        kind: get("type"),
        description: get("description"),
        category: get("category"),
        rationale: get("rationale"),
        status: get("status"),
        generated: doc.get("generated").map(parse_generated).unwrap_or_default(),
        sources: doc.get("sources").map(parse_sources).unwrap_or_default(),
        related: doc
            .get("related")
            .and_then(Value::as_sequence)
            .map(|seq| seq.iter().filter_map(as_text).collect())
            .unwrap_or_default(),
        present: false,
    }
}

fn parse_generated(value: &Value) -> Generated {
    let get = |key: &str| value.get(key).and_then(as_text);
    Generated { by: get("by"), model: get("model"), effort: get("effort"), at: get("at") }
}

fn parse_sources(value: &Value) -> Vec<SourceRef> {
    let Some(seq) = value.as_sequence() else {
        // The linter requires a list; a scalar here is a real error but we still
        // show what is there rather than rendering an empty inspector.
        return as_text(value).map(|id| vec![SourceRef { id, ..Default::default() }]).unwrap_or_default();
    };
    seq.iter()
        .filter_map(|entry| match entry {
            Value::Mapping(_) => entry.get("id").and_then(as_text).map(|id| SourceRef {
                id,
                resource: entry.get("resource").and_then(as_text),
                title: entry.get("title").and_then(as_text),
                author: entry.get("author").and_then(as_text),
                year: entry
                    .get("year")
                    .or_else(|| entry.get("last_modified"))
                    .and_then(as_text),
            }),
            other => as_text(other).map(|id| SourceRef { id, ..Default::default() }),
        })
        .collect()
}

/// YAML scalars arrive as strings, ints or floats depending on how they were
/// written; every consumer here wants text.
fn as_text(value: &Value) -> Option<String> {
    match value {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        Value::Bool(b) => Some(b.to_string()),
        _ => None,
    }
}

pub fn rel_path(path: &Path, root: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace(std::path::MAIN_SEPARATOR, "/")
}

fn first_heading(body: &str) -> Option<String> {
    body.lines()
        .find_map(|line| line.strip_prefix("# ").map(|t| t.trim().to_string()))
        .filter(|t| !t.is_empty())
}

/// Word count over the body only, matching the linter: fenced code is skipped.
fn count_words(body: &str) -> usize {
    let mut in_fence = false;
    body.lines()
        .filter(|line| {
            if line.trim_start().starts_with("```") {
                in_fence = !in_fence;
                return false;
            }
            !in_fence
        })
        .map(|line| line.split_whitespace().count())
        .sum()
}

/// `[^label]` occurrences outside fenced code, excluding definitions
/// (`[^label]:` at the start of a line).
pub fn scan_footnote_refs(body: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut in_fence = false;
    for line in body.lines() {
        if line.trim_start().starts_with("```") {
            in_fence = !in_fence;
            continue;
        }
        if in_fence {
            continue;
        }
        let bytes = line.as_bytes();
        let mut i = 0usize;
        while i + 2 < bytes.len() {
            if bytes[i] == b'[' && bytes[i + 1] == b'^' {
                if let Some(end) = line[i + 2..].find(']') {
                    let label = &line[i + 2..i + 2 + end];
                    let after = i + 2 + end + 1;
                    let is_definition = line[..i].trim().is_empty()
                        && line.as_bytes().get(after) == Some(&b':');
                    if !label.is_empty() && !is_definition && !label.contains('[') {
                        out.push(label.to_string());
                    }
                    i = after;
                    continue;
                }
            }
            i += 1;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn page(raw: &str) -> Page {
        Page::parse(raw, Path::new("/r/wiki/x/y.md"), Path::new("/r"))
    }

    const FULL: &str = "---\ntitle: Caffeine\ntype: concept\ncategory: health/nutrition\nrationale: A stimulant.\nstatus: verified\ngenerated:\n  by: podarcis:synthesizer\n  model: claude-sonnet-5\n  effort: high\n  at: '2026-01-01'\nsources:\n  - id: lin_2023\n    resource: ../../sources/lin_2023/metadata.md\n    title: Caffeine and grey matter\n    author: Lin et al.\n    year: 2023\n  - bare_id\n---\n# Caffeine\n\nBlocks adenosine[^lin_2023].\n";

    #[test]
    fn parses_full_okf_frontmatter() {
        let p = page(FULL);
        assert!(p.okf.present);
        assert_eq!(p.okf.kind.as_deref(), Some("concept"));
        assert_eq!(p.okf.status.as_deref(), Some("verified"));
        assert_eq!(p.okf.generated.model.as_deref(), Some("claude-sonnet-5"));
        assert_eq!(p.okf.generated.effort.as_deref(), Some("high"));
        let ids: Vec<&str> = p.okf.sources.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, vec!["lin_2023", "bare_id"]);
        assert_eq!(p.okf.sources[0].author.as_deref(), Some("Lin et al."));
        assert_eq!(p.okf.sources[0].year.as_deref(), Some("2023"));
        assert_eq!(p.rel, "wiki/x/y.md");
    }

    #[test]
    fn body_start_is_the_line_the_body_really_begins_on() {
        let p = page(FULL);
        let file_lines: Vec<&str> = FULL.lines().collect();
        assert_eq!(file_lines[p.body_start], "# Caffeine");
        assert!(p.body.starts_with("# Caffeine"));
    }

    #[test]
    fn last_modified_is_accepted_as_the_year() {
        let p = page("---\nsources:\n  - id: hayes_2012\n    last_modified: 2012\n---\nbody[^hayes_2012]\n");
        assert_eq!(p.okf.sources[0].year.as_deref(), Some("2012"));
    }

    #[test]
    fn no_frontmatter_leaves_the_whole_file_as_body() {
        let p = page("# Index\n\nJust a list.\n");
        assert!(!p.okf.present);
        assert_eq!(p.body_start, 0);
        assert_eq!(p.title(), "Index");
    }

    #[test]
    fn unterminated_frontmatter_does_not_swallow_the_file() {
        let p = page("---\ntitle: x\n# Heading\n");
        assert_eq!(p.body_start, 0);
        assert!(p.body.contains("# Heading"));
    }

    #[test]
    fn footnote_definitions_are_read_with_their_link_text() {
        let p = page("---\ntitle: T\n---\nBody[^a][^b]\n\n[^a]: [Some paper title](../s/raw.md)\n[^b]: Plain prose\n[^]: not a definition\n");
        assert_eq!(
            p.footnote_definitions(),
            vec![
                ("a".to_string(), "Some paper title".to_string()),
                ("b".to_string(), "Plain prose".to_string()),
            ]
        );
    }

    #[test]
    fn word_count_skips_fenced_code_and_frontmatter() {
        let p = page("---\ntitle: t\n---\none two three\n\n```\nfour five six seven\n```\n");
        assert_eq!(p.words, 3);
    }

    #[test]
    fn index_pages_are_exempt_from_frontmatter() {
        let idx = Page::parse("# Index\n", Path::new("/r/wiki/_index.md"), Path::new("/r"));
        assert!(idx.is_index);
        assert!(!idx.needs_frontmatter());
        let src = Page::parse("x\n", Path::new("/r/sources/lit/a/raw.md"), Path::new("/r"));
        assert!(!src.needs_frontmatter(), "sources/ is raw evidence, not an OKF page");
    }

    #[test]
    fn title_falls_back_from_frontmatter_to_heading_to_filename() {
        assert_eq!(page("---\ntitle: From FM\n---\n# From Heading\n").title(), "From FM");
        assert_eq!(page("# From Heading\n").title(), "From Heading");
        assert_eq!(page("no heading at all\n").title(), "y");
    }
}
