//! Rendering.
//!
//! The only thing drawing writes back to the app is `Areas` — the geometry it
//! just measured — because the pty needs to be resized to the box it is drawn
//! into and the mouse needs to know what it clicked.

pub mod csv;
pub mod highlight;
pub mod markdown;
pub mod mermaid;
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
    app.areas = split_body(body, app);

    if app.open.as_ref().and_then(|o| o.editor.as_ref()).is_none() {
        let badge_len = format!(" {} ▾ ", app.project_name).chars().count() as u16;
        app.areas.project_selector = Some(Rect::new(status.x, status.y, badge_len, 1));
    } else {
        app.areas.project_selector = None;
    }

    // Sizing the child to the box it is about to be drawn into keeps the two in
    // step even while the terminal is being dragged.
    app.sync_sidebar();

    if !app.areas.tab_bar.is_empty() {
        panes::tab_bar(frame, app);
    }
    if !app.areas.tree.is_empty() {
        let tree_area = app.areas.tree;
        panes::tree(frame, app, tree_area);
    }
    if !app.areas.doc.is_empty() {
        panes::document(frame, app, app.areas.doc);
    }
    if !app.areas.sidebar.is_empty() {
        panes::sidebar(frame, app, app.areas.sidebar);
    }
    panes::toggles(frame, app);
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
    // Below the threshold, panes stop sitting side by side and a tab strip
    // takes their place as the way to switch which one is showing.
    let (tab_bar, body) = if narrow && body.height > 1 {
        let [bar, rest] = Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).areas(body);
        (tab_layout(bar), rest)
    } else {
        (Vec::new(), body)
    };
    // Below the threshold the panes stop sharing the row entirely: the tab
    // strip is the only way to switch, so exactly one pane is showing at a
    // time and it gets the full width. Splitting here would leave the reader
    // in a column too thin to read beside a tree it did not ask to see.
    if narrow {
        let focus = match app.focus {
            Focus::Tree if !app.show_tree => Focus::Doc,
            Focus::Sidebar if !app.show_sidebar => Focus::Doc,
            other => other,
        };
        let mut areas = match focus {
            Focus::Tree => Areas { tree: body, ..Default::default() },
            Focus::Sidebar => Areas { sidebar: body, ..Default::default() },
            Focus::Doc => Areas { doc: body, ..Default::default() },
        };
        areas.tab_bar = tab_bar;
        return areas;
    }

    let show_tree = app.show_tree;
    let show_sidebar = app.show_sidebar;

    let min_doc = 30u16;
    let min_pane = 12u16;
    let available = body.width.saturating_sub(min_doc);

    let (tree_w, sidebar_w) = match (show_tree, show_sidebar) {
        (true, false) => (app.cfg.tree_width.clamp(min_pane, available.max(min_pane)), 0),
        (false, true) => (0, app.cfg.sidebar_width.clamp(min_pane, available.max(min_pane))),
        (true, true) => {
            if available < min_pane * 2 {
                (min_pane, min_pane)
            } else if app.cfg.tree_width + app.cfg.sidebar_width <= available {
                (app.cfg.tree_width.max(min_pane), app.cfg.sidebar_width.max(min_pane))
            } else {
                match app.focus {
                    Focus::Tree => {
                        let t = app.cfg.tree_width.clamp(min_pane, available.saturating_sub(min_pane));
                        let s = available.saturating_sub(t).max(min_pane);
                        (t, s)
                    }
                    Focus::Sidebar => {
                        let s = app.cfg.sidebar_width.clamp(min_pane, available.saturating_sub(min_pane));
                        let t = available.saturating_sub(s).max(min_pane);
                        (t, s)
                    }
                    _ => {
                        // Both side panes want more than there is, so split the
                        // shortfall in proportion to what each asked for.
                        let sum = app.cfg.tree_width as u32 + app.cfg.sidebar_width as u32;
                        let t = (app.cfg.tree_width as u32 * available as u32)
                            .checked_div(sum)
                            .map_or(available / 2, |width| width as u16);
                        let t = t.clamp(min_pane, available.saturating_sub(min_pane));
                        let s = available.saturating_sub(t).max(min_pane);
                        (t, s)
                    }
                }
            }
        }
        (false, false) => (0, 0),
    };

    let mut constraints = Vec::new();
    if show_tree {
        constraints.push(Constraint::Length(tree_w));
    }
    constraints.push(Constraint::Min(min_doc));
    if show_sidebar {
        constraints.push(Constraint::Length(sidebar_w));
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
    // Panes share a border column, so the divider is the last column of the
    // pane on its left. That is the column the pointer has to hit.
    areas.tree_divider = show_tree.then(|| areas.tree.x + areas.tree.width - 1);
    areas.sidebar_divider = show_sidebar.then_some(areas.sidebar.x);

    // A collapse/expand handle always sits on whichever border the pointer
    // would actually see — the pane's own divider when open, or the
    // document's outer edge when there is no pane left to divide against.
    let mid = |r: Rect| r.y + r.height / 2;
    areas.tree_toggle = if show_tree {
        areas.tree_divider.map(|x| Rect::new(x, mid(areas.tree), 1, 1))
    } else if !areas.doc.is_empty() {
        Some(Rect::new(areas.doc.x, mid(areas.doc), 1, 1))
    } else {
        None
    };
    areas.sidebar_toggle = if show_sidebar {
        areas.sidebar_divider.map(|x| Rect::new(x, mid(areas.sidebar), 1, 1))
    } else if !areas.doc.is_empty() {
        Some(Rect::new(areas.doc.x + areas.doc.width - 1, mid(areas.doc), 1, 1))
    } else {
        None
    };
    areas.tab_bar = tab_bar;
    areas
}

