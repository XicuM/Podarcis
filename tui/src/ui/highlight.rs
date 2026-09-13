//! Minimal syntax highlighting — for fenced code blocks in the reader and for
//! the editor's markdown buffer.
//!
//! Hand-rolled rather than pulled from a crate: a full grammar engine
//! (syntect and friends) ships megabytes of syntax definitions and its own
//! colour themes, and the reader needs neither — code in a wiki page is a
//! handful of lines, and every colour has to come from `Theme` anyway so the
//! block matches the rest of the pane. Six token classes is all a 20-line
//! snippet can usefully distinguish.
//!
//! An unknown language highlights as nothing, which is the honest fallback:
//! plain text beats a tokenizer guessing at a grammar it does not know. The
//! markdown tokenizer applies the same rule line by line: a construct it does
//! not recognise, or one still half-typed, stays body text rather than a wrong
//! guess.

use ratatui::style::{Modifier, Style};

use crate::theme::Theme;

/// What a run of code text is, semantically. `Plain` covers whitespace,
/// operators, and anything the tokenizer chose not to claim.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tok {
    Plain,
    Keyword,
    Str,
    Num,
    Comment,
    /// A name being called or defined, and type-ish names.
    Name,
    /// Mapping keys in the key/value languages — YAML, TOML, JSON.
    Key,
}

#[derive(Clone, Copy)]
struct Syntax {
    keywords: &'static [&'static str],
    line_comment: &'static [&'static str],
    block_comment: Option<(&'static str, &'static str)>,
    /// Quote characters that open a string.
    quotes: &'static str,
    /// `key: value` / `key = value` lines get their key highlighted.
    keyed: bool,
}

const RUST: &[&str] = &[
    "as", "async", "await", "break", "const", "continue", "crate", "dyn", "else", "enum", "extern",
    "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub",
    "ref", "return", "self", "Self", "static", "struct", "super", "trait", "true", "type", "unsafe",
    "use", "where", "while",
];

const PYTHON: &[&str] = &[
    "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif",
    "else", "except", "False", "finally", "for", "from", "global", "if", "import", "in", "is",
    "lambda", "None", "nonlocal", "not", "or", "pass", "raise", "return", "True", "try", "while",
    "with", "yield",
];

const JS: &[&str] = &[
    "async", "await", "break", "case", "catch", "class", "const", "continue", "default", "delete",
    "do", "else", "export", "extends", "false", "finally", "for", "from", "function", "if",
    "import", "in", "instanceof", "interface", "let", "new", "null", "of", "return", "static",
    "switch", "this", "throw", "true", "try", "type", "typeof", "undefined", "var", "void", "while",
    "yield",
];

const SHELL: &[&str] = &[
    "case", "do", "done", "elif", "else", "esac", "export", "fi", "for", "function", "if", "in",
    "local", "return", "then", "until", "while",
];

const C: &[&str] = &[
    "auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else", "enum",
    "extern", "float", "for", "goto", "if", "inline", "int", "long", "return", "short", "signed",
    "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned", "void", "volatile",
    "while",
];

const GO: &[&str] = &[
    "break", "case", "chan", "const", "continue", "default", "defer", "else", "fallthrough", "for",
    "func", "go", "goto", "if", "import", "interface", "map", "package", "range", "return",
    "select", "struct", "switch", "type", "var",
];

const SQL: &[&str] = &[
    "and", "as", "asc", "by", "create", "delete", "desc", "distinct", "drop", "from", "group",
    "having", "insert", "inner", "into", "join", "left", "limit", "not", "null", "on", "or",
    "order", "select", "set", "table", "union", "update", "values", "where", "with",
];

const LITERALS: &[&str] = &["true", "false", "null", "yes", "no", "none"];

/// The syntax for a fence info string, or `None` when the language is one we
/// do not claim to know.
fn syntax_for(lang: &str) -> Option<Syntax> {
    // `python {highlight=1}` and `bash title="x"` are still python and bash.
    let lang = lang.split_whitespace().next().unwrap_or("").to_ascii_lowercase();
    let c_like = |keywords| Syntax {
        keywords,
        line_comment: &["//"],
        block_comment: Some(("/*", "*/")),
        quotes: "\"'",
        keyed: false,
    };
    Some(match lang.as_str() {
        "rust" | "rs" => Syntax { quotes: "\"", ..c_like(RUST) },
        "python" | "py" => Syntax {
            keywords: PYTHON,
            line_comment: &["#"],
            block_comment: None,
            quotes: "\"'",
            keyed: false,
        },
        "javascript" | "js" | "jsx" | "typescript" | "ts" | "tsx" => Syntax {
            quotes: "\"'`",
            ..c_like(JS)
        },
        "c" | "cpp" | "c++" | "h" | "hpp" | "java" | "cs" => c_like(C),
        "go" => c_like(GO),
        "bash" | "sh" | "zsh" | "shell" | "console" => Syntax {
            keywords: SHELL,
            line_comment: &["#"],
            block_comment: None,
            quotes: "\"'",
            keyed: false,
        },
        "sql" => Syntax {
            keywords: SQL,
            line_comment: &["--"],
            block_comment: Some(("/*", "*/")),
            quotes: "'\"",
            keyed: false,
        },
        "json" => Syntax {
            keywords: LITERALS,
            line_comment: &[],
            block_comment: None,
            quotes: "\"",
            keyed: true,
        },
        "yaml" | "yml" | "toml" | "ini" | "conf" => Syntax {
            keywords: LITERALS,
            line_comment: &["#"],
            block_comment: None,
            quotes: "\"'",
            keyed: true,
        },
        _ => return None,
    })
}

