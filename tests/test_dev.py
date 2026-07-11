from pathlib import Path

import pytest

from app import dev


def test_tailwind_command_uses_locked_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dev.shutil, "which", lambda _: "/venv/bin/tailwindcss")

    command = dev._tailwind_command(watch=True)

    assert command == [
        "/venv/bin/tailwindcss",
        "-i",
        str(dev.INPUT_CSS),
        "-o",
        str(dev.OUTPUT_CSS),
        "--watch",
    ]
    assert dev.INPUT_CSS == Path(dev.ROOT, "static", "css", "input.css")


def test_tailwind_command_explains_missing_dev_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dev.shutil, "which", lambda _: None)

    with pytest.raises(RuntimeError, match="uv sync --extra dev"):
        dev._tailwind_command()
