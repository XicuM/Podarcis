//! Application state and the command dispatcher.
//!
//! Everything the user can do arrives here as a `Cmd` from the keymap or an
//! overlay, so there is one list of behaviours and one place they are
//! implemented. Rendering reads this state and writes nothing back except the
//! pane geometry it just measured.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::{Constraint, Layout, Rect};

use crate::actions::{JobResult, JobRunner, NativeOutcome};
use crate::config::Config;
use crate::control::{Command as Control, Request};
use crate::editor::{Editor, Save};
use crate::event::AppEvent;
use crate::herdr;
use crate::keymap::{self, Cmd, Ctx, Resolved};
use crate::platform;
use crate::search::{self, Hit};
use crate::theme::{Flavor, Theme};
use crate::ui::markdown::{self, Mark};
use crate::vault::git::{GitMap, TrackState};
use crate::vault::index::Index;
use crate::vault::links::LinkKind;
use crate::vault::page::Page;
use crate::vault::tree::Tree;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Focus {
    Tree,
    Doc,
    Sidebar,
}

/// Which box inside `Focus::Doc` currently has the cursor: the reader itself,
/// or the sources/backlinks inspector below it. The two are drawn as separate
/// bordered panes, so Tab needs to be able to stop at each in turn instead of
/// treating the whole column as one.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DocStop {
    Content,
    Sources,
}

/// One Tab-cycle stop: either a specific collection box in the tree column,
/// one of the two boxes in the doc column, or the agents pane. Distinct from
/// `Focus`, which only tracks the coarse column — this is what lets each
/// visible bordered box get its own stop instead of grouping a whole column.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stop {
    TreeSection(usize),
    DocContent,
    DocSources,
    Sidebar,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Info,
    Good,
    Warn,
    Bad,
}

pub struct Toast {
    pub text: String,
    pub level: Level,
    pub at: Instant,
}

/// How long a toast stays up. Long enough to read a sentence, short enough that
/// it never becomes furniture.
const TOAST_TTL: Duration = Duration::from_secs(6);

/// Narrowest a side pane may be dragged. Below this it is a sliver that can
/// only be widened again by luck.
const MIN_PANE: u16 = 12;

/// How long a single click stays crouched, ready to become a double click.
const DOUBLE_CLICK: Duration = Duration::from_millis(400);

pub struct Open {
    pub page: Page,
    pub doc: markdown::Doc,
    pub doc_width: u16,
    pub scroll: usize,
    /// Display columns panned off the left edge. Non-zero only for a table too
    /// wide for the pane — a CSV with more columns than fit, or a broad
    /// markdown table.
    pub hscroll: usize,
    pub inspect_scroll: usize,
    pub link: Option<usize>,
    pub editor: Option<Editor>,
    pub selection: Option<markdown::Selection>,
    /// A citation clicked (or navigated to) in the body, highlighted in the
    /// inspector's sources section.
    pub selected_citation: Option<String>,
    /// Set alongside `selected_citation` when the selection came from a fresh
    /// click, so the inspector scrolls it into view exactly once — a later
    /// manual scroll away from it is left alone.
    pub citation_scroll_pending: bool,
}

/// The in-page find bar — `ctrl+f`. Scoped to the open page, unlike the
/// finder's text mode, which sweeps the whole vault.
///
/// Only the query and which hit is current live here: the hits themselves are
/// recomputed from the rendered `Doc` on every use. A page is small enough that
/// this costs nothing, and it means a resize — which rewraps, moving every
/// rendered column — cannot leave a highlight pointing at the wrong text.
pub struct PageFind {
    pub query: String,
    pub current: usize,
}

/// Files the reader never parses: they are handed to the system's default
/// viewer instead. A PDF and an image are both binary, so rendering either
/// one as markdown would only paint mojibake.
pub fn is_external(path: &Path) -> bool {
    matches!(
        path.extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase).as_deref(),
        Some("pdf" | "png" | "jpg" | "jpeg")
    )
}

impl Open {
    fn load(path: &Path, root: &Path, width: u16, theme: &Theme) -> Option<Self> {
        let page = Page::load(path, root).ok()?;
        let doc = if Self::is_csv(path) {
            crate::ui::csv::Grid::parse(&page.body).to_doc(width, theme)
        } else {
            markdown::render(&page.body, width, theme)
        };
        Some(Self {
            page,
            doc,
            doc_width: width,
            scroll: 0,
            hscroll: 0,
            inspect_scroll: 0,
            link: None,
            editor: None,
            selection: None,
            selected_citation: None,
            citation_scroll_pending: false,
        })
    }

    pub(crate) fn is_csv(path: &Path) -> bool {
        path.extension().and_then(|e| e.to_str()) == Some("csv")
    }

    /// A CSV renders as a table rather than markdown — navigating one is the
    /// same reader, but its `doc` is a box-drawn grid of the raw file.
    pub fn csv(&self) -> bool {
        Self::is_csv(&self.page.path)
    }

    pub(crate) fn reflow(&mut self, width: u16, theme: &Theme) {
        if width == self.doc_width {
            return;
        }
        self.selection = None;
        let anchor = self.doc.source_for_line(self.scroll);
        self.doc = if self.csv() {
            crate::ui::csv::Grid::parse(&self.page.body).to_doc(width, theme)
        } else {
            markdown::render(&self.page.body, width, theme)
        };
        self.doc_width = width;
        self.hscroll = 0;
        self.scroll = self.doc.line_for_source(anchor);
    }

    pub fn editing(&self) -> bool {
        self.editor.is_some()
    }

    /// Raw source lines touched by a rendered selection.
    ///
    /// Rendering strips markdown syntax and rewraps prose, so there is no exact
    /// column mapping back to source text; instead this resolves every source
    /// line any part of the selection's rendered lines came from and returns
    /// them verbatim, syntax included.
    pub fn selected_source_text(&self, sel: markdown::Selection) -> String {
        let (start, end) = sel.range();
        if start == end {
            return String::new();
        }
        let start_src = self.doc.source_for_line(start.line);
        let end_src = self.doc.source_for_line(end.line);
        self.page
            .body
            .lines()
            .skip(start_src)
            .take(end_src.saturating_sub(start_src) + 1)
            .collect::<Vec<_>>()
            .join("\n")
    }
}

pub struct Finder {
    pub mode: search::Mode,
    pub query: String,
    pub hits: Vec<Hit>,
    pub selected: usize,
    pub collection: Option<&'static str>,
    /// Job id of an in-flight semantic search.
    pub running: Option<u64>,
    pub warning: Option<String>,
}

pub struct Palette {
    pub query: String,
    pub items: Vec<(Cmd, &'static str, String)>,
    pub selected: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PromptKind {
    Commit,
    NewPage,
    Rename,
    NewProject,
    RenameProject,
    DeleteProject,
}

pub struct Prompt {
    pub kind: PromptKind,
    pub title: String,
    pub value: String,
    /// The file or folder a rename prompt applies to.
    pub path: Option<PathBuf>,
}

pub struct RepoConfig {
    pub name: String,
    /// 0 = apply git URL, 1 = local-only, 2 = gdrive (sources only).
    pub selected: usize,
    pub url: String,
}

impl RepoConfig {
    pub fn option_count(&self) -> usize {
        if self.name == "sources" {
            3
        } else {
            2
        }
    }
}

/// A question an agent put to the user, and the request still waiting on the
/// answer. Holding the request here is what makes the agent's CLI call block
/// until a key is pressed.
pub struct Ask {
    pub question: String,
    pub options: Vec<String>,
    pub selected: usize,
    pub request: Request,
}

pub enum Overlay {
    Finder(Finder),
    Palette(Palette),
    Help { scroll: usize },
    Outline { selected: usize },
    Prompt(Prompt),
    RepoConfig(RepoConfig),
    /// A right-click context menu on a tree row.
    Menu(Menu),
    /// The theme picker. `original` is restored if the picker is cancelled, so
    /// browsing twenty themes never costs you the one you had.
    Themes { selected: usize, original: Flavor },
    /// The project switcher. Name and path only: the registry's description
    /// field is bookkeeping, not something worth a column in a switcher.
    Projects { selected: usize, items: Vec<(String, PathBuf)> },
    /// A question from an agent on the control socket.
    Ask(Ask),
    /// The apm extensions browser — skills and MCP servers.
    Extensions(Extensions),
}

/// One action `apm` can be asked to run on the selected extension.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExtensionAction {
    Install,
    Update,
    Uninstall,
}

impl ExtensionAction {
    fn verb(self) -> &'static str {
        match self {
            ExtensionAction::Install => "install",
            ExtensionAction::Update => "update",
            ExtensionAction::Uninstall => "uninstall",
        }
    }
}

pub struct Extensions {
    pub selected: usize,
    pub items: Vec<crate::extensions::ExtensionItem>,
    /// Raw stdout/stderr of the last `apm` invocation — shown verbatim, since
    /// apm's own output isn't machine-parseable.
    pub log: Option<String>,
    /// Uninstalling is destructive, so it is gated behind one extra keypress,
    /// the same way tree-row delete is (`MenuAction::Delete`).
    pub confirm: Option<ExtensionAction>,
}

/// One action from the tree's right-click menu. `Delete` opens a confirmation
/// menu whose only committing item is `ConfirmDelete`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MenuAction {
    Open,
    NewPage,
    Rename,
    Delete,
    ConfirmDelete,
    CopyRelative,
    CopyAbsolute,
    Cancel,
}

/// The context menu shown for the tree row under a right-click. Kept as its own
/// overlay rather than a generic list because it carries the row's path and the
/// popup rect the mouse resolves clicks against.
pub struct Menu {
    /// The row the menu was opened for; a confirmation menu keeps it untouched.
    pub path: PathBuf,
    pub items: Vec<(MenuAction, &'static str)>,
    pub selected: usize,
    /// Popup rectangle, computed from the click position and the layout.
    pub area: Rect,
    /// Row the mouse pressed down on, so a release on the same row activates.
    pub pressed: Option<usize>,
}

/// One collection's box inside the tree column, for drawing and mouse hits.
#[derive(Clone, Copy, Debug, Default)]
pub struct TreePane {
    pub area: Rect,
    pub inner: Rect,
    /// Index of the collection header row in `Tree::rows`.
    pub header: usize,
    /// First child row (header + 1). Equal to `end` when collapsed.
    pub start: usize,
    /// Exclusive end of this collection's rows.
    pub end: usize,
    pub offset: usize,
    /// The one cell that folds this collection away, or brings it back.
    pub fold: Option<Rect>,
    pub folded: bool,
}

/// A clickable backlink in the inspector pane.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InspectorBacklink {
    pub row: usize,
    pub col_start: u16,
    pub col_end: u16,
    pub path: PathBuf,
}

/// Geometry from the last frame, so mouse events and the pty know where things
/// are. Written by the renderer, read by the app.
#[derive(Clone, Debug, Default)]
pub struct Areas {
    pub tree: Rect,
    pub doc: Rect,
    pub doc_main: Rect,
    pub doc_body: Rect,
    pub sidebar: Rect,
    pub inspector: Rect,
    /// Inner content rect of the inspector, borders and padding excluded —
    /// what a click's `(x, y)` needs to turn into a row index.
    pub inspector_body: Rect,
    /// The citation id behind each currently visible inspector row (after
    /// wrapping and scrolling), aligned to `inspector_body`, so a click can be
    /// mapped back to a source without re-deriving the layout.
    pub inspector_rows: Vec<Option<String>>,
    /// Clickable backlinks in the visible inspector rows.
    pub inspector_backlinks: Vec<InspectorBacklink>,
    /// Hit-testing for each collection pane inside the tree column.
    pub tree_panes: Vec<TreePane>,
    /// Boundaries between stacked collection boxes, as `(index of the box
    /// above, the row they meet on)`. The last box ends at the column's edge
    /// and so has none.
    pub collection_dividers: Vec<(usize, u16)>,
    /// Column of the tree/document divider, when the tree is shown.
    pub tree_divider: Option<u16>,
    /// Column of the document/agents divider, when the agents pane is shown.
    pub sidebar_divider: Option<u16>,
    /// Row of the document/inspector divider, when the inspector is shown.
    pub inspector_divider: Option<u16>,
    /// The one cell that collapses the tree when shown, or reopens it when
    /// collapsed — always sitting on whichever border is currently visible.
    pub tree_toggle: Option<Rect>,
    /// Same idea, for the agents sidebar.
    pub sidebar_toggle: Option<Rect>,
    /// The one cell that collapses the sources inspector when the document is
    /// open, or reopens it when collapsed — on the divider row when shown, on
    /// the document's bottom border when not.
    pub inspector_toggle: Option<Rect>,
    /// Navigation arrow to go back to the last visited page.
    pub nav_back: Option<Rect>,
    /// Navigation arrow to go forward (reverse) in history.
    pub nav_forward: Option<Rect>,
    /// Clickable edit button on the document header.
    pub doc_edit: Option<Rect>,
    /// Clickable project selector badge on the bottom bar.
    pub project_selector: Option<Rect>,
    /// The narrow-terminal tab strip: one clickable rect per pane, labelled,
    /// replacing the side-by-side layout when there isn't room for it. Empty
    /// on a wide terminal, where the panes sit next to each other instead.
    pub tab_bar: Vec<(Focus, &'static str, Rect)>,
}

/// Which divider the pointer is dragging.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Divider {
    Tree,
    /// The boundary under collection box `usize` in the tree column.
    Collection(usize),
    Sidebar,
    Inspector,
}

/// Which scrollbar the pointer is dragging.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScrollbarDrag {
    Doc,
    Inspector,
}

/// A place the reader can be.
///
/// `Home` is the screen with the mark on it. It is a destination in its own
/// right, not the absence of one, which is why the history holds this rather
/// than a bare path: while it held paths, opening your first page pushed
/// nothing, so back had nowhere to go and home was unreachable for the rest
/// of the session.
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Spot {
    Home,
    Page(PathBuf),
}

pub struct App {
    pub cfg: Config,
    pub theme: Theme,
    pub tree: Tree,
    pub git: GitMap,
    pub git_track: Vec<(PathBuf, TrackState)>,
    pub index: Index,
    pub components: platform::Components,
    pub open: Option<Open>,
    pub focus: Focus,
    pub doc_stop: DocStop,
    pub overlay: Option<Overlay>,
    /// The in-page find bar, open over the reader.
    pub find: Option<PageFind>,
    pub toasts: Vec<Toast>,
    pub jobs: JobRunner,
    pub sidebar: Option<herdr::pty::Pane>,
    pub sidebar_error: Option<String>,
    pub show_tree: bool,
    pub show_sidebar: bool,
    pub show_inspector: bool,
    pub zoom: bool,
    pub back: Vec<Spot>,
    pub forward: Vec<Spot>,
    pub pending: Option<char>,
    pub finder_engine: search::Engine,
    pub quit: bool,
    /// Like `quit`, but the process is replaced with a fresh copy of itself
    /// instead of ending — how the palette's "restart" picks up a rebuild.
    pub restart: bool,
    /// Set by a first `q` while a job is running.
    quit_confirmed: bool,
    /// Set by a first attempt to leave the editor with unsaved changes.
    discard_armed: bool,
    /// Set by a save that found the file changed on disk. The next ctrl+s
    /// overwrites, parking the outside version beside the page.
    overwrite_armed: bool,
    /// The divider currently being dragged, if any.
    pub dragging: Option<Divider>,
    /// The scrollbar currently being dragged, if any.
    pub dragging_scrollbar: Option<ScrollbarDrag>,
    /// Whether mouse drag is currently selecting text in the reader.
    pub selecting_text: bool,
    /// The current selection drag started on the right button: releasing then
    /// pays the selection to the agents pane as a mention instead of copying
    /// it to the clipboard.
    selecting_right: bool,
    pub indexing: bool,
    /// When this instance started. The spinner phase is read off it, so every
    /// spinner on screen turns together regardless of redraw cadence.
    started: Instant,
    /// Source-line ranges an agent asked the reader to mark, by page. In
    /// memory only: an agent pointing at a passage must never edit it.
    pub marks: HashMap<PathBuf, Vec<Mark>>,
    /// Control requests accepted but not yet acted on, because the editor was
    /// open or a question was already on screen.
    pub deferred: Vec<Request>,
    /// Where the control socket is listening, when one could be bound.
    pub control_socket: Option<PathBuf>,
    /// Engine version from `pyproject.toml`, for the status bar.
    pub engine_version: Option<String>,
    /// A splash one-liner from `config.yaml` for the status bar's right corner.
    pub oneline: Option<String>,
    /// Active project name displayed on the bottom bar selector.
    pub project_name: String,
    pub areas: Areas,
    pub tx: Sender<AppEvent>,
    /// Cursor into `index.all_findings()` for `n` / `N`.
    finding_cursor: Option<usize>,
    /// Every mouse `Down` increments this. Two clicks only form a double click
    /// when they are adjacent (one generation apart), so a click in another
    /// pane between the two quietly breaks the pair.
    mouse_down: u64,
    /// The timestamp, row and `mouse_down` generation of the most recent click
    /// in the tree, used to recognise a second click on the same row.
    last_tree_click: Option<(Instant, usize, u64)>,
    /// Same as `last_tree_click`, for a source row in the inspector.
    last_inspector_click: Option<(Instant, usize, u64)>,
}

impl App {
    pub fn new(cfg: Config, tx: Sender<AppEvent>) -> Self {
        let theme = Theme::new(cfg.flavor);
        let engine_version = crate::config::engine_version(&cfg.root);
        let oneline = cfg.oneline();
        let collections: Vec<PathBuf> = cfg.collections().into_iter().map(|(_, p)| p).collect();
        let git = GitMap::scan(&cfg.root, &collections);
        let git_track = collections
            .iter()
            .map(|p| (p.clone(), crate::vault::git::track_state(p, &cfg.root)))
            .collect();
        let reg = crate::project::ProjectRegistry::load();
        let project_name = reg
            .projects
            .iter()
            .find(|(_, entry)| entry.path == cfg.root)
            .map(|(n, _)| n.clone())
            .unwrap_or_else(|| {
                cfg.root
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("project")
                    .to_string()
            });
        let components = platform::discover_components(&cfg.root);
        Self {
            tree: Tree::new(&cfg.root, collections),
            git,
            git_track,
            show_tree: cfg.tree_open,
            show_sidebar: cfg.sidebar_open,
            theme,
            cfg,
            index: Index::default(),
            components,
            open: None,
            focus: Focus::Tree,
            doc_stop: DocStop::Content,
            overlay: None,
            find: None,
            toasts: Vec::new(),
            jobs: JobRunner::default(),
            sidebar: None,
            sidebar_error: None,
            show_inspector: true,
            zoom: false,
            back: Vec::new(),
            forward: Vec::new(),
            pending: None,
            finder_engine: search::Engine::default(),
            quit: false,
            quit_confirmed: false,
            restart: false,
            discard_armed: false,
            overwrite_armed: false,
            dragging: None,
            dragging_scrollbar: None,
            selecting_text: false,
            selecting_right: false,
            engine_version,
            oneline,
            project_name,
            indexing: true,
            started: Instant::now(),
            marks: HashMap::new(),
            deferred: Vec::new(),
            control_socket: None,
            areas: Areas::default(),
            tx,
            finding_cursor: None,
            mouse_down: 0,
            last_tree_click: None,
            last_inspector_click: None,
        }
    }

    fn hit_tree_title(&self, x: u16, y: u16) -> Option<String> {
        for pane in &self.areas.tree_panes {
            if x < pane.area.x || x >= pane.area.x + pane.area.width {
                continue;
            }
            if y != pane.area.y {
                continue;
            }
            return self.tree.rows.get(pane.header).map(|r| r.label.clone());
        }
        None
    }

    /// The collections in the tree column, by name, in the order they are drawn.
    ///
    /// The name is the directory's own — `wiki`, `workspace`, `sources` — which
    /// is what `collection_heights` and `collapsed_collections` are keyed by,
    /// so the geometry survives a checkout moving.
    pub fn collection_names(&self) -> Vec<String> {
        self.tree
            .collection_paths()
            .iter()
            .map(|p| p.file_name().and_then(|n| n.to_str()).unwrap_or_default().to_string())
            .collect()
    }

    /// Fold or unfold the collection the tree cursor is in.
    fn toggle_collection(&mut self) {
        if let Some(i) = self.tree.section_of(self.tree.selected) {
            self.fold_collection(i);
        }
    }

    /// Fold or unfold collection `i`. The handle and the key both land here.
    pub fn fold_collection(&mut self, i: usize) {
        let names = self.collection_names();
        let Some(name) = names.get(i).cloned() else { return };
        if !self.cfg.collapsed_collections.remove(&name) {
            self.cfg.collapsed_collections.insert(name);
        }
        self.persist_widths();
    }

    fn hit_tree_row(&self, x: u16, y: u16) -> Option<usize> {
        if self.areas.tree_panes.is_empty() {
            let row = (y.saturating_sub(self.areas.tree.y + 1)) as usize;
            return Some(row.min(self.tree.rows.len().saturating_sub(1)));
        }
        for pane in &self.areas.tree_panes {
            if y < pane.area.y || y >= pane.area.y + pane.area.height {
                continue;
            }
            if x < pane.area.x || x >= pane.area.x + pane.area.width {
                continue;
            }
            if pane.inner.is_empty() || y < pane.inner.y {
                return Some(pane.header);
            }
            let row = (y.saturating_sub(pane.inner.y)) as usize;
            let i = pane.start + pane.offset + row;
            if i < pane.end {
                return Some(i);
            }
            return Some(pane.header);
        }
        None
    }

    fn open_repo_config(&mut self, name: &str) {
        let url = self
            .cfg
            .repo_url(name)
            .filter(|u| !u.is_empty() && *u != "local" && *u != "gdrive")
            .unwrap_or("")
            .to_string();
        self.overlay = Some(Overlay::RepoConfig(RepoConfig {
            name: name.to_string(),
            selected: 0,
            url,
        }));
    }

    pub fn collection_dirs(&self) -> Vec<PathBuf> {
        self.cfg.collections().into_iter().map(|(_, p)| p).collect()
    }

    pub fn refresh_git(&mut self) {
        let dirs = self.collection_dirs();
        self.git = GitMap::scan(&self.cfg.root, &dirs);
        self.git_track = dirs
            .iter()
            .map(|p| (p.clone(), crate::vault::git::track_state(p, &self.cfg.root)))
            .collect();
    }

    pub fn track_for(&self, path: &Path) -> &TrackState {
        self.git_track
            .iter()
            .find(|(p, _)| p == path)
            .map(|(_, s)| s)
            .unwrap_or(&TrackState::Untracked)
    }

    // ---------------------------------------------------------------- toasts

    pub fn toast(&mut self, level: Level, text: impl Into<String>) {
        self.toasts.push(Toast { text: text.into(), level, at: Instant::now() });
        // Three is as many as can be read at a glance.
        while self.toasts.len() > 3 {
            self.toasts.remove(0);
        }
    }

    pub fn expire_toasts(&mut self) {
        self.toasts.retain(|t| t.at.elapsed() < TOAST_TTL);
    }

    // -------------------------------------------------------------- motion

    /// Is anything on screen driven by time rather than by input?
    ///
    /// The answer decides whether a `Tick` costs a frame. A static `◐` beside
    /// a running job reads as a hung process, so background work spins — but
    /// an idle reader has nothing moving and must not repaint four times a
    /// second for the rest of the session.
    pub fn animating(&self) -> bool {
        self.indexing || !self.jobs.labels().is_empty() || !self.toasts.is_empty()
    }

    /// The current frame of the app's one spinner.
    ///
    /// Driven by the wall clock rather than a counter, so every spinner on
    /// screen is in step and none of them depends on how often we happened to
    /// redraw.
    pub fn spinner(&self) -> char {
        const FRAMES: [char; 8] = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠇'];
        let ms = self.started.elapsed().as_millis() as usize;
        FRAMES[(ms / 120) % FRAMES.len()]
    }

    // --------------------------------------------------------------- agents

    /// Marks an agent left on `path`, for the reader to tint.
    pub fn marks_for(&self, path: &Path) -> &[Mark] {
        self.marks.get(path).map_or(&[], Vec::as_slice)
    }

    /// A control request, straight off the socket.
    ///
    /// Nothing here steals the terminal from someone mid-sentence: while the
    /// editor is open — or a question is already on screen — the request is
    /// parked and acknowledged, and runs the moment the way is clear.
    pub fn on_control(&mut self, request: Request) {
        if self.control_blocked(&request.cmd) {
            request.deferred();
            self.announce_deferred(&request.cmd);
            self.deferred.push(request);
            return;
        }
        self.apply_control(request);
    }

    /// Run whatever was parked, oldest first, for as long as the way is clear.
    pub fn flush_control(&mut self) {
        while let Some(i) = self.deferred.iter().position(|r| !self.control_blocked(&r.cmd)) {
            let request = self.deferred.remove(i);
            self.apply_control(request);
        }
    }

    fn control_blocked(&self, cmd: &Control) -> bool {
        let editing = self.open.as_ref().is_some_and(Open::editing);
        match cmd {
            // Passive: a tint repaints under whatever you are doing.
            Control::Highlight { .. } => false,
            Control::Open { .. } => editing,
            Control::Ask { .. } => editing || self.overlay.is_some(),
        }
    }

    fn announce_deferred(&mut self, cmd: &Control) {
        let what = match cmd {
            Control::Open { path, .. } => format!("agent wants to open {path}"),
            Control::Ask { .. } => "agent is waiting on a question".to_string(),
            Control::Highlight { .. } => return,
        };
        self.toast(Level::Info, format!("{what} — after you leave the editor"));
    }

    fn apply_control(&mut self, request: Request) {
        match request.cmd.clone() {
            Control::Open { path, line } => match self.resolve_control_path(&path) {
                Ok(target) => {
                    self.open_path(&target, true);
                    self.tree.reveal(&target);
                    // Agents count file lines, the way `Read` and `sed` show
                    // them; the reader counts body lines.
                    if let (Some(open), Some(line)) = (self.open.as_mut(), line) {
                        let body = line.saturating_sub(1).saturating_sub(open.page.body_start);
                        open.scroll = open.doc.line_for_source(body);
                    }
                    self.toast(Level::Info, format!("agent opened {path}"));
                    request.ok(serde_json::json!({"path": target.display().to_string()}));
                }
                Err(err) => request.err(err),
            },
            Control::Highlight { path, ranges, label, clear } => {
                let target = match path.as_deref().map(|p| self.resolve_control_path(p)).transpose() {
                    Ok(target) => target,
                    Err(err) => return request.err(err),
                };
                match (&target, clear) {
                    (None, _) => self.marks.clear(),
                    (Some(target), true) => {
                        self.marks.remove(target);
                    }
                    (Some(_), false) => {}
                }
                let count = ranges.len();
                if let Some(target) = target.filter(|_| !ranges.is_empty()) {
                    let offset = self.body_start(&target);
                    let marks = ranges.into_iter().map(|(start, end)| Mark {
                        start: start.saturating_sub(1).saturating_sub(offset),
                        end: end.saturating_sub(1).saturating_sub(offset),
                        label: label.clone(),
                    });
                    self.marks.entry(target).or_default().extend(marks);
                    let what = label.unwrap_or_else(|| "marked".to_string());
                    self.toast(Level::Warn, format!("{what} — {count} passage(s) flagged by an agent"));
                }
                request.ok(serde_json::json!({"marks": count}));
            }
            Control::Ask { question, options } => {
                self.overlay = Some(Overlay::Ask(Ask { question, options, selected: 0, request }));
                self.focus = Focus::Doc;
            }
        }
    }

