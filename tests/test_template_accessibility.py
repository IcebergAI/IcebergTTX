from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORM_TEMPLATES = (
    ROOT / "app/templates/scenarios/editor.html",
    ROOT / "app/templates/settings.html",
)
FORM_CONTROLS = {"input", "select", "textarea"}


class TemplateSemanticsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.controls: list[tuple[str, dict[str, str | None], int]] = []
        self.labels: list[tuple[dict[str, str | None], int]] = []
        self.static_ids: list[tuple[str, int]] = []
        self.dynamic_ids: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        line, _ = self.getpos()
        if tag in FORM_CONTROLS:
            self.controls.append((tag, attributes, line))
        if tag == "label":
            self.labels.append((attributes, line))
        if field_id := attributes.get("id"):
            self.static_ids.append((field_id, line))
        if field_id := attributes.get(":id"):
            self.dynamic_ids.append((field_id, line))


def parse_template(path: Path) -> TemplateSemanticsParser:
    parser = TemplateSemanticsParser()
    parser.feed(path.read_text())
    return parser


@pytest.mark.parametrize("path", FORM_TEMPLATES, ids=lambda path: path.name)
def test_form_controls_have_explicit_accessible_names(path: Path) -> None:
    parser = parse_template(path)
    static_labels = {attrs.get("for") for attrs, _ in parser.labels if attrs.get("for")}
    dynamic_labels = {attrs.get(":for") for attrs, _ in parser.labels if attrs.get(":for")}
    failures = []

    for tag, attrs, line in parser.controls:
        static_id = attrs.get("id")
        dynamic_id = attrs.get(":id")
        if static_id and static_id in static_labels:
            continue
        if dynamic_id and dynamic_id in dynamic_labels:
            continue
        failures.append(f"line {line}: <{tag}> has no matching explicit label")

    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("path", FORM_TEMPLATES, ids=lambda path: path.name)
def test_form_templates_have_no_duplicate_ids(path: Path) -> None:
    parser = parse_template(path)
    static_counts = Counter(field_id for field_id, _ in parser.static_ids)
    dynamic_counts = Counter(field_id for field_id, _ in parser.dynamic_ids)

    duplicate_static = sorted(field_id for field_id, count in static_counts.items() if count > 1)
    duplicate_dynamic = sorted(field_id for field_id, count in dynamic_counts.items() if count > 1)

    assert duplicate_static == []
    assert duplicate_dynamic == []


@pytest.mark.parametrize("path", FORM_TEMPLATES, ids=lambda path: path.name)
def test_form_descriptions_reference_existing_static_ids(path: Path) -> None:
    parser = parse_template(path)
    static_ids = {field_id for field_id, _ in parser.static_ids}
    failures = []

    for tag, attrs, line in parser.controls:
        for described_id in (attrs.get("aria-describedby") or "").split():
            if described_id not in static_ids:
                failures.append(
                    f"line {line}: <{tag}> describes itself with missing #{described_id}"
                )

    assert not failures, "\n".join(failures)