/// Tokenizes one code block, carrying the state a block comment or an unclosed
/// string needs to survive across lines.
pub struct Highlighter {
    syntax: Option<Syntax>,
    in_block_comment: bool,
}

impl Highlighter {
    pub fn new(lang: &str) -> Self {
        Self { syntax: syntax_for(lang), in_block_comment: false }
    }

    /// Split one line into styled runs. The runs always reassemble to the
    /// input exactly, so the caller can measure and clip them like any text.
    pub fn line(&mut self, line: &str) -> Vec<(String, Tok)> {
        let Some(syntax) = self.syntax else {
            return vec![(line.to_string(), Tok::Plain)];
        };
        let chars: Vec<char> = line.chars().collect();
        let mut out: Vec<(String, Tok)> = Vec::new();
        let mut i = 0;

        if self.in_block_comment {
            let (_, close) = syntax.block_comment.unwrap();
            match find_from(&chars, 0, close) {
                Some(end) => {
                    push(&mut out, &chars[..end + close.chars().count()], Tok::Comment);
                    i = end + close.chars().count();
                    self.in_block_comment = false;
                }
                None => {
                    push(&mut out, &chars, Tok::Comment);
                    return out;
                }
            }
        }

        if syntax.keyed {
            if let Some(end) = key_end(&chars, i) {
                push(&mut out, &chars[i..end], Tok::Key);
                i = end;
            }
        }

        while i < chars.len() {
            let ch = chars[i];

            if let Some(marker) = syntax.line_comment.iter().find(|m| starts_with(&chars, i, m)) {
                // A `#` inside a shell parameter (`${#x}`) is not a comment,
                // but a `#` that opens the rest of the line is — close enough
                // for a reader, and never wrong for the common case.
                let _ = marker;
                push(&mut out, &chars[i..], Tok::Comment);
                return out;
            }

            if let Some((open, close)) = syntax.block_comment {
                if starts_with(&chars, i, open) {
                    match find_from(&chars, i + open.chars().count(), close) {
                        Some(end) => {
                            let stop = end + close.chars().count();
                            push(&mut out, &chars[i..stop], Tok::Comment);
                            i = stop;
                        }
                        None => {
                            push(&mut out, &chars[i..], Tok::Comment);
                            self.in_block_comment = true;
                            return out;
                        }
                    }
                    continue;
                }
            }

            if syntax.quotes.contains(ch) {
                let end = string_end(&chars, i, ch);
                push(&mut out, &chars[i..end], Tok::Str);
                i = end;
                continue;
            }

            if ch.is_ascii_digit() && !prev_is_word(&chars, i) {
                let mut end = i;
                while end < chars.len()
                    && (chars[end].is_ascii_alphanumeric() || chars[end] == '.' || chars[end] == '_')
                {
                    end += 1;
                }
                push(&mut out, &chars[i..end], Tok::Num);
                i = end;
                continue;
            }

            if ch.is_alphabetic() || ch == '_' {
                let mut end = i;
                while end < chars.len() && (chars[end].is_alphanumeric() || chars[end] == '_') {
                    end += 1;
                }
                let word: String = chars[i..end].iter().collect();
                let kind = if syntax.keywords.iter().any(|k| *k == word)
                    || (syntax.keyed && syntax.keywords.iter().any(|k| k.eq_ignore_ascii_case(&word)))
                {
                    Tok::Keyword
                } else if chars.get(end) == Some(&'(') || word.starts_with(char::is_uppercase) {
                    Tok::Name
                } else {
                    Tok::Plain
                };
                out.push((word, kind));
                i = end;
                continue;
            }

            let mut end = i;
            while end < chars.len() {
                let c = chars[end];
                if c.is_alphanumeric()
                    || c == '_'
                    || syntax.quotes.contains(c)
                    || syntax.line_comment.iter().any(|m| starts_with(&chars, end, m))
                    || syntax.block_comment.is_some_and(|(o, _)| starts_with(&chars, end, o))
                {
                    break;
                }
                end += 1;
            }
            push(&mut out, &chars[i..end.max(i + 1)], Tok::Plain);
            i = end.max(i + 1);
        }

        out
    }
}

