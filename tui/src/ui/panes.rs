//! The three panes and the status line.

use edtui::{EditorStatusLine, EditorTheme, EditorView, LineNumbers};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;
use tui_term::widget::PseudoTerminal;

use crate::app::{App, Focus, Open, TreePane};
use crate::theme::Theme;
use crate::vault::git::GitStatus;
use crate::vault::index::Severity;

/// A block in the app's chrome: rounded, titled, dim unless focused.
fn pane(theme: &Theme, title: &str, focused: bool) -> Block<'static> {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(theme.border(focused))
        .style(Style::default().bg(theme.bg));
    if title.is_empty() {
        block
    } else {
        block.title(Span::styled(format!(" {title} "), theme.title(focused)))
    }
}

// ------------------------------------------------------------------- tree

pub fn tree(frame: &mut Frame, app: &mut App, area: Rect) {
    let n = app.tree.collection_paths().len().max(1);
    let constraints: Vec<Constraint> = (0..n).map(|_| Constraint::Fill(1)).collect();
    let chunks = Layout::vertical(constraints).split(area);

    let focused = app.focus == Focus::Tree;
    let open_path = app.open.as_ref().map(|o| o.page.path.clone());
    let selected = app.tree.selected;

    let mut panes = Vec::with_capacity(n);
    for i in 0..app.tree.collection_paths().len() {
        let Some((header, end)) = app.tree.section_span(i) else {
            continue;
        };
        let chunk = chunks[i];
        let row = &app.tree.rows[header];
        let track = app.track_for(&row.path);
        let section_focus = focused && selected >= header && selected < end;

        let git_style = if track.tracked() {
            Style::default().fg(app.theme.ok).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(app.theme.warn)
        };
        let title = Line::from(vec![
            Span::styled(format!(" {} ", row.label), app.theme.title(section_focus)),
            Span::styled(
                format!(" {} ", track.label()),
                git_style,
            ),
        ]);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(app.theme.border(section_focus))
            .title(title)
            .style(Style::default().bg(app.theme.bg));
        let inner = block.inner(chunk);
        frame.render_widget(block, chunk);

        let start = header + 1;
        let height = inner.height as usize;
        let local_selected = if selected >= start && selected < end {
            selected - start
        } else {
            0
        };
        let child_count = end.saturating_sub(start);
        let offset = if selected >= start && selected < end {
            scroll_offset(local_selected, child_count, height)
        } else {
            0
        };

        if !inner.is_empty() && start < end {
            let lines: Vec<Line> = app
                .tree
                .rows
                .iter()
                .enumerate()
                .skip(start + offset)
                .take(end.saturating_sub(start + offset).min(height))
                .map(|(i, row)| tree_line(app, i, row, selected, focused, open_path.as_deref()))
                .collect();
            frame.render_widget(Paragraph::new(lines), inner);
        }

        panes.push(TreePane {
            area: chunk,
            inner,
            header,
            start,
            end,
            offset,
        });
    }
    app.areas.tree_panes = panes;
}

