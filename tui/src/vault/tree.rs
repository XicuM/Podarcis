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
            expanded: collections.first().cloned().into_iter().collect(),
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

    /// `l` / `→`: open a directory, or signal that a file should be opened.
    pub fn expand(&mut self) -> Option<PathBuf> {
        let row = self.selected_row()?.clone();
        if !row.is_dir {
            return Some(row.path);
        }
        if !row.expanded {
            self.expanded.insert(row.path);
            self.rebuild();
        } else {
            self.move_by(1);
        }
        None
    }

    /// `h` / `←`: collapse a directory, or jump to the parent.
    pub fn collapse(&mut self) {
        let Some(row) = self.selected_row().cloned() else { return };
        if row.is_dir && row.expanded {
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

    pub fn toggle(&mut self) -> Option<PathBuf> {
        let row = self.selected_row()?.clone();
        if !row.is_dir {
            return Some(row.path);
        }
        if row.expanded {
            self.expanded.remove(&row.path);
        } else {
            self.expanded.insert(row.path);
        }
        self.rebuild();
        None
    }

    pub fn collapse_all(&mut self) {
        self.expanded.clear();
        if let Some(first) = self.collections.first().cloned() {
            self.expanded.insert(first);
        }
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

/// `_index.md` first, then directories, then files — each alphabetically.
/// An index page is the entry point to its folder, so it belongs at the top.
fn list_dir(dir: &Path) -> Vec<(PathBuf, bool)> {
    let Ok(read) = std::fs::read_dir(dir) else { return Vec::new() };
    let mut dirs = Vec::new();
    let mut files = Vec::new();
    let mut index = Vec::new();
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
                if !name.ends_with(".md") {
                    continue;
                }
                if name == "_index.md" || name == "index.md" {
                    index.push((name, path));
                } else {
                    files.push((name, path));
                }
            }
            _ => {}
        }
    }
    dirs.sort();
    files.sort();
    index.sort();
    index
        .into_iter()
        .map(|(_, p)| (p, false))
        .chain(dirs.into_iter().map(|(_, p)| (p, true)))
        .chain(files.into_iter().map(|(_, p)| (p, false)))
        .collect()
}

/// Underscores read as spaces, and the leading underscore of `_index.md` is
/// dropped rather than becoming a stray gap before the word.
fn label_for(path: &Path, is_dir: bool) -> String {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let stem = if is_dir { name.as_ref() } else { name.trim_end_matches(".md") };
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
    fn first_collection_starts_open_and_the_rest_closed() {
        let f = Fixture::new("initial");
        let tree = f.tree();
        assert_eq!(
            labels(&tree),
            vec!["wiki", "  index", "  health", "  alpha", "  zebra", "workspace"]
        );
    }

    #[test]
    fn ordering_is_index_then_directories_then_files() {
        let f = Fixture::new("order");
        let mut tree = f.tree();
        tree.move_to(2); // health
        tree.expand();
        assert_eq!(
            labels(&tree),
            vec![
                "wiki",
                "  index",
                "  health",
                "    index",
                "    nutrition",
                "    caffeine",
                "  alpha",
                "  zebra",
                "workspace",
            ]
        );
    }

    #[test]
    fn non_markdown_files_are_hidden() {
        let f = Fixture::new("filter");
        assert!(!labels(&f.tree()).iter().any(|l| l.contains("notes")));
    }

    #[test]
    fn expanding_a_file_returns_it_to_be_opened() {
        let f = Fixture::new("open");
        let mut tree = f.tree();
        tree.move_to(3); // alpha.md
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
        tree.move_to(1); // wiki/_index at depth 1
        tree.move_sibling(true);
        assert_eq!(tree.selected_row().unwrap().label, "health");
        tree.move_sibling(true);
        assert_eq!(tree.selected_row().unwrap().label, "alpha");
        tree.move_sibling(false);
        assert_eq!(tree.selected_row().unwrap().label, "health");
    }

    #[test]
    fn collapse_all_leaves_only_the_first_collection_open() {
        let f = Fixture::new("collapse-all");
        let mut tree = f.tree();
        tree.reveal(&f.0.join("wiki/health/nutrition/creatine.md"));
        tree.collapse_all();
        assert_eq!(labels(&tree), vec!["wiki", "  index", "  health", "  alpha", "  zebra", "workspace"]);
    }
}
