#!/usr/bin/env python3
"""
setup_env.py - one-time setup on a new machine (any Python 3.10+ can run it):

    python3 setup_env.py            # creates .venv, installs requirements-gui.txt, downloads Chromium
    python3 setup_env.py --no-venv  # use the current interpreter instead of .venv

Afterwards:
    source .venv/bin/activate        (Windows: .venv\\Scripts\\activate)
    python uk_ereg_gui.py

`requirements.txt` cannot download browsers (pip has no post-install step), so this script runs
`playwright install chromium` for you. The app also does this by itself the first time Chromium is
missing, so the script is a convenience, not a requirement.
"""
from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=str(HERE))


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main() -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required.")
        return 1
    if "--no-venv" in sys.argv:
        py = Path(sys.executable)
    else:
        if not venv_python().exists():
            print(f"Creating virtual environment in {VENV} ...")
            venv.EnvBuilder(with_pip=True, upgrade_deps=False).create(VENV)
        py = venv_python()
    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(py), "-m", "pip", "install", "-r", "requirements-gui.txt"])
    run([str(py), "-m", "playwright", "install", "chromium"])
    example, settings = HERE / "captcha_settings.example.json", HERE / "captcha_settings.json"
    if example.exists() and not settings.exists():
        settings.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Created {settings.name} - add API keys there if you want automatic captcha reading.")
    activate = r".venv\Scripts\activate" if os.name == "nt" else "source .venv/bin/activate"
    print("\nDone. Run the app with:")
    if "--no-venv" not in sys.argv:
        print(f"  {activate}")
    print("  python uk_ereg_gui.py")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\nStep failed with exit code {exc.returncode}: {' '.join(exc.cmd)}")
        sys.exit(exc.returncode)
