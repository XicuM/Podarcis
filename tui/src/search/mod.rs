//! Search, in three flavours, all reachable from one overlay.
//!
//! * **Files** — fuzzy over titles and paths. Runs on every keystroke.
//! * **Text** — literal or regex over file contents. Runs on every keystroke.
//! * **Semantic** — `qmd query`/`qmd vsearch`, called directly. Explicit only.
//!
//! The split exists because the third one is slow: `qmd query` measures around
//! thirty seconds on a real checkout. Putting it on the typing path would make
//! the whole app feel broken, so it is a deliberate action with a spinner.

use std::path::{Path, PathBuf};
use std::process::Command;

use grep_regex::RegexMatcherBuilder;
use grep_searcher::sinks::UTF8;
use grep_searcher::{BinaryDetection, SearcherBuilder};
use nucleo_matcher::pattern::{CaseMatching, Normalization, Pattern};
use nucleo_matcher::{Config, Matcher, Utf32Str};

use crate::vault::index::{Entry, Index};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Mode {
    #[default]
    Files,
    Text,
    Semantic,
}

impl Mode {
    pub fn label(self) -> &'static str {
        match self {
            Self::Files => "files",
            Self::Text => "text",
            Self::Semantic => "semantic",
        }
    }

    pub fn next(self) -> Self {
        match self {
            Self::Files => Self::Text,
            Self::Text => Self::Semantic,
            Self::Semantic => Self::Files,
        }
    }

    /// Whether results update as you type. Semantic search does not.
    pub fn is_live(self) -> bool {
        !matches!(self, Self::Semantic)
    }
}

#[derive(Clone, Debug)]
pub struct Hit {
    pub path: PathBuf,
    pub rel: String,
    pub title: String,
    /// Line of the match, when there is one.
    pub line: Option<usize>,
    /// Context shown under the title.
    pub snippet: String,
    /// Character offsets in `rel` that matched, for highlighting.
    pub matched: Vec<u32>,
    pub score: u32,
}

/// Cap on results kept. Beyond this a query is too broad to pick from anyway,
/// and the cap is what keeps a one-letter text search instant.
pub const MAX_HITS: usize = 200;

pub struct Engine {
    matcher: Matcher,
    buf: Vec<char>,
    indices: Vec<u32>,
}

impl Default for Engine {
    fn default() -> Self {
        Self { matcher: Matcher::new(Config::DEFAULT.match_paths()), buf: Vec::new(), indices: Vec::new() }
    }
}

impl Engine {
    /// Fuzzy match over `title — rel`, so typing either the human name or the
    /// path finds the page.
    pub fn files(&mut self, index: &Index, query: &str, collection: Option<&str>) -> Vec<Hit> {
        let candidates: Vec<&Entry> = index
            .entries
            .iter()
            .filter(|e| collection.is_none_or(|c| e.collection() == c))
            .collect();

        if query.trim().is_empty() {
            let mut hits: Vec<Hit> = candidates.iter().map(|e| hit_for(e, Vec::new(), 0)).collect();
            hits.sort_by(|a, b| rank(&a.rel).cmp(&rank(&b.rel)).then_with(|| a.rel.cmp(&b.rel)));
            hits.truncate(MAX_HITS);
            return hits;
        }

        let pattern = Pattern::parse(query, CaseMatching::Ignore, Normalization::Smart);
        let mut hits: Vec<Hit> = Vec::new();
        for entry in candidates {
            let haystack = format!("{} {}", entry.title, entry.rel);
            self.buf.clear();
            let utf32 = Utf32Str::new(&haystack, &mut self.buf);
            self.indices.clear();
            let Some(score) = pattern.indices(utf32, &mut self.matcher, &mut self.indices) else {
                continue;
            };
            // Report highlight offsets against `rel`, which is what we draw.
            let shift = entry.title.chars().count() as u32 + 1;
            let matched: Vec<u32> =
                self.indices.iter().filter(|i| **i >= shift).map(|i| i - shift).collect();
            hits.push(hit_for(entry, matched, score));
        }
        // Authored pages outrank raw evidence: `sources/` is three times the
        // size of the wiki, and without this every query drowns in it.
        hits.sort_by(|a, b| {
            rank(&a.rel)
                .cmp(&rank(&b.rel))
                .then_with(|| b.score.cmp(&a.score))
                .then_with(|| a.rel.cmp(&b.rel))
        });
        hits.truncate(MAX_HITS);
        hits
    }