    /// Where `path`'s body begins, so a file line an agent quoted can be
    /// turned into the body line the reader renders. The open page already
    /// knows; any other one costs a read, which a highlight can afford.
    fn body_start(&self, path: &Path) -> usize {
        match self.open.as_ref().filter(|o| o.page.path == path) {
            Some(open) => open.page.body_start,
            None => Page::load(path, &self.cfg.root).map_or(0, |page| page.body_start),
        }
    }

    /// Resolve an agent-supplied path against the checkout.
    ///
    /// Absolute paths and `..` escapes are refused rather than followed: the
    /// control socket reaches this project's collections, not the filesystem.
    fn resolve_control_path(&self, path: &str) -> Result<PathBuf, String> {
        let candidate = Path::new(path);
        let rel = candidate.strip_prefix(&self.cfg.root).unwrap_or(candidate);
        if rel.is_absolute() || rel.components().any(|c| c == std::path::Component::ParentDir) {
            return Err(format!("{path} is outside the checkout"));
        }
        let target = self.cfg.root.join(rel);
        if !target.exists() {
            return Err(format!("{path} does not exist"));
        }
        Ok(target)
    }

    fn ask_key(&mut self, key: KeyEvent) {
        let Some(Overlay::Ask(ask)) = self.overlay.as_mut() else { return };
        match key.code {
            KeyCode::Down | KeyCode::Char('j') | KeyCode::Tab => {
                ask.selected = (ask.selected + 1) % ask.options.len();
            }
            KeyCode::Up | KeyCode::Char('k') | KeyCode::BackTab => {
                ask.selected = (ask.selected + ask.options.len() - 1) % ask.options.len();
            }
            KeyCode::Esc => {
                let Some(Overlay::Ask(ask)) = self.overlay.take() else { return };
                // A dismissed question is an answer: "not now", not silence.
                ask.request.ok(serde_json::json!({"cancelled": true, "answer": null}));
                self.toast(Level::Info, "question dismissed");
            }
            KeyCode::Enter => {
                let Some(Overlay::Ask(ask)) = self.overlay.take() else { return };
                let answer = ask.options[ask.selected].clone();
                ask.request.ok(serde_json::json!({
                    "answer": answer,
                    "index": ask.selected,
                    "cancelled": false,
                }));
                self.toast(Level::Good, format!("answered: {answer}"));
            }
            // A digit picks its option outright, so a two-way question is one
            // keystroke rather than an arrow and a return.
            KeyCode::Char(c) if c.is_ascii_digit() && c != '0' => {
                let pick = c.to_digit(10).unwrap() as usize - 1;
                if pick < ask.options.len() {
                    ask.selected = pick;
                    self.ask_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                }
            }
            _ => {}
        }
    }

    // ------------------------------------------------------------- documents

    pub fn doc_width(&self) -> u16 {
        self.measure(self.open.as_ref().is_some_and(Open::csv))
    }

    /// Width a page is laid out at. Two columns of border plus one of padding
    /// on each side; prose is then capped at the reading measure, but a CSV is
    /// a table and takes the whole pane — capping it would pan columns that
    /// had room to be drawn.
    pub fn measure(&self, csv: bool) -> u16 {
        let inner = self.areas.doc.width.saturating_sub(4).max(20);
        if csv {
            inner
        } else {
            inner.min(crate::ui::markdown::MAX_WIDTH)
        }
    }

    pub fn open_path(&mut self, path: &Path, push_history: bool) {
        if is_external(path) {
            return self.open_external(path);
        }
        if path.is_dir() {
            let index = path.join("_index.md");
            if index.is_file() {
                return self.open_path(&index, push_history);
            }
            return;
        }
        let here = self.here();
        if here == Spot::Page(path.to_path_buf()) {
            return;
        }
        // Pushed even when leaving home, so the first page you open has
        // somewhere to go back to.
        if push_history {
            self.back.push(here);
            self.forward.clear();
        }
        match Open::load(path, &self.cfg.root, self.measure(Open::is_csv(path)), &self.theme) {
            Some(open) => {
                self.open = Some(open);
                self.focus = Focus::Doc;
                self.doc_stop = DocStop::Content;
            }
            None => self.toast(Level::Bad, format!("could not read {}", path.display())),
        }
    }

    /// Hand a file off to the system's default viewer instead of rendering it
    /// in-pane — used for PDFs and images, which the tree lists but never
    /// tries to parse as markdown.
    fn open_external(&mut self, path: &Path) {
        let opener = if cfg!(target_os = "macos") { "open" } else { "xdg-open" };
        match std::process::Command::new(opener)
            .arg(path)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
        {
            Ok(_) => self.toast(Level::Info, format!("opened {} externally", path.display())),
            Err(_) => self.toast(Level::Bad, format!("no viewer found for {}", path.display())),
        }
    }

    /// Re-read the open page from disk, keeping the reading position.
    pub fn reload_open(&mut self) {
        let Some(open) = self.open.as_ref() else { return };
        if open.editing() {
            return;
        }
        let (path, anchor, inspect_scroll) =
            (open.page.path.clone(), open.doc.source_for_line(open.scroll), open.inspect_scroll);
        let width = self.measure(Open::is_csv(&path));
        if let Some(mut fresh) = Open::load(&path, &self.cfg.root, width, &self.theme) {
            fresh.scroll = fresh.doc.line_for_source(anchor);
            fresh.inspect_scroll = inspect_scroll;
            self.open = Some(fresh);
        }
    }

    pub fn reflow(&mut self) {
        let (width, theme) = (self.doc_width(), self.theme);
        if let Some(open) = self.open.as_mut() {
            open.reflow(width, &theme);
        }
    }

    pub fn inspector_total_lines(&self) -> usize {
        let Some(open) = self.open.as_ref() else { return 0 };
        let lines = crate::ui::panes::inspector_lines(open, &self.index, &self.cfg.root, &self.theme);
        // 2 columns of border, 2 of padding — matches the block the inspector
        // is actually drawn into (`ui::panes::inspector`).
        let width = self.areas.inspector.width.saturating_sub(4) as usize;
        crate::ui::panes::wrap_lines(lines, width).len()
    }

    pub fn scroll_inspector(&mut self, delta: isize) {
        let total = self.inspector_total_lines();
        let height = self.areas.inspector.height.saturating_sub(2) as usize;
        let max_scroll = total.saturating_sub(height);
        let Some(open) = self.open.as_mut() else { return };
        if delta < 0 {
            open.inspect_scroll = open.inspect_scroll.saturating_sub(delta.unsigned_abs());
        } else {
            open.inspect_scroll = open.inspect_scroll.saturating_add(delta as usize).min(max_scroll);
        }
    }

    pub fn scroll_inspector_to_y(&mut self, y: u16) {
        let area = self.areas.inspector;
        if area.height < 4 {
            return;
        }
        let total = self.inspector_total_lines();
        let height = area.height.saturating_sub(2) as usize;
        if total <= height || height == 0 {
            if let Some(open) = self.open.as_mut() {
                open.inspect_scroll = 0;
            }
            return;
        }
        let max_scroll = total.saturating_sub(height);
        let track_y = area.y + 1;
        let track_len = area.height.saturating_sub(2) as usize;
        let span = ((height * track_len) / total).max(1);
        let scroll = Self::scroll_from_track_y(y, track_y, track_len, span, max_scroll);
        if let Some(open) = self.open.as_mut() {
            open.inspect_scroll = scroll;
        }
    }

    pub fn scroll_doc_to_y(&mut self, y: u16) {
        let area = if !self.areas.doc_main.is_empty() {
            self.areas.doc_main
        } else {
            self.areas.doc
        };
        if area.height < 4 {
            return;
        }
        let Some(open) = self.open.as_mut() else { return };
        let total = open.doc.height();
        let height = self.areas.doc_body.height as usize;
        if total <= height || height == 0 {
            open.scroll = 0;
            return;
        }
        let max_scroll = total.saturating_sub(height);
        let track_y = area.y + 1;
        let track_len = area.height.saturating_sub(2) as usize;
        let span = ((height * track_len) / total).max(1);
        open.scroll = Self::scroll_from_track_y(y, track_y, track_len, span, max_scroll);
    }

    // ----------------------------------------------------------------- mouse

fn scroll_from_track_y(y: u16, track_y: u16, track_len: usize, span: usize, max_scroll: usize) -> usize {
    if track_len <= span || max_scroll == 0 {
        return 0;
    }
    let click_offset = (y.saturating_sub(track_y) as usize).min(track_len.saturating_sub(1));
    let target_top = click_offset.saturating_sub(span / 2);
    let travel = track_len.saturating_sub(span);
    // Rounded rather than truncated, so dragging the thumb to the bottom of the
    // track really does reach the bottom of the document.
    max_scroll
        .checked_mul(target_top)
        .and_then(|scaled| scaled.checked_add(travel / 2))
        .and_then(|scaled| scaled.checked_div(travel))
        .map_or(0, |scroll| scroll.min(max_scroll))
}

fn is_on_scrollbar(area: Rect, x: u16, y: u16) -> bool {
    if area.height < 4 || area.width < 2 {
        return false;
    }
    let right_border = area.x + area.width.saturating_sub(1);
    let reach = if area.width >= 10 { 2 } else { 1 };
    let left_edge = right_border.saturating_sub(reach);
    x >= left_edge
        && x <= right_border
        && y > area.y
        && y < area.y + area.height.saturating_sub(1)
}

    /// Click to focus and select, wheel to scroll, drag a divider to resize.
    /// The pane under the pointer acts, whether or not it has keyboard focus —
    /// that is what a pointer is for.
    pub fn on_mouse(&mut self, mouse: MouseEvent) {
        // Number every Down, so a double-click pair must be two *adjacent*
        // clicks on the same row — any click elsewhere in between disqualifies.
        if matches!(mouse.kind, MouseEventKind::Down(_)) {
            self.mouse_down += 1;
        }

        if self.overlay.is_some() {
            self.overlay_mouse(mouse);
            return;
        }
        let (x, y) = (mouse.column, mouse.row);

        // A drag in progress owns the pointer until the button comes up, even
        // if it strays outside the divider's own column.
        if let Some(divider) = self.dragging {
            match mouse.kind {
                MouseEventKind::Drag(MouseButton::Left) => return self.resize_to(divider, x, y),
                MouseEventKind::Up(_) => {
                    self.dragging = None;
                    self.persist_widths();
                    return;
                }
                _ => {}
            }
        }

        if let Some(drag) = self.dragging_scrollbar {
            match mouse.kind {
                MouseEventKind::Drag(MouseButton::Left) => {
                    match drag {
                        ScrollbarDrag::Doc => self.scroll_doc_to_y(y),
                        ScrollbarDrag::Inspector => self.scroll_inspector_to_y(y),
                    }
                    return;
                }
                MouseEventKind::Up(_) => {
                    self.dragging_scrollbar = None;
                    return;
                }
                _ => {}
            }
        }

        if self.selecting_text {
            match mouse.kind {
                MouseEventKind::Drag(btn) if (btn == MouseButton::Right) == self.selecting_right => {
                    return self.extend_selection(x, y);
                }
                MouseEventKind::Up(btn) if (btn == MouseButton::Right) == self.selecting_right => {
                    if self.selecting_right {
                        return self.finish_selection_right(x, y);
                    }
                    return self.finish_selection(x, y);
                }
                _ => {}
            }
        }

        if matches!(mouse.kind, MouseEventKind::Down(MouseButton::Left)) {
            if let Some((focus, _, _)) =
                self.areas.tab_bar.iter().find(|(_, _, r)| x >= r.x && x < r.x + r.width && y == r.y)
            {
                self.select_tab(*focus);
                return;
            }
            if let Some(i) = self
                .areas
                .tree_panes
                .iter()
                .position(|p| p.fold.is_some_and(|r| r.x == x && r.y == y))
            {
                self.fold_collection(i);
                return;
            }
            if self.areas.tree_toggle.is_some_and(|r| r.x == x && r.y == y) {
                self.run(Cmd::ToggleTree);
                return;
            }
            if self.areas.project_selector.is_some_and(|r| x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height) {
                self.run(Cmd::Projects);
                return;
            }
            if self.areas.sidebar_toggle.is_some_and(|r| r.x == x && r.y == y) {
                self.run(Cmd::ToggleSidebar);
                return;
            }
            if self.areas.inspector_toggle.is_some_and(|r| r.x == x && r.y == y) {
                self.run(Cmd::ToggleInspector);
                return;
            }
            if self.areas.nav_back.is_some_and(|r| x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height) {
                self.set_focus(Focus::Doc);
                self.run(Cmd::Back);
                return;
            }
            if self.areas.nav_forward.is_some_and(|r| x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height) {
                self.set_focus(Focus::Doc);
                self.run(Cmd::Forward);
                return;
            }
            if self.areas.doc_edit.is_some_and(|r| x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height) {
                self.set_focus(Focus::Doc);
                self.run(Cmd::Edit);
                return;
            }

            let inspect_area = self.areas.inspector;
            let inspect_total = self.inspector_total_lines();
            let inspect_h = inspect_area.height.saturating_sub(2) as usize;
            if !inspect_area.is_empty() && inspect_total > inspect_h && Self::is_on_scrollbar(inspect_area, x, y) {
                self.dragging_scrollbar = Some(ScrollbarDrag::Inspector);
                self.scroll_inspector_to_y(y);
                return;
            }

            let doc_area = if !self.areas.doc_main.is_empty() {
                self.areas.doc_main
            } else {
                self.areas.doc
            };
            let doc_total = self.open.as_ref().map(|o| o.doc.height()).unwrap_or(0);
            let doc_h = self.areas.doc_body.height as usize;
            if !doc_area.is_empty() && doc_total > doc_h && doc_h > 0 && Self::is_on_scrollbar(doc_area, x, y) {
                self.dragging_scrollbar = Some(ScrollbarDrag::Doc);
                self.scroll_doc_to_y(y);
                return;
            }

            if let Some(divider) = self.divider_at(x, y) {
                self.dragging = Some(divider);
                self.zoom = false;
                return;
            }
        }

        let inside = |rect: Rect| {
            !rect.is_empty()
                && x >= rect.x
                && x < rect.x + rect.width
                && y >= rect.y
                && y < rect.y + rect.height
        };

        if inside(self.areas.sidebar) {
            let area = self.areas.sidebar;
            let cols = area.width.saturating_sub(2);
            let rows = area.height.saturating_sub(2);

            if matches!(mouse.kind, MouseEventKind::Down(_)) {
                self.set_focus(Focus::Sidebar);
            }

            if cols > 0 && rows > 0 {
                let inside_inner = x > area.x
                    && x <= area.x + cols
                    && y > area.y
                    && y <= area.y + rows;

                if inside_inner || !matches!(mouse.kind, MouseEventKind::Down(_)) {
                    let col = (x.saturating_sub(area.x)).clamp(1, cols);
                    let row = (y.saturating_sub(area.y)).clamp(1, rows);
                    let pixel = self.sidebar.as_ref().and_then(|pane| {
                        pane.pixel_mouse().then_some(herdr::keys::PTY_CELL_PX)
                    });
                    let bytes = herdr::keys::encode_mouse(&mouse, col, row, pixel);
                    if let Some(pane) = self.sidebar.as_mut() {
                        pane.send(&bytes);
                    }
                }
            }
            return;
        }

        if inside(self.areas.tree) {
            match mouse.kind {
                MouseEventKind::ScrollDown => self.tree.move_by(1),
                MouseEventKind::ScrollUp => self.tree.move_by(-1),
                MouseEventKind::Down(MouseButton::Left) => {
                    self.set_focus(Focus::Tree);
                    if let Some(name) = self.hit_tree_title(x, y) {
                        self.open_repo_config(&name);
                    } else if let Some(i) = self.hit_tree_row(x, y) {
                        self.tree.move_to(i);
                        let now = Instant::now();
                        let double = match self.last_tree_click.replace((now, i, self.mouse_down)) {
                            Some((at, row, gen)) => {
                                row == i
                                    && gen + 1 == self.mouse_down
                                    && now.duration_since(at) <= DOUBLE_CLICK
                            }
                            None => false,
                        };
                        if double {
                            // The first press already toggled, so a folder is
                            // made sure to stay expanded before its index opens.
                            if let Some(path) = self.tree.open_double(i) {
                                self.open_path(&path, true);
                            }
                        } else {
                            // A single click selects a page, and toggles a
                            // folder — opening is saved for the double click.
                            self.tree.toggle();
                        }
                    }
                }
                MouseEventKind::Down(MouseButton::Right) => {
                    self.set_focus(Focus::Tree);
                    if let Some(i) = self.hit_tree_row(x, y) {
                        self.tree.move_to(i);
                        self.open_tree_menu(i, x, y);
                    }
                }
                _ => {}
            }
            return;
        }

        if inside(self.areas.inspector) {
            match mouse.kind {
                MouseEventKind::ScrollDown => self.scroll_inspector(3),
                MouseEventKind::ScrollUp => self.scroll_inspector(-3),
                MouseEventKind::Down(MouseButton::Left) => {
                    self.set_focus(Focus::Doc);
                    let body = self.areas.inspector_body;
                    if body.is_empty()
                        || y < body.y
                        || y >= body.y + body.height
                        || x < body.x
                        || x >= body.x + body.width
                    {
                        return;
                    }
                    let row = (y - body.y) as usize;
                    let col = (x - body.x) as u16;

                    if let Some(link) = self
                        .areas
                        .inspector_backlinks
                        .iter()
                        .find(|b| b.row == row && col >= b.col_start && col < b.col_end)
                    {
                        let path = link.path.clone();
                        self.open_path(&path, true);
                        return;
                    }

                    if let Some(Some(id)) = self.areas.inspector_rows.get(row).cloned() {
                        let now = Instant::now();
                        let double = match self.last_inspector_click.replace((now, row, self.mouse_down)) {
                            Some((at, r, gen)) => {
                                r == row
                                    && gen + 1 == self.mouse_down
                                    && now.duration_since(at) <= DOUBLE_CLICK
                            }
                            None => false,
                        };
                        if double {
                            self.open_source(&id);
                        } else {
                            // A single click selects the source — its `[n]` is
                            // highlighted in the body. Opening is a double click.
                            self.select_source(&id);
                        }
                    }
                }
                _ => {}
            }
            return;
        }

        if inside(self.areas.doc) {
            // The editor resolves the pointer itself, against the text area it
            // recorded at render time: click to place the cursor, drag to
            // select, wheel to scroll. Without this the pointer does nothing
            // at all in the one pane where typing happens.
            if self.open.as_ref().is_some_and(Open::editing) {
                if matches!(mouse.kind, MouseEventKind::Down(_)) {
                    self.set_focus(Focus::Doc);
                }
                if let Some(editor) = self.open.as_mut().and_then(|o| o.editor.as_mut()) {
                    editor.mouse(mouse);
                }
                return;
            }
            match mouse.kind {
                // Shift+wheel is how a terminal reports a sideways scroll on a
                // mouse that has no tilt; a trackpad sends it directly.
                MouseEventKind::ScrollDown if mouse.modifiers.contains(KeyModifiers::SHIFT) => {
                    self.pan(Self::PAN_STEP)
                }
                MouseEventKind::ScrollUp if mouse.modifiers.contains(KeyModifiers::SHIFT) => {
                    self.pan(-Self::PAN_STEP)
                }
                MouseEventKind::ScrollRight => self.pan(Self::PAN_STEP),
                MouseEventKind::ScrollLeft => self.pan(-Self::PAN_STEP),
                MouseEventKind::ScrollDown => self.scroll(3),
                MouseEventKind::ScrollUp => self.scroll(-3),
                MouseEventKind::Down(MouseButton::Left) => {
                    self.set_focus(Focus::Doc);
                    if self.open.as_ref().is_some_and(|o| !o.editing()) {
                        self.start_selection(x, y);
                    }
                }
                MouseEventKind::Down(MouseButton::Right) => {
                    // A right-drag is the same selection gesture, paid to the
                    // agent on release instead of the clipboard.
                    self.set_focus(Focus::Doc);
                    if self.open.as_ref().is_some_and(|o| !o.editing()) {
                        self.selecting_right = true;
                        self.start_selection(x, y);
                    }
                }
                _ => {}
            }
        }
    }

    /// Whole terminal in cells, inferred from the panes being drawn.
    fn viewport(&self) -> (u16, u16) {
        let rects = [self.areas.tree, self.areas.doc, self.areas.sidebar, self.areas.inspector];
        let width = rects
            .iter()
            .filter(|r| !r.is_empty())
            .map(|r| r.x.saturating_add(r.width))
            .max()
            .unwrap_or(80);
        let height = rects
            .iter()
            .filter(|r| !r.is_empty())
            .map(|r| r.y.saturating_add(r.height))
            .max()
            .unwrap_or(24);
        (width, height)
    }

    /// The right-click menu is the only overlay that takes the pointer. A left
    /// press highlights the item under it and a release on the same row runs
    /// it; a press anywhere else dismisses the menu.
    fn overlay_mouse(&mut self, mouse: MouseEvent) {
        let area = match self.overlay.as_ref() {
            Some(Overlay::Menu(menu)) => menu.area,
            _ => return,
        };
        if area.is_empty() {
            return;
        }
        let inside = mouse.column >= area.x
            && mouse.column < area.x + area.width
            && mouse.row >= area.y
            && mouse.row < area.y + area.height;
        let row = mouse.row.saturating_sub(area.y + 1) as usize;

        match mouse.kind {
            MouseEventKind::Down(MouseButton::Left) if inside => {
                if let Some(Overlay::Menu(menu)) = self.overlay.as_mut() {
                    menu.selected = row.min(menu.items.len().saturating_sub(1));
                    menu.pressed = (row < menu.items.len()).then_some(row);
                }
            }
            MouseEventKind::Down(MouseButton::Left) => self.overlay = None,
            MouseEventKind::Up(MouseButton::Left) if inside => {
                let fire = self.overlay.as_ref().is_some_and(|o| match o {
                    Overlay::Menu(menu) => menu.pressed == Some(row),
                    _ => false,
                });
                if fire {
                    self.activate_menu();
                }
            }
            _ => {}
        }
    }

    /// Open the right-click context menu for tree row `i`, positioned at the
    /// pointer but clamped to the layout so it never leaves the screen.
    fn open_tree_menu(&mut self, i: usize, x: u16, y: u16) {
        let Some(row) = self.tree.rows.get(i).cloned() else { return };
        let items: Vec<(MenuAction, &'static str)> = if row.is_collection {
            // A collection root has no rename or delete: those names belong to
            // `repositories:` in the config, not to the row in the tree.
            vec![
                (MenuAction::Open, "open"),
                (MenuAction::NewPage, "new page…"),
                (MenuAction::CopyRelative, "copy relative path"),
                (MenuAction::CopyAbsolute, "copy absolute path"),
            ]
        } else if row.is_dir {
            vec![
                (MenuAction::Open, "open"),
                (MenuAction::NewPage, "new page…"),
                (MenuAction::Rename, "rename…"),
                (MenuAction::Delete, "delete…"),
                (MenuAction::CopyRelative, "copy relative path"),
                (MenuAction::CopyAbsolute, "copy absolute path"),
            ]
        } else {
            vec![
                (MenuAction::Open, "open"),
                (MenuAction::Rename, "rename…"),
                (MenuAction::Delete, "delete…"),
                (MenuAction::CopyRelative, "copy relative path"),
                (MenuAction::CopyAbsolute, "copy absolute path"),
            ]
        };
        let width = items.iter().map(|(_, label)| label.len()).max().unwrap_or(12) as u16 + 6;
        let height = items.len() as u16 + 3;
        let (screen_w, screen_h) = self.viewport();
        let area = Rect {
            x: x.saturating_add(1).min(screen_w.saturating_sub(width)),
            y: y.saturating_sub(height / 2).min(screen_h.saturating_sub(height)),
            width,
            height,
        };
        self.overlay = Some(Overlay::Menu(Menu {
            path: row.path,
            items,
            selected: 0,
            area,
            pressed: None,
        }));
    }

    fn divider_at(&self, x: u16, y: u16) -> Option<Divider> {
        let body = self.areas.doc;
        if body.is_empty() {
            return None;
        }
        let near = |pos: Option<u16>, coord: u16| {
            pos.is_some_and(|p| coord.abs_diff(p) <= crate::ui::GRAB)
        };
        let tree = self.areas.tree;
        if !tree.is_empty() && x >= tree.x && x < tree.x + tree.width {
            // Checked before the vertical dividers: inside the tree column a
            // horizontal boundary is the likelier target, and the two only
            // overlap on the corner cell.
            if let Some((i, _)) = self
                .areas
                .collection_dividers
                .iter()
                .find(|(_, edge)| y.abs_diff(*edge) <= crate::ui::GRAB)
            {
                return Some(Divider::Collection(*i));
            }
        }
        if y >= body.y && y < body.y + body.height {
            if near(self.areas.tree_divider, x) {
                return Some(Divider::Tree);
            }
            // Grab the shared border and the document side of it, not the first
            // inner column of the herdr pane — that column is a real click target.
            if self.areas.sidebar_divider.is_some_and(|p| x == p || x + 1 == p) {
                return Some(Divider::Sidebar);
            }
        }
        if x >= body.x && x < body.x + body.width
            && near(self.areas.inspector_divider, y) {
                return Some(Divider::Inspector);
            }
        None
    }

    /// Move a divider to column `x` or row `y`. Widths are clamped so neither the document
    /// nor the pane being dragged can be squeezed out of existence.
    fn resize_to(&mut self, divider: Divider, x: u16, y: u16) {
        let total = self.areas.tree.width + self.areas.doc.width + self.areas.sidebar.width;
        let origin = self.areas.tree.x.min(self.areas.doc.x);
        let min_doc = 30u16;
        match divider {
            Divider::Tree => {
                if total == 0 {
                    return;
                }
                let max = total.saturating_sub(min_doc + self.areas.sidebar.width);
                let width = x.saturating_sub(origin).saturating_add(1);
                self.cfg.tree_width = width.clamp(MIN_PANE, max.max(MIN_PANE));
            }
            Divider::Sidebar => {
                if total == 0 {
                    return;
                }
                let max = total.saturating_sub(min_doc + self.areas.tree.width);
                let width = (origin + total).saturating_sub(x);
                self.cfg.sidebar_width = width.clamp(MIN_PANE, max.max(MIN_PANE));
            }
            Divider::Collection(i) => {
                let Some(pane) = self.areas.tree_panes.get(i) else { return };
                // A folded box is one row by definition; there is nothing to
                // drag, and writing a height here would silently resize it the
                // moment it was unfolded.
                if pane.folded {
                    return;
                }
                let names = self.collection_names();
                let Some(name) = names.get(i).cloned() else { return };
                // The divider sits on the first row of the box below, so the
                // box above keeps every row up to it.
                let rows = y.saturating_sub(pane.area.y);
                let max = self
                    .areas
                    .tree
                    .height
                    .saturating_sub(crate::ui::panes::MIN_COLLECTION_ROWS);
                let rows = rows.clamp(
                    crate::ui::panes::MIN_COLLECTION_ROWS,
                    max.max(crate::ui::panes::MIN_COLLECTION_ROWS),
                );
                self.cfg.collection_heights.insert(name, rows);
            }
            Divider::Inspector => {
                let doc_bottom = self.areas.doc.y + self.areas.doc.height;
                let min_main = 8u16;
                let min_inspect = 4u16;
                let max_inspect = self.areas.doc.height.saturating_sub(min_main);
                let new_h = doc_bottom.saturating_sub(y);
                self.cfg.inspector_height = new_h.clamp(min_inspect, max_inspect.max(min_inspect));
            }
        }
    }

