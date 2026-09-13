//! Markdown → styled terminal lines.
//!
//! Produces a `Doc` of width-wrapped segments rather than a ratatui `Text`
//! directly, because the reader needs two things a finished `Text` throws away:
//! which segments belong to which link (so one can be highlighted and followed)
//! and which source line each rendered line came from (so `e` drops the editor
//! cursor where you were reading).

use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthStr;

use crate::theme::Theme;
use crate::vault::links::LinkKind;

#[derive(Clone, Debug)]
pub struct Seg {
    pub text: String,
    pub style: Style,
    /// Index into `Doc::links` when this segment is part of a link.
    pub link: Option<usize>,
}

#[derive(Clone, Debug, Default)]
pub struct DocLine {
    pub segs: Vec<Seg>,
    /// 0-based body line this rendered line came from, best effort.
    pub src_line: usize,
}

#[derive(Clone, Debug)]
pub struct DocLink {
    pub target: String,
    pub kind: LinkKind,
    /// Rendered line the link starts on.
    pub line: usize,
}

#[derive(Clone, Debug, Default)]
pub struct Doc {
    pub lines: Vec<DocLine>,
    pub links: Vec<DocLink>,
    /// `(rendered line, level, text)` for the outline popup.
    pub headings: Vec<(usize, u8, String)>,
}

impl Doc {
    pub fn height(&self) -> usize {
        self.lines.len()
    }

    /// Rendered line for a body line — used when switching from the editor back
    /// to the reader without losing your place.
    pub fn line_for_source(&self, src: usize) -> usize {
        self.lines
            .iter()
            .position(|l| l.src_line >= src)
            .unwrap_or(self.lines.len().saturating_sub(1))
    }

    pub fn source_for_line(&self, line: usize) -> usize {
        self.lines.get(line).map(|l| l.src_line).unwrap_or(0)
    }

    /// The next link at or after `from`, wrapping around.
    pub fn link_after(&self, from: Option<usize>, forward: bool) -> Option<usize> {
        if self.links.is_empty() {
            return None;
        }
        let n = self.links.len();
        Some(match (from, forward) {
            (None, true) => 0,
            (None, false) => n - 1,
            (Some(i), true) => (i + 1) % n,
            (Some(i), false) => (i + n - 1) % n,
        })
    }

    /// Convert to ratatui lines, highlighting `active` if set.
    pub fn to_lines(&self, theme: &Theme, active: Option<usize>, from: usize, height: usize) -> Vec<Line<'static>> {
        self.lines
            .iter()
            .skip(from)
            .take(height)
            .map(|line| {
                let spans: Vec<Span<'static>> = line
                    .segs
                    .iter()
                    .map(|seg| {
                        let style = match (seg.link, active) {
                            (Some(i), Some(a)) if i == a => {
                                Style::default().fg(theme.bg).bg(theme.link).add_modifier(Modifier::BOLD)
                            }
                            _ => seg.style,
                        };
                        Span::styled(seg.text.clone(), style)
                    })
                    .collect();
                Line::from(spans)
            })
            .collect()
    }
}

pub fn render(body: &str, width: u16, theme: &Theme) -> Doc {
    let width = width.max(20) as usize;
    let body = &neutralize_footnote_definitions(body);
    let mut w = Writer::new(width, theme);
    let mut opts = Options::empty();
    opts.insert(Options::ENABLE_TABLES);
    opts.insert(Options::ENABLE_STRIKETHROUGH);
    opts.insert(Options::ENABLE_TASKLISTS);

    let parser = Parser::new_ext(body, opts).into_offset_iter();
    for (event, range) in parser {
        w.src_line = body[..range.start].matches('\n').count();
        w.event(event);
    }
    w.flush();
    w.doc
}

