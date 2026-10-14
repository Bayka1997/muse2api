"""Package macOS sources with an executable launcher and no runtime data."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "START.command", "launch.py", "MACOS-QUICKSTART.md", "requirements.txt",
    "app.py", "engine.py", "cdp.py", "config.py", "store.py", "session_views.py",
    "admin.html", "version.json", "LICENSE", ".env.example", "README.md",
)


def main():
    output = ROOT / "dist" / "muse2api-macos.zip"
    output.parent.mkdir(exist_ok=True)
    paths = [ROOT / name for name in FILES]
    for folder in ("extension", "deploy", "tools"):
        paths.extend(p for p in (ROOT / folder).rglob("*")
                     if p.is_file() and p.suffix in (".py", ".js", ".json", ".html", ".md", ".txt", ".conf", ".service"))
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(paths)):
            if "__pycache__" in path.parts:
                continue
            info = zipfile.ZipInfo("muse2api/" + path.relative_to(ROOT).as_posix())
            info.create_system = 3
            info.external_attr = (0o100755 if path.name == "START.command" else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            # Text files must use Unix line endings, particularly START.command.
            archive.writestr(info, path.read_bytes().replace(b"\r\n", b"\n"))
    print(output)


if __name__ == "__main__":
    main()
