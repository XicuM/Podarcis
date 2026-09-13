//! Popups: finder, palette, help, outline, prompt, toasts.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Padding, Paragraph, Wrap};
use ratatui::Frame;

use crate::app::{App, Level, Overlay};
use crate::search::Hit;
use crate::theme::Theme;

fn popup(frame: &mut Frame, theme: &Theme, area: Rect, title: &str) -> Rect {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme.accent))
        .title(Span::styled(
            format!(" {title} "),
            Style::default().fg(theme.accent).add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.overlay).fg(theme.text))
        .padding(Padding::horizontal(1));
    let inner = block.inner(area);
    frame.render_widget(Clear, area);
    frame.render_widget(block, area);
    inner
}

pub fn draw(frame: &mut Frame, app: &App, area: Rect) {
    match app.overlay.as_ref() {
        Some(Overlay::Finder(_)) => finder(frame, app, area),
        Some(Overlay::Palette(_)) => palette(frame, app, area),
        Some(Overlay::Themes { selected, .. }) => themes(frame, app, area, *selected),
        Some(Overlay::Help { scroll }) => help(frame, app, area, *scroll),
        Some(Overlay::Outline { selected }) => outline(frame, app, area, *selected),
        Some(Overlay::Prompt(_)) => prompt(frame, app, area),
        Some(Overlay::RepoConfig(_)) => repo_config(frame, app, area),
        Some(Overlay::Menu(_)) => menu(frame, app),
        None => {}
    }
}

/// A query line with a block cursor, so it reads as an input rather than text.
fn query_line(theme: &Theme, prompt: &str, query: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(prompt.to_string(), Style::default().fg(theme.accent)),
        Span::styled(query.to_string(), Style::default().fg(theme.text)),
        Span::styled("▌", Style::default().fg(theme.accent)),
    ])
}

fn hit_lines(theme: &Theme, hits: &[Hit], selected: usize, height: usize) -> Vec<Line<'static>> {
    let start = selected.saturating_sub(height / 3).min(hits.len().saturating_sub(height.max(1)));
    hits.iter()
        .enumerate()
        .skip(start)
        .take(height)
        .map(|(i, hit)| {
            let on = i == selected;
            let base = if on { theme.selection(true) } else { Style::default().fg(theme.text) };
            let mut spans = vec![
                Span::styled(if on { "▌ " } else { "  " }, Style::default().fg(theme.accent)),
                Span::styled(hit.title.clone(), base.add_modifier(Modifier::BOLD)),
                Span::styled("  ", base),
            ];
            // Highlight the characters the fuzzy match landed on.
            if hit.matched.is_empty() {
                spans.push(Span::styled(hit.rel.clone(), theme.faint_style()));
            } else {
                for (n, ch) in hit.rel.chars().enumerate() {
                    let matched = hit.matched.contains(&(n as u32));
                    let style = if matched {
                        Style::default().fg(theme.accent).add_modifier(Modifier::BOLD)
                    } else {
                        theme.faint_style()
                    };
                    spans.push(Span::styled(ch.to_string(), style));
                }
            }
            if !hit.snippet.is_empty() {
                spans.push(Span::styled(format!("  {}", hit.snippet), theme.dim()));
            }
            Line::from(spans)
        })
        .collect()
}