fn push(out: &mut Vec<(String, Tok)>, chars: &[char], tok: Tok) {
    if chars.is_empty() {
        return;
    }
    let text: String = chars.iter().collect();
    match out.last_mut() {
        Some((prev, prev_tok)) if *prev_tok == tok => prev.push_str(&text),
        _ => out.push((text, tok)),
    }
}

fn starts_with(chars: &[char], at: usize, needle: &str) -> bool {
    needle.chars().enumerate().all(|(k, c)| chars.get(at + k) == Some(&c))
}

fn find_from(chars: &[char], from: usize, needle: &str) -> Option<usize> {
    (from..chars.len()).find(|i| starts_with(chars, *i, needle))
}

fn prev_is_word(chars: &[char], at: usize) -> bool {
    at > 0 && (chars[at - 1].is_alphanumeric() || chars[at - 1] == '_')
}

/// Index one past the closing quote, or the end of the line when the string
/// never closes (a line-spanning string reads as a string to its line's end).
fn string_end(chars: &[char], start: usize, quote: char) -> usize {
    let mut i = start + 1;
    while i < chars.len() {
        if chars[i] == '\\' {
            i += 2;
            continue;
        }
        if chars[i] == quote {
            return i + 1;
        }
        i += 1;
    }
    chars.len()
}

/// Index one past the `:` or `=` of a leading `key:` / `key =`, if the line
/// opens with one. Bare indentation and list dashes are part of the key run so
/// the whole lead-in is coloured together.
fn key_end(chars: &[char], from: usize) -> Option<usize> {
    let mut i = from;
    while i < chars.len() && (chars[i] == ' ' || chars[i] == '\t' || chars[i] == '-') {
        i += 1;
    }
    let start = i;
    if i < chars.len() && (chars[i] == '"' || chars[i] == '\'') {
        i = string_end(chars, i, chars[i]);
    } else {
        while i < chars.len() && (chars[i].is_alphanumeric() || "_-.".contains(chars[i])) {
            i += 1;
        }
    }
    if i == start {
        return None;
    }
    let mut j = i;
    while j < chars.len() && chars[j] == ' ' {
        j += 1;
    }
    match chars.get(j) {
        Some(':') | Some('=') => Some(j + 1),
        _ => None,
    }
}

/// The `Theme` colours a code token is drawn in. Shared by the reader's code
/// bands and the editor's fenced-block lines so the two ports can't drift
/// apart on the same token class.
pub fn token_style(theme: &Theme, tok: Tok) -> Style {
    let s = Style::default();
    match tok {
        Tok::Plain => s.fg(theme.text),
        Tok::Keyword => s.fg(theme.accent).add_modifier(Modifier::BOLD),
        Tok::Str => s.fg(theme.ok),
        Tok::Num => s.fg(theme.warn),
        Tok::Comment => s.fg(theme.faint).add_modifier(Modifier::ITALIC),
        Tok::Name => s.fg(theme.link),
        Tok::Key => s.fg(theme.literal),
    }
}

// ---------------------------------------------------------------- markdown

/// One styled run inside an editor line: char indices into the line, end
/// exclusive, ascending. Runs never replace text — they layer colour over it —
/// and a run whose style equals the editor's base style is dropped, because a
/// mark that draws nothing is only a lookup cost at render time.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Run {
    pub start: usize,
    pub end: usize,
    pub style: Style,
}

impl Run {
    fn new(start: usize, end: usize, style: Style) -> Self {
        Self { start, end, style }
    }
}

fn punct(theme: &Theme) -> Style {
    Style::default().fg(theme.faint)
}