fn tree_line<'a>(
    app: &'a App,
    i: usize,
    row: &'a crate::vault::tree::Row,
    selected: usize,
    focused: bool,
    open_path: Option<&std::path::Path>,
) -> Line<'a> {
    let is_open = open_path == Some(row.path.as_path());
    let marker = if row.is_dir {
        if row.expanded {
            "▾ "
        } else {
            "▸ "
        }
    } else {
        "  "
    };

    let mut style = if row.is_dir {
        Style::default().fg(app.theme.text)
    } else {
        Style::default().fg(app.theme.subtext)
    };
    if is_open {
        style = style.fg(app.theme.text).add_modifier(Modifier::BOLD);
    }
    if i == selected {
        style = app.theme.selection(focused);
    }

    let indent = row.depth.saturating_sub(1);
    let mut spans = vec![
        Span::styled(" ".repeat(indent), style),
        Span::styled(marker, style),
        Span::styled(row.label.clone(), style),
    ];

    if !row.is_dir && row.path.extension().and_then(|e| e.to_str()) == Some("csv") {
        spans.push(Span::styled("  csv", Style::default().fg(app.theme.accent)));
    }

    if let Some(status) = app.git.status_for(&row.path) {
        let colour = match status {
            GitStatus::Conflict => app.theme.err,
            GitStatus::Modified | GitStatus::Deleted => app.theme.warn,
            GitStatus::Added | GitStatus::Renamed => app.theme.ok,
            GitStatus::Untracked => app.theme.subtext,
        };
        spans.push(Span::styled(
            format!(" {}", status.glyph()),
            Style::default().fg(colour),
        ));
    }

    let badge = app
        .index
        .get(&row.rel)
        .map(|e| e.worst())
        .filter(|_| !row.is_dir)
        .or_else(|| app.index.dir_findings.contains_key(&row.rel).then_some(Severity::Warn));
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
    if app.open.is_none() {
        app.areas.doc_main = area;
        app.areas.doc_body = Rect::ZERO;
        app.areas.inspector = Rect::ZERO;
        app.areas.inspector_divider = None;
        app.areas.inspector_toggle = None;
        app.areas.nav_back = None;
        app.areas.nav_forward = None;
        empty_document(frame, app, area);
        return;
    }
    let min_main = 8u16;
    let min_inspect = 4u16;
    let (main, inspect) = if app.show_inspector && area.height >= min_main + min_inspect {
        let max_inspect = area.height.saturating_sub(min_main);
        let height = app.cfg.inspector_height.clamp(min_inspect, max_inspect);
        let [main, inspect] = Layout::vertical([Constraint::Min(min_main), Constraint::Length(height)]).areas(area);
        (main, inspect)
    } else {
        (area, Rect::default())
    };

    app.areas.doc_main = main;
    app.areas.inspector = inspect;
    app.areas.inspector_divider = (!inspect.is_empty()).then_some(inspect.y);
    // The collapse handle: on the divider row while the sources section is
    // open, on the document's own bottom border once it is gone — the same
    // reopen affordance the side panes use on their closed edges.
    app.areas.inspector_toggle = if main.is_empty() {
        None
    } else if !inspect.is_empty() {
        Some(Rect::new(main.x + main.width / 2, inspect.y, 1, 1))
    } else {
        Some(Rect::new(main.x + main.width / 2, main.y + main.height - 1, 1, 1))
    };

    let open = app.open.as_mut().unwrap();
    // 2 columns for borders, 2 for padding; capped so prose on an ultra-wide
    // pane doesn't run edge to edge.
    let width = main.width.saturating_sub(4).min(crate::ui::markdown::MAX_WIDTH);
    open.reflow(width, &app.theme);

    if open.editing() {
        app.areas.doc_body = Rect::ZERO;
        app.areas.nav_back = None;
        app.areas.nav_forward = None;
        // Side by side when there is room, and the editor alone when there is
        // not — a 40-column preview is worse than none.
        let preview = app.preview && main.width >= 90;
        if preview {
            let [left, right] =
                Layout::horizontal([Constraint::Percentage(55), Constraint::Percentage(45)]).areas(main);
            editor(frame, app, left);
            preview_pane(frame, app, right);
        } else {
            editor(frame, app, main);
        }
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

fn reader(frame: &mut Frame, app: &mut App, area: Rect) {
    let open = app.open.as_ref().unwrap();
    let focused = app.focus == Focus::Doc;

    let can_back = !app.back.is_empty();
    let can_forward = !app.forward.is_empty();

    let back_style = if can_back {
        app.theme.title(focused)
    } else {
        app.theme.faint_style()
    };
    let forward_style = if can_forward {
        app.theme.title(focused)
    } else {
        app.theme.faint_style()
    };

    let title = Line::from(vec![
        Span::styled(" ← ", back_style),
        Span::styled(" → ", forward_style),
    ]);

    if area.width >= 8 {
        app.areas.nav_back = Some(Rect::new(area.x + 1, area.y, 3, 1));
        app.areas.nav_forward = Some(Rect::new(area.x + 4, area.y, 3, 1));
    } else {
        app.areas.nav_back = None;
        app.areas.nav_forward = None;
    }

    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(app.theme.border(focused))
        .style(Style::default().bg(app.theme.bg))
        .padding(ratatui::widgets::Padding::horizontal(1))
        .title(title);

    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        app.areas.doc_body = Rect::ZERO;
        return;
    }
    app.areas.doc_body = inner;

    let lines = open.doc.to_lines(&app.theme, open.link, open.selection, open.scroll, inner.height as usize);
    frame.render_widget(Paragraph::new(lines), inner);

    scrollbar(frame, app, area, open.scroll, open.doc.height(), inner.height as usize);
}

