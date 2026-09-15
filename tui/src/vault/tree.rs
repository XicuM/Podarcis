//! The navigable content tree.
//!
//! Rows are rebuilt from disk on every structural change rather than cached:
//! listing the expanded directories costs a handful of `readdir` calls, and a
//! tree that is always a fresh read can never disagree with the filesystem.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use super::page::rel_path;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Row {
    pub path: PathBuf,
    pub rel: String,
    pub label: String,
    pub depth: usize,
    pub is_dir: bool,
    pub expanded: bool,
    /// A collection root (`wiki`, `workspace`, `sources`) — rendered as a header.
    pub is_collection: bool,
}

#[derive(Debug, Default)]
pub struct Tree {
    root: PathBuf,
    collections: Vec<PathBuf>,
    expanded: BTreeSet<PathBuf>,
    pub rows: Vec<Row>,
    pub selected: usize,
}

impl Tree {
    pub fn new(root: &Path, collections: Vec<PathBuf>) -> Self {
        let mut tree = Self {
            root: root.to_path_buf(),
            expanded: collections.iter().cloned().collect(),
            collections,
            ..Default::default()
        };
        tree.rebuild();
        tree
    }

    pub fn rebuild(&mut self) {
        let anchor = self.selected_path();
        self.rows.clear();
        for collection in self.collections.clone() {
            let expanded = self.expanded.contains(&collection);
            self.rows.push(Row {
                rel: rel_path(&collection, &self.root),
                label: collection.file_name().unwrap_or_default().to_string_lossy().to_string(),
                path: collection.clone(),
                depth: 0,
                is_dir: true,
                expanded,
                is_collection: true,
            });
            if expanded {
                self.push_children(&collection, 1);
            }
        }
        self.selected = anchor
            .and_then(|path| self.rows.iter().position(|r| r.path == path))
            .unwrap_or_else(|| self.selected.min(self.rows.len().saturating_sub(1)));
    }

    fn push_children(&mut self, dir: &Path, depth: usize) {
        for (path, is_dir) in list_dir(dir) {
            let expanded = is_dir && self.expanded.contains(&path);
            self.rows.push(Row {
                rel: rel_path(&path, &self.root),
                label: label_for(&path, is_dir),
                depth,
                is_dir,
                expanded,
                is_collection: false,
                path: path.clone(),
            });
            if expanded {
                self.push_children(&path, depth + 1);
            }
        }
    }

    pub fn collection_paths(&self) -> &[PathBuf] {
        &self.collections
    }

    /// Inclusive start and exclusive end of the rows that belong to collection `i`.
    pub fn section_span(&self, i: usize) -> Option<(usize, usize)> {
        let path = self.collections.get(i)?;
        let start = self.rows.iter().position(|r| r.path == *path)?;
        let end = self
            .rows
            .iter()
            .enumerate()
            .skip(start + 1)
            .find(|(_, r)| r.is_collection)
            .map(|(j, _)| j)
            .unwrap_or(self.rows.len());
        Some((start, end))
    }

    pub fn selected_row(&self) -> Option<&Row> {
        self.rows.get(self.selected)
    }

    pub fn selected_path(&self) -> Option<PathBuf> {
        self.selected_row().map(|r| r.path.clone())
    }

    pub fn move_by(&mut self, delta: isize) {
        if self.rows.is_empty() {
            return;
        }
        let last = self.rows.len() as isize - 1;
        self.selected = (self.selected as isize + delta).clamp(0, last) as usize;
    }

    pub fn move_to(&mut self, index: usize) {
        self.selected = index.min(self.rows.len().saturating_sub(1));
    }

    /// `l` / `→`: expand a directory (and open its index page), or signal
    /// that a file should be opened.
    pub fn expand(&mut self) -> Option<PathBuf> {
        let row = self.selected_row()?.clone();
        if !row.is_dir {
            return Some(row.path);
        }
        if !row.expanded {
            self.expanded.insert(row.path.clone());
            self.rebuild();
        } else {
            self.move_by(1);
        }
        Some(row.path)
    }

    /// `h` / `←`: collapse a directory, or jump to the parent. A collection
    /// root never collapses — the top level is permanently open.
    pub fn collapse(&mut self) {
        let Some(row) = self.selected_row().cloned() else { return };
        if row.is_dir && row.expanded && !row.is_collection {
            self.expanded.remove(&row.path);
            self.rebuild();
            return;
        }
        if let Some(parent) = row.path.parent() {
            if let Some(i) = self.rows.iter().position(|r| r.path == parent) {
                self.selected = i;
            }
        }
    }

