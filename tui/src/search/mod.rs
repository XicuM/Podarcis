//! Search, in three flavours, all reachable from one overlay.
//!
//! * **Files** — fuzzy over titles and paths. Runs on every keystroke.
//! * **Text** — literal or regex over file contents. Runs on every keystroke.
//! * **Semantic** — `podarcis wiki search --json`, which is qmd. Explicit only.
//!
//! The split exists because the third one is slow: `qmd query` measures around
//! thirty seconds on a real checkout. Putting it on the typing path would make
//! the whole app feel broken, so it is a deliberate action with a spinner.

use std::path::{Path, PathBuf};

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

/// Parse the JSON `podarcis wiki search --json` prints.
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
}