    pub fn persist_widths(&self) {
        let _ = self.cfg.save_tui();
    }

    pub fn adjust_inspector_height(&mut self, delta: i16) {
        let min_main = 8u16;
        let min_inspect = 4u16;
        let max_inspect = self.areas.doc.height.saturating_sub(min_main);
        let current = self.cfg.inspector_height as i16;
        let new_h = (current + delta).clamp(min_inspect as i16, max_inspect.max(min_inspect) as i16) as u16;
        if new_h != self.cfg.inspector_height {
            self.cfg.inspector_height = new_h;
            self.persist_widths();
        }
    }

    pub fn adjust_tree_width(&mut self, delta: i16) {
        let total = self.areas.tree.width + self.areas.doc.width + self.areas.sidebar.width;
        let min_doc = 30u16;
        let sidebar_w = if self.show_sidebar { self.areas.sidebar.width } else { 0 };
        let max = if total > 0 {
            total.saturating_sub(min_doc + sidebar_w)
        } else {
            120
        };
        let current = self.cfg.tree_width as i16;
        let new_w = (current + delta).clamp(MIN_PANE as i16, max.max(MIN_PANE) as i16) as u16;
        if new_w != self.cfg.tree_width {
            self.cfg.tree_width = new_w;
            self.persist_widths();
        }
    }

    pub fn adjust_sidebar_width(&mut self, delta: i16) {
        let total = self.areas.tree.width + self.areas.doc.width + self.areas.sidebar.width;
        let min_doc = 30u16;
        let tree_w = if self.show_tree { self.areas.tree.width } else { 0 };
        let max = if total > 0 {
            total.saturating_sub(min_doc + tree_w)
        } else {
            120
        };
        let current = self.cfg.sidebar_width as i16;
        let new_w = (current + delta).clamp(MIN_PANE as i16, max.max(MIN_PANE) as i16) as u16;
        if new_w != self.cfg.sidebar_width {
            self.cfg.sidebar_width = new_w;
            self.persist_widths();
        }
    }

    pub fn shrink_active_pane(&mut self) {
        match self.focus {
            Focus::Tree => self.adjust_tree_width(-4),
            Focus::Sidebar => self.adjust_sidebar_width(-4),
            Focus::Doc => {
                if self.show_sidebar {
                    self.adjust_sidebar_width(4);
                } else if self.show_tree {
                    self.adjust_tree_width(-4);
                }
            }
        }
    }

    pub fn widen_active_pane(&mut self) {
        match self.focus {
            Focus::Tree => self.adjust_tree_width(4),
            Focus::Sidebar => self.adjust_sidebar_width(4),
            Focus::Doc => {
                if self.show_sidebar {
                    self.adjust_sidebar_width(-4);
                } else if self.show_tree {
                    self.adjust_tree_width(4);
                }
            }
        }
    }

    // ------------------------------------------------------------------ keys

    pub fn ctx(&self) -> Ctx {
        match self.focus {
            Focus::Sidebar if self.sidebar.is_some() => Ctx::Sidebar,
            Focus::Tree => Ctx::Tree,
            _ if self.open.as_ref().is_some_and(Open::editing) => Ctx::Edit,
            Focus::Doc | Focus::Sidebar => Ctx::Doc,
        }
    }

