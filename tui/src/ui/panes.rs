//! The three panes and the status line.

use edtui::{EditorMode, EditorStatusLine, EditorTheme, EditorView, LineNumbers};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;
use tui_term::widget::PseudoTerminal;

use crate::app::{App, Focus, Open};
use crate::theme::Theme;
use crate::vault::index::Severity;

/// A block in the app's chrome: rounded, titled, dim unless focused.
fn pane(theme: &Theme, title: &str, focused: bool) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(theme.border(focused))
        .title(Span::styled(format!(" {title} "), theme.title(focused)))
        .style(Style::default().bg(theme.bg))
}

// ------------------------------------------------------------------- tree

pub fn tree(frame: &mut Frame, app: &App, area: Rect) {
    let focused = app.focus == Focus::Tree;
    let block = pane(&app.theme, "tree", focused);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        return;
    }

    let height = inner.height as usize;
    let offset = scroll_offset(app.tree.selected, app.tree.rows.len(), height);
    let open_path = app.open.as_ref().map(|o| o.page.path.clone());

    let lines: Vec<Line> = app
        .tree
        .rows
        .iter()
        .enumerate()
        .skip(offset)
        .take(height)
        .map(|(i, row)| {
            let selected = i == app.tree.selected;
            let is_open = open_path.as_deref() == Some(row.path.as_path());

            let marker = if row.is_dir {
                if row.expanded {
                    "▾ "
                } else {
                    "▸ "
                }
            } else {
                "  "
            };

            let mut style = if row.is_collection {
                Style::default().fg(app.theme.accent).add_modifier(Modifier::BOLD)
            } else if row.is_dir {
                Style::default().fg(app.theme.text)
            } else {
                Style::default().fg(app.theme.subtext)
            };
            if is_open {
                style = style.fg(app.theme.text).add_modifier(Modifier::BOLD);
            }
            if selected {
                style = app.theme.selection(focused);
            }

            let badge = app
                .index
                .get(&row.rel)
                .map(|e| e.worst())
                .filter(|_| !row.is_dir)
                .or_else(|| app.index.dir_findings.contains_key(&row.rel).then_some(Severity::Warn));

            let mut spans = vec![
                Span::styled(" ".repeat(row.depth), style),
                Span::styled(marker, style),
                Span::styled(row.label.clone(), style),
            ];
            if let Some(severity) = badge {
                let (mark, colour) = match severity {
                    Severity::Clean => ("", app.theme.ok),
                    Severity::Warn => (" ⚠", app.theme.warn),
                    Severity::Error => (" ●", app.theme.err),
                };
                if !mark.is_empty() {
                    spans.push(Span::styled(mark, Style::default().fg(colour)));
                }
            }
            Line::from(spans)
        })
        .collect();

    frame.render_widget(Paragraph::new(lines), inner);
}

/// The tree's current scroll offset. Shared with the mouse handler so a click
/// lands on the row the user actually sees.
pub fn tree_offset(app: &App) -> usize {
    let height = app.areas.tree.height.saturating_sub(2) as usize;
    scroll_offset(app.tree.selected, app.tree.rows.len(), height)
}

fn scroll_offset(selected: usize, total: usize, height: usize) -> usize {
    if total <= height {
        return 0;
    }
    // Keep the cursor a third of the way down where possible: context above,
    // room to read below.
    selected.saturating_sub(height / 3).min(total - height)
}

// --------------------------------------------------------------- document

pub fn document(frame: &mut Frame, app: &mut App, area: Rect) {
    let Some(open) = app.open.as_ref() else {
        return empty_document(frame, app, area);
    };

    let show_inspector = app.show_inspector && !open.editing() && area.height > 14;
    let [main, inspect] = if show_inspector {
        let h = inspector_height(open, area.height);
        Layout::vertical([Constraint::Min(5), Constraint::Length(h)]).areas(area)
    } else {
        [area, Rect::ZERO]
    };

    if open.editing() {
        editor(frame, app, main);
    } else {
        reader(frame, app, main);
    }
    if !inspect.is_empty() {
        inspector(frame, app, inspect);
    }
}

