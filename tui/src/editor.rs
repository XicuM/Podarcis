//! The in-app editor.
//!
//! edtui supplies vim modes and motions; everything here is what makes it an
//! *OKF* editor rather than a generic text box — citation and link completion
//! drawn from the page's own frontmatter and the real tree, and a gutter fed by
//! the live findings.

use std::path::{Path, PathBuf};

use edtui::{EditorEventHandler, EditorMode, EditorState, Highlight, Index2, Lines, RowIndex};
use ratatui::style::Style;

use crate::theme::Theme;
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
    /// The content width the pane last rendered at (pane width minus the
    /// line-number gutter) — kept in sync by `panes::editor` so wrapped-line
    /// cursor movement can mirror `EditorView`'s own `wrap(true)` splitting.
    /// Zero until the first render, which `wrap_starts` treats as "no wrap".
    pub render_width: usize,
    /// `(theme signature, text)` the syntax marks in `state.highlights` were
    /// tokenized from, so `refresh_syntax` can skip the work while neither
    /// changed — the tokenizer runs only when the buffer actually moved.
    syntax: Option<(Style, String)>,
    /// The file's modification time when this buffer was filled, so `save` can
    /// tell "nobody touched it" from "someone did".
    ///
    /// The watcher notices an outside edit, but a save that was already under
    /// way when the event arrived would still land on top of it. `None` when
    /// the file had no mtime to read (a page being created), which nothing can
    /// conflict with.
    opened_at: Option<std::time::SystemTime>,
}

/// What a [`Editor::save`] did.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Save {
    /// The buffer is on disk.
    Written,
    /// Nothing was written: the file moved since it was opened.
    ChangedUnderneath,
}

/// A file's modification time, or `None` if it has none to read.
fn mtime(path: &Path) -> Option<std::time::SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

impl Editor {
    pub fn open(page: &Page) -> Self {
        let text = std::fs::read_to_string(&page.path).unwrap_or_default();
        let opened_at = mtime(&page.path);
        let mut state = EditorState::new(Lines::from(text.as_str()));
        // Modeless. There is no normal mode to escape to and no `i` to
        // remember: you open a page and type, with readline motions
        // (ctrl+a/e/f/b, alt+f/b) that every shell already taught you.
        state.mode = EditorMode::Insert;
        Self {
            state,
            events: EditorEventHandler::emacs_mode(),
            path: page.path.clone(),
            completion: None,
            saved: text,
            render_width: 0,
            syntax: None,
            opened_at,
        }
    }

    /// edtui can still switch itself out of insert mode (a search, say); the
    /// editor is modeless, so put it back.
    pub fn keep_modeless(&mut self) {
        if self.state.mode == EditorMode::Normal {
            self.state.mode = EditorMode::Insert;
        }
    }

    pub fn text(&self) -> String {
        self.state.lines.to_string()
    }

    pub fn dirty(&self) -> bool {
        self.text() != self.saved
    }

    /// Re-tokenize the buffer into markdown syntax marks, layering each
    /// styled run as an edtui `Highlight` over the base text. Cheap to call
    /// every frame: it returns immediately while neither the text nor the
    /// theme moved, and the marks are recomputed only when one of them did.
    pub fn refresh_syntax(&mut self, theme: &Theme) {
        let base = Style::default().fg(theme.text).bg(theme.bg);
        let text = self.text();
        if self.syntax.as_ref().is_some_and(|(b, t)| *b == base && t == &text) {
            return;
        }
        let mut marks = Vec::new();
        for (row, (_, runs)) in self
            .state
            .lines
            .iter()
            .zip(crate::ui::highlight::markdown_runs(&text, theme, &base))
            .enumerate()
        {
            for r in runs {
                marks.push(Highlight::new(Index2::new(row, r.start), Index2::new(row, r.end.saturating_sub(1)), r.style));
            }
        }
        self.state.set_highlights(marks);
        self.syntax = Some((base, text));
    }

    pub fn cursor_col(&self) -> usize {
        self.state.cursor.col
    }

    /// Write to disk. Trailing whitespace is left alone — this is prose, and a
    /// silent rewrite of someone's text is not a feature.
    ///
    /// Refuses to write when the file's mtime moved since it was opened. The
    /// pages here are edited by more than one writer — Obsidian on the same
    /// folder, `wiki_publish` from an agent, a `git pull` — and the loser of
    /// that race would otherwise be whoever saved last, silently.
    pub fn save(&mut self) -> std::io::Result<Save> {
        if self.changed_underneath() {
            return Ok(Save::ChangedUnderneath);
        }
        self.write()?;
        Ok(Save::Written)
    }

