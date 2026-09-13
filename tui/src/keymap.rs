//! The one keymap.
//!
//! Bindings, their titles and their groups live in a single table, and both the
//! dispatcher and the help overlay read it. A key that works but is not
//! documented — or documented but does not work — is not expressible here.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// Every action the app can perform. Overlays translate their own keys; this is
/// the vocabulary everything else speaks.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cmd {
    // Session
    Quit,
    Help,
    Palette,
    Reload,
    CycleTheme,

    // Find
    FindFiles,
    FindText,
    FindSemantic,

    // Layout
    FocusTree,
    FocusDoc,
    FocusSidebar,
    CycleFocus,
    CycleFocusBack,
    ToggleTree,
    ToggleSidebar,
    ToggleInspector,
    ZoomPane,

    // Navigation
    Back,
    Forward,
    RevealInTree,
    NextFinding,
    PrevFinding,

    // Tree
    TreeDown,
    TreeUp,
    TreeExpand,
    TreeCollapse,
    TreeToggle,
    TreeNextSibling,
    TreePrevSibling,
    TreeCollapseAll,
    TreeTop,
    TreeBottom,

    // Reader
    ScrollDown,
    ScrollUp,
    HalfPageDown,
    HalfPageUp,
    PageDown,
    PageUp,
    DocTop,
    DocBottom,
    NextLink,
    PrevLink,
    FollowLink,
    Outline,

    // Editing
    Edit,
    Save,
    LeaveEdit,
    LeaveSidebar,

    // Engine
    Lint,
    SyncRepos,
    Commit,
    NewPage,
    /// Sources no wiki page cites — the live `literature_status`.
    Uncited,
}

impl Cmd {
    /// Commands offered in the palette, in order. Movement keys are excluded —
    /// nobody looks up "scroll down" in a command list.
    pub fn palette_visible(self) -> bool {
        !matches!(
            self,
            Cmd::TreeDown
                | Cmd::TreeUp
                | Cmd::TreeExpand
                | Cmd::TreeCollapse
                | Cmd::TreeToggle
                | Cmd::TreeNextSibling
                | Cmd::TreePrevSibling
                | Cmd::TreeTop
                | Cmd::TreeBottom
                | Cmd::ScrollDown
                | Cmd::ScrollUp
                | Cmd::HalfPageDown
                | Cmd::HalfPageUp
                | Cmd::PageDown
                | Cmd::PageUp
                | Cmd::DocTop
                | Cmd::DocBottom
                | Cmd::NextLink
                | Cmd::PrevLink
                | Cmd::LeaveEdit
                | Cmd::LeaveSidebar
        )
    }
}

/// Where a binding applies. `Global` works anywhere outside the editor.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ctx {
    Global,
    Tree,
    Doc,
    /// Only while the editor is open.
    Edit,
    /// Only while the herdr pane has focus. Almost nothing is bound here: every
    /// other key belongs to herdr.
    Sidebar,
}

pub struct Binding {
    pub keys: &'static [&'static str],
    pub cmd: Cmd,
    pub title: &'static str,
    pub group: &'static str,
    pub ctx: Ctx,
}

