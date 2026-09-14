//! Markdown → styled terminal lines.
//!
//! Produces a `Doc` of width-wrapped segments rather than a ratatui `Text`
//! directly, because the reader needs two things a finished `Text` throws away:
//! which segments belong to which link (so one can be highlighted and followed)
//! and which source line each rendered line came from (so `e` drops the editor
//! cursor where you were reading).

use pulldown_cmark::{
    Alignment, BlockQuoteKind, CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd,
};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthStr;

use crate::theme::Theme;
use crate::vault::links::LinkKind;

/// Prose past this column count is harder to track line-to-line, so the
/// reader and edit preview never wrap wider than this even on an ultra-wide
/// pane.
pub const MAX_WIDTH: u16 = 120;

#[derive(Clone, Debug)]
pub struct Seg {
    pub text: String,
    pub style: Style,
    /// Index into `Doc::links` when this segment is part of a link.
    pub link: Option<usize>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TextPos {
    pub line: usize,
    pub col: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Selection {
    pub anchor: TextPos,
    pub cursor: TextPos,
}

impl Selection {
    pub fn new(anchor: TextPos, cursor: TextPos) -> Self {
        Self { anchor, cursor }
    }

    pub fn range(&self) -> (TextPos, TextPos) {
        if (self.anchor.line, self.anchor.col) <= (self.cursor.line, self.cursor.col) {
            (self.anchor, self.cursor)
        } else {
            (self.cursor, self.anchor)
        }
    }

    pub fn is_empty(&self) -> bool {
        self.anchor == self.cursor
    }
}

#[derive(Clone, Debug, Default)]
pub struct DocLine {
    pub segs: Vec<Seg>,
    /// 0-based body line this rendered line came from, best effort.
    pub src_line: usize,
}

impl DocLine {
    pub fn plain_text(&self) -> String {
        self.segs.iter().map(|s| s.text.as_str()).collect()
    }
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
    /// Footnote labels in order of first citation, so the body can show a
    /// short `[1]` where the source writes `[^atarodi_2024_…]` and the
    /// inspector can number the same sources identically.
    pub citations: Vec<String>,
}

impl Doc {
    /// 1-based citation number for a footnote label, if the body cites it.
    pub fn citation_number(&self, label: &str) -> Option<usize> {
        self.citations.iter().position(|l| l == label).map(|i| i + 1)
    }

    /// Index of the first body link for a footnote label — the `[n]` the
    /// reader can highlight when its source row is picked in the inspector.
    pub fn citation_link(&self, label: &str) -> Option<usize> {
        self.links
            .iter()
            .position(|l| l.kind == LinkKind::Footnote && l.target == label)
    }
}

impl Doc {
    pub fn height(&self) -> usize {
        self.lines.len()
    }

    /// Extract selected text as a string, preserving line breaks.
    ///
    /// Rendered, not raw-source, text — links, emphasis markers and heading
    /// hashes are already stripped by rendering. The reader's copy commands
    /// use `Open::selected_source_text` instead, which returns the untouched
    /// markdown; this stays as the primitive the rendering tests exercise.
    #[allow(dead_code)]
    pub fn selected_text(&self, sel: Selection) -> String {
        let (start, end) = sel.range();
        if start == end {
            return String::new();
        }
        let mut result = Vec::new();
        for line_idx in start.line..=end.line {
            let Some(line) = self.lines.get(line_idx) else {
                break;
            };
            let plain = line.plain_text();
            let chars: Vec<char> = plain.chars().collect();
            let col_start = if line_idx == start.line { start.col.min(chars.len()) } else { 0 };
            let col_end = if line_idx == end.line { end.col.min(chars.len()) } else { chars.len() };
            if col_start < col_end {
                result.push(chars[col_start..col_end].iter().collect::<String>());
            } else if (line_idx != start.line && line_idx != end.line)
                || (line_idx == start.line && col_start == 0 && chars.is_empty())
            {
                // A line wholly inside the selection, or an empty first line,
                // still contributes a newline to the copied text.
                result.push(String::new());
            }
        }
        result.join("\n")
    }

    #[allow(dead_code)]
    pub fn plain_text(&self) -> String {
        self.lines.iter().map(|l| l.plain_text()).collect::<Vec<_>>().join("\n")
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

    /// Convert to ratatui lines, highlighting `active` link and `selection` if set.
    pub fn to_lines(
        &self,
        theme: &Theme,
        active: Option<usize>,
        selection: Option<Selection>,
        from: usize,
        height: usize,
    ) -> Vec<Line<'static>> {
        let sel_range = selection.filter(|s| !s.is_empty()).map(|s| s.range());
        let sel_style = Style::default().fg(theme.bg).bg(theme.accent);

        self.lines
            .iter()
            .enumerate()
            .skip(from)
            .take(height)
            .map(|(line_idx, line)| {
                let line_sel = sel_range.and_then(|(start, end)| {
                    if line_idx >= start.line && line_idx <= end.line {
                        let total_chars = line.plain_text().chars().count();
                        let col_start = if line_idx == start.line { start.col } else { 0 };
                        let col_end = if line_idx == end.line { end.col } else { total_chars };
                        if col_start < col_end {
                            Some((col_start, col_end))
                        } else {
                            None
                        }
                    } else {
                        None
                    }
                });

                let mut spans: Vec<Span<'static>> = Vec::new();
                let mut cur_char_offset = 0usize;

                for seg in &line.segs {
                    let base_style = match (seg.link, active) {
                        (Some(i), Some(a)) if i == a => {
                            Style::default().fg(theme.bg).bg(theme.link).add_modifier(Modifier::BOLD)
                        }
                        _ => seg.style,
                    };

                    let seg_char_count = seg.text.chars().count();
                    let seg_start = cur_char_offset;
                    let seg_end = cur_char_offset + seg_char_count;
                    cur_char_offset = seg_end;

                    match line_sel {
                        Some((sel_start, sel_end))
                            if sel_end > seg_start && sel_start < seg_end =>
                        {
                            let overlap_start = seg_start.max(sel_start);
                            let overlap_end = seg_end.min(sel_end);
                            let chars: Vec<char> = seg.text.chars().collect();
                            let rel_start = overlap_start - seg_start;
                            let rel_end = overlap_end - seg_start;

                            if rel_start > 0 {
                                let before: String = chars[..rel_start].iter().collect();
                                spans.push(Span::styled(before, base_style));
                            }
                            let mid: String = chars[rel_start..rel_end].iter().collect();
                            spans.push(Span::styled(mid, sel_style));
                            if rel_end < chars.len() {
                                let after: String = chars[rel_end..].iter().collect();
                                spans.push(Span::styled(after, base_style));
                            }
                        }
                        _ => {
                            spans.push(Span::styled(seg.text.clone(), base_style));
                        }
                    }
                }
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
    // Alerts, so `> [!WARNING]` arrives as a typed blockquote rather than as
    // literal prose inside the quote.
    opts.insert(Options::ENABLE_GFM);
    // `$…$`, `$$…$$`, `\(…\)` and `\[…\]` become math events rather than prose.
    // A lone `$` in front of a price is safe: `$` only opens a span when the
    // next character is not whitespace and a whitespace-preceded `$` can never
    // close one, so `$40 to $50` stays text.
    opts.insert(Options::ENABLE_MATH);

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
    /// `indent` is how far the item's text sits from the block's left edge —
    /// the width of its own marker, so a nested list lines up under the text
    /// of its parent rather than under the parent's bullet. `items` counts the
    /// items opened so far, which is what tells a second item from a first,
    /// and `item_line` is where the open item started, which is what tells a
    /// one-line item from one that wrapped.
    List { ordered: Option<u64>, index: u64, indent: usize, items: usize, item_line: usize },
    Quote { tone: Tone },
    Code,
    Table { rows: Vec<Row>, head: bool, aligns: Vec<Alignment> },
}

/// One table row: cells, each a styled run that may contain links.
type Row = Vec<Vec<Seg>>;

/// What a blockquote is *for*. GFM spells it `> [!WARNING]`; the wiki spells
/// it with a leading emoji (`> ⚠️ Moderate evidence`), and both mean the same
/// thing to a reader, so both colour the bar.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Tone {
    Plain,
    Note,
    Tip,
    Important,
    Warning,
    Caution,
}

impl Tone {
    fn of(kind: Option<BlockQuoteKind>) -> Self {
        match kind {
            Some(BlockQuoteKind::Note) => Self::Note,
            Some(BlockQuoteKind::Tip) => Self::Tip,
            Some(BlockQuoteKind::Important) => Self::Important,
            Some(BlockQuoteKind::Warning) => Self::Warning,
            Some(BlockQuoteKind::Caution) => Self::Caution,
            None => Self::Plain,
        }
    }

    /// The tone a leading emoji implies, for quotes that predate GFM alerts.
    fn of_emoji(text: &str) -> Option<Self> {
        let lead = text.trim_start().chars().next()?;
        Some(match lead {
            '⚠' | '❗' | '‼' => Self::Warning,
            '🚫' | '❌' | '⛔' => Self::Caution,
            '✅' | '✔' | '💡' => Self::Tip,
            'ℹ' | '📝' | '📌' => Self::Note,
            _ => return None,
        })
    }

    fn title(self) -> Option<&'static str> {
        Some(match self {
            Self::Note => "Note",
            Self::Tip => "Tip",
            Self::Important => "Important",
            Self::Warning => "Warning",
            Self::Caution => "Caution",
            Self::Plain => return None,
        })
    }
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
    /// The table cell being collected, as a styled run.
    cell: Option<Vec<Seg>>,
    /// Source of a mermaid block being collected.
    mermaid: Option<String>,
    /// `(language, source, first body line)` of a fenced block being collected.
    /// Buffered rather than emitted line by line because the band is drawn to
    /// the full pane width and the highlighter needs the language before the
    /// first line. The line is remembered from the opening fence, since by the
    /// time the block is drawn `src_line` has moved to the closing one.
    code: Option<(String, String, usize)>,
    /// `(destination, alt text so far)` of the image being collected.
    image: Option<(String, String)>,
    /// Whether each link has been placed on a line yet, so a link's recorded
    /// line is the one it is actually drawn on rather than the one its
    /// paragraph started on.
    placed: Vec<bool>,
    /// Level of the heading being collected.
    heading: u8,
    /// Suppress the blank line before the next paragraph — a callout's title
    /// row belongs to the paragraph under it, not above it.
    tight: bool,
    /// The style a strong span started from, so closing it restores exactly
    /// what `**` opened over.
    strong: Option<Style>,
}

impl<'a> Writer<'a> {
    fn new(width: usize, theme: &'a Theme) -> Self {
        // The document leads with one blank row so its first line (normally the
        // title) never sits flush against the pane's border. It is part of the
        // doc — recorded first, so every line index to come already accounts
        // for it — rather than chrome pinned to the top of the pane, so it
        // scrolls away with the content.
        let mut doc = Doc::default();
        doc.lines.push(DocLine { segs: Vec::new(), src_line: 0 });
        Self {
            doc,
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
            mermaid: None,
            code: None,
            image: None,
            placed: Vec::new(),
            heading: 0,
            tight: false,
            strong: None,
        }
    }

    fn indent(&self) -> usize {
        self.blocks
            .iter()
            .map(|b| match b {
                Block::List { indent, .. } => *indent,
                Block::Quote { .. } => 2,
                Block::Code => 2,
                Block::Table { .. } => 0,
            })
            .sum()
    }

    fn quote_depth(&self) -> usize {
        self.blocks.iter().filter(|b| matches!(b, Block::Quote { .. })).count()
    }

    /// The tone of the innermost quote, which colours its bar.
    fn quote_tone(&self) -> Tone {
        self.blocks
            .iter()
            .rev()
            .find_map(|b| match b {
                Block::Quote { tone } => Some(*tone),
                _ => None,
            })
            .unwrap_or(Tone::Plain)
    }

    fn tone_colour(&self, tone: Tone) -> Color {
        match tone {
            Tone::Plain => self.theme.accent,
            Tone::Note => self.theme.link,
            Tone::Tip => self.theme.ok,
            Tone::Important => self.theme.accent,
            Tone::Warning => self.theme.warn,
            Tone::Caution => self.theme.err,
        }
    }

    /// Math shares the literal colour with code, but keeps an italic face and no
    /// band, so it reads as a changed register mid-sentence rather than as code.
    fn math_style(&self) -> Style {
        Style::default().fg(self.theme.literal).add_modifier(Modifier::ITALIC)
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
        let n = self.doc.lines.len();
        for seg in &segs {
            if let Some(i) = seg.link {
                if !self.placed[i] {
                    self.placed[i] = true;
                    self.doc.links[i].line = n;
                }
            }
        }
        self.doc.lines.push(DocLine { segs, src_line: self.src_line });
    }

    /// Runs with the same style are coalesced. pulldown-cmark hands `[^id]`
    /// back as four separate text events (`[`, `^id`, `]`, …) because OKF
    /// footnotes have no in-body definition to resolve against, so the citation
    /// only becomes visible once the run is whole again.
    fn text(&mut self, s: &str) {
        if s.is_empty() {
            return;
        }
        if let Some((_, alt)) = self.image.as_mut() {
            alt.push_str(s);
            return;
        }
        // The first text of an untyped quote decides its tone: the wiki marks
        // confidence with a leading emoji, not with a GFM alert header.
        if self.pending.is_empty() && self.cell.is_none() {
            if let Some(tone) = Tone::of_emoji(s) {
                if let Some(Block::Quote { tone: current }) =
                    self.blocks.iter_mut().rev().find(|b| matches!(b, Block::Quote { .. }))
                {
                    if *current == Tone::Plain {
                        *current = tone;
                    }
                }
            }
        }
        let (style, link) = (self.style, self.link);
        let run = self.cell.as_mut().unwrap_or(&mut self.pending);
        match run.last_mut() {
            Some(last) if last.style == style && last.link == link => last.text.push_str(s),
            _ => run.push(Seg { text: s.to_string(), style, link }),
        }
    }

    /// Restyle `[^id]` citations inside a finished run, rewriting them as
    /// `[id]` — the caret is syntax, not something to read. Code spans are left
    /// alone, since a bracket in code is just a bracket.
    fn expand_footnotes(&mut self, segs: Vec<Seg>) -> Vec<Seg> {
        let citation = Style::default().fg(self.theme.literal).add_modifier(Modifier::DIM);
        let mut out = Vec::with_capacity(segs.len());
        for seg in segs {
            if seg.style.bg.is_some() || !seg.text.contains("[^") {
                out.push(seg);
                continue;
            }
            for (text, label) in split_footnotes(&seg.text) {
                match label {
                    Some(label) => {
                        let n = self.cite(&label);
                        // A fresh `DocLink` per occurrence — same citation
                        // clicked twice in the body is two separate targets,
                        // just like two `<a>` tags to the same URL.
                        self.doc.links.push(DocLink {
                            target: label,
                            kind: LinkKind::Footnote,
                            line: self.doc.lines.len(),
                        });
                        // Kept in lockstep with `doc.links` — `push_line` indexes
                        // both by the same `seg.link`.
                        self.placed.push(false);
                        let link = Some(self.doc.links.len() - 1);
                        out.push(Seg { text: format!("[{n}]"), style: citation, link });
                    }
                    None => out.push(Seg { text, style: seg.style, link: seg.link }),
                }
            }
        }
        out
    }

    /// The citation number for a label, assigning the next one on first sight.
    fn cite(&mut self, label: &str) -> usize {
        match self.doc.citations.iter().position(|l| l == label) {
            Some(i) => i + 1,
            None => {
                self.doc.citations.push(label.to_string());
                self.doc.citations.len()
            }
        }
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

        // Columns the marker needs beyond the block indent — a `10. ` or a
        // heading's `### ` is wider than the two spaces a list nests by, and
        // its continuation lines hang under the text, not under the marker.
        let pad = indent.saturating_sub(quote * 2);
        let marker_width = marker.as_ref().map(|(t, _)| t.width()).unwrap_or(0);
        let hang = marker_width.saturating_sub(pad);
        let avail = self.width.saturating_sub(indent + hang).max(8);

        let out = wrap(segs, avail);

        let bar = Seg {
            text: "▌ ".repeat(quote),
            style: Style::default().fg(self.tone_colour(self.quote_tone())),
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
            match (i, &marker) {
                (0, Some((text, style))) => {
                    let lead = pad.saturating_sub(marker_width);
                    if lead > 0 {
                        full.push(Seg { text: " ".repeat(lead), style: Style::default(), link: None });
                    }
                    full.push(Seg { text: text.clone(), style: *style, link: None });
                }
                _ if pad + hang > 0 => full.push(Seg {
                    text: " ".repeat(pad + hang),
                    style: Style::default(),
                    link: None,
                }),
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
                if let Some(code) = self.mermaid.as_mut() {
                    code.push_str(&text);
                } else if let Some((_, code, _)) = self.code.as_mut() {
                    code.push_str(&text);
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
            Event::InlineMath(math) => {
                let saved = self.style;
                self.style = self.math_style();
                self.text(&tex_to_unicode(&math));
                self.style = saved;
            }
            Event::DisplayMath(math) => {
                self.flush();
                self.push_blank();
                let style = self.math_style();
                let indent = self.indent();
                let avail = self.width.saturating_sub(indent);
                for line in tex_to_unicode(&math).split('\n') {
                    let body = line.trim();
                    if body.is_empty() {
                        continue;
                    }
                    let (text, fits) = if body.width() <= avail {
                        (body.to_string(), true)
                    } else {
                        // A formula is never wrapped — a broken one reads as a
                        // different expression. What does not fit is cut with an
                        // ellipsis; the editor is one keypress away.
                        let (head, _) = split_at_width(body, avail.saturating_sub(1));
                        (format!("{head}…"), false)
                    };
                    let lead = if fits { (avail - text.width()) / 2 } else { 0 };
                    self.push_line(vec![
                        Seg { text: " ".repeat(indent + lead), style: Style::default(), link: None },
                        Seg { text, style, link: None },
                    ]);
                }
                self.push_blank();
            }
            Event::SoftBreak => self.text(" "),
            Event::HardBreak => {
                self.flush();
            }
            Event::Rule => {
                self.flush();
                self.push_blank();
                let indent = self.indent();
                self.push_line(vec![
                    Seg { text: " ".repeat(indent), style: Style::default(), link: None },
                    Seg {
                        text: "─".repeat(self.width.saturating_sub(indent).max(1)),
                        style: Style::default().fg(self.theme.faint),
                        link: None,
                    },
                ]);
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
        }
    }

    fn start(&mut self, tag: Tag) {
        match tag {
            Tag::Heading { level, .. } => {
                self.flush();
                self.push_blank();
                let n = heading_level(level);
                self.heading = n;
                self.style = self.theme.heading(n);
                // The `#`s are kept, dimmed: at a glance they are the only
                // thing that tells an h4 from an h5 in a terminal that has
                // neither size nor spacing to spend on the difference.
                self.marker = Some((
                    format!("{} ", "#".repeat(n as usize)),
                    Style::default().fg(self.theme.faint),
                ));
            }
            Tag::Paragraph => {
                self.flush();
                if !self.in_table() && !std::mem::take(&mut self.tight) {
                    self.push_blank();
                }
            }
            Tag::BlockQuote(kind) => {
                self.flush();
                self.push_blank();
                let tone = Tone::of(kind);
                self.blocks.push(Block::Quote { tone });
                if let Some(title) = tone.title() {
                    let colour = self.tone_colour(tone);
                    let quote = self.quote_depth();
                    self.push_line(vec![
                        Seg {
                            text: "▌ ".repeat(quote),
                            style: Style::default().fg(colour),
                            link: None,
                        },
                        Seg {
                            text: format!("{} {title}", tone_glyph(tone)),
                            style: Style::default().fg(colour).add_modifier(Modifier::BOLD),
                            link: None,
                        },
                    ]);
                    self.tight = true;
                }
            }
            Tag::CodeBlock(kind) => {
                self.flush();
                self.push_blank();
                let lang = match &kind {
                    CodeBlockKind::Fenced(lang) => lang.to_string(),
                    CodeBlockKind::Indented => String::new(),
                };
                if lang.eq_ignore_ascii_case("mermaid") {
                    // Collected rather than printed: a mermaid block is a
                    // picture, and it can only be drawn once the whole source
                    // is in hand.
                    self.mermaid = Some(String::new());
                } else {
                    // Also collected: the band is drawn to the pane width and
                    // the highlighter wants the language before the first line.
                    self.code = Some((lang, String::new(), self.src_line + 1));
                }
                self.blocks.push(Block::Code);
            }
            Tag::List(ordered) => {
                self.flush();
                if self.blocks.iter().all(|b| !matches!(b, Block::List { .. })) {
                    self.push_blank();
                }
                let index = ordered.unwrap_or(1);
                let indent = match ordered {
                    Some(start) => format!("{start}. ").width(),
                    None => 2,
                };
                self.blocks.push(Block::List { ordered, index, indent, items: 0, item_line: 0 });
            }
            Tag::Item => {
                self.flush();
                let line = self.doc.lines.len();
                let depth =
                    self.blocks.iter().filter(|b| matches!(b, Block::List { .. })).count();
                // A blank line between top-level items, but only where one of
                // them runs past a single line. A row is the smallest gap a
                // terminal has, so it is too much air between one-line items —
                // a list of short entries reads as a list already — and about
                // right between items that are themselves paragraphs.
                let wrapped = match self.blocks.last_mut() {
                    Some(Block::List { items, item_line, .. }) => {
                        *items += 1;
                        let wrapped = *items > 1 && line - *item_line > 1;
                        *item_line = line;
                        wrapped
                    }
                    _ => false,
                };
                if wrapped && depth == 1 {
                    self.push_blank();
                    if let Some(Block::List { item_line, .. }) = self.blocks.last_mut() {
                        *item_line = self.doc.lines.len();
                    }
                }
                let text = match self.blocks.last_mut() {
                    Some(Block::List { ordered: Some(_), index, .. }) => {
                        let n = *index;
                        *index += 1;
                        format!("{n}. ")
                    }
                    // Nesting is two spaces deep, which on its own is easy to
                    // lose in a wrapped paragraph; the glyph makes the level
                    // readable without counting columns.
                    _ => format!("{} ", ["•", "◦", "▪"][(depth.max(1) - 1) % 3]),
                };
                self.marker = Some((text, Style::default().fg(self.theme.accent)));
            }
            Tag::Emphasis => self.style = self.style.add_modifier(Modifier::ITALIC),
            Tag::Strong => {
                // Bold text jumps to the page's contrast pole (black on light,
                // white on dark) because on many terminals `BOLD` alone renders
                // like body text. A link keeps its own colour so emphasis
                // inside a link doesn't erase the "this is a link" cue.
                self.strong = Some(self.style);
                self.style = if self.link.is_some() {
                    self.style.add_modifier(Modifier::BOLD)
                } else {
                    let bold = self.theme.bold();
                    if self.style.add_modifier.contains(Modifier::ITALIC) {
                        bold.add_modifier(Modifier::ITALIC)
                    } else {
                        bold
                    }
                };
            }
            Tag::Strikethrough => self.style = self.style.add_modifier(Modifier::CROSSED_OUT),
            Tag::Link { dest_url, .. } => {
                let kind = classify(&dest_url);
                self.doc.links.push(DocLink {
                    target: dest_url.to_string(),
                    kind,
                    line: self.doc.lines.len(),
                });
                self.placed.push(false);
                self.link = Some(self.doc.links.len() - 1);
                self.style = self.style.fg(self.theme.link).add_modifier(Modifier::UNDERLINED);
            }
            Tag::Image { dest_url, .. } => {
                self.image = Some((dest_url.to_string(), String::new()));
            }
            Tag::Table(aligns) => {
                self.flush();
                self.push_blank();
                self.blocks.push(Block::Table { rows: Vec::new(), head: false, aligns });
            }
            Tag::TableHead => {
                // pulldown emits the header's cells directly inside TableHead,
                // with no TableRow of their own — so the row has to be opened
                // here or the header is dropped on the floor.
                if let Some(Block::Table { rows, head, .. }) = self.blocks.last_mut() {
                    *head = true;
                    rows.push(Vec::new());
                }
            }
            Tag::TableRow => {
                if let Some(Block::Table { rows, .. }) = self.blocks.last_mut() {
                    rows.push(Vec::new());
                }
            }
            Tag::TableCell => self.cell = Some(Vec::new()),
            _ => {}
        }
    }

    fn end(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Heading(_) => {
                let text: String = self.pending.iter().map(|s| s.text.as_str()).collect();
                let line = self.doc.lines.len();
                let level = std::mem::take(&mut self.heading);
                self.flush();
                // A page's h1 is its title, and a rule under it separates the
                // title from the body the way a blank line cannot.
                if level == 1 {
                    let indent = self.indent();
                    self.push_line(vec![
                        Seg { text: " ".repeat(indent), style: Style::default(), link: None },
                        Seg {
                            text: "━".repeat(self.width.saturating_sub(indent).max(1)),
                            style: Style::default().fg(self.theme.accent).add_modifier(Modifier::DIM),
                            link: None,
                        },
                    ]);
                }
                self.doc.headings.push((line, level, text));
                self.style = Style::default().fg(self.theme.text);
            }
            TagEnd::Paragraph => self.flush(),
            TagEnd::BlockQuote(_) => {
                self.flush();
                self.blocks.pop();
                self.push_blank();
            }
            TagEnd::CodeBlock => {
                self.blocks.pop();
                if let Some(code) = self.mermaid.take() {
                    self.draw_mermaid(&code);
                } else if let Some((lang, code, from)) = self.code.take() {
                    self.draw_code(&lang, &code, from);
                }
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
            TagEnd::Strong => {
                if let Some(saved) = self.strong.take() {
                    self.style = saved;
                }
            }
            TagEnd::Strikethrough => self.style = self.style.remove_modifier(Modifier::CROSSED_OUT),
            TagEnd::Link => {
                self.link = None;
                self.style = Style::default().fg(self.theme.text);
            }
            TagEnd::Image => {
                if let Some((dest, alt)) = self.image.take() {
                    let label = if alt.trim().is_empty() { dest } else { alt };
                    let saved = self.style;
                    self.style = Style::default().fg(self.theme.subtext).add_modifier(Modifier::ITALIC);
                    self.text(&format!("🖼 {label}"));
                    self.style = saved;
                }
            }
            TagEnd::TableCell => {
                // Through the same path as prose, so a citation in a cell is
                // numbered in document order alongside the rest.
                let raw = self.cell.take().unwrap_or_default();
                let cell = trim_segs(self.expand_footnotes(raw));
                if let Some(Block::Table { rows, .. }) = self.blocks.last_mut() {
                    if let Some(row) = rows.last_mut() {
                        row.push(cell);
                    }
                }
            }
            TagEnd::TableHead => {
                if let Some(Block::Table { head, .. }) = self.blocks.last_mut() {
                    *head = false;
                }
            }
            TagEnd::Table => {
                if let Some(Block::Table { rows, aligns, .. }) = self.blocks.pop() {
                    self.render_table(&rows, &aligns);
                }
            }
            _ => {}
        }
    }

    /// Draw a fenced code block as a banded slab: a tinted background the full
    /// width of the block, a coloured gutter down its left edge, and the
    /// language as a chip on the first row. The band is what separates code
    /// from prose at a glance — tinting only the glyphs leaves a ragged edge
    /// that reads as damage rather than as structure.
    fn draw_code(&mut self, lang: &str, code: &str, from: usize) {
        let indent = self.indent();
        let band = self.width.saturating_sub(indent).max(8);
        // gutter, one space, and a trailing space of breathing room.
        let avail = band.saturating_sub(3);
        let surface = Style::default().bg(self.theme.surface);
        let mut hl = crate::ui::highlight::Highlighter::new(lang);

        let row = |this: &mut Self, body: Vec<Seg>| {
            let mut segs = Vec::new();
            if indent > 0 {
                segs.push(Seg { text: " ".repeat(indent), style: Style::default(), link: None });
            }
            segs.push(Seg {
                text: "▎".to_string(),
                style: surface.fg(this.theme.accent),
                link: None,
            });
            segs.push(Seg { text: " ".to_string(), style: surface, link: None });
            let used: usize = body.iter().map(|s| s.text.width()).sum();
            segs.extend(body);
            segs.push(Seg {
                text: " ".repeat(band.saturating_sub(used + 2)),
                style: surface,
                link: None,
            });
            this.push_line(segs);
        };

        if !lang.trim().is_empty() {
            let chip = Seg {
                text: lang.trim().to_string(),
                style: surface.fg(self.theme.faint).add_modifier(Modifier::ITALIC),
                link: None,
            };
            row(self, vec![chip]);
        }

        let start = self.src_line;
        for (n, line) in code.trim_end_matches('\n').lines().enumerate() {
            // Source lines map one to one here, unlike prose, so the editor
            // lands on the line you were looking at inside the block.
            self.src_line = from + n;
            let expanded = line.replace('\t', "    ");
            let mut used = 0usize;
            let mut body = Vec::new();
            for (text, tok) in hl.line(&expanded) {
                let style = surface.patch(self.token_style(tok));
                let w = text.width();
                if used + w <= avail {
                    used += w;
                    body.push(Seg { text, style, link: None });
                    continue;
                }
                // Code is never wrapped: a broken line reads as a different
                // program. What does not fit is cut with an ellipsis, and the
                // editor is one keypress away.
                let room = avail.saturating_sub(used + 1);
                if room > 0 {
                    let (head, _) = split_at_width(&text, room);
                    body.push(Seg { text: format!("{head}…"), style, link: None });
                } else if used < avail {
                    body.push(Seg { text: "…".to_string(), style, link: None });
                }
                break;
            }
            row(self, body);
        }
        self.src_line = start;
    }

    fn token_style(&self, tok: crate::ui::highlight::Tok) -> Style {
        crate::ui::highlight::token_style(self.theme, tok)
    }

    /// Draw a mermaid block, falling back to its source when the kind is not
    /// one we can draw — a wrong picture is worse than legible source.
    fn draw_mermaid(&mut self, code: &str) {
        let indent = self.indent();
        let available = self.width.saturating_sub(indent);
        let Some(rows) = super::mermaid::render(code, available) else {
            self.push_line(vec![Seg {
                text: format!("{}mermaid", " ".repeat(indent)),
                style: Style::default().fg(self.theme.faint).add_modifier(Modifier::ITALIC),
                link: None,
            }]);
            for line in code.lines() {
                self.push_line(vec![Seg {
                    text: format!("{}{line}", " ".repeat(indent)),
                    style: Style::default().fg(self.theme.literal).bg(self.theme.surface),
                    link: None,
                }]);
            }
            return;
        };

        for row in rows {
            // A diagram is never wrapped — a broken box is unreadable — so an
            // over-long row (a label in the outline fallback) is trimmed.
            let mut used = indent;
            let mut segs: Vec<Seg> = vec![Seg {
                text: " ".repeat(indent),
                style: Style::default(),
                link: None,
            }];
            for (text, ink) in row {
                let style = self.ink(ink);
                if used + text.width() <= self.width {
                    used += text.width();
                    segs.push(Seg { text, style, link: None });
                    continue;
                }
                let room = self.width.saturating_sub(used + 1);
                if room > 0 {
                    let cut: String = text.chars().take(room).collect();
                    segs.push(Seg { text: format!("{cut}…"), style, link: None });
                }
                break;
            }
            self.push_line(segs);
        }
    }

    fn ink(&self, ink: super::mermaid::Ink) -> Style {
        use super::mermaid::Ink;
        match ink {
            Ink::Frame => Style::default().fg(self.theme.faint),
            Ink::Node => Style::default().fg(self.theme.text).add_modifier(Modifier::BOLD),
            Ink::Edge => Style::default().fg(self.theme.accent),
            Ink::Label => Style::default().fg(self.theme.literal),
            Ink::Title => Style::default().fg(self.theme.subtext).add_modifier(Modifier::ITALIC),
        }
    }

    /// Draw a table as a closed box.
    ///
    /// Columns are shrunk widest-first rather than scaled proportionally: a
    /// table of one prose column and three short ones should lose columns from
    /// the prose, not squeeze `n` down to four characters alongside it.
    fn render_table(&mut self, rows: &[Row], aligns: &[Alignment]) {
        // Markdown tables live on a handful of source lines; mapping every
        // rendered line to the parse line keeps the reader's editor entry on
        // the table.
        let src = self.src_line;
        self.render_table_rows(rows, aligns, |_| src);
    }

    /// Like `render_table`, but each rendered line maps back to the source line
    /// `src(row)` suggested for the grid row it draws — how a CSV table keeps
    /// the editor cursor on the raw file row it mirrors.
    fn render_table_rows(&mut self, rows: &[Row], aligns: &[Alignment], src: impl Fn(usize) -> usize) {
        let cols = rows.iter().map(Vec::len).max().unwrap_or(0);
        if cols == 0 {
            return;
        }
        let indent = self.indent();
        let mut widths = vec![0usize; cols];
        for row in rows {
            for (i, cell) in row.iter().enumerate() {
                widths[i] = widths[i].max(seg_width(cell));
            }
        }
        // `│ ` before every cell plus a closing `│`.
        let overhead = 3 * cols + 1;
        shrink_to(&mut widths, self.width.saturating_sub(indent + overhead), self.width / 8);

        let frame = Style::default().fg(self.theme.faint);
        let pad = Seg { text: " ".repeat(indent), style: Style::default(), link: None };
        let rule = |left: &str, mid: &str, right: &str| {
            let bars: Vec<String> = widths.iter().map(|w| "─".repeat(w + 2)).collect();
            format!("{left}{}{right}", bars.join(mid))
        };

        self.src_line = src(0);
        self.push_line(vec![
            pad.clone(),
            Seg { text: rule("┌", "┬", "┐"), style: frame, link: None },
        ]);
        let head = Style::default().fg(self.theme.accent).add_modifier(Modifier::BOLD);
        for (r, row) in rows.iter().enumerate() {
            self.src_line = src(r);
            // Cells wrap rather than truncate: a table of prose in a narrow
            // pane is all ellipsis otherwise, and a tall table still says
            // something where a clipped one says nothing.
            let cells: Vec<Vec<Vec<Seg>>> = widths
                .iter()
                .enumerate()
                .map(|(i, width)| wrap(row.get(i).cloned().unwrap_or_default(), *width))
                .collect();
            let height = cells.iter().map(Vec::len).max().unwrap_or(1);
            for line in 0..height {
                let mut segs = vec![pad.clone()];
                for (i, width) in widths.iter().enumerate() {
                    segs.push(Seg { text: "│ ".to_string(), style: frame, link: None });
                    let empty = Vec::new();
                    let cell = cells[i].get(line).unwrap_or(&empty);
                    let align = aligns.get(i).copied().unwrap_or(Alignment::None);
                    segs.extend(fit_segs(cell, *width, align, (r == 0).then_some(head)));
                    segs.push(Seg { text: " ".to_string(), style: frame, link: None });
                }
                segs.push(Seg { text: "│".to_string(), style: frame, link: None });
                self.push_line(segs);
            }
            if r == 0 {
                self.push_line(vec![
                    pad.clone(),
                    Seg { text: rule("├", "┼", "┤"), style: frame, link: None },
                ]);
            }
        }
        self.src_line = src(rows.len().saturating_sub(1));
        self.push_line(vec![pad, Seg { text: rule("└", "┴", "┘"), style: frame, link: None }]);
        self.push_blank();
    }
}

/// Render a pre-parsed grid (a CSV) as a closed box table — the same drawing
/// markdown tables get, but from data already split into cells.
///
/// Each content line maps back to the grid row it draws, so pressing `e` in the
/// reader drops the editor cursor on the raw CSV line the visible row came from.
pub fn render_grid(rows: &[Vec<String>], width: u16, theme: &Theme) -> Doc {
    let width = width.max(20) as usize;
    let grid: Vec<Row> = rows
        .iter()
        .map(|row| {
            row.iter()
                .map(|cell| vec![Seg { text: cell.clone(), style: Style::default(), link: None }])
                .collect()
        })
        .collect();
    let cols = grid.iter().map(Vec::len).max().unwrap_or(0);
    let aligns = vec![Alignment::None; cols];
    let mut w = Writer::new(width, theme);
    w.render_table_rows(&grid, &aligns, |r| r);
    w.doc
}

fn tone_glyph(tone: Tone) -> &'static str {
    match tone {
        Tone::Note => "ℹ",
        Tone::Tip => "💡",
        Tone::Important => "◆",
        Tone::Warning => "⚠",
        Tone::Caution => "⛔",
        Tone::Plain => "▌",
    }
}

fn seg_width(segs: &[Seg]) -> usize {
    segs.iter().map(|s| s.text.width()).sum()
}

/// Drop the whitespace a cell picked up around its markdown.
fn trim_segs(mut segs: Vec<Seg>) -> Vec<Seg> {
    if let Some(first) = segs.first_mut() {
        first.text = first.text.trim_start().to_string();
    }
    if let Some(last) = segs.last_mut() {
        last.text = last.text.trim_end().to_string();
    }
    segs.retain(|s| !s.text.is_empty());
    segs
}

/// Shrink the widest column repeatedly until the row fits, never below a width
/// that can still show something.
fn shrink_to(widths: &mut [usize], budget: usize, min: usize) {
    let min = min.max(3);
    let floor = min * widths.len();
    let budget = budget.max(floor);
    while widths.iter().sum::<usize>() > budget {
        let Some(widest) = widths
            .iter()
            .enumerate()
            .filter(|(_, w)| **w > min)
            .max_by_key(|(_, w)| **w)
            .map(|(i, _)| i)
        else {
            break;
        };
        widths[widest] -= 1;
    }
}

/// Pad or truncate a styled cell to an exact width, keeping its styles and its
/// links — a link in a table is still a link to follow.
fn fit_segs(segs: &[Seg], width: usize, align: Alignment, force: Option<Style>) -> Vec<Seg> {
    let restyle = |seg: &Seg| Seg {
        text: seg.text.clone(),
        style: force.unwrap_or(seg.style),
        link: seg.link,
    };
    let total = seg_width(segs);
    let mut out: Vec<Seg> = Vec::with_capacity(segs.len() + 2);
    let blank = |n: usize| Seg { text: " ".repeat(n), style: Style::default(), link: None };

    if total > width {
        let mut used = 0usize;
        for seg in segs {
            let w = seg.text.width();
            if used + w <= width.saturating_sub(1) {
                used += w;
                out.push(restyle(seg));
                continue;
            }
            let (head, _) = split_at_width(&seg.text, width.saturating_sub(used + 1));
            used += head.width();
            out.push(Seg { text: head, ..restyle(seg) });
            break;
        }
        out.push(Seg { text: "…".to_string(), style: force.unwrap_or_default(), link: None });
        used += 1;
        if used < width {
            out.push(blank(width - used));
        }
        return out;
    }

    let slack = width - total;
    let (left, right) = match align {
        Alignment::Right => (slack, 0),
        Alignment::Center => (slack / 2, slack - slack / 2),
        _ => (0, slack),
    };
    if left > 0 {
        out.push(blank(left));
    }
    out.extend(segs.iter().map(restyle));
    if right > 0 {
        out.push(blank(right));
    }
    out
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

/// Render TeX math as well as one terminal line can: map the command spellings
/// the notes actually use to their unicode glyphs and keep everything else
/// verbatim — a wrong glyph is worse than legible source, and the editor is one
/// keypress away. Grouping braces are syntax and are dropped; `\frac{a}{b}`
/// becomes `a/b` so even a fraction stays on one line.
fn tex_to_unicode(src: &str) -> String {
    let chars: Vec<char> = src.chars().collect();
    let mut out = String::with_capacity(src.len());
    let mut i = 0;
    while i < chars.len() {
        match chars[i] {
            '{' | '}' => i += 1,
            '~' => {
                out.push(' ');
                i += 1;
            }
            '\\' => {
                let start = i + 1;
                let mut end = start;
                while end < chars.len() && chars[end].is_ascii_alphabetic() {
                    end += 1;
                }
                if end > start {
                    let cmd: String = chars[start..end].iter().collect();
                    i = end;
                    match cmd.as_str() {
                        "frac" => {
                            if let Some((num, den, after)) = frac_terms(&chars, end) {
                                out.push_str(&tex_to_unicode(&num));
                                out.push('/');
                                out.push_str(&tex_to_unicode(&den));
                                i = after;
                            } else {
                                out.push_str("frac");
                            }
                            continue;
                        }
                        // Font-and-text commands just print the word they wrap:
                        // `\text{or}` is "or", and `\mathbb{R}` is "R".
                        "text" | "mbox" | "textrm" | "textup" | "mathrm" | "mathbf" | "mathit"
                        | "mathsf" | "mathtt" | "mathcal" | "mathbb" | "mathfrak"
                        | "operatorname" => {
                            if let Some((inner, after)) = take_braced(&chars, end) {
                                out.push_str(&tex_to_unicode(&inner));
                                i = after;
                            }
                            continue;
                        }
                        // Delimiter sizing — `\left(` means `(`, no more.
                        "left" | "right" | "big" | "Big" | "bigg" | "Bigg" | "bigl" | "bigr"
                        | "Bigl" | "Bigr" | "biggl" | "biggr" | "Biggl" | "Biggr" => continue,
                        _ => {}
                    }
                    match tex_command(&cmd) {
                        Some(glyph) => out.push_str(glyph),
                        None => {
                            out.push('\\');
                            out.push_str(&cmd);
                        }
                    }
                } else {
                    // One non-letter after the backslash: `\,`, `\ `, `\%`, `\\`.
                    i = match chars.get(start) {
                        None => {
                            out.push('\\');
                            start
                        }
                        Some(&c) => {
                            let esc: String = ['\\', c].iter().collect();
                            match esc.as_str() {
                                // Spacing commands all collapse to a space.
                                "\\," | "\\;" | "\\:" | "\\!" | "\\ " => out.push(' '),
                                "\\\\" => out.push('\n'),
                                "\\%" => out.push('%'),
                                "\\$" => out.push('$'),
                                "\\#" => out.push('#'),
                                "\\&" => out.push('&'),
                                "\\_" => out.push('_'),
                                "\\{" => out.push('{'),
                                "\\}" => out.push('}'),
                                _ => out.push_str(&esc),
                            }
                            start + 1
                        }
                    };
                }
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    out
}

/// TeX command → terminal glyph. Only the spellings a note plausibly uses;
/// anything else stays verbatim in the rendered line.
fn tex_command(cmd: &str) -> Option<&'static str> {
    Some(match cmd {
        // arrows
        "to" | "rightarrow" => "→",
        "gets" | "leftarrow" => "←",
        "leftrightarrow" => "↔",
        "Rightarrow" | "implies" => "⇒",
        "Leftarrow" | "impliedby" => "⇐",
        "Leftrightarrow" | "iff" => "⇔",
        "uparrow" => "↑",
        "downarrow" => "↓",
        "updownarrow" => "↕",
        "Uparrow" => "⇑",
        "Downarrow" => "⇓",
        "Updownarrow" => "⇕",
        "mapsto" => "↦",
        "longrightarrow" => "⟶",
        "longleftarrow" => "⟵",
        "longleftrightarrow" => "⟷",
        "nearrow" => "↗",
        "searrow" => "↘",
        "swarrow" => "↙",
        "nwarrow" => "↖",
        "hookrightarrow" => "↪",
        "hookleftarrow" => "↩",
        "twoheadrightarrow" => "↠",
        "rightleftharpoons" => "⇌",
        // greek, lower
        "alpha" => "α",
        "beta" => "β",
        "gamma" => "γ",
        "delta" => "δ",
        "epsilon" => "ε",
        "varepsilon" => "ϵ",
        "zeta" => "ζ",
        "eta" => "η",
        "theta" => "θ",
        "vartheta" => "ϑ",
        "iota" => "ι",
        "kappa" => "κ",
        "lambda" => "λ",
        "mu" => "μ",
        "nu" => "ν",
        "xi" => "ξ",
        "pi" => "π",
        "varpi" => "ϖ",
        "rho" => "ρ",
        "varrho" => "ϱ",
        "sigma" => "σ",
        "varsigma" => "ς",
        "tau" => "τ",
        "upsilon" => "υ",
        "phi" => "φ",
        "varphi" => "ϕ",
        "chi" => "χ",
        "psi" => "ψ",
        "omega" => "ω",
        // greek, upper
        "Gamma" => "Γ",
        "Delta" => "Δ",
        "Theta" => "Θ",
        "Lambda" => "Λ",
        "Xi" => "Ξ",
        "Pi" => "Π",
        "Sigma" => "Σ",
        "Upsilon" => "Υ",
        "Phi" => "Φ",
        "Psi" => "Ψ",
        "Omega" => "Ω",
        // relations
        "neg" | "lnot" => "¬",
        "in" => "∈",
        "notin" => "∉",
        "ni" => "∋",
        "subset" => "⊂",
        "supset" => "⊃",
        "subseteq" => "⊆",
        "supseteq" => "⊇",
        "nsubseteq" => "⊈",
        "nsupseteq" => "⊉",
        "cup" => "∪",
        "cap" => "∩",
        "sqcup" => "⊔",
        "sqcap" => "⊓",
        "bigcup" => "⋃",
        "bigcap" => "⋂",
        "wedge" | "land" => "∧",
        "vee" | "lor" => "∨",
        "forall" => "∀",
        "exists" => "∃",
        "nexists" => "∄",
        "therefore" => "∴",
        "because" => "∵",
        "mid" => "∣",
        "nmid" => "∤",
        "parallel" => "∥",
        "perp" => "⊥",
        "leq" | "le" => "≤",
        "geq" | "ge" => "≥",
        "neq" | "ne" => "≠",
        "approx" => "≈",
        "equiv" => "≡",
        "sim" => "∼",
        "simeq" => "≃",
        "cong" => "≅",
        "propto" => "∝",
        // operators and misc
        "pm" => "±",
        "mp" => "∓",
        "times" => "×",
        "div" => "÷",
        "cdot" => "⋅",
        "cdots" => "⋯",
        "ldots" | "dots" => "…",
        "vdots" => "⋮",
        "ddots" => "⋱",
        "sum" => "∑",
        "prod" => "∏",
        "coprod" => "∐",
        "int" => "∫",
        "iint" => "∬",
        "iiint" => "∭",
        "oint" => "∮",
        "star" => "⋆",
        "ast" => "∗",
        "circ" => "∘",
        "bullet" => "•",
        "dagger" => "†",
        "ddagger" => "‡",
        "partial" => "∂",
        "nabla" => "∇",
        "infty" => "∞",
        "emptyset" | "varnothing" => "∅",
        "prime" => "′",
        "angle" => "∠",
        "triangle" => "△",
        "square" => "□",
        "surd" | "sqrt" => "√",
        "quad" => "  ",
        "qquad" => "    ",
        _ => return None,
    })
}

/// The next `{…}` group as its (nested-aware) inner text plus the index just
/// past the close, or `None` if the next character is not `{`.
fn take_braced(chars: &[char], at: usize) -> Option<(String, usize)> {
    if chars.get(at) != Some(&'{') {
        return None;
    }
    let mut depth = 0usize;
    let mut inner = String::new();
    for (k, &c) in chars.iter().enumerate().skip(at) {
        match c {
            '{' => {
                if depth > 0 {
                    inner.push('{');
                }
                depth += 1;
            }
            '}' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    return Some((inner, k + 1));
                }
                inner.push('}');
            }
            _ => inner.push(c),
        }
    }
    None
}

/// `\frac`'s two groups, or `None` when either is missing.
fn frac_terms(chars: &[char], at: usize) -> Option<(String, String, usize)> {
    let (num, after) = take_braced(chars, at)?;
    let (den, end) = take_braced(chars, after)?;
    Some((num, den, end))
}

/// Split a text run into plain parts and `[^label]` citations, handing back the
/// label of each citation so the caller can number it. The label itself is not
/// something to read mid-sentence: `[^atarodi_2024_freelance_consulting]` is
/// forty columns of identifier in the middle of a clause, and the inspector
/// already lists what each one resolves to.
fn split_footnotes(text: &str) -> Vec<(String, Option<String>)> {
    let mut out: Vec<(String, Option<String>)> = Vec::new();
    let mut rest = text;
    while let Some(start) = rest.find("[^") {
        if let Some(len) = rest[start + 2..].find(']') {
            let label = &rest[start + 2..start + 2 + len];
            if !label.is_empty() && !label.contains('[') {
                if start > 0 {
                    out.push((rest[..start].to_string(), None));
                }
                out.push((label.to_string(), Some(label.to_string())));
                rest = &rest[start + 2 + len + 1..];
                continue;
            }
        }
        out.push((rest[..start + 2].to_string(), None));
        rest = &rest[start + 2..];
    }
    if !rest.is_empty() {
        out.push((rest.to_string(), None));
    }
    out
}

/// Wrap a styled run to `avail` columns, breaking only at whitespace and
/// dropping the space a break falls on.
///
/// Styling boundaries are not break opportunities: `varies[1].` arrives as
/// three segments — prose, citation, full stop — and breaking between them
/// would leave a line starting with `[1].`. Adjacent non-space segments are
/// therefore welded into one token and move together.
///
/// A token wider than the whole line — a bare URL, a long identifier — is cut
/// across lines rather than allowed to run past the pane, because the reader
/// clips at the viewport edge and everything past it would simply be gone.
fn wrap(segs: Vec<Seg>, avail: usize) -> Vec<Vec<Seg>> {
    let mut out: Vec<Vec<Seg>> = vec![Vec::new()];
    let mut used = 0usize;
    let newline = |out: &mut Vec<Vec<Seg>>, used: &mut usize| {
        out.push(Vec::new());
        *used = 0;
    };

    for token in tokenize(segs) {
        let width: usize = token.pieces.iter().map(|p| p.text.width()).sum();
        if token.space {
            if used == 0 {
                continue;
            }
            if used + width > avail {
                newline(&mut out, &mut used);
                continue;
            }
            out.last_mut().unwrap().extend(token.pieces);
            used += width;
            continue;
        }

        if used + width > avail && used > 0 {
            newline(&mut out, &mut used);
        }
        if width <= avail {
            out.last_mut().unwrap().extend(token.pieces);
            used += width;
            continue;
        }
        for piece in token.pieces {
            let mut text = piece.text;
            while !text.is_empty() {
                let (mut head, mut tail) = split_at_width(&text, avail - used);
                if head.is_empty() {
                    if used > 0 {
                        newline(&mut out, &mut used);
                        continue;
                    }
                    // A single character wider than the line: take it anyway
                    // rather than spin.
                    let mut chars = text.chars();
                    head = chars.next().map(String::from).unwrap_or_default();
                    tail = chars.collect();
                }
                used += head.width();
                out.last_mut()
                    .unwrap()
                    .push(Seg { text: head, style: piece.style, link: piece.link });
                text = tail;
                if used >= avail && !text.is_empty() {
                    newline(&mut out, &mut used);
                }
            }
        }
    }
    out
}

/// A run that moves as a unit when wrapping: either one whitespace run or one
/// word, the word carrying however many styled pieces it was written with.
struct Token {
    pieces: Vec<Seg>,
    space: bool,
}

fn tokenize(segs: Vec<Seg>) -> Vec<Token> {
    let mut out: Vec<Token> = Vec::new();
    for seg in segs {
        for part in split_keeping_spaces(&seg.text) {
            let space = part.chars().all(char::is_whitespace);
            let piece = Seg { text: part, style: seg.style, link: seg.link };
            match out.last_mut() {
                Some(last) if !space && !last.space => last.pieces.push(piece),
                _ => out.push(Token { pieces: vec![piece], space }),
            }
        }
    }
    out
}

/// Split a string at a display width, never inside a character.
fn split_at_width(text: &str, width: usize) -> (String, String) {
    let mut head = String::new();
    let mut used = 0usize;
    let mut chars = text.chars().peekable();
    while let Some(&ch) = chars.peek() {
        let w = ch.to_string().width();
        if used + w > width {
            break;
        }
        used += w;
        head.push(ch);
        chars.next();
    }
    (head, chars.collect())
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
        let body = "# A very long heading that certainly does not fit\n\n- a list item that is also rather long indeed\n\n> a quoted passage that goes on and on and on\n\n| a | bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb | c |\n|---|---|---|\n| 1 | 2 | 3 |\n\n```rust\nlet extremely_long_identifier = some_function_with_a_long_name(argument);\n```\n\n1. https://example.com/an/absurdly/long/path/with/no/break/opportunity/at/all\n";
        for width in [20u16, 30, 47, 80] {
            for line in plain(&doc(body, width)) {
                assert!(line.width() <= width as usize, "width {width}: {line:?}");
            }
        }
    }

    #[test]
    fn a_citation_never_starts_a_line_on_its_own() {
        // The citation is its own styled segment; a break between the word and
        // its marker would strand `[1].` at the head of a line.
        let d = doc("adherence varies[^manfredini_2021] over time.\n", 22);
        for line in plain(&d) {
            assert!(!line.trim_start().starts_with('['), "{:?}", plain(&d));
        }
        assert!(plain(&d).iter().any(|l| l.contains("varies[1]")), "{:?}", plain(&d));
    }

    #[test]
    fn a_word_wider_than_the_line_is_broken_rather_than_left_to_overflow() {
        let url = "https://example.com/a/really/long/path/that/nobody/should/have/written";
        let out = plain(&doc(&format!("See {url} here.\n"), 30));
        assert!(out.iter().all(|l| l.width() <= 30), "{out:?}");
        // Broken, not dropped: every character survives somewhere.
        assert!(out.join("").contains("nobody/should/have/written"), "{out:?}");
    }

    #[test]
    fn a_gfm_alert_colours_its_bar_and_keeps_its_title_attached() {
        let theme = Theme::default();
        let d = render("> [!WARNING]\n> Do not do this.\n", 60, &theme);
        let out = plain(&d);
        // Line 0 is the doc's leading blank; the quote follows it.
        assert!(out[1].contains("Warning"), "{out:?}");
        assert!(out[2].contains("Do not do this."), "the title is not orphaned: {out:?}");
        let bars: Vec<&Seg> = d.lines.iter().filter_map(|l| l.segs.first()).collect();
        assert!(bars.iter().all(|s| s.style.fg == Some(theme.warn)), "the bar takes the tone");
    }

    #[test]
    fn a_leading_emoji_tones_a_plain_quote() {
        let theme = Theme::default();
        let warn = render("> \u{26a0}\u{fe0f} single source\n", 60, &theme);
        assert_eq!(warn.lines[1].segs[0].style.fg, Some(theme.warn));
        let plain_quote = render("> just a quote\n", 60, &theme);
        assert_eq!(plain_quote.lines[1].segs[0].style.fg, Some(theme.accent));
    }

    #[test]
    fn a_table_cell_keeps_its_links_and_its_alignment() {
        let d = doc("| a | b |\n|:--|--:|\n| [Doc](../x.md) | 7 |\n", 40);
        assert_eq!(d.links.len(), 1, "a link in a cell is still a link");
        assert_eq!(d.links[0].target, "../x.md");
        let line = &d.lines[d.links[0].line];
        assert!(line.segs.iter().any(|s| s.link == Some(0) && s.text.contains("Doc")), "{line:?}");
        // Right-aligned, so the padding precedes the value.
        let row = line.plain_text();
        assert!(row.trim_end().ends_with("7 │"), "{row:?}");
    }

    #[test]
    fn a_link_records_the_line_it_is_drawn_on() {
        // Not the line its paragraph started on: `gg`-style jumps scroll to
        // the recorded line, and a long paragraph would land pages away.
        let d = doc("one two three four five [target](a.md)\n", 12);
        let line = d.links[0].line;
        assert!(d.lines[line].plain_text().contains("target"), "{:?}", plain(&d));
    }

    #[test]
    fn code_is_highlighted_against_a_full_width_band() {
        let theme = Theme::default();
        let d = render("```rust\nlet x = 1;\n```\n", 40, &theme);
        let band: Vec<&DocLine> =
            d.lines.iter().filter(|l| l.plain_text().contains('▎')).collect();
        assert_eq!(band.len(), 2, "the chip and the one line");
        for line in &band {
            assert_eq!(line.plain_text().width(), 40, "the band is the full width");
            assert!(line.segs.iter().all(|s| s.style.bg == Some(theme.surface)));
        }
        let keyword = band[1].segs.iter().find(|s| s.text == "let").expect("a keyword run");
        assert_eq!(keyword.style.fg, Some(theme.accent));
    }

    #[test]
    fn an_unknown_fence_language_is_banded_but_not_coloured() {
        let theme = Theme::default();
        let d = render("```brainfuck\nlet x = 1;\n```\n", 40, &theme);
        let code = d.lines.iter().find(|l| l.plain_text().contains("let x")).unwrap();
        assert!(code.segs.iter().all(|s| s.style.bg == Some(theme.surface)));
        assert!(code.segs.iter().all(|s| s.style.fg != Some(theme.accent) || s.text == "▎"));
    }

    #[test]
    fn an_image_shows_its_alt_text_and_falls_back_to_its_destination() {
        assert!(plain(&doc("![a diagram](x.png)\n", 60)).join("").contains("🖼 a diagram"));
        assert!(plain(&doc("![](x.png)\n", 60)).join("").contains("🖼 x.png"));
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
    fn short_items_stay_tight_and_only_a_multi_line_one_earns_a_gap() {
        // "two" runs to three lines with its sub-list, so the eye needs the
        // break after it; "one" does not.
        let out = plain(&doc("- one\n- two\n  - deep\n  - deeper\n- three\n", 40));
        assert_eq!(
            out,
            vec!["", "• one", "• two", "  ◦ deep", "  ◦ deeper", "", "• three"],
            "{out:?}"
        );
    }

    #[test]
    fn a_list_of_one_line_entries_gets_no_gaps_at_all() {
        let out = plain(&doc("- alpha\n- beta\n- gamma\n", 40));
        assert_eq!(out, vec!["", "• alpha", "• beta", "• gamma"], "{out:?}");
    }

    #[test]
    fn a_wrapped_item_is_not_split_by_the_gap() {
        let out = plain(&doc("- one that is long enough to wrap twice over\n- two\n", 20));
        // The leading blank plus the single inter-item gap — never one inside an item.
        let blanks = out.iter().filter(|l| l.is_empty()).count();
        assert_eq!(blanks, 2, "one doc lead, one between the items: {out:?}");
        assert_eq!(out.last().unwrap(), "• two");
    }

    #[test]
    fn bullets_and_numbers_are_rendered_and_nested() {
        let out = plain(&doc("- one\n- two\n  - deep\n\n1. first\n2. second\n", 80));
        let joined = out.join("\n");
        assert!(joined.contains("• one"), "{joined}");
        assert!(joined.contains("  ◦ deep"), "the depth is in the glyph: {joined}");
        assert!(joined.contains("1. first"), "{joined}");
        assert!(joined.contains("2. second"), "{joined}");
    }

    #[test]
    fn blockquotes_get_a_bar() {
        let out = plain(&doc("> \u{26a0}\u{fe0f} competing hypothesis\n", 80));
        assert!(out.iter().any(|l| l.starts_with('▌')), "{out:?}");
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
        assert_eq!(d.citations, vec!["burke_2023"]);
        assert!(text.contains("[1]"), "{text:?}");
        assert!(!text.contains("burke_2023"), "the label is not read mid-sentence: {text:?}");
        // The citation itself is a `Footnote`-kind `DocLink` (so it can be
        // clicked); the definition block's `[nutrition/x/raw.md](../x/raw.md)`
        // must not additionally turn into a real, followable link.
        assert_eq!(d.links.len(), 1, "a footnote definition is not a link: {:?}", d.links);
        assert_eq!(d.links[0].kind, LinkKind::Footnote);
        assert_eq!(d.links[0].target, "burke_2023");
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
    fn footnote_references_are_numbered_in_order_of_citation() {
        let d = doc("Body[^smith2024] and[^jones2020] and again[^smith2024].\n", 80);
        let out = plain(&d).join(" ");
        assert!(out.contains("Body[1]"), "{out}");
        assert!(out.contains("and[2]"), "{out}");
        assert!(out.contains("again[1]"), "a label keeps its number: {out}");
        assert_eq!(d.citations, vec!["smith2024", "jones2020"]);
        assert_eq!(d.citation_number("jones2020"), Some(2));
        assert_eq!(d.citation_number("nobody"), None);
    }

    #[test]
    fn selecting_a_source_highlights_the_corresponding_mark_in_the_body() {
        let d = doc("Body[^smith2024] and[^jones2020].\n", 80);
        let link_idx = d.citation_link("smith2024");
        assert_eq!(link_idx, Some(0), "the first footnote is index 0");
        let theme = Theme::default();
        let lines = d.to_lines(&theme, link_idx, None, 0, 40);
        let active = lines.iter().flat_map(|l| &l.spans).any(|s| {
            s.content.contains("[1]")
                && s.style.bg == Some(theme.link)
                && s.style.add_modifier.contains(Modifier::BOLD)
        });
        assert!(active, "[1] must carry the active-link highlight");
    }

    #[test]
    fn tables_render_aligned_with_a_rule() {
        let out = plain(&doc("| a | bbbb |\n|---|------|\n| 1 | 2 |\n", 80));
        let joined = out.join("\n");
        assert!(joined.contains('│'), "{joined}");
        assert!(joined.contains('┼'), "{joined}");
    }

    #[test]
    fn the_table_header_row_is_not_dropped() {
        // pulldown puts the header's cells directly inside TableHead, with no
        // TableRow around them; forgetting that loses the header and promotes
        // the first body row in its place.
        let out = plain(&doc("| Modality | Evidence |\n|---|---|\n| Splints | Strong |\n", 80));
        assert!(out[0].is_empty(), "the doc leads with a blank row: {out:?}");
        assert!(out[1].starts_with('┌'), "the box is closed: {out:?}");
        assert!(out[2].contains("Modality") && out[2].contains("Evidence"), "{out:?}");
        assert!(out[3].contains('┼'), "the rule sits under the header: {out:?}");
        assert!(out[4].contains("Splints"), "{out:?}");
        assert!(out[5].starts_with('└'), "{out:?}");
    }

    #[test]
    fn a_grid_renders_like_a_table_with_per_row_source_mapping() {
        let grid = vec![
            vec!["symbol".to_string(), "price".to_string()],
            vec!["AAPL".to_string(), "232.1".to_string()],
            vec!["MSFT".to_string(), "417.4".to_string()],
        ];
        let d = render_grid(&grid, 80, &Theme::default());
        let out = plain(&d);
        assert!(out[1].starts_with('┌'), "{out:?}");
        assert!(out[2].contains("symbol") && out[2].contains("price"), "{out:?}");
        assert!(out[4].contains("AAPL") && out[5].contains("MSFT"), "{out:?}");

        // Each content line maps back to the grid row it draws, so the editor
        // cursor lands on the same raw CSV line it was reading.
        let header_line = out.iter().position(|l| l.contains("symbol")).unwrap();
        let msft_line = out.iter().position(|l| l.contains("MSFT")).unwrap();
        assert_eq!(d.lines[header_line].src_line, 0);
        assert_eq!(d.lines[msft_line].src_line, 2);
    }

    #[test]
    fn a_mermaid_block_is_drawn_not_printed() {
        let out = plain(&doc("```mermaid\nflowchart TD\n  A[Root] --> B[Leaf]\n```\n", 80)).join("\n");
        assert!(out.contains("Root") && out.contains("Leaf"));
        assert!(out.contains('▼'), "arrows, not source: {out}");
        assert!(!out.contains("flowchart TD"), "the source is not shown: {out}");
        assert!(!out.contains("-->"), "{out}");
    }

    #[test]
    fn an_undrawable_mermaid_block_falls_back_to_its_source() {
        let out = plain(&doc("```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n", 80)).join("\n");
        assert!(out.contains("sequenceDiagram"), "{out}");
        assert!(out.contains("A->>B: hi"), "{out}");
    }

    #[test]
    fn a_mermaid_diagram_respects_the_pane_width() {
        let code = "```mermaid\nflowchart TD\n  A[Root] --> B[A child with a fairly long label]\n  A --> C[Another child with a long label]\n```\n";
        for width in [24u16, 40, 80] {
            for line in plain(&doc(code, width)) {
                assert!(line.width() <= width as usize, "width {width}: {line:?}");
            }
        }
    }

    #[test]
    fn code_blocks_keep_their_lines_verbatim() {
        let out = plain(&doc("```python\nx = 1\ny = 2\n```\n", 80));
        assert!(out.iter().any(|l| l.contains("x = 1")), "{out:?}");
        assert!(out.iter().any(|l| l.contains("y = 2")), "{out:?}");
        assert!(out.iter().any(|l| l.contains("python")), "the chip names the language: {out:?}");
        // Every band row is the same width, gutter included, or the slab has a
        // ragged edge.
        let band: Vec<usize> = out.iter().filter(|l| l.contains('▎')).map(|l| l.width()).collect();
        assert_eq!(band.len(), 3, "chip plus two lines: {out:?}");
        assert!(band.windows(2).all(|w| w[0] == w[1]), "{band:?}");
    }

    #[test]
    fn headings_are_collected_for_the_outline() {
        let d = doc("# One\n\ntext\n\n## Two\n\n#### Deep\n", 80);
        let titles: Vec<&str> = d.headings.iter().map(|(_, _, t)| t.as_str()).collect();
        assert_eq!(titles, vec!["One", "Two", "Deep"]);
        // The level is what lets the outline show the shape of the page.
        let levels: Vec<u8> = d.headings.iter().map(|(_, l, _)| *l).collect();
        assert_eq!(levels, vec![1, 2, 4]);
    }

    #[test]
    fn a_code_block_maps_each_line_back_to_its_own_source_line() {
        // Line 0 is the paragraph, 2 the fence, 3 and 4 the code.
        let d = doc("intro\n\n```python\nx = 1\ny = 2\n```\n", 40);
        let at = |needle: &str| {
            d.lines.iter().position(|l| l.plain_text().contains(needle)).unwrap()
        };
        assert_eq!(d.source_for_line(at("x = 1")), 3);
        assert_eq!(d.source_for_line(at("y = 2")), 4);
    }

    #[test]
    fn a_citation_inside_a_table_cell_is_numbered_too() {
        let d = doc("a[^first]\n\n| x |\n|---|\n| see[^second] |\n", 40);
        assert_eq!(d.citations, vec!["first", "second"]);
        assert!(plain(&d).iter().any(|l| l.contains("see[2]")), "{:?}", plain(&d));
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
        assert_eq!(cited, vec!["[1]", "[2]"]);
    }

    #[test]
    fn a_lone_bracket_caret_is_left_alone() {
        let out = plain(&doc("array[^ ] and [^] here\n", 80)).join(" ");
        assert!(out.contains("[^"), "{out}");
    }

    #[test]
    fn empty_input_renders_nothing_and_does_not_panic() {
        let empty = doc("", 80);
        assert_eq!(empty.lines.len(), 1, "just the leading blank");
        assert!(doc("", 80).lines.iter().all(|l| l.segs.is_empty()));
        assert!(doc("\n\n\n", 80).lines.iter().all(|l| l.segs.is_empty()));
    }

    #[test]
    fn a_very_narrow_viewport_still_produces_output() {
        let d = doc("some prose that must fit somewhere", 1);
        assert!(!d.lines.is_empty());
    }

    #[test]
    fn text_selection_extracts_substring_and_multiline() {
        let d = doc("First line of text\n\nSecond line of text\n", 80);
        // Line 0 is the doc's leading blank, so the prose starts at line 1.
        let sel1 = Selection::new(TextPos { line: 1, col: 6 }, TextPos { line: 1, col: 10 });
        assert_eq!(d.selected_text(sel1), "line");

        // Select backward (anchor after cursor)
        let sel1_back = Selection::new(TextPos { line: 1, col: 10 }, TextPos { line: 1, col: 6 });
        assert_eq!(d.selected_text(sel1_back), "line");

        // Multiline selection
        let sel2 = Selection::new(TextPos { line: 1, col: 14 }, TextPos { line: 3, col: 6 });
        let text = d.selected_text(sel2);
        assert_eq!(text, "text\n\nSecond");
    }

    #[test]
    fn to_lines_highlights_selected_range() {
        let theme = Theme::default();
        let d = doc("Alpha **Beta** Gamma", 80);
        let sel = Selection::new(TextPos { line: 1, col: 3 }, TextPos { line: 1, col: 10 });
        let lines = d.to_lines(&theme, None, Some(sel), 1, 1);
        assert_eq!(lines.len(), 1);
        let line = &lines[0];
        // Check that some spans have the selection style (accent background)
        let has_sel_span = line.spans.iter().any(|s| s.style.bg == Some(theme.accent));
        assert!(has_sel_span, "selected range must have theme.accent background");
    }

    #[test]
    fn plain_text_extracts_entire_rendered_body() {
        let d = doc("First\n\nSecond", 80);
        assert!(d.plain_text().contains("First"));
        assert!(d.plain_text().contains("Second"));
    }

    #[test]
    fn inline_math_renders_known_commands_and_hides_the_delimiters() {
        let d = doc("## 1. The Climax $\\rightarrow$ Calm Pill\n", 80);
        let out = plain(&d).join("\n");
        assert!(out.contains("The Climax → Calm Pill"), "{out}");
        assert!(!out.contains('$'), "delimiters are syntax: {out}");
        assert!(!out.contains("\\rightarrow"), "{out}");
        assert!(!out.contains('\\'), "{out}");
    }

    #[test]
    fn inline_math_is_styled_like_math_not_body_text() {
        let theme = Theme::default();
        let d = render("eat $\\alpha\\beta$ more\n", 80, &theme);
        let span = d
            .lines
            .iter()
            .flat_map(|l| l.segs.iter())
            .find(|s| s.text == "αβ")
            .expect("the greek makes one run");
        assert_eq!(span.style.fg, Some(theme.literal));
        assert!(span.style.add_modifier.contains(Modifier::ITALIC));
    }

    #[test]
    fn math_translates_commands_lists_fractions_and_fonts() {
        assert_eq!(tex_to_unicode(r"\frac{a}{b}"), "a/b");
        assert_eq!(tex_to_unicode(r"x^{\frac{1}{2}}"), "x^1/2");
        assert_eq!(tex_to_unicode(r"\text{Climax} \to \mathbb{R}"), "Climax → R");
        assert_eq!(tex_to_unicode(r"\left(x\right)^{2} \leq \alpha"), "(x)^2 ≤ α");
    }

    #[test]
    fn unknown_math_keeps_its_source_verbatim() {
        let out = plain(&doc("a $\\frobnicate{y}$ z\n", 80)).join(" ");
        assert!(out.contains("\\frobnicate"), "{out}");
    }

    #[test]
    fn a_priced_dollar_is_not_treated_as_math() {
        // `$` only opens a span when followed by non-whitespace and needs a
        // closing `$` that is not preceded by whitespace, so currency survives.
        let out = plain(&doc("It costs $40 to $50 on a good day.\n", 80)).join(" ");
        assert!(out.contains("$40 to $50"), "{out}");
        assert_eq!(plain(&doc("It costs $5.\n", 80)).join(" ").trim(), "It costs $5.");
    }

    #[test]
    fn display_math_is_a_centered_block() {
        let d = doc("before\n\n$$\\frac{a}{b} \\rightarrow c$$\n\nafter\n", 60);
        let out = plain(&d);
        let line = out.iter().find(|l| l.trim() == "a/b → c").expect("the formula renders");
        let lead = line.width() - line.trim().width();
        assert_eq!(lead, (60 - line.trim().width()) / 2, "centered in the pane: {out:?}");
        assert!(out.iter().any(|l| l.trim() == "before"), "prose before: {out:?}");
        assert!(out.iter().any(|l| l.trim() == "after"), "prose after: {out:?}");
    }

    #[test]
    fn over_wide_display_math_is_cut_not_broken() {
        for width in [20u16, 40] {
            let d = doc("$$\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2} \\text{ really quite long}$$\n", width);
            for line in plain(&d) {
                assert!(line.width() <= width as usize, "width {width}: {line:?}");
            }
        }
    }
}






