//! Rendering.
//!
//! The only thing drawing writes back to the app is `Areas` — the geometry it
//! just measured — because the pty needs to be resized to the box it is drawn
//! into and the mouse needs to know what it clicked.

pub mod markdown;
pub mod overlays;
pub mod panes;

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::Frame;

use crate::app::{App, Areas, Focus};

/// Below this width the tree and sidebar are not worth the columns they cost.
const NARROW: u16 = 100;

pub fn draw(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    frame.render_widget(ratatui::widgets::Block::default().style(app.theme.base()), area);

    let [body, status] = Layout::vertical([Constraint::Min(3), Constraint::Length(1)]).areas(area);
    let areas = split_body(body, app);
    app.areas = areas;

    // Sizing the child to the box it is about to be drawn into keeps the two in
    // step even while the terminal is being dragged.
    app.sync_sidebar();

    if !areas.tree.is_empty() {
        panes::tree(frame, app, areas.tree);
    }
    if !areas.doc.is_empty() {
        panes::document(frame, app, areas.doc);
    }
    if !areas.sidebar.is_empty() {
        panes::sidebar(frame, app, areas.sidebar);
    }
    panes::status(frame, app, status);
    overlays::toasts(frame, app, body);
    overlays::draw(frame, app, area);
}

fn split_body(body: Rect, app: &App) -> Areas {
    // Zoom is a reading mode: one pane, full width, nothing else competing.
    if app.zoom {
        return match app.focus {
            Focus::Tree => Areas { tree: body, ..Default::default() },
            Focus::Sidebar => Areas { sidebar: body, ..Default::default() },
            Focus::Doc => Areas { doc: body, ..Default::default() },
        };
    }

    let narrow = body.width < NARROW;
    // On a narrow terminal only the focused side pane survives, so the reader
    // never gets squeezed into a column too thin to read.
    let show_tree = app.show_tree && (!narrow || app.focus == Focus::Tree);
    let show_sidebar = app.show_sidebar && (!narrow || app.focus == Focus::Sidebar);

    let mut constraints = Vec::new();
    if show_tree {
        constraints.push(Constraint::Length(app.cfg.tree_width.min(body.width / 3)));
    }
    constraints.push(Constraint::Min(30));
    if show_sidebar {
        constraints.push(Constraint::Length(app.cfg.sidebar_width.min(body.width / 2)));
    }

    let chunks = Layout::horizontal(constraints).split(body);
    let mut next = 0;
    let mut areas = Areas::default();
    if show_tree {
        areas.tree = chunks[next];
        next += 1;
    }
    areas.doc = chunks[next];
    next += 1;
    if show_sidebar {
        areas.sidebar = chunks[next];
    }
    areas
}

/// Centre a box of the given size inside `area`, clamped to fit.
pub fn centered(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width);
    let height = height.min(area.height);
    Rect {
        x: area.x + (area.width - width) / 2,
        y: area.y + (area.height - height) / 2,
        width,
        height,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Each test gets its own directory: these run in parallel, and a shared one
    // is a race, not a fixture.
    fn app_in(name: &str) -> App {
        let dir = std::env::temp_dir().join(format!("podarcis-ui-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
        std::fs::write(dir.join("AGENTS.md"), "x").unwrap();
        std::fs::write(dir.join(".podarcis/config.yaml"), "").unwrap();
        std::fs::create_dir_all(dir.join("wiki")).unwrap();
        crate::app::test_app(&dir).0
    }

    #[test]
    fn a_wide_terminal_shows_all_three_panes() {
        let a = app_in("wide");
        let areas = split_body(Rect::new(0, 0, 160, 40), &a);
        assert!(!areas.tree.is_empty() && !areas.doc.is_empty() && !areas.sidebar.is_empty());
        assert_eq!(areas.tree.width + areas.doc.width + areas.sidebar.width, 160);
    }

    #[test]
    fn a_narrow_terminal_keeps_only_the_focused_side_pane() {
        let mut a = app_in("narrow");
        a.focus = Focus::Doc;
        let areas = split_body(Rect::new(0, 0, 80, 40), &a);
        assert!(areas.tree.is_empty());
        assert!(areas.sidebar.is_empty());
        assert_eq!(areas.doc.width, 80);

        a.focus = Focus::Tree;
        let areas = split_body(Rect::new(0, 0, 80, 40), &a);
        assert!(!areas.tree.is_empty());
        assert!(areas.sidebar.is_empty());
    }

    #[test]
    fn zoom_gives_the_focused_pane_everything() {
        let mut a = app_in("zoom");
        a.zoom = true;
        a.focus = Focus::Doc;
        let areas = split_body(Rect::new(0, 0, 160, 40), &a);
        assert_eq!(areas.doc.width, 160);
        assert!(areas.tree.is_empty() && areas.sidebar.is_empty());
    }

    #[test]
    fn side_panes_never_take_more_than_their_share() {
        let mut a = app_in("share");
        a.cfg.tree_width = 80;
        a.cfg.sidebar_width = 80;
        let areas = split_body(Rect::new(0, 0, 160, 40), &a);
        assert!(areas.tree.width <= 160 / 3);
        assert!(areas.doc.width >= 30);
    }

    #[test]
    fn centering_clamps_to_the_available_area() {
        let area = Rect::new(0, 0, 40, 10);
        assert_eq!(centered(area, 20, 4), Rect::new(10, 3, 20, 4));
        assert_eq!(centered(area, 200, 200), area);
    }
}
