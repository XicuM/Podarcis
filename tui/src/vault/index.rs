//! A whole-vault index: every markdown page, its OKF facts, its outbound links
//! and — derived from those — its backlinks.
//!
//! Built once on a background thread at start-up and refreshed incrementally by
//! the file watcher. Roughly 700 files / 11 MB on the reference checkout, which
//! is well under a frame's worth of work, so there is no cache on disk to go
//! stale.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use super::links::{scan_links, LinkKind};
use super::page::{rel_path, Page};

pub use super::lint::{Finding, Severity};

#[derive(Clone, Debug)]
pub struct Entry {
    pub path: PathBuf,
    pub rel: String,
    pub title: String,
    pub kind: Option<String>,
    pub status: Option<String>,
    pub category: Option<String>,
    pub is_index: bool,
    /// Relative paths of in-checkout files this page links to.
    pub out_links: Vec<String>,
    pub findings: Vec<Finding>,
}

impl Entry {
    pub fn worst(&self) -> Severity {
        self.findings
            .iter()
            .map(|f| super::lint::severity(f.code))
            .max()
            .unwrap_or(Severity::Clean)
    }

    /// The collection this page belongs to: `wiki`, `workspace` or `sources`.
    pub fn collection(&self) -> &str {
        self.rel.split('/').next().unwrap_or("")
    }
}

#[derive(Debug, Default)]
pub struct Index {
    pub root: PathBuf,
    pub entries: Vec<Entry>,
    by_rel: HashMap<String, usize>,
    backlinks: HashMap<String, Vec<usize>>,
    /// Directory-level findings, keyed by relative directory path.
    pub dir_findings: HashMap<String, Finding>,
}

impl Index {
    /// Walk the content collections and build the index.
    ///
    /// `.gitignore` is deliberately not honoured: `wiki/`, `workspace/` and
    /// `sources/` are decoupled repositories and are all listed in the root
    /// `.gitignore`, so an ignore-aware walk would find nothing at all.
    pub fn build(root: &Path, collections: &[PathBuf]) -> Self {
        let mut files = Vec::new();
        let mut dir_counts: HashMap<String, usize> = HashMap::new();
        for dir in collections {
            collect(dir, root, &mut files, &mut dir_counts);
        }
        files.sort();

        let entries: Vec<Entry> = files.iter().filter_map(|path| entry_for(path, root)).collect();
        let mut index = Self { root: root.to_path_buf(), entries, ..Default::default() };
        index.dir_findings = dir_counts
            .into_iter()
            .filter(|(rel, _)| is_okf_scope(rel))
            .filter_map(|(rel, n)| super::lint::bloat(n).map(|finding| (rel, finding)))
            .collect();
        index.reindex();
        index
    }

    fn reindex(&mut self) {
        self.by_rel = self
            .entries
            .iter()
            .enumerate()
            .map(|(i, e)| (e.rel.clone(), i))
            .collect();
        self.backlinks.clear();
        for (i, entry) in self.entries.iter().enumerate() {
            for target in &entry.out_links {
                self.backlinks.entry(target.clone()).or_default().push(i);
            }
        }
        for list in self.backlinks.values_mut() {
            list.sort_unstable();
            list.dedup();
        }
    }

    pub fn get(&self, rel: &str) -> Option<&Entry> {
        self.by_rel.get(rel).map(|i| &self.entries[*i])
    }

    /// Pages that link *to* `rel`, sorted by title.
    pub fn backlinks(&self, rel: &str) -> Vec<&Entry> {
        let mut out: Vec<&Entry> = self
            .backlinks
            .get(rel)
            .map(|ids| ids.iter().map(|i| &self.entries[*i]).collect())
            .unwrap_or_default();
        out.sort_by(|a, b| a.title.cmp(&b.title));
        out
    }

    /// Look an entry up by absolute path.
    pub fn by_path(&self, path: &Path) -> Option<&Entry> {
        self.get(&rel_path(path, &self.root))
    }