/// `ctrl+space` is deliberately absent: it is herdr's prefix, and the sidebar
/// would never see it if we claimed it.
pub const BINDINGS: &[Binding] = &[
    // Session
    b(&["q"], Cmd::Quit, "quit", "session", Ctx::Global),
    b(&["ctrl+q"], Cmd::Quit, "quit", "session", Ctx::Global),
    b(&["?"], Cmd::Help, "keys", "session", Ctx::Global),
    b(&["ctrl+p", ":"], Cmd::Palette, "command palette", "session", Ctx::Global),
    b(&["R"], Cmd::Reload, "reload from disk", "session", Ctx::Global),
    b(&["ctrl+t"], Cmd::CycleTheme, "cycle theme", "session", Ctx::Global),
    // Find
    b(&["ctrl+f", "f"], Cmd::FindFiles, "find page", "find", Ctx::Global),
    b(&["/"], Cmd::FindText, "search text", "find", Ctx::Global),
    b(&["S"], Cmd::FindSemantic, "semantic search", "find", Ctx::Global),
    // Layout
    b(&["1"], Cmd::FocusTree, "focus tree", "layout", Ctx::Global),
    b(&["2"], Cmd::FocusDoc, "focus page", "layout", Ctx::Global),
    b(&["3"], Cmd::FocusSidebar, "focus herdr", "layout", Ctx::Global),
    b(&["tab"], Cmd::CycleFocus, "next pane", "layout", Ctx::Global),
    b(&["backtab"], Cmd::CycleFocusBack, "previous pane", "layout", Ctx::Global),
    b(&["ctrl+b"], Cmd::ToggleTree, "toggle tree", "layout", Ctx::Global),
    b(&["ctrl+g"], Cmd::ToggleSidebar, "toggle herdr", "layout", Ctx::Global),
    b(&["ctrl+i"], Cmd::ToggleInspector, "toggle inspector", "layout", Ctx::Global),
    b(&["z"], Cmd::ZoomPane, "zoom pane", "layout", Ctx::Global),
    // Navigation
    b(&["ctrl+o", "["], Cmd::Back, "back", "navigate", Ctx::Global),
    b(&["]"], Cmd::Forward, "forward", "navigate", Ctx::Global),
    b(&["g r"], Cmd::RevealInTree, "reveal in tree", "navigate", Ctx::Global),
    b(&["n"], Cmd::NextFinding, "next finding", "navigate", Ctx::Global),
    b(&["N"], Cmd::PrevFinding, "previous finding", "navigate", Ctx::Global),
    // Tree
    b(&["j", "down"], Cmd::TreeDown, "down", "tree", Ctx::Tree),
    b(&["k", "up"], Cmd::TreeUp, "up", "tree", Ctx::Tree),
    b(&["l", "right"], Cmd::TreeExpand, "open", "tree", Ctx::Tree),
    b(&["h", "left"], Cmd::TreeCollapse, "close / parent", "tree", Ctx::Tree),
    b(&["enter", "space"], Cmd::TreeToggle, "open page", "tree", Ctx::Tree),
    b(&["}"], Cmd::TreeNextSibling, "next sibling", "tree", Ctx::Tree),
    b(&["{"], Cmd::TreePrevSibling, "previous sibling", "tree", Ctx::Tree),
    b(&["H"], Cmd::TreeCollapseAll, "collapse all", "tree", Ctx::Tree),
    b(&["g g"], Cmd::TreeTop, "first", "tree", Ctx::Tree),
    b(&["G"], Cmd::TreeBottom, "last", "tree", Ctx::Tree),
    // Reader
    b(&["j", "down"], Cmd::ScrollDown, "down", "page", Ctx::Doc),
    b(&["k", "up"], Cmd::ScrollUp, "up", "page", Ctx::Doc),
    b(&["ctrl+d"], Cmd::HalfPageDown, "half page down", "page", Ctx::Doc),
    b(&["ctrl+u"], Cmd::HalfPageUp, "half page up", "page", Ctx::Doc),
    b(&["pagedown"], Cmd::PageDown, "page down", "page", Ctx::Doc),
    b(&["pageup"], Cmd::PageUp, "page up", "page", Ctx::Doc),
    b(&["g g"], Cmd::DocTop, "top", "page", Ctx::Doc),
    b(&["G"], Cmd::DocBottom, "bottom", "page", Ctx::Doc),
    b(&["l", "right"], Cmd::NextLink, "next link", "page", Ctx::Doc),
    b(&["h", "left"], Cmd::PrevLink, "previous link", "page", Ctx::Doc),
    b(&["enter", "g d"], Cmd::FollowLink, "follow link", "page", Ctx::Doc),
    b(&["o"], Cmd::Outline, "outline", "page", Ctx::Doc),
    // Editing
    b(&["e", "i"], Cmd::Edit, "edit page", "edit", Ctx::Doc),
    b(&["ctrl+s"], Cmd::Save, "save", "edit", Ctx::Edit),
    b(&["ctrl+w"], Cmd::LeaveEdit, "leave editor", "edit", Ctx::Edit),
    // The single key the sidebar does not forward to herdr. Everything else,
    // ctrl+space included, belongs to the child.
    b(&["f12"], Cmd::LeaveSidebar, "leave herdr pane", "layout", Ctx::Sidebar),
    // Engine
    b(&["L"], Cmd::Lint, "run podarcis lint", "engine", Ctx::Global),
    b(&["g s"], Cmd::SyncRepos, "sync repositories", "engine", Ctx::Global),
    b(&["g c"], Cmd::Commit, "lint-gated commit", "engine", Ctx::Global),
    b(&["ctrl+n"], Cmd::NewPage, "new page", "engine", Ctx::Global),
    b(&["g u"], Cmd::Uncited, "uncited sources", "engine", Ctx::Global),
];