fn scrollbar(frame: &mut Frame, app: &App, area: Rect, scroll: usize, total: usize, height: usize) {
    if total <= height || area.height < 4 || area.width < 2 {
        return;
    }
    let track = area.height.saturating_sub(2) as usize;
    let span = ((height * track) / total).max(1);
    let top = ((scroll * track) / total).min(track.saturating_sub(span));
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
    // Mirrors `EditorView::line_number_width` (`LineNumbers::Absolute`), so
    // wrapped-line cursor movement (`Editor::move_visual`) sees the same
    // content width the view itself wraps at.
    let gutter = (ed.state.lines.len().max(1).to_string().len() + 1) as u16;
    ed.render_width = inner.width.saturating_sub(gutter) as usize;
    ed.refresh_syntax(&theme);
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

/// The page as it will read, re-rendered from the editor's buffer on every
/// keystroke. This is what makes the editor a markdown editor rather than a
/// text box that happens to hold markdown.
fn preview_pane(frame: &mut Frame, app: &mut App, area: Rect) {
    let theme = app.theme;
    let block = pane(&theme, "preview", false).padding(ratatui::widgets::Padding::horizontal(1));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        return;
    }

    let Some(open) = app.open.as_mut() else { return };
    let Some(editor) = open.editor.as_ref() else { return };
    let text = editor.text();
    let cursor_line = editor.cursor_line();

    // A CSV's preview is its table, re-parsed from the editor buffer on every
    // keystroke, so a header you edit is a header you see change.
    if open.csv() {
        let doc = crate::ui::csv::Grid::parse(&text).to_doc(inner.width, &theme);
        let target = doc.line_for_source(cursor_line);
        let height = inner.height as usize;
        let scroll = target.saturating_sub(height / 3).min(doc.height().saturating_sub(height.min(doc.height())));
        frame.render_widget(Paragraph::new(doc.to_lines(&theme, None, None, scroll, height)), inner);
        return;
    }

    let body_start = text
        .lines()
        .position(|line| line.trim_end() == "---")
        .filter(|first| *first == 0)
        .and_then(|_| text.lines().skip(1).position(|line| line.trim_end() == "---").map(|i| i + 2))
        .unwrap_or(0);
    let body: String = text.lines().skip(body_start).collect::<Vec<_>>().join("\n");

    let doc = crate::ui::markdown::render(&body, inner.width.min(crate::ui::markdown::MAX_WIDTH), &theme);
    // Follow the cursor, so what you are typing is what you are looking at.
    let target = doc.line_for_source(cursor_line.saturating_sub(body_start));
    let height = inner.height as usize;
    let scroll = target.saturating_sub(height / 3).min(doc.height().saturating_sub(height.min(doc.height())));
    frame.render_widget(Paragraph::new(doc.to_lines(&theme, None, None, scroll, height)), inner);
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

#[allow(dead_code)]
fn inspector_height(open: &Open, available: u16) -> u16 {
    let citations = open.page.okf.sources.len() + open.page.footnote_definitions().len();
    let rows = 2 + citations.min(4) + usize::from(!open.page.okf.related.is_empty());
    (rows as u16 + 2).min(available / 2).max(5)
}

pub fn inspector_lines(
    open: &Open,
    index: &crate::vault::index::Index,
    root: &std::path::Path,
    theme: &Theme,
) -> Vec<Line<'static>> {
    inspector_rows(open, index, root, theme).into_iter().map(|(line, _)| line).collect()
}