    /// Re-read one file in place. Returns true if the index changed shape and
    /// backlinks had to be rebuilt. Non-markdown files (PDFs, CSVs) are not
    /// indexed and are ignored.
    pub fn refresh(&mut self, path: &Path) -> bool {
        if path.extension().and_then(|e| e.to_str()) != Some("md") {
            return false;
        }
        let rel = rel_path(path, &self.root);
        let fresh = entry_for(path, &self.root);
        match (self.by_rel.get(&rel).copied(), fresh) {
            (Some(i), Some(entry)) => {
                let links_changed = self.entries[i].out_links != entry.out_links;
                self.entries[i] = entry;
                if links_changed {
                    self.reindex();
                }
                links_changed
            }
            (Some(i), None) => {
                self.entries.remove(i);
                self.reindex();
                true
            }
            (None, Some(entry)) => {
                self.entries.push(entry);
                self.entries.sort_by(|a, b| a.rel.cmp(&b.rel));
                self.reindex();
                true
            }
            (None, None) => false,
        }
    }

    /// Every finding in the vault, page findings first then directory ones.
    pub fn all_findings(&self) -> Vec<(String, &Finding)> {
        let mut out: Vec<(String, &Finding)> = self
            .entries
            .iter()
            .flat_map(|e| e.findings.iter().map(|f| (e.rel.clone(), f)))
            .collect();
        out.extend(self.dir_findings.iter().map(|(rel, f)| (rel.clone(), f)));
        out
    }

    pub fn finding_count(&self) -> usize {
        self.entries.iter().map(|e| e.findings.len()).sum::<usize>() + self.dir_findings.len()
    }

    /// Sources that no page under `wiki/` cites — the live equivalent of
    /// `literature_status`, derived from the citation graph rather than a
    /// manifest.
    pub fn uncited_sources(&self) -> Vec<&Entry> {
        let cited: std::collections::HashSet<&str> = self
            .entries
            .iter()
            .filter(|e| e.collection() == "wiki")
            .flat_map(|e| e.out_links.iter().map(String::as_str))
            .collect();
        let mut out: Vec<&Entry> = self
            .entries
            .iter()
            .filter(|e| e.collection() == "sources" && !e.is_index && !cited.contains(e.rel.as_str()))
            .collect();
        out.sort_by(|a, b| a.rel.cmp(&b.rel));
        out
    }
}

/// The engine counts subdirectories *and* non-index files toward the bloat
/// limit, so this counts both.
fn collect(dir: &Path, root: &Path, files: &mut Vec<PathBuf>, dir_counts: &mut HashMap<String, usize>) {
    let Ok(read) = std::fs::read_dir(dir) else { return };
    let mut here = 0usize;
    for entry in read.flatten() {
        let path = entry.path();
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with('.') {
            continue;
        }
        match entry.file_type() {
            Ok(ft) if ft.is_dir() => {
                here += 1;
                collect(&path, root, files, dir_counts);
            }
            // `_index.md` is excluded from the bloat count, as in the linter.
            Ok(ft) if ft.is_file() && path.extension().and_then(|e| e.to_str()) == Some("md") => {
                if name != "_index.md" {
                    here += 1;
                }
                files.push(path);
            }
            _ => {}
        }
    }
    dir_counts.insert(rel_path(dir, root), here);
}

fn is_okf_scope(rel: &str) -> bool {
    matches!(rel.split('/').next(), Some("wiki") | Some("workspace") | Some("user"))
}

