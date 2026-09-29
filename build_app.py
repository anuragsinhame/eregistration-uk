#!/usr/bin/env python3
"""
build_app.py - package the GUI as a double-clickable app with PyInstaller.

    python build_app.py                 # -> dist/UK e-Registration Search.app (macOS) or dist/UK e-Registration Search/ (Windows)
    python build_app.py --zip           # also writes dist/UK-e-Registration-Search-<platform>-<arch>.zip
    python build_app.py --dmg           # also writes a macOS disk image
    python build_app.py --onefile       # writes a standalone Windows executable
    python build_app.py --no-browser    # smaller app without Chromium (target machine then needs `playwright install chromium`)

What it does
  1. pip installs the app requirements and PyInstaller into the current interpreter.
  2. Downloads Playwright's Chromium (+ headless shell) into build/ms-playwright.
  3. Runs PyInstaller (--windowed, one-folder) with gui.html, hindi_input.js and the example settings as
     data files and the whole playwright package (its Node driver) collected.
  4. Copies the browsers INTO the app at playwright/driver/package/.local-browsers - with `ditto` on
     macOS so Chromium's framework symlinks survive - and makes the driver's `node` executable.
     uk_ereg_gui.py sets PLAYWRIGHT_BROWSERS_PATH=0 when it finds that folder, so the app is
     self-contained: no Python, Playwright or browser install needed on the target machine.
  5. Optionally zips the result for distribution.

Run it on the OS you are building for (PyInstaller does not cross-compile). The GitHub Actions
workflow in .github/workflows/build.yml runs this on macOS and Windows runners.

Notes for the built app
  * macOS blocks the first launch of an unsigned downloaded app: System Settings -> Privacy & Security
    -> "Open Anyway", or  xattr -dr com.apple.quarantine "UK e-Registration Search.app".
  * Windows needs the WebView2 runtime (present on Windows 10/11 with Edge); SmartScreen may warn once.
  * Settings, captcha keys and downloads live in the user's Application Support / AppData folder
    when the app is frozen (APP_DIR in uk_ereg_gui.py), never inside the bundle.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "UK e-Registration Search"
ENTRY = "uk_ereg_gui.py"
DATA_FILES = ["gui.html", "hindi_input.js", "captcha_settings.example.json"]
HIDDEN = ["uk_eregistration", "hindi_input", "captcha_solvers", "report_export", "openpyxl", "truststore",
          "pytesseract", "PIL"]
BROWSERS_BUILD_DIR = HERE / "build" / "ms-playwright"


def run(cmd: list[str], **env) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=str(HERE), env={**os.environ, **env})


def internal_dir(built: Path) -> Path:
    """Where sys._MEIPASS points at runtime for this build."""
    if sys.platform == "darwin":
        return built / "Contents" / "Frameworks"
    return built / "_internal"


def copy_tree(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    if sys.platform == "darwin":
        run(["ditto", str(src), str(dst)])          # preserves symlinks, permissions and resource forks
    else:
        shutil.copytree(src, dst, symlinks=True)


def make_dmg(app: Path, out: Path) -> None:
    if out.exists():
        out.unlink()
    run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", str(app),
         "-ov", "-format", "UDZO", str(out)])
    print("DMG:", out, f"({out.stat().st_size / 1e6:.0f} MB)")


def main() -> int:
    zip_it = "--zip" in sys.argv
    create_dmg = "--dmg" in sys.argv
    onefile = "--onefile" in sys.argv
    bundle_browser = "--no-browser" not in sys.argv
    py = sys.executable

    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-r", "requirements-gui.txt", "pyinstaller"])
    if bundle_browser:
        BROWSERS_BUILD_DIR.mkdir(parents=True, exist_ok=True)
        run([py, "-m", "playwright", "install", "chromium"], PLAYWRIGHT_BROWSERS_PATH=str(BROWSERS_BUILD_DIR))

    sep = ";" if os.name == "nt" else ":"
    cmd = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", APP_NAME,
           "--collect-all", "playwright"]
    if bundle_browser and onefile and sys.platform == "win32":
        cmd += ["--add-data", f"{BROWSERS_BUILD_DIR}{sep}playwright/driver/package/.local-browsers"]
    if sys.platform == "darwin":
        cmd += ["--osx-bundle-identifier", "com.roxstar.ukeregsearch"]
    if onefile and sys.platform == "win32":
        cmd += ["--onefile"]
    for f in DATA_FILES:
        cmd += ["--add-data", f"{f}{sep}."]
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    icon = HERE / ("app.icns" if sys.platform == "darwin" else "app.ico")
    if icon.exists():
        cmd += ["--icon", str(icon)]
    cmd.append(ENTRY)
    run(cmd)

    dist = HERE / "dist"
    if onefile and sys.platform == "win32":
        built = dist / f"{APP_NAME}.exe"
    else:
        built = dist / (f"{APP_NAME}.app" if sys.platform == "darwin" else APP_NAME)
    if not built.exists():
        print("build output not found:", built)
        return 1
    if onefile and sys.platform == "win32":
        artifact = dist / "UK-e-Registration-Search-windows-x64.exe"
        if artifact.exists():
            artifact.unlink()
        built.rename(artifact)
        built = artifact
    if sys.platform == "darwin" and (dist / APP_NAME).is_dir():
        shutil.rmtree(dist / APP_NAME, ignore_errors=True)   # PyInstaller's bare folder next to the .app

    internal = internal_dir(built)
    node = internal / "playwright" / "driver" / ("node.exe" if os.name == "nt" else "node")
    if node.exists() and os.name != "nt":
        node.chmod(node.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if bundle_browser:
        rel = Path("playwright") / "driver" / "package" / ".local-browsers"
        if onefile and sys.platform == "win32":
            print("Browsers bundled into the Windows executable.")
            target = BROWSERS_BUILD_DIR
        elif sys.platform == "darwin":
            # PyInstaller 6 keeps data files (the driver's .js) in Contents/Resources and symlinks them
            # from Contents/Frameworks. Node resolves the driver's real path, so the browsers must be
            # real under Resources; Frameworks gets a symlink so sys._MEIPASS-based checks see them too.
            real = built / "Contents" / "Resources" / rel
            copy_tree(BROWSERS_BUILD_DIR, real)
            link = internal / rel
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                (shutil.rmtree(link) if link.is_dir() and not link.is_symlink() else link.unlink())
            link.symlink_to(os.path.relpath(real, link.parent))
            target = real
        else:
            target = internal / rel
            copy_tree(BROWSERS_BUILD_DIR, target)
        try:
            print("Bundled browsers:", ", ".join(p.name for p in target.iterdir()))
        except OSError:
            print("Bundled browsers:", target)
    print("\nBuilt:", built)

    tag = {"darwin": "macos", "win32": "windows"}.get(sys.platform, platform.system().lower())
    arch = platform.machine().lower().replace("x86_64", "x64").replace("amd64", "x64")

    if create_dmg and sys.platform == "darwin":
        make_dmg(built, dist / f"UK-e-Registration-Search-{tag}-{arch}.dmg")

    if zip_it:
        out = dist / f"UK-e-Registration-Search-{tag}-{arch}.zip"
        if out.exists():
            out.unlink()
        if sys.platform == "darwin":
            run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(built), str(out)])
        else:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in built.rglob("*"):
                    zf.write(p, p.relative_to(dist))
        print("Zip:", out, f"({out.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