/// Lay out one clickable, labelled rect per pane across `bar`, evenly split.
/// Tree and Agents are included whether or not they are currently open —
/// a hidden pane's tab is still a legitimate switch target, it just reopens
/// the pane on click (`App::select_tab`).
fn tab_layout(bar: Rect) -> Vec<(Focus, &'static str, Rect)> {
    const TABS: [(Focus, &str); 3] =
        [(Focus::Tree, "tree"), (Focus::Doc, "page"), (Focus::Sidebar, "agent")];
    if bar.width == 0 {
        return Vec::new();
    }
    let n = TABS.len() as u16;
    let width = bar.width / n;
    let mut x = bar.x;
    TABS.iter()
        .enumerate()
        .map(|(i, (focus, label))| {
            // The last tab absorbs the remainder so the strip always fills
            // the full width, instead of leaving a sliver on the right.
            let w = if i as u16 == n - 1 { bar.x + bar.width - x } else { width };
            let rect = Rect::new(x, bar.y, w, 1);
            x += w;
            (*focus, *label, rect)
        })
        .collect()
}

/// How close to a divider a click counts as grabbing it. One column is a cruel
/// target with a mouse; three is comfortable and still unambiguous.
pub const GRAB: u16 = 1;

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
    fn a_narrow_terminal_shows_exactly_the_focused_pane_and_nothing_else() {
        let mut a = app_in("narrow");
        a.focus = Focus::Doc;
        let areas = split_body(Rect::new(0, 0, 80, 40), &a);
        assert!(areas.tree.is_empty());
        assert!(areas.sidebar.is_empty());
        assert_eq!(areas.doc.width, 80);

        a.focus = Focus::Tree;
        let areas = split_body(Rect::new(0, 0, 80, 40), &a);
        assert_eq!(areas.tree.width, 80);
        assert!(areas.doc.is_empty(), "the page does not sit beside the tree when narrow");
        assert!(areas.sidebar.is_empty());

        a.focus = Focus::Sidebar;
        let areas = split_body(Rect::new(0, 0, 80, 40), &a);
        assert_eq!(areas.sidebar.width, 80);
        assert!(areas.doc.is_empty());
        assert!(areas.tree.is_empty());
    }

    #[test]
    fn a_narrow_terminal_falls_back_to_the_page_when_the_focused_pane_is_closed() {
        let mut a = app_in("narrow-closed");
        a.focus = Focus::Tree;
        a.show_tree = false;
        let areas = split_body(Rect::new(0, 0, 80, 40), &a);
        assert_eq!(areas.doc.width, 80);
        assert!(areas.tree.is_empty());
    }

    #[test]
    fn a_narrow_terminal_draws_no_dividers_or_handles() {
        let mut a = app_in("narrow-handles");
        a.focus = Focus::Doc;
        let areas = split_body(Rect::new(0, 0, 80, 40), &a);
        assert!(areas.tree_divider.is_none() && areas.sidebar_divider.is_none());
        assert!(areas.tree_toggle.is_none() && areas.sidebar_toggle.is_none());
    }

    #[test]
    fn a_wide_terminal_has_no_tab_bar() {
        let a = app_in("wide-no-tabs");
        let areas = split_body(Rect::new(0, 0, 160, 40), &a);
        assert!(areas.tab_bar.is_empty());
    }

    #[test]
    fn a_narrow_terminal_gets_a_tab_bar_above_a_shrunk_body() {
        let mut a = app_in("narrow-tabs");
        a.focus = Focus::Doc;
        let areas = split_body(Rect::new(0, 0, 80, 40), &a);
        assert_eq!(areas.tab_bar.len(), 3);
        assert_eq!(areas.doc.height, 39, "the tab row is carved out of the body, not overlaid");
        assert_eq!(areas.doc.y, areas.tab_bar[0].2.y + 1);

        let labels: Vec<&str> = areas.tab_bar.iter().map(|(_, l, _)| *l).collect();
        assert_eq!(labels, ["tree", "page", "agent"]);

        let total: u16 = areas.tab_bar.iter().map(|(_, _, r)| r.width).sum();
        assert_eq!(total, 80, "the tabs fill the whole width, no leftover sliver");
    }

    #[test]
    fn the_tree_handle_sits_on_its_own_border_when_open_and_on_the_documents_edge_when_closed() {
        let mut a = app_in("handle-tree");
        let areas = split_body(Rect::new(0, 0, 160, 40), &a);
        assert_eq!(areas.tree_toggle.unwrap().x, areas.tree_divider.unwrap());

        a.show_tree = false;
        let areas = split_body(Rect::new(0, 0, 160, 40), &a);
        assert_eq!(areas.tree_toggle.unwrap().x, areas.doc.x, "reopens from the document's own left edge");
    }

    #[test]
    fn the_sidebar_handle_sits_on_its_own_border_when_open_and_on_the_documents_edge_when_closed() {
        let mut a = app_in("handle-sidebar");
        let areas = split_body(Rect::new(0, 0, 160, 40), &a);
        assert_eq!(areas.sidebar_toggle.unwrap().x, areas.sidebar_divider.unwrap());

        a.show_sidebar = false;
        let areas = split_body(Rect::new(0, 0, 160, 40), &a);
        assert_eq!(
            areas.sidebar_toggle.unwrap().x,
            areas.doc.x + areas.doc.width - 1,
            "reopens from the document's own right edge"
        );
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
        assert!(areas.doc.width >= 30, "document keeps at least 30 columns");
        assert!(areas.tree.width > 160 / 3, "relaxed layout caps allow wider sidebars");
    }

    #[test]
    fn centering_clamps_to_the_available_area() {
        let area = Rect::new(0, 0, 40, 10);
        assert_eq!(centered(area, 20, 4), Rect::new(10, 3, 20, 4));
        assert_eq!(centered(area, 200, 200), area);
    }
}
