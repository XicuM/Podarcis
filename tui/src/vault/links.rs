//! Markdown link scanning and resolution.
//!
//! Cross references must be relative markdown links (`[Text](../path.md)`);
//! `[[wikilinks]]` are forbidden, so we detect them in order to report them
//! rather than to follow them.

use std::path::{Component, Path, PathBuf};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LinkKind {
    /// A relative path into the checkout — the only kind that navigates.
    Relative,
    /// `http(s):`, `mailto:`, `gdrive:` — skipped by the linter, opened externally.
    External,
    /// `#section` within the current page.
    Anchor,
    /// `[[wikilink]]` — forbidden by the conventions, reported as a finding.
    Wiki,
    /// A `[^id]` citation, rendered as `[n]` — not a real link, but reusing
    /// `DocLink` gets it keyboard navigation, click handling and highlight for
    /// free. Never produced by `scan_links`, only by the reader.
    Footnote,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Link {
    pub text: String,
    pub target: String,
    pub kind: LinkKind,
    /// 0-based line within the body.
    pub line: usize,
    /// Byte offset of the link's opening bracket within its line.
    pub col: usize,
}

impl Link {
    /// Resolve a relative target against the page's directory. Returns `None`
    /// for kinds that do not point at a file, or if the path escapes the root.
    pub fn resolve(&self, page_dir: &Path, root: &Path) -> Option<PathBuf> {
        if self.kind != LinkKind::Relative {
            return None;
        }
        let bare = self.target.split(['#', '?']).next().unwrap_or(&self.target);
        if bare.is_empty() {
            return None;
        }
        let joined = if let Some(rest) = bare.strip_prefix('/') {
            root.join(rest)
        } else {
            page_dir.join(bare)
        };
        let normalized = normalize(&joined);
        normalized.starts_with(root).then_some(normalized)
    }
}

/// Lexical `..`/`.` removal. We cannot use `canonicalize` because a broken link
/// points at a path that does not exist, and that is exactly the case we need
/// to report.
pub fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

fn classify(target: &str) -> LinkKind {
    let t = target.trim();
    if t.starts_with('#') {
        LinkKind::Anchor
    } else if t.starts_with("http://")
        || t.starts_with("https://")
        || t.starts_with("mailto:")
        || t.starts_with("gdrive:")
        || t.starts_with("ftp://")
    {
        LinkKind::External
    } else {
        LinkKind::Relative
    }
}

/// Scan a markdown body for links. Fenced code blocks are skipped, matching the
/// linter's `strip_code_blocks`.
pub fn scan_links(body: &str) -> Vec<Link> {
    let mut out = Vec::new();
    let mut in_fence = false;
    for (line_no, line) in body.lines().enumerate() {
        if line.trim_start().starts_with("```") {
            in_fence = !in_fence;
            continue;
        }
        if in_fence {
            continue;
        }
        scan_line(line, line_no, &mut out);
    }
    out
}

fn scan_line(line: &str, line_no: usize, out: &mut Vec<Link>) {
    let bytes = line.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] != b'[' {
            i += 1;
            continue;
        }
        // `[[wikilink]]`
        if bytes.get(i + 1) == Some(&b'[') {
            if let Some(end) = line[i + 2..].find("]]") {
                let inner = &line[i + 2..i + 2 + end];
                out.push(Link {
                    text: inner.to_string(),
                    target: inner.to_string(),
                    kind: LinkKind::Wiki,
                    line: line_no,
                    col: i,
                });
                i = i + 2 + end + 2;
                continue;
            }
        }
        // `[^footnote]` is not a link.
        if bytes.get(i + 1) == Some(&b'^') {
            i += 1;
            continue;
        }
        let Some(text_end) = match_bracket(line, i, b'[', b']') else {
            i += 1;
            continue;
        };
        if line.as_bytes().get(text_end + 1) != Some(&b'(') {
            i = text_end + 1;
            continue;
        }
        let Some(target_end) = match_bracket(line, text_end + 1, b'(', b')') else {
            i = text_end + 1;
            continue;
        };
        let text = line[i + 1..text_end].to_string();
        let raw_target = line[text_end + 2..target_end].trim();
        // `[text](path "title")` — drop the optional title.
        let target = raw_target.split_whitespace().next().unwrap_or("").trim_matches('<').trim_matches('>');
        if !target.is_empty() {
            out.push(Link {
                text,
                target: target.to_string(),
                kind: classify(target),
                line: line_no,
                col: i,
            });
        }
        i = target_end + 1;
    }
}

