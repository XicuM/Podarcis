//! The three panes and the status line.

use edtui::{EditorStatusLine, EditorTheme, EditorView, LineNumbers};
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;
use tui_term::widget::PseudoTerminal;

use crate::app::{App, DocStop, Focus, InspectorBacklink, Open, TreePane};
use crate::theme::Theme;
use crate::ui::markdown::{Finds, Overlays};
use crate::vault::git::GitStatus;
use crate::vault::index::Severity;
use unicode_width::UnicodeWidthStr;

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

/// A folded collection shows its header rule and nothing else.
pub const FOLDED_ROWS: u16 = 1;
/// Two borders and one row of content: below this a box cannot show anything.
pub const MIN_COLLECTION_ROWS: u16 = 3;

/// How many rows each collection's box gets.
///
/// Sizes are the user's, not the content's: an empty collection keeps whatever
/// height it was dragged to. A folded one costs a single row, and the last
/// *open* one absorbs the remainder — that is what keeps the column exactly
/// full at any terminal height without having to store fractions, and it is
/// why its own stored height is never consulted.
pub fn collection_rows(heights: &[Option<u16>], folded: &[bool], total: u16) -> Vec<u16> {
    let n = heights.len();
    if n == 0 || total == 0 {
        return vec![0; n];
    }
    // Fewer rows than boxes. Even a header rule costs one, so the boxes that
    // fit get theirs and the rest get nothing — a box drawn into zero rows
    // draws nothing, which is the honest outcome at this size. Without this
    // the folded boxes alone could claim more rows than the column has.
    if total < n as u16 {
        return (0..n).map(|i| u16::from((i as u16) < total)).collect();
    }

    let open: Vec<usize> = (0..n).filter(|i| !folded[*i]).collect();
    let mut out = vec![FOLDED_ROWS; n];

    // Everything folded: the rows still have to add up, so the last one takes
    // the slack rather than leaving a gap under the column.
    let Some((&last, rest)) = open.split_last() else {
        out[n - 1] = total.saturating_sub(FOLDED_ROWS * (n as u16 - 1));
        return out;
    };

    let reserved = FOLDED_ROWS * (n - open.len()) as u16;
    let budget = total.saturating_sub(reserved);
    // Too cramped to honour anyone's preference: share what there is evenly and
    // let the last take the remainder.
    if budget < MIN_COLLECTION_ROWS * open.len() as u16 {
        let each = budget / open.len() as u16;
        for &i in rest {
            out[i] = each;
        }
        out[last] = budget - each * rest.len() as u16;
        return out;
    }

    let even = budget / open.len() as u16;
    let mut spent = 0u16;
    for (k, &i) in rest.iter().enumerate() {
        // Whatever is left must still seat every open box after this one.
        let after = (rest.len() - k) as u16 * MIN_COLLECTION_ROWS;
        let ceiling = budget.saturating_sub(spent + after).max(MIN_COLLECTION_ROWS);
        out[i] = heights[i].unwrap_or(even).clamp(MIN_COLLECTION_ROWS, ceiling);
        spent += out[i];
    }
    out[last] = budget - spent;
    out
}

pub fn tree(frame: &mut Frame, app: &mut App, area: Rect) {
    let names: Vec<String> = app.collection_names();
    let n = names.len().max(1);
    let folded: Vec<bool> = names.iter().map(|c| app.cfg.collapsed_collections.contains(c)).collect();
    let wanted: Vec<Option<u16>> =
        names.iter().map(|c| app.cfg.collection_heights.get(c).copied()).collect();
    let rows = collection_rows(&wanted, &folded, area.height);
    let constraints: Vec<Constraint> = if rows.is_empty() {
        (0..n).map(|_| Constraint::Fill(1)).collect()
    } else {
        rows.iter().map(|r| Constraint::Length(*r)).collect()
    };
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

        let mut spans = vec![
            Span::styled(format!(" {}", row.label), app.theme.title(section_focus)),
        ];
        if let Some(branch) = track.branch() {
            spans.push(Span::styled(" · ", app.theme.faint_style()));
            spans.push(Span::styled(
                format!("{branch} "),
                Style::default().fg(app.theme.ok).add_modifier(Modifier::BOLD),
            ));
        } else {
            spans[0] = Span::styled(format!(" {} ", row.label), app.theme.title(section_focus));
        }
        let title = Line::from(spans);
        // A folded collection keeps its header rule and gives up everything
        // else, so the column reads as a stack of sections rather than a set
        // of boxes that vanish.
        let borders = if folded[i] { Borders::TOP } else { Borders::ALL };
        let block = Block::default()
            .borders(borders)
            .border_type(BorderType::Rounded)
            .border_style(app.theme.border(section_focus))
            .title(title)
            .style(Style::default().bg(app.theme.bg));
        let inner = if folded[i] { Rect::ZERO } else { block.inner(chunk) };
        frame.render_widget(block, chunk);

        // The fold handle, on the header rule at the right-hand end. `\u{25be}`
        // points at the content it would hide; `\u{25b8}` points at content
        // waiting to come back.
        let fold = if chunk.width >= 6 {
            let rect = Rect::new(chunk.x + chunk.width - 3, chunk.y, 1, 1);
            frame.render_widget(
                Paragraph::new(Span::styled(
                    if folded[i] { "\u{25b8}" } else { "\u{25be}" },
                    Style::default().fg(app.theme.accent).add_modifier(Modifier::BOLD),
                )),
                rect,
            );
            Some(rect)
        } else {
            None
        };

        let content_focused = app.focus == Focus::Doc && app.doc_stop == DocStop::Content;
        let open_row = if content_focused {
            open_path.as_ref().and_then(|p| app.tree.rows.iter().position(|r| &r.path == p))
        } else {
            None
        };
        let active_row = if content_focused && open_row.is_some() {
            open_row.unwrap()
        } else {
            selected
        };

        let start = header + 1;
        let height = inner.height as usize;
        let local_selected = if active_row >= start && active_row < end {
            active_row - start
        } else {
            0
        };
        let child_count = end.saturating_sub(start);
        let offset = if active_row >= start && active_row < end {
            scroll_offset(local_selected, child_count, height)
        } else {
            0
        };

        if !folded[i] && !inner.is_empty() && start < end {
            let lines: Vec<Line> = app
                .tree
                .rows
                .iter()
                .enumerate()
                .skip(start + offset)
                .take(end.saturating_sub(start + offset).min(height))
                .map(|(i, row)| {
                    tree_line(app, i, row, selected, focused, open_path.as_deref(), inner.width as usize)
                })
                .collect();
            frame.render_widget(Paragraph::new(lines), inner);
        }

        panes.push(TreePane {
            area: chunk,
            inner,
            header,
            // A folded collection draws none of its rows, so it owns none:
            // `start == end` is what tells hit-testing to answer with the
            // header rather than a row that is not on screen.
            start: if folded[i] { end } else { start },
            end,
            offset,
            fold,
            folded: folded[i],
        });
    }
    // The boundary between two stacked boxes, for dragging. The last box ends
    // at the column's own edge, so it has none below it — which is also why it
    // is the one that absorbs the remainder.
    app.areas.collection_dividers = panes
        .windows(2)
        .enumerate()
        .map(|(i, pair)| (i, pair[1].area.y))
        .collect();
    app.areas.tree_panes = panes;
}

/// Left-of-label badge for a non-markdown file. The colours come from the
/// theme's neutral range, not its alert range — see `Theme::file_badge`.
fn external_badge(theme: &Theme, path: &std::path::Path, is_dir: bool) -> Option<(&'static str, Color)> {
    if is_dir {
        return None;
    }
    let ext = path.extension().and_then(|e| e.to_str())?.to_ascii_lowercase();
    theme.file_badge(&ext)
}

fn tree_line<'a>(
    app: &'a App,
    i: usize,
    row: &'a crate::vault::tree::Row,
    selected: usize,
    focused: bool,
    open_path: Option<&std::path::Path>,
    width: usize,
) -> Line<'a> {
    let is_open = open_path == Some(row.path.as_path());
    let content_focused = app.focus == Focus::Doc && app.doc_stop == DocStop::Content;
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

    let is_highlighted = if content_focused {
        if open_path.is_some() {
            is_open
        } else {
            i == selected
        }
    } else {
        i == selected
    };

    if is_highlighted {
        style = if content_focused && is_open {
            app.theme.selection(false).add_modifier(Modifier::BOLD)
        } else {
            app.theme.selection(focused)
        };
    }

    // Guide runs rather than bare spaces: one space per level left a ragged
    // edge that got harder to read the deeper the tree went, and depth is the
    // one thing a tree exists to show. The guides stay `faint` on a selected
    // row too, so the selection wash reads as one block.
    let indent = row.depth.saturating_sub(1);
    let guide = if is_highlighted {
        style
    } else {
        app.theme.faint_style()
    };
    let mut spans = vec![
        // One cell per level, exactly as the plain spaces were, so adding the
        // guides costs the label no columns in a narrow pane.
        Span::styled("│".repeat(indent), guide),
        Span::styled(marker, style),
    ];

    if let Some((tag, colour)) = external_badge(&app.theme, &row.path, row.is_dir) {
        // The colour block itself is the badge — one space of padding inside
        // it on each side so the three letters are not flush against the edge.
        spans.push(Span::styled(
            format!(" {tag} "),
            Style::default().fg(app.theme.overlay).bg(colour).add_modifier(Modifier::BOLD),
        ));
        spans.push(Span::raw(" "));
    }

    spans.push(Span::styled(row.label.clone(), style));

    // The git glyph and the lint mark used to trail the label, so their column
    // moved with every filename and the eye had to re-find them on each row.
    // They are collected here and pinned to the right edge instead, and the
    // gap between is padded in the row's own style — which also turns the
    // selection into a full-width bar rather than a ragged stub.
    let mut marks: Vec<Span<'a>> = Vec::new();
    if let Some(status) = app.git.status_for(&row.path) {
        let colour = match status {
            GitStatus::Conflict => app.theme.err,
            GitStatus::Modified | GitStatus::Deleted => app.theme.warn,
            GitStatus::Added | GitStatus::Renamed => app.theme.ok,
            GitStatus::Untracked => app.theme.subtext,
        };
        marks.push(Span::styled(
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
        // A weight ramp, lightest to heaviest: a triangle warns, a filled dot
        // is an error. The warning used to be an emoji-presentation codepoint
        // that some terminals draw two cells wide, which shears a
        // column-aligned tree.
        let (mark, colour) = match severity {
            Severity::Clean => ("", app.theme.ok),
            Severity::Warn => (" ▲", app.theme.warn),
            Severity::Error => (" ●", app.theme.err),
        };
        if !mark.is_empty() {
            marks.push(Span::styled(mark, Style::default().fg(colour)));
        }
    }

    let used: usize = spans.iter().map(|s| s.content.as_ref().width()).sum();
    let mark_w: usize = marks.iter().map(|s| s.content.as_ref().width()).sum();
    if used + mark_w > width {
        // Too narrow for both: the label gives up cells, because the marks are
        // the part that cannot be inferred from anything else on the row.
        let over = used + mark_w - width;
        if let Some(label) = spans.last_mut() {
            let kept = label.content.as_ref().width().saturating_sub(over);
            label.content = truncate(label.content.as_ref(), kept).into();
        }
    }
    let used: usize = spans.iter().map(|s| s.content.as_ref().width()).sum();
    spans.push(Span::styled(
        " ".repeat(width.saturating_sub(used + mark_w)),
        style,
    ));
    spans.extend(marks);
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
        app.areas.doc_edit = None;
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
        app.areas.doc_edit = None;
        editor(frame, app, main);
    } else {
        reader(frame, app, main);
    }
    if !inspect.is_empty() {
        inspector(frame, app, inspect);
    }
}