fn entry_for(path: &Path, root: &Path) -> Option<Entry> {
    if super::lint::is_skipped(path) {
        return None;
    }
    let raw = std::fs::read_to_string(path).ok()?;
    let page = Page::parse(&raw, path, root);
    let page_dir = path.parent().unwrap_or(root);

    // Outbound links are the navigation graph, which is a different question
    // from whether a link is broken: `related:` entries count here and do not
    // count for the linter.
    let mut out_links = Vec::new();
    for link in scan_links(&page.body) {
        if link.kind != LinkKind::Relative {
            continue;
        }
        if let Some(target) = link.resolve(page_dir, root) {
            if target.exists() {
                out_links.push(rel_path(&target, root));
            }
        }
    }
    for rel in &page.okf.related {
        let joined = super::links::normalize(&page_dir.join(rel));
        let joined = if joined.starts_with(root) { joined } else { super::links::normalize(&root.join(rel)) };
        if joined.starts_with(root) && joined.exists() {
            out_links.push(rel_path(&joined, root));
        }
    }
    out_links.sort();
    out_links.dedup();

    Some(Entry {
        title: page.title().to_string(),
        kind: page.okf.kind.clone(),
        status: page.okf.status.clone(),
        category: page.okf.category.clone(),
        is_index: page.is_index,
        rel: page.rel.clone(),
        findings: super::lint::check(&raw, path),
        path: path.to_path_buf(),
        out_links,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vault::lint::MAX_DIR_ENTRIES;

    struct Vault(PathBuf);

    impl Vault {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("podarcis-index-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }
        fn write(&self, rel: &str, body: &str) -> PathBuf {
            let path = self.0.join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, body).unwrap();
            path
        }
        fn index(&self) -> Index {
            let dirs: Vec<PathBuf> = ["wiki", "workspace", "sources"]
                .iter()
                .map(|d| self.0.join(d))
                .filter(|p| p.is_dir())
                .collect();
            Index::build(&self.0, &dirs)
        }
    }

    impl Drop for Vault {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn ok_page(title: &str, extra: &str, body: &str) -> String {
        format!("---\ntitle: {title}\ntype: concept\ncategory: c\nrationale: r\n{extra}---\n{body}")
    }

    #[test]
    fn builds_backlinks_from_relative_links() {
        let v = Vault::new("backlinks");
        v.write("wiki/a.md", &ok_page("A", "", "See [B](b.md).\n"));
        v.write("wiki/b.md", &ok_page("B", "", "Leaf.\n"));
        let idx = v.index();
        assert_eq!(idx.get("wiki/a.md").unwrap().out_links, vec!["wiki/b.md"]);
        let back: Vec<&str> = idx.backlinks("wiki/b.md").iter().map(|e| e.rel.as_str()).collect();
        assert_eq!(back, vec!["wiki/a.md"]);
        assert!(idx.backlinks("wiki/a.md").is_empty());
    }

    #[test]
    fn related_frontmatter_entries_count_as_links() {
        let v = Vault::new("related");
        v.write("sources/lit/s.md", "raw text\n");
        v.write(
            "wiki/a.md",
            &ok_page("A", "related:\n- ../sources/lit/s.md\n", "Body.\n"),
        );
        let idx = v.index();
        assert_eq!(idx.get("wiki/a.md").unwrap().out_links, vec!["sources/lit/s.md"]);
        assert_eq!(idx.backlinks("sources/lit/s.md").len(), 1);
    }

    #[test]
    fn reports_broken_links_with_a_file_line() {
        let v = Vault::new("broken");
        v.write("wiki/a.md", &ok_page("A", "", "line one\nSee [Gone](nope.md).\n"));
        let idx = v.index();
        let entry = idx.get("wiki/a.md").unwrap();
        let f = entry.findings.iter().find(|f| f.code == "broken_link").unwrap();
        // frontmatter is 6 lines, body line 1 -> file line 7
        assert_eq!(f.line, Some(7));
        assert_eq!(entry.worst(), Severity::Error);
    }

    #[test]
    fn flags_missing_and_unknown_frontmatter_keys() {
        let v = Vault::new("frontmatter");
        v.write("wiki/bare.md", "# No frontmatter\n");
        v.write("wiki/partial.md", "---\ntitle: T\ntype: nonsense\n---\nbody\n");
        v.write("wiki/_index.md", "# Index, exempt\n");
        v.write("sources/lit/raw.md", "extracted paper text\n");
        v.write("sources/lit/metadata.md", "raw evidence, exempt\n");
        let idx = v.index();
        let codes = |rel: &str| -> Vec<&str> {
            idx.get(rel).unwrap().findings.iter().map(|f| f.code).collect()
        };
        assert_eq!(codes("wiki/bare.md"), vec!["missing_frontmatter"]);
        let partial: Vec<String> =
            idx.get("wiki/partial.md").unwrap().findings.iter().map(|f| f.detail.clone()).collect();
        assert!(partial.iter().any(|d| d.contains("Missing required fields: category, rationale")));
        assert!(partial.iter().any(|d| d.contains("Unknown OKF document type 'nonsense'")));
        assert!(idx.get("wiki/_index.md").unwrap().findings.is_empty());
        assert!(idx.get("sources/lit/metadata.md").unwrap().findings.is_empty());
        assert!(idx.get("sources/lit/raw.md").is_none(), "raw extracted text is not a page");
    }

    #[test]
    fn footnote_findings_use_the_engines_codes() {
        let v = Vault::new("footnotes");
        v.write(
            "wiki/a.md",
            &ok_page(
                "A",
                "sources:\n- id: good\n",
                "Cites[^good] and[^absent] and[^1].\n\n[^orphan]: never referenced\n",
            ),
        );
        let idx = v.index();
        let codes: Vec<&str> = idx.get("wiki/a.md").unwrap().findings.iter().map(|f| f.code).collect();
        assert!(codes.contains(&"missing_footnote"), "{codes:?}");
        assert!(codes.contains(&"positional_footnote"), "{codes:?}");
        assert!(codes.contains(&"unused_footnote"), "{codes:?}");
        assert!(codes.contains(&"unmatched_source"), "{codes:?}");
        for code in codes {
            assert!(super::super::lint::is_known(code), "{code} is not an engine code");
        }
    }

    #[test]
    fn flags_bloated_directories_in_okf_scope_only() {
        let v = Vault::new("bloat");
        for i in 0..MAX_DIR_ENTRIES + 1 {
            v.write(&format!("wiki/big/p{i}.md"), &ok_page("P", "", "x\n"));
            v.write(&format!("sources/big/p{i}.md"), "x\n");
        }
        let idx = v.index();
        assert!(idx.dir_findings.contains_key("wiki/big"));
        assert!(!idx.dir_findings.contains_key("sources/big"), "sources/ has no bloat limit");
    }

    #[test]
    fn index_files_do_not_count_toward_the_bloat_limit() {
        let v = Vault::new("bloat-index");
        for i in 0..MAX_DIR_ENTRIES {
            v.write(&format!("wiki/d/p{i}.md"), &ok_page("P", "", "x\n"));
        }
        v.write("wiki/d/_index.md", "# Index\n");
        assert!(!v.index().dir_findings.contains_key("wiki/d"));
    }

    #[test]
    fn uncited_sources_are_derived_from_the_citation_graph() {
        let v = Vault::new("uncited");
        v.write("sources/lit/used/metadata.md", "m\n");
        v.write("sources/lit/unused/metadata.md", "m\n");
        v.write("wiki/a.md", &ok_page("A", "", "See [S](../sources/lit/used/metadata.md).\n"));
        let idx = v.index();
        let uncited: Vec<&str> = idx.uncited_sources().iter().map(|e| e.rel.as_str()).collect();
        assert_eq!(uncited, vec!["sources/lit/unused/metadata.md"]);
    }

    #[test]
    fn a_workspace_link_into_sources_does_not_count_as_citing_it() {
        // The citation chain is workspace -> wiki -> sources. Only wiki/ pages
        // may cite sources, so a workspace link must not mark one as cited.
        let v = Vault::new("chain");
        v.write("sources/lit/s/metadata.md", "m\n");
        v.write("workspace/p.md", &ok_page("P", "", "[S](../sources/lit/s/metadata.md)\n"));
        assert_eq!(v.index().uncited_sources().len(), 1);
    }

    #[test]
    fn refresh_updates_a_page_and_its_backlinks() {
        let v = Vault::new("refresh");
        let a = v.write("wiki/a.md", &ok_page("A", "", "no links\n"));
        v.write("wiki/b.md", &ok_page("B", "", "leaf\n"));
        let mut idx = v.index();
        assert!(idx.backlinks("wiki/b.md").is_empty());

        std::fs::write(&a, ok_page("A", "", "now [B](b.md)\n")).unwrap();
        assert!(idx.refresh(&a), "link set changed, backlinks must be rebuilt");
        assert_eq!(idx.backlinks("wiki/b.md").len(), 1);

        std::fs::remove_file(&a).unwrap();
        idx.refresh(&a);
        assert!(idx.get("wiki/a.md").is_none());
        assert!(idx.backlinks("wiki/b.md").is_empty());
    }

    #[test]
    fn refresh_picks_up_a_brand_new_file() {
        let v = Vault::new("new-file");
        v.write("wiki/a.md", &ok_page("A", "", "x\n"));
        let mut idx = v.index();
        let fresh = v.write("wiki/c.md", &ok_page("C", "", "x\n"));
        assert!(idx.refresh(&fresh));
        assert_eq!(idx.get("wiki/c.md").unwrap().title, "C");
    }

    #[test]
    fn hidden_directories_are_skipped() {
        let v = Vault::new("hidden");
        v.write("wiki/.obsidian/cache.md", "x\n");
        v.write("wiki/a.md", &ok_page("A", "", "x\n"));
        assert_eq!(v.index().entries.len(), 1);
    }
}