/// Blank out `[^id]: …` lines, keeping the line count.
///
/// CommonMark reads such a line as a *link reference definition* with the label
/// `^id`, which turns every `[^id]` in the prose into a shortcut reference link
/// — rendering it as bare `^id` with the brackets eaten, and swallowing the
/// definition block whole. OKF footnotes are not CommonMark links, so the
/// definitions are removed before parsing and shown in the inspector instead.
/// Lines are blanked rather than deleted so source-line mapping still holds.
fn neutralize_footnote_definitions(body: &str) -> String {
    let mut out = String::with_capacity(body.len());
    for (i, line) in body.lines().enumerate() {
        if i > 0 {
            out.push('\n');
        }
        if !is_footnote_definition(line) {
            out.push_str(line);
        }
    }
    if body.ends_with('\n') {
        out.push('\n');
    }
    out
}

pub fn is_footnote_definition(line: &str) -> bool {
    line.trim_start()
        .strip_prefix("[^")
        .and_then(|rest| rest.find("]:"))
        .map(|end| end > 0)
        .unwrap_or(false)
}

/// Nesting context, so a close tag knows what it is closing.
#[derive(Clone, Debug)]
enum Block {
    List { ordered: Option<u64>, index: u64 },
    Quote,
    Code,
    Table { rows: Vec<Vec<String>>, head: bool },
}

struct Writer<'a> {
    doc: Doc,
    theme: &'a Theme,
    width: usize,
    /// Pending inline segments for the current paragraph.
    pending: Vec<Seg>,
    style: Style,
    blocks: Vec<Block>,
    /// Prefix applied to the first line of the current block (a bullet).
    marker: Option<(String, Style)>,
    link: Option<usize>,
    src_line: usize,
    /// Suppress a blank line at the very top of the document.
    started: bool,
    cell: Option<String>,
}

impl<'a> Writer<'a> {
    fn new(width: usize, theme: &'a Theme) -> Self {
        Self {
            doc: Doc::default(),
            theme,
            width,
            pending: Vec::new(),
            style: Style::default().fg(theme.text),
            blocks: Vec::new(),
            marker: None,
            link: None,
            src_line: 0,
            started: false,
            cell: None,
        }
    }

    fn indent(&self) -> usize {
        self.blocks
            .iter()
            .map(|b| match b {
                Block::List { .. } => 2,
                Block::Quote => 2,
                Block::Code => 2,
                Block::Table { .. } => 0,
            })
            .sum()
    }

    fn quote_depth(&self) -> usize {
        self.blocks.iter().filter(|b| matches!(b, Block::Quote)).count()
    }

    fn in_code(&self) -> bool {
        matches!(self.blocks.last(), Some(Block::Code))
    }

    fn in_table(&self) -> bool {
        self.blocks.iter().any(|b| matches!(b, Block::Table { .. }))
    }

    fn push_blank(&mut self) {
        if !self.started {
            return;
        }
        if self.doc.lines.last().map(|l| l.segs.is_empty()).unwrap_or(true) {
            return;
        }
        self.doc.lines.push(DocLine { segs: Vec::new(), src_line: self.src_line });
    }

    fn push_line(&mut self, segs: Vec<Seg>) {
        self.started = true;
        self.doc.lines.push(DocLine { segs, src_line: self.src_line });
    }

    /// Runs with the same style are coalesced. pulldown-cmark hands `[^id]`
    /// back as four separate text events (`[`, `^id`, `]`, …) because OKF
    /// footnotes have no in-body definition to resolve against, so the citation
    /// only becomes visible once the run is whole again.
    fn text(&mut self, s: &str) {
        if let Some(cell) = self.cell.as_mut() {
            cell.push_str(s);
            return;
        }
        if s.is_empty() {
            return;
        }
        match self.pending.last_mut() {
            Some(last) if last.style == self.style && last.link == self.link => last.text.push_str(s),
            _ => self.pending.push(Seg { text: s.to_string(), style: self.style, link: self.link }),
        }
    }

    /// Restyle `[^id]` citations inside a finished run, rewriting them as
    /// `[id]` — the caret is syntax, not something to read. Code spans are left
    /// alone, since a bracket in code is just a bracket.
    fn expand_footnotes(&self, segs: Vec<Seg>) -> Vec<Seg> {
        let citation = Style::default().fg(self.theme.literal).add_modifier(Modifier::DIM);
        let mut out = Vec::with_capacity(segs.len());
        for seg in segs {
            if seg.style.bg.is_some() || !seg.text.contains("[^") {
                out.push(seg);
                continue;
            }
            for (text, is_footnote) in split_footnotes(&seg.text) {
                let style = if is_footnote { citation } else { seg.style };
                out.push(Seg { text, style, link: seg.link });
            }
        }
        out
    }