fn empty_document(frame: &mut Frame, app: &App, area: Rect) {
    let block = pane(&app.theme, "podarcis", app.focus == Focus::Doc);
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let hint = vec![
        Line::from(""),
        Line::from(Span::styled(
            "  Nothing open yet.",
            Style::default().fg(app.theme.text).add_modifier(Modifier::BOLD),
        )),
        Line::from(""),
        Line::from(Span::styled("  ctrl+f   find a page", app.theme.dim())),
        Line::from(Span::styled("  /        search text", app.theme.dim())),
        Line::from(Span::styled("  ctrl+p   command palette", app.theme.dim())),
        Line::from(Span::styled("  ?        every key", app.theme.dim())),
    ];
    frame.render_widget(Paragraph::new(hint), inner);
}

fn reader(frame: &mut Frame, app: &App, area: Rect) {
    let open = app.open.as_ref().unwrap();
    let focused = app.focus == Focus::Doc;
    let title = crate::vault::page::rel_path(&open.page.path, &app.cfg.root);
    let block = pane(&app.theme, &title, focused).padding(ratatui::widgets::Padding::horizontal(1));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        return;
    }

    let [header, body] = Layout::vertical([Constraint::Length(2), Constraint::Min(1)]).areas(inner);
    frame.render_widget(Paragraph::new(header_lines(app, open)), header);

    let lines = open.doc.to_lines(&app.theme, open.link, open.scroll, body.height as usize);
    frame.render_widget(Paragraph::new(lines), body);

    scrollbar(frame, app, area, open.scroll, open.doc.height(), body.height as usize);
}

/// Frontmatter rendered as a typed header rather than raw YAML.
fn header_lines(app: &App, open: &Open) -> Vec<Line<'static>> {
    let okf = &open.page.okf;
    let mut facts: Vec<Span> = Vec::new();
    let push = |text: String, style: Style, facts: &mut Vec<Span>| {
        if !facts.is_empty() {
            facts.push(Span::styled(" · ", app.theme.faint_style()));
        }
        facts.push(Span::styled(text, style));
    };

    if let Some(kind) = &okf.kind {
        let known = crate::vault::page::KNOWN_TYPES.contains(&kind.as_str());
        let style = if known { app.theme.dim() } else { Style::default().fg(app.theme.err) };
        push(kind.clone(), style, &mut facts);
    }
    if let Some(status) = &okf.status {
        push(status.clone(), Style::default().fg(app.theme.status_of(status)), &mut facts);
    }
    if let Some(category) = &okf.category {
        push(category.clone(), app.theme.dim(), &mut facts);
    }
    let words = open.page.words;
    let over = open.page.over_word_limit();
    push(
        format!("{words} w"),
        if over { Style::default().fg(app.theme.warn) } else { app.theme.faint_style() },
        &mut facts,
    );
    if let Some(model) = &okf.generated.model {
        push(model.clone(), app.theme.faint_style(), &mut facts);
    }
    if facts.is_empty() {
        facts.push(Span::styled("no frontmatter", Style::default().fg(app.theme.err)));
    }

    vec![
        Line::from(Span::styled(
            open.page.title().to_string(),
            Style::default().fg(app.theme.accent).add_modifier(Modifier::BOLD),
        )),
        Line::from(facts),
    ]
}

fn scrollbar(frame: &mut Frame, app: &App, area: Rect, scroll: usize, total: usize, height: usize) {
    if total <= height || area.height < 4 || area.width < 2 {
        return;
    }
    let track = area.height.saturating_sub(2) as usize;
    let span = ((height * track) / total).max(1);
    let top = (scroll * track) / total;
    let x = area.x + area.width - 1;
    // The scrollbar lives *in* the right border, so the track has to keep
    // drawing a border glyph — a blank would punch a hole in the frame.
    for i in 0..track {
        let filled = i >= top && i < top + span;
        let (cell, style) = if filled {
            ("┃", Style::default().fg(app.theme.accent))
        } else {
            ("│", app.theme.border(false))
        };
        frame.render_widget(
            Paragraph::new(Span::styled(cell, style)),
            Rect::new(x, area.y + 1 + i as u16, 1, 1),
        );
    }
}

