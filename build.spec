# PyInstaller spec — build with:  .venv\Scripts\pyinstaller build.spec --noconfirm
# Produces dist\ReviewKit\ReviewKit.exe (folder build: fast start-up, ships the bundled Claude CLI).
import os

import claude_agent_sdk
from PyInstaller.utils.hooks import collect_submodules

sdk_dir = os.path.dirname(claude_agent_sdk.__file__)

datas = [
    ("skills", "skills"),
    ("app/static", "app/static"),
    (os.path.join(sdk_dir, "_bundled", "claude.exe"), "claude_agent_sdk/_bundled"),
]
hidden = collect_submodules("uvicorn") + collect_submodules("claude_agent_sdk")

a = Analysis(
    ["run_reviewkit.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "numpy"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ReviewKit",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ReviewKit", upx=False)