    /// Literal or regex search over file contents.
    pub fn text(&mut self, index: &Index, query: &str, collection: Option<&str>, regex: bool) -> Vec<Hit> {
        if query.trim().is_empty() {
            return Vec::new();
        }
        let pattern = if regex { query.to_string() } else { escape_literal(query) };
        let Ok(matcher) = RegexMatcherBuilder::new().case_insensitive(true).build(&pattern) else {
            return Vec::new();
        };
        let mut searcher = SearcherBuilder::new()
            .binary_detection(BinaryDetection::quit(0))
            .line_number(true)
            .build();

        let mut hits = Vec::new();
        for entry in index
            .entries
            .iter()
            .filter(|e| collection.is_none_or(|c| e.collection() == c))
        {
            if hits.len() >= MAX_HITS {
                break;
            }
            let mut first: Option<(u64, String)> = None;
            let _ = searcher.search_path(
                &matcher,
                &entry.path,
                UTF8(|line_no, line| {
                    if first.is_none() {
                        first = Some((line_no, line.trim().to_string()));
                    }
                    // One hit per file: the overlay is for finding a page, and
                    // the reader's own `/` is for finding a spot inside it.
                    Ok(false)
                }),
            );
            if let Some((line_no, snippet)) = first {
                hits.push(Hit {
                    path: entry.path.clone(),
                    rel: entry.rel.clone(),
                    title: entry.title.clone(),
                    line: Some(line_no.saturating_sub(1) as usize),
                    snippet: truncate(&snippet, 160),
                    matched: Vec::new(),
                    score: 0,
                });
            }
        }
        hits
    }
}

/// Collection precedence: wiki, then workspace, then everything else.
fn rank(rel: &str) -> u8 {
    match rel.split('/').next() {
        Some("wiki") => 0,
        Some("workspace") => 1,
        _ => 2,
    }
}

fn hit_for(entry: &Entry, matched: Vec<u32>, score: u32) -> Hit {
    Hit {
        path: entry.path.clone(),
        rel: entry.rel.clone(),
        title: entry.title.clone(),
        line: None,
        snippet: [entry.status.as_deref(), entry.category.as_deref(), entry.kind.as_deref()]
            .into_iter()
            .flatten()
            .next()
            .unwrap_or_else(|| entry.collection())
            .to_string(),
        matched,
        score,
    }
}

/// Collections `qmd` is scoped to, and the directories each one covers.
/// Mirrors `search.py::COLLECTION_DIRS`.
const COLLECTIONS: [&str; 4] = ["wiki", "protocols", "sources", "all"];

const DEFAULT_LIMIT: usize = 20;

/// Is a `qmd` binary reachable at all? An absent `engines.qmd` key is not a
/// decision the user made, so the config asks this rather than defaulting to
/// "off" and then blaming a line nobody wrote.
pub fn qmd_on_path() -> bool {
    crate::herdr::pty::which("qmd").is_some()
}