fn editor(frame: &mut Frame, app: &mut App, area: Rect) {
    let theme = app.theme;
    let root = app.cfg.root.clone();
    let Some(open) = app.open.as_mut() else { return };
    let title = crate::vault::page::rel_path(&open.page.path, &root);
    let dirty = open.editor.as_ref().is_some_and(crate::editor::Editor::dirty);
    let label = if dirty { format!("{title}  ⏺") } else { title };
    let block = pane(&theme, &label, true);
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let Some(ed) = open.editor.as_mut() else { return };
    let editor_theme = EditorTheme::default()
        .base(Style::default().fg(theme.text).bg(theme.bg))
        .cursor_style(Style::default().fg(theme.bg).bg(theme.accent))
        .selection_style(Style::default().fg(theme.bg).bg(theme.link))
        .line_numbers_style(Style::default().fg(theme.faint))
        .status_line(
            EditorStatusLine::default()
                .style_mode(Style::default().fg(theme.bg).bg(theme.accent).add_modifier(Modifier::BOLD))
                .style_line(Style::default().fg(theme.subtext).bg(theme.surface)),
        );
    frame.render_widget(
        EditorView::new(&mut ed.state)
            .theme(editor_theme)
            .wrap(true)
            .line_numbers(LineNumbers::Absolute),
        inner,
    );

    completion_popup(frame, app, inner);
}

fn completion_popup(frame: &mut Frame, app: &App, area: Rect) {
    let Some(editor) = app.open.as_ref().and_then(|o| o.editor.as_ref()) else { return };
    let Some(completion) = editor.completion.as_ref() else { return };

    let rows = completion.candidates.len().min(7) as u16;
    let width = completion
        .candidates
        .iter()
        .map(|c| c.value.len() + c.note.len() + 6)
        .max()
        .unwrap_or(20)
        .clamp(24, area.width.saturating_sub(4).max(24) as usize) as u16;

    let cursor_row = editor.state.cursor.row;
    let offset = editor.state.viewport_offset().1;
    let y = area.y + (cursor_row.saturating_sub(offset) as u16).min(area.height.saturating_sub(1)) + 1;
    // Flip above the cursor when there is no room below.
    let y = if y + rows + 2 > area.y + area.height { y.saturating_sub(rows + 3) } else { y };
    let x = (area.x + 6).min(area.x + area.width.saturating_sub(width));
    let popup = Rect { x, y, width, height: rows + 2 }.intersection(area);
    if popup.is_empty() {
        return;
    }

    let label = match &completion.what {
        crate::editor::Completing::Footnote { .. } => "sources",
        crate::editor::Completing::Link { .. } => "pages",
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(app.theme.link))
        .title(Span::styled(format!(" {label} "), Style::default().fg(app.theme.link)))
        .style(Style::default().bg(app.theme.overlay));
    let inner = block.inner(popup);
    frame.render_widget(Clear, popup);
    frame.render_widget(block, popup);

    let start = scroll_offset(completion.selected, completion.candidates.len(), inner.height as usize);
    let lines: Vec<Line> = completion
        .candidates
        .iter()
        .enumerate()
        .skip(start)
        .take(inner.height as usize)
        .map(|(i, candidate)| {
            let selected = i == completion.selected;
            let style = if selected {
                app.theme.selection(true)
            } else {
                Style::default().fg(app.theme.text)
            };
            Line::from(vec![
                Span::styled(format!(" {} ", candidate.value), style),
                Span::styled(candidate.note.clone(), app.theme.faint_style()),
            ])
        })
        .collect();
    frame.render_widget(Paragraph::new(lines), inner);
}

// -------------------------------------------------------------- inspector

fn inspector_height(open: &Open, available: u16) -> u16 {
    let citations = open.page.okf.sources.len() + open.page.footnote_definitions().len();
    let rows = 2 + citations.min(4) + usize::from(!open.page.okf.related.is_empty());
    (rows as u16 + 2).min(available / 2).max(5)
}