    /// Flush the pending inline run as wrapped lines.
    fn flush(&mut self) {
        if self.pending.is_empty() {
            return;
        }
        let taken = std::mem::take(&mut self.pending);
        let segs = self.expand_footnotes(taken);
        let indent = self.indent();
        let quote = self.quote_depth();
        let marker = self.marker.take();

        let prefix_width = indent;
        let avail = self.width.saturating_sub(prefix_width).max(8);

        let mut out: Vec<Vec<Seg>> = vec![Vec::new()];
        let mut used = 0usize;
        for seg in segs {
            for (i, word) in split_keeping_spaces(&seg.text).into_iter().enumerate() {
                let w = word.width();
                let is_space = word.chars().all(char::is_whitespace);
                if used + w > avail && used > 0 && !(is_space && i == 0) {
                    out.push(Vec::new());
                    used = 0;
                    if is_space {
                        continue;
                    }
                }
                if used == 0 && is_space {
                    continue;
                }
                out.last_mut().unwrap().push(Seg { text: word, style: seg.style, link: seg.link });
                used += w;
            }
        }

        let bar = Seg {
            text: "▏ ".repeat(quote),
            style: Style::default().fg(self.theme.accent),
            link: None,
        };
        for (i, mut line) in out.into_iter().enumerate() {
            if line.is_empty() {
                continue;
            }
            let mut full = Vec::new();
            if quote > 0 {
                full.push(bar.clone());
            }
            let pad = indent.saturating_sub(quote * 2);
            match (i, &marker) {
                (0, Some((text, style))) => {
                    let lead = pad.saturating_sub(text.width());
                    if lead > 0 {
                        full.push(Seg { text: " ".repeat(lead), style: Style::default(), link: None });
                    }
                    full.push(Seg { text: text.clone(), style: *style, link: None });
                }
                _ if pad > 0 => {
                    full.push(Seg { text: " ".repeat(pad), style: Style::default(), link: None })
                }
                _ => {}
            }
            full.append(&mut line);
            self.push_line(full);
        }
    }

    fn event(&mut self, event: Event) {
        match event {
            Event::Start(tag) => self.start(tag),
            Event::End(tag) => self.end(tag),
            Event::Text(text) => {
                if self.in_code() {
                    for line in text.lines() {
                        let indent = self.indent();
                        self.push_line(vec![
                            Seg {
                                text: format!("{}{}", " ".repeat(indent), line),
                                style: Style::default().fg(self.theme.literal).bg(self.theme.surface),
                                link: None,
                            },
                        ]);
                    }
                } else {
                    self.text(&text);
                }
            }
            Event::Code(code) => {
                let saved = self.style;
                self.style = Style::default().fg(self.theme.literal).bg(self.theme.surface);
                self.text(&code);
                self.style = saved;
            }
            Event::FootnoteReference(label) => {
                let saved = self.style;
                self.style = Style::default().fg(self.theme.literal).add_modifier(Modifier::DIM);
                self.text(&format!("[{label}]"));
                self.style = saved;
            }
            Event::SoftBreak => self.text(" "),
            Event::HardBreak => {
                self.flush();
            }
            Event::Rule => {
                self.flush();
                self.push_blank();
                self.push_line(vec![Seg {
                    text: "─".repeat(self.width.min(80)),
                    style: Style::default().fg(self.theme.faint),
                    link: None,
                }]);
                self.push_blank();
            }
            Event::TaskListMarker(done) => {
                let mark = if done { "[x] " } else { "[ ] " };
                let saved = self.style;
                self.style = Style::default().fg(if done { self.theme.ok } else { self.theme.subtext });
                self.text(mark);
                self.style = saved;
            }
            Event::Html(html) | Event::InlineHtml(html) => {
                let trimmed = html.trim();
                if !trimmed.is_empty() && !trimmed.starts_with("<!--") {
                    let saved = self.style;
                    self.style = Style::default().fg(self.theme.faint);
                    self.text(trimmed);
                    self.style = saved;
                }
            }
            _ => {}
        }
    }

