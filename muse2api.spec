from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_all

root = Path(SPECPATH)
ffmpeg_datas, ffmpeg_binaries, ffmpeg_imports = collect_all("imageio_ffmpeg")
a = Analysis(
    [str(root / "windows_launcher.py")],
    pathex=[str(root)],
    binaries=ffmpeg_binaries,
    datas=[(str(root / "admin.html"), "."),
           (str(root / "version.json"), "."),
           (str(root / "extension"), "extension"),
           (str(root / "tools" / "get_muse_cookie.py"), "tools")] + ffmpeg_datas,
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("websockets") + ffmpeg_imports,
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="muse2api",
          debug=False, strip=False, upx=False, console=True)