fn inspector(frame: &mut Frame, app: &App, area: Rect) {
    let open = app.open.as_ref().unwrap();
    let rel = crate::vault::page::rel_path(&open.page.path, &app.cfg.root);
    let entry = app.index.by_path(&open.page.path);
    let backlinks = app.index.backlinks(&rel);

    let title = format!(
        "sources {} · backlinks {} · findings {}",
        open.page.okf.sources.len() + open.page.footnote_definitions().len(),
        backlinks.len(),
        entry.map(|e| e.findings.len()).unwrap_or(0)
    );
    let block = pane(&app.theme, &title, false).padding(ratatui::widgets::Padding::horizontal(1));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        return;
    }

    let mut lines: Vec<Line> = Vec::new();

    // The `rationale` is the OKF justification for the page existing, so it is
    // the first thing worth reading about it.
    if let Some(rationale) = open.page.okf.rationale.as_deref().or(open.page.okf.description.as_deref()) {
        lines.push(Line::from(Span::styled(rationale.to_string(), app.theme.dim())));
    }

    if let Some(entry) = entry {
        for finding in entry.findings.iter().take(4) {
            lines.push(Line::from(vec![
                Span::styled("⚠ ", Style::default().fg(app.theme.warn)),
                Span::styled(finding.code, Style::default().fg(app.theme.err)),
                Span::styled(format!("  {}", finding.detail), app.theme.dim()),
            ]));
        }
    }

    // A citation may live in the frontmatter `sources:` list or as an in-body
    // `[^id]:` definition. The linter accepts both, so the inspector shows them
    // as one list rather than making the reader know which style a page used.
    let cited = open.page.footnote_refs();
    let mut citations: Vec<(String, String)> = open
        .page
        .okf
        .sources
        .iter()
        .map(|source| {
            let mut label = source.title.clone().unwrap_or_default();
            if let Some(author) = &source.author {
                label = if label.is_empty() { author.clone() } else { format!("{author} — {label}") };
            }
            (source.id.clone(), label)
        })
        .collect();
    for (id, text) in open.page.footnote_definitions() {
        if !citations.iter().any(|(known, _)| *known == id) {
            citations.push((id, text));
        }
    }

    for (id, label) in citations.iter().take(6) {
        let used = cited.contains(id);
        let mark = if used { "✓" } else { "·" };
        let colour = if used { app.theme.ok } else { app.theme.faint };
        lines.push(Line::from(vec![
            Span::styled(format!("{mark} "), Style::default().fg(colour)),
            Span::styled(id.clone(), Style::default().fg(app.theme.literal)),
            Span::styled(format!("  {label}"), app.theme.faint_style()),
        ]));
    }

    if !backlinks.is_empty() {
        let names: Vec<String> = backlinks.iter().take(6).map(|e| e.title.clone()).collect();
        lines.push(Line::from(vec![
            Span::styled("← ", Style::default().fg(app.theme.link)),
            Span::styled(names.join(", "), app.theme.dim()),
        ]));
    }

    if lines.is_empty() {
        lines.push(Line::from(Span::styled(
            "no sources, no backlinks, nothing to fix",
            app.theme.faint_style(),
        )));
    }

    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), inner);
}

// ---------------------------------------------------------------- sidebar

pub fn sidebar(frame: &mut Frame, app: &App, area: Rect) {
    let focused = app.focus == Focus::Sidebar;
    let title = if focused { "herdr — f12 to leave" } else { "herdr" };
    let block = pane(&app.theme, title, focused);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        return;
    }

    if let Some(pane) = app.sidebar.as_ref() {
        let drawn = pane.with_screen(|screen| {
            let widget = PseudoTerminal::new(screen);
            frame.render_widget(widget, inner);
        });
        if drawn.is_some() {
            return;
        }
    }

    let message = app
        .sidebar_error
        .clone()
        .unwrap_or_else(|| "starting herdr…".to_string());
    let lines = vec![
        Line::from(""),
        Line::from(Span::styled(format!(" {message}"), Style::default().fg(app.theme.warn))),
        Line::from(""),
        Line::from(Span::styled(" ctrl+g hides this pane", app.theme.faint_style())),
    ];
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

// ----------------------------------------------------------------- status