    /// A paste, delivered whole by bracketed paste rather than as a burst of
    /// key events. Where it lands depends on what has focus, because only some
    /// of these surfaces take text at all.
    pub fn on_paste(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        // Text inputs read `Char` keys, which is exactly what they were being
        // fed before bracketed paste was enabled. Control characters are
        // dropped: a newline in a pasted query would submit the prompt
        // halfway through the paste.
        if self.overlay.is_some() || self.find.is_some() {
            for ch in text.chars().filter(|c| !c.is_control()) {
                self.on_key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE));
            }
            return;
        }
        match self.ctx() {
            Ctx::Sidebar => {
                let bracketed = self.sidebar.as_ref().is_some_and(herdr::pty::Pane::bracketed_paste);
                let bytes = if bracketed {
                    format!("\x1b[200~{text}\x1b[201~").into_bytes()
                } else {
                    text.as_bytes().to_vec()
                };
                if let Some(pane) = self.sidebar.as_mut() {
                    pane.send(&bytes);
                }
            }
            Ctx::Edit => {
                self.with_editor(|editor| editor.paste(text));
                // Typing again means the warning has to be re-earned.
                self.discard_armed = false;
                self.overwrite_armed = false;
                self.refresh_completion();
            }
            // Nothing in the reader or the tree takes text, and replaying the
            // characters as keys would run each one as a command.
            _ => self.toast(Level::Info, "nothing here takes a paste — press e to edit"),
        }
    }

    pub fn on_key(&mut self, key: KeyEvent) {
        if self.overlay.is_some() {
            return self.overlay_key(key);
        }

        // The find bar is a text input sitting over the reader: while it is
        // open every key is its own, or nothing.
        if self.find.is_some() {
            return self.find_key(key);
        }

        let ctx = self.ctx();

        // The sidebar owns its keys: only its escape hatch is ours.
        if ctx == Ctx::Sidebar {
            if let Resolved::Run(cmd) = keymap::resolve(&key, ctx, None) {
                return self.run(cmd);
            }
            let application_cursor = self.sidebar.as_ref().is_some_and(|p| p.application_cursor());
            let bytes = herdr::keys::encode(&key, application_cursor);
            if let Some(pane) = self.sidebar.as_mut() {
                pane.send(&bytes);
            }
            return;
        }

        if ctx == Ctx::Edit {
            return self.editor_key(key);
        }

        match keymap::resolve(&key, ctx, self.pending.take()) {
            Resolved::Run(cmd) => {
                if cmd != Cmd::Quit {
                    self.quit_confirmed = false;
                }
                self.run(cmd)
            }
            Resolved::Pending(prefix) => self.pending = Some(prefix),
            Resolved::None => {}
        }
    }

    /// Hits for the live find query against the open page.
    ///
    /// Derived, never stored: see `PageFind`.
    pub fn finds(&self) -> markdown::Finds {
        let (Some(find), Some(open)) = (self.find.as_ref(), self.open.as_ref()) else {
            return markdown::Finds::default();
        };
        let hits = open.doc.find(&find.query);
        let current = find.current.min(hits.len().saturating_sub(1));
        markdown::Finds { hits, current }
    }

    fn open_find(&mut self) {
        if self.open.is_none() {
            return self.toast(Level::Warn, "no page open to search");
        }
        self.focus = Focus::Doc;
        self.find = Some(PageFind { query: String::new(), current: 0 });
    }

    fn find_key(&mut self, key: KeyEvent) {
        match (key.code, key.modifiers) {
            (KeyCode::Esc, _) => self.find = None,
            (KeyCode::Enter, m) if m.contains(KeyModifiers::SHIFT) => self.step_find(false),
            (KeyCode::Enter | KeyCode::Down, _) => self.step_find(true),
            (KeyCode::Up, _) => self.step_find(false),
            (KeyCode::Char('n' | 'f'), KeyModifiers::CONTROL) => self.step_find(true),
            (KeyCode::Char('p'), KeyModifiers::CONTROL) => self.step_find(false),
            (KeyCode::Backspace, _) => {
                if let Some(find) = self.find.as_mut() {
                    find.query.pop();
                    find.current = 0;
                }
                self.scroll_to_find();
            }
            (KeyCode::Char(c), m) if (m - KeyModifiers::SHIFT).is_empty() => {
                if let Some(find) = self.find.as_mut() {
                    find.query.push(c);
                    find.current = 0;
                }
                self.scroll_to_find();
            }
            _ => {}
        }
    }

    fn step_find(&mut self, forward: bool) {
        let n = self.finds().hits.len();
        if n == 0 {
            return;
        }
        if let Some(find) = self.find.as_mut() {
            let cur = find.current.min(n - 1);
            find.current = if forward { (cur + 1) % n } else { (cur + n - 1) % n };
        }
        self.scroll_to_find();
    }

    /// Bring the current hit on screen, leaving the scroll alone if it already
    /// is — a find that jumps the page on every keystroke is unreadable.
    fn scroll_to_find(&mut self) {
        let finds = self.finds();
        let Some(hit) = finds.hits.get(finds.current).copied() else { return };
        let height = self.areas.doc_body.height as usize;
        let Some(open) = self.open.as_mut() else { return };
        if hit.line < open.scroll {
            open.scroll = hit.line;
        } else if height > 0 && hit.line >= open.scroll + height {
            open.scroll = hit.line + 1 - height;
        }
    }

    /// The editor is modeless: every key is text unless it is one of the few
    /// the editor itself claims. A visible completion popup claims its own
    /// navigation keys first.
    fn editor_key(&mut self, key: KeyEvent) {
        let has_completion = self
            .open
            .as_ref()
            .and_then(|o| o.editor.as_ref())
            .is_some_and(|e| e.completion.is_some());

        if has_completion {
            let editor = self.open.as_mut().unwrap().editor.as_mut().unwrap();
            let handled = match (key.code, key.modifiers) {
                (KeyCode::Tab, _) | (KeyCode::Down, _) => {
                    editor.completion.as_mut().unwrap().move_by(1);
                    true
                }
                (KeyCode::BackTab, _) | (KeyCode::Up, _) => {
                    editor.completion.as_mut().unwrap().move_by(-1);
                    true
                }
                (KeyCode::Enter, _) => {
                    editor.accept_completion();
                    true
                }
                (KeyCode::Esc, _) => {
                    editor.dismiss_completion();
                    true
                }
                _ => false,
            };
            if handled {
                return;
            }
        }

        if let Resolved::Run(cmd) = keymap::resolve(&key, Ctx::Edit, None) {
            return self.run(cmd);
        }

        // Markdown-aware keys the editor handles before edtui sees them.
        match (key.code, key.modifiers) {
            (KeyCode::Enter, KeyModifiers::NONE) => {
                let continued = self
                    .open
                    .as_mut()
                    .and_then(|o| o.editor.as_mut())
                    .is_some_and(Editor::continue_block);
                if continued {
                    return;
                }
            }
            (KeyCode::Tab, _) => {
                self.with_editor(|editor| editor.indent(false));
                return;
            }
            (KeyCode::BackTab, _) => {
                self.with_editor(|editor| editor.indent(true));
                return;
            }
            (KeyCode::Down, KeyModifiers::NONE) => {
                self.with_editor(|editor| editor.move_visual(true));
                return;
            }
            (KeyCode::Up, KeyModifiers::NONE) => {
                self.with_editor(|editor| editor.move_visual(false));
                return;
            }
            _ => {}
        }

        if let Some(editor) = self.open.as_mut().and_then(|o| o.editor.as_mut()) {
            editor.events.on_key_event(key, &mut editor.state);
            editor.keep_modeless();
        }
        // Typing again means the warning has to be re-earned.
        self.discard_armed = false;
        self.overwrite_armed = false;
        self.refresh_completion();
    }

    fn with_editor(&mut self, action: impl FnOnce(&mut Editor)) {
        if let Some(editor) = self.open.as_mut().and_then(|o| o.editor.as_mut()) {
            action(editor);
        }
    }

    fn refresh_completion(&mut self) {
        let (root, index) = (self.cfg.root.clone(), &self.index);
        if let Some(open) = self.open.as_mut() {
            let page = &open.page;
            if let Some(editor) = open.editor.as_mut() {
                editor.refresh_completion(page, index, &root);
            }
        }
    }

    // -------------------------------------------------------------- commands

    /// Whether leaving the app should be held back for one press.
    ///
    /// A sync mid-flight and an unsaved buffer are both things not to discard
    /// by reflex; say so once, then take the second press as meaning it. The
    /// editor already guards `esc` this way — quitting is the same loss by a
    /// different key, so it asks the same question.
    fn exit_blocked(&mut self, again: &str) -> bool {
        if self.quit_confirmed {
            return false;
        }
        let warning = if self.jobs.is_busy() {
            let running = self.jobs.labels().join(", ");
            Some(format!("still running: {running} — {again} to quit"))
        } else if self.open.as_ref().and_then(|o| o.editor.as_ref()).is_some_and(Editor::dirty) {
            Some(format!("unsaved changes — ctrl+s to save, {again} to discard"))
        } else {
            None
        };
        match warning {
            Some(message) => {
                self.quit_confirmed = true;
                self.toast(Level::Warn, message);
                true
            }
            None => false,
        }
    }

    pub fn run(&mut self, cmd: Cmd) {
        match cmd {
            Cmd::ClearMarks => {
                let count: usize = self.marks.values().map(Vec::len).sum();
                self.marks.clear();
                self.toast(Level::Info, format!("cleared {count} agent mark(s)"));
            }
            Cmd::Quit => {
                if !self.exit_blocked("press ctrl+q again") {
                    self.quit = true;
                }
            }
            Cmd::Help => self.overlay = Some(Overlay::Help { scroll: 0 }),
            Cmd::Palette => self.open_palette(),
            Cmd::Restart => {
                // A restart is a quit with a re-exec on the way out, so it
                // carries the same guard — except for the unsaved buffer.
                // Restart is reachable only from the palette, and opening the
                // palette resets the confirmation, so "choose it again" is a
                // prompt that can never be satisfied. An instruction can be.
                if self.open.as_ref().and_then(|o| o.editor.as_ref()).is_some_and(Editor::dirty) {
                    self.toast(
                        Level::Warn,
                        "unsaved changes — ctrl+s to save or esc to discard, then restart",
                    );
                } else if !self.exit_blocked("choose restart again") {
                    self.restart = true;
                    self.quit = true;
                }
            }
            Cmd::Theme => {
                let selected = Flavor::ALL.iter().position(|f| *f == self.cfg.flavor).unwrap_or(0);
                self.overlay = Some(Overlay::Themes { selected, original: self.cfg.flavor });
            }
            Cmd::Projects => self.open_projects(),

            Cmd::FindFiles => self.open_finder(search::Mode::Files),
            Cmd::FindText => self.open_finder(search::Mode::Text),
            Cmd::FindSemantic => self.open_finder(search::Mode::Semantic),
            Cmd::FindInPage => self.open_find(),

            Cmd::FocusTree => self.set_focus(Focus::Tree),
            Cmd::FocusDoc => self.set_focus(Focus::Doc),
            Cmd::FocusSidebar => self.set_focus(Focus::Sidebar),
            Cmd::CycleFocus => self.cycle_focus(1),
            Cmd::CycleFocusBack => self.cycle_focus(-1),
            Cmd::ToggleTree => {
                self.show_tree = !self.show_tree;
                if !self.show_tree && self.focus == Focus::Tree {
                    self.focus = Focus::Doc;
                }
            }
            Cmd::ToggleSidebar => {
                self.show_sidebar = !self.show_sidebar;
                if self.show_sidebar {
                    // Showing it again is also how you retry after a failure.
                    self.sidebar_error = None;
                } else if self.focus == Focus::Sidebar {
                    self.focus = Focus::Doc;
                }
            }
            Cmd::ToggleInspector => {
                self.show_inspector = !self.show_inspector;
                if !self.show_inspector {
                    self.doc_stop = DocStop::Content;
                }
            }
            Cmd::ShrinkInspector => self.adjust_inspector_height(-2),
            Cmd::GrowInspector => self.adjust_inspector_height(2),
            Cmd::ZoomPane => self.zoom = !self.zoom,
            Cmd::ShrinkPane => self.shrink_active_pane(),
            Cmd::WidenPane => self.widen_active_pane(),
            Cmd::ShrinkTree => self.adjust_tree_width(-4),
            Cmd::WidenTree => self.adjust_tree_width(4),
            Cmd::ShrinkSidebar => self.adjust_sidebar_width(-4),
            Cmd::WidenSidebar => self.adjust_sidebar_width(4),
            Cmd::LeaveSidebar => self.cycle_focus(1),

            Cmd::Back => self.go_back(),
            Cmd::Forward => self.go_forward(),
            Cmd::Home => self.go_home(),
            Cmd::ToggleCollection => self.toggle_collection(),
            Cmd::RevealInTree => self.reveal_open(),
            Cmd::NextFinding => self.jump_finding(1),
            Cmd::PrevFinding => self.jump_finding(-1),

            Cmd::TreeDown => self.tree.move_by(1),
            Cmd::TreeUp => self.tree.move_by(-1),
            Cmd::TreeExpand => {
                if let Some(path) = self.tree.expand() {
                    self.open_path(&path.clone(), true);
                }
            }
            Cmd::TreeCollapse => self.tree.collapse(),
            Cmd::TreeToggle => {
                if let Some(path) = self.tree.toggle() {
                    self.open_path(&path.clone(), true);
                }
            }
            Cmd::TreeNextSibling => self.tree.move_sibling(true),
            Cmd::TreePrevSibling => self.tree.move_sibling(false),
            Cmd::TreeCollapseAll => self.tree.collapse_all(),
            Cmd::TreeTop => self.tree.move_to(0),
            Cmd::TreeBottom => self.tree.move_to(usize::MAX),

            Cmd::ScrollDown => self.scroll(1),
            Cmd::ScrollUp => self.scroll(-1),
            Cmd::HalfPageDown => self.scroll(self.page_step() / 2),
            Cmd::HalfPageUp => self.scroll(-(self.page_step() / 2)),
            Cmd::PageDown => self.scroll(self.page_step()),
            Cmd::PageUp => self.scroll(-self.page_step()),
            Cmd::DocTop => self.scroll(isize::MIN / 2),
            Cmd::DocBottom => self.scroll(isize::MAX / 2),
            Cmd::PanLeft => self.pan(-Self::PAN_STEP),
            Cmd::PanRight => self.pan(Self::PAN_STEP),
            Cmd::NextLink => self.move_link(true),
            Cmd::PrevLink => self.move_link(false),
            Cmd::FollowLink => self.follow_link(),
            Cmd::Outline => {
                if self.open.as_ref().is_some_and(|o| !o.doc.headings.is_empty()) {
                    self.overlay = Some(Overlay::Outline { selected: 0 });
                }
            }
            Cmd::Copy => self.copy_selection_or_page(),
            Cmd::ClearSelection => self.clear_selection(),

            Cmd::Edit => self.enter_editor(),
            Cmd::Save => self.save(),
            Cmd::LeaveEdit => self.leave_editor(),
            Cmd::Bold => self.with_editor(|editor| editor.wrap_emphasis("**")),
            Cmd::Italic => self.with_editor(|editor| editor.wrap_emphasis("_")),
            Cmd::Link => {
                self.with_editor(crate::editor::Editor::insert_link);
                self.refresh_completion();
            }
            Cmd::Undo => self.with_editor(crate::editor::Editor::undo),
            Cmd::Redo => self.with_editor(crate::editor::Editor::redo),

            Cmd::Lint => self.run_lint(),
            Cmd::SyncRepos => self.run_sync_repos(),
            Cmd::Commit => self.prompt(PromptKind::Commit, "commit message"),
            Cmd::NewPage => self.prompt(PromptKind::NewPage, "new page path (relative to the checkout)"),
            Cmd::Uncited => self.show_uncited(),
            Cmd::Extensions => self.open_extensions(),
        }
    }

    /// Tab-bar click: unlike `set_focus`, this may reopen a pane the user had
    /// hidden — clicking a tab is asking for that pane, not just its focus.
    fn select_tab(&mut self, focus: Focus) {
        match focus {
            Focus::Tree => self.show_tree = true,
            Focus::Sidebar => {
                self.show_sidebar = true;
                self.sidebar_error = None;
            }
            Focus::Doc => {}
        }
        self.set_focus(focus);
    }

    fn set_focus(&mut self, focus: Focus) {
        let next = match focus {
            Focus::Tree if !self.show_tree => Focus::Doc,
            Focus::Sidebar if !self.show_sidebar || self.sidebar.is_none() => self.focus,
            other => other,
        };
        if next != self.focus {
            if self.focus == Focus::Sidebar {
                if let Some(pane) = self.sidebar.as_mut() {
                    pane.send(b"\x1b[O");
                }
            }
            if next == Focus::Sidebar {
                if let Some(pane) = self.sidebar.as_mut() {
                    pane.send(b"\x1b[I");
                }
            }
            self.focus = next;
        }
        if next == Focus::Doc {
            // A direct jump (a number key, a tab-bar click, opening a page)
            // always means the reader, never the sources box a previous
            // Tab-cycle happened to leave selected.
            self.doc_stop = DocStop::Content;
        }
        self.zoom = false;
    }

    /// Every Tab-cycle stop currently on screen, in order: each tree
    /// collection box, the reader, the sources box (only once it is actually
    /// rendered — the layout can collapse it on a short terminal even with
    /// `show_inspector` set), then the agents pane.
    fn stops(&self) -> Vec<Stop> {
        let mut out = Vec::new();
        if self.show_tree {
            out.extend((0..self.tree.collection_paths().len()).map(Stop::TreeSection));
        }
        out.push(Stop::DocContent);
        if self.open.is_some() && self.show_inspector && !self.areas.inspector.is_empty() {
            out.push(Stop::DocSources);
        }
        if self.show_sidebar && self.sidebar.is_some() {
            out.push(Stop::Sidebar);
        }
        out
    }

    fn current_stop(&self) -> Stop {
        match self.focus {
            Focus::Tree => {
                let i = (0..self.tree.collection_paths().len())
                    .find(|&i| {
                        self.tree
                            .section_span(i)
                            .is_some_and(|(start, end)| self.tree.selected >= start && self.tree.selected < end)
                    })
                    .unwrap_or(0);
                Stop::TreeSection(i)
            }
            Focus::Sidebar => Stop::Sidebar,
            Focus::Doc => match self.doc_stop {
                DocStop::Content => Stop::DocContent,
                DocStop::Sources => Stop::DocSources,
            },
        }
    }

    fn goto_stop(&mut self, stop: Stop) {
        match stop {
            Stop::TreeSection(i) => {
                self.focus = Focus::Tree;
                if let Some((start, end)) = self.tree.section_span(i) {
                    if !(self.tree.selected >= start && self.tree.selected < end) {
                        self.tree.selected = start;
                    }
                }
            }
            Stop::DocContent => {
                self.focus = Focus::Doc;
                self.doc_stop = DocStop::Content;
            }
            Stop::DocSources => {
                self.focus = Focus::Doc;
                self.doc_stop = DocStop::Sources;
            }
            Stop::Sidebar => self.focus = Focus::Sidebar,
        }
    }

    fn cycle_focus(&mut self, delta: isize) {
        let stops = self.stops();
        if stops.is_empty() {
            return;
        }
        let cur = self.current_stop();
        let at = stops.iter().position(|s| *s == cur).unwrap_or(0) as isize;
        let n = stops.len() as isize;
        let next = stops[(((at + delta) % n + n) % n) as usize];
        self.goto_stop(next);
    }

    /// Rows of document the reader can actually show. The sources pane sits
    /// *below* the reader inside the same column, so measuring the column
    /// would count its rows as places content could scroll into — and the
    /// last screenful would stay unreachable, reading as if the pane were
    /// drawn over the end of the page.
    fn viewport_height(&self) -> usize {
        let rows = if self.areas.doc_body.height > 0 {
            self.areas.doc_body.height
        } else if !self.areas.doc_main.is_empty() {
            self.areas.doc_main.height.saturating_sub(2)
        } else {
            self.areas.doc.height.saturating_sub(2)
        };
        rows.max(1) as usize
    }

    fn page_step(&self) -> isize {
        self.viewport_height() as isize
    }

    /// Columns a single pan moves. Wide enough to make progress across a broad
    /// table, narrow enough that you can still line a column up under the edge.
    const PAN_STEP: isize = 8;

    /// Pan a table wider than the pane. Clamped to the doc's own width, so the
    /// right edge of the box is as far as it goes and a prose page — never
    /// wider than its measure — cannot be panned at all.
    fn pan(&mut self, delta: isize) {
        let view = self.viewport_width();
        if let Some(open) = self.open.as_mut() {
            let last = open.doc.width.saturating_sub(view);
            open.hscroll = (open.hscroll as isize + delta).clamp(0, last as isize) as usize;
        }
    }

    pub(crate) fn viewport_width(&self) -> usize {
        let cols = if self.areas.doc_body.width > 0 {
            self.areas.doc_body.width
        } else {
            self.areas.doc.width.saturating_sub(4)
        };
        cols.max(1) as usize
    }

    fn scroll(&mut self, delta: isize) {
        if self.focus == Focus::Doc && self.doc_stop == DocStop::Sources {
            // The inspector clamps this against its own row count at render
            // time, so an out-of-range value here is harmless.
            if let Some(open) = self.open.as_mut() {
                open.inspect_scroll = (open.inspect_scroll as isize + delta).max(0) as usize;
            }
            return;
        }
        let height = self.viewport_height();
        if let Some(open) = self.open.as_mut() {
            let last = open.doc.height().saturating_sub(height.min(open.doc.height()));
            open.scroll = (open.scroll as isize + delta).clamp(0, last as isize) as usize;
        }
    }

    fn move_link(&mut self, forward: bool) {
        let height = self.viewport_height();
        if let Some(open) = self.open.as_mut() {
            open.link = open.doc.link_after(open.link, forward);
            if let Some(link) = open.link.and_then(|i| open.doc.links.get(i)) {
                // Keep the highlighted link on screen.
                if link.line < open.scroll || link.line >= open.scroll + height {
                    open.scroll = link.line.saturating_sub(height / 3);
                }
                if link.kind == LinkKind::Footnote {
                    open.selected_citation = Some(link.target.clone());
                    open.citation_scroll_pending = true;
                } else {
                    open.selected_citation = None;
                }
            } else {
                open.selected_citation = None;
            }
        }
    }

    fn follow_link(&mut self) {
        let Some(open) = self.open.as_ref() else { return };
        let Some(link) = open.link.and_then(|i| open.doc.links.get(i)).cloned() else {
            self.toast(Level::Info, "no link selected — use l / h to pick one");
            return;
        };
        match link.kind {
            LinkKind::Relative => {
                let dir = open.page.path.parent().unwrap_or(&self.cfg.root).to_path_buf();
                let bare = link.target.split(['#', '?']).next().unwrap_or(&link.target);
                let target = crate::vault::links::normalize(&dir.join(bare));
                if target.starts_with(&self.cfg.root) && target.exists() {
                    self.open_path(&target, true);
                    self.reveal_open();
                } else {
                    self.toast(Level::Bad, format!("broken link: {}", link.target));
                }
            }
            LinkKind::External => self.toast(Level::Info, format!("external: {}", link.target)),
            LinkKind::Anchor => self.jump_to_anchor(&link.target),
            LinkKind::Wiki => self.toast(Level::Warn, "wikilinks are forbidden — use a relative link"),
            LinkKind::Footnote => self.select_citation(&link.target),
        }
    }

    /// Highlight a citation in the inspector's sources section — from a click
    /// or keyboard nav on its `[n]` mark in the body — and queue it to be
    /// scrolled into view on the next inspector render.
    fn select_citation(&mut self, id: &str) {
        self.show_inspector = true;
        if let Some(open) = self.open.as_mut() {
            open.selected_citation = Some(id.to_string());
            open.citation_scroll_pending = true;
        }
    }

    /// The mirror of `select_citation`: a source picked in the inspector's
    /// sources section highlights the `[n]` mark that cites it in the body and
    /// scrolls it into view.
    fn select_source(&mut self, id: &str) {
        self.show_inspector = true;
        let Some(open) = self.open.as_mut() else { return };
        open.selected_citation = Some(id.to_string());
        open.citation_scroll_pending = true;
        if let Some(link) = open.doc.citation_link(id) {
            open.link = Some(link);
            let line = open.doc.links[link].line;
            let height = self.areas.doc_body.height.saturating_sub(1).max(1) as usize;
            if line < open.scroll || line >= open.scroll + height {
                open.scroll = line.saturating_sub(height / 3);
            }
        }
    }

    /// Open a source's own file (its `resource:` path) in the main document
    /// pane — the reader is the only "visualizer" this app has, so opening a
    /// source means reading it the same way as any wiki page.
    pub fn open_source(&mut self, id: &str) {
        let Some(open) = self.open.as_ref() else { return };
        // A frontmatter `sources:` entry names its own `resource:` path; a
        // footnote-only citation carries its link target on the definition line.
        let dir = open.page.path.parent().unwrap_or(&self.cfg.root).to_path_buf();
        let resource = open
            .page
            .okf
            .sources
            .iter()
            .find(|s| s.id == id)
            .and_then(|s| s.resource.as_deref().map(str::to_string))
            .or_else(|| {
                open.page
                    .footnote_defs()
                    .into_iter()
                    .find(|(fid, _, _)| fid == id)
                    .and_then(|(_, _, target)| target)
            });
        let Some(resource) = resource else {
            self.toast(Level::Info, format!("{id} has no resource path"));
            return;
        };
        let target = crate::vault::links::normalize(&dir.join(&resource));
        if target.starts_with(&self.cfg.root) && target.exists() {
            self.open_path(&target, true);
        } else {
            self.toast(Level::Bad, format!("broken source: {resource}"));
        }
    }

    pub fn reader_body(&self) -> Rect {
        if !self.areas.doc_body.is_empty() {
            return self.areas.doc_body;
        }
        let area = self.areas.doc;
        if area.is_empty() {
            return Rect::default();
        }
        let min_main = 8u16;
        let min_inspect = 4u16;
        let main = if self.show_inspector && area.height >= min_main + min_inspect {
            let max_inspect = area.height.saturating_sub(min_main);
            let height = self.cfg.inspector_height.clamp(min_inspect, max_inspect);
            let [main, _] = Layout::vertical([Constraint::Min(min_main), Constraint::Length(height)]).areas(area);
            main
        } else {
            area
        };
        let inner_x = main.x.saturating_add(2);
        let inner_y = main.y.saturating_add(1);
        let inner_w = main.width.saturating_sub(4);
        let inner_h = main.height.saturating_sub(2);
        Rect::new(
            inner_x,
            inner_y.saturating_add(2),
            inner_w,
            inner_h.saturating_sub(2),
        )
    }

    pub fn char_pos_at(&self, x: u16, y: u16) -> Option<markdown::TextPos> {
        let open = self.open.as_ref()?;
        if open.doc.lines.is_empty() {
            return None;
        }
        let body = self.reader_body();
        if body.is_empty() || body.height == 0 {
            return None;
        }

        let rel_y = if y < body.y {
            0
        } else if y >= body.y + body.height {
            body.height.saturating_sub(1) as usize
        } else {
            (y - body.y) as usize
        };

        let line_idx = (open.scroll + rel_y).min(open.doc.lines.len().saturating_sub(1));
        let line = &open.doc.lines[line_idx];
        let plain = line.plain_text();

        // The pan moved the text under the pointer, so the clicked cell is
        // that many columns further into the line than the pane suggests.
        let rel_x = (x as isize - body.x as isize).max(0) as usize + open.hscroll;
        let mut cur_col = 0usize;
        let mut char_idx = 0usize;
        for c in plain.chars() {
            let w = unicode_width::UnicodeWidthChar::width(c).unwrap_or(1);
            if cur_col + w > rel_x {
                break;
            }
            cur_col += w;
            char_idx += 1;
        }

        Some(markdown::TextPos { line: line_idx, col: char_idx })
    }

    pub fn start_selection(&mut self, x: u16, y: u16) {
        if let Some(pos) = self.char_pos_at(x, y) {
            self.selecting_text = true;
            if let Some(open) = self.open.as_mut() {
                open.selection = Some(markdown::Selection::new(pos, pos));
            }
        }
    }

    pub fn extend_selection(&mut self, x: u16, y: u16) {
        let body = self.reader_body();
        if !body.is_empty() {
            if y < body.y {
                self.scroll(-1);
            } else if y >= body.y + body.height {
                self.scroll(1);
            }
        }

        if let Some(pos) = self.char_pos_at(x, y) {
            if let Some(open) = self.open.as_mut() {
                if let Some(sel) = open.selection.as_mut() {
                    sel.cursor = pos;
                }
            }
        }
    }

    pub fn finish_selection(&mut self, x: u16, y: u16) {
        self.selecting_text = false;
        let click_pos = self.char_pos_at(x, y);
        if let Some(pos) = click_pos {
            if let Some(open) = self.open.as_mut() {
                if let Some(sel) = open.selection.as_mut() {
                    sel.cursor = pos;
                }
            }
        }
        let Some(open) = self.open.as_mut() else { return };
        let Some(sel) = open.selection else { return };
        if sel.is_empty() {
            open.selection = None;
            // A click without dragging on a link activates and follows that link
            let mut clicked_link = None;
            if let Some(pos) = click_pos {
                if let Some(line) = open.doc.lines.get(pos.line) {
                    let mut cur = 0;
                    for seg in &line.segs {
                        let len = seg.text.chars().count();
                        if pos.col >= cur && pos.col < cur + len {
                            if let Some(link_idx) = seg.link {
                                open.link = Some(link_idx);
                                clicked_link = Some(link_idx);
                            }
                            break;
                        }
                        cur += len;
                    }
                }
            }
            if clicked_link.is_some() {
                self.follow_link();
            }
            return;
        }
        let text = open.selected_source_text(sel);
        if !text.is_empty() {
            crate::clipboard::copy(&text);
            let n = text.lines().count().max(1);
            let label = if n == 1 { "copied 1 line".to_string() } else { format!("copied {n} lines") };
            self.toast(Level::Good, label);
        } else {
            open.selection = None;
        }
    }

    pub fn clear_selection(&mut self) {
        if let Some(open) = self.open.as_mut() {
            open.selection = None;
        }
    }

    /// Right-button release ends the drag the same way as a left one, but the
    /// selection is paid to the agents pane as a `@path:lines` mention rather
    /// than copied to the clipboard. A click without a drag selects nothing and
    /// sends nothing — and never follows a link, which is a left-button habit.
    pub fn finish_selection_right(&mut self, x: u16, y: u16) {
        self.selecting_text = false;
        self.selecting_right = false;
        if let Some(pos) = self.char_pos_at(x, y) {
            if let Some(open) = self.open.as_mut() {
                if let Some(sel) = open.selection.as_mut() {
                    sel.cursor = pos;
                }
            }
        }
        let Some(mention) = self.selection_mention() else {
            self.clear_selection();
            return;
        };
        self.send_to_agent(&mention);
    }

    /// The `@<rel>:<first>-<last>` mention for the current selection, if there
    /// is a non-empty one. Line numbers are the file's real 1-based numbers:
    /// `source_for_line` indexes into the body, which starts at `body_start`.
    fn selection_mention(&self) -> Option<String> {
        let open = self.open.as_ref()?;
        let sel = open.selection.filter(|s| !s.is_empty())?;
        let (start, end) = sel.range();
        let first = open.page.body_start + open.doc.source_for_line(start.line) + 1;
        let last = open.page.body_start + open.doc.source_for_line(end.line) + 1;
        let range = if first == last { format!("{first}") } else { format!("{first}-{last}") };
        Some(format!("@{}:{range}", open.page.rel))
    }

    /// Type `text` into the herdr pane and press enter, so the mention arrives
    /// in the agent session exactly as if the user had typed and submitted it.
    fn send_to_agent(&mut self, text: &str) {
        let Some(pane) = self.sidebar.as_mut() else {
            self.toast(Level::Warn, "no agent session — open the agents pane (ctrl+g)");
            return;
        };
        if !pane.is_alive() {
            self.toast(Level::Warn, "the agent session has exited — ctrl+g to restart");
            return;
        }
        let mut bytes = text.as_bytes().to_vec();
        bytes.push(b'\r');
        pane.send(&bytes);
        self.toast(Level::Good, "sent selection to the agent");
    }

    pub fn copy_selection_or_page(&mut self) {
        let Some(open) = self.open.as_ref() else { return };
        if let Some(sel) = open.selection {
            if !sel.is_empty() {
                let text = open.selected_source_text(sel);
                if !text.is_empty() {
                    crate::clipboard::copy(&text);
                    let n = text.lines().count().max(1);
                    let label = if n == 1 { "copied 1 line".to_string() } else { format!("copied {n} lines") };
                    self.toast(Level::Good, label);
                    return;
                }
            }
        }
        crate::clipboard::copy(&open.page.body);
        self.toast(Level::Good, "copied page to clipboard");
    }

    fn jump_to_anchor(&mut self, anchor: &str) {
        let needle = anchor.trim_start_matches('#').replace('-', " ").to_lowercase();
        let Some(open) = self.open.as_mut() else { return };
        if let Some((line, _, _)) = open
            .doc
            .headings
            .iter()
            .find(|(_, _, text)| text.to_lowercase().replace('-', " ") == needle)
        {
            open.scroll = *line;
        }
    }

    fn reveal_open(&mut self) {
        if let Some(path) = self.open.as_ref().map(|o| o.page.path.clone()) {
            self.tree.reveal(&path);
        }
    }

    /// Where the reader is right now.
    pub fn here(&self) -> Spot {
        match self.open.as_ref() {
            Some(open) => Spot::Page(open.page.path.clone()),
            None => Spot::Home,
        }
    }

    /// Go to a destination without touching the history — the caller has
    /// already recorded whatever move it is making.
    fn go_to(&mut self, spot: Spot) {
        match spot {
            Spot::Home => self.show_home(),
            Spot::Page(path) => self.open_path(&path, false),
        }
    }

    /// Close the open page and show the home screen.
    ///
    /// The find bar belongs to the page that was open, so it closes with it;
    /// leaving it up would search a page that is no longer on screen.
    fn show_home(&mut self) {
        self.open = None;
        self.find = None;
        self.focus = Focus::Doc;
        self.doc_stop = DocStop::Content;
    }

    /// The `home` command: go to the mark, recording the move so `forward`
    /// brings you back to the page you left.
    fn go_home(&mut self) {
        if self.open.is_none() {
            return;
        }
        self.back.push(self.here());
        self.forward.clear();
        self.show_home();
    }

    fn go_back(&mut self) {
        let Some(previous) = self.back.pop() else { return };
        self.forward.push(self.here());
        self.go_to(previous);
    }

    fn go_forward(&mut self) {
        let Some(next) = self.forward.pop() else { return };
        self.back.push(self.here());
        self.go_to(next);
    }

    fn jump_finding(&mut self, delta: isize) {
        let findings: Vec<(String, Option<usize>)> = self
            .index
            .all_findings()
            .into_iter()
            .map(|(rel, f)| (rel, f.line))
            .collect();
        if findings.is_empty() {
            self.toast(Level::Good, "no findings — the vault is clean");
            return;
        }
        let n = findings.len() as isize;
        // The first `n` lands on the first finding rather than the second.
        let at = match self.finding_cursor {
            None if delta > 0 => 0,
            None => n - 1,
            Some(at) => ((at as isize + delta) % n + n) % n,
        };
        self.finding_cursor = Some(at as usize);
        let (rel, line) = findings[at as usize].clone();
        let path = self.cfg.root.join(&rel);
        if path.is_file() {
            self.open_path(&path, true);
            if let (Some(open), Some(line)) = (self.open.as_mut(), line) {
                open.scroll = open.doc.line_for_source(line.saturating_sub(open.page.body_start));
            }
        }
        self.tree.reveal(&path);
        self.toast(
            Level::Info,
            format!("finding {}/{}: {rel}", at + 1, findings.len()),
        );
    }

    fn reload_everything(&mut self) {
        self.tree.rebuild();
        self.refresh_git();
        self.reload_open();
        self.start_indexing();
        self.toast(Level::Info, "reloading");
    }

    // --------------------------------------------------------------- editing

    fn enter_editor(&mut self) {
        let Some(open) = self.open.as_mut() else { return };
        if open.editing() {
            return;
        }
        let line = open.page.body_start + open.doc.source_for_line(open.scroll);
        let mut editor = Editor::open(&open.page);
        editor.goto_line(line);
        open.editor = Some(editor);
        self.focus = Focus::Doc;
        self.discard_armed = false;
        self.overwrite_armed = false;
    }

    fn save(&mut self) {
        self.discard_armed = false;
        let armed = std::mem::take(&mut self.overwrite_armed);
        let Some(open) = self.open.as_mut() else { return };
        let Some(editor) = open.editor.as_mut() else { return };
        let path = editor.path.clone();

        // Armed by a previous ctrl+s that hit a conflict — the same
        // arm-then-confirm shape as discarding an unsaved buffer, so the
        // second press is the answer rather than a new dialog.
        if armed {
            match editor.save_overwriting() {
                Ok(backup) => {
                    let kept = crate::vault::page::rel_path(&backup, &self.cfg.root);
                    self.after_save(&path);
                    self.toast(Level::Warn, format!("saved — their version kept as {kept}"));
                }
                Err(err) => self.toast(Level::Bad, format!("save failed: {err}")),
            }
            return;
        }

        match editor.save() {
            Ok(Save::Written) => {
                self.after_save(&path);
                let rel = crate::vault::page::rel_path(&path, &self.cfg.root);
                self.toast(Level::Good, format!("saved {rel}"));
            }
            Ok(Save::ChangedUnderneath) => {
                self.overwrite_armed = true;
                self.toast(
                    Level::Warn,
                    "changed on disk since you opened it — ctrl+s again to overwrite (a copy is kept), esc esc to discard yours",
                );
            }
            Err(err) => self.toast(Level::Bad, format!("save failed: {err}")),
        }
    }

    /// Re-index and re-parse after a write that actually landed.
    fn after_save(&mut self, path: &Path) {
        if path.extension().and_then(|e| e.to_str()) == Some("md") {
            self.index.refresh(path);
        }
        self.refresh_page_after_save();
    }

    /// Re-parse the page so the inspector and findings reflect what was saved,
    /// without closing the editor. A CSV re-renders its table grid the same way.
    fn refresh_page_after_save(&mut self) {
        let root = self.cfg.root.clone();
        if let Some(open) = self.open.as_mut() {
            if let Ok(page) = Page::load(&open.page.path, &root) {
                open.page = page;
                if open.csv() {
                    let width = open.doc_width.max(20);
                    open.doc = crate::ui::csv::Grid::parse(&open.page.body).to_doc(width, &self.theme);
                }
            }
        }
    }

    /// Leaving with unsaved changes asks once, then discards on a second press.
    fn leave_editor(&mut self) {
        let dirty = self
            .open
            .as_ref()
            .and_then(|o| o.editor.as_ref())
            .is_some_and(Editor::dirty);

        if dirty && !self.discard_armed {
            self.discard_armed = true;
            self.toast(Level::Warn, "unsaved changes — ctrl+s to save, esc again to discard");
            return;
        }
        self.discard_armed = false;
        self.overwrite_armed = false;
        if let Some(open) = self.open.as_mut() {
            open.editor = None;
        }
        self.reload_open();
    }

    // --------------------------------------------------------------- overlays

    fn open_finder(&mut self, mode: search::Mode) {
        let mut finder = Finder {
            mode,
            query: String::new(),
            hits: Vec::new(),
            selected: 0,
            collection: None,
            running: None,
            warning: None,
        };
        if mode.is_live() {
            finder.hits = self.finder_engine.files(&self.index, "", None);
        }
        self.overlay = Some(Overlay::Finder(finder));
    }

    /// Sources ingested but never cited by a `wiki/` page. Derived live from
    /// the citation graph, which is the same thing `literature_status` does —
    /// there is no manifest to fall out of date.
    fn show_uncited(&mut self) {
        let hits: Vec<Hit> = self
            .index
            .uncited_sources()
            .into_iter()
            .map(|entry| Hit {
                path: entry.path.clone(),
                rel: entry.rel.clone(),
                title: entry.title.clone(),
                line: None,
                snippet: "not cited by any wiki page".into(),
                matched: Vec::new(),
                score: 0,
            })
            .collect();
        if hits.is_empty() {
            return self.toast(Level::Good, "every ingested source is cited");
        }
        let count = hits.len();
        self.overlay = Some(Overlay::Finder(Finder {
            mode: search::Mode::Files,
            query: String::new(),
            hits,
            selected: 0,
            collection: Some("sources"),
            running: None,
            warning: Some(format!("{count} ingested sources are not cited by any wiki page")),
        }));
    }

    /// Switch theme: repaint, re-wrap the page, and hand the new flavour to
    /// the agents pane so the two never disagree.
    fn apply_flavor(&mut self, flavor: Flavor) {
        if self.cfg.flavor == flavor {
            return;
        }
        self.cfg.flavor = flavor;
        self.theme = Theme::new(flavor);
        // The document is re-rendered rather than re-wrapped: every span
        // carries a colour from the old theme.
        let (width, theme) = (self.doc_width(), self.theme);
        if let Some(open) = self.open.as_mut() {
            open.doc_width = 0;
            open.reflow(width, &theme);
        }
        herdr::pty::reload_config(flavor);
    }

    fn open_palette(&mut self) {
        let items = palette_items("");
        self.overlay = Some(Overlay::Palette(Palette { query: String::new(), items, selected: 0 }));
    }

    fn prompt(&mut self, kind: PromptKind, title: &str) {
        self.overlay = Some(Overlay::Prompt(Prompt {
            kind,
            title: title.to_string(),
            value: String::new(),
            path: None,
        }));
    }

    fn overlay_key(&mut self, key: KeyEvent) {
        match self.overlay.as_mut() {
            Some(Overlay::Help { scroll }) => match key.code {
                KeyCode::Esc | KeyCode::Char('q') | KeyCode::Char('?') => self.overlay = None,
                KeyCode::Down | KeyCode::Char('j') => *scroll += 1,
                KeyCode::Up | KeyCode::Char('k') => *scroll = scroll.saturating_sub(1),
                _ => {}
            },
            Some(Overlay::Outline { selected }) => match key.code {
                KeyCode::Esc | KeyCode::Char('q') => self.overlay = None,
                KeyCode::Down | KeyCode::Char('j') => *selected += 1,
                KeyCode::Up | KeyCode::Char('k') => *selected = selected.saturating_sub(1),
                KeyCode::Enter => {
                    let at = *selected;
                    self.overlay = None;
                    if let Some(open) = self.open.as_mut() {
                        if let Some((line, _, _)) = open.doc.headings.get(at) {
                            open.scroll = *line;
                        }
                    }
                }
                _ => {}
            },
            Some(Overlay::Themes { .. }) => self.theme_key(key),
            Some(Overlay::Projects { .. }) => self.project_key(key),
            Some(Overlay::Palette(_)) => self.palette_key(key),
            Some(Overlay::Finder(_)) => self.finder_key(key),
            Some(Overlay::Prompt(_)) => self.prompt_key(key),
            Some(Overlay::Menu(_)) => self.menu_key(key),
            Some(Overlay::RepoConfig(_)) => self.repo_config_key(key),
            Some(Overlay::Ask(_)) => self.ask_key(key),
            Some(Overlay::Extensions(_)) => self.extensions_key(key),
            None => {}
        }
    }

    /// Say why up front rather than opening an overlay whose every action
    /// would fail — same posture as `run_semantic_search`'s `qmd_off_reason`
    /// check.
    fn open_extensions(&mut self) {
        if let Some(reason) = self.cfg.apm_off_reason {
            self.toast(Level::Warn, reason);
            return;
        }
        let items = crate::extensions::load(&self.cfg.root);
        self.overlay = Some(Overlay::Extensions(Extensions { selected: 0, items, log: None, confirm: None }));
    }

    fn extensions_key(&mut self, key: KeyEvent) {
        let Some(Overlay::Extensions(state)) = self.overlay.as_mut() else { return };
        if let Some(action) = state.confirm {
            match key.code {
                KeyCode::Char('y') | KeyCode::Enter => {
                    state.confirm = None;
                    self.run_extension_action(action);
                }
                _ => state.confirm = None,
            }
            return;
        }
        let n = state.items.len();
        match key.code {
            KeyCode::Esc | KeyCode::Char('q') => self.overlay = None,
            KeyCode::Down | KeyCode::Char('j') if n > 0 => state.selected = (state.selected + 1).min(n - 1),
            KeyCode::Up | KeyCode::Char('k') => state.selected = state.selected.saturating_sub(1),
            KeyCode::Char('i') if n > 0 => self.run_extension_action(ExtensionAction::Install),
            KeyCode::Char('u') if n > 0 => self.run_extension_action(ExtensionAction::Update),
            KeyCode::Char('d') | KeyCode::Char('x') if n > 0 => state.confirm = Some(ExtensionAction::Uninstall),
            _ => {}
        }
    }

    /// Every action shells out to the real `apm` binary — this never touches
    /// `apm.yml`/`apm.lock.yaml` itself. Runs as a native job (`extensions::run`
    /// on its own thread) so a slow install never blocks the UI, mirroring
    /// `run_semantic_search`.
    fn run_extension_action(&mut self, action: ExtensionAction) {
        let Some(Overlay::Extensions(state)) = self.overlay.as_ref() else { return };
        let Some(item) = state.items.get(state.selected) else { return };
        let key = item.key.clone();
        let root = self.cfg.root.clone();
        let tx = self.tx.clone();
        let args: Vec<String> = match action {
            ExtensionAction::Install => vec!["install".into(), key],
            ExtensionAction::Update => vec!["update".into(), key, "-y".into()],
            ExtensionAction::Uninstall => vec!["uninstall".into(), key],
        };
        let mut job_args = vec!["apm".to_string()];
        job_args.extend(args.clone());
        self.jobs.spawn_native(
            format!("apm {}", action.verb()),
            job_args,
            move || {
                let refs: Vec<&str> = args.iter().map(String::as_str).collect();
                let (code, stdout, stderr) = crate::extensions::run(&root, &refs);
                NativeOutcome { code, stdout, stderr }
            },
            tx,
        );
    }

    fn open_projects(&mut self) {
        let reg = crate::project::ProjectRegistry::load();
        let mut items = Vec::new();
        let mut sorted_names: Vec<&String> = reg.projects.keys().collect();
        sorted_names.sort();
        for name in sorted_names {
            let entry = &reg.projects[name];
            items.push((name.clone(), entry.path.clone()));
        }
        let selected = items.iter().position(|(n, _)| *n == reg.active).unwrap_or(0);
        self.overlay = Some(Overlay::Projects { selected, items });
    }

    fn project_key(&mut self, key: KeyEvent) {
        // Creating a project is valid even with an empty registry, so handle it
        // before the "no items" bail-out below closes the overlay outright.
        if key.code == KeyCode::Char('n') {
            self.overlay = None;
            self.prompt_new_project();
            return;
        }
        let Some(Overlay::Projects { selected, items }) = self.overlay.as_mut() else { return };
        let n = items.len();
        if n == 0 {
            if matches!(key.code, KeyCode::Esc | KeyCode::Char('q')) {
                self.overlay = None;
            }
            return;
        }
        match key.code {
            KeyCode::Esc | KeyCode::Char('q') => {
                self.overlay = None;
            }
            KeyCode::Down | KeyCode::Char('j') | KeyCode::Tab => {
                *selected = (*selected + 1) % n;
            }
            KeyCode::Up | KeyCode::Char('k') | KeyCode::BackTab => {
                *selected = (*selected + n - 1) % n;
            }
            KeyCode::Enter => {
                let (name, path) = items[*selected].clone();
                self.overlay = None;
                self.switch_project(&name, &path);
            }
            KeyCode::Char('r') => {
                let (name, path) = items[*selected].clone();
                self.overlay = None;
                self.prompt_rename_project(&name, &path);
            }
            KeyCode::Char('d') => {
                let (name, path) = items[*selected].clone();
                self.overlay = None;
                self.prompt_delete_project(&name, &path);
            }
            _ => {}
        }
    }

    fn prompt_new_project(&mut self) {
        self.overlay = Some(Overlay::Prompt(Prompt {
            kind: PromptKind::NewProject,
            title: "new project name".to_string(),
            value: String::new(),
            path: None,
        }));
    }

    fn prompt_rename_project(&mut self, name: &str, path: &Path) {
        self.overlay = Some(Overlay::Prompt(Prompt {
            kind: PromptKind::RenameProject,
            title: format!("rename project '{name}'"),
            value: name.to_string(),
            path: Some(path.to_path_buf()),
        }));
    }

    fn prompt_delete_project(&mut self, name: &str, path: &Path) {
        let is_in_projects_dir = path.starts_with(crate::project::projects_dir());
        let title = if is_in_projects_dir {
            format!("type '{name}' to delete it (files will be removed from disk)")
        } else {
            format!("type '{name}' to unregister it (external files kept on disk)")
        };
        self.overlay = Some(Overlay::Prompt(Prompt {
            kind: PromptKind::DeleteProject,
            title,
            value: String::new(),
            path: Some(path.to_path_buf()),
        }));
    }

    /// Create a project in the default XDG projects directory, register it, and switch to it.
    fn create_new_project(&mut self, raw_name: &str) {
        let name = raw_name.trim().replace(' ', "_");
        if name.is_empty() {
            return;
        }
        let reg = crate::project::ProjectRegistry::load();
        if reg.projects.contains_key(&name) {
            return self.toast(Level::Bad, format!("project '{name}' already exists"));
        }
        match crate::project::create_project(&name, None, "", "", "", "", "local") {
            Ok(proj) => {
                let _ = herdr::space::create_workspace_if_server_running(&proj.name, &proj.root);
                self.switch_project(&proj.name, &proj.root);
                self.toast(Level::Good, format!("created project '{name}'"));
            }
            Err(err) => self.toast(Level::Bad, format!("could not create project: {err}")),
        }
    }

    /// Rename a registered project's key, leaving its files untouched.
    fn rename_project(&mut self, path: Option<&Path>, value: &str) {
        let Some(path) = path else { return };
        let mut reg = crate::project::ProjectRegistry::load();
        let Some(old_name) = reg.projects.iter().find(|(_, e)| e.path == path).map(|(n, _)| n.clone()) else {
            return self.toast(Level::Bad, "project no longer registered — reload");
        };
        let new_name = value.trim().replace(' ', "_");
        if new_name.is_empty() || new_name == old_name {
            return;
        }
        if reg.projects.contains_key(&new_name) {
            return self.toast(Level::Bad, format!("project '{new_name}' already exists"));
        }
        let entry = reg.projects.remove(&old_name).unwrap();
        let was_active = reg.active == old_name;
        reg.projects.insert(new_name.clone(), entry);
        if was_active {
            reg.active = new_name.clone();
        }
        if let Err(err) = reg.save() {
            return self.toast(Level::Bad, format!("could not save registry: {err}"));
        }
        if was_active {
            self.project_name = new_name.clone();
        }
        if let Ok(workspaces) = herdr::space::list_workspaces() {
            if let Some(ws) = workspaces.iter().find(|w| w.label == old_name) {
                let _ = herdr::space::rename_workspace(&ws.id, &new_name);
            }
        }
        self.toast(Level::Good, format!("renamed project '{old_name}' → '{new_name}'"));
    }

    /// Unregister or delete a project after the user types its name back to confirm.
    /// Projects located in the default XDG projects directory are deleted from disk
    /// so they are not immediately re-discovered by registry scanning.
    fn delete_project(&mut self, path: Option<&Path>, typed: &str) {
        let Some(path) = path else { return };
        let mut reg = crate::project::ProjectRegistry::load();
        let Some(name) = reg.projects.iter().find(|(_, e)| e.path == path).map(|(n, _)| n.clone()) else {
            return self.toast(Level::Bad, "project no longer registered — reload");
        };
        if typed.trim() != name {
            return self.toast(Level::Warn, "name did not match — deletion cancelled");
        }
        reg.projects.remove(&name);
        let was_active = reg.active == name;
        if was_active {
            reg.active = reg.projects.keys().next().cloned().unwrap_or_else(|| "default".to_string());
        }

        if let Ok(workspaces) = herdr::space::list_workspaces() {
            if let Some(ws) = workspaces.iter().find(|w| w.label == name) {
                let _ = herdr::space::close_workspace(&ws.id);
            }
        }

        let is_in_projects_dir = path.starts_with(crate::project::projects_dir());
        if is_in_projects_dir && path.is_dir() {
            let _ = std::fs::remove_dir_all(path);
            self.toast(Level::Good, format!("deleted project '{name}'"));
        } else {
            self.toast(Level::Good, format!("unregistered project '{name}' (files kept at {})", path.display()));
        }

        if let Err(err) = reg.save() {
            return self.toast(Level::Bad, format!("could not save registry: {err}"));
        }

        if was_active {
            if let Some(entry) = reg.projects.get(&reg.active) {
                let new_path = entry.path.clone();
                let new_name = reg.active.clone();
                self.switch_project(&new_name, &new_path);
            }
        }
    }

    pub fn switch_project(&mut self, name: &str, path: &Path) {
        if !path.is_dir() {
            self.toast(Level::Warn, format!("Project directory does not exist: {}", path.display()));
            return;
        }

        let mut reg = crate::project::ProjectRegistry::load();
        reg.active = name.to_string();
        let _ = reg.save();

        self.cfg = Config::load(path);
        self.project_name = name.to_string();
        let collections: Vec<PathBuf> = self.cfg.collections().into_iter().map(|(_, p)| p).collect();
        self.tree = Tree::new(path, collections);
        self.index = Index::build(path, &self.collection_dirs());
        self.components = platform::discover_components(path);
        self.open = None;
        self.doc_stop = DocStop::Content;

        if let Some(pane) = self.sidebar.as_mut() {
            pane.switch_project(name, path);
        } else {
            let _ = herdr::space::ensure_project_space(name, path);
        }

        self.toast(Level::Good, format!("Switched to project '{name}'"));
    }

    fn theme_key(&mut self, key: KeyEvent) {
        let Some(Overlay::Themes { selected, original }) = self.overlay.as_mut() else { return };
        let original = *original;
        let n = Flavor::ALL.len();
        match key.code {
            KeyCode::Esc => {
                self.overlay = None;
                self.apply_flavor(original);
            }
            KeyCode::Enter => {
                self.overlay = None;
                let _ = self.cfg.save_tui();
                let name = self.cfg.flavor.display_name();
                self.toast(Level::Good, format!("theme: {name}"));
            }
            KeyCode::Down | KeyCode::Char('j') | KeyCode::Tab => {
                *selected = (*selected + 1) % n;
                let flavor = Flavor::ALL[*selected];
                self.apply_flavor(flavor);
            }
            KeyCode::Up | KeyCode::Char('k') | KeyCode::BackTab => {
                *selected = (*selected + n - 1) % n;
                let flavor = Flavor::ALL[*selected];
                self.apply_flavor(flavor);
            }
            _ => {}
        }
    }

    fn palette_key(&mut self, key: KeyEvent) {
        let Some(Overlay::Palette(palette)) = self.overlay.as_mut() else { return };
        match key.code {
            KeyCode::Esc => self.overlay = None,
            KeyCode::Down | KeyCode::Tab => {
                palette.selected = (palette.selected + 1).min(palette.items.len().saturating_sub(1))
            }
            KeyCode::Up | KeyCode::BackTab => palette.selected = palette.selected.saturating_sub(1),
            KeyCode::Enter => {
                let cmd = palette.items.get(palette.selected).map(|(c, _, _)| *c);
                self.overlay = None;
                if let Some(cmd) = cmd {
                    self.run(cmd);
                }
            }
            KeyCode::Backspace => {
                palette.query.pop();
                palette.items = palette_items(&palette.query);
                palette.selected = 0;
            }
            KeyCode::Char(c) => {
                palette.query.push(c);
                palette.items = palette_items(&palette.query);
                palette.selected = 0;
            }
            _ => {}
        }
    }

    fn finder_key(&mut self, key: KeyEvent) {
        let Some(Overlay::Finder(finder)) = self.overlay.as_mut() else { return };
        let mut requery = false;
        let mut run_semantic = false;
        match key.code {
            KeyCode::Esc => {
                self.overlay = None;
                return;
            }
            KeyCode::Down | KeyCode::Tab => {
                finder.selected = (finder.selected + 1).min(finder.hits.len().saturating_sub(1))
            }
            KeyCode::Up | KeyCode::BackTab => finder.selected = finder.selected.saturating_sub(1),
            KeyCode::Enter => {
                if finder.mode == search::Mode::Semantic && finder.hits.is_empty() {
                    run_semantic = true;
                } else {
                    let hit = finder.hits.get(finder.selected).cloned();
                    self.overlay = None;
                    if let Some(hit) = hit {
                        self.open_path(&hit.path.clone(), true);
                        self.tree.reveal(&hit.path);
                        if let (Some(line), Some(open)) = (hit.line, self.open.as_mut()) {
                            let body_line = line.saturating_sub(open.page.body_start);
                            open.scroll = open.doc.line_for_source(body_line);
                        }
                    }
                    return;
                }
            }
            KeyCode::Char('s') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                finder.mode = finder.mode.next();
                finder.hits.clear();
                finder.warning = None;
                requery = finder.mode.is_live();
            }
            KeyCode::Char('l') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                finder.collection = match finder.collection {
                    None => Some("wiki"),
                    Some("wiki") => Some("workspace"),
                    Some("workspace") => Some("sources"),
                    _ => None,
                };
                requery = finder.mode.is_live();
            }
            KeyCode::Backspace => {
                finder.query.pop();
                requery = finder.mode.is_live();
            }
            KeyCode::Char(c) => {
                finder.query.push(c);
                requery = finder.mode.is_live();
            }
            _ => {}
        }

        if run_semantic {
            return self.run_semantic_search();
        }
        if requery {
            self.requery_finder();
        }
    }

    fn requery_finder(&mut self) {
        let Some(Overlay::Finder(finder)) = self.overlay.as_ref() else { return };
        let (mode, query, collection) = (finder.mode, finder.query.clone(), finder.collection);
        let hits = match mode {
            search::Mode::Files => self.finder_engine.files(&self.index, &query, collection),
            search::Mode::Text => self.finder_engine.text(&self.index, &query, collection, false),
            search::Mode::Semantic => return,
        };
        if let Some(Overlay::Finder(finder)) = self.overlay.as_mut() {
            finder.hits = hits;
            finder.selected = 0;
        }
    }

    fn run_semantic_search(&mut self) {
        let Some(Overlay::Finder(finder)) = self.overlay.as_ref() else { return };
        if finder.query.trim().is_empty() {
            return;
        }
        if let Some(reason) = self.cfg.qmd_off_reason {
            if let Some(Overlay::Finder(finder)) = self.overlay.as_mut() {
                finder.warning = Some(reason.to_string());
            }
            return;
        }
        let collection = finder.collection.unwrap_or("all").to_string();
        let query = finder.query.clone();
        let root = self.cfg.root.clone();
        let tx = self.tx.clone();
        let id = self.jobs.spawn_native(
            "semantic search",
            vec!["wiki".into(), "search".into(), query.clone(), "--json".into(), "--collection".into(), collection.clone()],
            move || match search::semantic_search(&root, &query, &collection) {
                Ok(stdout) => NativeOutcome { code: 0, stdout, stderr: String::new() },
                Err(err) => NativeOutcome { code: 1, stdout: String::new(), stderr: err },
            },
            tx,
        );
        if let Some(Overlay::Finder(finder)) = self.overlay.as_mut() {
            finder.running = Some(id);
            finder.warning = None;
        }
    }

    fn menu_key(&mut self, key: KeyEvent) {
        let Some(Overlay::Menu(menu)) = self.overlay.as_ref() else { return };
        let last = menu.items.len().saturating_sub(1);
        match key.code {
            KeyCode::Esc | KeyCode::Char('q') => self.overlay = None,
            KeyCode::Down | KeyCode::Tab | KeyCode::Char('j') => {
                if let Some(Overlay::Menu(menu)) = self.overlay.as_mut() {
                    menu.selected = (menu.selected + 1).min(last);
                }
            }
            KeyCode::Up | KeyCode::BackTab | KeyCode::Char('k') => {
                if let Some(Overlay::Menu(menu)) = self.overlay.as_mut() {
                    menu.selected = menu.selected.saturating_sub(1);
                }
            }
            KeyCode::Enter | KeyCode::Char(' ') => self.activate_menu(),
            _ => {}
        }
    }

    fn activate_menu(&mut self) {
        let (action, path, area) = match self.overlay.as_ref() {
            Some(Overlay::Menu(menu)) => (
                menu.items.get(menu.selected).map(|(a, _)| *a),
                menu.path.clone(),
                menu.area,
            ),
            _ => return,
        };
        let rel = crate::vault::page::rel_path(&path, &self.cfg.root);
        self.overlay = None;
        match action {
            Some(MenuAction::Open) => self.open_path(&path, true),
            Some(MenuAction::NewPage) => self.prompt_new_page(&path),
            Some(MenuAction::Rename) => self.prompt_rename(&path),
            Some(MenuAction::Delete) => self.confirm_delete(path, area),
            Some(MenuAction::ConfirmDelete) => self.delete_path(&path),
            Some(MenuAction::CopyRelative) => {
                crate::clipboard::copy(&rel);
                self.toast(Level::Good, format!("copied {rel}"));
            }
            Some(MenuAction::CopyAbsolute) => {
                let abs = std::fs::canonicalize(&path)
                    .unwrap_or(path)
                    .to_string_lossy()
                    .to_string();
                crate::clipboard::copy(&abs);
                self.toast(Level::Good, format!("copied {abs}"));
            }
            _ => {}
        }
    }

    /// Ask for the new basename. The tree label is a spaced rendering of the
    /// snake_case filename, so the prompt opens on the slug without extension.
    fn prompt_rename(&mut self, path: &Path) {
        if self.tree.collection_paths().iter().any(|c| c == path) {
            return self.toast(Level::Bad, "a collection root cannot be renamed");
        }
        let rel = crate::vault::page::rel_path(path, &self.cfg.root);
        let mut name = path.file_name().unwrap_or_default().to_string_lossy().to_string();
        if path.is_file() {
            let ext = path.extension().map(|e| e.to_string_lossy().to_string()).unwrap_or_default();
            name = name.trim_end_matches(&ext).trim_end_matches('.').to_string();
        }
        self.overlay = Some(Overlay::Prompt(Prompt {
            kind: PromptKind::Rename,
            title: format!("rename {rel}"),
            value: name,
            path: Some(path.to_path_buf()),
        }));
    }

    /// Ask for the name of a page to create inside `dir`. The prompt is a
    /// basename joined onto the folder, so the menu can't create elsewhere.
    fn prompt_new_page(&mut self, dir: &Path) {
        let rel = crate::vault::page::rel_path(dir, &self.cfg.root);
        self.overlay = Some(Overlay::Prompt(Prompt {
            kind: PromptKind::NewPage,
            title: format!("new page in {rel}"),
            value: String::new(),
            path: Some(dir.to_path_buf()),
        }));
    }

    fn rename_path(&mut self, path: &Path, value: &str) {
        let Some(parent) = path.parent() else { return self.toast(Level::Bad, "cannot rename the checkout root") };
        if !path.exists() {
            return self.toast(Level::Bad, "no longer exists — reload");
        }
        let mut name = value.trim().replace(' ', "_");
        if name.is_empty() {
            return;
        }
        if path.is_file() {
            // Keep the file's own extension unless the new name already
            // carries it — so a `.png` stays a `.png` without this having to
            // know the set of extensions the tree lists.
            let ext = path.extension().map(|e| e.to_string_lossy().to_string()).unwrap_or_default();
            if !ext.is_empty() && !name.to_ascii_lowercase().ends_with(&format!(".{}", ext.to_ascii_lowercase())) {
                name = format!("{name}.{ext}");
            }
        }
        let target = parent.join(&name);
        if target == path {
            return self.toast(Level::Info, "name unchanged");
        }
        if target.exists() {
            return self.toast(Level::Bad, "a page with that name already exists");
        }
        let was_open = self.open.as_ref().map(|o| o.page.path == *path).unwrap_or(false);
        match std::fs::rename(path, &target) {
            Ok(()) => {
                let rel = crate::vault::page::rel_path(path, &self.cfg.root);
                let rel2 = crate::vault::page::rel_path(&target, &self.cfg.root);
                self.open = None;
                self.reload_everything();
                if was_open {
                    self.open_path(&target, false);
                }
                self.toast(Level::Good, format!("renamed {rel} → {rel2}"));
            }
            Err(err) => self.toast(Level::Bad, format!("could not rename: {err}")),
        }
    }

    /// Deleting is destructive and never confirmed anywhere else in the app, so
    /// the menu first narrows to a confirm overlay carrying the same path.
    fn confirm_delete(&mut self, path: PathBuf, area: Rect) {
        self.overlay = Some(Overlay::Menu(Menu {
            path,
            items: vec![(MenuAction::Cancel, "cancel"), (MenuAction::ConfirmDelete, "delete")],
            selected: 0,
            area,
            pressed: None,
        }));
    }

    fn delete_path(&mut self, path: &Path) {
        if self.tree.collection_paths().iter().any(|c| c == path) {
            return self.toast(Level::Bad, "a collection root cannot be deleted");
        }
        let rel = crate::vault::page::rel_path(path, &self.cfg.root);
        let editing_target = self.open.as_ref().is_some_and(|open| {
            let under = if path.is_dir() {
                open.page.path.starts_with(path)
            } else {
                open.page.path == *path
            };
            under && open.editing()
        });
        if editing_target {
            return self.toast(Level::Warn, "save or leave the editor first");
        }
        let result = if path.is_dir() {
            std::fs::remove_dir_all(path)
        } else {
            std::fs::remove_file(path)
        };
        match result {
            Ok(()) => {
                let open_under = self.open.as_ref().is_some_and(|open| {
                    if path.is_dir() {
                        open.page.path.starts_with(path)
                    } else {
                        open.page.path == *path
                    }
                });
                if open_under {
                    self.open = None;
                    self.doc_stop = DocStop::Content;
                }
                self.reload_everything();
                self.toast(Level::Good, format!("deleted {rel}"));
            }
            Err(err) => self.toast(Level::Bad, format!("could not delete {rel}: {err}")),
        }
    }

    fn repo_config_key(&mut self, key: KeyEvent) {
        let Some(Overlay::RepoConfig(state)) = self.overlay.as_mut() else { return };
        let last = state.option_count().saturating_sub(1);
        match key.code {
            KeyCode::Esc => self.overlay = None,
            KeyCode::Down | KeyCode::Tab => state.selected = (state.selected + 1).min(last),
            KeyCode::Up | KeyCode::BackTab => state.selected = state.selected.saturating_sub(1),
            KeyCode::Backspace if state.selected == 0 => {
                state.url.pop();
            }
            KeyCode::Char(c) if state.selected == 0 && !key.modifiers.contains(KeyModifiers::CONTROL) => {
                state.url.push(c);
            }
            KeyCode::Enter => {
                let name = state.name.clone();
                let selected = state.selected;
                let url = state.url.trim().to_string();
                self.overlay = None;
                self.apply_repo_config(&name, selected, &url);
            }
            _ => {}
        }
    }

    /// Set `repositories.<name>` and, for a git URL or `local`, make sure the
    /// checkout has a repo to match. Local, fast operations — no network call
    /// happens here (a fresh remote is only pulled by `SyncRepos`), so this
    /// runs synchronously rather than through a native job.
    fn apply_repo_config(&mut self, name: &str, selected: usize, url: &str) {
        let new_url = match selected {
            1 => "local".to_string(),
            2 => "gdrive".to_string(),
            _ if url.is_empty() => {
                self.toast(Level::Warn, "enter a git URL, or pick local");
                self.open_repo_config(name);
                return;
            }
            _ => url.to_string(),
        };

        self.cfg.repositories.insert(name.to_string(), new_url.clone());
        if let Err(err) = self.cfg.save_repositories() {
            self.toast(Level::Bad, format!("config {name} failed: {err}"));
            return;
        }

        if new_url != "gdrive" {
            let repo_dir = self.cfg.root.join(name);
            if repo_dir.join(".git").is_dir() {
                crate::vault::git::set_remote(&repo_dir, &new_url);
            } else {
                crate::vault::git::ensure_local_repo(&self.cfg.root, name);
                if new_url != "local" {
                    crate::vault::git::set_remote(&self.cfg.root.join(name), &new_url);
                }
            }
        }

        self.toast(Level::Good, format!("{name}: configured"));
        self.refresh_git();
        self.reload_everything();
    }

    fn prompt_key(&mut self, key: KeyEvent) {
        let Some(Overlay::Prompt(prompt)) = self.overlay.as_mut() else { return };
        match key.code {
            KeyCode::Esc => self.overlay = None,
            KeyCode::Backspace => {
                prompt.value.pop();
            }
            KeyCode::Char(c) => prompt.value.push(c),
            KeyCode::Enter => {
                let path = prompt.path.clone();
                let kind = prompt.kind;
                let value = prompt.value.trim().to_string();
                self.overlay = None;
                if value.is_empty() {
                    return;
                }
                match kind {
                    PromptKind::Commit => self.run_commit(value),
                    PromptKind::NewPage => self.create_page(&value, path.as_deref()),
                    PromptKind::Rename => {
                        if let Some(path) = path {
                            self.rename_path(&path, &value);
                        }
                    }
                    PromptKind::NewProject => self.create_new_project(&value),
                    PromptKind::RenameProject => self.rename_project(path.as_deref(), &value),
                    PromptKind::DeleteProject => self.delete_project(path.as_deref(), &value),
                }
            }
            _ => {}
        }
    }

    fn create_page(&mut self, rel: &str, base: Option<&Path>) {
        let rel = if rel.ends_with(".md") { rel.to_string() } else { format!("{rel}.md") };
        let root = &self.cfg.root;
        let base = base.unwrap_or(root);
        let path = crate::vault::links::normalize(&base.join(&rel));
        if !path.starts_with(root) {
            return self.toast(Level::Bad, "path escapes the checkout");
        }
        if path.exists() {
            self.open_path(&path, true);
            return self.toast(Level::Info, "page already exists");
        }
        let title = path
            .file_stem()
            .map(|s| s.to_string_lossy().replace('_', " "))
            .unwrap_or_default();
        let category = path
            .parent()
            .and_then(|p| p.strip_prefix(&self.cfg.root).ok())
            .map(|p| p.to_string_lossy().replace(std::path::MAIN_SEPARATOR, "/"))
            .unwrap_or_default();
        let category = category.split_once('/').map(|(_, rest)| rest).unwrap_or(&category);
        let scaffold = format!(
            "---\ntitle: {title}\ntype: concept\ncategory: {category}\nrationale: \nstatus: draft\nsources: []\n---\n# {title}\n\n"
        );
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        match std::fs::write(&path, scaffold) {
            Ok(()) => {
                self.index.refresh(&path);
                self.tree.rebuild();
                self.open_path(&path, true);
                self.tree.reveal(&path);
                self.run(Cmd::Edit);
                self.toast(Level::Good, format!("created {rel}"));
            }
            Err(err) => self.toast(Level::Bad, format!("could not create {rel}: {err}")),
        }
    }

    // ------------------------------------------------------------------ jobs

    /// The whole vault is already scanned and kept fresh by the file watcher
    /// (`self.index`), so this needs no subprocess: assemble the same payload
    /// `podarcis lint --json` used to produce and report it immediately.
    fn run_lint(&mut self) {
        let payload = crate::vault::lint::to_json_payload(&self.index);
        let ok = payload.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
        let result = JobResult {
            id: 0,
            label: "lint".into(),
            args: vec!["lint".into(), "--json".into()],
            code: if ok { 0 } else { 1 },
            stdout: payload.to_string(),
            stderr: String::new(),
        };
        self.on_job_done(result);
    }

    fn run_sync_repos(&mut self) {
        let root = self.cfg.root.clone();
        let repos = self.cfg.repositories.clone();
        let tx = self.tx.clone();
        self.jobs.spawn_native(
            "sync",
            vec!["repo".into(), "sync".into()],
            move || {
                let results = crate::vault::git::sync_repos(&root, &repos);
                let ok = results.iter().all(|r| r.status == "ok");
                let stdout = serde_json::to_string(&results).unwrap_or_default();
                NativeOutcome { code: if ok { 0 } else { 1 }, stdout, stderr: String::new() }
            },
            tx,
        );
    }

    /// Lint-gate on the in-memory index, then `git add -A` + commit every
    /// dirty configured repo. Mirrors `audit.py::audit_and_commit`, run
    /// through `podarcis repo commit -m` until now.
    fn run_commit(&mut self, message: String) {
        let lint_ok = !self.index.entries.iter().any(|e| e.worst() == crate::vault::lint::Severity::Error);
        let root = self.cfg.root.clone();
        let repos = self.cfg.repositories.clone();
        let tx = self.tx.clone();
        self.jobs.spawn_native(
            "commit",
            vec!["repo".into(), "commit".into(), "-m".into(), message.clone()],
            move || {
                let outcome = crate::vault::git::commit_dirty(&root, &repos, lint_ok, &message);
                if outcome.ok {
                    NativeOutcome { code: 0, stdout: outcome.message, stderr: String::new() }
                } else {
                    NativeOutcome { code: 1, stdout: String::new(), stderr: outcome.message }
                }
            },
            tx,
        );
    }

    pub fn start_indexing(&mut self) {
        self.indexing = true;
        let root = self.cfg.root.clone();
        let dirs = self.collection_dirs();
        let tx = self.tx.clone();
        std::thread::spawn(move || {
            let index = Index::build(&root, &dirs);
            let _ = tx.send(AppEvent::IndexReady(Box::new(index)));
        });
    }

    pub fn on_job_done(&mut self, result: JobResult) {
        self.jobs.finish(result.id);

        let is_semantic = matches!(
            self.overlay.as_ref(),
            Some(Overlay::Finder(f)) if f.running == Some(result.id)
        );
        if is_semantic {
            let payload = result.json();
            let hits = payload
                .as_ref()
                .map(|v| search::parse_semantic(v, &self.cfg.root))
                .unwrap_or_default();
            let warning = payload.as_ref().and_then(search::semantic_warning);
            if let Some(Overlay::Finder(finder)) = self.overlay.as_mut() {
                finder.running = None;
                finder.selected = 0;
                finder.warning = warning.or_else(|| {
                    (!result.ok()).then(|| first_line(&result.stderr).unwrap_or_else(|| "search failed".into()))
                });
                finder.hits = hits;
            }
            return;
        }

        if result.args.first().map(String::as_str) == Some("lint") {
            return self.report_lint(&result);
        }

        if result.args.first().map(String::as_str) == Some("apm") {
            return self.report_extension_action(&result);
        }

        let detail = first_line(&result.stderr)
            .or_else(|| result.stdout.lines().rev().find(|l| !l.trim().is_empty()).map(str::to_string))
            .unwrap_or_default();
        if result.ok() {
            self.toast(Level::Good, format!("{} ok{}", result.label, suffix(&detail)));
            self.reload_everything();
        } else {
            self.toast(Level::Bad, format!("{} failed{}", result.label, suffix(&detail)));
        }
    }

    /// Refresh the browse list from `apm.yml`/`apm.lock.yaml` and show the raw
    /// `apm` output — never parsed, since apm prints Rich tables, not JSON.
    fn report_extension_action(&mut self, result: &JobResult) {
        let items = crate::extensions::load(&self.cfg.root);
        let log = if result.stdout.trim().is_empty() { result.stderr.clone() } else { result.stdout.clone() };
        if let Some(Overlay::Extensions(state)) = self.overlay.as_mut() {
            state.items = items;
            state.selected = state.selected.min(state.items.len().saturating_sub(1));
            state.log = Some(log);
        }
        if result.ok() {
            self.toast(Level::Good, format!("{} ok", result.label));
        } else {
            let detail = first_line(&result.stderr).unwrap_or_default();
            self.toast(Level::Bad, format!("{} failed{}", result.label, suffix(&detail)));
        }
    }

    fn report_lint(&mut self, result: &JobResult) {
        let Some(payload) = result.json() else {
            self.toast(Level::Bad, "lint produced no JSON payload");
            return;
        };
        let files = payload.get("files").and_then(|f| f.as_object());
        let count: usize = files.map(|f| f.values().filter_map(|v| v.as_array()).map(Vec::len).sum()).unwrap_or(0);
        if count == 0 {
            self.toast(Level::Good, "lint: clean");
        } else {
            let n = files.map(|f| f.len()).unwrap_or(0);
            self.toast(Level::Warn, format!("lint: {count} findings across {n} files"));
        }
    }

    pub fn on_index_ready(&mut self, index: Index) {
        self.index = index;
        self.indexing = false;
        self.finding_cursor = None;
        self.components = platform::discover_components(&self.cfg.root);
    }

    pub fn on_fs_changed(&mut self, paths: Vec<PathBuf>) {
        let mut structural = false;
        for path in &paths {
            structural |= self.index.refresh(path);
        }
        if structural {
            self.tree.rebuild();
            self.components = platform::discover_components(&self.cfg.root);
        }
        self.refresh_git();
        let open_changed = self
            .open
            .as_ref()
            .map(|o| paths.contains(&o.page.path))
            .unwrap_or(false);
        if open_changed {
            self.reload_open();
        }
    }

    // --------------------------------------------------------------- sidebar

    /// Start or resize the agents pane to match the area it was drawn into.
    pub fn sync_sidebar(&mut self) {
        if !self.show_sidebar {
            self.sidebar = None;
            return;
        }
        let area = self.areas.sidebar;
        if area.width < 4 || area.height < 3 {
            return;
        }
        let (rows, cols) = (area.height.saturating_sub(2), area.width.saturating_sub(2));

        if let Some(pane) = self.sidebar.as_mut() {
            if pane.is_alive() {
                pane.resize(rows, cols);
                return;
            }
            self.sidebar = None;
            self.sidebar_error = Some("the agent session exited — ctrl+g twice to restart".into());
            if self.focus == Focus::Sidebar {
                self.focus = Focus::Doc;
            }
            return;
        }
        if self.sidebar_error.is_some() {
            return;
        }
        if !herdr::pty::available() {
            self.sidebar_error = Some("herdr is not installed — the agents pane needs it (herdr.dev)".into());
            return;
        }
        if let Err(err) = herdr::config::provision(self.cfg.flavor) {
            self.sidebar_error = Some(format!("agents pane config: {err}"));
            return;
        }
        match herdr::pty::Pane::spawn(&self.project_name, &self.cfg.root, rows, cols, self.tx.clone()) {
            Ok(pane) => self.sidebar = Some(pane),
            Err(err) => self.sidebar_error = Some(format!("herdr: {err}")),
        }
    }

}