    /// Also returns a directory's path (so it opens its index page) rather
    /// than only ever signalling files. A collection root never collapses.
    pub fn toggle(&mut self) -> Option<PathBuf> {
        let row = self.selected_row()?.clone();
        if !row.is_dir {
            return Some(row.path);
        }
        if row.expanded && !row.is_collection {
            self.expanded.remove(&row.path);
        } else if !row.expanded {
            self.expanded.insert(row.path.clone());
        }
        self.rebuild();
        Some(row.path)
    }

    /// A double-click's second press: what a single click already toggled, a
    /// folder is made sure to stay expanded (a page's single click only
    /// selected it), and the row's path is handed back to be opened — a file
    /// in the reader, a folder's `_index.md` through `App::open_path`.
    pub fn open_double(&mut self, i: usize) -> Option<PathBuf> {
        let row = self.rows.get(i)?.clone();
        if row.is_dir && !row.expanded {
            self.expanded.insert(row.path.clone());
            self.rebuild();
        }
        Some(row.path)
    }

    /// Collapses every subdirectory, but the collection roots themselves
    /// (`wiki`, `workspace`, `sources`) always stay open — they are the
    /// permanent top level of the tree, not a folder a user closes.
    pub fn collapse_all(&mut self) {
        self.expanded = self.collections.iter().cloned().collect();
        self.rebuild();
    }

    /// Expand every ancestor of `path` and select it. This is how the tree
    /// follows the reader after a link jump or a search hit.
    pub fn reveal(&mut self, path: &Path) -> bool {
        if !path.starts_with(&self.root) {
            return false;
        }
        let mut ancestor = path.parent();
        while let Some(dir) = ancestor {
            self.expanded.insert(dir.to_path_buf());
            if self.collections.iter().any(|c| c == dir) || dir == self.root {
                break;
            }
            ancestor = dir.parent();
        }
        self.rebuild();
        match self.rows.iter().position(|r| r.path == path) {
            Some(i) => {
                self.selected = i;
                true
            }
            None => false,
        }
    }

    /// Jump to the next/previous sibling at the current depth — `}` / `{`.
    pub fn move_sibling(&mut self, forward: bool) {
        let Some(depth) = self.selected_row().map(|r| r.depth) else { return };
        let range: Vec<usize> = if forward {
            (self.selected + 1..self.rows.len()).collect()
        } else {
            (0..self.selected).rev().collect()
        };
        for i in range {
            if self.rows[i].depth < depth {
                break;
            }
            if self.rows[i].depth == depth {
                self.selected = i;
                return;
            }
        }
    }
}

/// Directories first, then files — each alphabetically. `_index.md` /
/// `index.md` are never listed: selecting the folder itself opens its index
/// (see `App::open_path`), so the index page has no row of its own.
fn list_dir(dir: &Path) -> Vec<(PathBuf, bool)> {
    let Ok(read) = std::fs::read_dir(dir) else { return Vec::new() };
    let mut dirs = Vec::new();
    let mut files = Vec::new();
    for entry in read.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy().to_string();
        if name.starts_with('.') {
            continue;
        }
        let path = entry.path();
        match entry.file_type() {
            Ok(ft) if ft.is_dir() => dirs.push((name, path)),
            Ok(ft) if ft.is_file() => {
                if !name.ends_with(".md")
                    && !name.ends_with(".pdf")
                    && !name.ends_with(".csv")
                    && !name.ends_with(".png")
                    && !name.ends_with(".jpg")
                    && !name.ends_with(".jpeg")
                {
                    continue;
                }
                if name == "_index.md" || name == "index.md" {
                    continue;
                }
                files.push((name, path));
            }
            _ => {}
        }
    }
    dirs.sort();
    files.sort();
    dirs.into_iter().map(|(_, p)| (p, true)).chain(files.into_iter().map(|(_, p)| (p, false))).collect()
}