fn run_qmd(root: &Path, args: &[&str]) -> Result<String, String> {
    let out = Command::new("qmd")
        .args(args)
        .current_dir(root)
        .output()
        .map_err(|err| format!("could not run qmd: {err}"))?;
    if !out.status.success() {
        let text = if !out.stderr.is_empty() { &out.stderr } else { &out.stdout };
        return Err(format!("qmd {} failed: {}", args.join(" "), String::from_utf8_lossy(text).trim()));
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// Strip a `qmd://` scheme or leading `./`, then make it root-relative.
/// Mirrors `search.py::_normalize_qmd_path`.
fn normalize_qmd_path(raw: &str, root: &Path) -> String {
    let s = raw.trim();
    let s = s.strip_prefix("qmd://").unwrap_or(s);
    let s = s.strip_prefix("./").unwrap_or(s);
    let p = Path::new(s);
    if p.is_absolute() {
        p.strip_prefix(root)
            .map(|rel| rel.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| s.to_string())
    } else {
        s.trim_start_matches('/').to_string()
    }
}

fn infer_collection(rel: &str) -> &'static str {
    if rel.starts_with("workspace/protocols/") || rel == "workspace/protocols" {
        "protocols"
    } else if rel.starts_with("sources/") {
        "sources"
    } else {
        "wiki"
    }
}

/// The `title:` frontmatter field, or `None` if there is none. Mirrors
/// `search.py::_title_of`, minus the OS-error handling Rust doesn't need.
fn extract_title(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let head: String = text.chars().take(4000).collect();
    let body = head.strip_prefix("---")?;
    let body = body.strip_prefix('\n').unwrap_or(body);
    let end = body.find("\n---")?;
    for line in body[..end].lines() {
        if let Some(rest) = line.strip_prefix("title:") {
            let v = rest.trim().trim_matches(['\'', '"']);
            if !v.is_empty() {
                return Some(v.to_string());
            }
        }
    }
    None
}

fn number_after(text: &str, marker: &str) -> Option<u64> {
    let idx = text.find(marker)?;
    let digits: String =
        text[idx + marker.len()..].trim_start().chars().take_while(|c| c.is_ascii_digit() || *c == ',').collect();
    (!digits.is_empty()).then(|| digits.replace(',', "").parse().unwrap_or(0))
}

/// A warning for index conditions `qmd` does not report itself. Mirrors
/// `search.py::parse_index_health`.
fn parse_index_health(status_text: &str) -> Option<String> {
    let lower = status_text.to_lowercase();
    if number_after(&lower, "vectors:") == Some(0) {
        let pending = number_after(&lower, "pending:").unwrap_or(0);
        return Some(format!(
            "QMD index has NO embeddings ({pending} documents pending). Semantic and hybrid \
             search cannot work — results below are keyword-only. Run `qmd embed`."
        ));
    }
    let idx = lower.find("updated:")?;
    let rest = lower[idx + "updated:".len()..].trim_start();
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    let amount: u64 = digits.parse().ok()?;
    let unit = rest[digits.len()..].chars().next()?;
    let days = match unit {
        'h' => amount as f64 / 24.0,
        'd' => amount as f64,
        _ => 0.0,
    };
    (days >= 7.0)
        .then(|| format!("QMD index was last updated {amount}{unit} ago and may not reflect recent edits — run `qmd update`."))
}

/// Reshape `qmd query`/`qmd vsearch --json` output into the `{path, title,
/// score, collection, snippet}` shape `parse_semantic` expects. Mirrors
/// `search.py::_hits_from_qmd_json`.
fn hits_from_qmd_json(payload: &serde_json::Value, root: &Path, collection: &str, limit: usize) -> Vec<serde_json::Value> {
    let items: Vec<&serde_json::Value> = match payload {
        serde_json::Value::Array(a) => a.iter().collect(),
        serde_json::Value::Object(_) => ["hits", "results", "items"]
            .iter()
            .find_map(|k| payload.get(k).and_then(|v| v.as_array()))
            .map(|a| a.iter().collect())
            .unwrap_or_default(),
        _ => Vec::new(),
    };

    let mut hits = Vec::new();
    for item in items {
        let Some(obj) = item.as_object() else { continue };
        let raw = ["path", "file", "filepath"]
            .iter()
            .find_map(|k| obj.get(*k).and_then(|v| v.as_str()))
            .unwrap_or("");
        let rel = normalize_qmd_path(raw, root);
        if rel.is_empty() {
            continue;
        }
        let abs = root.join(&rel);
        let title = obj
            .get("title")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .or_else(|| extract_title(&abs))
            .unwrap_or_else(|| Path::new(&rel).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default());
        let score = obj.get("score").and_then(|v| v.as_f64());
        let snippet = ["snippet", "body", "text"]
            .iter()
            .find_map(|k| obj.get(*k).and_then(|v| v.as_str()))
            .unwrap_or("");
        let snippet = truncate(snippet.trim(), 240);
        let coll = if collection == "all" { infer_collection(&rel) } else { collection };
        hits.push(serde_json::json!({
            "path": rel, "title": title, "score": score, "collection": coll, "snippet": snippet,
        }));
        if hits.len() >= limit {
            break;
        }
    }
    hits
}

/// Run a semantic search through `qmd` directly and return the same
/// `{query, collection, method, warning, hits}` shape `parse_semantic` and
/// `semantic_warning` already know how to read — previously produced by
/// `podarcis wiki search --json` (`search.py::search`), now built here so the
/// TUI no longer shells out to the Python engine for it. Runs two `qmd`
/// invocations (the query, then a quick `status` for the index-health
/// warning), so callers should run this off the UI thread.
pub fn semantic_search(root: &Path, query: &str, collection: &str) -> Result<String, String> {
    let coll = if COLLECTIONS.contains(&collection) { collection } else { "wiki" };
    let limit = DEFAULT_LIMIT;
    let limit_s = limit.to_string();

    let mut args = vec!["query", query, "-n", &limit_s];
    if coll != "all" {
        args.push("-c");
        args.push(coll);
    }
    args.push("--json");

    let raw = run_qmd(root, &args)?;
    let payload: serde_json::Value =
        if raw.trim().is_empty() { serde_json::Value::Array(vec![]) } else { serde_json::from_str(&raw).unwrap_or(serde_json::Value::Array(vec![])) };
    let hits = hits_from_qmd_json(&payload, root, coll, limit);

    let warning = run_qmd(root, &["status"]).ok().and_then(|text| parse_index_health(&text));

    Ok(serde_json::json!({
        "query": query, "collection": coll, "method": "hybrid", "warning": warning, "hits": hits,
    })
    .to_string())
}

/// Parse the JSON `search::semantic_search` produces.
pub fn parse_semantic(value: &serde_json::Value, root: &Path) -> Vec<Hit> {
    value
        .get("hits")
        .and_then(|h| h.as_array())
        .map(|hits| {
            hits.iter()
                .filter_map(|hit| {
                    let rel = hit.get("path")?.as_str()?.to_string();
                    let rel = rel.trim_start_matches("./").to_string();
                    Some(Hit {
                        path: root.join(&rel),
                        title: hit
                            .get("title")
                            .and_then(|t| t.as_str())
                            .unwrap_or(&rel)
                            .to_string(),
                        line: None,
                        snippet: truncate(
                            hit.get("snippet").and_then(|s| s.as_str()).unwrap_or("").trim(),
                            160,
                        ),
                        matched: Vec::new(),
                        score: hit.get("score").and_then(|s| s.as_f64()).unwrap_or(0.0).max(0.0) as u32,
                        rel,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// The warning `podarcis wiki search` emits when the index is stale or qmd is
/// unavailable. Worth showing: it explains empty results.
pub fn semantic_warning(value: &serde_json::Value) -> Option<String> {
    value
        .get("warning")
        .and_then(|w| w.as_str())
        .map(str::trim)
        .filter(|w| !w.is_empty())
        .map(str::to_string)
}

/// Escape every regex metacharacter so a literal query is literal.
fn escape_literal(query: &str) -> String {
    let mut out = String::with_capacity(query.len() * 2);
    for ch in query.chars() {
        if "\\.+*?()|[]{}^$#&-~".contains(ch) {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}

fn truncate(text: &str, max: usize) -> String {
    let text = text.replace(['\n', '\r'], " ");
    if text.chars().count() <= max {
        return text;
    }
    let cut: String = text.chars().take(max.saturating_sub(1)).collect();
    format!("{cut}…")
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture(PathBuf, Index);

    impl Fixture {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("podarcis-search-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            let files = [
                ("wiki/health/caffeine.md", "---\ntitle: Caffeine\ntype: concept\ncategory: health\nrationale: r\n---\nBlocks adenosine receptors.\n"),
                ("wiki/health/creatine.md", "---\ntitle: Creatine\ntype: concept\ncategory: health\nrationale: r\n---\nRaises phosphocreatine stores.\n"),
                ("workspace/protocols/sleep.md", "---\ntitle: Sleep protocol\ntype: protocol\ncategory: health\nrationale: r\n---\nAvoid caffeine after noon.\n"),
            ];
            for (rel, body) in files {
                let path = dir.join(rel);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(path, body).unwrap();
            }
            let dirs: Vec<PathBuf> = ["wiki", "workspace"].iter().map(|d| dir.join(d)).collect();
            let index = Index::build(&dir, &dirs);
            Self(dir, index)
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn rels(hits: &[Hit]) -> Vec<&str> {
        hits.iter().map(|h| h.rel.as_str()).collect()
    }

    #[test]
    fn fuzzy_finds_by_title_and_by_path() {
        let f = Fixture::new("fuzzy");
        let mut e = Engine::default();
        assert_eq!(rels(&e.files(&f.1, "caffeine", None)), vec!["wiki/health/caffeine.md"]);
        assert_eq!(rels(&e.files(&f.1, "wprosleep", None)), vec!["workspace/protocols/sleep.md"]);
    }

    #[test]
    fn authored_pages_outrank_raw_evidence() {
        let f = Fixture::new("rank");
        std::fs::create_dir_all(f.0.join("sources/lit")).unwrap();
        std::fs::write(f.0.join("sources/lit/caffeine_paper.md"), "caffeine everywhere\n").unwrap();
        let dirs: Vec<PathBuf> = ["wiki", "workspace", "sources"].iter().map(|d| f.0.join(d)).collect();
        let index = Index::build(&f.0, &dirs);

        let mut e = Engine::default();
        let hits = e.files(&index, "caffeine", None);
        assert_eq!(hits[0].rel, "wiki/health/caffeine.md", "{:?}", rels(&hits));
        assert!(hits.iter().any(|h| h.rel.starts_with("sources/")));
    }

    #[test]
    fn an_empty_query_lists_everything_in_path_order() {
        let f = Fixture::new("empty");
        let mut e = Engine::default();
        assert_eq!(
            rels(&e.files(&f.1, "  ", None)),
            vec!["wiki/health/caffeine.md", "wiki/health/creatine.md", "workspace/protocols/sleep.md"]
        );
    }

    #[test]
    fn results_can_be_scoped_to_one_collection() {
        let f = Fixture::new("scope");
        let mut e = Engine::default();
        let hits = e.files(&f.1, "", Some("wiki"));
        assert!(hits.iter().all(|h| h.rel.starts_with("wiki/")));
        assert_eq!(hits.len(), 2);
    }

    #[test]
    fn highlight_offsets_point_into_the_path_we_draw() {
        let f = Fixture::new("highlight");
        let mut e = Engine::default();
        let hit = &e.files(&f.1, "creatine", None)[0];
        let chars: String = hit.matched.iter().filter_map(|i| hit.rel.chars().nth(*i as usize)).collect();
        assert_eq!(chars, "creatine");
    }

    #[test]
    fn text_search_is_case_insensitive_and_reports_the_line() {
        let f = Fixture::new("text");
        let mut e = Engine::default();
        let hits = e.text(&f.1, "ADENOSINE", None, false);
        assert_eq!(rels(&hits), vec!["wiki/health/caffeine.md"]);
        assert_eq!(hits[0].line, Some(6), "0-based file line, frontmatter included");
        assert!(hits[0].snippet.contains("adenosine"));
    }

    #[test]
    fn text_search_matches_across_files_and_reports_one_hit_each() {
        let f = Fixture::new("text-multi");
        let mut e = Engine::default();
        let hits = e.text(&f.1, "caffeine", None, false);
        assert_eq!(hits.len(), 2, "one hit per file: {:?}", rels(&hits));
    }

    #[test]
    fn literal_mode_does_not_treat_the_query_as_a_regex() {
        let f = Fixture::new("literal");
        let mut e = Engine::default();
        assert!(e.text(&f.1, "a.enosine", None, false).is_empty());
        assert_eq!(e.text(&f.1, "a.enosine", None, true).len(), 1);
    }

    #[test]
    fn every_metacharacter_is_escaped_in_literal_mode() {
        assert_eq!(escape_literal("a.b*c[d]"), "a\\.b\\*c\\[d\\]");
        assert_eq!(escape_literal("plain"), "plain");
    }

    #[test]
    fn an_invalid_regex_returns_nothing_rather_than_panicking() {
        let f = Fixture::new("bad-regex");
        let mut e = Engine::default();
        assert!(e.text(&f.1, "(unclosed", None, true).is_empty());
    }

    #[test]
    fn empty_text_query_returns_nothing() {
        let f = Fixture::new("text-empty");
        let mut e = Engine::default();
        assert!(e.text(&f.1, "   ", None, false).is_empty());
    }

    #[test]
    fn semantic_results_are_parsed_from_the_engine_payload() {
        let payload = serde_json::json!({
            "query": "creatine",
            "hits": [
                {"path": "wiki/health/creatine.md", "title": "Creatine", "score": 12.5, "snippet": "Raises\nstores"},
                {"path": "./wiki/health/caffeine.md"}
            ]
        });
        let hits = parse_semantic(&payload, Path::new("/r"));
        assert_eq!(rels(&hits), vec!["wiki/health/creatine.md", "wiki/health/caffeine.md"]);
        assert_eq!(hits[0].path, PathBuf::from("/r/wiki/health/creatine.md"));
        assert_eq!(hits[0].score, 12);
        assert_eq!(hits[0].snippet, "Raises stores");
        assert_eq!(hits[1].title, "wiki/health/caffeine.md", "falls back to the path");
    }

    #[test]
    fn semantic_warnings_are_surfaced_but_blank_ones_are_not() {
        assert_eq!(
            semantic_warning(&serde_json::json!({"warning": "index is stale"})).as_deref(),
            Some("index is stale")
        );
        assert!(semantic_warning(&serde_json::json!({"warning": "  "})).is_none());
        assert!(semantic_warning(&serde_json::json!({})).is_none());
    }

    #[test]
    fn mode_cycles_and_only_semantic_is_deferred() {
        assert_eq!(Mode::Files.next(), Mode::Text);
        assert_eq!(Mode::Semantic.next(), Mode::Files);
        assert!(Mode::Files.is_live());
        assert!(!Mode::Semantic.is_live());
    }

    #[test]
    fn normalize_qmd_path_strips_scheme_and_makes_it_relative() {
        let root = Path::new("/r");
        assert_eq!(normalize_qmd_path("qmd://wiki/a.md", root), "wiki/a.md");
        assert_eq!(normalize_qmd_path("./wiki/a.md", root), "wiki/a.md");
        assert_eq!(normalize_qmd_path("/r/wiki/a.md", root), "wiki/a.md");
        assert_eq!(normalize_qmd_path("wiki/a.md", root), "wiki/a.md");
    }

    #[test]
    fn infer_collection_reads_the_top_level_directory() {
        assert_eq!(infer_collection("wiki/health/a.md"), "wiki");
        assert_eq!(infer_collection("workspace/protocols/p.md"), "protocols");
        assert_eq!(infer_collection("sources/literature/x.md"), "sources");
    }

    #[test]
    fn extract_title_reads_frontmatter_falls_back_to_none() {
        let f = Fixture::new("extract-title");
        assert_eq!(extract_title(&f.0.join("wiki/health/caffeine.md")), Some("Caffeine".to_string()));
        assert_eq!(extract_title(&f.0.join("does/not/exist.md")), None);
    }

    #[test]
    fn parse_index_health_flags_an_empty_index_and_a_stale_one() {
        assert!(parse_index_health("Vectors: 0 embedded\nPending: 12 need embedding\n")
            .unwrap()
            .contains("NO embeddings"));
        assert!(parse_index_health("Vectors: 40 embedded\nUpdated: 9d ago\n").unwrap().contains("last updated"));
        assert!(parse_index_health("Vectors: 40 embedded\nUpdated: 2h ago\n").is_none());
    }

    #[test]
    fn hits_from_qmd_json_reshapes_a_bare_array_and_a_wrapped_object() {
        let root = Path::new("/r");
        let array = serde_json::json!([{"path": "wiki/a.md", "title": "A", "score": 1.5, "snippet": "hi"}]);
        let hits = hits_from_qmd_json(&array, root, "wiki", 20);
        assert_eq!(hits[0]["path"], serde_json::json!("wiki/a.md"));
        assert_eq!(hits[0]["collection"], serde_json::json!("wiki"));

        let wrapped = serde_json::json!({"hits": [{"path": "./sources/x.md"}]});
        let hits = hits_from_qmd_json(&wrapped, root, "all", 20);
        assert_eq!(hits[0]["collection"], serde_json::json!("sources"), "inferred when collection is 'all'");
    }
}