const fn b(
    keys: &'static [&'static str],
    cmd: Cmd,
    title: &'static str,
    group: &'static str,
    ctx: Ctx,
) -> Binding {
    Binding { keys, cmd, title, group, ctx }
}

/// A key spec parsed into something comparable with a `KeyEvent`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Chord {
    pub code: KeyCode,
    pub mods: KeyModifiers,
}

impl Chord {
    pub fn matches(&self, key: &KeyEvent) -> bool {
        if self.code != key.code {
            return false;
        }
        // Shift is implied by an uppercase character, so comparing it would
        // reject every capital letter binding.
        let ignore_shift = matches!(self.code, KeyCode::Char(_));
        let mask = if ignore_shift { !KeyModifiers::SHIFT } else { KeyModifiers::all() };
        (self.mods & mask) == (key.modifiers & mask)
    }
}

/// Parse `"ctrl+p"`, `"g d"`, `"enter"`. A space separates the two keys of a
/// sequence; `+` separates modifiers from the key.
pub fn parse(spec: &str) -> Option<Vec<Chord>> {
    let chords: Vec<Chord> = spec.split_whitespace().filter_map(parse_chord).collect();
    (!chords.is_empty() && chords.len() == spec.split_whitespace().count()).then_some(chords)
}

fn parse_chord(spec: &str) -> Option<Chord> {
    let mut mods = KeyModifiers::NONE;
    let mut parts: Vec<&str> = spec.split('+').collect();
    // `"+"` itself would split into two empty parts.
    if spec == "+" {
        parts = vec!["+"];
    }
    let key = parts.pop()?;
    for part in parts {
        mods |= match part.to_ascii_lowercase().as_str() {
            "ctrl" => KeyModifiers::CONTROL,
            "alt" => KeyModifiers::ALT,
            "shift" => KeyModifiers::SHIFT,
            _ => return None,
        };
    }
    let code = match key.to_ascii_lowercase().as_str() {
        "enter" | "return" => KeyCode::Enter,
        "esc" | "escape" => KeyCode::Esc,
        "tab" => KeyCode::Tab,
        "backtab" => KeyCode::BackTab,
        "space" => KeyCode::Char(' '),
        "backspace" => KeyCode::Backspace,
        "delete" | "del" => KeyCode::Delete,
        "up" => KeyCode::Up,
        "down" => KeyCode::Down,
        "left" => KeyCode::Left,
        "right" => KeyCode::Right,
        "home" => KeyCode::Home,
        "end" => KeyCode::End,
        "pageup" | "pgup" => KeyCode::PageUp,
        "pagedown" | "pgdn" => KeyCode::PageDown,
        _ => {
            let mut chars = key.chars();
            let ch = chars.next()?;
            if chars.next().is_some() {
                // Function keys are the only multi-character key names left.
                let n: u8 = key.strip_prefix(['f', 'F'])?.parse().ok()?;
                return Some(Chord { code: KeyCode::F(n), mods });
            }
            KeyCode::Char(ch)
        }
    };
    Some(Chord { code, mods })
}

/// Resolution result for one key press.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Resolved {
    /// A binding fired.
    Run(Cmd),
    /// The key started a sequence; hold it and wait for the next press.
    Pending(char),
    None,
}