fn finder(frame: &mut Frame, app: &App, area: Rect) {
    let Some(Overlay::Finder(state)) = app.overlay.as_ref() else { return };
    let theme = &app.theme;

    let scope = state.collection.unwrap_or("all");
    let title = format!("{} · {scope}", state.mode.label());
    let box_area = crate::ui::centered(area, area.width.saturating_sub(8).min(110), area.height.saturating_sub(6).min(24));
    let inner = popup(frame, theme, box_area, &title);
    if inner.is_empty() {
        return;
    }

    let [query, list, hint] =
        Layout::vertical([Constraint::Length(2), Constraint::Min(1), Constraint::Length(1)]).areas(inner);

    frame.render_widget(Paragraph::new(query_line(theme, "⌕ ", &state.query)), query);

    if state.running.is_some() {
        let note = Line::from(Span::styled(
            "  searching — qmd takes a while, that is why it is not on the typing path",
            theme.dim(),
        ));
        frame.render_widget(Paragraph::new(note), list);
    } else if let Some(warning) = &state.warning {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!("  {warning}"),
                Style::default().fg(theme.warn),
            )))
            .wrap(Wrap { trim: true }),
            list,
        );
    } else if state.hits.is_empty() {
        let message = if state.mode == crate::search::Mode::Semantic {
            "  type a query, then enter to run the semantic search"
        } else if state.query.is_empty() {
            "  start typing"
        } else {
            "  nothing matches"
        };
        frame.render_widget(Paragraph::new(Line::from(Span::styled(message, theme.faint_style()))), list);
    } else {
        let lines = hit_lines(theme, &state.hits, state.selected, list.height as usize);
        frame.render_widget(Paragraph::new(lines), list);
    }

    let hints = Line::from(vec![
        Span::styled("ctrl+s ", theme.faint_style()),
        Span::styled("mode   ", theme.dim()),
        Span::styled("ctrl+l ", theme.faint_style()),
        Span::styled("scope   ", theme.dim()),
        Span::styled("enter ", theme.faint_style()),
        Span::styled("open   ", theme.dim()),
        Span::styled("esc ", theme.faint_style()),
        Span::styled("close", theme.dim()),
    ]);
    frame.render_widget(Paragraph::new(hints), hint);
}

/// The theme picker. Each row is painted in its own theme, so the list is the
/// preview — and the page behind it is repainted live as the cursor moves.
fn themes(frame: &mut Frame, app: &App, area: Rect, selected: usize) {
    use crate::theme::Flavor;

    let theme = &app.theme;
    let all = Flavor::ALL;
    let height = (all.len() as u16 + 3).min(area.height.saturating_sub(4)).max(6);
    let box_area = crate::ui::centered(area, 52.min(area.width), height);
    let inner = popup(frame, theme, box_area, "theme");
    if inner.is_empty() {
        return;
    }

    let [list, hint] = Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).areas(inner);
    let rows = list.height as usize;
    let start = selected.saturating_sub(rows / 2).min(all.len().saturating_sub(rows.max(1)));

    let lines: Vec<Line> = all
        .iter()
        .enumerate()
        .skip(start)
        .take(rows)
        .map(|(i, flavor)| {
            let swatch = Theme::new(*flavor);
            let on = i == selected;
            let name = Style::default().fg(if on { theme.accent } else { theme.text });
            let name = if on { name.add_modifier(Modifier::BOLD) } else { name };
            Line::from(vec![
                Span::styled(if on { "▌ " } else { "  " }, Style::default().fg(theme.accent)),
                Span::styled(format!("{:<22}", flavor.display_name()), name),
                // A strip of the theme's own colours, painted in that theme.
                Span::styled("  ", Style::default().bg(swatch.bg)),
                Span::styled("  ", Style::default().bg(swatch.surface)),
                Span::styled("  ", Style::default().bg(swatch.accent)),
                Span::styled("  ", Style::default().bg(swatch.ok)),
                Span::styled("  ", Style::default().bg(swatch.warn)),
                Span::styled("  ", Style::default().bg(swatch.err)),
                Span::styled(" Aa", Style::default().fg(swatch.text).bg(swatch.bg)),
            ])
        })
        .collect();
    frame.render_widget(Paragraph::new(lines), list);

    let hints = Line::from(vec![
        Span::styled("j/k ", theme.faint_style()),
        Span::styled("preview   ", theme.dim()),
        Span::styled("enter ", theme.faint_style()),
        Span::styled("keep   ", theme.dim()),
        Span::styled("esc ", theme.faint_style()),
        Span::styled("cancel", theme.dim()),
    ]);
    frame.render_widget(Paragraph::new(hints), hint);
}

fn palette(frame: &mut Frame, app: &App, area: Rect) {
    let Some(Overlay::Palette(state)) = app.overlay.as_ref() else { return };
    let theme = &app.theme;
    let height = (state.items.len() as u16 + 4).min(20);
    let box_area = crate::ui::centered(area, area.width.saturating_sub(20).min(76), height);
    let inner = popup(frame, theme, box_area, "commands");
    if inner.is_empty() {
        return;
    }

    let [query, list] = Layout::vertical([Constraint::Length(2), Constraint::Min(1)]).areas(inner);
    frame.render_widget(Paragraph::new(query_line(theme, "› ", &state.query)), query);

    let height = list.height as usize;
    let start = state.selected.saturating_sub(height / 3).min(state.items.len().saturating_sub(height.max(1)));
    let lines: Vec<Line> = state
        .items
        .iter()
        .enumerate()
        .skip(start)
        .take(height)
        .map(|(i, (_, title, keys))| {
            let on = i == state.selected;
            let base = if on { theme.selection(true) } else { Style::default().fg(theme.text) };
            Line::from(vec![
                Span::styled(if on { "▌ " } else { "  " }, Style::default().fg(theme.accent)),
                Span::styled(format!("{title:<28}"), base),
                Span::styled(keys.clone(), theme.faint_style()),
            ])
        })
        .collect();
    frame.render_widget(Paragraph::new(lines), list);
}