    fn write(&mut self) -> std::io::Result<()> {
        let mut text = self.text();
        if !text.ends_with('\n') {
            text.push('\n');
        }
        std::fs::write(&self.path, &text)?;
        self.saved = self.text();
        self.opened_at = mtime(&self.path);
        Ok(())
    }

    /// Has the file moved since this buffer was filled from it?
    ///
    /// A file that has since been deleted counts as unchanged: the buffer is
    /// then the only copy left, and refusing to write it would be the one
    /// outcome that loses the text for good.
    fn changed_underneath(&self) -> bool {
        match (self.opened_at, mtime(&self.path)) {
            (Some(opened), Some(current)) => current != opened,
            _ => false,
        }
    }

    /// Write the buffer even though the file changed, keeping the outside
    /// version beside it as `<name>.conflict-<n>.md` rather than destroying
    /// it. Returns where the other copy was parked.
    pub fn save_overwriting(&mut self) -> std::io::Result<PathBuf> {
        let backup = self.conflict_path();
        std::fs::copy(&self.path, &backup)?;
        self.write()?;
        Ok(backup)
    }

    /// First free `<stem>.conflict-<n>.<ext>` beside the file.
    fn conflict_path(&self) -> PathBuf {
        let stem = self.path.file_stem().and_then(|s| s.to_str()).unwrap_or("page");
        let ext = self.path.extension().and_then(|s| s.to_str()).unwrap_or("md");
        let dir = self.path.parent().unwrap_or(Path::new("."));
        (1..)
            .map(|n| dir.join(format!("{stem}.conflict-{n}.{ext}")))
            .find(|candidate| !candidate.exists())
            // `(1..)` is unbounded, so `find` only ends by succeeding.
            .unwrap_or_else(|| dir.join(format!("{stem}.conflict.{ext}")))
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

    fn set_line(&mut self, row: usize, text: &str) {
        if let Some(line) = self.state.lines.get_mut(RowIndex::new(row)) {
            *line = text.chars().collect();
        }
    }

    /// Insert text at the cursor, leaving the cursor after it.
    fn insert_at_cursor(&mut self, text: &str) {
        let (row, col) = (self.state.cursor.row, self.state.cursor.col);
        let line = self.line_text(row);
        let chars: Vec<char> = line.chars().collect();
        let col = col.min(chars.len());
        let mut next: String = chars[..col].iter().collect();
        next.push_str(text);
        next.extend(chars[col..].iter());
        self.set_line(row, &next);
        self.state.cursor = Index2::new(row, col + text.chars().count());
    }

    /// Wrap the word under the cursor in `marker`, or insert an empty pair and
    /// place the cursor between the halves.
    pub fn wrap_emphasis(&mut self, marker: &str) {
        let (row, col) = (self.state.cursor.row, self.state.cursor.col);
        let chars: Vec<char> = self.line_text(row).chars().collect();
        let col = col.min(chars.len());

        let is_word = |c: char| !c.is_whitespace();
        let start = chars[..col].iter().rposition(|c| !is_word(*c)).map(|i| i + 1).unwrap_or(0);
        let end = col + chars[col..].iter().position(|c| !is_word(*c)).unwrap_or(chars.len() - col);

        if start == end {
            self.insert_at_cursor(&format!("{marker}{marker}"));
            self.state.cursor = Index2::new(row, col + marker.chars().count());
            return;
        }
        let word: String = chars[start..end].iter().collect();
        // Toggling off is what a second press should do.
        let (replacement, shift) = match word.strip_prefix(marker).and_then(|w| w.strip_suffix(marker)) {
            Some(inner) if !inner.is_empty() => (inner.to_string(), -(marker.chars().count() as isize)),
            _ => (format!("{marker}{word}{marker}"), marker.chars().count() as isize),
        };
        let mut next: String = chars[..start].iter().collect();
        next.push_str(&replacement);
        next.extend(chars[end..].iter());
        self.set_line(row, &next);
        self.state.cursor = Index2::new(row, (col as isize + shift).max(0) as usize);
    }

    /// Insert a link skeleton and park the cursor in the target, where the
    /// path completion will pick it up.
    pub fn insert_link(&mut self) {
        let (row, col) = (self.state.cursor.row, self.state.cursor.col);
        let chars: Vec<char> = self.line_text(row).chars().collect();
        let col = col.min(chars.len());
        let start = chars[..col].iter().rposition(|c| c.is_whitespace()).map(|i| i + 1).unwrap_or(0);
        let word: String = chars[start..col].iter().collect();

        let mut next: String = chars[..start].iter().collect();
        next.push_str(&format!("[{word}]("));
        let cursor = next.chars().count();
        next.push(')');
        next.extend(chars[col..].iter());
        self.set_line(row, &next);
        self.state.cursor = Index2::new(row, cursor);
    }

    /// Continue a list or quote on Enter. Returns false when the line is
    /// ordinary prose and the editor should handle the key itself.
    pub fn continue_block(&mut self) -> bool {
        let row = self.state.cursor.row;
        let line = self.line_text(row);
        let Some(prefix) = block_prefix(&line) else { return false };

        // An empty item means "stop the list", so clear it instead.
        if line.trim() == prefix.trim() {
            self.set_line(row, "");
            self.state.cursor = Index2::new(row, 0);
            return true;
        }
        let chars: Vec<char> = line.chars().collect();
        let col = self.state.cursor.col.min(chars.len());
        let head: String = chars[..col].iter().collect();
        let tail: String = chars[col..].iter().collect();
        self.set_line(row, &head);
        let next = format!("{prefix}{tail}");
        let cursor = prefix.chars().count();
        self.state.lines.insert(RowIndex::new(row + 1), next.chars().collect::<Vec<char>>());
        self.state.cursor = Index2::new(row + 1, cursor);
        true
    }

    /// Indent or outdent the current line by two spaces — list nesting.
    pub fn indent(&mut self, out: bool) {
        let row = self.state.cursor.row;
        let line = self.line_text(row);
        let col = self.state.cursor.col;
        if out {
            let trimmed = line.strip_prefix("  ").unwrap_or(&line).to_string();
            let removed = line.chars().count() - trimmed.chars().count();
            self.set_line(row, &trimmed);
            self.state.cursor = Index2::new(row, col.saturating_sub(removed));
        } else {
            self.set_line(row, &format!("  {line}"));
            self.state.cursor = Index2::new(row, col + 2);
        }
    }

    /// Arrow-down/up: land on the visual row directly below/above the
    /// cursor, not the next *logical* line — edtui's own `MoveDown`/`MoveUp`
    /// only know about logical rows, so a wrapped line makes plain arrows
    /// skip whatever is still on screen. The two coincide once nothing has
    /// wrapped, which is why this degrades to the old behavior when
    /// `render_width` is still zero (nothing rendered yet).
    pub fn move_visual(&mut self, down: bool) {
        let width = self.render_width;
        let row = self.state.cursor.row;
        let col = self.state.cursor.col;
        let line: Vec<char> = self.state.lines.get(RowIndex::new(row)).cloned().unwrap_or_default();
        let starts = wrap_starts(&line, width);
        let chunk = starts.iter().rposition(|&s| s <= col).unwrap_or(0);
        let rel = col - starts[chunk];

        if down {
            if chunk + 1 < starts.len() {
                let (start, max_rel) = chunk_bounds(&starts, line.len(), chunk + 1);
                self.state.cursor.col = start + rel.min(max_rel);
            } else if row + 1 < self.state.lines.len() {
                let next_line: Vec<char> = self.state.lines.get(RowIndex::new(row + 1)).cloned().unwrap_or_default();
                let next_starts = wrap_starts(&next_line, width);
                let (start, max_rel) = chunk_bounds(&next_starts, next_line.len(), 0);
                self.state.cursor = Index2::new(row + 1, start + rel.min(max_rel));
            }
        } else if chunk > 0 {
            let (start, max_rel) = chunk_bounds(&starts, line.len(), chunk - 1);
            self.state.cursor.col = start + rel.min(max_rel);
        } else if row > 0 {
            let prev_line: Vec<char> = self.state.lines.get(RowIndex::new(row - 1)).cloned().unwrap_or_default();
            let prev_starts = wrap_starts(&prev_line, width);
            let (start, max_rel) = chunk_bounds(&prev_starts, prev_line.len(), prev_starts.len() - 1);
            self.state.cursor = Index2::new(row - 1, start + rel.min(max_rel));
        }
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

/// edtui's `ViewState` default (`panes::editor` never overrides it).
const TAB_WIDTH: usize = 2;

fn char_display_width(ch: char) -> usize {
    if ch == '\t' {
        return TAB_WIDTH;
    }
    unicode_width::UnicodeWidthChar::width(ch).unwrap_or(0)
}

/// The char index each wrapped visual segment of `line` starts at, mirroring
/// edtui's own char-width-based (not word-based) wrapping exactly — see
/// `LineWrapper::wrap_line` in the edtui crate. `width == 0` (nothing
/// rendered yet, or wrap disabled) is one segment covering the whole line.
fn wrap_starts(line: &[char], width: usize) -> Vec<usize> {
    if width == 0 {
        return vec![0];
    }
    let mut starts = vec![0];
    let mut used = 0;
    for (i, &ch) in line.iter().enumerate() {
        let w = char_display_width(ch);
        if used + w > width {
            starts.push(i);
            used = 0;
        }
        used += w;
    }
    starts
}

/// The `(start, max_rel)` a column may take within segment `idx` of `starts`
/// and stay in that visual row: `max_rel` is exclusive of the next segment's
/// start, except for the line's last segment, where one-past-the-end is a
/// legitimate cursor position (the editor is always in insert mode).
fn chunk_bounds(starts: &[usize], line_len: usize, idx: usize) -> (usize, usize) {
    let start = starts[idx];
    let end = starts.get(idx + 1).copied().unwrap_or(line_len);
    let is_last = idx + 1 >= starts.len();
    let max_rel = if is_last { end - start } else { (end - start).saturating_sub(1) };
    (start, max_rel)
}

/// The list or quote marker a new line should repeat: `- `, `1. `, `> `,
/// `- [ ] `, preserving indentation. `None` for ordinary prose.
fn block_prefix(line: &str) -> Option<String> {
    let indent: String = line.chars().take_while(|c| *c == ' ' || *c == '\t').collect();
    let rest = &line[indent.len()..];

    for marker in ["- [ ] ", "- [x] ", "* ", "- ", "+ ", "> "] {
        if rest.starts_with(marker) {
            // A finished task continues as an unfinished one.
            let marker = if marker == "- [x] " { "- [ ] " } else { marker };
            return Some(format!("{indent}{marker}"));
        }
    }
    // `12. ` continues as `13. `.
    let digits: String = rest.chars().take_while(char::is_ascii_digit).collect();
    if !digits.is_empty() {
        if let Some(after) = rest[digits.len()..].strip_prefix(". ") {
            let _ = after;
            let n: usize = digits.parse().ok()?;
            return Some(format!("{indent}{}. ", n + 1));
        }
    }
    None
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
    fn the_editor_is_modeless() {
        let f = Fixture::new("modeless");
        let mut editor = Editor::open(&f.page());
        assert_eq!(editor.state.mode, EditorMode::Insert);
        editor.state.mode = EditorMode::Normal;
        editor.keep_modeless();
        assert_eq!(editor.state.mode, EditorMode::Insert, "there is no mode to fall back to");
    }

    #[test]
    fn bold_wraps_the_word_under_the_cursor_and_toggles_off() {
        let f = Fixture::new("bold");
        let mut editor = Editor::open(&f.page());
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "make this bold");
        editor.state.cursor = Index2::new(row, 12);

        editor.wrap_emphasis("**");
        assert_eq!(editor.line_text(row), "make this **bold**");
        editor.wrap_emphasis("**");
        assert_eq!(editor.line_text(row), "make this bold");
    }

    #[test]
    fn emphasis_on_whitespace_opens_an_empty_pair_around_the_cursor() {
        let f = Fixture::new("bold-empty");
        let mut editor = Editor::open(&f.page());
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "word ");
        editor.state.cursor = Index2::new(row, 5);

        editor.wrap_emphasis("_");
        assert_eq!(editor.line_text(row), "word __");
        assert_eq!(editor.cursor_col(), 6, "the cursor sits between the markers");
    }

    #[test]
    fn a_link_wraps_the_preceding_word_and_parks_in_the_target() {
        let f = Fixture::new("link");
        let mut editor = Editor::open(&f.page());
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "see Caffeine");
        editor.state.cursor = Index2::new(row, 12);

        editor.insert_link();
        assert_eq!(editor.line_text(row), "see [Caffeine]()");
        assert_eq!(editor.cursor_col(), 15, "inside the parentheses");
    }