/// The app's front door, and the only screen that exists purely to say what
/// this thing is. It gets the mark, the wordmark and the tagline; the key
/// hints come after, because someone who has read them once wants the brand
/// to be the thing they see, not a menu.
fn empty_document(frame: &mut Frame, app: &mut App, area: Rect) {
    let focused = app.focus == Focus::Doc && app.doc_stop == DocStop::Content;
    // Theme is `Copy`; taking it by value here releases the borrow so the nav
    // arrows can record their hit rects on the same `app`.
    let theme = app.theme;
    // The arrows, and no title text: the body carries the wordmark, and
    // printing the name twice on one screen is how a splash starts to look
    // like chrome.
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(theme.border(focused))
        .style(Style::default().bg(theme.bg))
        .title(Line::from(nav_arrows(app, area, focused)));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        return;
    }

    let keys: [(&str, &str); 5] = [
        ("f", "find a page"),
        ("ctrl+f", "find in the open page"),
        ("/", "search text across the vault"),
        ("ctrl+p", "command palette"),
        ("?", "every key"),
    ];
    // The hint block is laid out as one unit so the keys form a column rather
    // than a ragged left edge.
    let key_w = keys.iter().map(|(k, _)| k.len()).max().unwrap_or(0);
    let hint_w = keys.iter().map(|(_, l)| key_w + 3 + l.len()).max().unwrap_or(0);

    let mut lines: Vec<Line> = Vec::new();

    let logo_w = crate::brand::logo_width();
    // The mark is drawn as one block, so its own internal alignment survives:
    // every row gets the same leading pad, not a per-row centring that would
    // shear the drawing.
    let logo_pad = " ".repeat(centre_pad(inner.width as usize, logo_w));
    // Two tones: the animal in the accent, the strokes it sits against quieter
    // behind it, so the lizard reads first.
    let body_style = Style::default().fg(theme.accent);
    let frame_style = theme.faint_style();
    for row in crate::brand::logo_lines() {
        let mut spans = vec![Span::raw(logo_pad.clone())];
        for ch in row.chars() {
            let style = if crate::brand::is_body(ch) { body_style } else { frame_style };
            match spans.last_mut() {
                // Runs of one tone coalesce into a single span rather than one
                // span per cell.
                Some(last) if last.style == style => last.content.to_mut().push(ch),
                _ => spans.push(Span::styled(ch.to_string(), style)),
            }
        }
        lines.push(Line::from(spans));
    }

    let version = app
        .engine_version
        .as_deref()
        .map(|v| format!("{} v{v}", crate::brand::name()))
        .unwrap_or_else(|| crate::brand::name().to_string());
    lines.push(Line::from(""));
    lines.push(centred(
        &version,
        inner.width,
        Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
    ));
    lines.push(centred(
        crate::brand::tagline(),
        inner.width,
        Style::default().fg(theme.subtext),
    ));

    if let Some(oneline) = app.oneline.as_deref() {
        lines.push(Line::from(""));
        lines.push(centred(
            oneline,
            inner.width,
            theme.faint_style().add_modifier(Modifier::ITALIC),
        ));
    }

    lines.push(Line::from(""));
    lines.push(Line::from(""));
    let pad = " ".repeat(centre_pad(inner.width as usize, hint_w));
    for (key, label) in keys {
        lines.push(Line::from(vec![
            Span::styled(format!("{pad}{key:<key_w$}"), Style::default().fg(theme.accent)),
            Span::styled(format!("   {label}"), theme.dim()),
        ]));
    }

    // Vertically centred, and clipped from the top when the pane is short so
    // the hints — the useful half — are what survives.
    let top = centre_pad(inner.height as usize, lines.len());
    let body = Rect {
        y: inner.y + top as u16,
        height: inner.height.saturating_sub(top as u16),
        ..inner
    };
    let overflow = lines.len().saturating_sub(body.height as usize);
    frame.render_widget(Paragraph::new(lines.split_off(overflow)), body);
}

/// Left pad that centres `width` cells of content in `total`, never negative.
fn centre_pad(total: usize, width: usize) -> usize {
    total.saturating_sub(width) / 2
}

/// Clip to `width` cells, marking the cut with an ellipsis.
fn truncate(text: &str, width: usize) -> String {
    use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};
    if text.width() <= width {
        return text.to_string();
    }
    // One cell is reserved for the ellipsis, so the result is never wider than
    // asked for — the caller has already spent the rest of the row.
    let room = width.saturating_sub(1);
    let mut out = String::new();
    let mut used = 0usize;
    for ch in text.chars() {
        let w = ch.width().unwrap_or(0);
        if used + w > room {
            break;
        }
        used += w;
        out.push(ch);
    }
    out.push('…');
    out
}

/// One centred line of plain text.
fn centred(text: &str, width: u16, style: Style) -> Line<'static> {
    use unicode_width::UnicodeWidthStr;
    let pad = " ".repeat(centre_pad(width as usize, text.width()));
    Line::from(Span::styled(format!("{pad}{text}"), style))
}

/// The back/forward arrows a pane title opens with, and the hit rects behind
/// them.
///
/// Shared by the reader and the home screen: home is a history position like
/// any other, so it carries the same affordance — without arrows of its own
/// there was no way to click forward out of it.
fn nav_arrows(app: &mut App, area: Rect, focused: bool) -> Vec<Span<'static>> {
    let style = |enabled: bool| {
        if enabled {
            app.theme.title(focused)
        } else {
            app.theme.faint_style()
        }
    };
    let spans = vec![
        Span::styled(" \u{2190} ", style(!app.back.is_empty())),
        Span::styled(" \u{2192} ", style(!app.forward.is_empty())),
    ];
    if area.width >= 8 {
        app.areas.nav_back = Some(Rect::new(area.x + 1, area.y, 3, 1));
        app.areas.nav_forward = Some(Rect::new(area.x + 4, area.y, 3, 1));
    } else {
        app.areas.nav_back = None;
        app.areas.nav_forward = None;
    }
    spans
}

fn reader(frame: &mut Frame, app: &mut App, area: Rect) {
    let focused = app.focus == Focus::Doc && app.doc_stop == DocStop::Content;

    let show_edit = area.width >= 12;
    if show_edit {
        app.areas.doc_edit = Some(Rect::new(area.x + area.width.saturating_sub(4), area.y, 3, 1));
    } else {
        app.areas.doc_edit = None;
    }

    let mut title = nav_arrows(app, area, focused);
    let open = app.open.as_ref().unwrap();
    let spent = 8u16 + if show_edit { 4 } else { 0 };
    if area.width > spent + 12 {
        let budget = (area.width - spent - 4) as usize;
        let name = open
            .page
            .okf
            .title
            .clone()
            .unwrap_or_else(|| open.page.rel.rsplit('/').next().unwrap_or("").to_string());
        let collection = open.page.rel.split('/').next().unwrap_or("");
        let status = open.page.okf.status.clone();
        // The status chip is the first thing dropped when the pane narrows,
        // then the collection: the page's own name is what must survive.
        let chip_w = status.as_deref().map_or(0, |s| s.len() + 3);
        let coll_w = collection.len() + 3;
        let (show_chip, show_coll) = (
            chip_w > 0 && name.len() + chip_w <= budget,
            name.len() + coll_w <= budget,
        );
        let room = budget
            .saturating_sub(if show_chip { chip_w } else { 0 })
            .saturating_sub(if show_coll { coll_w } else { 0 });
        title.push(Span::styled(truncate(&name, room), app.theme.title(focused)));
        if show_coll {
            title.push(Span::styled(format!("  {collection}"), app.theme.faint_style()));
        }
        if let Some(status) = status.filter(|_| show_chip) {
            title.push(Span::styled(
                format!(" {status} "),
                Style::default().fg(app.theme.bg).bg(app.theme.status_of(&status)),
            ));
        }
        title.push(Span::raw(" "));
    }
    let title = Line::from(title);

    let mut block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(app.theme.border(focused))
        .style(Style::default().bg(app.theme.bg))
        .padding(ratatui::widgets::Padding::horizontal(1))
        .title(title);

    if show_edit {
        block = block.title(
            Line::from(Span::styled(" \u{270e} ", app.theme.title(focused)))
                .alignment(Alignment::Right),
        );
    }

    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        app.areas.doc_body = Rect::ZERO;
        return;
    }
    // The find bar eats the bottom row of the reader. `doc_body` stays the text
    // region alone: it is what mouse clicks and scroll arithmetic are measured
    // against, so it must never include chrome.
    let (body, bar) = if app.find.is_some() && inner.height > 1 {
        let [body, bar] =
            Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).areas(inner);
        (body, Some(bar))
    } else {
        (inner, None)
    };
    // Prose is reflowed to at most `MAX_WIDTH`, so on a wide pane the text used
    // to sit hard against the left border with the surplus columns left empty.
    // Centring the measure is what makes it read as a page instead of a log.
    // `doc_body` moves with it: it is what clicks and scroll maths measure
    // against, so the text region and the hit region must be the same rect.
    // A table is data rather than prose: it takes the whole pane, and is
    // panned when even that is not enough.
    let measure =
        if open.csv() { body.width } else { body.width.min(crate::ui::markdown::MAX_WIDTH) };
    let body = Rect {
        x: body.x + (body.width - measure) / 2,
        width: measure,
        ..body
    };
    app.areas.doc_body = body;

    let finds = app.finds();
    let marks = app.marks_for(&open.page.path);
    let view = body.width as usize;
    // Clamped here rather than trusted: the pane can narrow under a pan that
    // was legal at the old width.
    let hscroll = open.hscroll.min(open.doc.width.saturating_sub(view));
    let mut over = Overlays::new(open.link, open.selection, &finds, marks);
    over.hscroll = hscroll;
    let lines = open.doc.to_lines(&app.theme, &over, open.scroll, body.height as usize);
    frame.render_widget(Paragraph::new(lines), body);
    pan_edges(frame, app, area, body, open.doc.width, hscroll);

    if let Some(bar) = bar {
        find_bar(frame, app, &finds, bar);
    }

    scrollbar(frame, app, area, open.scroll, open.doc.height(), body.height as usize);
}