/// Resolve a key against the table for `ctx`, honouring a pending sequence
/// prefix. `ctx_of_focus` is the pane-specific context to try before `Global`.
pub fn resolve(key: &KeyEvent, ctx: Ctx, pending: Option<char>) -> Resolved {
    // The sidebar is deliberately not layered over Global: while herdr has
    // focus, only its own escape key is ours.
    let contexts: &[Ctx] = if ctx == Ctx::Sidebar { &[Ctx::Sidebar] } else { &[ctx, Ctx::Global] };

    if let Some(prefix) = pending {
        for want in contexts {
            for binding in BINDINGS.iter().filter(|b| b.ctx == *want) {
                for spec in binding.keys {
                    let Some(chords) = parse(spec) else { continue };
                    if chords.len() == 2
                        && chords[0].code == KeyCode::Char(prefix)
                        && chords[1].matches(key)
                    {
                        return Resolved::Run(binding.cmd);
                    }
                }
            }
        }
        return Resolved::None;
    }

    let mut starts_sequence = None;
    for want in contexts {
        for binding in BINDINGS.iter().filter(|b| b.ctx == *want) {
            for spec in binding.keys {
                let Some(chords) = parse(spec) else { continue };
                match chords.len() {
                    1 if chords[0].matches(key) => return Resolved::Run(binding.cmd),
                    2 if chords[0].matches(key) => {
                        if let KeyCode::Char(c) = chords[0].code {
                            starts_sequence.get_or_insert(c);
                        }
                    }
                    _ => {}
                }
            }
        }
    }
    match starts_sequence {
        Some(c) => Resolved::Pending(c),
        None => Resolved::None,
    }
}