fn suffix(detail: &str) -> String {
    if detail.trim().is_empty() {
        String::new()
    } else {
        format!(" — {}", detail.trim())
    }
}

fn first_line(text: &str) -> Option<String> {
    text.lines().map(str::trim).find(|l| !l.is_empty()).map(str::to_string)
}

/// Palette entries, filtered by a plain substring match over the title — a
/// command list is short enough that fuzzy ranking would only add surprise.
pub fn palette_items(query: &str) -> Vec<(Cmd, &'static str, String)> {
    let needle = query.trim().to_lowercase();
    let mut seen: Vec<Cmd> = Vec::new();
    let mut out = Vec::new();
    for binding in keymap::BINDINGS {
        if !binding.cmd.palette_visible() || seen.contains(&binding.cmd) {
            continue;
        }
        let hay = format!("{} {}", binding.group, binding.title).to_lowercase();
        if !needle.is_empty() && !hay.contains(&needle) {
            continue;
        }
        seen.push(binding.cmd);
        let keys = keymap::BINDINGS
            .iter()
            .filter(|b| b.cmd == binding.cmd)
            .flat_map(|b| b.keys.iter().copied())
            .collect::<Vec<_>>()
            .join("  ");
        out.push((binding.cmd, binding.title, keys));
    }
    out
}

