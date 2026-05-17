#!/usr/bin/env python3
"""Build Bilibili UP Monitor Dashboard as a standalone .exe using PyInstaller."""

import subprocess
import sys
import shutil
import os
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "bilibili-monitor-dashboard.spec"

# Preserve user data before cleaning
_preserved: dict[str, bytes | None] = {}
for _name in (".env",):
    _p = DIST / _name
    if _p.exists():
        _preserved[_name] = _p.read_bytes()
_data_dir = DIST / "data"
_preserved_data: dict[str, bytes] = {}
if _data_dir.exists():
    for _f in _data_dir.rglob("*"):
        if _f.is_file():
            _rel = _f.relative_to(_data_dir)
            try:
                _preserved_data[str(_rel)] = _f.read_bytes()
            except Exception:
                pass

# Clean previous builds
for d in (DIST, BUILD):
    if d.exists():
        try:
            shutil.rmtree(d)
        except PermissionError:
            import stat
            def _remove_readonly(func, path, _):
                os.chmod(path, stat.S_IWRITE)
                func(path)
            try:
                shutil.rmtree(d, onerror=_remove_readonly)
            except PermissionError:
                print(f"Warning: Could not fully clean {d} (some files locked). Continuing...")

# Restore user data after cleaning
DIST.mkdir(parents=True, exist_ok=True)
for _name, _content in _preserved.items():
    if _content is not None:
        (DIST / _name).write_bytes(_content)
        print(f"Preserved {_name} across rebuild")
if _preserved_data:
    (_data_dir).mkdir(parents=True, exist_ok=True)
    for _rel, _content in _preserved_data.items():
        _fp = _data_dir / _rel
        _fp.parent.mkdir(parents=True, exist_ok=True)
        _fp.write_bytes(_content)
    print(f"Preserved {len(_preserved_data)} data files across rebuild")

# Source modules that need to be bundled
SRC_MODULES = ["config", "fetcher", "store", "monitor", "notifier", "web_server"]

import certifi
_certifi_ca = Path(certifi.where())

python_exe = sys.executable

cmd = [
    python_exe, "-m", "PyInstaller",
    "--name", "MOGU-bili监控器",
    "--onefile",
    "--noconfirm",
    "--clean",
    f"--paths={SRC}",
    "--add-data", f"{SRC / 'dashboard.html'};.",
    "--add-data", f"{SRC / 'manifest.json'};.",
    "--add-data", f"{ROOT / 'icon.png'};.",
    f"--add-data", f"{_certifi_ca};.",
    *[f"--hidden-import={m}" for m in SRC_MODULES],
    "--hidden-import=webview",
    "--hidden-import=webview.platforms",
    "--hidden-import=webview.platforms.winforms",
    "--hidden-import=clr_loader",
    "--hidden-import=pythonnet",
    "--hidden-import=certifi",
    "--hidden-import=ssl",
    "--exclude-module=PyQt5",
    "--exclude-module=PyQt6",
    "--exclude-module=PySide2",
    "--exclude-module=PySide6",
    "--exclude-module=tkinter",
    "--exclude-module=_tkinter",
    "--exclude-module=PIL",
    "--exclude-module=numpy",
    "--exclude-module=matplotlib",
    "--windowed",
    "--noupx",
    f"--icon={ROOT / 'icon.ico'}",
    str(SRC / "app.pyw"),
]

print("=" * 60)
print("Building BilibiliMonitor.exe ...")
print("=" * 60)

result = subprocess.run(cmd, cwd=str(ROOT))

if result.returncode == 0:
    exe_path = DIST / "MOGU-bili监控器.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"\n{'=' * 60}")
        print(f"SUCCESS: {exe_path}")
        print(f"Size: {size_mb:.1f} MB")
        print(f"{'=' * 60}")

        env_example = ROOT / ".env.example"
        if env_example.exists():
            shutil.copy2(env_example, DIST / ".env.example")
            print(f"Copied .env.example -> {DIST / '.env.example'}")

        print(f"""
Usage:
  1. Double-click BilibiliMonitor.exe to start
  2. A native app window will open automatically
  3. No CMD window, no browser needed
  4. Configuration is saved in .env next to the exe
""")
    else:
        print(f"\nERROR: Build succeeded but exe not found at {exe_path}")
        sys.exit(1)
else:
    print(f"\nERROR: Build failed with return code {result.returncode}")
    sys.exit(1)