fn help(frame: &mut Frame, app: &App, area: Rect, scroll: usize) {
    let theme = &app.theme;
    let rows = crate::keymap::help_rows();
    let box_area = crate::ui::centered(area, area.width.saturating_sub(10).min(84), area.height.saturating_sub(4).min(30));
    let inner = popup(frame, theme, box_area, "keys");
    if inner.is_empty() {
        return;
    }

    let mut lines: Vec<Line> = Vec::new();
    let mut group = "";
    for (g, keys, title) in &rows {
        if *g != group {
            if !lines.is_empty() {
                lines.push(Line::from(""));
            }
            lines.push(Line::from(Span::styled(
                g.to_uppercase(),
                Style::default().fg(theme.accent).add_modifier(Modifier::BOLD),
            )));
            group = g;
        }
        lines.push(Line::from(vec![
            Span::styled(format!("  {keys:<22}"), Style::default().fg(theme.literal)),
            Span::styled(title.to_string(), Style::default().fg(theme.text)),
        ]));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "  ctrl+space and every other key go to herdr while its pane has focus.",
        theme.dim(),
    )));

    let max = lines.len().saturating_sub(inner.height as usize);
    frame.render_widget(Paragraph::new(lines).scroll((scroll.min(max) as u16, 0)), inner);
}

fn outline(frame: &mut Frame, app: &App, area: Rect, selected: usize) {
    let Some(open) = app.open.as_ref() else { return };
    let theme = &app.theme;
    let height = (open.doc.headings.len() as u16 + 2).min(24);
    let box_area = crate::ui::centered(area, area.width.saturating_sub(20).min(70), height);
    let inner = popup(frame, theme, box_area, "outline");
    if inner.is_empty() {
        return;
    }
    let selected = selected.min(open.doc.headings.len().saturating_sub(1));
    // Indented by level: the outline is there to show the shape of the page,
    // and a flat list of titles shows only its length.
    let lines: Vec<Line> = open
        .doc
        .headings
        .iter()
        .enumerate()
        .take(inner.height as usize)
        .map(|(i, (_, level, text))| {
            let on = i == selected;
            let style = if on { theme.selection(true) } else { theme.heading(*level) };
            let indent = "  ".repeat(level.saturating_sub(1) as usize);
            Line::from(Span::styled(format!(" {indent}{text} "), style))
        })
        .collect();
    frame.render_widget(Paragraph::new(lines), inner);
}

fn repo_config(frame: &mut Frame, app: &App, area: Rect) {
    let Some(Overlay::RepoConfig(state)) = app.overlay.as_ref() else { return };
    let theme = &app.theme;
    let height = if state.name == "sources" { 14 } else { 13 };
    let box_area = crate::ui::centered(area, area.width.saturating_sub(16).min(72), height);
    let inner = popup(frame, theme, box_area, &format!("configure {}", state.name));
    if inner.is_empty() {
        return;
    }

    let current = app.cfg.repo_url(&state.name).unwrap_or("local-only");
    let path = app.cfg.root.join(&state.name);
    let mut options = vec![
        ("git URL", "point this collection at another remote"),
        ("local only", "keep a git repo here with no origin"),
    ];
    if state.name == "sources" {
        options.push(("Google Drive", "no local sources/ checkout"));
    }

    let mut lines = vec![
        Line::from(Span::styled(
            format!("current  {current}"),
            theme.dim(),
        )),
        Line::from(Span::styled(format!("path     {}", path.display()), theme.faint_style())),
        Line::from(""),
    ];
    for (i, (label, hint)) in options.iter().enumerate() {
        let on = i == state.selected;
        let style = if on { theme.selection(true) } else { Style::default().fg(theme.text) };
        lines.push(Line::from(vec![
            Span::styled(if on { "▌ " } else { "  " }, Style::default().fg(theme.accent)),
            Span::styled(format!("{label:<14}"), style.add_modifier(Modifier::BOLD)),
            Span::styled(*hint, if on { style } else { theme.faint_style() }),
        ]));
    }
    lines.push(Line::from(""));
    if state.selected == 0 {
        lines.push(query_line(theme, "› ", &state.url));
    } else {
        lines.push(Line::from(Span::styled(
            "enter applies the selected option",
            theme.faint_style(),
        )));
    }
    lines.push(Line::from(Span::styled(
        "↑↓ choose · enter apply · esc cancel",
        theme.faint_style(),
    )));
    frame.render_widget(Paragraph::new(lines), inner);
}

