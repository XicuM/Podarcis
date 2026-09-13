//! Application state and the command dispatcher.
//!
//! Everything the user can do arrives here as a `Cmd` from the keymap or an
//! overlay, so there is one list of behaviours and one place they are
//! implemented. Rendering reads this state and writes nothing back except the
//! pane geometry it just measured.

use std::path::{Path, PathBuf};
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;

use crate::actions::{JobResult, JobRunner, JobSpec};
use crate::config::Config;
use crate::editor::Editor;
use crate::event::AppEvent;
use crate::herdr;
use crate::keymap::{self, Cmd, Ctx, Resolved};
use crate::search::{self, Hit};
use crate::theme::Theme;
use crate::ui::markdown;
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

pub struct Open {
    pub page: Page,
    pub doc: markdown::Doc,
    pub doc_width: u16,
    pub scroll: usize,
    pub link: Option<usize>,
    pub editor: Option<Editor>,
}

impl Open {
    fn load(path: &Path, root: &Path, width: u16, theme: &Theme) -> Option<Self> {
        let page = Page::load(path, root).ok()?;
        let doc = markdown::render(&page.body, width, theme);
        Some(Self { page, doc, doc_width: width, scroll: 0, link: None, editor: None })
    }

    fn reflow(&mut self, width: u16, theme: &Theme) {
        if width == self.doc_width {
            return;
        }
        let anchor = self.doc.source_for_line(self.scroll);
        self.doc = markdown::render(&self.page.body, width, theme);
        self.doc_width = width;
        self.scroll = self.doc.line_for_source(anchor);
    }

    pub fn editing(&self) -> bool {
        self.editor.is_some()
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
}

pub struct Prompt {
    pub kind: PromptKind,
    pub title: String,
    pub value: String,
}

pub enum Overlay {
    Finder(Finder),
    Palette(Palette),
    Help { scroll: usize },
    Outline { selected: usize },
    Prompt(Prompt),
}

/// Geometry from the last frame, so mouse events and the pty know where things
/// are. Written by the renderer, read by the app.
#[derive(Clone, Copy, Debug, Default)]
pub struct Areas {
    pub tree: Rect,
    pub doc: Rect,
    pub sidebar: Rect,
}

pub struct App {
    pub cfg: Config,
    pub theme: Theme,
    pub tree: Tree,
    pub index: Index,
    pub open: Option<Open>,
    pub focus: Focus,
    pub overlay: Option<Overlay>,
    pub toasts: Vec<Toast>,
    pub jobs: JobRunner,
    pub sidebar: Option<herdr::pty::Pane>,
    pub sidebar_error: Option<String>,
    pub show_tree: bool,
    pub show_sidebar: bool,
    pub show_inspector: bool,
    pub zoom: bool,
    pub back: Vec<PathBuf>,
    pub forward: Vec<PathBuf>,
    pub pending: Option<char>,
    pub finder_engine: search::Engine,
    pub quit: bool,
    /// Set by a first `q` while a job is running.
    quit_confirmed: bool,
    pub indexing: bool,
    /// Engine version from `pyproject.toml`, for the status bar.
    pub engine_version: Option<String>,
    pub areas: Areas,
    pub tx: Sender<AppEvent>,
    /// Cursor into `index.all_findings()` for `n` / `N`.
    finding_cursor: Option<usize>,
}

impl App {
    pub fn new(cfg: Config, tx: Sender<AppEvent>) -> Self {
        let theme = Theme::new(cfg.flavor);
        let engine_version = crate::config::engine_version(&cfg.root);
        let collections: Vec<PathBuf> = cfg.collections().into_iter().map(|(_, p)| p).collect();
        Self {
            tree: Tree::new(&cfg.root, collections),
            show_tree: cfg.tree_open,
            show_sidebar: cfg.sidebar_open,
            theme,
            cfg,
            index: Index::default(),
            open: None,
            focus: Focus::Tree,
            overlay: None,
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
            engine_version,
            indexing: true,
            areas: Areas::default(),
            tx,
            finding_cursor: None,
        }
    }