pub fn status(frame: &mut Frame, app: &App, area: Rect) {
    let theme = &app.theme;
    let mut left: Vec<Span> = Vec::new();

    let (mode, mode_style) = match app.open.as_ref().and_then(|o| o.editor.as_ref()).map(|e| e.mode()) {
        Some(EditorMode::Insert) => ("INSERT", Style::default().fg(theme.bg).bg(theme.ok)),
        Some(EditorMode::Visual) => ("VISUAL", Style::default().fg(theme.bg).bg(theme.literal)),
        Some(EditorMode::Search) => ("SEARCH", Style::default().fg(theme.bg).bg(theme.link)),
        Some(_) => ("NORMAL", Style::default().fg(theme.bg).bg(theme.accent)),
        None => ("READ", Style::default().fg(theme.bg).bg(theme.accent)),
    };
    left.push(Span::styled(format!(" {mode} "), mode_style.add_modifier(Modifier::BOLD)));

    if let Some(prefix) = app.pending {
        left.push(Span::styled(format!(" {prefix}…"), Style::default().fg(theme.literal)));
    }

    if app.indexing {
        left.push(Span::styled("  indexing…", theme.dim()));
    } else {
        let findings = app.index.finding_count();
        let (mark, style) = if findings == 0 {
            ("✓ clean", Style::default().fg(theme.ok))
        } else {
            ("⚠", Style::default().fg(theme.warn))
        };
        left.push(Span::styled(format!("  {mark}"), style));
        if findings > 0 {
            left.push(Span::styled(format!(" {findings} findings"), theme.dim()));
        }
        left.push(Span::styled(format!("  {} pages", app.index.entries.len()), theme.faint_style()));
    }

    for job in app.jobs.labels() {
        left.push(Span::styled(format!("  ◐ {job}"), Style::default().fg(theme.link)));
    }

    if let Some(editor) = app.open.as_ref().and_then(|o| o.editor.as_ref()) {
        left.push(Span::styled(format!("  ln {}", editor.cursor_line() + 1), theme.faint_style()));
    }

    let mut right = vec![
        Span::styled("ctrl+p ", theme.faint_style()),
        Span::styled("palette   ", theme.dim()),
        Span::styled("? ", theme.faint_style()),
        Span::styled("keys   ", theme.dim()),
    ];
    if let Some(version) = app.engine_version.as_deref() {
        right.push(Span::styled(format!("v{version}  "), theme.faint_style()));
    }
    right.push(Span::styled(
        format!("catppuccin {} ", theme.flavor.as_str()),
        theme.faint_style(),
    ));

    let bar = Style::default().bg(theme.surface).fg(theme.text);
    frame.render_widget(Block::default().style(bar), area);
    frame.render_widget(Paragraph::new(Line::from(left)).style(bar), area);
    let right_line = Line::from(right).right_aligned();
    frame.render_widget(Paragraph::new(right_line).style(bar), area);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_cursor_stays_a_third_down_once_the_list_scrolls() {
        assert_eq!(scroll_offset(0, 100, 30), 0);
        assert_eq!(scroll_offset(5, 100, 30), 0, "no scroll until the cursor passes the line");
        assert_eq!(scroll_offset(40, 100, 30), 30);
        assert_eq!(scroll_offset(99, 100, 30), 70, "clamped at the end");
        assert_eq!(scroll_offset(5, 10, 30), 0, "a short list never scrolls");
    }

    #[test]
    fn the_inspector_never_takes_more_than_half_the_pane() {
        let dir = std::env::temp_dir().join(format!("podarcis-panes-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        let raw = format!(
            "---\ntitle: T\ntype: concept\ncategory: c\nrationale: r\nsources:\n{}---\nbody\n",
            (0..20).map(|i| format!("  - id: s{i}\n")).collect::<String>()
        );
        let path = dir.join("wiki/a.md");
        std::fs::write(&path, raw).unwrap();
        let page = crate::vault::page::Page::load(&path, &dir).unwrap();
        let open = Open {
            doc: crate::ui::markdown::render(&page.body, 40, &Theme::default()),
            page,
            doc_width: 40,
            scroll: 0,
            link: None,
            editor: None,
        };
        assert!(inspector_height(&open, 40) <= 20);
        assert!(inspector_height(&open, 12) >= 5);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
