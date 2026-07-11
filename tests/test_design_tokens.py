"""Static accessibility checks for shared design-system color tokens."""

import re
from math import cos, radians, sin
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "static/css/iceberg.css").read_text(encoding="utf-8")


def _block(selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", CSS, re.DOTALL)
    assert match is not None, f"missing {selector} token block"
    return match.group("body")


def _oklch(block: str, token: str) -> tuple[float, float, float]:
    match = re.search(
        rf"{re.escape(token)}:\s*oklch\(([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\)",
        block,
    )
    assert match is not None, f"{token} must be a concrete oklch color"
    return tuple(float(value) for value in match.groups())


def _relative_luminance(color: tuple[float, float, float]) -> float:
    lightness, chroma, hue = color
    a = chroma * cos(radians(hue))
    b = chroma * sin(radians(hue))
    l_value = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_value = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_value = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3
    red, green, blue = (
        4.0767416621 * l_value - 3.3077115913 * m_value + 0.2309699292 * s_value,
        -1.2684380046 * l_value + 2.6097574011 * m_value - 0.3413193965 * s_value,
        -0.0041960863 * l_value - 0.7034186147 * m_value + 1.707614701 * s_value,
    )
    red, green, blue = (max(0.0, min(1.0, channel)) for channel in (red, green, blue))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_shared_secondary_text_tokens_meet_wcag_aa() -> None:
    light = _block(":root")
    dark = _block('html[data-theme="dark"]')

    combinations = [
        (light, "--muted", light, "--paper"),
        (light, "--muted", light, "--surface"),
        (light, "--faint", light, "--paper"),
        (light, "--faint", light, "--surface"),
        (light, "--rail-faint", light, "--rail"),
        (light, "--rail-faint", light, "--rail-2"),
        (light, "--primary-ink", light, "--accent-ink"),
        (light, "--primary-ink", light, "--accent-deep"),
        (light, "--surface-ink", light, "--accent"),
        (dark, "--muted", dark, "--paper"),
        (dark, "--muted", dark, "--surface"),
        (dark, "--faint", dark, "--paper"),
        (dark, "--faint", dark, "--surface"),
        (dark, "--rail-faint", dark, "--rail"),
        (dark, "--rail-faint", dark, "--rail-2"),
        (dark, "--primary-ink", dark, "--accent-ink"),
        (dark, "--primary-ink", dark, "--accent-deep"),
        (dark, "--surface-ink", dark, "--accent"),
    ]

    for foreground_block, foreground, background_block, background in combinations:
        ratio = _contrast(
            _oklch(foreground_block, foreground),
            _oklch(background_block, background),
        )
        assert ratio >= 4.5, f"{foreground} on {background} is only {ratio:.2f}:1"