/// Mark the sides of a page that runs wider than the pane.
///
/// A clipped table says nothing about what it is hiding, which is the whole of
/// the bug this exists for: the arrow says there is more, and for a CSV — whose
/// doc is one table with known column stops — it says how many columns.
fn pan_edges(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    body: Rect,
    doc_width: usize,
    hscroll: usize,
) {
    let view = body.width as usize;
    if doc_width <= view || area.height < 2 || area.width < 8 {
        return;
    }
    let open = app.open.as_ref().unwrap();
    let (left, right) = open.doc.hidden_columns(hscroll, view);
    let style = Style::default().fg(app.theme.bg).bg(app.theme.accent).add_modifier(Modifier::BOLD);
    // The chips sit on the bottom border, where the vertical scrollbar sits on
    // the right one: chrome belongs in the frame, not over the text.
    let row = area.y + area.height - 1;
    if hscroll > 0 {
        let tag = count_tag("◂", left, true);
        let w = tag.chars().count() as u16;
        frame.render_widget(
            Paragraph::new(Span::styled(tag, style)),
            Rect::new(area.x + 1, row, w, 1),
        );
    }
    if hscroll + view < doc_width {
        let tag = count_tag("▸", right, false);
        let w = tag.chars().count() as u16;
        frame.render_widget(
            Paragraph::new(Span::styled(tag, style)),
            Rect::new(area.x + area.width.saturating_sub(w + 1), row, w, 1),
        );
    }
}

/// ` ◂ 4 ` when the doc knows its columns, a bare ` ◂ ` when it does not — a
/// markdown page can hold several tables, so there is no column count to give.
fn count_tag(arrow: &str, columns: usize, leading: bool) -> String {
    match (columns, leading) {
        (0, _) => format!(" {arrow} "),
        (n, true) => format!(" {arrow} {n} "),
        (n, false) => format!(" {n} {arrow} "),
    }
}

/// The in-page find input: query, hit counter, and the two keys that move.
fn find_bar(frame: &mut Frame, app: &App, finds: &Finds, area: Rect) {
    let Some(find) = app.find.as_ref() else { return };
    let theme = &app.theme;
    let count = if find.query.is_empty() {
        String::new()
    } else if finds.hits.is_empty() {
        "  no matches".to_string()
    } else {
        format!("  {}/{}", finds.current + 1, finds.hits.len())
    };
    let line = Line::from(vec![
        Span::styled("⌕ ", Style::default().fg(theme.accent)),
        Span::styled(find.query.clone(), Style::default().fg(theme.text)),
        Span::styled("▌", Style::default().fg(theme.accent)),
        Span::styled(count, Style::default().fg(theme.warn)),
        Span::styled("   enter next · shift+enter previous · esc close", theme.faint_style()),
    ]);
    frame.render_widget(Paragraph::new(line).style(Style::default().bg(theme.surface)), area);
}