    fn start(&mut self, tag: Tag) {
        match tag {
            Tag::Heading { level, .. } => {
                self.flush();
                self.push_blank();
                let n = heading_level(level);
                self.style = self.theme.heading(n);
                if n <= 2 {
                    self.marker = None;
                }
            }
            Tag::Paragraph => {
                self.flush();
                if !self.in_table() {
                    self.push_blank();
                }
            }
            Tag::BlockQuote(_) => {
                self.flush();
                self.push_blank();
                self.blocks.push(Block::Quote);
            }
            Tag::CodeBlock(kind) => {
                self.flush();
                self.push_blank();
                if let CodeBlockKind::Fenced(lang) = &kind {
                    if !lang.is_empty() {
                        let indent = self.indent();
                        self.push_line(vec![Seg {
                            text: format!("{}{}", " ".repeat(indent), lang),
                            style: Style::default().fg(self.theme.faint).add_modifier(Modifier::ITALIC),
                            link: None,
                        }]);
                    }
                }
                self.blocks.push(Block::Code);
            }
            Tag::List(ordered) => {
                self.flush();
                if self.blocks.iter().all(|b| !matches!(b, Block::List { .. })) {
                    self.push_blank();
                }
                self.blocks.push(Block::List { ordered, index: ordered.unwrap_or(1) });
            }
            Tag::Item => {
                self.flush();
                let (text, style) = match self.blocks.last_mut() {
                    Some(Block::List { ordered: Some(_), index }) => {
                        let n = *index;
                        *index += 1;
                        (format!("{n}. "), Style::default().fg(self.theme.accent))
                    }
                    _ => ("• ".to_string(), Style::default().fg(self.theme.accent)),
                };
                self.marker = Some((text, style));
            }
            Tag::Emphasis => self.style = self.style.add_modifier(Modifier::ITALIC),
            Tag::Strong => self.style = self.style.add_modifier(Modifier::BOLD),
            Tag::Strikethrough => self.style = self.style.add_modifier(Modifier::CROSSED_OUT),
            Tag::Link { dest_url, .. } => {
                let kind = classify(&dest_url);
                self.doc.links.push(DocLink {
                    target: dest_url.to_string(),
                    kind,
                    line: self.doc.lines.len(),
                });
                self.link = Some(self.doc.links.len() - 1);
                self.style = self.style.fg(self.theme.link).add_modifier(Modifier::UNDERLINED);
            }
            Tag::Image { dest_url, .. } => {
                self.text(&format!("🖼 {dest_url} "));
            }
            Tag::Table(_) => {
                self.flush();
                self.push_blank();
                self.blocks.push(Block::Table { rows: Vec::new(), head: false });
            }
            Tag::TableHead => {
                if let Some(Block::Table { head, .. }) = self.blocks.last_mut() {
                    *head = true;
                }
            }
            Tag::TableRow => {
                if let Some(Block::Table { rows, .. }) = self.blocks.last_mut() {
                    rows.push(Vec::new());
                }
            }
            Tag::TableCell => self.cell = Some(String::new()),
            _ => {}
        }
    }