/// Tokenize a markdown buffer for the editor: per line, the runs to layer
/// over body text. The state that has to survive across lines is honoured —
/// a leading OKF frontmatter block and fenced code, whose interior reuses the
/// code highlighter above — everything else is decided line by line, so a
/// half-typed construct reads as plain text rather than as a wrong guess.
pub fn markdown_runs(text: &str, theme: &Theme, base: &Style) -> Vec<Vec<Run>> {
    let lines: Vec<&str> = text.split('\n').collect();
    let mut out = Vec::with_capacity(lines.len());
    let mut code: Option<(char, Highlighter)> = None;
    let mut in_fm = false;

    for (idx, line) in lines.iter().enumerate() {
        let chars: Vec<char> = line.chars().collect();

        if let Some((open, hl)) = code.as_mut() {
            if fence_run(&chars).is_some_and(|(_, _, c, _)| c == *open) {
                code = None;
                out.push(vec![Run::new(0, chars.len(), punct(theme))]);
                continue;
            }
            let text: String = chars.iter().collect();
            let mut runs = Vec::new();
            let mut pos = 0;
            for (part, tok) in hl.line(&text) {
                // Plain code tokens match the base style, so they drop out and
                // only the words the grammar claims get coloured.
                let style = if tok == Tok::Plain { *base } else { token_style(theme, tok) };
                let end = pos + part.chars().count();
                runs.push(Run::new(pos, end, style));
                pos = end;
            }
            out.push(finish(runs, base));
            continue;
        }

        // A leading `---` line opens OKF frontmatter; the next `---` closes it.
        if !in_fm && idx == 0 && chars == ['-', '-', '-'] {
            in_fm = true;
            out.push(vec![Run::new(0, 3, punct(theme))]);
            continue;
        }

        if in_fm {
            if line.trim() == "---" {
                in_fm = false;
                out.push(vec![Run::new(0, chars.len(), punct(theme))]);
            } else {
                out.push(finish(frontmatter_runs(theme, &chars), base));
            }
            continue;
        }

        if let Some((ind, _, _, after)) = fence_run(&chars) {
            // The fence itself is syntax; the language it names is a hint.
            out.push(vec![Run::new(0, chars.len(), punct(theme))]);
            let info: String = chars[after..].iter().collect();
            let lang = info.split_whitespace().next().unwrap_or("");
            code = Some((chars[ind], Highlighter::new(lang)));
            continue;
        }

        let mut runs = Vec::new();
        prose_runs(theme, &chars, &mut runs);
        out.push(finish(runs, base));
    }
    out
}

/// The whole frontmatter block reads as metadata: keys keep their YAML
/// colour (they are what you reach for), values drop to faint.
fn frontmatter_runs(theme: &Theme, chars: &[char]) -> Vec<Run> {
    let text: String = chars.iter().collect();
    let mut runs = Vec::new();
    let mut pos = 0;
    for (part, tok) in Highlighter::new("yaml").line(&text) {
        let style = match tok {
            Tok::Key => Style::default().fg(theme.literal),
            _ => Style::default().fg(theme.faint),
        };
        let end = pos + part.chars().count();
        runs.push(Run::new(pos, end, style));
        pos = end;
    }
    runs
}

fn prose_runs(theme: &Theme, chars: &[char], out: &mut Vec<Run>) {
    let ind = indent_count(chars);

    if is_hr(chars) {
        out.push(Run::new(0, chars.len(), punct(theme)));
        return;
    }
    if let Some((level, after)) = heading_at(chars, ind) {
        out.push(Run::new(ind, after, punct(theme)));
        // Skip the single space between the markers and the title, so the
        // title never carries a stray run over a leading blank.
        let start = if chars.get(after) == Some(&' ') { after + 1 } else { after };
        if start < chars.len() {
            out.push(Run::new(start, chars.len(), theme.heading(level)));
        }
        return;
    }
    if ind <= 3 && chars.get(ind) == Some(&'>') {
        let accent = Style::default().fg(theme.accent);
        let mut j = ind;
        while j < chars.len() && chars[j] == '>' {
            out.push(Run::new(j, j + 1, accent));
            j += 1;
            if chars.get(j) == Some(&' ') {
                j += 1;
            }
        }
        scan_inline(theme, chars, j, out);
        return;
    }
    if let Some(after) = list_at(theme, chars, ind, out) {
        scan_inline(theme, chars, after, out);
        return;
    }
    scan_inline(theme, chars, 0, out);
}

fn indent_count(chars: &[char]) -> usize {
    chars.iter().take_while(|c| **c == ' ' || **c == '\t').count()
}

/// An ATX heading's `#` run, verified the CommonMark way: 1–6 hashes after
/// no more than three spaces, then space or end of line. Returns the heading
/// level and the index one past the hashes.
fn heading_at(chars: &[char], ind: usize) -> Option<(u8, usize)> {
    if ind > 3 || chars.get(ind) != Some(&'#') {
        return None;
    }
    let n = chars[ind..].iter().take_while(|&&c| c == '#').count();
    if !(1..=6).contains(&n) {
        return None;
    }
    match chars.get(ind + n) {
        None | Some(' ') | Some('\t') => Some((n as u8, ind + n)),
        _ => None,
    }
}

fn is_hr(chars: &[char]) -> bool {
    let ind = indent_count(chars);
    if ind > 3 {
        return false;
    }
    let Some(first) = chars.get(ind).copied() else { return false };
    if !matches!(first, '-' | '*' | '_') {
        return false;
    }
    if chars[ind..].iter().filter(|&&c| c == first).count() < 3 {
        return false;
    }
    chars[ind..].iter().all(|&c| c == first || c.is_whitespace())
}

