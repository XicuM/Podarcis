//! The in-app editor.
//!
//! edtui supplies vim modes and motions; everything here is what makes it an
//! *OKF* editor rather than a generic text box — citation and link completion
//! drawn from the page's own frontmatter and the real tree, and a gutter fed by
//! the live findings.

use std::path::{Path, PathBuf};

use edtui::{EditorEventHandler, EditorMode, EditorState, Index2, Lines, RowIndex};

use crate::vault::index::Index;
use crate::vault::page::Page;

/// What the cursor is sitting inside, and so what completing means.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Completing {
    /// Inside `[^…`, completing a footnote against `sources[].id`.
    Footnote { prefix: String, start: usize },
    /// Inside `](…`, completing a relative path against the tree.
    Link { prefix: String, start: usize },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate {
    pub value: String,
    /// Shown dimmed after the value: an author, a page title, "cited".
    pub note: String,
}

#[derive(Debug)]
pub struct Completion {
    pub what: Completing,
    pub candidates: Vec<Candidate>,
    pub selected: usize,
}

impl Completion {
    pub fn current(&self) -> Option<&Candidate> {
        self.candidates.get(self.selected)
    }

    pub fn move_by(&mut self, delta: isize) {
        if self.candidates.is_empty() {
            return;
        }
        let n = self.candidates.len() as isize;
        self.selected = (((self.selected as isize + delta) % n) + n) as usize % self.candidates.len();
    }
}

pub struct Editor {
    pub state: EditorState,
    pub events: EditorEventHandler,
    pub path: PathBuf,
    pub completion: Option<Completion>,
    saved: String,
}

impl Editor {
    pub fn open(page: &Page) -> Self {
        let text = std::fs::read_to_string(&page.path).unwrap_or_default();
        let mut state = EditorState::new(Lines::from(text.as_str()));
        state.mode = EditorMode::Normal;
        Self {
            state,
            events: EditorEventHandler::default(),
            path: page.path.clone(),
            completion: None,
            saved: text,
        }
    }

    pub fn text(&self) -> String {
        self.state.lines.to_string()
    }

    pub fn dirty(&self) -> bool {
        self.text() != self.saved
    }

    pub fn mode(&self) -> EditorMode {
        self.state.mode
    }

    /// Write to disk. Trailing whitespace is left alone — this is prose, and a
    /// silent rewrite of someone's text is not a feature.
    pub fn save(&mut self) -> std::io::Result<()> {
        let mut text = self.text();
        if !text.ends_with('\n') {
            text.push('\n');
        }
        std::fs::write(&self.path, &text)?;
        self.saved = self.text();
        Ok(())
    }

    /// Place the cursor on a file line, as when entering the editor from the
    /// reader or jumping to a finding.
    pub fn goto_line(&mut self, line: usize) {
        let row = line.min(self.state.lines.len().saturating_sub(1));
        self.state.cursor = Index2::new(row, 0);
    }

    pub fn cursor_line(&self) -> usize {
        self.state.cursor.row
    }

    fn line_text(&self, row: usize) -> String {
        self.state
            .lines
            .get(RowIndex::new(row))
            .map(|chars| chars.iter().collect())
            .unwrap_or_default()
    }

    /// Recompute the completion popup from the text before the cursor.
    pub fn refresh_completion(&mut self, page: &Page, index: &Index, root: &Path) {
        if self.state.mode != EditorMode::Insert {
            self.completion = None;
            return;
        }
        let line = self.line_text(self.state.cursor.row);
        let Some(what) = detect(&line, self.state.cursor.col) else {
            self.completion = None;
            return;
        };
        let candidates = match &what {
            Completing::Footnote { prefix, .. } => footnote_candidates(page, prefix),
            Completing::Link { prefix, .. } => link_candidates(index, root, &self.path, prefix),
        };
        if candidates.is_empty() {
            self.completion = None;
            return;
        }
        // Keep the highlighted entry if it survived the new prefix.
        let selected = self
            .completion
            .as_ref()
            .and_then(|c| c.current())
            .and_then(|current| candidates.iter().position(|c| c.value == current.value))
            .unwrap_or(0);
        self.completion = Some(Completion { what, candidates, selected });
    }

