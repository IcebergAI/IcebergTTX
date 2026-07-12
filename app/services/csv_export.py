"""Spreadsheet-safe CSV cell encoding.

CSV quoting protects the file structure, but spreadsheet applications may still
evaluate a quoted cell as a formula. Keep that output concern in one helper so
every current and future CSV field crosses the same safety boundary.
"""

import unicodedata
from collections.abc import Iterable

_FORMULA_MARKERS = frozenset({"=", "+", "-", "@"})
_LEADING_CONTROL_MARKERS = frozenset({"\t", "\r", "\n"})


def spreadsheet_safe_cell(value: object) -> object:
    """Return *value* with formula-capable strings forced to spreadsheet text.

    Detection uses NFKC only for comparison so full-width marker variants and
    ignorable leading whitespace/control characters cannot evade the check. The
    original value is preserved after a leading apostrophe; numbers and other
    non-string values keep their native CSV representation.
    """
    if not isinstance(value, str) or not value:
        return value

    normalized = unicodedata.normalize("NFKC", value)
    if not normalized:
        return value

    unsafe_control = False
    first_significant = ""
    for char in normalized:
        if char.isspace() or unicodedata.category(char).startswith("C"):
            unsafe_control = unsafe_control or char in _LEADING_CONTROL_MARKERS
            continue
        first_significant = char
        break
    if unsafe_control or first_significant in _FORMULA_MARKERS:
        return "'" + value
    return value


def spreadsheet_safe_row(values: Iterable[object]) -> list[object]:
    """Apply :func:`spreadsheet_safe_cell` to one complete CSV row."""
    return [spreadsheet_safe_cell(value) for value in values]