/// A citation's display label, built from the source's own metadata (its
/// `resource` file's frontmatter — real author lists, not the abbreviated
/// `Author et al.` copy a wiki page keeps in its own `sources:` entry) and
/// falling back to that copy when the resource is missing or unreadable.
fn citation_label(source: &crate::vault::page::SourceRef, page_dir: &std::path::Path) -> String {
    let meta = source
        .resource
        .as_deref()
        .map(|r| crate::vault::links::normalize(&page_dir.join(r)))
        .and_then(|p| crate::vault::page::SourceMeta::load(&p))
        .filter(|m| !m.is_empty());

    let (title, authors, year) = match meta {
        Some(m) => (
            m.title.or_else(|| source.title.clone()),
            (!m.authors.is_empty()).then(|| m.authors.join(", ")).or_else(|| source.author.clone()),
            m.year.or_else(|| source.year.clone()),
        ),
        None => (source.title.clone(), source.author.clone(), source.year.clone()),
    };

    let mut label = authors.unwrap_or_default();
    if let Some(year) = year {
        label = if label.is_empty() { format!("({year})") } else { format!("{label} ({year})") };
    }
    if let Some(title) = title {
        label = if label.is_empty() { title } else { format!("{label} — {title}") };
    }
    label
}

/// Each inspector line paired with the source id it belongs to, if any — a
/// citation clicked in the body needs this to scroll to its row, and a click
/// on the row itself needs it to know what to open.
fn inspector_rows(
    open: &Open,
    index: &crate::vault::index::Index,
    root: &std::path::Path,
    theme: &Theme,
) -> Vec<(Line<'static>, Option<String>)> {
    let rel = crate::vault::page::rel_path(&open.page.path, root);
    let entry = index.by_path(&open.page.path);
    let backlinks = index.backlinks(&rel);
    let page_dir = open.page.path.parent().unwrap_or(root);

    let mut rows: Vec<(Line<'static>, Option<String>)> = Vec::new();

    if let Some(entry) = entry {
        for finding in &entry.findings {
            rows.push((
                Line::from(vec![
                    Span::styled("⚠ ", Style::default().fg(theme.warn)),
                    Span::styled(finding.code, Style::default().fg(theme.err)),
                    Span::styled(format!("  {}", finding.detail), theme.dim()),
                ]),
                None,
            ));
        }
    }

    let cited = open.page.footnote_refs();
    let defs = open.page.footnote_defs();

    // Candidates first, each tagged with the order its `[N]` is cited in the
    // body — numbered sources sort by that number, the uncited trail after.
    let mut candidates: Vec<(usize, String, Line<'static>)> = Vec::new();

    // A footnote definition carries the full citation the wiki wrote — prefer
    // it over the abbreviated `sources:` copy.
    for (id, text, _) in &defs {
        let used = cited.contains(id);
        let mark = match open.doc.citation_number(id) {
            Some(n) => format!("[{n}]"),
            None if used => "✓".to_string(),
            None => "·".to_string(),
        };
        let selected = open.selected_citation.as_deref() == Some(id.as_str());
        let order = open.doc.citation_number(id).unwrap_or(usize::MAX);
        let colour = if used { theme.ok } else { theme.faint };
        let id_style = if selected {
            Style::default().fg(theme.bg).bg(theme.accent).add_modifier(Modifier::BOLD)
        } else {
            theme.faint_style()
        };
        candidates.push((
            order,
            id.clone(),
            Line::from(vec![
                Span::styled(format!("{mark} "), Style::default().fg(colour)),
                Span::styled(text.clone(), Style::default().fg(theme.text)),
                Span::styled(format!("  {id}"), id_style),
            ]),
        ));
    }
    for source in &open.page.okf.sources {
        if defs.iter().any(|(id, _, _)| id == &source.id) {
            continue;
        }
        let label = citation_label(source, page_dir);
        let used = cited.contains(&source.id);
        let mark = match open.doc.citation_number(&source.id) {
            Some(n) => format!("[{n}]"),
            None if used => "✓".to_string(),
            None => "·".to_string(),
        };
        let selected = open.selected_citation.as_deref() == Some(source.id.as_str());
        let order = open.doc.citation_number(&source.id).unwrap_or(usize::MAX);
        let colour = if used { theme.ok } else { theme.faint };
        let id_style = if selected {
            Style::default().fg(theme.bg).bg(theme.accent).add_modifier(Modifier::BOLD)
        } else {
            theme.faint_style()
        };
        candidates.push((
            order,
            source.id.clone(),
            Line::from(vec![
                Span::styled(format!("{mark} "), Style::default().fg(colour)),
                Span::styled(label, Style::default().fg(theme.text)),
                Span::styled(format!("  {}", source.id), id_style),
            ]),
        ));
    }
    candidates.sort_by_key(|(order, _, _)| *order);
    for (_, id, line) in candidates {
        rows.push((line, Some(id)));
    }

    if !backlinks.is_empty() {
        let names: Vec<String> = backlinks.iter().map(|e| e.title.clone()).collect();
        rows.push((
            Line::from(vec![
                Span::styled("← ", Style::default().fg(theme.link)),
                Span::styled(names.join(", "), theme.dim()),
            ]),
            None,
        ));
    }

    if rows.is_empty() {
        rows.push((
            Line::from(Span::styled("no sources, no backlinks, nothing to fix", theme.faint_style())),
            None,
        ));
    }

    rows
}

fn inspector(frame: &mut Frame, app: &mut App, area: Rect) {
    let open = app.open.as_ref().unwrap();
    let rel = crate::vault::page::rel_path(&open.page.path, &app.cfg.root);
    let entry = app.index.by_path(&open.page.path);
    let backlinks = app.index.backlinks(&rel);

    let sources = open.page.okf.sources.len() + open.page.footnote_definitions().len();
    let findings = entry.map(|e| e.findings.len()).unwrap_or(0);
    // Same marks the pane body uses for each thing — a citation bracket, `←`
    // for backlinks, `⚠` for findings — so the title reads as a summary of
    // what's below rather than a second vocabulary to learn. A count of zero
    // means "nothing to see here", so the segment is dropped instead of
    // printed as `0`.
    let title = [
        (sources > 0).then(|| format!("[{sources}]")),
        (!backlinks.is_empty()).then(|| format!("← {}", backlinks.len())),
        (findings > 0).then(|| format!("⚠ {findings}")),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join("  ");
    let title = if title.is_empty() { "empty".to_string() } else { title };
    let block = pane(&app.theme, &title, false).padding(ratatui::widgets::Padding::horizontal(1));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    app.areas.inspector_body = inner;
    if inner.is_empty() {
        app.areas.inspector_rows.clear();
        return;
    }

    // Wrapped one raw row at a time (rather than via `wrap_lines` over the
    // whole batch) so each wrapped screen row keeps the citation id its raw
    // row carried — the mapping a click and a scroll-to-selection both need.
    let open = app.open.as_ref().unwrap();
    let raw_rows = inspector_rows(open, &app.index, &app.cfg.root, &app.theme);
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut ids: Vec<Option<String>> = Vec::new();
    for (line, id) in raw_rows {
        let wrapped = if id.is_some() {
            wrap_biblio(line, inner.width as usize)
        } else {
            wrap_line(line, inner.width as usize)
        };
        ids.extend(std::iter::repeat_n(id, wrapped.len()));
        lines.extend(wrapped);
    }

    let total = lines.len();
    let height = inner.height as usize;
    let max_scroll = total.saturating_sub(height);

    let open = app.open.as_mut().unwrap();
    if open.citation_scroll_pending {
        open.citation_scroll_pending = false;
        if let Some(row) = open
            .selected_citation
            .as_deref()
            .and_then(|sel| ids.iter().position(|id| id.as_deref() == Some(sel)))
        {
            if row < open.inspect_scroll || row >= open.inspect_scroll + height {
                open.inspect_scroll = row.saturating_sub(height / 2);
            }
        }
    }
    let scroll = if total <= height { 0 } else { open.inspect_scroll.min(max_scroll) };

    let visible: Vec<Line> = lines.into_iter().skip(scroll).take(height).collect();
    app.areas.inspector_rows = ids.into_iter().skip(scroll).take(height).collect();
    frame.render_widget(Paragraph::new(visible), inner);

    scrollbar(frame, app, area, scroll, total, height);
}

/// Wrap already-styled lines to `width` columns, one output `Line` per screen
/// row, breaking at whitespace where possible. Mirrors what
/// `Paragraph::wrap` would draw, except the caller gets the row count back
/// instead of it being a rendering-time secret.
pub fn wrap_lines(lines: Vec<Line<'static>>, width: usize) -> Vec<Line<'static>> {
    if width == 0 {
        return lines;
    }
    lines.into_iter().flat_map(|line| wrap_line(line, width)).collect()
}

fn wrap_line(line: Line<'static>, width: usize) -> Vec<Line<'static>> {
    use unicode_width::UnicodeWidthChar;

    let chars: Vec<(char, Style)> = line
        .spans
        .iter()
        .flat_map(|span| span.content.chars().map(move |c| (c, span.style)))
        .collect();
    if chars.is_empty() {
        return vec![Line::default()];
    }

    let mut rows: Vec<&[(char, Style)]> = Vec::new();
    let mut start = 0usize;
    while start < chars.len() {
        let mut col = 0usize;
        let mut end = start;
        let mut last_space = None;
        while end < chars.len() {
            let w = chars[end].0.width().unwrap_or(1);
            if col + w > width {
                break;
            }
            if chars[end].0 == ' ' {
                last_space = Some(end);
            }
            col += w;
            end += 1;
        }
        if end == chars.len() {
            rows.push(&chars[start..end]);
            break;
        }
        // Break at the last space in this row when there is one, so a break
        // never falls mid-word; otherwise hard-break (a token wider than the
        // whole row, e.g. a long id).
        let break_at = last_space.filter(|&pos| pos >= start).unwrap_or(end.max(start + 1));
        rows.push(&chars[start..break_at]);
        start = if chars.get(break_at).is_some_and(|(c, _)| *c == ' ') { break_at + 1 } else { break_at };
        while start < chars.len() && chars[start].0 == ' ' {
            start += 1;
        }
    }

    rows.into_iter()
        .map(|row| {
            let end = row.iter().rposition(|(c, _)| *c != ' ').map_or(0, |i| i + 1);
            let mut spans: Vec<Span<'static>> = Vec::new();
            for &(c, style) in &row[..end] {
                match spans.last_mut() {
                    Some(last) if last.style == style => last.content.to_mut().push(c),
                    _ => spans.push(Span::styled(c.to_string(), style)),
                }
            }
            Line::from(spans)
        })
        .collect()
}

/// Wrap a bibliography row so its continuation lines hang under the citation
/// text instead of under its `[1]` mark. The row's first span *is* the mark
/// (with its trailing space), so everything after it wraps into `width - mark`
/// columns and every continuation line opens with a spacer that wide.
fn wrap_biblio(line: Line<'static>, width: usize) -> Vec<Line<'static>> {
    use unicode_width::UnicodeWidthStr;
    let Some(mark) = line.spans.first().cloned() else {
        return wrap_line(line, width);
    };
    let hang = mark.content.as_ref().width();
    if width <= hang {
        return wrap_line(line, width);
    }
    let mut rest = line;
    rest.spans.remove(0);
    let rest_rows = wrap_line(rest, width - hang);
    if rest_rows.len() == 1 && rest_rows[0].spans.is_empty() {
        return vec![mark.into()];
    }
    let spacer = Span::styled(" ".repeat(hang), mark.style);
    rest_rows
        .into_iter()
        .enumerate()
        .map(|(i, row)| {
            let mut spans = Vec::with_capacity(row.spans.len() + 1);
            spans.push(if i == 0 { mark.clone() } else { spacer.clone() });
            spans.extend(row.spans);
            Line::from(spans)
        })
        .collect()
}

// -------------------------------------------------------------- collapsers

/// The collapse/expand handles on the tree and sidebar borders — the only
/// visual sign that either pane can be tucked away, and the only way back
/// in once it has been. Each glyph points the way the pane would travel:
/// outward, into the pane, to collapse it; inward, into the document, to
/// pull a collapsed one back.
pub fn toggles(frame: &mut Frame, app: &App) {
    let style = Style::default().fg(app.theme.accent).add_modifier(Modifier::BOLD);
    if let Some(rect) = app.areas.tree_toggle {
        let glyph = if app.show_tree { "◂" } else { "▸" };
        frame.render_widget(Paragraph::new(Span::styled(glyph, style)), rect);
    }
    if let Some(rect) = app.areas.sidebar_toggle {
        let glyph = if app.show_sidebar { "▸" } else { "◂" };
        frame.render_widget(Paragraph::new(Span::styled(glyph, style)), rect);
    }
    if let Some(rect) = app.areas.inspector_toggle {
        let glyph = if app.show_inspector { "▾" } else { "▴" };
        frame.render_widget(Paragraph::new(Span::styled(glyph, style)), rect);
    }
}

// ---------------------------------------------------------------- sidebar

pub fn sidebar(frame: &mut Frame, app: &App, area: Rect) {
    let focused = app.focus == Focus::Sidebar;
    let block = pane(&app.theme, "agents", focused);
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
        .unwrap_or_else(|| "starting the agent session…".to_string());
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

    // The editor is modeless, so there is no mode to report — only whether you
    // are reading or writing.
    let (mode, mode_style) = match app.open.as_ref().and_then(|o| o.editor.as_ref()) {
        Some(editor) if editor.dirty() => ("EDIT ⏺", Style::default().fg(theme.bg).bg(theme.warn)),
        Some(_) => ("EDIT", Style::default().fg(theme.bg).bg(theme.ok)),
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
        left.push(Span::styled(
            format!("  {}:{}", editor.cursor_line() + 1, editor.cursor_col() + 1),
            theme.faint_style(),
        ));
        left.push(Span::styled("  ctrl+s save  esc leave", theme.faint_style()));
    }

    let mut right = Vec::new();
    if let Some(version) = app.engine_version.as_deref() {
        right.push(Span::styled(format!("v{version}  "), theme.faint_style()));
    }
    right.push(Span::styled(
        format!("catppuccin {}  ", theme.flavor.as_str()),
        theme.faint_style(),
    ));
    if let Some(oneline) = app.oneline.as_deref() {
        right.push(Span::styled(oneline.to_string(), theme.dim()));
    }

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
    fn navigation_arrows_rendered_in_block_title() {
        use ratatui::buffer::Buffer;
        use ratatui::widgets::Widget;
        let area = Rect::new(0, 0, 20, 3);
        let mut buf = Buffer::empty(area);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .title(Line::from(vec![Span::raw(" ← "), Span::raw(" → ")]));
        block.render(area, &mut buf);
        assert_eq!(buf[(0, 0)].symbol(), "╭");
        assert_eq!(buf[(1, 0)].symbol(), " ");
        assert_eq!(buf[(2, 0)].symbol(), "←");
        assert_eq!(buf[(3, 0)].symbol(), " ");
        assert_eq!(buf[(4, 0)].symbol(), " ");
        assert_eq!(buf[(5, 0)].symbol(), "→");
        assert_eq!(buf[(6, 0)].symbol(), " ");
        assert_eq!(buf[(7, 0)].symbol(), "─");
    }

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
            inspect_scroll: 0,
            link: None,
            editor: None,
            selection: None,
            selected_citation: None,
            citation_scroll_pending: false,
        };
        assert!(inspector_height(&open, 40) <= 20);
        assert!(inspector_height(&open, 12) >= 5);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn open_doc(body: &str) -> Open {
        let dir = std::env::temp_dir().join(format!("podarcis-insp-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        let path = dir.join("wiki/a.md");
        std::fs::write(&path, format!("---\ntitle: T\n---\n{body}\n")).unwrap();
        let page = crate::vault::page::Page::load(&path, &dir).unwrap();
        let doc = crate::ui::markdown::render(&page.body, 40, &Theme::default());
        Open {
            doc,
            page,
            doc_width: 40,
            scroll: 0,
            inspect_scroll: 0,
            link: None,
            editor: None,
            selection: None,
            selected_citation: None,
            citation_scroll_pending: false,
        }
    }

    #[test]
    fn the_sources_section_lists_full_citations_in_citation_order_with_the_id_last() {
        let open = open_doc(
            "Cites beta first, then alpha[^b][^a].\n\n## Sources\n[^a]: [Alpha paper (2020). A deliberately long title so the truncation that used to eat it would be visible in the row. *Journal of Letters*, 1(1).](../lit/a/metadata.md)\n[^b]: [Beta paper (2019).](../lit/b/metadata.md)\n",
        );
        let root = open.page.path.parent().unwrap().to_path_buf();
        let index = crate::vault::index::Index::default();
        let lines = inspector_lines(&open, &index, &root, &Theme::default());

        let rows: Vec<String> = lines.iter().map(|l| format!("{l}")).collect();
        assert_eq!(rows.len(), 2, "the two footnote definitions render, untruncated");
        assert!(
            rows[0].starts_with("[1]") && rows[1].starts_with("[2]"),
            "numbered in body citation order, not definition order: {:?}",
            rows
        );
        assert!(rows[0].contains("Beta paper (2019)"));
        assert!(rows[1].to_lowercase().contains("long title"), "full text, not a truncated copy: {:?}", rows);
        assert!(rows[1].to_lowercase().contains("journal of letters"));
        assert!(rows[0].ends_with("b"), "the id hangs at the row's end: {:?}", rows[0]);
        assert!(rows[1].ends_with("a"), "the id hangs at the row's end: {:?}", rows[1]);
    }

    #[test]
    fn an_uncited_source_rows_after_the_numbered_ones() {
        let open = open_doc("One cited[^a].\n\n## Sources\n[^a]: [A paper (2021).](../lit/a/metadata.md)\n[^ghost]: [Never cited (2022).](../lit/g/metadata.md)\n");
        let root = open.page.path.parent().unwrap().to_path_buf();
        let index = crate::vault::index::Index::default();
        let lines = inspector_lines(&open, &index, &root, &Theme::default());

        let rows: Vec<String> = lines.iter().map(|l| format!("{l}")).collect();
        assert!(rows[0].starts_with("[1]") && rows[0].contains("A paper (2021)"));
        assert!(rows[1].starts_with("·") && rows[1].contains("Never cited (2022)"));
    }

    #[test]
    fn biblio_wrapping_hangs_continuation_lines_under_the_citation() {
        let row = Line::from(vec![
            Span::raw("[2] "),
            Span::raw("A paper title long enough that it must wrap onto another line here."),
            Span::raw("  fame1970"),
        ]);
        let wrapped = wrap_biblio(row, 20);
        let rows: Vec<String> = wrapped.iter().map(|l| format!("{l}")).collect();
        assert!(rows.len() > 1, "long citation wraps: {:?}", rows);
        assert!(rows[0].starts_with("[2] "), "first line keeps the mark: {:?}", rows);
        for row in rows.iter().skip(1) {
            assert!(row.starts_with("    "), "continuations hang 4 under the mark: {:?}", row);
        }
    }
}
