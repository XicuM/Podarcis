"""wiki_publish must not destroy a page that is already there.

The tool was a bare `write_text` onto the target path, so publishing over an
existing page — one a human had just edited, or one another agent wrote a
minute earlier — replaced it with nothing to recover from. The front-end's
editor refuses that write and parks the other version as
`<name>.conflict-<n>.md`; this is the same rule enforced from the other side of
the same file.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

# Loaded under a unique module name: both the wiki and research MCP servers are
# named server.py, so a plain `import server` collides in a full-suite run.
_SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
_spec = importlib.util.spec_from_file_location("wiki_server_publish_mod", _SERVER_PATH)
wiki_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wiki_server)


PAGE = (
    "---\ntype: concept\ntitle: T\ncategory: c\nrationale: r\n---\n\n"
    "Claim.[^src_2024]\n\n[^src_2024]: Source.\n"
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Point the server at a scratch root, never the real collections."""
    (tmp_path / "wiki").mkdir()
    monkeypatch.setattr(wiki_server, "ROOT", tmp_path)
    # Neither the index nor the linter is under test here.
    monkeypatch.setattr(wiki_server, "get_qmd_status", lambda: ("disabled", "off"))

    async def _no_lint(*_args):
        return ""

    monkeypatch.setattr(wiki_server, "_run_lint", _no_lint)
    return tmp_path


def publish(content, path="wiki/page.md"):
    return asyncio.run(
        wiki_server.wiki_publish(
            queue_id="src_2024",
            wiki_path=path,
            content=content,
            category="c",
            rationale="r",
            related=[],
            title="T",
        )
    )


def test_a_new_page_is_written_with_no_conflict_copy(root):
    out = publish(PAGE)
    assert (root / "wiki/page.md").read_text() == PAGE
    assert "already existed" not in out
    assert not list((root / "wiki").glob("*.conflict-*"))


def test_republishing_identical_content_leaves_no_copy(root):
    publish(PAGE)
    out = publish(PAGE)
    assert not list((root / "wiki").glob("*.conflict-*")), "an idempotent publish is not a conflict"
    assert "already existed" not in out


def test_overwriting_different_content_preserves_the_previous_page(root):
    theirs = PAGE.replace("Claim.", "A human's paragraph nobody should lose.")
    (root / "wiki/page.md").write_text(theirs, encoding="utf-8")

    out = publish(PAGE)

    assert (root / "wiki/page.md").read_text() == PAGE, "publishing still wins"
    kept = root / "wiki/page.conflict-1.md"
    assert kept.read_text() == theirs, "and the previous version survives"
    assert "already existed" in out
    assert "page.conflict-1.md" in out, "the agent is told, or the copy is as good as lost"


def test_a_second_conflict_does_not_clobber_the_first(root):
    (root / "wiki/page.md").write_text("first\n", encoding="utf-8")
    publish(PAGE)
    (root / "wiki/page.md").write_text("second\n", encoding="utf-8")
    publish(PAGE)

    assert (root / "wiki/page.conflict-1.md").read_text() == "first\n"
    assert (root / "wiki/page.conflict-2.md").read_text() == "second\n"