    /// Accept the highlighted candidate, replacing the typed prefix.
    pub fn accept_completion(&mut self) -> bool {
        let Some(completion) = self.completion.take() else { return false };
        let Some(candidate) = completion.current().cloned() else { return false };
        let (start, closing) = match &completion.what {
            Completing::Footnote { start, .. } => (*start, ']'),
            Completing::Link { start, .. } => (*start, ')'),
        };

        let row = self.state.cursor.row;
        let col = self.state.cursor.col;
        let Some(line) = self.state.lines.get_mut(RowIndex::new(row)) else { return false };

        let already_closed = line.get(col) == Some(&closing);
        let mut replacement: Vec<char> = candidate.value.chars().collect();
        if !already_closed {
            replacement.push(closing);
        }
        let after = line.split_off(col);
        line.truncate(start);
        let new_col = start + replacement.len();
        line.extend(replacement);
        line.extend(after);
        self.state.cursor = Index2::new(row, if already_closed { new_col + 1 } else { new_col });
        true
    }

    pub fn dismiss_completion(&mut self) {
        self.completion = None;
    }
}

/// Find an open `[^` or `](` before the cursor with nothing closing it.
fn detect(line: &str, col: usize) -> Option<Completing> {
    let chars: Vec<char> = line.chars().collect();
    let col = col.min(chars.len());
    let before: String = chars[..col].iter().collect();

    let footnote = before.rfind("[^").map(|i| (i + 2, true));
    let link = before.rfind("](").map(|i| (i + 2, false));
    let (start, is_footnote) = match (footnote, link) {
        (Some(f), Some(l)) => {
            if f.0 > l.0 {
                f
            } else {
                l
            }
        }
        (Some(f), None) => f,
        (None, Some(l)) => l,
        (None, None) => return None,
    };

    let prefix: String = chars[start..col].iter().collect();
    // A closing delimiter or whitespace inside the prefix means we already left
    // the construct.
    let closer = if is_footnote { ']' } else { ')' };
    if prefix.contains(closer) || prefix.contains('[') || prefix.contains(char::is_whitespace) {
        return None;
    }
    Some(if is_footnote {
        Completing::Footnote { prefix, start }
    } else {
        Completing::Link { prefix, start }
    })
}

/// A footnote is satisfied by a `sources[].id` **or** by an in-body `[^id]:`
/// definition — the linter accepts both, and most of the real wiki cites the
/// second way — so completion offers both.
fn footnote_candidates(page: &Page, prefix: &str) -> Vec<Candidate> {
    let cited = page.footnote_refs();
    let needle = prefix.to_lowercase();
    let mut out: Vec<Candidate> = Vec::new();

    for source in &page.okf.sources {
        if !source.id.to_lowercase().starts_with(&needle) {
            continue;
        }
        let mut note = String::new();
        if let Some(author) = &source.author {
            note.push_str(author);
        }
        if let Some(year) = &source.year {
            if !note.is_empty() {
                note.push_str(", ");
            }
            note.push_str(year);
        }
        if note.is_empty() {
            note = source.title.clone().unwrap_or_default();
        }
        out.push(Candidate { value: source.id.clone(), note });
    }

    for (label, text) in page.footnote_definitions() {
        if !label.to_lowercase().starts_with(&needle) || out.iter().any(|c| c.value == label) {
            continue;
        }
        out.push(Candidate { value: label, note: text });
    }

    for candidate in out.iter_mut() {
        if cited.contains(&candidate.value) {
            candidate.note = if candidate.note.is_empty() {
                "already cited".into()
            } else {
                format!("{} · cited", candidate.note)
            };
        }
    }
    out
}

/// Relative paths to other pages, ranked so near neighbours come first. A link
/// is only valid if it resolves, so the candidates are built from the index
/// rather than from free text.
fn link_candidates(index: &Index, root: &Path, from: &Path, prefix: &str) -> Vec<Candidate> {
    let from_dir = from.parent().unwrap_or(root);
    let needle = prefix.to_lowercase();
    let mut out: Vec<(usize, Candidate)> = index
        .entries
        .iter()
        .filter(|entry| entry.path != from)
        .filter_map(|entry| {
            let rel = relative_from(from_dir, &entry.path)?;
            let matches = rel.to_lowercase().contains(&needle)
                || entry.title.to_lowercase().contains(&needle);
            if !matches {
                return None;
            }
            let depth = rel.matches("../").count();
            Some((depth, Candidate { value: rel, note: entry.title.clone() }))
        })
        .collect();
    out.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.value.len().cmp(&b.1.value.len())));
    out.truncate(50);
    out.into_iter().map(|(_, c)| c).collect()
}

