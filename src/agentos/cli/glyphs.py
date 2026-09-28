"""Terminal glyphs with an ASCII fallback.

A legacy Windows console is often cp1252, where writing a character like U+25CF
raises UnicodeEncodeError and takes the whole command down. We ask the real
output stream whether it can encode a character and degrade to ASCII when it
cannot, so output is plainer on old consoles but never crashes.
"""

from __future__ import annotations

import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def unicode_supported() -> bool:
    """True when stdout can encode the box/bullet characters we use."""
    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        return False
    try:
        "●○✓✗→".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# name -> (preferred, ascii fallback)
_GLYPHS: dict[str, tuple[str, str]] = {
    "filled": ("●", "*"),     # running / active
    "hollow": ("○", "o"),     # pending / waiting
    "check": ("✓", "+"),      # completed
    "cross": ("✗", "x"),      # failed
    "arrow": ("→", "->"),     # sender -> recipient
    "dash": ("–", "-"),       # cancelled / not applicable
}


def glyph(name: str) -> str:
    preferred, fallback = _GLYPHS[name]
    return preferred if unicode_supported() else fallback


def safe(text: str) -> str:
    """Strip characters the current stdout cannot encode.

    Used for text that came from an agent, which may contain anything.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, "replace")
    except LookupError:
        return text
    return text


def literal(text: str) -> str:
    """Text that must appear exactly as written, not as Rich markup.

    Memories and agent output routinely contain square brackets -- a category tag
    like [decision], or a log line -- which Rich would otherwise interpret as a
    style and silently swallow. That is unacceptable in commands whose whole job
    is to show what is really there.
    """
    from rich.markup import escape

    return escape(safe(text))