/// Find the matching close for the delimiter at `start`, honouring nesting so
/// that `[a [b] c](x)` and `(a(b)c)` are handled.
fn match_bracket(line: &str, start: usize, open: u8, close: u8) -> Option<usize> {
    let bytes = line.as_bytes();
    if bytes.get(start) != Some(&open) {
        return None;
    }
    let mut depth = 0i32;
    for (offset, byte) in bytes[start..].iter().enumerate() {
        if *byte == open {
            depth += 1;
        } else if *byte == close {
            depth -= 1;
            if depth == 0 {
                return Some(start + offset);
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scans_relative_external_and_anchor_links() {
        let links = scan_links("See [A](../a.md), [B](https://x.dev) and [C](#here).\n");
        assert_eq!(links.len(), 3);
        assert_eq!(links[0].kind, LinkKind::Relative);
        assert_eq!(links[0].target, "../a.md");
        assert_eq!(links[0].text, "A");
        assert_eq!(links[1].kind, LinkKind::External);
        assert_eq!(links[2].kind, LinkKind::Anchor);
    }

    #[test]
    fn detects_forbidden_wikilinks() {
        let links = scan_links("A [[wikilink]] here.\n");
        assert_eq!(links.len(), 1);
        assert_eq!(links[0].kind, LinkKind::Wiki);
        assert_eq!(links[0].target, "wikilink");
    }

    #[test]
    fn footnotes_are_not_links() {
        assert!(scan_links("Body[^smith2024] text.\n").is_empty());
    }

    #[test]
    fn handles_nested_brackets_and_parens() {
        let links = scan_links("[**bold [x]**](../a(1).md)\n");
        assert_eq!(links.len(), 1);
        assert_eq!(links[0].target, "../a(1).md");
    }

    #[test]
    fn drops_the_optional_title() {
        let links = scan_links("[A](../a.md \"Title\")\n");
        assert_eq!(links[0].target, "../a.md");
    }

    #[test]
    fn skips_fenced_code() {
        assert!(scan_links("```\n[A](../a.md)\n```\n").is_empty());
    }

    #[test]
    fn records_line_and_column() {
        let links = scan_links("first\n  see [A](../a.md)\n");
        assert_eq!(links[0].line, 1);
        assert_eq!(links[0].col, 6);
    }

    #[test]
    fn resolves_relative_targets_and_strips_anchors() {
        let link = Link {
            text: "A".into(),
            target: "../nutrition/caffeine.md#origins".into(),
            kind: LinkKind::Relative,
            line: 0,
            col: 0,
        };
        let got = link.resolve(Path::new("/r/wiki/health/sleep"), Path::new("/r")).unwrap();
        assert_eq!(got, PathBuf::from("/r/wiki/health/nutrition/caffeine.md"));
    }

    #[test]
    fn refuses_to_escape_the_checkout() {
        let link = Link {
            text: "A".into(),
            target: "../../../../etc/passwd".into(),
            kind: LinkKind::Relative,
            line: 0,
            col: 0,
        };
        assert!(link.resolve(Path::new("/r/wiki/health"), Path::new("/r")).is_none());
    }

    #[test]
    fn external_and_anchor_links_do_not_resolve_to_files() {
        for kind in [LinkKind::External, LinkKind::Anchor, LinkKind::Wiki] {
            let link = Link { text: "x".into(), target: "y".into(), kind, line: 0, col: 0 };
            assert!(link.resolve(Path::new("/r/wiki"), Path::new("/r")).is_none());
        }
    }

    #[test]
    fn normalize_is_lexical_so_broken_paths_still_resolve() {
        assert_eq!(normalize(Path::new("/r/a/../b/./c.md")), PathBuf::from("/r/b/c.md"));
    }
}
