"""Tests for QMD index-health detection.

Regression coverage for diag-1787825572 and the condition found while fixing it:
the wiki index sat at `Vectors: 0 embedded` for months while get_qmd_status()
reported "enabled_ok", so every semantic query silently degraded to keyword
results that the caller had no reason to distrust. Binary presence is not health.
"""

import importlib.util
from pathlib import Path

import pytest

# Loaded under a unique module name: both the wiki and research MCP servers are
# named server.py, so a plain `import server` collides in a full-suite run.
_SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
_spec = importlib.util.spec_from_file_location("wiki_server_health_mod", _SERVER_PATH)
wiki_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wiki_server)


HEALTHY = """QMD Status

Index: /home/xicu/Projects/Podarcis/.qmd/index.sqlite
Size:  26.1 MB

Documents
  Total:    608 files indexed
  Vectors:  608 embedded
  Pending:  0 need embedding
  Updated:  2h ago
"""

NO_VECTORS = """QMD Status

Documents
  Total:    544 files indexed
  Vectors:  0 embedded
  Pending:  540 need embedding
  Updated:  25d ago
"""

PARTIAL = """QMD Status

Documents
  Total:    608 files indexed
  Vectors:  600 embedded
  Pending:  8 need embedding
  Updated:  1h ago
"""

STALE = """QMD Status

Documents
  Total:    608 files indexed
  Vectors:  608 embedded
  Pending:  0 need embedding
  Updated:  25d ago
"""


def test_healthy_index_produces_no_warning():
    assert wiki_server._parse_index_health(HEALTHY) is None


def test_zero_vectors_is_reported_as_unusable():
    """The exact state this repo was in — semantic search cannot work at all."""
    warning = wiki_server._parse_index_health(NO_VECTORS)
    assert warning is not None
    assert "NO embeddings" in warning
    assert "540" in warning
    assert "qmd embed" in warning


def test_partial_embedding_is_left_to_qmd():
    """qmd prints its own 'N documents need embeddings' notice, which reaches the
    caller through _qmd(). Re-reporting it here would duplicate that warning."""
    assert wiki_server._parse_index_health(PARTIAL) is None


def test_stale_index_is_reported():
    warning = wiki_server._parse_index_health(STALE)
    assert warning is not None
    assert "qmd update" in warning


def test_fresh_index_is_not_called_stale():
    assert wiki_server._parse_index_health(HEALTHY) is None


def test_unparseable_status_does_not_crash():
    assert wiki_server._parse_index_health("garbage output") is None


@pytest.mark.anyio
async def test_health_warning_is_cached(monkeypatch):
    """`qmd status` spawns a subprocess; it must not run once per search."""
    calls = []

    async def fake_qmd(*args, json_output=False):
        calls.append(args)
        return NO_VECTORS

    monkeypatch.setattr(wiki_server, "_qmd", fake_qmd)
    monkeypatch.setattr(wiki_server, "_qmd_health_cache", (0.0, None))

    first = await wiki_server._qmd_index_warning()
    second = await wiki_server._qmd_index_warning()

    assert first == second
    assert "NO embeddings" in first
    assert len(calls) == 1, "second call should be served from cache"