    fn end(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Heading(_) => {
                let text: String = self.pending.iter().map(|s| s.text.as_str()).collect();
                let line = self.doc.lines.len();
                self.flush();
                self.doc.headings.push((line, 1, text));
                self.style = Style::default().fg(self.theme.text);
            }
            TagEnd::Paragraph => self.flush(),
            TagEnd::BlockQuote(_) => {
                self.flush();
                self.blocks.pop();
            }
            TagEnd::CodeBlock => {
                self.blocks.pop();
                self.push_blank();
            }
            TagEnd::List(_) => {
                self.flush();
                self.blocks.pop();
                self.marker = None;
            }
            TagEnd::Item => {
                self.flush();
                self.marker = None;
            }
            TagEnd::Emphasis => self.style = self.style.remove_modifier(Modifier::ITALIC),
            TagEnd::Strong => self.style = self.style.remove_modifier(Modifier::BOLD),
            TagEnd::Strikethrough => self.style = self.style.remove_modifier(Modifier::CROSSED_OUT),
            TagEnd::Link => {
                self.link = None;
                self.style = Style::default().fg(self.theme.text);
            }
            TagEnd::TableCell => {
                let text = self.cell.take().unwrap_or_default();
                if let Some(Block::Table { rows, .. }) = self.blocks.last_mut() {
                    if let Some(row) = rows.last_mut() {
                        row.push(text.trim().to_string());
                    }
                }
            }
            TagEnd::TableHead => {
                if let Some(Block::Table { head, .. }) = self.blocks.last_mut() {
                    *head = false;
                }
            }
            TagEnd::Table => {
                if let Some(Block::Table { rows, .. }) = self.blocks.pop() {
                    self.render_table(&rows);
                }
            }
            _ => {}
        }
    }

    fn render_table(&mut self, rows: &[Vec<String>]) {
        if rows.is_empty() {
            return;
        }
        let cols = rows.iter().map(Vec::len).max().unwrap_or(0);
        let mut widths = vec![0usize; cols];
        for row in rows {
            for (i, cell) in row.iter().enumerate() {
                widths[i] = widths[i].max(cell.width());
            }
        }
        let budget = self.width.saturating_sub(cols * 3 + 1).max(cols * 4);
        let total: usize = widths.iter().sum();
        if total > budget {
            let scale = budget as f64 / total as f64;
            for w in widths.iter_mut() {
                *w = ((*w as f64 * scale) as usize).max(4);
            }
        }

        let rule: String = widths.iter().map(|w| format!("─{}─", "─".repeat(*w))).collect::<Vec<_>>().join("┼");
        for (r, row) in rows.iter().enumerate() {
            let style = if r == 0 {
                Style::default().fg(self.theme.accent).add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(self.theme.text)
            };
            let cells: Vec<String> = (0..cols)
                .map(|i| {
                    let cell = row.get(i).map(String::as_str).unwrap_or("");
                    format!(" {} ", pad_or_trim(cell, widths[i]))
                })
                .collect();
            self.push_line(vec![Seg { text: cells.join("│"), style, link: None }]);
            if r == 0 {
                self.push_line(vec![Seg {
                    text: rule.clone(),
                    style: Style::default().fg(self.theme.faint),
                    link: None,
                }]);
            }
        }
        self.push_blank();
    }
}

fn heading_level(level: HeadingLevel) -> u8 {
    match level {
        HeadingLevel::H1 => 1,
        HeadingLevel::H2 => 2,
        HeadingLevel::H3 => 3,
        HeadingLevel::H4 => 4,
        HeadingLevel::H5 => 5,
        HeadingLevel::H6 => 6,
    }
}

fn classify(target: &str) -> LinkKind {
    if target.starts_with('#') {
        LinkKind::Anchor
    } else if target.contains("://") || target.starts_with("mailto:") || target.starts_with("gdrive:") {
        LinkKind::External
    } else {
        LinkKind::Relative
    }
}

fn pad_or_trim(text: &str, width: usize) -> String {
    let w = text.width();
    if w <= width {
        format!("{text}{}", " ".repeat(width - w))
    } else {
        let mut out = String::new();
        for ch in text.chars() {
            if out.width() + 1 >= width {
                break;
            }
            out.push(ch);
        }
        out.push('…');
        format!("{out}{}", " ".repeat(width.saturating_sub(out.width())))
    }
}

