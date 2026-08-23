# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all


ROOT = Path.cwd().resolve()

if (ROOT / "main.py").is_file():
    ENTRY_SCRIPT = "main.py"
elif (ROOT / "steam_save_manager.py").is_file():
    ENTRY_SCRIPT = "steam_save_manager.py"
else:
    raise FileNotFoundError("Expected main.py or steam_save_manager.py in project root.")


datas = []
binaries = []
hiddenimports = [
    "pystray._win32",
    "pystray",
    "PIL",
    "PIL.Image",
    "psutil",
    "watchdog",
    "watchdog.observers",
    "watchdog.events",
    "webdav3",
    "webdav3.client",
]

for package_name in ("customtkinter", "pystray", "webdav3"):
    try:
        collected = collect_all(package_name)
    except Exception:
        continue
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

assets_dir = ROOT / "assets"
if assets_dir.is_dir():
    datas.append((str(assets_dir), "assets"))
app_icon = assets_dir / "app_logo.ico"


a = Analysis(
    [ENTRY_SCRIPT],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL._avif"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SteamSaveManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(app_icon) if app_icon.is_file() else "NONE",
)