/// Build a `../`-style path from one directory to a file.
pub fn relative_from(from_dir: &Path, target: &Path) -> Option<String> {
    let from: Vec<_> = from_dir.components().collect();
    let to: Vec<_> = target.components().collect();
    let shared = from.iter().zip(&to).take_while(|(a, b)| a == b).count();
    let ups = from.len() - shared;
    let mut out = String::new();
    for _ in 0..ups {
        out.push_str("../");
    }
    for (i, component) in to[shared..].iter().enumerate() {
        if i > 0 {
            out.push('/');
        }
        out.push_str(&component.as_os_str().to_string_lossy());
    }
    (!out.is_empty()).then_some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set_line(editor: &mut Editor, row: usize, text: &str) {
        *editor.state.lines.get_mut(RowIndex::new(row)).unwrap() = text.chars().collect();
    }

    fn page(raw: &str) -> Page {
        Page::parse(raw, Path::new("/r/wiki/a.md"), Path::new("/r"))
    }

    const SOURCED: &str = "---\ntitle: A\ntype: concept\ncategory: c\nrationale: r\nsources:\n  - id: lin_2023\n    author: Lin et al.\n    year: 2023\n  - id: lohse_2011\n    title: Attentional focus\n  - id: other\n---\nBody[^lin_2023]\n";

    #[test]
    fn detects_a_footnote_in_progress() {
        assert_eq!(
            detect("Text[^lin", 9),
            Some(Completing::Footnote { prefix: "lin".into(), start: 6 })
        );
        assert_eq!(detect("Text[^", 6), Some(Completing::Footnote { prefix: String::new(), start: 6 }));
    }

    #[test]
    fn detects_a_link_target_in_progress() {
        assert_eq!(
            detect("See [A](../nut", 14),
            Some(Completing::Link { prefix: "../nut".into(), start: 8 })
        );
    }

    #[test]
    fn a_closed_construct_is_not_completing() {
        assert_eq!(detect("Text[^lin_2023] more", 20), None);
        assert_eq!(detect("See [A](../a.md) more", 21), None);
    }

    #[test]
    fn whitespace_ends_the_completion() {
        assert_eq!(detect("Text[^lin 2023", 14), None);
    }

    #[test]
    fn the_nearest_construct_wins() {
        // A link opened after a footnote was closed.
        assert_eq!(
            detect("[^a] then [B](../b", 18),
            Some(Completing::Link { prefix: "../b".into(), start: 14 })
        );
    }

    #[test]
    fn footnote_candidates_come_from_the_pages_own_sources() {
        let p = page(SOURCED);
        let all = footnote_candidates(&p, "");
        assert_eq!(
            all.iter().map(|c| c.value.as_str()).collect::<Vec<_>>(),
            vec!["lin_2023", "lohse_2011", "other"]
        );
        let filtered = footnote_candidates(&p, "lo");
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].value, "lohse_2011");
    }

    #[test]
    fn candidates_say_which_sources_are_already_cited() {
        let p = page(SOURCED);
        let all = footnote_candidates(&p, "");
        assert!(all[0].note.contains("cited"), "{:?}", all[0]);
        assert!(!all[1].note.contains("cited"));
        assert_eq!(all[0].note, "Lin et al., 2023 · cited");
        assert_eq!(all[1].note, "Attentional focus", "falls back to the title");
    }

    #[test]
    fn in_body_definitions_are_offered_too() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\n---\nClaim[^burke_2023].\n\n[^burke_2023]: [Creatine and hypertrophy](../s/raw.md)\n[^other_2020]: Plain text source\n";
        let p = page(raw);
        let all = footnote_candidates(&p, "");
        assert_eq!(all.iter().map(|c| c.value.as_str()).collect::<Vec<_>>(), vec!["burke_2023", "other_2020"]);
        assert_eq!(all[0].note, "Creatine and hypertrophy · cited");
        assert_eq!(all[1].note, "Plain text source");
    }

    #[test]
    fn a_source_id_is_not_duplicated_by_its_definition() {
        let raw = "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\nsources:\n  - id: dup\n    author: A\n---\nx\n\n[^dup]: also here\n";
        let all = footnote_candidates(&page(raw), "");
        assert_eq!(all.len(), 1);
        assert_eq!(all[0].note, "A");
    }

    #[test]
    fn prefix_matching_is_case_insensitive() {
        assert_eq!(footnote_candidates(&page(SOURCED), "LIN").len(), 1);
    }

    #[test]
    fn relative_paths_go_up_and_down_correctly() {
        assert_eq!(
            relative_from(Path::new("/r/wiki/health"), Path::new("/r/wiki/health/x.md")).unwrap(),
            "x.md"
        );
        assert_eq!(
            relative_from(Path::new("/r/wiki/health/sleep"), Path::new("/r/wiki/food/x.md")).unwrap(),
            "../../food/x.md"
        );
        assert_eq!(
            relative_from(Path::new("/r/workspace"), Path::new("/r/wiki/x.md")).unwrap(),
            "../wiki/x.md"
        );
    }

    struct Fixture(PathBuf);

    impl Fixture {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("podarcis-editor-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            for (rel, body) in [
                ("wiki/a.md", SOURCED),
                ("wiki/health/caffeine.md", "---\ntitle: Caffeine\ntype: concept\ncategory: c\nrationale: r\n---\nx\n"),
            ] {
                let path = dir.join(rel);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(path, body).unwrap();
            }
            Self(dir)
        }
        fn index(&self) -> Index {
            Index::build(&self.0, &[self.0.join("wiki")])
        }
        fn page(&self) -> Page {
            Page::load(&self.0.join("wiki/a.md"), &self.0).unwrap()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn link_candidates_are_real_relative_paths_to_real_pages() {
        let f = Fixture::new("links");
        let index = f.index();
        let from = f.0.join("wiki/a.md");
        let hits = link_candidates(&index, &f.0, &from, "caff");
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].value, "health/caffeine.md");
        assert_eq!(hits[0].note, "Caffeine");
        // The page never offers a link to itself.
        assert!(link_candidates(&index, &f.0, &from, "a.md").iter().all(|c| c.value != "a.md"));
    }

    #[test]
    fn accepting_a_footnote_inserts_the_id_and_closes_the_bracket() {
        let f = Fixture::new("accept-fn");
        let page = f.page();
        let mut editor = Editor::open(&page);
        editor.state.mode = EditorMode::Insert;
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "Cite[^lo");
        editor.state.cursor = Index2::new(row, 8);

        editor.refresh_completion(&page, &f.index(), &f.0);
        assert!(editor.completion.is_some());
        assert!(editor.accept_completion());
        assert_eq!(editor.line_text(row), "Cite[^lohse_2011]");
        assert_eq!(editor.state.cursor.col, 17);
        assert!(editor.completion.is_none());
    }

    #[test]
    fn accepting_does_not_double_the_closing_bracket() {
        let f = Fixture::new("accept-closed");
        let page = f.page();
        let mut editor = Editor::open(&page);
        editor.state.mode = EditorMode::Insert;
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "Cite[^lo] tail");
        editor.state.cursor = Index2::new(row, 8);

        editor.refresh_completion(&page, &f.index(), &f.0);
        assert!(editor.accept_completion());
        assert_eq!(editor.line_text(row), "Cite[^lohse_2011] tail");
    }

    #[test]
    fn completion_is_only_offered_in_insert_mode() {
        let f = Fixture::new("mode");
        let page = f.page();
        let mut editor = Editor::open(&page);
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "Cite[^lo");
        editor.state.cursor = Index2::new(row, 8);

        editor.state.mode = EditorMode::Normal;
        editor.refresh_completion(&page, &f.index(), &f.0);
        assert!(editor.completion.is_none());

        editor.state.mode = EditorMode::Insert;
        editor.refresh_completion(&page, &f.index(), &f.0);
        assert!(editor.completion.is_some());
    }

    #[test]
    fn no_candidates_means_no_popup() {
        let f = Fixture::new("nomatch");
        let page = f.page();
        let mut editor = Editor::open(&page);
        editor.state.mode = EditorMode::Insert;
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "Cite[^zzzz");
        editor.state.cursor = Index2::new(row, 10);
        editor.refresh_completion(&page, &f.index(), &f.0);
        assert!(editor.completion.is_none());
    }

    #[test]
    fn dirty_tracks_the_file_and_save_clears_it() {
        let f = Fixture::new("dirty");
        let page = f.page();
        let mut editor = Editor::open(&page);
        assert!(!editor.dirty());

        editor.state.lines.push("appended".chars().collect::<Vec<char>>());
        assert!(editor.dirty());

        editor.save().unwrap();
        assert!(!editor.dirty());
        assert!(std::fs::read_to_string(&page.path).unwrap().ends_with("appended\n"));
    }

    #[test]
    fn completion_selection_wraps_in_both_directions() {
        let mut completion = Completion {
            what: Completing::Footnote { prefix: String::new(), start: 0 },
            candidates: vec![
                Candidate { value: "a".into(), note: String::new() },
                Candidate { value: "b".into(), note: String::new() },
            ],
            selected: 0,
        };
        completion.move_by(-1);
        assert_eq!(completion.selected, 1);
        completion.move_by(1);
        assert_eq!(completion.selected, 0);
    }

    #[test]
    fn goto_line_is_clamped_to_the_document() {
        let f = Fixture::new("goto");
        let mut editor = Editor::open(&f.page());
        editor.goto_line(9999);
        assert_eq!(editor.cursor_line(), editor.state.lines.len() - 1);
    }
}