/// A fenced block: three or more backticks or tildes at up to three spaces of
/// indent. Returns (indent, marker count, marker char, one past the markers).
fn fence_run(chars: &[char]) -> Option<(usize, usize, char, usize)> {
    let ind = indent_count(chars);
    if ind > 3 {
        return None;
    }
    let c = *chars.get(ind)?;
    if c != '`' && c != '~' {
        return None;
    }
    let n = chars[ind..].iter().take_while(|&&x| x == c).count();
    if n < 3 {
        return None;
    }
    Some((ind, n, c, ind + n))
}

/// Style a list item's marker in place — bullet, number, or task box — and
/// return where the item's text begins.
fn list_at(theme: &Theme, chars: &[char], ind: usize, out: &mut Vec<Run>) -> Option<usize> {
    if ind > 3 {
        return None;
    }
    let accent = Style::default().fg(theme.accent);
    match chars.get(ind).copied() {
        // `- [ ]` / `- [x]` task items read as boxes: the dash and brackets
        // are syntax, the checkmark is the state.
        Some('-') if chars.get(ind + 2) == Some(&'[')
            && matches!(chars.get(ind + 3), Some(' ') | Some('x'))
            && chars.get(ind + 4) == Some(&']') =>
        {
            let done = chars.get(ind + 3) == Some(&'x');
            out.push(Run::new(ind, ind + 2, punct(theme)));
            out.push(Run::new(ind + 2, ind + 3, punct(theme)));
            out.push(Run::new(
                ind + 3,
                ind + 4,
                Style::default()
                    .fg(if done { theme.ok } else { theme.subtext })
                    .add_modifier(Modifier::BOLD),
            ));
            out.push(Run::new(ind + 4, ind + 5, punct(theme)));
            Some(if chars.get(ind + 5) == Some(&' ') { ind + 6 } else { ind + 5 })
        }
        Some('-' | '*' | '+') if chars.get(ind + 1).is_some_and(|&c| c.is_whitespace()) => {
            out.push(Run::new(ind, ind + 1, accent));
            Some(ind + 2)
        }
        Some(c) if c.is_ascii_digit() => {
            let mut j = ind;
            while j < chars.len() && chars[j].is_ascii_digit() {
                j += 1;
            }
            if j - ind > 9 || !matches!(chars.get(j), Some('.') | Some(')')) {
                return None;
            }
            out.push(Run::new(ind, j + 1, accent));
            Some(if chars.get(j + 1) == Some(&' ') { j + 2 } else { j + 1 })
        }
        _ => None,
    }
}

/// Inline constructs — code spans, links, footnotes, images, emphasis —
/// scanned left to right. Anything unrecognized is left for the base style.
fn scan_inline(theme: &Theme, chars: &[char], start: usize, out: &mut Vec<Run>) {
    let p = punct(theme);
    let mut i = start;
    while i < chars.len() {
        match chars[i] {
            '\\' if i + 1 < chars.len() => i += 2,
            '`' => {
                if let Some(d) = chars[i + 1..].iter().position(|&c| c == '`') {
                    let j = i + 1 + d;
                    out.push(Run::new(i, i + 1, p));
                    if j > i + 1 {
                        out.push(Run::new(
                            i + 1,
                            j,
                            Style::default().fg(theme.literal).bg(theme.surface),
                        ));
                    }
                    out.push(Run::new(j, j + 1, p));
                    i = j + 1;
                } else {
                    i += 1;
                }
            }
            '!' if chars.get(i + 1) == Some(&'[') => {
                match image_runs(theme, chars, i, p, out) {
                    Some(next) => i = next,
                    None => i += 2,
                }
            }
            '[' => {
                if chars.get(i + 1) == Some(&'^') {
                    if let Some(j) = chars[i + 1..].iter().position(|&c| c == ']').map(|d| i + 1 + d) {
                        if j > i + 1 {
                            out.push(Run::new(
                                i,
                                j + 1,
                                Style::default().fg(theme.literal).add_modifier(Modifier::DIM),
                            ));
                            i = j + 1;
                            continue;
                        }
                    }
                    i += 1;
                } else {
                    match link_runs(chars, i, p, out, theme) {
                        Some(next) => i = next,
                        None => i += 1,
                    }
                }
            }
            '*' | '_' | '~' => {
                if let Some((next, mark, inner)) = emphasis(theme, chars, i) {
                    out.push(Run::new(i, i + mark, p));
                    out.push(Run::new(i + mark, next - mark, inner));
                    out.push(Run::new(next - mark, next, p));
                    i = next;
                } else {
                    i += 1;
                }
            }
            _ => i += 1,
        }
    }
}

