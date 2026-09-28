"""A legacy Windows console must not crash the CLI."""

from __future__ import annotations

import io

import pytest

from agentos.cli import glyphs


@pytest.fixture(autouse=True)
def clear_cache():
    glyphs.unicode_supported.cache_clear()
    yield
    glyphs.unicode_supported.cache_clear()


class FakeStdout(io.StringIO):
    def __init__(self, encoding: str) -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding


def test_utf8_console_gets_unicode(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdout", FakeStdout("utf-8"))
    assert glyphs.unicode_supported() is True
    assert glyphs.glyph("filled") == "●"
    assert glyphs.glyph("arrow") == "→"


def test_cp1252_console_falls_back_to_ascii(monkeypatch) -> None:
    """Regression: writing U+25CF to a cp1252 console raised and killed the run."""
    monkeypatch.setattr("sys.stdout", FakeStdout("cp1252"))
    assert glyphs.unicode_supported() is False
    assert glyphs.glyph("filled") == "*"
    assert glyphs.glyph("hollow") == "o"
    assert glyphs.glyph("check") == "+"
    assert glyphs.glyph("arrow") == "->"


def test_all_glyphs_encode_on_cp1252(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdout", FakeStdout("cp1252"))
    for name in glyphs._GLYPHS:
        glyphs.glyph(name).encode("cp1252")  # must not raise


def test_missing_encoding_is_treated_as_unsupported(monkeypatch) -> None:
    class NoEncoding(io.StringIO):
        encoding = None

    monkeypatch.setattr("sys.stdout", NoEncoding())
    assert glyphs.unicode_supported() is False


def test_safe_replaces_unencodable_agent_text(monkeypatch) -> None:
    """Agent output is arbitrary text and must never crash rendering."""
    monkeypatch.setattr("sys.stdout", FakeStdout("cp1252"))
    result = glyphs.safe("emoji \U0001f600 here")
    result.encode("cp1252")  # must not raise
    assert "here" in result


def test_safe_passes_through_on_utf8(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdout", FakeStdout("utf-8"))
    assert glyphs.safe("emoji \U0001f600") == "emoji \U0001f600"
