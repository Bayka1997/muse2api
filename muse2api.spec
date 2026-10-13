from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH)
a = Analysis(
    [str(root / "windows_launcher.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "admin.html"), "."),
           (str(root / "version.json"), "."),
           (str(root / "extension"), "extension"),
           (str(root / "tools" / "get_muse_cookie.py"), "tools")],
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("websockets"),
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="muse2api",
          debug=False, strip=False, upx=False, console=True)