/// Split a text run into plain parts and `[^label]` citations, rewriting each
/// citation as `[label]` — the caret is syntax, not something to read.
fn split_footnotes(text: &str) -> Vec<(String, bool)> {
    let mut out: Vec<(String, bool)> = Vec::new();
    let mut rest = text;
    while let Some(start) = rest.find("[^") {
        if let Some(len) = rest[start + 2..].find(']') {
            let label = &rest[start + 2..start + 2 + len];
            if !label.is_empty() && !label.contains('[') {
                if start > 0 {
                    out.push((rest[..start].to_string(), false));
                }
                out.push((format!("[{label}]"), true));
                rest = &rest[start + 2 + len + 1..];
                continue;
            }
        }
        out.push((rest[..start + 2].to_string(), false));
        rest = &rest[start + 2..];
    }
    if !rest.is_empty() {
        out.push((rest.to_string(), false));
    }
    out
}

/// Split into words and the whitespace runs between them, so wrapping can drop
/// a space at a line break without gluing words together.
fn split_keeping_spaces(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut current = String::new();
    let mut in_space = false;
    for ch in text.chars() {
        let is_space = ch.is_whitespace();
        if !current.is_empty() && is_space != in_space {
            out.push(std::mem::take(&mut current));
        }
        in_space = is_space;
        current.push(ch);
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn doc(body: &str, width: u16) -> Doc {
        render(body, width, &Theme::default())
    }

    fn plain(doc: &Doc) -> Vec<String> {
        doc.lines
            .iter()
            .map(|l| l.segs.iter().map(|s| s.text.as_str()).collect::<String>())
            .collect()
    }

    #[test]
    fn wraps_at_the_given_width_without_gluing_words() {
        let d = doc("alpha beta gamma delta epsilon zeta", 20);
        for line in plain(&d) {
            assert!(line.width() <= 20, "{line:?} is wider than 20");
        }
        let joined = plain(&d).join(" ");
        assert!(joined.contains("alpha beta"));
        assert!(joined.contains("epsilon zeta"));
        assert!(!joined.contains("gammadelta"));
    }

    #[test]
    fn never_emits_a_line_wider_than_the_viewport() {
        let body = "# A very long heading that certainly does not fit\n\n- a list item that is also rather long indeed\n\n> a quoted passage that goes on and on and on\n";
        for width in [20u16, 30, 47, 80] {
            for line in plain(&doc(body, width)) {
                assert!(line.width() <= width as usize, "width {width}: {line:?}");
            }
        }
    }

    #[test]
    fn records_links_with_their_kind() {
        let d = doc("See [A](../a.md), [B](https://x.dev) and [C](#s).\n", 80);
        assert_eq!(d.links.len(), 3);
        assert_eq!(d.links[0].target, "../a.md");
        assert_eq!(d.links[0].kind, LinkKind::Relative);
        assert_eq!(d.links[1].kind, LinkKind::External);
        assert_eq!(d.links[2].kind, LinkKind::Anchor);
        assert!(d.lines.iter().any(|l| l.segs.iter().any(|s| s.link == Some(0))));
    }

    #[test]
    fn link_cursor_wraps_in_both_directions() {
        let d = doc("[A](a.md) [B](b.md)\n", 80);
        assert_eq!(d.link_after(None, true), Some(0));
        assert_eq!(d.link_after(Some(1), true), Some(0));
        assert_eq!(d.link_after(Some(0), false), Some(1));
        assert_eq!(doc("no links", 80).link_after(None, true), None);
    }

    #[test]
    fn bullets_and_numbers_are_rendered_and_nested() {
        let out = plain(&doc("- one\n- two\n  - deep\n\n1. first\n2. second\n", 80));
        let joined = out.join("\n");
        assert!(joined.contains("• one"), "{joined}");
        assert!(joined.contains("  • deep"), "{joined}");
        assert!(joined.contains("1. first"), "{joined}");
        assert!(joined.contains("2. second"), "{joined}");
    }

    #[test]
    fn blockquotes_get_a_bar() {
        let out = plain(&doc("> \u{26a0}\u{fe0f} competing hypothesis\n", 80));
        assert!(out.iter().any(|l| l.starts_with('▏')), "{out:?}");
    }

    #[test]
    fn a_footnote_definition_does_not_turn_its_references_into_links() {
        // The failure this guards against is subtle: CommonMark reads
        // `[^id]: …` as a link reference definition, and then renders every
        // `[^id]` as bare `^id`.
        let d = doc(
            "Claim[^burke_2023].\n\n[^burke_2023]: [nutrition/x/raw.md](../x/raw.md)\n",
            90,
        );
        let text = plain(&d).join("\n");
        assert!(text.contains("[burke_2023]"), "{text:?}");
        assert!(!text.contains("^burke_2023"), "{text:?}");
        assert!(d.links.is_empty(), "a footnote definition is not a link");
        assert!(!text.contains("raw.md"), "the definition block is not prose");
    }

    #[test]
    fn blanking_definitions_keeps_the_line_count() {
        let body = "one\n[^a]: x\ntwo\n";
        assert_eq!(neutralize_footnote_definitions(body).lines().count(), 3);
        assert_eq!(neutralize_footnote_definitions(body), "one\n\ntwo\n");
    }

    #[test]
    fn only_real_definitions_are_blanked() {
        assert!(is_footnote_definition("[^a]: text"));
        assert!(is_footnote_definition("  [^a]: text"));
        assert!(!is_footnote_definition("[^a] not a definition"));
        assert!(!is_footnote_definition("[^]: empty label"));
        assert!(!is_footnote_definition("plain prose"));
    }

    #[test]
    fn footnote_references_are_shown_without_the_caret() {
        let out = plain(&doc("Body[^smith2024] text.\n", 80)).join(" ");
        assert!(out.contains("[smith2024]"), "{out}");
        assert!(!out.contains("[^smith2024]"));
    }

    #[test]
    fn tables_render_aligned_with_a_rule() {
        let out = plain(&doc("| a | bbbb |\n|---|------|\n| 1 | 2 |\n", 80));
        let joined = out.join("\n");
        assert!(joined.contains('│'), "{joined}");
        assert!(joined.contains('┼'), "{joined}");
    }

    #[test]
    fn code_blocks_keep_their_lines_verbatim() {
        let out = plain(&doc("```python\nx = 1\ny = 2\n```\n", 80));
        assert!(out.iter().any(|l| l.trim() == "x = 1"));
        assert!(out.iter().any(|l| l.trim() == "y = 2"));
        assert!(out.iter().any(|l| l.trim() == "python"));
    }

    #[test]
    fn headings_are_collected_for_the_outline() {
        let d = doc("# One\n\ntext\n\n## Two\n", 80);
        let titles: Vec<&str> = d.headings.iter().map(|(_, _, t)| t.as_str()).collect();
        assert_eq!(titles, vec!["One", "Two"]);
    }

    #[test]
    fn source_lines_are_monotonic_and_map_back() {
        let d = doc("# One\n\npara one\n\n## Two\n\npara two\n", 80);
        let srcs: Vec<usize> = d.lines.iter().map(|l| l.src_line).collect();
        assert!(srcs.windows(2).all(|w| w[0] <= w[1]), "{srcs:?}");
        let line = d.line_for_source(6);
        assert!(d.source_for_line(line) >= 4);
    }

    #[test]
    fn consecutive_footnotes_are_all_styled() {
        let d = doc("Claim[^a][^b] end.\n", 80);
        let cited: Vec<&str> = d
            .lines
            .iter()
            .flat_map(|l| l.segs.iter())
            .filter(|s| s.style.add_modifier.contains(Modifier::DIM))
            .map(|s| s.text.as_str())
            .collect();
        assert_eq!(cited, vec!["[a]", "[b]"]);
    }

    #[test]
    fn a_lone_bracket_caret_is_left_alone() {
        let out = plain(&doc("array[^ ] and [^] here\n", 80)).join(" ");
        assert!(out.contains("[^"), "{out}");
    }

    #[test]
    fn empty_input_renders_nothing_and_does_not_panic() {
        assert!(doc("", 80).lines.is_empty());
        assert!(doc("\n\n\n", 80).lines.iter().all(|l| l.segs.is_empty()));
    }

    #[test]
    fn a_very_narrow_viewport_still_produces_output() {
        let d = doc("some prose that must fit somewhere", 1);
        assert!(!d.lines.is_empty());
    }
}