/// Build a `Config` and `App` for tests without touching a real checkout.
#[cfg(test)]
pub fn test_app(root: &Path) -> (App, std::sync::mpsc::Receiver<AppEvent>) {
    let (tx, rx) = std::sync::mpsc::channel();
    let cfg = Config::load(root);
    let mut app = App::new(cfg, tx);
    app.areas = Areas {
        tree: Rect::new(0, 0, 30, 40),
        doc: Rect::new(30, 0, 60, 40),
        doc_main: Rect::new(30, 0, 60, 30),
        doc_body: Rect::new(30, 0, 60, 30),
        sidebar: Rect::new(90, 0, 40, 40),
        inspector: Rect::new(30, 30, 60, 10),
        inspector_body: Rect::default(),
        inspector_rows: Vec::new(),
        tree_panes: Vec::new(),
        collection_dividers: Vec::new(),
        tree_divider: Some(29),
        sidebar_divider: Some(90),
        inspector_divider: Some(30),
        tree_toggle: Some(Rect::new(29, 20, 1, 1)),
        sidebar_toggle: Some(Rect::new(90, 20, 1, 1)),
        inspector_toggle: Some(Rect::new(75, 30, 1, 1)),
        ..Default::default()
    };
    app.index = Index::build(root, &app.collection_dirs());
    app.indexing = false;
    (app, rx)
}

#[cfg(test)]
#[allow(clippy::items_after_test_module)]
mod tests {
    use super::*;

    struct Vault(PathBuf);