/// A `[label](target)` link, or `None` when the brackets are ordinary text.
/// The label is styled as a link, the target is faint, and the syntax
/// characters around both stay dim.
fn link_runs(chars: &[char], i: usize, p: Style, out: &mut Vec<Run>, theme: &Theme) -> Option<usize> {
    let label_end = chars[i + 1..].iter().position(|&c| c == ']').map(|d| i + 1 + d)?;
    if chars.get(label_end + 1) != Some(&'(') {
        return None;
    }
    let close = chars[label_end + 2..]
        .iter()
        .position(|&c| c == ')')
        .map(|d| label_end + 2 + d)?;
    out.push(Run::new(i, i + 1, p));
    if label_end > i + 1 {
        out.push(Run::new(
            i + 1,
            label_end,
            Style::default().fg(theme.link).add_modifier(Modifier::UNDERLINED),
        ));
    }
    out.push(Run::new(label_end, label_end + 2, p));
    if close > label_end + 2 {
        out.push(Run::new(label_end + 2, close, Style::default().fg(theme.faint)));
    }
    out.push(Run::new(close, close + 1, p));
    Some(close + 1)
}

/// An `![alt](target)` image — same anatomy as a link, one `!` wider.
fn image_runs(theme: &Theme, chars: &[char], bang: usize, p: Style, out: &mut Vec<Run>) -> Option<usize> {
    let label_end = chars[bang + 2..].iter().position(|&c| c == ']').map(|d| bang + 2 + d)?;
    let close = chars[label_end + 2..].iter().position(|&c| c == ')').map(|d| label_end + 2 + d)?;
    out.push(Run::new(bang, bang + 2, p));
    if label_end > bang + 2 {
        out.push(Run::new(bang + 2, label_end, Style::default().fg(theme.link).add_modifier(Modifier::UNDERLINED)));
    }
    out.push(Run::new(label_end, label_end + 2, p));
    if close > label_end + 2 {
        out.push(Run::new(label_end + 2, close, Style::default().fg(theme.faint)));
    }
    out.push(Run::new(close, close + 1, p));
    Some(close + 1)
}

/// Emphasis and strikethrough: an opening run of markers, a styled interior,
/// and the matching closing run. `_` keeps the CommonMark rule that it may
/// not open or close between word characters; `*` and `~` may. Returns (one
/// past the closing markers, width of a marker run, style of the interior).
fn emphasis(theme: &Theme, chars: &[char], i: usize) -> Option<(usize, usize, Style)> {
    let c = chars[i];
    let mark = if chars.get(i + 1) == Some(&c) { 2 } else { 1 };
    if c == '~' && mark != 2 {
        return None;
    }
    let inner = if c == '~' {
        Style::default().fg(theme.text).add_modifier(Modifier::CROSSED_OUT)
    } else if mark == 2 {
        theme.bold()
    } else {
        Style::default().fg(theme.text).add_modifier(Modifier::ITALIC)
    };
    let needle = vec![c; mark];
    let d = chars[i + mark..].windows(mark).position(|w| w == needle.as_slice())?;
    let close = i + mark + d;
    if close == i + mark {
        return None;
    }
    if c == '_' {
        let opens = i == 0 || !chars[i - 1].is_alphanumeric();
        let closes = chars.get(close + mark).is_none_or(|c| !c.is_alphanumeric());
        if !opens || !closes {
            return None;
        }
    }
    Some((close + mark, mark, inner))
}

