//! CSV support: RFC 4180 parsing and table rendering.
//!
//! Parsing goes through the `csv` crate so quoting, escaped newlines and
//! embedded separators survive round trips. Rendering hands the grid to
//! `markdown::render_grid`, which draws the same closed box the markdown reader
//! paints for a GFM table — so a CSV gets the reader's full machinery (scroll,
//! mouse wheel, selection, reader↔editor source mapping) for free.

use crate::theme::Theme;
use crate::ui::markdown;

/// A parsed grid. Row 0 is treated as the header when rendered.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Grid(pub Vec<Vec<String>>);

impl Grid {
    pub fn parse(text: &str) -> Self {
        let mut reader = csv::ReaderBuilder::new()
            // All rows are data: the first record is the header, but it still
            // renders as a row of its own.
            .has_headers(false)
            // Ragged rows render rather than erroring the whole file.
            .flexible(true)
            .from_reader(text.as_bytes());
        let rows = reader
            .records()
            .filter_map(|r| r.ok())
            .map(|r| r.iter().map(str::to_string).collect())
            .collect();
        Self(rows)
    }

    pub fn to_doc(&self, width: u16, theme: &Theme) -> markdown::Doc {
        markdown::render_grid(&self.0, width, theme)
    }
}

#[cfg(test)]
mod tests {
    use super::Grid;

    fn parse(text: &str) -> Vec<Vec<String>> {
        Grid::parse(text).0
    }

    #[test]
    fn quoted_cells_survive() {
        let text = "a,b\n1,\"two, three\"\n";
        assert_eq!(parse(text), vec![vec!["a", "b"], vec!["1", "two, three"]]);
    }

    #[test]
    fn escaped_quotes_round_trip() {
        let text = "a\n\"say \"\"hi\"\"\"\n";
        assert_eq!(parse(text), vec![vec!["a"], vec!["say \"hi\""]]);
    }

    #[test]
    fn embedded_newline_is_one_cell() {
        let text = "a,b\n\"line 1\nline 2\",b\n";
        let grid = parse(text);
        assert_eq!(grid.len(), 2);
        assert_eq!(grid[1][0], "line 1\nline 2");
    }

    #[test]
    fn ragged_rows_stay_readable() {
        let text = "a,b,c\n1\n2,3\n";
        let grid = parse(text);
        assert_eq!(grid[1], vec!["1"]);
        assert_eq!(grid[2], vec!["2", "3"]);
    }

    #[test]
    fn empty_file_is_an_empty_grid() {
        assert!(parse("").is_empty());
    }
}