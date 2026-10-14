"""Create a distributable ZIP from an explicit allowlist, excluding user data."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "START.bat", "launch.py", "WINDOWS-QUICKSTART.md", "requirements.txt",
    "app.py", "engine.py", "cdp.py", "config.py", "store.py", "session_views.py",
    "admin.html", "version.json", "LICENSE", ".env.example", "README.md",
    "Dockerfile", "docker-compose.yml",
)


def main():
    output = ROOT / "dist" / "muse2api-windows.zip"
    output.parent.mkdir(exist_ok=True)
    paths = [ROOT / name for name in FILES]
    for folder in ("extension", "deploy", "tools"):
        paths.extend(p for p in (ROOT / folder).rglob("*")
                     if p.is_file() and p.suffix in (".py", ".js", ".json", ".html", ".md", ".txt", ".conf", ".service"))
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(paths)):
            if "__pycache__" not in path.parts:
                archive.write(path, "muse2api/" + path.relative_to(ROOT).as_posix())
    print(output)


if __name__ == "__main__":
    main()