fn scrollbar(frame: &mut Frame, app: &App, area: Rect, scroll: usize, total: usize, height: usize) {
    if total <= height || area.height < 4 || area.width < 2 {
        return;
    }
    let track = area.height.saturating_sub(2) as usize;
    let span = ((height * track) / total).max(1);
    let top = ((scroll * track) / total).min(track.saturating_sub(span));
    let x = area.x + area.width - 1;
    // The scrollbar lives in the right border column. The thumb renders as
    // a left half-block `▌` in the accent color, giving a sleek midpoint
    // between a hairline `┃` and a massive full block without intruding on
    // the inner document padding.
    for i in 0..track {
        let filled = i >= top && i < top + span;
        let y = area.y + 1 + i as u16;
        let (cell, style) = if filled {
            ("▌", Style::default().fg(app.theme.accent))
        } else {
            ("│", app.theme.border(false))
        };
        frame.render_widget(
            Paragraph::new(Span::styled(cell, style)),
            Rect::new(x, y, 1, 1),
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

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum InspectorTarget {
    Source(String),
    Backlinks(Vec<(String, std::path::PathBuf)>),
    None,
}

impl InspectorTarget {
    pub fn as_deref(&self) -> Option<&str> {
        match self {
            Self::Source(s) => Some(s.as_str()),
            _ => None,
        }
    }
}

/// Each inspector line paired with the source id it belongs to, if any — a
/// citation clicked in the body needs this to scroll to its row, and a click
/// on the row itself needs it to know what to open.
fn inspector_rows(
    open: &Open,
    index: &crate::vault::index::Index,
    root: &std::path::Path,
    theme: &Theme,
) -> Vec<(Line<'static>, InspectorTarget)> {
    let rel = crate::vault::page::rel_path(&open.page.path, root);
    let entry = index.by_path(&open.page.path);
    let backlinks = index.backlinks(&rel);
    let page_dir = open.page.path.parent().unwrap_or(root);

    let mut rows: Vec<(Line<'static>, InspectorTarget)> = Vec::new();

    if let Some(entry) = entry {
        for finding in &entry.findings {
            rows.push((
                Line::from(vec![
                    Span::styled("▲ ", Style::default().fg(theme.warn)),
                    Span::styled(finding.code, Style::default().fg(theme.err)),
                    Span::styled(format!("  {}", finding.detail), theme.dim()),
                ]),
                InspectorTarget::None,
            ));
        }
    }

    let cited = open.page.footnote_refs();
    let defs = open.page.footnote_defs();

    // Candidates first, each tagged with the order its `[N]` is cited in the
    // body — numbered sources sort by that number, the uncited trail after.
    let mut candidates: Vec<(usize, String, Line<'static>)> = Vec::new();

    let active_link_target = open
        .link
        .and_then(|i| open.doc.links.get(i))
        .filter(|l| l.kind == crate::vault::links::LinkKind::Footnote)
        .map(|l| l.target.as_str());

    // A footnote definition carries the full citation the wiki wrote — prefer
    // it over the abbreviated `sources:` copy.
    for (id, text, _) in &defs {
        let used = cited.contains(id);
        let mark = match open.doc.citation_number(id) {
            Some(n) => format!("[{n}]"),
            None if used => "✓".to_string(),
            None => "·".to_string(),
        };
        let selected = open.selected_citation.as_deref() == Some(id.as_str())
            || active_link_target == Some(id.as_str());
        let order = open.doc.citation_number(id).unwrap_or(usize::MAX);
        let colour = if used { theme.ok } else { theme.faint };
        let mark_style = if selected {
            Style::default().fg(theme.bg).bg(theme.accent).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(colour)
        };
        candidates.push((
            order,
            id.clone(),
            Line::from(vec![
                Span::styled(mark, mark_style),
                Span::raw(" "),
                Span::styled(text.clone(), Style::default().fg(theme.text)),
                Span::styled(format!("  {id}"), theme.faint_style()),
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
        let selected = open.selected_citation.as_deref() == Some(source.id.as_str())
            || active_link_target == Some(source.id.as_str());
        let order = open.doc.citation_number(&source.id).unwrap_or(usize::MAX);
        let colour = if used { theme.ok } else { theme.faint };
        let mark_style = if selected {
            Style::default().fg(theme.bg).bg(theme.accent).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(colour)
        };
        candidates.push((
            order,
            source.id.clone(),
            Line::from(vec![
                Span::styled(mark, mark_style),
                Span::raw(" "),
                Span::styled(label, Style::default().fg(theme.text)),
                Span::styled(format!("  {}", source.id), theme.faint_style()),
            ]),
        ));
    }
    candidates.sort_by_key(|(order, _, _)| *order);
    if !candidates.is_empty() {
        rows.push((Line::default(), InspectorTarget::None));
        for (i, (_, id, line)) in candidates.into_iter().enumerate() {
            if i > 0 {
                rows.push((Line::default(), InspectorTarget::None));
            }
            rows.push((line, InspectorTarget::Source(id)));
        }
        rows.push((Line::default(), InspectorTarget::None));
    }

    if !backlinks.is_empty() {
        let mut spans = vec![Span::styled("← ", Style::default().fg(theme.link))];
        for (i, entry) in backlinks.iter().enumerate() {
            if i > 0 {
                spans.push(Span::styled(" · ", theme.dim()));
            }
            spans.push(Span::styled(entry.title.clone(), Style::default().fg(theme.link)));
        }
        let entries = backlinks.iter().map(|e| (e.title.clone(), e.path.clone())).collect();
        rows.push((Line::from(spans), InspectorTarget::Backlinks(entries)));
    }

    if rows.is_empty() {
        rows.push((
            Line::from(Span::styled("no sources, no backlinks, nothing to fix", theme.faint_style())),
            InspectorTarget::None,
        ));
    }

    rows
}

pub fn wrap_backlinks(
    entries: &[(String, std::path::PathBuf)],
    width: usize,
    theme: &Theme,
) -> (Vec<Line<'static>>, Vec<InspectorBacklink>) {
    use unicode_width::UnicodeWidthChar;

    if width == 0 || entries.is_empty() {
        return (vec![Line::default()], Vec::new());
    }

    let mut chars: Vec<(char, Style, Option<std::path::PathBuf>)> = Vec::new();
    chars.push(('←', Style::default().fg(theme.link), None));
    chars.push((' ', Style::default().fg(theme.link), None));

    for (i, (title, path)) in entries.iter().enumerate() {
        if i > 0 {
            chars.push((' ', theme.dim(), None));
            chars.push(('·', theme.dim(), None));
            chars.push((' ', theme.dim(), None));
        }
        for c in title.chars() {
            chars.push((c, Style::default().fg(theme.link), Some(path.clone())));
        }
    }

    let mut rows: Vec<&[(char, Style, Option<std::path::PathBuf>)]> = Vec::new();
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
        let break_at = last_space.filter(|&pos| pos >= start).unwrap_or(end.max(start + 1));
        rows.push(&chars[start..break_at]);
        start = if chars.get(break_at).is_some_and(|(c, _, _)| *c == ' ') { break_at + 1 } else { break_at };
        while start < chars.len() && chars[start].0 == ' ' {
            start += 1;
        }
    }

    let mut wrapped_lines: Vec<Line<'static>> = Vec::new();
    let mut links: Vec<InspectorBacklink> = Vec::new();

    for (row_idx, row) in rows.into_iter().enumerate() {
        let end = row.iter().rposition(|(c, _, _)| *c != ' ').map_or(0, |i| i + 1);
        let mut spans: Vec<Span<'static>> = Vec::new();
        let mut cur_col = 0u16;
        let mut cur_link: Option<(u16, u16, std::path::PathBuf)> = None;

        for &(c, style, ref target) in &row[..end] {
            let w = c.width().unwrap_or(1) as u16;

            match target {
                Some(path) => match &mut cur_link {
                    Some((_, end_col, cur_path)) if cur_path == path => {
                        *end_col += w;
                    }
                    _ => {
                        if let Some((start_col, end_col, p)) = cur_link.take() {
                            links.push(InspectorBacklink {
                                row: row_idx,
                                col_start: start_col,
                                col_end: end_col,
                                path: p,
                            });
                        }
                        cur_link = Some((cur_col, cur_col + w, path.clone()));
                    }
                },
                None => {
                    if let Some((start_col, end_col, p)) = cur_link.take() {
                        links.push(InspectorBacklink {
                            row: row_idx,
                            col_start: start_col,
                            col_end: end_col,
                            path: p,
                        });
                    }
                }
            }

            cur_col += w;

            match spans.last_mut() {
                Some(last) if last.style == style => last.content.to_mut().push(c),
                _ => spans.push(Span::styled(c.to_string(), style)),
            }
        }

        if let Some((start_col, end_col, p)) = cur_link {
            links.push(InspectorBacklink {
                row: row_idx,
                col_start: start_col,
                col_end: end_col,
                path: p,
            });
        }

        wrapped_lines.push(Line::from(spans));
    }

    (wrapped_lines, links)
}

fn inspector(frame: &mut Frame, app: &mut App, area: Rect) {
    let open = app.open.as_ref().unwrap();
    let rel = crate::vault::page::rel_path(&open.page.path, &app.cfg.root);
    let entry = app.index.by_path(&open.page.path);
    let backlinks = app.index.backlinks(&rel);

    let sources = open.page.okf.sources.len() + open.page.footnote_definitions().len();
    let findings = entry.map(|e| e.findings.len()).unwrap_or(0);
    // Same marks the pane body uses for each thing — a citation bracket, `←`
    // for backlinks, `▲` for findings — so the title reads as a summary of
    // what's below rather than a second vocabulary to learn. A count of zero
    // means "nothing to see here", so the segment is dropped instead of
    // printed as `0`.
    let title = [
        (sources > 0).then(|| format!("[{sources}]")),
        (!backlinks.is_empty()).then(|| format!("← {}", backlinks.len())),
        (findings > 0).then(|| format!("▲ {findings}")),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join("  ");
    let title = if title.is_empty() { "empty".to_string() } else { title };
    let focused = app.focus == Focus::Doc && app.doc_stop == DocStop::Sources;
    let block = pane(&app.theme, &title, focused).padding(ratatui::widgets::Padding::horizontal(1));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    app.areas.inspector_body = inner;
    if inner.is_empty() {
        app.areas.inspector_rows.clear();
        app.areas.inspector_backlinks.clear();
        return;
    }

    // Wrapped one raw row at a time (rather than via `wrap_lines` over the
    // whole batch) so each wrapped screen row keeps the citation id its raw
    // row carried — the mapping a click and a scroll-to-selection both need.
    let open = app.open.as_ref().unwrap();
    let raw_rows = inspector_rows(open, &app.index, &app.cfg.root, &app.theme);
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut ids: Vec<Option<String>> = Vec::new();
    let mut all_backlinks: Vec<InspectorBacklink> = Vec::new();
    for (line, target) in raw_rows {
        match target {
            InspectorTarget::Source(id) => {
                let wrapped = wrap_biblio(line, inner.width as usize);
                ids.extend(std::iter::repeat_n(Some(id), wrapped.len()));
                lines.extend(wrapped);
            }
            InspectorTarget::Backlinks(entries) => {
                let (wrapped, backlinks) = wrap_backlinks(&entries, inner.width as usize, &app.theme);
                let base_row = lines.len();
                for mut bl in backlinks {
                    bl.row += base_row;
                    all_backlinks.push(bl);
                }
                ids.extend(std::iter::repeat_n(None, wrapped.len()));
                lines.extend(wrapped);
            }
            InspectorTarget::None => {
                let wrapped = wrap_line(line, inner.width as usize);
                ids.extend(std::iter::repeat_n(None, wrapped.len()));
                lines.extend(wrapped);
            }
        }
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
    app.areas.inspector_backlinks = all_backlinks
        .into_iter()
        .filter(|l| l.row >= scroll && l.row < scroll + height)
        .map(|mut l| {
            l.row -= scroll;
            l
        })
        .collect();
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
/// (with its trailing space or as a separate span), so everything after it wraps
/// into `width - mark` columns and every continuation line opens with a spacer that wide.
fn wrap_biblio(line: Line<'static>, width: usize) -> Vec<Line<'static>> {
    use unicode_width::UnicodeWidthStr;
    let Some(mark) = line.spans.first().cloned() else {
        return wrap_line(line, width);
    };
    let (prefix, hang, remove_count) = if mark.content.ends_with(' ') {
        (vec![mark.clone()], mark.content.as_ref().width(), 1)
    } else if let Some(space) = line.spans.get(1) {
        if space.content.chars().all(|c| c.is_whitespace()) {
            let hang = mark.content.as_ref().width() + space.content.as_ref().width();
            (vec![mark.clone(), space.clone()], hang, 2)
        } else {
            (vec![mark.clone()], mark.content.as_ref().width(), 1)
        }
    } else {
        (vec![mark.clone()], mark.content.as_ref().width(), 1)
    };

    if width <= hang {
        return wrap_line(line, width);
    }
    let mut rest = line;
    for _ in 0..remove_count {
        rest.spans.remove(0);
    }
    let rest_rows = wrap_line(rest, width - hang);
    if rest_rows.len() == 1 && rest_rows[0].spans.is_empty() {
        return vec![Line::from(prefix)];
    }
    let spacer = Span::raw(" ".repeat(hang));
    rest_rows
        .into_iter()
        .enumerate()
        .map(|(i, row)| {
            let mut spans = Vec::with_capacity(row.spans.len() + prefix.len());
            if i == 0 {
                spans.extend(prefix.clone());
            } else {
                spans.push(spacer.clone());
            }
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
/// The narrow-terminal tab strip standing in for the side-by-side panes.
pub fn tab_bar(frame: &mut Frame, app: &App) {
    for (focus, label, rect) in &app.areas.tab_bar {
        let focused = *focus == app.focus;
        let style = if focused {
            Style::default().fg(app.theme.bg).bg(app.theme.accent).add_modifier(Modifier::BOLD)
        } else {
            app.theme.faint_style()
        };
        let text = format!("{label:^width$}", width = rect.width as usize);
        frame.render_widget(Paragraph::new(Span::styled(text, style)), *rect);
    }
}

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
    let mut block = pane(&app.theme, "agent", focused);
    if focused && area.width >= 28 {
        block = block.title(
            Line::from(Span::styled(" f12 to unfocus ", app.theme.dim()))
                .alignment(Alignment::Right),
        );
    }
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

/// The bottom bar.
///
/// Everything here is `subtext`, never `faint`: the bar sits on `surface` and
/// `faint` is the token for inactive borders and gutters, so using it for text
/// on a raised background left the whole bar close to unreadable. The splash
/// keeps the one-liner; the bar does not, because a line of flavour text is
/// not status and it crowded out the things that are.
pub fn status(frame: &mut Frame, app: &App, area: Rect) {
    let theme = &app.theme;
    let mut left: Vec<Span> = Vec::new();

    // When editing, show the active edit state (green or amber if dirty).
    // In reading mode, render the project selector badge instead of "READ".
    let (badge_text, badge_style) = match app.open.as_ref().and_then(|o| o.editor.as_ref()) {
        Some(editor) if editor.dirty() => (" EDIT ⏺ ".to_string(), Style::default().fg(theme.bg).bg(theme.warn)),
        Some(_) => (" EDIT ".to_string(), Style::default().fg(theme.bg).bg(theme.ok)),
        None => (
            format!(" {} ▾ ", app.project_name),
            // The theme's own contrast pole, not a hardcoded white: this was
            // the one widget in the app naming a colour the palette did not
            // hand it, so a light flavour got white-on-light here.
            Style::default().fg(theme.on_accent()).bg(theme.accent),
        ),
    };
    left.push(Span::styled(badge_text, badge_style.add_modifier(Modifier::BOLD)));

    if let Some(prefix) = app.pending {
        left.push(Span::styled(format!(" {prefix}…"), Style::default().fg(theme.literal)));
    }

    if app.indexing {
        left.push(Span::styled(format!("  {} indexing…", app.spinner()), Style::default().fg(theme.accent)));
    } else {
        left.push(Span::styled(
            format!(
                "  {} skills  {} agents  {} mcp",
                app.components.skills.len(),
                app.components.agents.len(),
                app.components.mcp.len()
            ),
            theme.dim(),
        ));
        let pages = app.index.entries.len();
        left.push(Span::styled(
            format!("  {pages} page{}", if pages == 1 { "" } else { "s" }),
            theme.dim(),
        ));

        // Vault health. The counts above never move while you work; this is
        // the number that does, and it was the one thing you had to open the
        // lint overlay to see.
        let (errors, warns) = app.index.finding_counts();
        if errors > 0 {
            left.push(Span::styled(format!("  ● {errors}"), Style::default().fg(theme.err)));
        }
        if warns > 0 {
            left.push(Span::styled(format!("  ▲ {warns}"), Style::default().fg(theme.warn)));
        }
        if errors == 0 && warns == 0 && !app.index.entries.is_empty() {
            left.push(Span::styled("  ✓ clean", Style::default().fg(theme.ok)));
        }
    }

    for job in app.jobs.labels() {
        left.push(Span::styled(
            format!("  {} {job}", app.spinner()),
            Style::default().fg(theme.link),
        ));
    }

    if let Some(editor) = app.open.as_ref().and_then(|o| o.editor.as_ref()) {
        left.push(Span::styled(
            format!("  {}:{}", editor.cursor_line() + 1, editor.cursor_col() + 1),
            theme.dim(),
        ));
        left.push(Span::styled("  ctrl+s save  esc leave", theme.dim()));
    } else if let Some(open) = app.open.as_ref() {
        // How far through the page you are. The scrollbar shows it as a
        // position; this says it as a number, which is what you want when you
        // are deciding whether to keep reading.
        let height = app.areas.doc_body.height as usize;
        let total = open.doc.height();
        if total > height && height > 0 {
            let pct = ((open.scroll + height) * 100 / total).min(100);
            left.push(Span::styled(format!("  {pct}%"), theme.dim()));
        }
    }

    let mut right = Vec::new();
    if let Some(version) = app.engine_version.as_deref() {
        right.push(Span::styled(format!("v{version}  "), theme.dim()));
    }
    // The flavour's own name. This used to be hardcoded to "catppuccin", so
    // picking Nord put someone else's brand in the one corner where the app
    // names itself.
    right.push(Span::styled(
        format!("{}  ", theme.flavor.display_name()),
        theme.dim(),
    ));

    let bar = Style::default().bg(theme.surface).fg(theme.text);
    frame.render_widget(Block::default().style(bar), area);

    let right_line = Line::from(right);
    let right_width = right_line.width() as u16;
    let right_area = Rect::new(
        area.x + area.width.saturating_sub(right_width),
        area.y,
        right_width.min(area.width),
        area.height,
    );
    let left_width = area.width.saturating_sub(right_width);
    let left_area = Rect::new(area.x, area.y, left_width, area.height);

    frame.render_widget(Paragraph::new(Line::from(left)), left_area);
    frame.render_widget(Paragraph::new(right_line), right_area);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The home screen is a history position, so it draws the same arrows a
    /// page does and publishes the same hit rects. Without them there is no
    /// way to click your way back out of it.
    #[test]
    fn the_home_screen_carries_the_navigation_arrows_too() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-homenav-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();
        std::fs::write(dir.join("wiki/a.md"), "---\ntitle: Alpha\n---\nbody\n").unwrap();

        let (mut app, _rx) = crate::app::test_app(&dir);
        let mut terminal = Terminal::new(TestBackend::new(48, 12)).unwrap();
        let top = |app: &mut App, terminal: &mut Terminal<TestBackend>| -> String {
            terminal.draw(|f| document(f, app, f.area())).unwrap();
            let b = terminal.backend().buffer().clone();
            (0..b.area.width).map(|x| b[(x, 0)].symbol().to_string()).collect()
        };

        let row = top(&mut app, &mut terminal);
        assert!(row.contains('←') && row.contains('→'), "home draws both arrows: {row}");
        let (back, forward) = (app.areas.nav_back, app.areas.nav_forward);
        assert!(back.is_some() && forward.is_some(), "and publishes both hit rects");

        // The rects are where the glyphs actually are, so a click lands on
        // what it looks like it is hitting.
        let glyph_at = |rect: Rect, terminal: &Terminal<TestBackend>| -> String {
            let b = terminal.backend().buffer();
            (rect.x..rect.x + rect.width).map(|x| b[(x, rect.y)].symbol().to_string()).collect()
        };
        assert_eq!(glyph_at(back.unwrap(), &terminal).trim(), "←");
        assert_eq!(glyph_at(forward.unwrap(), &terminal).trim(), "→");

        // Leaving and coming back keeps them in the same place on both screens.
        app.open_path(&dir.join("wiki/a.md"), true);
        let row = top(&mut app, &mut terminal);
        assert!(row.contains("Alpha"), "the reader names the page: {row}");
        assert_eq!(app.areas.nav_back, back, "the arrows do not move between screens");

        app.run(crate::keymap::Cmd::Home);
        let row = top(&mut app, &mut terminal);
        assert!(!row.contains("Alpha"), "g h leaves the page: {row}");
        assert_eq!(app.areas.nav_back, back);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// You must be able to tell what you are reading without leaving the
    /// reader, and the prose must sit in a centred measure rather than hard
    /// against the left border of a wide pane.
    #[test]
    fn the_reader_names_the_page_and_centres_its_measure() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-reader-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();
        let page = dir.join("wiki/glycogen.md");
        std::fs::write(
            &page,
            "---\ntitle: Glycogen Resynthesis\ntype: concept\ncategory: c\nrationale: r\nstatus: verified\n---\nbody text\n",
        )
        .unwrap();

        let (mut app, _rx) = crate::app::test_app(&dir);
        app.open_path(&page, true);

        // Far wider than MAX_WIDTH, so the measure has surplus to centre in.
        let width = crate::ui::markdown::MAX_WIDTH + 60;
        let mut terminal = Terminal::new(TestBackend::new(width, 12)).unwrap();
        terminal.draw(|f| document(f, &mut app, f.area())).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let rows: Vec<String> = (0..buffer.area.height)
            .map(|y| (0..buffer.area.width).map(|x| buffer[(x, y)].symbol().to_string()).collect())
            .collect();
        let screen = rows.join("\n");

        assert!(rows[0].contains("Glycogen Resynthesis"), "the title bar names the page: {:?}", rows[0]);
        assert!(rows[0].contains("wiki"), "and the collection it lives in: {:?}", rows[0]);
        assert!(rows[0].contains("verified"), "and its OKF status: {:?}", rows[0]);

        let body = rows.iter().find(|r| r.contains("body text")).expect("the body is drawn");
        let left = body.find("body text").unwrap();
        let right = body.len() - (left + "body text".len());
        assert!(
            left > 20,
            "the measure is centred, not flush left (text starts at column {left}):\n{screen}"
        );
        assert!(right > 20, "and there is surplus on the right too:\n{screen}");
        assert_eq!(
            app.areas.doc_body.width,
            crate::ui::markdown::MAX_WIDTH,
            "the hit region is the text region, so clicks still land where the words are"
        );

        if std::env::var("SHOW").is_ok() {
            println!("{screen}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The front door carries the brand. It is the one screen whose whole job
    /// is to say what this is, so the mark, the wordmark and the tagline are
    /// all load-bearing.
    #[test]
    fn the_empty_document_shows_the_mark_the_wordmark_and_the_tagline() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-splash-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();
        let (mut app, _rx) = crate::app::test_app(&dir);
        assert!(app.open.is_none(), "nothing is open, so this is the splash");

        let mut terminal = Terminal::new(TestBackend::new(80, 30)).unwrap();
        terminal.draw(|f| document(f, &mut app, f.area())).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let rows: Vec<String> = (0..buffer.area.height)
            .map(|y| (0..buffer.area.width).map(|x| buffer[(x, y)].symbol().to_string()).collect())
            .collect();
        let screen = rows.join("\n");

        assert!(screen.contains(crate::brand::name()), "the wordmark is on screen:\n{screen}");
        assert!(screen.contains(crate::brand::tagline()), "the tagline is on screen:\n{screen}");
        for row in crate::brand::logo_lines().iter().filter(|r| !r.trim().is_empty()) {
            assert!(screen.contains(row.trim_end()), "logo row {row:?} is drawn:\n{screen}");
        }
        assert!(screen.contains("command palette"), "the key hints survive:\n{screen}");

        // The mark is centred as a block, so every one of its rows starts at
        // the same column — a per-row centring would shear the drawing.
        let starts: Vec<usize> = rows
            .iter()
            .filter(|r| r.contains('\u{2588}'))
            .map(|r| r.find(|c: char| c != ' ' && c != '\u{2502}' && c != '\u{256d}' && c != '\u{2570}').unwrap())
            .collect();
        assert!(starts.len() >= 3, "several logo rows are drawn");

        if std::env::var("SHOW").is_ok() {
            println!("{screen}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

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

    /// The fold handle is on screen, folding hides the box's rows, and the
    /// handle stays reachable so it can be brought back.
    #[test]
    fn a_collection_folds_to_its_header_and_unfolds_from_the_same_handle() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-fold-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        for c in ["wiki", "workspace", "sources"] {
            std::fs::create_dir_all(dir.join(c)).unwrap();
            std::fs::write(dir.join(c).join("a.md"), "---\ntitle: A\n---\nbody\n").unwrap();
        }
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();

        let (mut app, _rx) = crate::app::test_app(&dir);
        let mut terminal = Terminal::new(TestBackend::new(34, 30)).unwrap();
        let screen = |app: &mut App, terminal: &mut Terminal<TestBackend>| -> String {
            let area = Rect::new(0, 0, 34, 30);
            terminal.draw(|f| tree(f, app, area)).unwrap();
            let b = terminal.backend().buffer().clone();
            (0..b.area.height)
                .map(|y| (0..b.area.width).map(|x| b[(x, y)].symbol().to_string()).collect::<String>())
                .collect::<Vec<_>>()
                .join("\n")
        };

        let before = screen(&mut app, &mut terminal);
        assert!(before.contains('▾'), "an open box offers a fold handle:\n{before}");
        let panes = app.areas.tree_panes.clone();
        assert_eq!(panes.len(), 3);
        let handle = panes[0].fold.expect("the first box has a handle");
        let open_rows = panes[0].area.height;
        assert!(open_rows > FOLDED_ROWS);

        // Clicking the handle folds it, and the box shrinks to its rule.
        app.fold_collection(0);
        let after = screen(&mut app, &mut terminal);
        assert_eq!(app.areas.tree_panes[0].area.height, FOLDED_ROWS, "folded to a rule:\n{after}");
        assert!(after.contains('▸'), "and the handle now points at hidden content:\n{after}");
        assert_eq!(
            app.areas.tree_panes[0].fold.map(|r| (r.x, r.y)),
            Some((handle.x, handle.y)),
            "the handle stays where it was, so it can be clicked back"
        );
        // The boxes below take the freed rows; the column stays exactly full.
        let total: u16 = app.areas.tree_panes.iter().map(|p| p.area.height).sum();
        assert_eq!(total, 30);
        assert!(app.areas.tree_panes[2].area.height > FOLDED_ROWS);

        app.fold_collection(0);
        assert_eq!(app.areas.tree_panes.len(), 3);
        let back = screen(&mut app, &mut terminal);
        assert!(back.contains('▾'), "unfolding restores the handle:\n{back}");
        assert_eq!(app.areas.tree_panes[0].area.height, open_rows, "and the height it had");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The column must be exactly full at every terminal height, whatever the
    /// stored preferences say — a row of slack shows as a gap under the tree,
    /// and a row of overflow silently clips the last box.
    #[test]
    fn collection_rows_always_add_up_to_the_column() {
        for total in 0..60u16 {
            for folded in [
                [false, false, false],
                [true, false, false],
                [false, true, false],
                [false, false, true],
                [true, true, false],
                [true, true, true],
            ] {
                for wanted in [
                    [None, None, None],
                    [Some(10), None, None],
                    [Some(4), Some(4), Some(4)],
                    [Some(200), Some(200), Some(200)],
                    [Some(0), Some(1), Some(2)],
                ] {
                    let rows = collection_rows(&wanted, &folded, total);
                    assert_eq!(rows.len(), 3);
                    assert_eq!(
                        rows.iter().sum::<u16>(),
                        total,
                        "total={total} folded={folded:?} wanted={wanted:?} -> {rows:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn a_folded_collection_costs_one_row_and_the_rest_is_shared() {
        let rows = collection_rows(&[None, None, None], &[false, true, false], 31);
        assert_eq!(rows[1], FOLDED_ROWS, "the folded box is a header rule: {rows:?}");
        assert_eq!(rows[0] + rows[2], 30);
        assert_eq!(rows.iter().sum::<u16>(), 31);
    }

    #[test]
    fn a_stored_height_is_honoured_and_the_last_open_box_absorbs_the_rest() {
        let rows = collection_rows(&[Some(8), Some(5), None], &[false; 3], 40);
        assert_eq!(rows[0], 8, "the first box gets what it asked for");
        assert_eq!(rows[1], 5);
        assert_eq!(rows[2], 27, "the last open box takes the remainder");

        // The last box's own stored height is deliberately ignored — it is the
        // absorber, so honouring it could only leave the column short.
        let rows = collection_rows(&[Some(8), Some(5), Some(3)], &[false; 3], 40);
        assert_eq!(rows[2], 27);
    }

    /// A box may never be squeezed below the point where it can show a row,
    /// however greedy the box above it is.
    #[test]
    fn an_oversized_preference_cannot_starve_the_boxes_below_it() {
        let rows = collection_rows(&[Some(200), Some(200), None], &[false; 3], 20);
        assert_eq!(rows.iter().sum::<u16>(), 20);
        for (i, r) in rows.iter().enumerate() {
            assert!(*r >= MIN_COLLECTION_ROWS, "box {i} was starved: {rows:?}");
        }
    }

    /// Below the point where everyone can have a minimum there is no good
    /// answer, only a defined one: share evenly and stay exactly full.
    #[test]
    fn a_column_too_short_for_every_box_still_fills_exactly() {
        for total in 0..(MIN_COLLECTION_ROWS * 3) {
            let rows = collection_rows(&[Some(9), Some(9), Some(9)], &[false; 3], total);
            assert_eq!(rows.iter().sum::<u16>(), total, "total={total} -> {rows:?}");
        }
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
            hscroll: 0,
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

    static INSP_COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

    fn open_doc(body: &str) -> Open {
        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-insp-{}-{count}", std::process::id()));
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
            hscroll: 0,
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
        assert_eq!(rows.len(), 5, "two citations with top, middle, and bottom blank lines");
        assert!(rows[0].is_empty(), "line on top of first source");
        assert!(
            rows[1].starts_with("[1]") && rows[3].starts_with("[2]"),
            "numbered in body citation order, not definition order: {:?}",
            rows
        );
        assert!(rows[1].contains("Beta paper (2019)"));
        assert!(rows[2].is_empty(), "extra line between sources");
        assert!(rows[3].to_lowercase().contains("long title"), "full text, not a truncated copy: {:?}", rows);
        assert!(rows[3].to_lowercase().contains("journal of letters"));
        assert!(rows[1].ends_with("b"), "the id hangs at the row's end: {:?}", rows[1]);
        assert!(rows[3].ends_with("a"), "the id hangs at the row's end: {:?}", rows[3]);
        assert!(rows[4].is_empty(), "line after last source");
    }

    #[test]
    fn an_uncited_source_rows_after_the_numbered_ones() {
        let open = open_doc("One cited[^a].\n\n## Sources\n[^a]: [A paper (2021).](../lit/a/metadata.md)\n[^ghost]: [Never cited (2022).](../lit/g/metadata.md)\n");
        let root = open.page.path.parent().unwrap().to_path_buf();
        let index = crate::vault::index::Index::default();
        let lines = inspector_lines(&open, &index, &root, &Theme::default());

        let rows: Vec<String> = lines.iter().map(|l| format!("{l}")).collect();
        assert_eq!(rows.len(), 5);
        assert!(rows[0].is_empty(), "line on top of first source");
        assert!(rows[1].starts_with("[1]") && rows[1].contains("A paper (2021)"));
        assert!(rows[2].is_empty(), "extra line between sources");
        assert!(rows[3].starts_with("·") && rows[3].contains("Never cited (2022)"));
        assert!(rows[4].is_empty(), "line after last source");
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

    #[test]
    fn biblio_wrapping_handles_separated_mark_and_space_without_tinting_spacer() {
        use ratatui::style::Color;
        let row = Line::from(vec![
            Span::styled("[1]", Style::default().bg(Color::Blue)),
            Span::raw(" "),
            Span::raw("A paper title long enough that it must wrap onto another line here."),
            Span::raw("  fame1970"),
        ]);
        let wrapped = wrap_biblio(row, 20);
        let rows: Vec<String> = wrapped.iter().map(|l| format!("{l}")).collect();
        assert!(rows.len() > 1, "long citation wraps: {:?}", rows);
        assert!(rows[0].starts_with("[1] "), "first line keeps the mark and space: {:?}", rows);
        assert_eq!(wrapped[0].spans[0].content, "[1]");
        assert_eq!(wrapped[0].spans[0].style.bg, Some(Color::Blue));
        assert_eq!(wrapped[0].spans[1].content, " ");
        assert_eq!(wrapped[0].spans[1].style.bg, None);
        for (i, line) in wrapped.iter().enumerate().skip(1) {
            assert!(rows[i].starts_with("    "), "continuations hang 4 under the mark: {:?}", rows[i]);
            assert_eq!(line.spans[0].content, "    ");
            assert_eq!(line.spans[0].style.bg, None, "spacer must not inherit mark background");
        }
    }

    #[test]
    fn selecting_a_citation_highlights_its_number_mark_not_its_id() {
        let mut open = open_doc(
            "Cites beta first, then alpha[^b][^a].\n\n## Sources\n[^a]: [Alpha paper (2020).](../lit/a/metadata.md)\n[^b]: [Beta paper (2019).](../lit/b/metadata.md)\n",
        );
        let root = open.page.path.parent().unwrap().to_path_buf();
        let index = crate::vault::index::Index::default();
        let theme = Theme::default();

        // When nothing is selected, [1] has ok colour, no bg; and id 'b' has faint style.
        let rows = inspector_rows(&open, &index, &root, &theme);
        assert!(rows[0].0.spans.is_empty(), "row 0 is blank top line");
        let (first_line, first_id) = &rows[1];
        assert_eq!(first_id.as_deref(), Some("b"));
        assert_eq!(first_line.spans[0].content, "[1]");
        assert_eq!(first_line.spans[0].style.fg, Some(theme.ok));
        assert_eq!(first_line.spans[0].style.bg, None);
        // The id span (index 3) is faint, not highlighted
        assert_eq!(first_line.spans[3].content, "  b");
        assert_eq!(first_line.spans[3].style.bg, None);

        // When citation "b" is selected, [1] is highlighted with accent bg, while the id stays faint.
        open.selected_citation = Some("b".to_string());
        let rows = inspector_rows(&open, &index, &root, &theme);
        let (first_line, first_id) = &rows[1];
        assert_eq!(first_id.as_deref(), Some("b"));
        assert_eq!(first_line.spans[0].content, "[1]");
        assert_eq!(first_line.spans[0].style.bg, Some(theme.accent));
        assert_eq!(first_line.spans[0].style.fg, Some(theme.bg));
        // id 'b' is still not highlighted with accent bg
        assert_eq!(first_line.spans[3].content, "  b");
        assert_eq!(first_line.spans[3].style.bg, None);

        // When the active link in the reader is [^b]
        open.selected_citation = None;
        open.link = open.doc.citation_link("b");
        let rows = inspector_rows(&open, &index, &root, &theme);
        let (first_line, _) = &rows[1];
        assert_eq!(first_line.spans[0].style.bg, Some(theme.accent));
        assert_eq!(first_line.spans[3].style.bg, None);
    }

    #[test]
    fn backlinks_render_with_dot_separator_and_link_style() {
        let entries = vec![
            ("Agent Memory Index".to_string(), std::path::PathBuf::from("/vault/wiki/index.md")),
            ("Agent Memory Taxonomies".to_string(), std::path::PathBuf::from("/vault/wiki/tax.md")),
        ];
        let theme = Theme::default();
        let (lines, links) = wrap_backlinks(&entries, 80, &theme);

        assert_eq!(lines.len(), 1);
        let line = &lines[0];
        assert_eq!(format!("{line}"), "← Agent Memory Index · Agent Memory Taxonomies");

        assert_eq!(line.spans[0].content, "← Agent Memory Index");
        assert_eq!(line.spans[0].style.fg, Some(theme.link));

        assert_eq!(line.spans[1].content, " · ");
        assert_eq!(line.spans[1].style, theme.dim());

        assert_eq!(line.spans[2].content, "Agent Memory Taxonomies");
        assert_eq!(line.spans[2].style.fg, Some(theme.link));

        assert_eq!(links.len(), 2);
        assert_eq!(links[0].row, 0);
        assert_eq!(links[0].col_start, 2);
        assert_eq!(links[0].col_end, 20);
        assert_eq!(links[0].path, std::path::PathBuf::from("/vault/wiki/index.md"));

        assert_eq!(links[1].row, 0);
        assert_eq!(links[1].col_start, 23);
        assert_eq!(links[1].col_end, 46);
        assert_eq!(links[1].path, std::path::PathBuf::from("/vault/wiki/tax.md"));
    }

    #[test]
    fn wrap_backlinks_handles_multiline_wrapping() {
        let entries = vec![
            ("Agent Memory Index".to_string(), std::path::PathBuf::from("/vault/wiki/index.md")),
            ("Agent Memory Taxonomies".to_string(), std::path::PathBuf::from("/vault/wiki/tax.md")),
        ];
        let theme = Theme::default();
        // Width 25 forces wrap between the first and second entry
        let (lines, links) = wrap_backlinks(&entries, 25, &theme);

        assert_eq!(lines.len(), 2);
        assert_eq!(format!("{}", lines[0]), "← Agent Memory Index ·");
        assert_eq!(format!("{}", lines[1]), "Agent Memory Taxonomies");

        assert_eq!(links.len(), 2);
        assert_eq!(links[0].row, 0);
        assert_eq!(links[0].col_start, 2);
        assert_eq!(links[0].col_end, 20);

        assert_eq!(links[1].row, 1);
        assert_eq!(links[1].col_start, 0);
        assert_eq!(links[1].col_end, 23);
        assert_eq!(links[1].path, std::path::PathBuf::from("/vault/wiki/tax.md"));
    }

    #[test]
    fn status_bar_renders_project_selector_instead_of_read() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;
        use crate::config::Config;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-status-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();

        let cfg = Config::load(&dir);
        let (tx, _rx) = std::sync::mpsc::channel();
        let mut app = App::new(cfg, tx);
        app.project_name = "test-project".to_string();

        let backend = TestBackend::new(80, 1);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|f| {
                status(f, &app, f.area());
            })
            .unwrap();

        let buffer = terminal.backend().buffer();
        let content: String = (0..buffer.area.width)
            .map(|x| buffer[(x, 0)].symbol().to_string())
            .collect();

        assert!(content.contains("test-project ▾"), "status has project selector: {content}");

        // The bar sits on `surface`, so nothing in it may use `faint` — that
        // is the gutter token, and text painted with it on a raised
        // background is what made the bar hard to read.
        for x in 0..buffer.area.width {
            assert_ne!(
                buffer[(x, 0)].fg,
                app.theme.faint,
                "column {x} of the status bar is painted in the gutter colour: {content}"
            );
        }
        assert_eq!(
            buffer[(1, 0)].fg,
            app.theme.on_accent(),
            "the badge takes its foreground from the palette, never a hardcoded white"
        );
        assert_eq!(buffer[(1, 0)].bg, app.theme.accent);
        assert!(!content.contains("READ"), "status does not contain READ: {content}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The sources pane sits below the reader, not on top of it: the reader's
    /// own bottom border survives, and the end of the page can be scrolled
    /// into the rows above it.
    #[test]
    fn the_sources_pane_does_not_cover_the_end_of_the_page() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-inspect-overlap-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        let body: String = (1..=60).map(|i| format!("para {i}\n\n")).collect();
        let page = dir.join("wiki/long.md");
        std::fs::write(
            &page,
            format!("---\ntitle: Long\ntype: concept\ncategory: c\nrationale: r\n---\n{body}"),
        )
        .unwrap();

        let (mut app, _rx) = crate::app::test_app(&dir);
        app.open_path(&page, true);

        let mut terminal = Terminal::new(TestBackend::new(60, 30)).unwrap();
        let draw = |app: &mut App, terminal: &mut Terminal<TestBackend>| {
            terminal.draw(|f| document(f, app, f.area())).unwrap();
            let buffer = terminal.backend().buffer().clone();
            let rows: Vec<String> = (0..buffer.area.height)
                .map(|y| (0..buffer.area.width).map(|x| buffer[(x, y)].symbol().to_string()).collect())
                .collect();
            rows
        };

        // First frame measures the panes; the second scrolls with those
        // measurements in hand, as the event loop does.
        draw(&mut app, &mut terminal);
        let reader_bottom = app.areas.inspector.y.saturating_sub(1) as usize;
        app.run(crate::keymap::Cmd::DocBottom);
        let rows = draw(&mut app, &mut terminal);

        assert!(app.areas.inspector.y > app.areas.doc_main.y, "the sources pane is below the reader");
        assert!(
            rows[reader_bottom].chars().all(|c| "╰╯─".contains(c)),
            "the reader keeps its own bottom border: {:?}",
            rows[reader_bottom]
        );
        assert!(
            rows[..reader_bottom].iter().any(|r| r.contains("para 60")),
            "the last paragraph scrolls into the reader:\n{}",
            rows.join("\n")
        );
    }

    /// The editor resolves clicks against the text area it records while
    /// rendering, so this needs a real frame before the click means anything.
    #[test]
    fn clicking_in_the_editor_places_the_cursor() {
        use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-editor-click-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        let body: String = (1..=12).map(|i| format!("line {i} with some words\n")).collect();
        let page = dir.join("wiki/click.md");
        std::fs::write(
            &page,
            format!("---\ntitle: Click\ntype: concept\ncategory: c\nrationale: r\n---\n{body}"),
        )
        .unwrap();

        let (mut app, _rx) = crate::app::test_app(&dir);
        app.open_path(&page, true);
        app.run(crate::keymap::Cmd::Edit);
        // `draw` calls `sync_sidebar`, which would spawn `herdr --session
        // podarcis` in this temp dir and steal the live TUI's workspace.
        app.show_sidebar = false;

        // The whole frame, not just this pane: `App::on_mouse` resolves the
        // pointer against `areas.doc`, which only the top-level layout sets.
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|f| crate::ui::draw(f, &mut app)).unwrap();

        let before = {
            let editor = app.open.as_ref().unwrap().editor.as_ref().unwrap();
            (editor.cursor_line(), editor.cursor_col())
        };
        let area = app.areas.doc_main;
        // Four rows down and ten columns in, well clear of the gutter.
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: area.x + 10,
            row: area.y + 4,
            modifiers: crossterm::event::KeyModifiers::NONE,
        });

        let editor = app.open.as_ref().unwrap().editor.as_ref().unwrap();
        assert_ne!((editor.cursor_line(), editor.cursor_col()), before, "the click moved the cursor");
        assert!(app.open.as_ref().unwrap().editing(), "and left the editor open");
    }

    #[test]
    fn tree_renders_repo_branch_and_omits_non_git() {
        use std::process::Command;
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;
        use crate::config::Config;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-tree-branch-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        std::fs::create_dir_all(dir.join("sources")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(
            dir.join(".podarcis/config.yaml"),
            "repositories:\n  wiki: local\n  sources: local\n",
        ).unwrap();

        // init git in wiki on branch "test-branch"
        Command::new("git").args(["init"]).current_dir(dir.join("wiki")).output().unwrap();
        Command::new("git").args(["checkout", "-b", "test-branch"]).current_dir(dir.join("wiki")).output().unwrap();

        // sources is not a git repo

        let cfg = Config::load(&dir);
        let (tx, _rx) = std::sync::mpsc::channel();
        let mut app = App::new(cfg, tx);

        let backend = TestBackend::new(80, 20);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|f| {
                tree(f, &mut app, f.area());
            })
            .unwrap();

        let buffer = terminal.backend().buffer();
        let mut rendered = String::new();
        for y in 0..buffer.area.height {
            for x in 0..buffer.area.width {
                rendered.push_str(buffer[(x, y)].symbol());
            }
            rendered.push('\n');
        }

        assert!(rendered.contains("wiki · test-branch"), "tree contains directory and branch separated by ·: {rendered}");
        assert!(!rendered.contains("git repo"), "tree does not contain 'git repo': {rendered}");
        assert!(!rendered.contains("no git"), "tree does not contain 'no git': {rendered}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_wide_csv_says_how_many_columns_are_off_screen() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let dir = std::env::temp_dir().join(format!("podarcis-csv-chip-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::create_dir_all(dir.join("workspace/finance")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();
        let cols: Vec<String> = (0..14).map(|i| format!("col_{i}")).collect();
        let page = dir.join("workspace/finance/wide.csv");
        std::fs::write(&page, format!("{}\n", cols.join(","))).unwrap();

        let (mut app, _rx) = crate::app::test_app(&dir);
        app.open_path(&page, true);
        let mut terminal = Terminal::new(TestBackend::new(60, 12)).unwrap();
        let screen = |app: &mut App, terminal: &mut Terminal<TestBackend>| -> String {
            terminal.draw(|f| document(f, app, f.area())).unwrap();
            let buffer = terminal.backend().buffer().clone();
            (0..buffer.area.height)
                .map(|y| {
                    (0..buffer.area.width).map(|x| buffer[(x, y)].symbol().to_string()).collect::<String>()
                })
                .collect::<Vec<_>>()
                .join("\n")
        };

        let first = screen(&mut app, &mut terminal);
        assert!(first.contains('▸'), "the frame says the table continues right:\n{first}");
        assert!(!first.contains('◂'), "nothing is off to the left yet:\n{first}");

        for _ in 0..40 {
            app.run(crate::keymap::Cmd::PanRight);
        }
        let last = screen(&mut app, &mut terminal);
        assert!(last.contains("col_13"), "panning right reaches the final column:\n{last}");
        assert!(last.contains('◂'), "and the frame says what is now behind you:\n{last}");
        assert!(!last.contains('▸'), "the right edge is the end of the pan:\n{last}");

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tree_renders_a_csv_badge_to_the_left_of_the_label() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;
        use crate::config::Config;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-tree-csv-badge-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::create_dir_all(dir.join("workspace/finance")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();
        std::fs::write(dir.join("workspace/finance/transactions.csv"), "a,b\n1,2\n").unwrap();

        let cfg = Config::load(&dir);
        let (tx, _rx) = std::sync::mpsc::channel();
        let mut app = App::new(cfg, tx);
        app.tree.expand(); // workspace
        app.tree.rebuild();
        app.tree.expand(); // finance
        app.tree.rebuild();

        for flavor in [crate::theme::Flavor::Terminal, crate::theme::Flavor::Latte] {
            app.theme = crate::theme::Theme::new(flavor);
            let backend = TestBackend::new(80, 20);
            let mut terminal = Terminal::new(backend).unwrap();
            terminal.draw(|f| tree(f, &mut app, f.area())).unwrap();

            let buffer = terminal.backend().buffer();
            let mut rendered = String::new();
            let mut badge_bg = None;
            for y in 0..buffer.area.height {
                for x in 0..buffer.area.width {
                    let cell = &buffer[(x, y)];
                    // The badge is now bare letters on a colour block, so the
                    // `C` of `CSV` is what identifies it rather than a bracket.
                    let is_csv_head = cell.symbol() == "C"
                        && x + 2 < buffer.area.width
                        && buffer[(x + 1, y)].symbol() == "S"
                        && buffer[(x + 2, y)].symbol() == "V";
                    if is_csv_head && badge_bg.is_none() {
                        badge_bg = Some(cell.bg);
                    }
                    rendered.push_str(cell.symbol());
                }
                rendered.push('\n');
            }

            assert!(rendered.contains(" CSV "), "{flavor:?} tree shows the csv badge: {rendered}");
            assert!(!rendered.contains("[CSV]"), "{flavor:?} badge is a highlight, not brackets: {rendered}");
            assert_eq!(
                badge_bg,
                Some(app.theme.link),
                "{flavor:?} badge keeps its highlight background (not reset by the flavor's `Color::Reset` bg): {rendered}"
            );
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_reader_shows_edit_button_in_top_right_corner() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-edit-btn-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();
        let page = dir.join("wiki/doc.md");
        std::fs::write(
            &page,
            "---\ntitle: Document\ntype: concept\ncategory: c\nrationale: r\n---\nbody\n",
        )
        .unwrap();

        let (mut app, _rx) = crate::app::test_app(&dir);
        let mut terminal = Terminal::new(TestBackend::new(50, 10)).unwrap();

        // On empty document (splash), there is no edit button.
        terminal.draw(|f| document(f, &mut app, f.area())).unwrap();
        assert!(app.areas.doc_edit.is_none(), "splash screen has no edit button");

        // When a page is open in reader, edit button appears in top-right.
        app.open_path(&page, true);
        terminal.draw(|f| document(f, &mut app, f.area())).unwrap();
        assert!(app.areas.doc_edit.is_some(), "reader has an edit button");
        let edit_rect = app.areas.doc_edit.unwrap();
        assert_eq!(edit_rect.y, 0);
        assert_eq!(edit_rect.width, 3);
        assert_eq!(edit_rect.height, 1);
        assert_eq!(edit_rect.x, 50 - 4);

        let buffer = terminal.backend().buffer();
        let glyphs: String = (edit_rect.x..edit_rect.x + edit_rect.width)
            .map(|x| buffer[(x, edit_rect.y)].symbol().to_string())
            .collect();
        assert!(glyphs.contains('✎'), "edit button contains pencil symbol: {glyphs:?}");

        // In editor mode, doc_edit is None.
        app.run(crate::keymap::Cmd::Edit);
        terminal.draw(|f| document(f, &mut app, f.area())).unwrap();
        assert!(app.areas.doc_edit.is_none(), "editor has no edit button");

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn scrollbar_renders_half_block_thumb() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;
        let dir = std::env::temp_dir().join(format!("podarcis-scrollbar-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        let (mut app, _rx) = crate::app::test_app(&dir);
        let path = dir.join("wiki/long.md");
        let body = (0..100).map(|i| format!("Line {i}\n\n")).collect::<String>();
        std::fs::write(&path, format!("---\ntitle: Long\ntype: concept\ncategory: c\n---\n{body}")).unwrap();
        app.open_path(&path, false);

        let mut terminal = Terminal::new(TestBackend::new(40, 15)).unwrap();
        terminal.draw(|f| document(f, &mut app, f.area())).unwrap();
        let b = terminal.backend().buffer();
        // The right border of the doc pane is column 39. The thumb should render on column 39 as '▌'.
        let mut found_thumb = false;
        for y in 1..14 {
            let col_outer = b[(39, y)].symbol();
            if col_outer == "▌" {
                found_thumb = true;
                break;
            }
        }
        assert!(found_thumb, "the scrollbar thumb is rendered as '▌' on the border column");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tree_highlights_open_page_only_when_content_panel_is_focused() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;

        let count = INSP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("podarcis-tree-highlight-{}-{count}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();

        let page_a = dir.join("wiki/alpha.md");
        let page_b = dir.join("wiki/beta.md");
        std::fs::write(&page_a, "---\ntitle: Alpha\ntype: concept\ncategory: c\nrationale: r\n---\nalpha\n").unwrap();
        std::fs::write(&page_b, "---\ntitle: Beta\ntype: concept\ncategory: c\nrationale: r\n---\nbeta\n").unwrap();

        let (mut app, _rx) = crate::app::test_app(&dir);
        app.tree.expand(); // wiki
        app.tree.rebuild();

        // Open page_b in content window.
        app.open_path(&page_b, true);
        // Position tree.selected on page_a explicitly.
        let pos_a = app.tree.rows.iter().position(|r| r.path == page_a).unwrap();
        app.tree.selected = pos_a;

        let mut terminal = Terminal::new(TestBackend::new(80, 20)).unwrap();

        // 1. Content panel is focused:
        app.focus = Focus::Doc;
        app.doc_stop = DocStop::Content;
        terminal.draw(|f| tree(f, &mut app, f.area())).unwrap();
        let buffer = terminal.backend().buffer().clone();

        let row_bg = |needle: &str| -> Option<Color> {
            for y in 0..buffer.area.height {
                let row_str: String = (0..buffer.area.width).map(|x| buffer[(x, y)].symbol()).collect();
                if let Some(pos) = row_str.find(needle) {
                    return Some(buffer[(pos as u16, y)].bg);
                }
            }
            None
        };

        // When content panel is focused, open page (beta) must have surface highlight background,
        // while alpha (selected in tree) must NOT be highlighted.
        assert_eq!(
            row_bg("beta"),
            Some(app.theme.surface),
            "open page is highlighted with surface background when content panel is focused"
        );
        assert_ne!(
            row_bg("alpha"),
            Some(app.theme.surface),
            "unfocused tree cursor row is not highlighted when content panel is focused"
        );

        // 2. Tree is focused:
        app.focus = Focus::Tree;
        terminal.draw(|f| tree(f, &mut app, f.area())).unwrap();
        let buffer2 = terminal.backend().buffer().clone();
        let row_bg2 = |needle: &str| -> Option<Color> {
            for y in 0..buffer2.area.height {
                let row_str: String = (0..buffer2.area.width).map(|x| buffer2[(x, y)].symbol()).collect();
                if let Some(pos) = row_str.find(needle) {
                    return Some(buffer2[(pos as u16, y)].bg);
                }
            }
            None
        };

        // Tree cursor (alpha) is now focused (accent background).
        // Open page (beta) is NOT highlighted.
        assert_eq!(
            row_bg2("alpha"),
            Some(app.theme.accent),
            "tree cursor is highlighted with accent background when tree is focused"
        );
        assert_ne!(
            row_bg2("beta"),
            Some(app.theme.surface),
            "open page is not highlighted when tree is focused"
        );
        assert_ne!(
            row_bg2("beta"),
            Some(app.theme.accent),
            "open page does not have accent background when tree is focused"
        );

        // 3. Sidebar is focused:
        app.focus = Focus::Sidebar;
        terminal.draw(|f| tree(f, &mut app, f.area())).unwrap();
        let buffer3 = terminal.backend().buffer().clone();
        let row_bg3 = |needle: &str| -> Option<Color> {
            for y in 0..buffer3.area.height {
                let row_str: String = (0..buffer3.area.width).map(|x| buffer3[(x, y)].symbol()).collect();
                if let Some(pos) = row_str.find(needle) {
                    return Some(buffer3[(pos as u16, y)].bg);
                }
            }
            None
        };

        // Open page (beta) is NOT highlighted when sidebar is focused.
        assert_ne!(
            row_bg3("beta"),
            Some(app.theme.surface),
            "open page is not highlighted when sidebar is focused"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }
}

