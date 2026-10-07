"""Plain terminal-safe text; weather/alert text is never interpreted as markup."""
from __future__ import annotations

import unicodedata


def plain_text(value: str) -> str:
    # Preserve paragraphs, replace tabs, discard terminal and bidi control codes.
    # Callers must also use markup=False (or a Rich Text object) when rendering.
    return "".join(
        "    " if character == "\t" else character
        for character in value
        if character in ("\n", "\t") or unicodedata.category(character) not in ("Cc", "Cf", "Cs")
    )