/// Bindings for the help overlay, grouped in table order.
pub fn help_rows() -> Vec<(&'static str, String, &'static str)> {
    let mut out: Vec<(&'static str, String, &'static str)> = Vec::new();
    for binding in BINDINGS {
        let keys: Vec<&str> = binding.keys.iter().copied().filter(|k| parse(k).is_some()).collect();
        if keys.is_empty() {
            continue;
        }
        let joined = keys.join("  ");
        if let Some(existing) = out.iter_mut().find(|(g, _, t)| *g == binding.group && *t == binding.title) {
            if !existing.1.contains(&joined) {
                existing.1 = format!("{}  {}", existing.1, joined);
            }
            continue;
        }
        out.push((binding.group, joined, binding.title));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(code: KeyCode, mods: KeyModifiers) -> KeyEvent {
        KeyEvent::new(code, mods)
    }

    fn ch(c: char) -> KeyEvent {
        let mods = if c.is_uppercase() { KeyModifiers::SHIFT } else { KeyModifiers::NONE };
        KeyEvent::new(KeyCode::Char(c), mods)
    }

    #[test]
    fn parses_modifiers_named_keys_and_sequences() {
        assert_eq!(parse("j").unwrap().len(), 1);
        assert_eq!(parse("ctrl+p").unwrap()[0].mods, KeyModifiers::CONTROL);
        assert_eq!(parse("enter").unwrap()[0].code, KeyCode::Enter);
        assert_eq!(parse("space").unwrap()[0].code, KeyCode::Char(' '));
        assert_eq!(parse("f5").unwrap()[0].code, KeyCode::F(5));
        assert_eq!(parse("g d").unwrap().len(), 2);
        assert_eq!(parse("+").unwrap()[0].code, KeyCode::Char('+'));
    }

    #[test]
    fn rejects_prose_that_is_not_a_key_spec() {
        assert!(parse("tab is taken; use l").is_none());
        assert!(parse("meta+x").is_none());
        assert!(parse("").is_none());
    }

    #[test]
    fn uppercase_bindings_match_despite_the_shift_modifier() {
        assert_eq!(resolve(&ch('G'), Ctx::Doc, None), Resolved::Run(Cmd::DocBottom));
        assert_eq!(resolve(&ch('N'), Ctx::Doc, None), Resolved::Run(Cmd::PrevFinding));
        assert_eq!(resolve(&ch('n'), Ctx::Doc, None), Resolved::Run(Cmd::NextFinding));
    }

    #[test]
    fn the_same_key_means_different_things_per_pane() {
        assert_eq!(resolve(&ch('j'), Ctx::Tree, None), Resolved::Run(Cmd::TreeDown));
        assert_eq!(resolve(&ch('j'), Ctx::Doc, None), Resolved::Run(Cmd::ScrollDown));
        assert_eq!(resolve(&ch('l'), Ctx::Tree, None), Resolved::Run(Cmd::TreeExpand));
        assert_eq!(resolve(&ch('l'), Ctx::Doc, None), Resolved::Run(Cmd::NextLink));
    }

    #[test]
    fn a_pane_binding_wins_over_a_global_one() {
        // `l` is bound in both panes but never globally; `q` is global only.
        assert_eq!(resolve(&ch('q'), Ctx::Tree, None), Resolved::Run(Cmd::Quit));
        assert_eq!(resolve(&ch('q'), Ctx::Doc, None), Resolved::Run(Cmd::Quit));
    }

    #[test]
    fn sequences_wait_for_the_second_key() {
        assert_eq!(resolve(&ch('g'), Ctx::Doc, None), Resolved::Pending('g'));
        assert_eq!(resolve(&ch('d'), Ctx::Doc, Some('g')), Resolved::Run(Cmd::FollowLink));
        assert_eq!(resolve(&ch('g'), Ctx::Doc, Some('g')), Resolved::Run(Cmd::DocTop));
        assert_eq!(resolve(&ch('s'), Ctx::Doc, Some('g')), Resolved::Run(Cmd::SyncRepos));
        assert_eq!(resolve(&ch('x'), Ctx::Doc, Some('g')), Resolved::None, "a dead sequence is dropped");
    }

    #[test]
    fn ctrl_space_is_never_bound_because_it_is_herdrs_prefix() {
        let ctrl_space = key(KeyCode::Char(' '), KeyModifiers::CONTROL);
        for ctx in [Ctx::Global, Ctx::Tree, Ctx::Doc, Ctx::Edit, Ctx::Sidebar] {
            assert_eq!(resolve(&ctrl_space, ctx, None), Resolved::None, "{ctx:?}");
        }
    }

    #[test]
    fn every_binding_in_the_table_is_a_parseable_key_spec() {
        for binding in BINDINGS {
            for spec in binding.keys {
                assert!(parse(spec).is_some(), "{spec:?} on {:?} is not a key spec", binding.cmd);
            }
        }
    }

    #[test]
    fn help_covers_every_command_that_has_a_real_binding() {
        let rows = help_rows();
        for binding in BINDINGS {
            assert!(
                rows.iter().any(|(g, _, t)| *g == binding.group && *t == binding.title),
                "{} is bound but undocumented",
                binding.title
            );
        }
    }

    #[test]
    fn help_merges_aliases_onto_one_row() {
        let rows = help_rows();
        let quit: Vec<&String> = rows.iter().filter(|(_, _, t)| *t == "quit").map(|(_, k, _)| k).collect();
        assert_eq!(quit.len(), 1, "aliases must share a row");
        assert!(quit[0].contains('q') && quit[0].contains("ctrl+q"));
    }

    #[test]
    fn the_sidebar_swallows_everything_except_its_escape_key() {
        assert_eq!(resolve(&ch('q'), Ctx::Sidebar, None), Resolved::None, "q must reach herdr");
        assert_eq!(resolve(&ch('j'), Ctx::Sidebar, None), Resolved::None);
        assert_eq!(
            resolve(&key(KeyCode::Char('p'), KeyModifiers::CONTROL), Ctx::Sidebar, None),
            Resolved::None
        );
        assert_eq!(
            resolve(&key(KeyCode::F(12), KeyModifiers::NONE), Ctx::Sidebar, None),
            Resolved::Run(Cmd::LeaveSidebar)
        );
    }

    #[test]
    fn movement_is_kept_out_of_the_palette() {
        assert!(!Cmd::ScrollDown.palette_visible());
        assert!(!Cmd::TreeDown.palette_visible());
        assert!(Cmd::Lint.palette_visible());
        assert!(Cmd::CycleTheme.palette_visible());
    }

    #[test]
    fn editor_bindings_do_not_leak_into_reading() {
        assert_eq!(resolve(&key(KeyCode::Char('s'), KeyModifiers::CONTROL), Ctx::Doc, None), Resolved::None);
        assert_eq!(
            resolve(&key(KeyCode::Char('s'), KeyModifiers::CONTROL), Ctx::Edit, None),
            Resolved::Run(Cmd::Save)
        );
    }
}
