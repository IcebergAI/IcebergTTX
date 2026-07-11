"""Run the local application and Tailwind compiler together."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

# Development-only supervisor; every command uses an argv list and ``shell=False``.
ROOT = Path(__file__).resolve().parents[1]
INPUT_CSS = ROOT / "static" / "css" / "input.css"
OUTPUT_CSS = ROOT / "static" / "css" / "output.css"


def _tailwind_command(*, watch: bool = False) -> list[str]:
    executable = shutil.which("tailwindcss")
    if executable is None:
        raise RuntimeError(
            "tailwindcss is unavailable; install the locked development tools with "
            "`uv sync --extra dev`"
        )
    command = [executable, "-i", str(INPUT_CSS), "-o", str(OUTPUT_CSS)]
    if watch:
        command.append("--watch")
    return command


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> int:
    """Compile CSS, then supervise Tailwind watch mode and Uvicorn reload mode."""
    try:
        # The executable is the resolved Tailwind binary from the locked dev environment.
        subprocess.run(  # nosec B603
            _tailwind_command(), cwd=ROOT, check=True
        )
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Unable to prepare development assets: {exc}", file=sys.stderr)
        return 1

    processes = [
        # Both child commands are fixed argv lists; no shell evaluates user input.
        subprocess.Popen(  # nosec B603
            _tailwind_command(watch=True), cwd=ROOT
        ),
        subprocess.Popen(  # nosec B603
            [sys.executable, "-m", "uvicorn", "app.main:app", "--reload", *sys.argv[1:]],
            cwd=ROOT,
        ),
    ]
    try:
        while True:
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    return return_code
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 130
    finally:
        for process in processes:
            _stop(process)


if __name__ == "__main__":
    raise SystemExit(main())