    pub fn collection_dirs(&self) -> Vec<PathBuf> {
        self.cfg.collections().into_iter().map(|(_, p)| p).collect()
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

    // ------------------------------------------------------------- documents

    pub fn doc_width(&self) -> u16 {
        // Two columns of border plus one of padding on each side.
        self.areas.doc.width.saturating_sub(4).max(20)
    }

    pub fn open_path(&mut self, path: &Path, push_history: bool) {
        if path.is_dir() {
            let index = path.join("_index.md");
            if index.is_file() {
                return self.open_path(&index, push_history);
            }
            return;
        }
        if let Some(current) = self.open.as_ref().map(|o| o.page.path.clone()) {
            if current == path {
                return;
            }
            if push_history {
                self.back.push(current);
                self.forward.clear();
            }
        }
        match Open::load(path, &self.cfg.root, self.doc_width(), &self.theme) {
            Some(open) => {
                self.open = Some(open);
                self.focus = Focus::Doc;
            }
            None => self.toast(Level::Bad, format!("could not read {}", path.display())),
        }
    }

    /// Re-read the open page from disk, keeping the reading position.
    pub fn reload_open(&mut self) {
        let Some(open) = self.open.as_ref() else { return };
        if open.editing() {
            return;
        }
        let (path, anchor) = (open.page.path.clone(), open.doc.source_for_line(open.scroll));
        if let Some(mut fresh) = Open::load(&path, &self.cfg.root, self.doc_width(), &self.theme) {
            fresh.scroll = fresh.doc.line_for_source(anchor);
            self.open = Some(fresh);
        }
    }

    pub fn reflow(&mut self) {
        let (width, theme) = (self.doc_width(), self.theme);
        if let Some(open) = self.open.as_mut() {
            open.reflow(width, &theme);
        }
    }

    // ----------------------------------------------------------------- mouse

    /// Click to focus and select, wheel to scroll. The pane under the pointer
    /// acts, whether or not it has keyboard focus — that is what a pointer is
    /// for.
    pub fn on_mouse(&mut self, mouse: MouseEvent) {
        if self.overlay.is_some() {
            return;
        }
        let (x, y) = (mouse.column, mouse.row);
        let inside = |rect: Rect| {
            !rect.is_empty()
                && x >= rect.x
                && x < rect.x + rect.width
                && y >= rect.y
                && y < rect.y + rect.height
        };

        if inside(self.areas.sidebar) {
            // The child gets its own mouse protocol; forwarding raw events is
            // more trouble than it is worth, so a click just moves focus.
            if matches!(mouse.kind, MouseEventKind::Down(MouseButton::Left)) {
                self.set_focus(Focus::Sidebar);
            }
            return;
        }

        if inside(self.areas.tree) {
            match mouse.kind {
                MouseEventKind::ScrollDown => self.tree.move_by(3),
                MouseEventKind::ScrollUp => self.tree.move_by(-3),
                MouseEventKind::Down(MouseButton::Left) => {
                    self.set_focus(Focus::Tree);
                    let row = (y.saturating_sub(self.areas.tree.y + 1)) as usize;
                    let offset = crate::ui::panes::tree_offset(self);
                    self.tree.move_to(offset + row);
                    if let Some(path) = self.tree.toggle() {
                        self.open_path(&path, true);
                    }
                }
                _ => {}
            }
            return;
        }

        if inside(self.areas.doc) {
            match mouse.kind {
                MouseEventKind::ScrollDown => self.scroll(3),
                MouseEventKind::ScrollUp => self.scroll(-3),
                MouseEventKind::Down(MouseButton::Left) => self.set_focus(Focus::Doc),
                _ => {}
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

    pub fn on_key(&mut self, key: KeyEvent) {
        if self.overlay.is_some() {
            return self.overlay_key(key);
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

    /// In the editor, only Save and Leave are ours; a visible completion popup
    /// claims its own navigation keys. Everything else is edtui's.
    fn editor_key(&mut self, key: KeyEvent) {
        if let Resolved::Run(cmd) = keymap::resolve(&key, Ctx::Edit, None) {
            return self.run(cmd);
        }

        let has_completion = self
            .open
            .as_ref()
            .and_then(|o| o.editor.as_ref())
            .is_some_and(|e| e.completion.is_some());

        if has_completion {
            let editor = self.open.as_mut().unwrap().editor.as_mut().unwrap();
            let handled = match (key.code, key.modifiers) {
                (KeyCode::Tab, _) | (KeyCode::Down, _) | (KeyCode::Char('n'), KeyModifiers::CONTROL) => {
                    editor.completion.as_mut().unwrap().move_by(1);
                    true
                }
                (KeyCode::BackTab, _) | (KeyCode::Up, _) | (KeyCode::Char('p'), KeyModifiers::CONTROL) => {
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

        if let Some(editor) = self.open.as_mut().and_then(|o| o.editor.as_mut()) {
            editor.events.on_key_event(key, &mut editor.state);
        }
        self.refresh_completion();
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

    pub fn run(&mut self, cmd: Cmd) {
        match cmd {
            Cmd::Quit => {
                // A sync or a commit mid-flight is not something to kill by
                // reflex; say so once, then take the second press as meaning it.
                if self.jobs.is_busy() && !self.quit_confirmed {
                    self.quit_confirmed = true;
                    let running = self.jobs.labels().join(", ");
                    self.toast(Level::Warn, format!("still running: {running} — press q again to quit"));
                } else {
                    self.quit = true;
                }
            }
            Cmd::Help => self.overlay = Some(Overlay::Help { scroll: 0 }),
            Cmd::Palette => self.open_palette(),
            Cmd::Reload => self.reload_everything(),
            Cmd::CycleTheme => self.cycle_theme(),

            Cmd::FindFiles => self.open_finder(search::Mode::Files),
            Cmd::FindText => self.open_finder(search::Mode::Text),
            Cmd::FindSemantic => self.open_finder(search::Mode::Semantic),

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
            Cmd::ToggleInspector => self.show_inspector = !self.show_inspector,
            Cmd::ZoomPane => self.zoom = !self.zoom,
            Cmd::LeaveSidebar => self.set_focus(Focus::Doc),

            Cmd::Back => self.go_back(),
            Cmd::Forward => self.go_forward(),
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
            Cmd::NextLink => self.move_link(true),
            Cmd::PrevLink => self.move_link(false),
            Cmd::FollowLink => self.follow_link(),
            Cmd::Outline => {
                if self.open.as_ref().is_some_and(|o| !o.doc.headings.is_empty()) {
                    self.overlay = Some(Overlay::Outline { selected: 0 });
                }
            }

            Cmd::Edit => self.enter_editor(),
            Cmd::Save => self.save(),
            Cmd::LeaveEdit => self.leave_editor(),

            Cmd::Lint => self.spawn(JobSpec::capturing("lint", &["lint", "--json"])),
            Cmd::SyncRepos => self.spawn(JobSpec::new("sync", &["repo", "sync"])),
            Cmd::Commit => self.prompt(PromptKind::Commit, "commit message"),
            Cmd::NewPage => self.prompt(PromptKind::NewPage, "new page path (relative to the checkout)"),
            Cmd::Uncited => self.show_uncited(),
        }
    }

    fn set_focus(&mut self, focus: Focus) {
        self.focus = match focus {
            Focus::Tree if !self.show_tree => Focus::Doc,
            Focus::Sidebar if !self.show_sidebar || self.sidebar.is_none() => self.focus,
            other => other,
        };
        self.zoom = false;
    }

    fn cycle_focus(&mut self, delta: isize) {
        let mut order = vec![Focus::Doc];
        if self.show_tree {
            order.insert(0, Focus::Tree);
        }
        if self.show_sidebar && self.sidebar.is_some() {
            order.push(Focus::Sidebar);
        }
        let at = order.iter().position(|f| *f == self.focus).unwrap_or(0) as isize;
        let n = order.len() as isize;
        self.focus = order[(((at + delta) % n + n) % n) as usize];
    }

    fn page_step(&self) -> isize {
        self.areas.doc.height.saturating_sub(2).max(1) as isize
    }

    fn scroll(&mut self, delta: isize) {
        let height = self.page_step().max(1) as usize;
        if let Some(open) = self.open.as_mut() {
            let last = open.doc.height().saturating_sub(height.min(open.doc.height()));
            open.scroll = (open.scroll as isize + delta).clamp(0, last as isize) as usize;
        }
    }

    fn move_link(&mut self, forward: bool) {
        if let Some(open) = self.open.as_mut() {
            open.link = open.doc.link_after(open.link, forward);
            if let Some(link) = open.link.and_then(|i| open.doc.links.get(i)) {
                // Keep the highlighted link on screen.
                let height = self.areas.doc.height.saturating_sub(2).max(1) as usize;
                if link.line < open.scroll || link.line >= open.scroll + height {
                    open.scroll = link.line.saturating_sub(height / 3);
                }
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
        }
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

    fn go_back(&mut self) {
        let Some(previous) = self.back.pop() else { return };
        if let Some(current) = self.open.as_ref().map(|o| o.page.path.clone()) {
            self.forward.push(current);
        }
        self.open_path(&previous, false);
    }

    fn go_forward(&mut self) {
        let Some(next) = self.forward.pop() else { return };
        if let Some(current) = self.open.as_ref().map(|o| o.page.path.clone()) {
            self.back.push(current);
        }
        self.open_path(&next, false);
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

    fn cycle_theme(&mut self) {
        self.cfg.flavor = self.cfg.flavor.next();
        self.theme = Theme::new(self.cfg.flavor);
        self.reflow_forced();
        herdr::pty::reload_config(self.cfg.flavor);
        let flavor = self.cfg.flavor.as_str();
        self.toast(Level::Info, format!("theme: catppuccin {flavor} (`podarcis config` to persist)"));
    }

    fn reflow_forced(&mut self) {
        let (width, theme) = (self.doc_width(), self.theme);
        if let Some(open) = self.open.as_mut() {
            open.doc_width = 0;
            open.reflow(width, &theme);
        }
    }

    fn reload_everything(&mut self) {
        self.tree.rebuild();
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
    }

    fn save(&mut self) {
        let Some(open) = self.open.as_mut() else { return };
        let Some(editor) = open.editor.as_mut() else { return };
        match editor.save() {
            Ok(()) => {
                let path = editor.path.clone();
                self.index.refresh(&path);
                self.toast(Level::Good, format!("saved {}", crate::vault::page::rel_path(&path, &self.cfg.root)));
                self.refresh_page_after_save();
            }
            Err(err) => self.toast(Level::Bad, format!("save failed: {err}")),
        }
    }

    /// Re-parse the page so the inspector and findings reflect what was saved,
    /// without closing the editor.
    fn refresh_page_after_save(&mut self) {
        let root = self.cfg.root.clone();
        if let Some(open) = self.open.as_mut() {
            if let Ok(page) = Page::load(&open.page.path, &root) {
                open.page = page;
            }
        }
    }

    fn leave_editor(&mut self) {
        let dirty = self
            .open
            .as_ref()
            .and_then(|o| o.editor.as_ref())
            .is_some_and(Editor::dirty);
        if dirty {
            self.toast(Level::Warn, "unsaved changes — ctrl+s to save, ctrl+w again to discard");
            if let Some(editor) = self.open.as_mut().and_then(|o| o.editor.as_mut()) {
                // Second press discards: mark it by clearing the completion and
                // letting the next call through.
                if editor.completion.is_none() && editor.dirty() {
                    editor.dismiss_completion();
                }
            }
        }
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

    fn open_palette(&mut self) {
        let items = palette_items("");
        self.overlay = Some(Overlay::Palette(Palette { query: String::new(), items, selected: 0 }));
    }

    fn prompt(&mut self, kind: PromptKind, title: &str) {
        self.overlay = Some(Overlay::Prompt(Prompt {
            kind,
            title: title.to_string(),
            value: String::new(),
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
            Some(Overlay::Palette(_)) => self.palette_key(key),
            Some(Overlay::Finder(_)) => self.finder_key(key),
            Some(Overlay::Prompt(_)) => self.prompt_key(key),
            None => {}
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
        if !self.cfg.qmd_enabled {
            let message = "semantic search is off (engines.qmd: false in config.yaml)";
            if let Some(Overlay::Finder(finder)) = self.overlay.as_mut() {
                finder.warning = Some(message.to_string());
            }
            return;
        }
        let collection = finder.collection.unwrap_or("all");
        let args = vec![
            "wiki".to_string(),
            "search".to_string(),
            finder.query.clone(),
            "--json".to_string(),
            "--collection".to_string(),
            collection.to_string(),
        ];
        let spec = JobSpec { label: "semantic search".into(), args, capture: true };
        let id = self.jobs.spawn(&self.cfg.cli(), &self.cfg.root, spec, self.tx.clone());
        if let Some(Overlay::Finder(finder)) = self.overlay.as_mut() {
            finder.running = Some(id);
            finder.warning = None;
        }
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
                let (kind, value) = (prompt.kind, prompt.value.trim().to_string());
                self.overlay = None;
                if value.is_empty() {
                    return;
                }
                match kind {
                    PromptKind::Commit => {
                        self.spawn(JobSpec::new("commit", &["repo", "commit", "-m", &value]))
                    }
                    PromptKind::NewPage => self.create_page(&value),
                }
            }
            _ => {}
        }
    }

    fn create_page(&mut self, rel: &str) {
        let rel = if rel.ends_with(".md") { rel.to_string() } else { format!("{rel}.md") };
        let path = crate::vault::links::normalize(&self.cfg.root.join(&rel));
        if !path.starts_with(&self.cfg.root) {
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

    fn spawn(&mut self, spec: JobSpec) {
        self.jobs.spawn(&self.cfg.cli(), &self.cfg.root, spec, self.tx.clone());
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

    fn report_lint(&mut self, result: &JobResult) {
        let Some(payload) = result.json() else {
            self.toast(Level::Bad, "lint produced no JSON payload");
            return;
        };
        let files = payload.get("files").and_then(|f| f.as_object());
        let count: usize = files.map(|f| f.values().filter_map(|v| v.as_array()).map(Vec::len).sum()).unwrap_or(0);
        if count == 0 {
            self.toast(Level::Good, "podarcis lint: clean");
        } else {
            let n = files.map(|f| f.len()).unwrap_or(0);
            self.toast(Level::Warn, format!("podarcis lint: {count} findings across {n} files"));
        }
    }

    pub fn on_index_ready(&mut self, index: Index) {
        self.index = index;
        self.indexing = false;
        self.finding_cursor = None;
    }

    pub fn on_fs_changed(&mut self, paths: Vec<PathBuf>) {
        let mut structural = false;
        for path in &paths {
            structural |= self.index.refresh(path);
        }
        if structural {
            self.tree.rebuild();
        }
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

    /// Start or resize the herdr pane to match the area it was drawn into.
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
            self.sidebar_error = Some("herdr exited — ctrl+g twice to restart".into());
            if self.focus == Focus::Sidebar {
                self.focus = Focus::Doc;
            }
            return;
        }
        if self.sidebar_error.is_some() {
            return;
        }
        if !herdr::pty::available() {
            self.sidebar_error = Some("herdr is not installed — see herdr.dev".into());
            return;
        }
        if let Err(err) = herdr::config::provision(self.cfg.flavor) {
            self.sidebar_error = Some(format!("herdr config: {err}"));
            return;
        }
        match herdr::pty::Pane::spawn(&self.cfg.root, rows, cols, self.tx.clone()) {
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
        sidebar: Rect::new(90, 0, 40, 40),
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
                    "---\ntitle: Alpha\ntype: concept\ncategory: c\nrationale: r\nsources:\n  - id: s1\n---\n# Alpha\n\nSee [Beta](b.md) and [gone](nope.md).\n\n## Section two\n\nCites[^s1].\n",
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
    fn opening_a_directory_opens_its_index_page() {
        let v = Vault::new("dir");
        let mut app = v.app();
        app.open_path(&v.path("wiki"), true);
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
        app.open_path(&v.path("wiki/a.md"), true);
        assert!(app.back.is_empty());
    }

    #[test]
    fn the_same_key_reaches_the_right_pane() {
        let v = Vault::new("focus");
        let mut app = v.app();
        app.areas.doc = Rect::new(30, 0, 60, 6);
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
        app.run(Cmd::CycleFocus);
        assert_eq!(app.focus, Focus::Doc);
        app.run(Cmd::CycleFocus);
        assert_eq!(app.focus, Focus::Tree, "no sidebar to visit");

        app.show_tree = false;
        app.focus = Focus::Doc;
        app.run(Cmd::CycleFocus);
        assert_eq!(app.focus, Focus::Doc);
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
        app.create_page("../../etc/evil");
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
        for c in "theme".chars() {
            app.on_key(ch(c));
        }
        let Some(Overlay::Palette(p)) = app.overlay.as_ref() else { panic!() };
        assert_eq!(p.items.len(), 1);
        assert_eq!(p.items[0].0, Cmd::CycleTheme);

        let before = app.theme.flavor;
        app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.overlay.is_none());
        assert_ne!(app.theme.flavor, before);
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
        app.jobs.running.push(crate::actions::Running {
            id: 1,
            label: "sync".into(),
            last_line: String::new(),
        });
        app.on_key(ch('q'));
        assert!(!app.quit);
        assert!(app.toasts.last().unwrap().text.contains("sync"));
        app.on_key(ch('q'));
        assert!(app.quit);
    }

    #[test]
    fn any_other_key_resets_the_quit_confirmation() {
        let v = Vault::new("quit-reset");
        let mut app = v.app();
        app.jobs.running.push(crate::actions::Running {
            id: 1,
            label: "sync".into(),
            last_line: String::new(),
        });
        app.on_key(ch('q'));
        app.on_key(ch('j'));
        app.on_key(ch('q'));
        assert!(!app.quit, "the warning has to be re-earned");
    }

    #[test]
    fn a_click_in_the_tree_selects_and_opens() {
        let v = Vault::new("mouse");
        let mut app = v.app();
        app.tree.move_to(0);
        let target = app.tree.rows.iter().position(|r| r.label == "a").unwrap();
        app.on_mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: app.areas.tree.x + 2,
            row: app.areas.tree.y + 1 + target as u16,
            modifiers: KeyModifiers::NONE,
        });
        assert_eq!(app.focus, Focus::Doc, "opening a page focuses it");
        assert_eq!(app.open.as_ref().unwrap().page.title(), "Alpha");
    }

    #[test]
    fn the_wheel_scrolls_the_pane_under_the_pointer() {
        let v = Vault::new("wheel");
        let mut app = v.app();
        app.areas.doc = Rect::new(30, 0, 60, 6);
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
        app.on_key(ch('q'));
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
    fn cycling_the_theme_reflows_the_document() {
        let v = Vault::new("theme");
        let mut app = v.app();
        app.open_path(&v.path("wiki/a.md"), true);
        let before = app.theme.flavor;
        app.run(Cmd::CycleTheme);
        assert_ne!(app.theme.flavor, before);
        assert_eq!(app.open.as_ref().unwrap().doc_width, app.doc_width());
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
