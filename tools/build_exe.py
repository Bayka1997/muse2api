"""Build a Windows EXE bundle, without user credentials or runtime data."""
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
            "--console", "--name", "Muse2API", "--distpath", str(ROOT / "dist"),
            "--workpath", str(ROOT / "build" / "exe"), "--specpath", str(ROOT / "build"),
            "--collect-submodules", "uvicorn", "--collect-submodules", "websockets"]
    for name in ("admin.html", "version.json", "extension", "tools", "deploy"):
        args += ["--add-data", f"{ROOT / name}:{name if (ROOT / name).is_dir() else '.'}"]
    args += [str(ROOT / "launch.py")]
    subprocess.run(args, cwd=ROOT, check=True)
    output = ROOT / "dist" / "muse2api-exe-windows.zip"
    folder = ROOT / "dist" / "Muse2API"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in folder.rglob("*"):
            if path.is_file() and not any(part in ("data", "browser-profiles", "__pycache__")
                                          for part in path.relative_to(folder).parts) and path.name != ".env":
                archive.write(path, "Muse2API/" + path.relative_to(folder).as_posix())
        archive.writestr("Muse2API/HUONG-DAN.txt",
                         "Giai nen toan bo thu muc vao o F. Bam Muse2API.exe de chay.\n"
                         "Khong can cai Python hay Docker. Can Chrome hoac Edge.\n"
                         "Giu thu muc _internal canh EXE. Ctrl+C de dung.\n"
                         "Cau hinh .env va du lieu data duoc tao canh EXE.\n"
                         "Nhap tai khoan Muse qua extension trong _internal/extension.\n")
    print(output)


if __name__ == "__main__":
    main()
