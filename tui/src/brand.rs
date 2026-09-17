//! The product's identity, in one place.
//!
//! The CLI banner (`.podarcis/banner.py`) and this front end draw the same
//! mark, so the logo lives in exactly one file and is compiled in from there
//! rather than transcribed — a second copy of a drawing is a copy that drifts
//! the first time someone nudges a pixel.

/// The wall lizard. Block art, every line single-width, at most 22 columns.
pub const LOGO: &str = include_str!("../../.podarcis/logo.txt");

/// The wordmark and the tagline, in that order, one per line. Shared with the
/// CLI banner for the same reason the drawing is: two copies of a brand string
/// are two things to forget to change together.
const STRINGS: &str = include_str!("../../.podarcis/brand.txt");

/// The wordmark. Lowercase everywhere else in the chrome; capitalised here
/// because this is the one place the product says its own name.
pub fn name() -> &'static str {
    STRINGS.lines().next().unwrap_or("Podarcis").trim()
}

/// What the product is for, in three words.
pub fn tagline() -> &'static str {
    STRINGS.lines().nth(1).unwrap_or("").trim()
}

/// The logo's lines, trailing blanks trimmed.
pub fn logo_lines() -> Vec<&'static str> {
    LOGO.lines().map(str::trim_end).collect()
}

/// Is this glyph part of the animal, rather than the frame it sits against?
///
/// The mark is drawn from block elements; every other glyph in it — the
/// `╭─╯`, `╰─╮` and `┴` strokes — is decoration around the body, drawn
/// quieter so the lizard reads first.
///
/// This was briefly a positional rule instead, because an earlier version of
/// the art put box-drawing strokes *inside* the body (a `^` head, a `╰──`
/// snout) where the same glyphs meant the opposite thing. The current art has
/// none, so the rule is back to a property of the character. If body strokes
/// ever return, they need a third ink rather than this predicate: a hairline
/// tinted with the accent reads as a thread next to a solid block, so such a
/// cell has to be *filled* with the accent and the glyph carved out of it.
pub fn is_body(c: char) -> bool {
    ('\u{2580}'..='\u{259f}').contains(&c)
}

/// Width of the widest logo line, in terminal cells.
pub fn logo_width() -> usize {
    use unicode_width::UnicodeWidthStr;
    logo_lines().iter().map(|l| l.width()).max().unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use unicode_width::UnicodeWidthStr;

    #[test]
    fn the_brand_strings_are_both_present() {
        assert_eq!(name(), "Podarcis");
        assert!(!tagline().is_empty(), "the tagline is what the splash promises");
    }

    /// The animal is the block elements and nothing else; the strokes around
    /// it are frame. Whitespace is never body — inking it would put a coloured
    /// rectangle in the gaps the drawing needs.
    #[test]
    fn the_block_elements_are_the_body_and_every_other_glyph_is_frame() {
        let rows = logo_lines();
        assert!(rows.iter().any(|r| r.chars().any(is_body)), "the mark has a body");
        assert!(
            rows.iter().any(|r| r.chars().any(|c| c != ' ' && !is_body(c))),
            "and a frame around it"
        );
        for row in &rows {
            for c in row.chars() {
                assert!(!(c == ' ' && is_body(c)), "whitespace is never inked");
            }
        }
    }

    #[test]
    fn the_logo_is_present_and_rectangular_enough_to_lay_out() {
        let lines = logo_lines();
        assert!(lines.len() >= 5, "the mark is more than a couple of rows");
        assert!(logo_width() > 0);
        // The banner's left column is 26 cells wide; anything wider breaks its
        // border. The front end centres the mark, so this is the binding cap.
        assert!(logo_width() <= 26, "the mark must fit the CLI banner's left column");
    }

    /// Every glyph in the mark must occupy exactly one cell. A double-width
    /// character would shear the drawing in half in a terminal.
    #[test]
    fn every_logo_glyph_is_single_width() {
        for line in logo_lines() {
            assert_eq!(
                line.width(),
                line.chars().count(),
                "a glyph in {line:?} is not one cell wide"
            );
        }
    }
}