/// Underscores read as spaces, and the leading underscore of `_index.md` is
/// dropped rather than becoming a stray gap before the word.
fn label_for(path: &Path, is_dir: bool) -> String {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let stem = if is_dir {
        name.as_ref()
    } else {
        name.trim_end_matches(".md")
            .trim_end_matches(".pdf")
            .trim_end_matches(".csv")
            .trim_end_matches(".png")
            .trim_end_matches(".jpg")
            .trim_end_matches(".jpeg")
    };
    stem.trim_start_matches('_').replace('_', " ")
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture(PathBuf);

    impl Fixture {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("podarcis-tree-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            for rel in [
                "wiki/_index.md",
                "wiki/zebra.md",
                "wiki/alpha.md",
                "wiki/health/_index.md",
                "wiki/health/caffeine.md",
                "wiki/health/nutrition/creatine.md",
                "workspace/profile.md",
            ] {
                let path = dir.join(rel);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(&path, "x").unwrap();
            }
            std::fs::write(dir.join("wiki").join("notes.txt"), "ignored").unwrap();
            Self(dir)
        }
        fn tree(&self) -> Tree {
            Tree::new(&self.0, vec![self.0.join("wiki"), self.0.join("workspace")])
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn labels(tree: &Tree) -> Vec<String> {
        tree.rows.iter().map(|r| format!("{}{}", "  ".repeat(r.depth), r.label)).collect()
    }

    #[test]
    fn section_span_covers_each_collection() {
        let f = Fixture::new("spans");
        let tree = f.tree();
        let (a, b) = tree.section_span(0).unwrap();
        let (c, d) = tree.section_span(1).unwrap();
        assert_eq!(tree.rows[a].label, "wiki");
        assert_eq!(tree.rows[c].label, "workspace");
        assert_eq!(b, c);
        assert_eq!(d, tree.rows.len());
    }

    #[test]
    fn every_collection_starts_open() {
        let f = Fixture::new("initial");
        let tree = f.tree();
        assert_eq!(
            labels(&tree),
            vec!["wiki", "  health", "  alpha", "  zebra", "workspace", "  profile"]
        );
    }

    #[test]
    fn index_files_are_never_listed_as_rows() {
        let f = Fixture::new("order");
        let mut tree = f.tree();
        tree.move_to(1); // health
        tree.expand();
        assert_eq!(
            labels(&tree),
            vec![
                "wiki",
                "  health",
                "    nutrition",
                "    caffeine",
                "  alpha",
                "  zebra",
                "workspace",
                "  profile",
            ]
        );
    }

    #[test]
    fn ordering_is_directories_then_files() {
        let f = Fixture::new("order-df");
        let tree = f.tree();
        assert_eq!(
            labels(&tree),
            vec!["wiki", "  health", "  alpha", "  zebra", "workspace", "  profile"]
        );
    }

    #[test]
    fn non_markdown_files_are_hidden() {
        let f = Fixture::new("filter");
        assert!(!labels(&f.tree()).iter().any(|l| l.contains("notes")));
    }

    #[test]
    fn pdf_sources_are_shown_with_extension_stripped_label() {
        let dir = std::env::temp_dir().join(format!("podarcis-tree-pdf-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("sources/literature/smith2024")).unwrap();
        std::fs::write(dir.join("sources/literature/smith2024/original.pdf"), b"%PDF-1.4").unwrap();
        let mut tree = Tree::new(&dir, vec![dir.join("sources")]);
        tree.move_to(1); // literature
        tree.expand();
        tree.move_to(2); // smith2024
        tree.expand();
        assert!(labels(&tree).iter().any(|l| l.contains("original")));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn csv_files_are_shown_with_extension_stripped_label() {
        let dir = std::env::temp_dir().join(format!("podarcis-tree-csv-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("workspace/finance")).unwrap();
        std::fs::write(dir.join("workspace/finance/quotes.csv"), "symbol,price\nAAPL,232.1\n").unwrap();
        let mut tree = Tree::new(&dir, vec![dir.join("workspace")]);
        tree.expand(); // workspace
        tree.expand(); // finance
        assert!(tree.rows.iter().any(|r| r.label == "finance"));
        assert!(tree.rows.iter().any(|r| r.label == "quotes" && r.path.extension().unwrap() == "csv"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn image_files_are_shown_with_extension_stripped_label() {
        let dir = std::env::temp_dir().join(format!("podarcis-tree-img-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("sources/literature/smith2024")).unwrap();
        std::fs::write(dir.join("sources/literature/smith2024/figure.png"), b"\x89PNG").unwrap();
        std::fs::write(dir.join("sources/literature/smith2024/photo.jpg"), b"\xff\xd8\xff").unwrap();
        let mut tree = Tree::new(&dir, vec![dir.join("sources")]);
        tree.move_to(1); // literature
        tree.expand();
        tree.move_to(2); // smith2024
        tree.expand();
        assert!(tree.rows.iter().any(|r| r.label == "figure" && r.path.extension().unwrap() == "png"));
        assert!(tree.rows.iter().any(|r| r.label == "photo" && r.path.extension().unwrap() == "jpg"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn expanding_a_file_returns_it_to_be_opened() {
        let f = Fixture::new("open");
        let mut tree = f.tree();
        tree.move_to(2); // alpha.md
        assert_eq!(tree.expand(), Some(f.0.join("wiki").join("alpha.md")));
    }

    #[test]
    fn collapse_on_a_file_jumps_to_its_parent() {
        let f = Fixture::new("parent");
        let mut tree = f.tree();
        tree.reveal(&f.0.join("wiki/health/caffeine.md"));
        tree.collapse();
        assert_eq!(tree.selected_row().unwrap().label, "health");
    }

    #[test]
    fn reveal_expands_every_ancestor_and_selects() {
        let f = Fixture::new("reveal");
        let mut tree = f.tree();
        let target = f.0.join("wiki/health/nutrition/creatine.md");
        assert!(tree.reveal(&target));
        assert_eq!(tree.selected_path().unwrap(), target);
        assert_eq!(tree.selected_row().unwrap().depth, 3);
    }

    #[test]
    fn reveal_refuses_a_path_outside_the_checkout() {
        let f = Fixture::new("outside");
        let mut tree = f.tree();
        assert!(!tree.reveal(Path::new("/etc/passwd")));
    }

    #[test]
    fn selection_sticks_to_the_path_across_a_rebuild() {
        let f = Fixture::new("sticky");
        let mut tree = f.tree();
        tree.reveal(&f.0.join("wiki/health/caffeine.md"));
        let before = tree.selected_path();
        tree.rebuild();
        assert_eq!(tree.selected_path(), before);
    }

    #[test]
    fn movement_is_clamped_at_both_ends() {
        let f = Fixture::new("clamp");
        let mut tree = f.tree();
        tree.move_by(-10);
        assert_eq!(tree.selected, 0);
        tree.move_by(1000);
        assert_eq!(tree.selected, tree.rows.len() - 1);
    }

    #[test]
    fn sibling_jumps_stay_at_one_depth() {
        let f = Fixture::new("siblings");
        let mut tree = f.tree();
        tree.move_to(1); // wiki/health at depth 1
        tree.move_sibling(true);
        assert_eq!(tree.selected_row().unwrap().label, "alpha");
        tree.move_sibling(true);
        assert_eq!(tree.selected_row().unwrap().label, "zebra");
        tree.move_sibling(false);
        assert_eq!(tree.selected_row().unwrap().label, "alpha");
    }

    #[test]
    fn collapse_all_leaves_every_collection_root_open() {
        let f = Fixture::new("collapse-all");
        let mut tree = f.tree();
        tree.reveal(&f.0.join("wiki/health/nutrition/creatine.md"));
        tree.collapse_all();
        assert_eq!(
            labels(&tree),
            vec!["wiki", "  health", "  alpha", "  zebra", "workspace", "  profile"]
        );
    }

    #[test]
    fn a_collection_root_never_collapses() {
        let f = Fixture::new("root-stays-open");
        let mut tree = f.tree();
        tree.move_to(0); // wiki
        tree.collapse();
        assert!(tree.selected_row().unwrap().expanded);
        tree.toggle();
        assert!(tree.selected_row().unwrap().expanded);
    }

    #[test]
    fn open_double_keeps_a_folder_expanded_and_handles_back_its_path() {
        let f = Fixture::new("open-double");
        let mut tree = f.tree();
        tree.move_to(1); // health, still collapsed
        tree.toggle(); // the first press of the double click expands it
        tree.move_to(1);
        let path = tree.open_double(1).unwrap();
        assert!(tree.rows[1].expanded, "the second press must not re-collapse");
        assert_eq!(path, f.0.join("wiki").join("health"));
    }

    #[test]
    fn open_double_reexpands_after_a_single_click_collapsed_it() {
        let f = Fixture::new("open-double-expand");
        let mut tree = f.tree();
        tree.reveal(&f.0.join("wiki/health/caffeine.md")); // health expanded
        tree.move_to(1);
        tree.toggle(); // single click collapses it
        let path = tree.open_double(1).unwrap();
        assert!(tree.rows[1].expanded);
        assert_eq!(path, f.0.join("wiki").join("health"));
    }
}