    #[test]
    fn enter_continues_bullets_numbers_tasks_and_quotes() {
        let cases = [
            ("- item", "- "),
            ("  * nested", "  * "),
            ("3. third", "4. "),
            ("- [x] done", "- [ ] "),
            ("> quoted", "> "),
        ];
        for (line, want) in cases {
            assert_eq!(block_prefix(line).as_deref(), Some(want), "{line:?}");
        }
        assert_eq!(block_prefix("ordinary prose"), None);
        // CommonMark really does read `2024. text` as an ordered list item, and
        // continuing it is what every markdown editor does.
        assert_eq!(block_prefix("2024. a list after all").as_deref(), Some("2025. "));
        assert_eq!(block_prefix("-not a bullet without a space"), None);
    }

    #[test]
    fn continuing_a_list_splits_the_line_at_the_cursor() {
        let f = Fixture::new("continue");
        let mut editor = Editor::open(&f.page());
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "- alpha beta");
        editor.state.cursor = Index2::new(row, 8);

        assert!(editor.continue_block());
        assert_eq!(editor.line_text(row), "- alpha ");
        assert_eq!(editor.line_text(row + 1), "- beta");
        assert_eq!(editor.cursor_line(), row + 1);
        assert_eq!(editor.cursor_col(), 2);
    }

    #[test]
    fn an_empty_item_ends_the_list_instead_of_repeating_it() {
        let f = Fixture::new("end-list");
        let mut editor = Editor::open(&f.page());
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "- ");
        editor.state.cursor = Index2::new(row, 2);

        assert!(editor.continue_block());
        assert_eq!(editor.line_text(row), "");
        assert_eq!(editor.cursor_col(), 0);
    }

    #[test]
    fn prose_is_left_to_the_editor() {
        let f = Fixture::new("prose");
        let mut editor = Editor::open(&f.page());
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "just a sentence");
        editor.state.cursor = Index2::new(row, 4);
        assert!(!editor.continue_block());
    }

    #[test]
    fn indenting_moves_the_cursor_with_the_text() {
        let f = Fixture::new("indent");
        let mut editor = Editor::open(&f.page());
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "- item");
        editor.state.cursor = Index2::new(row, 3);

        editor.indent(false);
        assert_eq!(editor.line_text(row), "  - item");
        assert_eq!(editor.cursor_col(), 5);

        editor.indent(true);
        assert_eq!(editor.line_text(row), "- item");
        assert_eq!(editor.cursor_col(), 3);

        editor.indent(true);
        assert_eq!(editor.line_text(row), "- item", "outdenting past zero is a no-op");
    }

    #[test]
    fn goto_line_is_clamped_to_the_document() {
        let f = Fixture::new("goto");
        let mut editor = Editor::open(&f.page());
        editor.goto_line(9999);
        assert_eq!(editor.cursor_line(), editor.state.lines.len() - 1);
    }

    #[test]
    fn arrow_down_lands_on_the_wrapped_segment_below_not_the_next_line() {
        let f = Fixture::new("wrap-down");
        let mut editor = Editor::open(&f.page());
        editor.render_width = 10;
        let row = editor.state.lines.len() - 1;
        // Wraps into three segments of width 10: "0123456789" | "abcdefghij" | "Z".
        set_line(&mut editor, row, "0123456789abcdefghijZ");

        editor.state.cursor = Index2::new(row, 3);
        editor.move_visual(true);
        assert_eq!(editor.cursor_line(), row, "still the same logical line");
        assert_eq!(editor.cursor_col(), 13, "same offset into the next wrapped segment");

        // The last segment is one char wide ("Z"); a larger offset clamps to
        // its far end (index 21 = one past "Z", the end of the line).
        editor.state.cursor = Index2::new(row, 13);
        editor.move_visual(true);
        assert_eq!(editor.cursor_col(), 21, "clamped to the last (one-char) segment");

        // From that last wrapped segment, down moves to the next logical line.
        editor.state.lines.insert(RowIndex::new(row + 1), "next line".chars().collect::<Vec<char>>());
        editor.state.cursor = Index2::new(row, 20);
        editor.move_visual(true);
        assert_eq!(editor.cursor_line(), row + 1);
        assert_eq!(editor.cursor_col(), 0, "same offset (0) into the next line's only segment");
    }

    #[test]
    fn arrow_up_from_a_wrapped_segment_mirrors_arrow_down() {
        let f = Fixture::new("wrap-up");
        let mut editor = Editor::open(&f.page());
        editor.render_width = 10;
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "0123456789abcdefghijZ");
        editor.state.cursor = Index2::new(row, 13);

        editor.move_visual(false);
        assert_eq!(editor.cursor_line(), row);
        assert_eq!(editor.cursor_col(), 3);
    }

    #[test]
    fn arrow_down_is_unaffected_when_nothing_has_wrapped() {
        let f = Fixture::new("no-wrap");
        let mut editor = Editor::open(&f.page());
        editor.render_width = 0;
        let row = editor.state.lines.len() - 1;
        set_line(&mut editor, row, "short");
        editor.state.lines.insert(RowIndex::new(row + 1), "also short".chars().collect::<Vec<char>>());
        editor.state.cursor = Index2::new(row, 2);

        editor.move_visual(true);
        assert_eq!(editor.cursor_line(), row + 1);
        assert_eq!(editor.cursor_col(), 2);
    }

    // ---- the mtime guard

    /// A real page on disk, since the guard is about the file, not the buffer.
    fn on_disk(name: &str, body: &str) -> (std::path::PathBuf, Editor) {
        let dir = std::env::temp_dir().join(format!("podarcis-editor-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("page.md");
        std::fs::write(&path, body).unwrap();
        let page = Page::load(&path, &dir).unwrap();
        (path, Editor::open(&page))
    }

    /// mtime has one-second granularity on some filesystems, so a test that
    /// writes twice in a row has to make the second write distinguishable.
    fn touch_later(path: &Path, body: &str) {
        std::fs::write(path, body).unwrap();
        let later = std::time::SystemTime::now() + std::time::Duration::from_secs(2);
        let _ = filetime_set(path, later);
    }

    fn filetime_set(path: &Path, when: std::time::SystemTime) -> std::io::Result<()> {
        let file = std::fs::OpenOptions::new().write(true).open(path)?;
        file.set_modified(when)
    }

    #[test]
    fn an_untouched_file_saves_normally() {
        let (path, mut editor) = on_disk("clean", "one\n");
        set_line(&mut editor, 0, "two");
        assert_eq!(editor.save().unwrap(), Save::Written);
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "two\n");
        assert!(!editor.dirty());
    }

    #[test]
    fn a_file_changed_underneath_is_not_written_over() {
        let (path, mut editor) = on_disk("conflict", "one\n");
        set_line(&mut editor, 0, "mine");
        touch_later(&path, "theirs\n");

        assert_eq!(editor.save().unwrap(), Save::ChangedUnderneath);
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            "theirs\n",
            "their edit survives a save that did not know about it"
        );
        assert!(editor.dirty(), "and our text is still in the buffer");
    }

    #[test]
    fn overwriting_keeps_their_version_beside_the_page() {
        let (path, mut editor) = on_disk("overwrite", "one\n");
        set_line(&mut editor, 0, "mine");
        touch_later(&path, "theirs\n");
        assert_eq!(editor.save().unwrap(), Save::ChangedUnderneath);

        let backup = editor.save_overwriting().unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "mine\n");
        assert_eq!(std::fs::read_to_string(&backup).unwrap(), "theirs\n");
        assert_eq!(backup.file_name().unwrap(), "page.conflict-1.md");
    }

    #[test]
    fn saving_twice_in_a_row_does_not_conflict_with_itself() {
        // The guard re-stamps on every write, or the second save of a session
        // would see its own mtime as somebody else's edit.
        let (_path, mut editor) = on_disk("restamp", "one\n");
        set_line(&mut editor, 0, "two");
        assert_eq!(editor.save().unwrap(), Save::Written);
        set_line(&mut editor, 0, "three");
        assert_eq!(editor.save().unwrap(), Save::Written);
    }

    #[test]
    fn a_deleted_file_is_still_written_rather_than_refused() {
        // The buffer is the last copy; refusing is the only outcome that loses it.
        let (path, mut editor) = on_disk("deleted", "one\n");
        set_line(&mut editor, 0, "mine");
        std::fs::remove_file(&path).unwrap();
        assert_eq!(editor.save().unwrap(), Save::Written);
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "mine\n");
    }
}
