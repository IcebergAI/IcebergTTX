"""Guard templates and static assets against undefined CSS custom properties."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "app/templates", ROOT / "static/css", ROOT / "static/js")
DECLARATION = re.compile(r"(?<![\w-])(--[A-Za-z0-9_-]+)\s*:")
USE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)(?P<remainder>[^)]*)\)")


def _sources() -> list[Path]:
    return sorted(
        path
        for source_root in SOURCE_ROOTS
        for path in source_root.rglob("*")
        if path.suffix in {".css", ".html", ".js"}
    )


def test_custom_property_uses_are_declared_or_have_fallbacks() -> None:
    sources = {path: path.read_text(encoding="utf-8") for path in _sources()}
    declared = {token for source in sources.values() for token in DECLARATION.findall(source)}
    unresolved: list[str] = []

    for path, source in sources.items():
        for match in USE.finditer(source):
            token = match.group(1)
            has_fallback = match.group("remainder").lstrip().startswith(",")
            if token in declared or has_fallback:
                continue
            line = source.count("\n", 0, match.start()) + 1
            unresolved.append(f"{path.relative_to(ROOT)}:{line}: {token}")

    assert not unresolved, "Undefined CSS custom properties:\n" + "\n".join(unresolved)