    impl Vault {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("podarcis-app-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(dir.join(".podarcis")).unwrap();
            std::fs::write(dir.join("AGENTS.md"), "# agents\n").unwrap();
            std::fs::write(dir.join(".podarcis/config.yaml"), "engines:\n  qmd: true\n").unwrap();
            for (rel, body) in [
                ("wiki/_index.md", "# Wiki\n"),
                (
                    "wiki/a.md",
                    "---\ntitle: Alpha\ntype: concept\ncategory: c\nrationale: r\nsources:\n  - id: s1\n    resource: ../sources/s1/metadata.md\n---\n# Alpha\n\nSee [Beta](b.md) and [gone](nope.md).\n\n## Section two\n\nCites[^s1].\n",
                ),
                ("wiki/b.md", "---\ntitle: Beta\ntype: concept\ncategory: c\nrationale: r\n---\n# Beta\n\nLeaf page.\n"),
                ("workspace/p.md", "---\ntitle: Protocol\ntype: protocol\ncategory: c\nrationale: r\n---\n# Protocol\n"),
            ] {
                let path = dir.join(rel);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(path, body).unwrap();
            }
            Self(dir)
        }
        fn app(&self) -> App {
            test_app(&self.0).0
        }
        fn path(&self, rel: &str) -> PathBuf {
            self.0.join(rel)
        }
        fn write(&self, rel: &str, content: &str) -> PathBuf {
            let path = self.0.join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, content).unwrap();
            path
        }
    }

    impl Drop for Vault {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn ch(c: char) -> KeyEvent {
        let mods = if c.is_uppercase() { KeyModifiers::SHIFT } else { KeyModifiers::NONE };
        KeyEvent::new(KeyCode::Char(c), mods)
    }

    fn ctrl(c: char) -> KeyEvent {
        KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
    }

    fn control(cmd: Control) -> (Request, std::sync::mpsc::Receiver<serde_json::Value>) {
        Request::for_test(cmd)
    }

    #[test]
    fn an_agent_can_open_a_page_at_a_line() {
        let v = Vault::new("control-open");
        let mut app = v.app();
        // File line 12 is body line 2: nine lines of frontmatter precede it.
        let (req, rx) = control(Control::Open { path: "wiki/a.md".into(), line: Some(12) });
        app.on_control(req);

        let open = app.open.as_ref().unwrap();
        assert_eq!(open.page.title(), "Alpha");
        assert_eq!(open.scroll, open.doc.line_for_source(2), "agents count file lines");
        assert_eq!(rx.try_recv().unwrap()["ok"], serde_json::json!(true));
    }

    #[test]
    fn an_agent_cannot_reach_outside_the_checkout() {
        let v = Vault::new("control-escape");
        let mut app = v.app();
        for path in ["../../etc/passwd", "/etc/passwd"] {
            let (req, rx) = control(Control::Open { path: path.into(), line: None });
            app.on_control(req);
            assert!(app.open.is_none(), "{path} was opened");
            assert_eq!(rx.try_recv().unwrap()["ok"], serde_json::json!(false));
        }
    }

    #[test]
    fn an_open_never_interrupts_the_editor_and_runs_once_it_closes() {
        let v = Vault::new("control-defer");
        let mut app = v.app();
        app.open_path(&v.path("wiki/b.md"), true);
        app.run(Cmd::Edit);

        let (req, rx) = control(Control::Open { path: "wiki/a.md".into(), line: None });
        app.on_control(req);
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Beta", "the editor kept the page");
        assert_eq!(rx.try_recv().unwrap()["deferred"], serde_json::json!(true));
        assert_eq!(app.deferred.len(), 1);

        app.run(Cmd::LeaveEdit);
        app.flush_control();
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Alpha");
        assert!(app.deferred.is_empty());
    }

    #[test]
    fn a_highlight_tints_the_lines_it_names_and_labels_the_first_one() {
        let v = Vault::new("control-mark");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        let (req, _rx) = control(Control::Highlight {
            path: Some("wiki/a.md".into()),
            ranges: vec![(12, 12)],
            label: Some("unsourced".into()),
            clear: false,
        });
        app.on_control(req);

        // File line 12 is body line 2: nine lines of frontmatter precede it.
        let marks = app.marks_for(&v.path("wiki/a.md"));
        assert_eq!(marks, [Mark { start: 2, end: 2, label: Some("unsourced".into()) }]);

        let open = app.open.as_ref().unwrap();
        let rendered = open
            .doc
            .to_lines(&app.theme, &markdown::Overlays { marks, ..Default::default() }, 0, open.doc.height())
            .iter()
            .map(|l| l.to_string())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(rendered.contains("◂ unsourced"), "{rendered}");

        app.run(Cmd::ClearMarks);
        assert!(app.marks_for(&v.path("wiki/a.md")).is_empty());
    }

    #[test]
    fn a_question_blocks_until_a_key_answers_it() {
        let v = Vault::new("control-ask");
        let mut app = v.app();
        let (req, rx) = control(Control::Ask {
            question: "Merge these two pages?".into(),
            options: vec!["yes".into(), "no".into()],
        });
        app.on_control(req);
        assert!(matches!(app.overlay, Some(Overlay::Ask(_))));
        assert!(rx.try_recv().is_err(), "nothing is answered before a keypress");

        app.on_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let reply = rx.try_recv().unwrap();
        assert_eq!(reply["answer"], serde_json::json!("no"));
        assert_eq!(reply["index"], serde_json::json!(1));
        assert!(app.overlay.is_none());
    }

    #[test]
    fn a_digit_answers_a_question_outright() {
        let v = Vault::new("control-ask-digit");
        let mut app = v.app();
        let (req, rx) = control(Control::Ask {
            question: "Which?".into(),
            options: vec!["alpha".into(), "beta".into(), "gamma".into()],
        });
        app.on_control(req);
        app.on_key(ch('3'));
        assert_eq!(rx.try_recv().unwrap()["answer"], serde_json::json!("gamma"));
    }

    #[test]
    fn a_dismissed_question_is_answered_as_a_dismissal_rather_than_left_hanging() {
        let v = Vault::new("control-ask-esc");
        let mut app = v.app();
        let (req, rx) = control(Control::Ask {
            question: "Merge?".into(),
            options: vec!["yes".into()],
        });
        app.on_control(req);
        app.on_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        let reply = rx.try_recv().unwrap();
        assert_eq!(reply["cancelled"], serde_json::json!(true));
        assert!(reply["answer"].is_null());
    }

    #[test]
    fn opening_a_page_renders_it_and_moves_focus() {
        let v = Vault::new("open");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        let open = app.open.as_ref().unwrap();
        assert_eq!(open.page.title(), "Alpha");
        assert!(open.doc.height() > 0);
        assert_eq!(app.focus, Focus::Doc);
    }

    #[test]
    fn a_csv_opens_as_a_table_and_edits_as_text() {
        let v = Vault::new("csv");
        v.write("workspace/finance/quotes.csv", "symbol,price\nAAPL,232.1\nMSFT,417.4\n");
        let mut app = v.app();
        app.open_path(&v.path("workspace/finance/quotes.csv"), true);
        let open = app.open.as_ref().unwrap();
        assert!(open.csv(), "a csv is a csv open, not a markdown one");
        let text = open.doc.lines.iter().map(|l| l.plain_text()).collect::<Vec<_>>().join("\n");
        assert!(text.contains("symbol") && text.contains("AAPL") && text.contains("MSFT"), "{text}");

        // The editor opens on the same raw file line the reader was showing.
        let msft = open.doc.lines.iter().position(|l| l.plain_text().contains("MSFT")).unwrap();
        app.open.as_mut().unwrap().scroll = msft;
        app.run(Cmd::Edit);
        let editor = app.open.as_ref().unwrap().editor.as_ref().unwrap();
        assert_eq!(editor.cursor_line(), 2, "editor lands on the MSFT row");
    }

    #[test]
    fn a_wide_csv_pans_to_its_last_column_instead_of_clipping_it() {
        let v = Vault::new("csv-wide");
        let cols: Vec<String> = (0..14).map(|i| format!("col_{i}")).collect();
        let vals: Vec<String> = (0..14).map(|i| format!("v{i:02}")).collect();
        v.write("workspace/finance/wide.csv", &format!("{}\n{}\n", cols.join(","), vals.join(",")));
        let mut app = v.app();
        app.open_path(&v.path("workspace/finance/wide.csv"), true);

        let view = app.viewport_width();
        let visible = |app: &App| -> String {
            let open = app.open.as_ref().unwrap();
            let over = markdown::Overlays { hscroll: open.hscroll, ..Default::default() };
            open.doc
                .to_lines(&app.theme, &over, 0, open.doc.height())
                .iter()
                .map(|l| {
                    l.spans
                        .iter()
                        .map(|s| s.content.as_ref())
                        .collect::<String>()
                        .chars()
                        .take(view)
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
                .join("\n")
        };

        // The box is wider than the pane — that is the bug: the tail columns
        // were drawn past the right edge and clipped away with no way back.
        assert!(app.open.as_ref().unwrap().doc.width > view, "the table overflows the pane");
        assert!(!visible(&app).contains("col_13"), "the last column starts off screen");

        // Panning right reaches it, and never scrolls past the right edge.
        for _ in 0..40 {
            app.run(Cmd::PanRight);
        }
        let open = app.open.as_ref().unwrap();
        assert_eq!(open.hscroll, open.doc.width - view, "the pan stops at the right edge");
        let shown = visible(&app);
        assert!(shown.contains("col_13") && shown.contains("v13"), "{shown}");

        // And back again, clamped at zero rather than going negative.
        for _ in 0..40 {
            app.run(Cmd::PanLeft);
        }
        assert_eq!(app.open.as_ref().unwrap().hscroll, 0);
        assert!(visible(&app).contains("col_0"));
    }

    #[test]
    fn prose_never_pans_and_a_reflow_drops_the_pan() {
        let v = Vault::new("csv-pan-reset");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::PanRight);
        assert_eq!(
            app.open.as_ref().unwrap().hscroll,
            0,
            "prose is wrapped to the measure, so there is nothing to pan to"
        );

        let cols: Vec<String> = (0..14).map(|i| format!("col_{i}")).collect();
        v.write("workspace/finance/wide.csv", &format!("{}\n", cols.join(",")));
        app.open_path(&v.path("workspace/finance/wide.csv"), true);
        app.run(Cmd::PanRight);
        assert!(app.open.as_ref().unwrap().hscroll > 0);
        // A narrower pane re-lays the table out, so the old offset addresses
        // columns that no longer sit there.
        app.areas.doc.width = 40;
        app.areas.doc_body.width = 36;
        app.reflow();
        assert_eq!(app.open.as_ref().unwrap().hscroll, 0, "a reflow starts from the left edge");
    }

    #[test]
    fn a_csv_is_laid_out_across_the_whole_pane_not_the_reading_measure() {
        let v = Vault::new("csv-measure");
        v.write("workspace/finance/quotes.csv", "symbol,price\nAAPL,232.1\n");
        let mut app = v.app();
        app.areas.doc.width = crate::ui::markdown::MAX_WIDTH + 40;
        app.open_path(&v.path("workspace/finance/quotes.csv"), true);
        assert_eq!(app.doc_width(), crate::ui::markdown::MAX_WIDTH + 36);
        app.open_path(&v.path("wiki/a.md"), true);
        assert_eq!(app.doc_width(), crate::ui::markdown::MAX_WIDTH, "prose keeps the measure");
    }

    #[test]
    fn opening_a_pdf_hands_off_to_the_system_viewer_instead_of_rendering_it() {
        let v = Vault::new("pdf");
        v.write("sources/literature/smith2024/original.pdf", "%PDF-1.4");
        let mut app = v.app();
        app.open_path(&v.path("sources/literature/smith2024/original.pdf"), true);
        assert!(app.open.is_none(), "a pdf is never opened in the reader pane");
        assert_eq!(app.toasts.len(), 1);
    }

    #[test]
    fn opening_an_image_hands_off_to_the_system_viewer_too() {
        let v = Vault::new("image");
        v.write("wiki/biology/figure.png", "\u{89}PNG");
        v.write("wiki/biology/photo.JPEG", "jpegdata");
        let mut app = v.app();
        for rel in ["wiki/biology/figure.png", "wiki/biology/photo.JPEG"] {
            app.open_path(&v.path(rel), true);
            assert!(app.open.is_none(), "{rel} is never opened in the reader pane");
        }
    }

    #[test]
    fn renaming_keeps_a_non_markdown_extension() {
        let v = Vault::new("rename-ext");
        v.write("wiki/biology/figure.png", "x");
        let mut app = v.app();
        app.rename_path(&v.path("wiki/biology/figure.png"), "diagram");
        assert!(v.path("wiki/biology/diagram.png").is_file(), "the .png survives a rename");
    }

    #[test]
    fn opening_a_directory_opens_its_index_page() {
        let v = Vault::new("dir");
        let mut app = v.app();
        app.open_path(&v.path("wiki"), true);
        assert_eq!(app.open.as_ref().unwrap().page.path, v.path("wiki/_index.md"));
    }

    #[test]
    fn selecting_the_wiki_folder_in_the_tree_opens_its_index_page() {
        let v = Vault::new("tree-dir");
        let mut app = v.app();
        app.tree.move_to(0); // the "wiki" collection row
        app.run(Cmd::TreeExpand);
        assert_eq!(app.open.as_ref().unwrap().page.path, v.path("wiki/_index.md"));
    }

    #[test]
    fn following_a_link_navigates_and_history_goes_back() {
        let v = Vault::new("follow");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::NextLink);
        app.run(Cmd::FollowLink);
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Beta");

        app.run(Cmd::Back);
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Alpha");
        app.run(Cmd::Forward);
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Beta");
    }

    #[test]
    fn clicking_a_link_navigates_to_page() {
        let v = Vault::new("click-link");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        // In wiki/a.md:
        // Line 0: the doc's leading blank row
        // Line 1: "# Alpha"
        // Line 2: the rule the reader draws under an h1
        // Line 3: ""
        // Line 4: "See Beta and gone."
        // doc_body starts at x=30, y=0. "Beta" link is on line 4, around col 4..8.
        let x = 30 + 5;
        let y = 4;
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Up(MouseButton::Left),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Beta");
    }

    #[test]
    fn clicking_navigation_arrows_goes_back_and_forward() {
        let v = Vault::new("click-nav-arrows");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.open_path(&v.path("wiki/b.md"), true);
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Beta");

        app.areas.nav_back = Some(Rect::new(31, 0, 3, 1));
        app.areas.nav_forward = Some(Rect::new(34, 0, 3, 1));

        // Click back arrow
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 32,
            row: 0,
            modifiers: KeyModifiers::NONE,
        });
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Alpha");

        // Click forward arrow
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 35,
            row: 0,
            modifiers: KeyModifiers::NONE,
        });
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Beta");
    }

    #[test]
    fn clicking_edit_button_enters_editor() {
        let v = Vault::new("click-edit-btn");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        assert!(!app.open.as_ref().unwrap().editing());

        app.areas.doc_edit = Some(Rect::new(85, 0, 3, 1));
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 86,
            row: 0,
            modifiers: KeyModifiers::NONE,
        });
        assert!(app.open.as_ref().unwrap().editing());
    }

    #[test]
    fn a_single_click_on_a_source_row_selects_it_and_a_double_one_opens_it() {
        let v = Vault::new("citation");
        v.write(
            "sources/s1/metadata.md",
            "---\ntitle: Source One\nauthors: [\"A. One\", \"B. Two\"]\nyear: 2024\n---\n# Source One\n\nAbstract.\n",
        );
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.show_inspector = false;

        // Links in wiki/a.md, in order: Beta, gone, then the `[^s1]` citation.
        app.run(Cmd::NextLink);
        app.run(Cmd::NextLink);
        app.run(Cmd::NextLink);
        app.run(Cmd::FollowLink);
        assert_eq!(app.open.as_ref().unwrap().selected_citation.as_deref(), Some("s1"));
        assert!(app.show_inspector, "following a citation reveals the inspector");

        // The renderer would normally fill these in; set them directly to
        // isolate the click-handling logic from layout.
        app.areas.inspector = Rect::new(30, 30, 60, 10);
        app.areas.inspector_body = Rect::new(31, 31, 58, 8);
        app.areas.inspector_divider = None;
        app.areas.inspector_rows = vec![Some("s1".to_string())];
        let down = |row: u16| MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 32,
            row,
            modifiers: KeyModifiers::NONE,
        };

        // A single click selects the source: its `[n]` stays highlighted in the
        // body and the page is untouched.
        app.on_mouse(down(31));
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Alpha");
        assert_eq!(app.open.as_ref().unwrap().selected_citation.as_deref(), Some("s1"));
        assert_eq!(app.open.as_ref().unwrap().link, Some(2), "the body [n] is the active link");

        // A second click on the same row within the double-click window opens it.
        app.on_mouse(down(31));
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Source One");
    }

    #[test]
    fn clicking_a_backlink_in_inspector_opens_the_page() {
        let v = Vault::new("backlinks-click");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Alpha");

        app.areas.inspector = Rect::new(30, 30, 60, 10);
        app.areas.inspector_body = Rect::new(31, 31, 58, 8);
        app.areas.inspector_divider = None;
        app.areas.inspector_rows = vec![None];
        app.areas.inspector_backlinks = vec![
            InspectorBacklink {
                row: 0,
                col_start: 2,
                col_end: 20,
                path: v.path("wiki/b.md"),
            },
        ];

        let click = |col: u16, row: u16| MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: col,
            row,
            modifiers: KeyModifiers::NONE,
        };

        // Clicking on "← " (column 31 or 32, i.e. col 0 or 1 inside body) does not open the page
        app.on_mouse(click(31, 31));
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Alpha");

        // Clicking inside the backlink title (column 34, i.e. col 3 inside body) opens wiki/b.md
        app.on_mouse(click(34, 31));
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Beta");
    }

    #[test]
    fn following_a_broken_link_says_so_instead_of_navigating() {
        let v = Vault::new("broken");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::NextLink);
        app.run(Cmd::NextLink); // the second link is the broken one
        app.run(Cmd::FollowLink);
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Alpha");
        assert!(app.toasts.last().unwrap().text.contains("broken link"));
    }

    #[test]
    fn navigating_to_the_page_already_open_does_not_stack_history() {
        let v = Vault::new("dedupe");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        // Leaving home is a move like any other, so the first open records it.
        assert_eq!(app.back, vec![Spot::Home]);
        app.open_path(&v.path("wiki/a.md"), true);
        assert_eq!(app.back, vec![Spot::Home], "re-opening the same page adds nothing");
    }

    /// Dragging the boundary under a collection resizes it, and the size
    /// survives a reload — geometry the user set is theirs, not the session's.
    #[test]
    fn a_collection_boundary_drags_and_the_height_persists() {
        let v = Vault::new("collection-drag");
        let mut app = v.app();
        app.areas.tree = Rect::new(0, 0, 30, 30);
        app.areas.tree_panes = vec![
            TreePane { area: Rect::new(0, 0, 30, 10), ..Default::default() },
            TreePane { area: Rect::new(0, 10, 30, 10), ..Default::default() },
            TreePane { area: Rect::new(0, 20, 30, 10), ..Default::default() },
        ];
        app.areas.collection_dividers = vec![(0, 10), (1, 20)];

        // The boundary under the first box is grabbable where it is drawn.
        assert_eq!(app.divider_at(4, 10), Some(Divider::Collection(0)));
        assert_eq!(app.divider_at(4, 20), Some(Divider::Collection(1)));

        app.resize_to(Divider::Collection(0), 4, 16);
        assert_eq!(app.cfg.collection_heights.get("wiki").copied(), Some(16));

        app.persist_widths();
        let reloaded = crate::config::Config::load_with_herdr_theme(&v.0, None);
        assert_eq!(reloaded.collection_heights.get("wiki").copied(), Some(16));
    }

    /// A folded box is one row by definition, so its boundary is not a handle.
    /// Writing a height there would resize it behind your back on unfold.
    #[test]
    fn dragging_a_folded_collections_boundary_does_nothing() {
        let v = Vault::new("collection-drag-folded");
        let mut app = v.app();
        app.areas.tree = Rect::new(0, 0, 30, 30);
        app.areas.tree_panes = vec![
            TreePane { area: Rect::new(0, 0, 30, 1), folded: true, ..Default::default() },
            TreePane { area: Rect::new(0, 1, 30, 29), ..Default::default() },
        ];
        app.resize_to(Divider::Collection(0), 4, 12);
        assert!(app.cfg.collection_heights.is_empty());
    }

    /// Folding is remembered too, and both the handle and the key reach it.
    #[test]
    fn folding_a_collection_persists_and_the_key_toggles_it() {
        let v = Vault::new("collection-fold");
        let mut app = v.app();
        app.fold_collection(0);
        assert!(app.cfg.collapsed_collections.contains("wiki"));
        let reloaded = crate::config::Config::load_with_herdr_theme(&v.0, None);
        assert!(reloaded.collapsed_collections.contains("wiki"));

        // The key acts on whichever collection the cursor is in.
        app.tree.selected = 0;
        app.run(Cmd::ToggleCollection);
        assert!(!app.cfg.collapsed_collections.contains("wiki"), "the same key unfolds it");
    }

    /// Home is reachable again once you have left it — by the `home` command,
    /// by walking the history back, and by the arrows that drive both.
    #[test]
    fn home_is_a_destination_you_can_navigate_back_to() {
        let v = Vault::new("home");
        let mut app = v.app();
        assert_eq!(app.here(), Spot::Home, "the session opens on the mark");

        app.open_path(&v.path("wiki/a.md"), true);
        assert!(matches!(app.here(), Spot::Page(_)));

        // Back from the first page lands on home rather than doing nothing.
        app.run(Cmd::Back);
        assert_eq!(app.here(), Spot::Home);
        assert!(app.open.is_none(), "the home screen is what gets drawn");

        // And forward returns to the page that was left.
        app.run(Cmd::Forward);
        assert_eq!(app.here(), Spot::Page(v.path("wiki/a.md")));

        // The command gets there in one step. Like opening a page, it is a
        // fresh navigation and so clears the forward stack — `back` is what
        // returns you, exactly as it would after following a link.
        app.run(Cmd::Home);
        assert_eq!(app.here(), Spot::Home);
        assert!(app.forward.is_empty(), "a new navigation drops the forward stack");
        app.run(Cmd::Back);
        assert_eq!(app.here(), Spot::Page(v.path("wiki/a.md")));
    }

    /// `home` while already home must not stack a history entry, or repeated
    /// presses bury the page you came from under copies of the mark.
    #[test]
    fn home_from_home_is_a_no_op() {
        let v = Vault::new("home-idempotent");
        let mut app = v.app();
        app.run(Cmd::Home);
        app.run(Cmd::Home);
        assert!(app.back.is_empty());
        assert_eq!(app.here(), Spot::Home);
    }

    #[test]
    fn the_same_key_reaches_the_right_pane() {
        let v = Vault::new("focus");
        let mut app = v.app();
        app.areas.doc = Rect::new(30, 0, 60, 6);
        app.areas.doc_main = app.areas.doc;
        app.areas.doc_body = Rect::new(32, 1, 56, 4);
        app.open_path(&v.path("wiki/a.md"), true);

        app.focus = Focus::Doc;
        app.on_key(ch('j'));
        assert_eq!(app.open.as_ref().unwrap().scroll, 1);

        app.focus = Focus::Tree;
        let before = app.tree.selected;
        app.on_key(ch('j'));
        assert_eq!(app.tree.selected, before + 1);
        assert_eq!(app.open.as_ref().unwrap().scroll, 1, "the reader did not also scroll");
    }

    #[test]
    fn a_two_key_sequence_is_held_then_applied() {
        let v = Vault::new("sequence");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::ScrollDown);

        app.on_key(ch('g'));
        assert_eq!(app.pending, Some('g'));
        app.on_key(ch('g'));
        assert_eq!(app.pending, None);
        assert_eq!(app.open.as_ref().unwrap().scroll, 0);
    }

    #[test]
    fn scrolling_is_clamped_to_the_document() {
        let v = Vault::new("scroll");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::DocTop);
        app.run(Cmd::ScrollUp);
        assert_eq!(app.open.as_ref().unwrap().scroll, 0);
        app.run(Cmd::DocBottom);
        let at = app.open.as_ref().unwrap().scroll;
        app.run(Cmd::ScrollDown);
        assert_eq!(app.open.as_ref().unwrap().scroll, at);
    }

    #[test]
    fn the_last_lines_are_reachable_with_the_sources_pane_open() {
        let v = Vault::new("scroll-inspector");
        let body: String = (1..=200).map(|i| format!("line {i}\n")).collect();
        let long = v.write("wiki/long.md", &format!("---\ntitle: Long\ntype: concept\ncategory: c\nrationale: r\n---\n{body}"));
        let mut app = v.app();
        app.open_path(&long, true);
        app.run(Cmd::DocBottom);
        let open = app.open.as_ref().unwrap();
        // The reader body is 30 rows of the 40-row column; the other 10 are
        // the sources pane and are not room the document can scroll into.
        assert_eq!(open.scroll, open.doc.height() - 30, "the tail of the page must scroll into the reader");
    }

    #[test]
    fn reflow_keeps_the_reading_position() {
        let v = Vault::new("reflow");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::DocBottom);
        let source = {
            let open = app.open.as_ref().unwrap();
            open.doc.source_for_line(open.scroll)
        };
        app.areas.doc = Rect::new(30, 0, 30, 40);
        app.reflow();
        let open = app.open.as_ref().unwrap();
        assert_eq!(open.doc.source_for_line(open.scroll), source);
    }

    #[test]
    fn focus_cycling_skips_hidden_panes() {
        let v = Vault::new("cycle");
        let mut app = v.app();
        app.show_sidebar = false;
        app.focus = Focus::Tree;
        assert_eq!(app.tree.collection_paths().len(), 2, "fixture has a wiki and a workspace collection");

        // Each collection box is its own stop before Tab moves on to the doc.
        app.run(Cmd::CycleFocus);
        assert_eq!(app.focus, Focus::Tree, "steps into the second collection box first");
        let (second_start, _) = app.tree.section_span(1).unwrap();
        assert_eq!(app.tree.selected, second_start);

        app.run(Cmd::CycleFocus);
        assert_eq!(app.focus, Focus::Doc);
        app.run(Cmd::CycleFocus);
        assert_eq!(app.focus, Focus::Tree, "no sidebar to visit, wraps back to the first collection");

        app.show_tree = false;
        app.focus = Focus::Doc;
        app.run(Cmd::CycleFocus);
        assert_eq!(app.focus, Focus::Doc);
    }

    #[test]
    fn cycling_stops_at_the_sources_box_when_the_inspector_is_open() {
        let v = Vault::new("cycle-sources");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.show_sidebar = false;
        app.show_tree = false;
        app.show_inspector = true;
        // The renderer sizes this normally; set it directly so `stops()` sees
        // the inspector as actually on screen without a full layout pass.
        app.areas.inspector = Rect::new(0, 30, 60, 10);
        app.focus = Focus::Doc;
        app.doc_stop = DocStop::Content;

        app.run(Cmd::CycleFocus);
        assert_eq!(app.focus, Focus::Doc);
        assert_eq!(app.doc_stop, DocStop::Sources, "the sources box gets its own stop");

        app.run(Cmd::CycleFocus);
        assert_eq!(app.focus, Focus::Doc);
        assert_eq!(app.doc_stop, DocStop::Content, "wraps back with tree and sidebar both hidden");

        app.run(Cmd::CycleFocusBack);
        assert_eq!(app.doc_stop, DocStop::Sources, "shift+tab steps backward through the same stops");
    }

    #[test]
    fn hiding_the_focused_pane_moves_focus_out_of_it() {
        let v = Vault::new("hide");
        let mut app = v.app();
        app.focus = Focus::Tree;
        app.run(Cmd::ToggleTree);
        assert!(!app.show_tree);
        assert_eq!(app.focus, Focus::Doc);
    }

    #[test]
    fn focusing_the_sidebar_is_refused_when_there_is_none() {
        let v = Vault::new("no-sidebar");
        let mut app = v.app();
        app.focus = Focus::Doc;
        app.run(Cmd::FocusSidebar);
        assert_eq!(app.focus, Focus::Doc);
    }

    #[test]
    fn leaving_the_sidebar_advances_instead_of_stepping_back() {
        let v = Vault::new("leave-sidebar");
        let mut app = v.app();
        app.show_sidebar = true;
        let cmd = portable_pty::CommandBuilder::new("/bin/sh");
        let (tx, _rx) = std::sync::mpsc::channel();
        app.sidebar = Some(crate::herdr::pty::Pane::spawn_command(cmd, 10, 40, tx, None).unwrap());
        app.focus = Focus::Sidebar;

        // F12 ("leave agents pane") used to always land on Doc, one step
        // *back* in Tree → Doc → Sidebar order. It should behave like Tab:
        // advance forward, wrapping to Tree.
        app.run(Cmd::LeaveSidebar);
        assert_eq!(app.focus, Focus::Tree, "f12 advances forward, wrapping past doc to the tree");
    }

    #[test]
    fn the_editor_opens_at_the_line_being_read_and_saves() {
        let v = Vault::new("edit");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::DocBottom);
        app.run(Cmd::Edit);

        let open = app.open.as_ref().unwrap();
        let editor = open.editor.as_ref().unwrap();
        assert!(editor.cursor_line() >= open.page.body_start);
        assert_eq!(app.ctx(), Ctx::Edit);

        app.run(Cmd::Save);
        assert!(app.toasts.last().unwrap().text.starts_with("saved"));
    }

    #[test]
    fn a_paste_into_the_editor_lands_verbatim() {
        let v = Vault::new("paste-edit");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::Edit);
        let row = app.open.as_ref().unwrap().editor.as_ref().unwrap().cursor_line();

        // A list, which as a burst of key events would have had every marker
        // doubled by `continue_block`, and a tab, which would have indented.
        app.on_paste("- one\n- two\n\ttail");

        let editor = app.open.as_ref().unwrap().editor.as_ref().unwrap();
        let lines: Vec<String> = editor.text().lines().map(str::to_string).collect();
        assert_eq!(lines[row], "- one");
        assert_eq!(lines[row + 1], "- two");
        // What the cursor was sitting in front of rides on the last pasted
        // line, the way splitting a line always works.
        assert_eq!(lines[row + 2], "\ttail# Alpha");
        assert_eq!(editor.cursor_line(), row + 2);
        assert_eq!(editor.cursor_col(), "\ttail".chars().count(), "cursor sits at the end of the paste");
    }

    /// Before bracketed paste, a paste reached the reader as one key event per
    /// character, so pasting prose containing `L` ran the linter and `P` opened
    /// the project switcher.
    #[test]
    fn undo_and_redo_walk_the_buffer_back_and_forward() {
        let v = Vault::new("undo");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::Edit);
        let before = app.open.as_ref().unwrap().editor.as_ref().unwrap().text();

        app.on_key(ch('x'));
        assert!(app.open.as_ref().unwrap().editor.as_ref().unwrap().dirty());

        app.on_key(ctrl('z'));
        assert_eq!(app.open.as_ref().unwrap().editor.as_ref().unwrap().text(), before, "undo restored the buffer");

        app.on_key(ctrl('r'));
        assert_ne!(app.open.as_ref().unwrap().editor.as_ref().unwrap().text(), before, "redo put the edit back");
    }

    #[test]
    fn a_paste_into_the_reader_runs_no_commands() {
        let v = Vault::new("paste-reader");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.focus = Focus::Doc;

        app.on_paste("Lint P / text");

        assert!(app.overlay.is_none(), "no command ran");
        assert!(app.find.is_none());
        assert!(!app.open.as_ref().unwrap().editing());
    }

    #[test]
    fn a_paste_into_a_prompt_is_typed_into_it() {
        let v = Vault::new("paste-prompt");
        let mut app = v.app();
        app.run(Cmd::FindFiles);
        app.on_paste("alpha\nbeta");
        match app.overlay.as_ref() {
            // The newline is dropped rather than submitting the finder midway.
            Some(Overlay::Finder(f)) => assert_eq!(f.query, "alphabeta"),
            _ => panic!("expected the finder to still be open"),
        }
    }

    #[test]
    fn reader_keys_do_not_fire_while_editing() {
        let v = Vault::new("edit-keys");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::Edit);
        let before = app.open.as_ref().unwrap().scroll;
        app.on_key(ch('j'));
        assert_eq!(app.open.as_ref().unwrap().scroll, before, "j is a motion, not a scroll");
        assert!(app.open.as_ref().unwrap().editing());
    }

    #[test]
    fn creating_a_page_scaffolds_okf_frontmatter_and_opens_the_editor() {
        let v = Vault::new("new");
        let mut app = v.app();
        app.run(Cmd::NewPage);
        for c in "wiki/health/sleep".chars() {
            app.on_key(ch(c));
        }
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));

        let created = v.path("wiki/health/sleep.md");
        assert!(created.is_file());
        let text = std::fs::read_to_string(&created).unwrap();
        assert!(text.contains("type: concept"));
        assert!(text.contains("category: health"));
        assert!(text.contains("title: sleep"));
        assert_eq!(app.open.as_ref().unwrap().page.path, created);
        assert!(app.open.as_ref().unwrap().editing());
    }

    #[test]
    fn a_new_page_path_cannot_escape_the_checkout() {
        let v = Vault::new("escape");
        let mut app = v.app();
        app.create_page("../../etc/evil", None);
        assert!(app.toasts.last().unwrap().text.contains("escapes"));
        assert!(app.open.is_none());
    }

    #[test]
    fn the_finder_filters_live_and_opens_the_hit() {
        let v = Vault::new("finder");
        let mut app = v.app();
        app.run(Cmd::FindFiles);
        assert!(matches!(app.overlay, Some(Overlay::Finder(_))));
        for c in "beta".chars() {
            app.on_key(ch(c));
        }
        let Some(Overlay::Finder(finder)) = app.overlay.as_ref() else { panic!() };
        assert_eq!(finder.hits.len(), 1);
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Beta");
    }

    #[test]
    fn the_finder_cycles_modes_and_scopes() {
        let v = Vault::new("finder-modes");
        let mut app = v.app();
        app.run(Cmd::FindFiles);
        app.on_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL));
        let Some(Overlay::Finder(f)) = app.overlay.as_ref() else { panic!() };
        assert_eq!(f.mode, search::Mode::Text);

        app.on_key(KeyEvent::new(KeyCode::Char('l'), KeyModifiers::CONTROL));
        let Some(Overlay::Finder(f)) = app.overlay.as_ref() else { panic!() };
        assert_eq!(f.collection, Some("wiki"));
    }

    #[test]
    fn semantic_search_is_never_run_while_typing() {
        let v = Vault::new("semantic");
        let mut app = v.app();
        app.run(Cmd::FindSemantic);
        for c in "creatine".chars() {
            app.on_key(ch(c));
        }
        assert!(!app.jobs.is_busy(), "typing must not spawn the engine");
        let Some(Overlay::Finder(f)) = app.overlay.as_ref() else { panic!() };
        assert!(f.hits.is_empty());
        assert!(f.running.is_none());
    }

    #[test]
    fn semantic_search_refuses_when_qmd_is_disabled() {
        let v = Vault::new("no-qmd");
        std::fs::write(v.path(".podarcis/config.yaml"), "engines:\n  qmd: false\n").unwrap();
        let mut app = v.app();
        app.cfg = Config::load(&v.0);
        app.run(Cmd::FindSemantic);
        for c in "x".chars() {
            app.on_key(ch(c));
        }
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let Some(Overlay::Finder(f)) = app.overlay.as_ref() else { panic!() };
        assert!(f.warning.as_ref().unwrap().contains("engines.qmd"));
        assert!(!app.jobs.is_busy());
    }

    #[test]
    fn the_palette_filters_by_title_and_runs_the_command() {
        let v = Vault::new("palette");
        let mut app = v.app();
        app.run(Cmd::Palette);
        for c in "restart".chars() {
            app.on_key(ch(c));
        }
        let Some(Overlay::Palette(p)) = app.overlay.as_ref() else { panic!() };
        assert_eq!(p.items.len(), 1);
        assert_eq!(p.items[0].0, Cmd::Restart);

        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
    }

    #[test]
    fn the_palette_never_offers_a_movement_command() {
        assert!(palette_items("").iter().all(|(cmd, _, _)| cmd.palette_visible()));
        assert!(palette_items("down").is_empty());
    }

    #[test]
    fn quitting_with_a_job_in_flight_asks_once() {
        let v = Vault::new("quit-busy");
        let mut app = v.app();
        app.jobs.running.push(crate::actions::Running { id: 1, label: "sync".into() });
        app.on_key(ctrl('q'));
        assert!(!app.quit);
        assert!(app.toasts.last().unwrap().text.contains("sync"));
        app.on_key(ctrl('q'));
        assert!(app.quit);
    }

    #[test]
    fn any_other_key_resets_the_quit_confirmation() {
        let v = Vault::new("quit-reset");
        let mut app = v.app();
        app.jobs.running.push(crate::actions::Running { id: 1, label: "sync".into() });
        app.on_key(ctrl('q'));
        app.on_key(ch('j'));
        app.on_key(ctrl('q'));
        assert!(!app.quit, "the warning has to be re-earned");
    }

    /// The reachable way to quit on top of a dirty buffer: `ctrl+q` is text
    /// while the editor holds focus, but clicking into the tree leaves the
    /// editor open and puts `ctrl+q` back in reach.
    #[test]
    fn quitting_with_unsaved_changes_asks_once() {
        let v = Vault::new("quit-dirty");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::Edit);
        app.editor_key(ch('x'));
        assert!(app.open.as_ref().unwrap().editor.as_ref().unwrap().dirty());
        app.focus = Focus::Tree;

        app.on_key(ctrl('q'));
        assert!(!app.quit, "an unsaved buffer is not discarded by reflex");
        assert!(app.toasts.last().unwrap().text.contains("unsaved changes"));
        app.on_key(ctrl('q'));
        assert!(app.quit, "the second press means it");
    }

    #[test]
    fn a_clean_buffer_quits_on_the_first_press() {
        let v = Vault::new("quit-clean");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::Edit);
        app.focus = Focus::Tree;
        app.on_key(ctrl('q'));
        assert!(app.quit, "the guard is about unsaved work, not about editing");
    }

    #[test]
    fn restarting_with_unsaved_changes_instructs_instead_of_prompting() {
        let v = Vault::new("restart-dirty");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::Edit);
        app.editor_key(ch('x'));

        // Twice, because the palette would reset a "choose it again" prompt.
        app.run(Cmd::Restart);
        app.run(Cmd::Restart);
        assert!(!app.restart);
        assert!(app.toasts.last().unwrap().text.contains("ctrl+s to save"));
    }

    #[test]
    fn q_does_not_quit_ctrl_q_does() {
        let v = Vault::new("quit-keys");
        let mut app = v.app();
        app.on_key(ch('q'));
        assert!(!app.quit);
        app.on_key(ch('q'));
        assert!(!app.quit, "double q does not quit either");

        app.on_key(ctrl('q'));
        assert!(app.quit);
    }

    #[test]
    fn a_single_click_in_the_tree_selects_but_does_not_open() {
        let v = Vault::new("mouse");
        let mut app = v.app();
        let target = app.tree.rows.iter().position(|r| r.label == "a").unwrap();
        let (x, y) = (app.areas.tree.x + 2, app.areas.tree.y + 1 + target as u16);
        press(&mut app, x, y);
        assert_eq!(app.tree.selected, target, "the click selects the row");
        assert!(app.open.is_none(), "a single click must not open the page");
        assert_eq!(app.focus, Focus::Tree);
    }

    #[test]
    fn a_double_click_in_the_tree_opens_the_page() {
        let v = Vault::new("mouse-double");
        let mut app = v.app();
        let target = app.tree.rows.iter().position(|r| r.label == "a").unwrap();
        let (x, y) = (app.areas.tree.x + 2, app.areas.tree.y + 1 + target as u16);
        press(&mut app, x, y);
        assert!(app.open.is_none());
        press(&mut app, x, y);
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Alpha");
        assert_eq!(app.focus, Focus::Doc, "opening a page focuses it");
    }

    #[test]
    fn two_clicks_on_different_rows_never_open() {
        let v = Vault::new("mouse-diff-rows");
        let mut app = v.app();
        let a = app.tree.rows.iter().position(|r| r.label == "a").unwrap();
        let b = app.tree.rows.iter().position(|r| r.label == "b").unwrap();
        let (x, ya) = (app.areas.tree.x + 2, app.areas.tree.y + 1 + a as u16);
        let yb = app.areas.tree.y + 1 + b as u16;
        press(&mut app, x, ya);
        press(&mut app, x, yb);
        assert!(app.open.is_none(), "fast clicks on different rows are singles");
        assert_eq!(app.tree.selected, b, "the last row is the one selected");
    }

    #[test]
    fn a_single_click_on_a_folder_toggles_it_without_opening() {
        let v = Vault::new("mouse-folder");
        v.write("wiki/health/_index.md", "# Health\n");
        let mut app = v.app();
        app.tree.rebuild();
        let folder = app.tree.rows.iter().position(|r| r.label == "health").unwrap();
        let (x, y) = (app.areas.tree.x + 2, app.areas.tree.y + 1 + folder as u16);
        press(&mut app, x, y);
        assert!(app.tree.rows[folder].expanded, "a single click expands a folder");
        assert!(app.open.is_none(), "and does not open its index");
    }

    #[test]
    fn a_double_click_on_a_folder_opens_its_index_page() {
        let v = Vault::new("mouse-folder-double");
        v.write("wiki/health/_index.md", "# Health\n");
        let mut app = v.app();
        app.tree.rebuild();
        let folder = app.tree.rows.iter().position(|r| r.label == "health").unwrap();
        let (x, y) = (app.areas.tree.x + 2, app.areas.tree.y + 1 + folder as u16);
        press(&mut app, x, y);
        press(&mut app, x, y);
        assert!(app.tree.rows[folder].expanded, "the folder stays expanded");
        assert_eq!(
            app.open.as_ref().unwrap().page.path,
            v.path("wiki/health/_index.md"),
            "the double click opens the folder's index"
        );
    }

    #[test]
    fn clicking_a_collection_title_opens_repo_config() {
        let v = Vault::new("repo-config");
        let mut app = v.app();
        app.areas.tree_panes = vec![crate::app::TreePane {
            area: Rect::new(0, 0, 30, 12),
            inner: Rect::new(1, 1, 28, 10),
            header: 0,
            start: 1,
            end: app.tree.rows.len(),
            offset: 0,
            fold: None,
            folded: false,
        }];
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 2,
            row: 0,
            modifiers: KeyModifiers::NONE,
        });
        match app.overlay {
            Some(Overlay::RepoConfig(ref state)) => {
                assert_eq!(state.name, app.tree.rows[0].label);
                assert_eq!(state.selected, 0);
            }
            _ => panic!("expected repo config overlay"),
        }
        assert!(app.tree.rows[0].expanded, "the collection stays expanded");
    }

    fn right_press(app: &mut App, x: u16, y: u16) {
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Right),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    }

    /// Right-click the tree row labelled `label` and return its row index.
    fn right_click(app: &mut App, label: &str) -> usize {
        let (i, y) = {
            let x0 = app.areas.tree.x;
            let y0 = app.areas.tree.y;
            let i = app.tree.rows.iter().position(|r| r.label == label).unwrap();
            (i, (x0 + 2, y0 + 1 + i as u16))
        };
        right_press(app, y.0, y.1);
        i
    }

    #[test]
    fn right_click_opens_a_context_menu_on_that_row() {
        let v = Vault::new("menu-open");
        let mut app = v.app();
        let i = right_click(&mut app, "a");
        assert_eq!(app.tree.selected, i, "the right-click selects the row");
        assert_eq!(app.focus, Focus::Tree);
        let Some(Overlay::Menu(menu)) = app.overlay.as_ref() else { panic!("expected menu overlay") };
        assert_eq!(menu.path, v.path("wiki/a.md"));
        let labels: Vec<&str> = menu.items.iter().map(|(_, l)| *l).collect();
        assert_eq!(labels, vec!["open", "rename…", "delete…", "copy relative path", "copy absolute path"]);
    }

    #[test]
    fn a_collection_root_offers_no_rename_or_delete() {
        let v = Vault::new("menu-collection");
        let mut app = v.app();
        right_click(&mut app, "wiki");
        let Some(Overlay::Menu(menu)) = app.overlay.as_ref() else { panic!("expected menu overlay") };
        let labels: Vec<&str> = menu.items.iter().map(|(_, l)| *l).collect();
        assert_eq!(labels, vec!["open", "new page…", "copy relative path", "copy absolute path"]);
    }

    #[test]
    fn the_menu_creates_a_page_inside_the_clicked_folder() {
        let v = Vault::new("menu-new-page");
        let mut app = v.app();
        right_click(&mut app, "wiki");
        app.on_key(ch('j')); // new page…
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let Some(Overlay::Prompt(prompt)) = app.overlay.as_ref() else { panic!("expected new-page prompt") };
        assert_eq!(prompt.kind, PromptKind::NewPage);
        assert_eq!(prompt.value, "");
        assert_eq!(prompt.path.as_deref(), Some(v.path("wiki").as_path()));
        app.on_key(ch('c'));
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
        assert!(v.path("wiki/c.md").exists(), "the page was created in the clicked folder");
        assert!(app.tree.rows.iter().any(|r| r.label == "c"), "the tree sees the new page");
        assert!(app.open.is_some(), "the new page is opened");
    }

    #[test]
    fn a_normal_folder_also_offers_new_page() {
        let v = Vault::new("menu-new-page-dir");
        v.write("wiki/nested/x.md", "# X\n");
        let mut app = v.app();
        right_click(&mut app, "nested");
        let Some(Overlay::Menu(menu)) = app.overlay.as_ref() else { panic!("expected menu overlay") };
        let labels: Vec<&str> = menu.items.iter().map(|(_, l)| *l).collect();
        assert_eq!(labels, vec!["open", "new page…", "rename…", "delete…", "copy relative path", "copy absolute path"]);
    }

    #[test]
    fn the_menu_scrolls_by_keypress_and_activates_on_enter() {
        let v = Vault::new("menu-keys");
        let mut app = v.app();
        right_click(&mut app, "a");
        app.on_key(ch('j'));
        app.on_key(ch('j'));
        let Some(Overlay::Menu(menu)) = app.overlay.as_ref() else { panic!("expected menu") };
        assert_eq!(menu.selected, 2, "j moves past open and rename…");
        app.on_key(ch('k'));
        let Some(Overlay::Menu(menu)) = app.overlay.as_ref() else { panic!("expected menu") };
        assert_eq!(menu.selected, 1);
        // Enter on rename… hands the slug to the prompt.
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let Some(Overlay::Prompt(prompt)) = app.overlay.as_ref() else { panic!("expected prompt") };
        assert_eq!(prompt.kind, PromptKind::Rename);
        assert_eq!(prompt.value, "a");
        assert_eq!(prompt.path.as_deref(), Some(v.path("wiki/a.md").as_path()));
    }

    #[test]
    fn esc_closes_the_menu_without_running_anything() {
        let v = Vault::new("menu-esc");
        let mut app = v.app();
        right_click(&mut app, "b");
        app.on_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.overlay.is_none(), "esc dismisses");
        assert!(app.open.is_none());
    }

    #[test]
    fn the_menu_renames_a_page_on_disk() {
        let v = Vault::new("menu-rename");
        let mut app = v.app();
        right_click(&mut app, "a");
        app.on_key(ch('j')); // rename…
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        // Backspace the slug "a", type "z", confirm.
        app.on_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        app.on_key(ch('z'));
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
        assert!(!v.path("wiki/a.md").exists(), "the old file is gone");
        assert!(v.path("wiki/z.md").exists(), "the new file is there");
        assert!(app.tree.rows.iter().any(|r| r.label == "z"), "the tree sees the new name");
        assert!(app.toasts.last().unwrap().text.contains("renamed wiki/a.md → wiki/z.md"));
    }

    #[test]
    fn the_menu_deletes_a_page_only_after_confirmation() {
        let v = Vault::new("menu-delete");
        let mut app = v.app();
        right_click(&mut app, "b");
        app.on_key(ch('j'));
        app.on_key(ch('j')); // delete…
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let Some(Overlay::Menu(confirm)) = app.overlay.as_ref() else { panic!("expected confirm menu") };
        assert_eq!(confirm.path, v.path("wiki/b.md"));
        assert_eq!(confirm.items.iter().map(|(_, l)| *l).collect::<Vec<_>>(), vec!["cancel", "delete"]);
        // Esc on the confirm leaves the file alone.
        app.on_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
        assert!(v.path("wiki/b.md").exists());

        // Back through the whole flow and confirm this time.
        right_click(&mut app, "b");
        app.on_key(ch('j'));
        app.on_key(ch('j'));
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        app.on_key(ch('j')); // move onto "delete"
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
        assert!(!v.path("wiki/b.md").exists());
        assert!(!app.tree.rows.iter().any(|r| r.label == "b"));
    }

    #[test]
    fn renaming_or_deleting_a_collection_root_is_refused() {
        let v = Vault::new("menu-rename-root");
        let mut app = v.app();
        app.prompt_rename(&v.path("wiki"));
        assert!(app.overlay.is_none(), "no rename prompt opens for a collection root");
        assert!(app.toasts.last().unwrap().text.contains("cannot be renamed"));

        app.delete_path(&v.path("workspace"));
        assert!(v.path("workspace").is_dir(), "the collection survives");
        assert!(app.toasts.last().unwrap().text.contains("cannot be deleted"));
    }

    #[test]
    fn clicking_outside_the_menu_dismisses_it() {
        let v = Vault::new("menu-dismiss");
        let mut app = v.app();
        right_click(&mut app, "a");
        assert!(app.overlay.is_some());
        // Click in the document area, well away from the popup.
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: app.areas.doc.x + 5,
            row: app.areas.doc.y + 5,
            modifiers: KeyModifiers::NONE,
        });
        assert!(app.overlay.is_none(), "a press outside the popup dismisses it");
    }

    #[test]
    fn releasing_on_a_menu_item_activates_it() {
        let v = Vault::new("menu-mouse");
        let mut app = v.app();
        right_click(&mut app, "a");
        let Some(Overlay::Menu(menu)) = app.overlay.as_ref() else { panic!("expected menu") };
        let area = menu.area;
        // Down on the second item, up on the same row -> rename prompt.
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: area.x + 2,
            row: area.y + 1 + 1,
            modifiers: KeyModifiers::NONE,
        });
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Up(MouseButton::Left),
            column: area.x + 2,
            row: area.y + 1 + 1,
            modifiers: KeyModifiers::NONE,
        });
        let Some(Overlay::Prompt(prompt)) = app.overlay.as_ref() else { panic!("expected rename prompt") };
        assert_eq!(prompt.kind, PromptKind::Rename);
    }

    #[test]
    fn the_menu_copies_relative_and_absolute_paths() {
        let v = Vault::new("menu-copy");
        let mut app = v.app();
        right_click(&mut app, "a");
        app.on_key(ch('j'));
        app.on_key(ch('j'));
        app.on_key(ch('j')); // copy relative path
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
        assert!(app.toasts.last().unwrap().text.contains("wiki/a.md"));

        right_click(&mut app, "a");
        app.on_key(ch('j'));
        app.on_key(ch('j'));
        app.on_key(ch('j'));
        app.on_key(ch('j')); // copy absolute path
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.toasts.last().unwrap().text.contains("wiki/a.md"));
    }

    #[test]
    fn the_wheel_scrolls_the_pane_under_the_pointer() {
        let v = Vault::new("wheel");
        let mut app = v.app();
        app.areas.doc = Rect::new(30, 0, 60, 6);
        app.areas.doc_main = app.areas.doc;
        app.areas.doc_body = Rect::new(32, 1, 56, 4);
        app.open_path(&v.path("wiki/a.md"), true);
        app.focus = Focus::Tree;
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::ScrollDown,
            column: 40,
            row: 3,
            modifiers: KeyModifiers::NONE,
        });
        assert!(app.open.as_ref().unwrap().scroll > 0, "the pointer decides, not focus");
        assert_eq!(app.focus, Focus::Tree, "scrolling does not steal focus");
    }

    fn right_press_at(app: &mut App, x: u16, y: u16) {
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Right),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    }

    fn right_drag_to(app: &mut App, x: u16, y: u16) {
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Drag(MouseButton::Right),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    }

    fn right_release_at(app: &mut App, x: u16, y: u16) {
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Up(MouseButton::Right),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    }

    #[test]
    fn right_drag_sends_a_file_mention_into_the_agent_session() {
        let v = Vault::new("right-send");
        let mut app = laid_out(&v);
        app.open_path(&v.path("wiki/a.md"), true);

        // A real child that echoes what it reads, so the test proves the bytes
        // actually leave the app rather than only updating state.
        let (tx, rx) = std::sync::mpsc::channel();
        let mut cmd = portable_pty::CommandBuilder::new("/bin/sh");
        cmd.args(["-c", "read line; printf 'GOT:%s\\n' \"$line\""]);
        cmd.env("TERM", "xterm-256color");
        let pane = crate::herdr::pty::Pane::spawn_command(cmd, 10, 40, tx, None).unwrap();
        app.sidebar = Some(pane);

        // Right-drag across "See Beta and gone." (wiki/a.md line 4, the one
        // body source line it renders from, starting at file line 9): the
        // mention must name file line 12 — not the rendered line 4.
        right_press_at(&mut app, 32, 4);
        right_drag_to(&mut app, 50, 4);
        right_release_at(&mut app, 50, 4);

        for _ in 0..50 {
            match rx.recv_timeout(std::time::Duration::from_millis(100)) {
                Ok(crate::event::AppEvent::PtyExited) | Err(_) => break,
                Ok(_) => {}
            }
        }
        app.sidebar.as_ref().unwrap().with_screen(|s| {
            let text = s.contents();
            assert!(text.contains("GOT:@wiki/a.md:12"), "{text:?}");
        });
        assert!(app.toasts.last().unwrap().text.contains("sent selection to the agent"));
        assert!(
            !app.toasts.last().unwrap().text.contains("@wiki/a.md"),
            "the toast must not leak the file path: {:?}",
            app.toasts.last().unwrap().text
        );
        assert!(app.open.as_ref().unwrap().selection.is_some(), "the selection stays visible");
    }

    #[test]
    fn right_drag_without_an_agent_session_warns_and_keeps_the_selection() {
        let v = Vault::new("right-send-none");
        let mut app = laid_out(&v);
        app.open_path(&v.path("wiki/a.md"), true);
        app.sidebar = None;

        right_press_at(&mut app, 32, 4);
        right_drag_to(&mut app, 50, 4);
        right_release_at(&mut app, 50, 4);

        assert!(app.toasts.last().unwrap().text.contains("no agent session"));
        assert!(app.open.as_ref().unwrap().selection.is_some());
    }

    #[test]
    fn a_right_click_without_a_drag_sends_nothing() {
        let v = Vault::new("right-click-none");
        let mut app = laid_out(&v);
        app.open_path(&v.path("wiki/a.md"), true);

        right_press_at(&mut app, 32, 4);
        right_release_at(&mut app, 32, 4);

        assert!(app.toasts.is_empty(), "a click without a drag sends nothing");
        assert!(app.open.as_ref().unwrap().selection.is_none());
    }

    fn press(app: &mut App, x: u16, y: u16) {
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    }

    fn drag(app: &mut App, x: u16, y: u16) {
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Drag(MouseButton::Left),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    }

    fn release(app: &mut App, x: u16, y: u16) {
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Up(MouseButton::Left),
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        });
    }

    fn laid_out(v: &Vault) -> App {
        let mut app = v.app();
        app.areas = Areas {
            tree: Rect::new(0, 0, 30, 40),
            doc: Rect::new(30, 0, 90, 40),
            doc_main: Rect::new(30, 0, 90, 30),
            doc_body: Rect::new(30, 0, 90, 30),
            sidebar: Rect::new(120, 0, 40, 40),
            inspector: Rect::new(30, 30, 90, 10),
            inspector_body: Rect::default(),
            inspector_rows: Vec::new(),
            tree_panes: Vec::new(),
            tree_divider: Some(29),
            sidebar_divider: Some(120),
            inspector_divider: Some(30),
            tree_toggle: Some(Rect::new(29, 20, 1, 1)),
            sidebar_toggle: Some(Rect::new(120, 20, 1, 1)),
            inspector_toggle: Some(Rect::new(75, 30, 1, 1)),
            ..Default::default()
        };
        app
    }

    #[test]
    fn clicking_the_tree_handle_collapses_it_instead_of_dragging() {
        let v = Vault::new("toggle-tree");
        let mut app = laid_out(&v);
        assert!(app.show_tree);
        press(&mut app, 29, 20);
        assert!(!app.show_tree, "the handle toggles, it does not start a drag");
        assert!(app.dragging.is_none());
    }

    #[test]
    fn clicking_a_tab_focuses_its_pane_and_reopens_it_if_hidden() {
        let v = Vault::new("tab-click");
        let mut app = laid_out(&v);
        app.areas.tab_bar = vec![
            (Focus::Tree, "Tree", Rect::new(0, 0, 27, 1)),
            (Focus::Doc, "Doc", Rect::new(27, 0, 26, 1)),
            (Focus::Sidebar, "Agents", Rect::new(53, 0, 27, 1)),
        ];
        app.show_tree = false;
        app.focus = Focus::Doc;

        press(&mut app, 10, 0);
        assert_eq!(app.focus, Focus::Tree, "clicking a hidden pane's tab still switches focus to it");
        assert!(app.show_tree, "and reopens it, the same way a hand-hidden pane is meant to come back");
    }

    #[test]
    fn clicking_the_sidebar_handle_collapses_it_instead_of_dragging() {
        let v = Vault::new("toggle-sidebar");
        let mut app = laid_out(&v);
        assert!(app.show_sidebar);
        press(&mut app, 120, 20);
        assert!(!app.show_sidebar);
        assert!(app.dragging.is_none());
    }

    #[test]
    fn clicking_the_inspector_handle_hides_the_sources_section() {
        let v = Vault::new("toggle-inspector");
        let mut app = laid_out(&v);
        assert!(app.show_inspector);
        press(&mut app, 75, 30);
        assert!(!app.show_inspector, "the handle toggles the inspector, it does not start a drag");
        assert!(app.dragging.is_none());
        // And it comes back from the same spot.
        press(&mut app, 75, 30);
        assert!(app.show_inspector);
    }

    #[test]
    fn dragging_the_tree_divider_resizes_it() {
        let v = Vault::new("drag-tree");
        let mut app = laid_out(&v);
        press(&mut app, 29, 5);
        assert_eq!(app.dragging, Some(Divider::Tree));
        drag(&mut app, 49, 5);
        assert_eq!(app.cfg.tree_width, 50);
        release(&mut app, 49, 5);
        assert!(app.dragging.is_none());
    }

    #[test]
    fn dragging_the_agents_divider_resizes_from_the_right() {
        let v = Vault::new("drag-sidebar");
        let mut app = laid_out(&v);
        press(&mut app, 120, 5);
        assert_eq!(app.dragging, Some(Divider::Sidebar));
        drag(&mut app, 100, 5);
        assert_eq!(app.cfg.sidebar_width, 60);
    }

    #[test]
    fn a_divider_can_be_grabbed_from_a_column_either_side() {
        let v = Vault::new("drag-grab");
        let mut app = laid_out(&v);
        press(&mut app, 30, 5);
        assert_eq!(app.dragging, Some(Divider::Tree), "one column of slack each way");
    }

    #[test]
    fn a_drag_never_squeezes_the_document_away() {
        let v = Vault::new("drag-clamp");
        let mut app = laid_out(&v);
        press(&mut app, 29, 5);
        drag(&mut app, 500, 5);
        assert!(app.cfg.tree_width <= 160 - 30 - 40, "the document keeps 30 columns");
        drag(&mut app, 0, 5);
        assert_eq!(app.cfg.tree_width, 12, "and the tree keeps a usable minimum");
    }

    #[test]
    fn a_drag_that_wanders_off_the_divider_still_resizes() {
        let v = Vault::new("drag-wander");
        let mut app = laid_out(&v);
        press(&mut app, 29, 5);
        // Straying into the tree pane must not be read as a click in the tree.
        drag(&mut app, 40, 5);
        assert_eq!(app.cfg.tree_width, 41);
        assert!(app.open.is_none(), "no page was opened by the wandering drag");
    }

    #[test]
    fn a_click_away_from_a_divider_is_an_ordinary_click() {
        let v = Vault::new("drag-none");
        let mut app = laid_out(&v);
        press(&mut app, 60, 5);
        assert!(app.dragging.is_none());
        assert_eq!(app.focus, Focus::Doc);
    }

    #[test]
    fn dragging_the_inspector_divider_resizes_height() {
        let v = Vault::new("drag-inspector");
        let mut app = laid_out(&v);
        press(&mut app, 60, 30);
        assert_eq!(app.dragging, Some(Divider::Inspector));
        drag(&mut app, 60, 25);
        assert_eq!(app.cfg.inspector_height, 15);
        drag(&mut app, 60, 35);
        assert_eq!(app.cfg.inspector_height, 5);
        release(&mut app, 60, 35);
        assert!(app.dragging.is_none());
    }

    #[test]
    fn inspector_drag_clamps_to_min_and_max() {
        let v = Vault::new("drag-inspector-clamp");
        let mut app = laid_out(&v);
        press(&mut app, 60, 30);
        drag(&mut app, 60, 0);
        assert_eq!(app.cfg.inspector_height, 32);
        drag(&mut app, 60, 50);
        assert_eq!(app.cfg.inspector_height, 4);
    }

    #[test]
    fn scrolling_inspector_with_mouse_wheel() {
        let v = Vault::new("scroll-inspect");
        let mut app = laid_out(&v);
        let path = v.write(
            "wiki/sources.md",
            "---\ntitle: S\ntype: concept\ncategory: c\nsources:\n  - id: s1\n  - id: s2\n  - id: s3\n  - id: s4\n  - id: s5\n  - id: s6\n  - id: s7\n  - id: s8\n  - id: s9\n  - id: s10\n---\nbody\n",
        );
        app.open_path(&path, false);
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::ScrollDown,
            column: 60,
            row: 35,
            modifiers: KeyModifiers::NONE,
        });
        assert!(app.open.as_ref().unwrap().inspect_scroll > 0);
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::ScrollUp,
            column: 60,
            row: 35,
            modifiers: KeyModifiers::NONE,
        });
        assert_eq!(app.open.as_ref().unwrap().inspect_scroll, 0);
    }

    #[test]
    fn clicking_and_dragging_inspector_scrollbar() {
        let v = Vault::new("scrollbar-inspect");
        let mut app = laid_out(&v);
        let sources_yaml = (0..20).map(|i| format!("  - id: s{i}\n")).collect::<String>();
        let path = v.write(
            "wiki/long_sources.md",
            &format!("---\ntitle: LS\ntype: concept\ncategory: c\nsources:\n{sources_yaml}---\nbody\n"),
        );
        app.open_path(&path, false);
        press(&mut app, 119, 37);
        assert_eq!(app.dragging_scrollbar, Some(ScrollbarDrag::Inspector));
        let scroll_after_press = app.open.as_ref().unwrap().inspect_scroll;
        assert!(scroll_after_press > 0);
        drag(&mut app, 119, 32);
        assert!(app.open.as_ref().unwrap().inspect_scroll < scroll_after_press);
        release(&mut app, 119, 32);
        assert!(app.dragging_scrollbar.is_none());
    }

    #[test]
    fn clicking_and_dragging_doc_scrollbar() {
        let v = Vault::new("scrollbar-doc");
        let mut app = laid_out(&v);
        let long_body = (0..100).map(|i| format!("Line {i}\n\n")).collect::<String>();
        let path = v.write(
            "wiki/long_doc.md",
            &format!("---\ntitle: LD\ntype: concept\ncategory: c\n---\n{long_body}"),
        );
        app.open_path(&path, false);
        press(&mut app, 119, 20);
        assert_eq!(app.dragging_scrollbar, Some(ScrollbarDrag::Doc));
        assert!(app.open.as_ref().unwrap().scroll > 0);
        release(&mut app, 119, 20);
        assert!(app.dragging_scrollbar.is_none());
    }

    #[test]
    fn clicking_doc_scrollbar_at_inner_columns() {
        let v = Vault::new("scrollbar-inner");
        let mut app = laid_out(&v);
        let long_body = (0..100).map(|i| format!("Line {i}\n\n")).collect::<String>();
        let path = v.write(
            "wiki/long_doc.md",
            &format!("---\ntitle: LD\ntype: concept\ncategory: c\n---\n{long_body}"),
        );
        app.open_path(&path, false);
        // Column 118 is right_border - 1 (inner thumb column)
        press(&mut app, 118, 20);
        assert_eq!(app.dragging_scrollbar, Some(ScrollbarDrag::Doc));
        release(&mut app, 118, 20);

        // Column 117 is right_border - 2 (reach leeway column)
        press(&mut app, 117, 20);
        assert_eq!(app.dragging_scrollbar, Some(ScrollbarDrag::Doc));
        release(&mut app, 117, 20);
    }

    #[test]
    fn a_click_in_an_empty_pane_is_ignored() {
        let v = Vault::new("mouse-empty");
        let mut app = v.app();
        app.areas.sidebar = Rect::ZERO;
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 200,
            row: 200,
            modifiers: KeyModifiers::NONE,
        });
        assert!(app.open.is_none());
    }

    #[test]
    fn keyboard_resize_adjusts_active_pane_and_persists() {
        let v = Vault::new("key-resize");
        let mut app = laid_out(&v);
        app.focus = Focus::Tree;
        let initial_tree = app.cfg.tree_width;
        app.run(Cmd::WidenPane);
        assert_eq!(app.cfg.tree_width, initial_tree + 4);
        app.run(Cmd::ShrinkPane);
        assert_eq!(app.cfg.tree_width, initial_tree);

        // Focusing sidebar resizes sidebar
        app.focus = Focus::Sidebar;
        let initial_sidebar = app.cfg.sidebar_width;
        app.run(Cmd::WidenSidebar);
        assert_eq!(app.cfg.sidebar_width, initial_sidebar + 4);
        app.run(Cmd::ShrinkSidebar);
        assert_eq!(app.cfg.sidebar_width, initial_sidebar);

        // When in Doc, WidenPane shrinks the sidebar so its divider follows
        // the arrow; the tree grows in its place.
        app.focus = Focus::Doc;
        app.show_sidebar = true;
        app.run(Cmd::WidenPane);
        assert_eq!(app.cfg.sidebar_width, initial_sidebar - 4);

        // And verifies persistence to config.yaml
        let reloaded = Config::load_with_herdr_theme(&app.cfg.root, None);
        assert_eq!(reloaded.sidebar_width, initial_sidebar - 4);
    }

    #[test]
    fn mouse_drag_release_persists_new_widths() {
        let v = Vault::new("drag-persist");
        let mut app = laid_out(&v);
        press(&mut app, 29, 5);
        drag(&mut app, 49, 5);
        release(&mut app, 49, 5);
        assert_eq!(app.cfg.tree_width, 50);

        let reloaded = Config::load_with_herdr_theme(&app.cfg.root, None);
        assert_eq!(reloaded.tree_width, 50);
    }

    #[test]
    fn drag_in_reader_selects_and_copies_text() {
        let v = Vault::new("drag-select");
        let mut app = laid_out(&v);
        app.open_path(&v.path("wiki/a.md"), true);
        app.areas.doc_body = Rect::new(32, 3, 50, 30);
        // Press at row 4 (line 1, the "# Alpha" title — line 0 is the doc's
        // leading blank), col 32
        press(&mut app, 32, 4);
        assert!(app.selecting_text);
        assert!(app.open.as_ref().unwrap().selection.is_some());

        // Drag to col 40, past the end of the rendered "# Alpha", which clamps
        // to its 7 columns — the `#` is drawn, so it is also selectable.
        drag(&mut app, 40, 4);
        let sel = app.open.as_ref().unwrap().selection.unwrap();
        assert_eq!(sel.anchor.col, 0);
        assert_eq!(sel.cursor.col, 7);

        // Release to copy
        release(&mut app, 40, 4);
        assert!(!app.selecting_text);
        assert!(app.open.as_ref().unwrap().selection.is_some());
        let toast = app.toasts.last().unwrap();
        assert!(toast.text.contains("copied"), "toast must announce copy: {}", toast.text);
    }

    #[test]
    fn click_without_drag_clears_selection() {
        let v = Vault::new("click-clear");
        let mut app = laid_out(&v);
        app.open_path(&v.path("wiki/a.md"), true);
        app.areas.doc_body = Rect::new(32, 3, 50, 30);
        // First, drag to select
        press(&mut app, 32, 4);
        drag(&mut app, 40, 4);
        release(&mut app, 40, 4);
        assert!(app.open.as_ref().unwrap().selection.is_some());

        // Click without dragging
        press(&mut app, 35, 4);
        release(&mut app, 35, 4);
        assert!(app.open.as_ref().unwrap().selection.is_none());
    }

    #[test]
    fn cmd_copy_copies_selection_then_page() {
        let v = Vault::new("cmd-copy");
        let mut app = laid_out(&v);
        app.open_path(&v.path("wiki/a.md"), true);
        app.areas.doc_body = Rect::new(32, 3, 50, 30);

        // With selection
        press(&mut app, 32, 3);
        drag(&mut app, 40, 3);
        release(&mut app, 40, 3);
        app.toasts.clear();
        app.run(Cmd::Copy);
        assert!(app.toasts.last().unwrap().text.contains("copied"));

        // Clear selection and copy page
        app.run(Cmd::ClearSelection);
        assert!(app.open.as_ref().unwrap().selection.is_none());
        app.toasts.clear();
        app.run(Cmd::Copy);
        assert!(app.toasts.last().unwrap().text.contains("copied page"));
    }

    #[test]
    fn selecting_rendered_text_still_copies_raw_markdown_syntax() {
        // "See Beta and gone." renders from a body line that reads
        // "See [Beta](b.md) and [gone](nope.md)." in the source — a partial
        // selection over the rendered prose must still yield that markdown
        // syntax, not the stripped display text.
        let v = Vault::new("copy-raw-source");
        let mut app = laid_out(&v);
        app.open_path(&v.path("wiki/a.md"), true);
        let open = app.open.as_ref().unwrap();
        let line = open
            .doc
            .lines
            .iter()
            .position(|l| l.plain_text().contains("See Beta and gone."))
            .expect("rendered line with the stripped link text");
        let sel = markdown::Selection::new(
            markdown::TextPos { line, col: 0 },
            markdown::TextPos { line, col: open.doc.lines[line].plain_text().chars().count() },
        );
        let text = open.selected_source_text(sel);
        assert_eq!(text, "See [Beta](b.md) and [gone](nope.md).");
    }

    #[test]
    fn uncited_sources_are_listed_from_the_citation_graph() {
        let v = Vault::new("uncited");
        std::fs::create_dir_all(v.path("sources/lit/x")).unwrap();
        std::fs::write(v.path("sources/lit/x/metadata.md"), "m\n").unwrap();
        let mut app = v.app();
        app.index = crate::vault::index::Index::build(&v.0, &app.collection_dirs());
        app.run(Cmd::Uncited);
        let Some(Overlay::Finder(f)) = app.overlay.as_ref() else { panic!("no finder") };
        assert_eq!(f.hits.len(), 1);
        assert_eq!(f.hits[0].rel, "sources/lit/x/metadata.md");
        assert!(f.warning.as_ref().unwrap().contains("not cited"));
    }

    #[test]
    fn overlays_swallow_keys_that_would_otherwise_quit() {
        let v = Vault::new("swallow");
        let mut app = v.app();
        app.run(Cmd::FindFiles);
        app.on_key(ch('q'));
        assert!(!app.quit, "q typed into the finder is a query, not a quit");
        app.on_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
        app.on_key(ctrl('q'));
        assert!(app.quit);
    }

    #[test]
    fn jumping_between_findings_visits_the_broken_link() {
        let v = Vault::new("findings");
        let mut app = v.app();
        let findings = app.index.all_findings();
        assert!(!findings.is_empty(), "expected findings, got none");
        app.run(Cmd::NextFinding);
        assert_eq!(app.open.as_ref().unwrap().page.path, v.path("wiki/a.md"));
        assert!(
            app.toasts.last().unwrap().text.contains("finding 1/"),
            "{:?}",
            app.toasts.last().unwrap().text
        );
    }

    #[test]
    fn a_clean_vault_says_so_rather_than_jumping_nowhere() {
        let v = Vault::new("clean");
        std::fs::write(v.path("wiki/a.md"), "---\ntitle: A\ntype: concept\ncategory: c\nrationale: r\n---\n# A\n").unwrap();
        let mut app = v.app();
        app.run(Cmd::NextFinding);
        assert_eq!(app.toasts.last().unwrap().level, Level::Good);
    }

    #[test]
    fn a_filesystem_change_refreshes_the_index_and_the_open_page() {
        let v = Vault::new("fs");
        let mut app = v.app();
        let path = v.path("wiki/b.md");
        app.open_path(&path, true);

        std::fs::write(&path, "---\ntitle: Beta Renamed\ntype: concept\ncategory: c\nrationale: r\n---\n# Beta Renamed\n").unwrap();
        app.on_fs_changed(vec![path.clone()]);

        assert_eq!(app.open.as_ref().unwrap().page.title(), "Beta Renamed");
        assert_eq!(app.index.get("wiki/b.md").unwrap().title, "Beta Renamed");
    }

    #[test]
    fn lint_results_are_summarised_not_dumped() {
        let v = Vault::new("lint");
        let mut app = v.app();
        app.on_job_done(JobResult {
            id: 1,
            label: "lint".into(),
            args: vec!["lint".into(), "--json".into()],
            code: 1,
            stdout: "{\"ok\": false, \"files\": {\"wiki/a.md\": [{\"code\": \"broken_link\", \"detail\": \"x\"}]}}".into(),
            stderr: String::new(),
        });
        let toast = app.toasts.last().unwrap();
        assert_eq!(toast.level, Level::Warn);
        assert!(toast.text.contains("1 findings across 1 files"), "{}", toast.text);
    }

    #[test]
    fn a_clean_lint_is_reported_as_good() {
        let v = Vault::new("lint-clean");
        let mut app = v.app();
        app.on_job_done(JobResult {
            id: 1,
            label: "lint".into(),
            args: vec!["lint".into(), "--json".into()],
            code: 0,
            stdout: "{\"ok\": true, \"files\": {}}".into(),
            stderr: String::new(),
        });
        assert_eq!(app.toasts.last().unwrap().level, Level::Good);
    }

    #[test]
    fn toasts_expire_and_never_pile_up() {
        let v = Vault::new("toasts");
        let mut app = v.app();
        for i in 0..6 {
            app.toast(Level::Info, format!("message {i}"));
        }
        assert_eq!(app.toasts.len(), 3);
        assert!(app.toasts[0].text.contains('3'));

        app.toasts[0].at = Instant::now() - TOAST_TTL - Duration::from_secs(1);
        app.expire_toasts();
        assert_eq!(app.toasts.len(), 2);
    }

    #[test]
    fn ctrl_f_searches_the_open_page_rather_than_the_whole_vault() {
        let v = Vault::new("find-in-page");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.on_key(ctrl('f'));
        assert!(app.find.is_some(), "ctrl+f opens the in-page bar");
        assert!(app.overlay.is_none(), "and never the vault-wide finder");

        for c in "beta".chars() {
            app.on_key(ch(c));
        }
        assert_eq!(app.finds().hits.len(), 1, "case-insensitive, and only this page");

        app.on_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.find.is_none(), "esc closes it");
    }

    #[test]
    fn enter_walks_the_hits_and_wraps_round() {
        let v = Vault::new("find-cycle");
        let mut app = v.app();
        let path = v.write(
            "wiki/c.md",
            "---\ntitle: Gamma\ntype: concept\ncategory: c\nrationale: r\n---\n# Gamma\n\nfirst hit\n\nsecond hit\n\nthird hit\n",
        );
        app.open_path(&path, true);
        app.run(Cmd::FindInPage);
        for c in "hit".chars() {
            app.on_key(ch(c));
        }
        assert_eq!(app.finds().hits.len(), 3);
        assert_eq!(app.finds().current, 0, "typing lands on the first hit");

        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        app.on_key(enter);
        assert_eq!(app.finds().current, 1);
        app.on_key(enter);
        app.on_key(enter);
        assert_eq!(app.finds().current, 0, "wraps past the last hit");
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT));
        assert_eq!(app.finds().current, 2, "shift+enter walks back");
    }

    #[test]
    fn the_find_bar_swallows_keys_that_would_otherwise_be_commands() {
        let v = Vault::new("find-swallows");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::FindInPage);
        // `q` quits and `e` edits when the reader has focus; in the bar they
        // are two characters of a query.
        app.on_key(ch('q'));
        app.on_key(ch('e'));
        assert!(!app.quit);
        assert!(!app.open.as_ref().unwrap().editing());
        assert_eq!(app.find.as_ref().unwrap().query, "qe");
        app.on_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        assert_eq!(app.find.as_ref().unwrap().query, "q");
    }

    #[test]
    fn a_hit_below_the_fold_is_scrolled_into_view() {
        let v = Vault::new("find-scroll");
        let mut app = v.app();
        let mut body =
            String::from("---\ntitle: Long\ntype: concept\ncategory: c\nrationale: r\n---\n# Long\n\n");
        for i in 0..120 {
            body.push_str(&format!("filler line {i}\n\n"));
        }
        body.push_str("the needle\n");
        let path = v.write("wiki/long.md", &body);
        app.open_path(&path, true);
        // The reader has not been drawn, so give it the viewport a draw would.
        app.areas.doc_body = Rect::new(0, 0, 80, 20);
        app.run(Cmd::FindInPage);
        for c in "needle".chars() {
            app.on_key(ch(c));
        }
        let finds = app.finds();
        assert_eq!(finds.hits.len(), 1);
        let open = app.open.as_ref().unwrap();
        let line = finds.hits[0].line;
        assert!(line >= open.scroll && line < open.scroll + 20, "hit {line} off screen at {}", open.scroll);
    }

    #[test]
    fn find_declines_politely_when_nothing_is_open() {
        let v = Vault::new("find-empty");
        let mut app = v.app();
        app.run(Cmd::FindInPage);
        assert!(app.find.is_none());
        assert!(app.toasts.last().unwrap().text.contains("no page open"));
    }

    #[test]
    fn the_outline_jumps_to_a_heading() {
        let v = Vault::new("outline");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        app.run(Cmd::Outline);
        assert!(matches!(app.overlay, Some(Overlay::Outline { .. })));
        app.on_key(ch('j'));
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
        assert!(app.open.as_ref().unwrap().scroll > 0);
    }
}