/// A right-click context menu, drawn at the popup rectangle the app recorded
/// when the menu was opened. The title is the row's path so the target is never
/// ambiguous, and a trailing hint answers the only two questions a context menu
/// invites: what keys, and how to leave.
fn menu(frame: &mut Frame, app: &App) {
    let Some(Overlay::Menu(menu)) = app.overlay.as_ref() else { return };
    let theme = &app.theme;
    if menu.area.is_empty() {
        return;
    }
    let title = crate::vault::page::rel_path(&menu.path, &app.cfg.root);
    let inner = popup(frame, theme, menu.area, &title);
    if inner.is_empty() {
        return;
    }
    let height = inner.height.saturating_sub(1) as usize;
    let lines: Vec<Line> = menu
        .items
        .iter()
        .enumerate()
        .take(height)
        .map(|(i, (_, label))| {
            let on = i == menu.selected;
            let style = if on { theme.selection(true) } else { Style::default().fg(theme.text) };
            Line::from(vec![
                Span::styled(if on { "▌ " } else { "  " }, Style::default().fg(theme.accent)),
                Span::styled(label.to_string(), style),
            ])
        })
        .collect();
    frame.render_widget(Paragraph::new(lines), inner);

    let hint = Line::from(Span::styled("↑↓ choose · enter run · esc close", theme.faint_style()));
    frame.render_widget(Paragraph::new(hint), Rect {
        x: inner.x,
        y: inner.y + inner.height.saturating_sub(1),
        width: inner.width,
        height: 1,
    });
}

fn prompt(frame: &mut Frame, app: &App, area: Rect) {
    let Some(Overlay::Prompt(state)) = app.overlay.as_ref() else { return };
    let theme = &app.theme;
    let box_area = crate::ui::centered(area, area.width.saturating_sub(20).min(80), 5);
    let inner = popup(frame, theme, box_area, &state.title);
    if inner.is_empty() {
        return;
    }
    let lines = vec![
        Line::from(""),
        query_line(theme, "› ", &state.value),
        Line::from(Span::styled("enter to confirm · esc to cancel", theme.faint_style())),
    ];
    frame.render_widget(Paragraph::new(lines), inner);
}

/// Toasts stack from the bottom-right, above the status line.
pub fn toasts(frame: &mut Frame, app: &App, area: Rect) {
    if app.toasts.is_empty() || area.width < 30 {
        return;
    }
    let theme = &app.theme;
    let width = area.width.saturating_sub(4).min(72);
    let mut y = area.y + area.height;

    for toast in app.toasts.iter().rev() {
        let colour = match toast.level {
            Level::Info => theme.link,
            Level::Good => theme.ok,
            Level::Warn => theme.warn,
            Level::Bad => theme.err,
        };
        let text = Paragraph::new(Line::from(Span::styled(toast.text.clone(), Style::default().fg(theme.text))))
            .wrap(Wrap { trim: true });
        let lines = (toast.text.len() as u16).div_ceil(width.saturating_sub(4).max(1)).max(1);
        let height = lines + 2;
        if y < area.y + height {
            break;
        }
        y -= height;
        let rect = Rect { x: area.x + area.width.saturating_sub(width + 2), y, width, height };
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(colour))
            .style(Style::default().bg(theme.overlay))
            .padding(Padding::horizontal(1));
        let inner = block.inner(rect);
        frame.render_widget(Clear, rect);
        frame.render_widget(block, rect);
        frame.render_widget(text, inner);
    }
}