/// Merge runs that end where the next begins with the same style, and drop
/// the ones no different from the editor's base — they would colour nothing.
fn finish(mut runs: Vec<Run>, base: &Style) -> Vec<Run> {
    runs.sort_by_key(|r| r.start);
    let mut out: Vec<Run> = Vec::with_capacity(runs.len());
    for r in runs {
        if r.end <= r.start || r.style == *base {
            continue;
        }
        match out.last_mut() {
            Some(prev) if prev.end == r.start && prev.style == r.style => prev.end = r.end,
            _ => out.push(r),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn toks(lang: &str, line: &str) -> Vec<(String, Tok)> {
        Highlighter::new(lang).line(line)
    }

    fn kinds(lang: &str, line: &str, tok: Tok) -> Vec<String> {
        toks(lang, line).into_iter().filter(|(_, t)| *t == tok).map(|(s, _)| s).collect()
    }

    #[test]
    fn runs_always_reassemble_to_the_input() {
        for (lang, line) in [
            ("rust", "let x = \"hi\"; // done"),
            ("python", "def f(a=1):  # note"),
            ("yaml", "  key: \"value\" # c"),
            ("nonsense", "@@@ whatever ###"),
            ("sql", "select * from t where a = 'b'"),
        ] {
            let joined: String = toks(lang, line).into_iter().map(|(s, _)| s).collect();
            assert_eq!(joined, line, "{lang}");
        }
    }

    #[test]
    fn an_unknown_language_is_left_plain() {
        assert_eq!(toks("brainfuck", "fn let const"), vec![("fn let const".into(), Tok::Plain)]);
    }

    #[test]
    fn keywords_strings_numbers_and_comments_are_separated() {
        assert_eq!(kinds("rust", "let n = 42; // why", Tok::Keyword), vec!["let"]);
        assert_eq!(kinds("rust", "let n = 42; // why", Tok::Num), vec!["42"]);
        assert_eq!(kinds("rust", "let n = 42; // why", Tok::Comment), vec!["// why"]);
        assert_eq!(kinds("python", "s = 'a # b'  # real", Tok::Str), vec!["'a # b'"]);
        assert_eq!(kinds("python", "s = 'a # b'  # real", Tok::Comment), vec!["# real"]);
    }

    #[test]
    fn a_keyword_inside_a_longer_word_is_not_a_keyword() {
        assert!(kinds("rust", "letter = deconstruct(iffy)", Tok::Keyword).is_empty());
    }

    #[test]
    fn a_block_comment_spans_lines() {
        let mut h = Highlighter::new("rust");
        assert_eq!(h.line("/* start").last().unwrap().1, Tok::Comment);
        assert_eq!(h.line("still in it").last().unwrap().1, Tok::Comment);
        let closing = h.line("end */ let x");
        assert_eq!(closing[0].1, Tok::Comment);
        assert!(closing.iter().any(|(s, t)| s == "let" && *t == Tok::Keyword));
    }

    #[test]
    fn mapping_keys_are_highlighted_in_keyed_languages() {
        assert_eq!(kinds("yaml", "title: Freelancing", Tok::Key), vec!["title:"]);
        assert_eq!(kinds("yaml", "  - id: x", Tok::Key), vec!["  - id:"]);
        assert_eq!(kinds("json", "  \"a\": 1", Tok::Key), vec!["  \"a\":"]);
        assert!(kinds("yaml", "just a sentence", Tok::Key).is_empty());
    }

    #[test]
    fn calls_and_types_read_as_names() {
        assert!(kinds("python", "render(body)", Tok::Name).contains(&"render".to_string()));
        assert!(kinds("rust", "let d: Doc = x;", Tok::Name).contains(&"Doc".to_string()));
    }

    #[test]
    fn an_unterminated_string_stops_at_the_line_end() {
        let out = toks("python", "s = 'never closed");
        assert_eq!(out.last().unwrap(), &("'never closed".to_string(), Tok::Str));
    }

    #[test]
    fn an_empty_line_yields_nothing_and_does_not_panic() {
        assert!(toks("rust", "").is_empty());
        assert!(toks("yaml", "").is_empty());
    }

    fn theme() -> Theme {
        Theme::default()
    }

    fn mark(text: &str) -> Vec<Vec<Run>> {
        let t = theme();
        let base = Style::default().fg(t.text).bg(t.bg);
        markdown_runs(text, &t, &base)
    }

    fn mark_runs(text: &str) -> Vec<Run> {
        mark(text).into_iter().flatten().collect()
    }

    fn slice(line: &str, run: &Run) -> String {
        line.chars().skip(run.start).take(run.end - run.start).collect()
    }

    fn find(runs: &[Run], f: impl Fn(&Run) -> bool) -> Option<&Run> {
        runs.iter().find(|r| f(r))
    }

    #[test]
    fn plain_prose_yields_no_marks() {
        assert_eq!(mark("Just some words.\nSecond line."), vec![vec![], vec![]]);
    }

    #[test]
    fn atx_heading_colors_markers_faint_and_title_by_level() {
        let p = punct(&theme());
        assert_eq!(mark("# Top"), vec![vec![Run::new(0, 1, p), Run::new(2, 5, theme().heading(1))]]);
        assert_eq!(mark("## Deep"), vec![vec![Run::new(0, 2, p), Run::new(3, 7, theme().heading(2))]]);
    }

    #[test]
    fn a_hash_in_prose_is_not_a_heading() {
        assert_eq!(mark("a # not heading"), vec![vec![]]);
        assert_eq!(mark("####### seven"), vec![vec![]]);
    }

    #[test]
    fn bold_and_italic_use_the_theme() {
        let text = "say **loud** *soft*";
        let p = punct(&theme());
        let it = Style::default().fg(theme().text).add_modifier(Modifier::ITALIC);
        assert_eq!(
            mark(text),
            vec![vec![
                Run::new(4, 6, p),
                Run::new(6, 10, theme().bold()),
                Run::new(10, 12, p),
                Run::new(13, 14, p),
                Run::new(14, 18, it),
                Run::new(18, 19, p),
            ]]
        );
    }

    #[test]
    fn underscore_emphasis_needs_word_boundaries() {
        assert_eq!(mark_runs("an_identifier_stays"), vec![]);
        assert!(find(&mark_runs("a _ok_ z"), |r| r.style.add_modifier.contains(Modifier::ITALIC)).is_some());
    }

    #[test]
    fn links_and_images_are_structural() {
        let text = "see [the wiki](../guide.md) ![img](x.png)";
        let runs = mark_runs(text);
        let label = find(&runs, |r| r.style.add_modifier.contains(Modifier::UNDERLINED)).unwrap();
        assert_eq!(slice(text, label), "the wiki");
        // the link's target and its brackets merge into one faint span
        assert!(runs
            .iter()
            .any(|r| r.style.fg == Some(theme().faint) && slice(text, r).contains("../guide.md")));
        // same anatomy for the image, one `!` wider
        assert!(runs
            .iter()
            .any(|r| r.style.fg == Some(theme().faint) && slice(text, r).contains("x.png")));
    }

    #[test]
    fn code_spans_use_literal_on_surface() {
        let text = "run `cargo build` now";
        let runs = mark_runs(text);
        let span = find(&runs, |r| r.style.bg == Some(theme().surface)).unwrap();
        assert_eq!(slice(text, span), "cargo build");
        // the backtick pair reads as syntax, not as literal
        assert!(runs.iter().any(|r| r.style == punct(&theme()) && r.start > 3 && r.end < 19));
    }

    #[test]
    fn a_code_span_starving_of_a_closer_is_left_plain() {
        assert_eq!(mark_runs("`unclosed"), vec![]);
    }

    #[test]
    fn task_items_mark_the_box_and_the_check() {
        let runs = mark("- [ ] todo\n- [x] done");
        assert!(runs[0].iter().any(|r| r.style.fg == Some(theme().subtext)));
        assert!(runs[1].iter().any(|r| r.style.fg == Some(theme().ok)));
    }

    #[test]
    fn footnotes_dim_the_whole_label() {
        let text = "[^jones2024] argues";
        let runs = mark_runs(text);
        let note = find(&runs, |r| {
            r.style.fg == Some(theme().literal) && r.style.add_modifier.contains(Modifier::DIM)
        })
        .unwrap();
        assert_eq!(slice(text, note), "[^jones2024]");
    }

    #[test]
    fn frontmatter_is_dim_but_keys_keep_their_colour() {
        let p = punct(&theme());
        let runs = mark("---\ntitle: A\nsources: []\n---\nBody");
        assert_eq!(runs[0], vec![Run::new(0, 3, p)]);
        assert!(runs[1].iter().any(|r| r.style.fg == Some(theme().literal)));
        assert_eq!(runs[3], vec![Run::new(0, 3, p)]);
        assert_eq!(runs[4], vec![]);
    }

    #[test]
    fn fenced_code_switches_to_the_highlighter() {
        let p = punct(&theme());
        let runs = mark("```rust\nlet x = 1;\n```\nplain");
        // the fence and its language hint are one faint span
        assert_eq!(runs[0], vec![Run::new(0, 7, p)]);
        assert!(runs[1].iter().any(|r| r.style.fg == Some(theme().accent)));
        assert_eq!(runs[2], vec![Run::new(0, 3, p)]);
        assert_eq!(runs[3], vec![]);
    }

    #[test]
    fn a_fence_that_never_closes_stays_code() {
        let runs = mark("```rust\nlet x");
        assert!(runs[1].iter().any(|r| r.style.fg == Some(theme().accent)));
        assert_eq!(runs.len(), 2);
    }

    #[test]
    fn thematic_breaks_and_quotes_read_as_syntax() {
        let p = punct(&theme());
        assert_eq!(mark_runs("---"), vec![Run::new(0, 3, p)]);
        assert_eq!(mark_runs("***"), vec![Run::new(0, 3, p)]);
        let runs = mark_runs("> quote");
        assert_eq!(runs[0], Run::new(0, 1, Style::default().fg(theme().accent)));
    }

    #[test]
    fn list_markers_are_syntax() {
        let accent = Style::default().fg(theme().accent);
        let runs = mark_runs("1. first\n99) more");
        assert_eq!(runs[0], Run::new(0, 2, accent));
        assert_eq!(runs[1], Run::new(0, 3, accent));
    }

    #[test]
    fn adjacent_equal_runs_merge_and_base_marks_drop() {
        // `word` in a heading: the heading title is one run covering it.
        let runs = mark("# word");
        assert_eq!(runs[0].len(), 2);
        // Plain code token styled as base is dropped entirely.
        let runs = mark("```rust\nx = 1\n```");
        let code: &[Run] = &runs[1];
        assert!(code.iter().all(|r| r.style.fg != Some(theme().text)));
    }
}
