"""Desktop launcher: bootstrap locally, then run the existing application."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from urllib.parse import quote

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
if FROZEN:
    os.environ.setdefault("MUSE2API_HOME", str(ROOT))


def bootstrap():
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required.")
    env_dir = ROOT / ".venv"
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        print("Creating Python environment...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(env_dir)], check=True)
    requirements = ROOT / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = env_dir / "muse2api-requirements.sha256"
    if not marker.exists() or marker.read_text() != digest:
        print("Installing dependencies (internet required on first run)...", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "--cache-dir",
                        str(ROOT / ".pip-cache"), "-r", str(requirements)], check=True)
        marker.write_text(digest)
    subprocess.run([str(python), str(ROOT / "launch.py"), "--serve"], check=True)


def prepare_config():
    path = ROOT / ".env"
    if not path.exists():
        key = "m2a_" + secrets.token_urlsafe(32)
        path.write_text(
            f"MUSE2API_KEY={key}\nMUSE2API_HOST=127.0.0.1\n"
            "MUSE2API_PORT=18610\nMUSE2API_CHROMIUM=\n"
            "MUSE2API_CONCURRENCY=10\nMUSE2API_PER_ACCOUNT=5\n", encoding="utf-8")
    # Keep browser profiles beside the app, even when extracted to another drive.
    configured_profile = any(
        line.strip().startswith("MUSE2API_PROFILE_ROOT=")
        for line in path.read_text(encoding="utf-8").splitlines()
    )
    if not configured_profile:
        os.environ.setdefault("MUSE2API_PROFILE_ROOT", str(ROOT / "browser-profiles"))


def serve():
    prepare_config()
    from config import CFG
    import uvicorn

    if not (Path(CFG.chromium).is_file()):
        import shutil
        if not shutil.which(CFG.chromium):
            raise RuntimeError("Install Chrome or Edge, or set MUSE2API_CHROMIUM in .env.")
    local_host = "127.0.0.1" if CFG.host == "0.0.0.0" else CFG.host
    with socket.socket() as probe:
        if probe.connect_ex((local_host, CFG.port)) == 0:
            raise RuntimeError(f"Port {CFG.port} is already in use. Stop the old instance first.")
    address = f"http://{local_host}:{CFG.port}/admin"
    print(f"Starting Muse2API: {address}\nPress Ctrl+C to stop.", flush=True)

    def open_when_ready():
        for _ in range(120):
            try:
                with urllib.request.urlopen(address, timeout=1) as response:
                    if response.status == 200:
                        webbrowser.open(address + "?key=" + quote(CFG.api_key, safe=""))
                        return
            except (OSError, ValueError):
                pass
            time.sleep(0.5)

    threading.Thread(target=open_when_ready, daemon=True).start()
    from app import app
    if "--check" in sys.argv:
        from app import _admin_html
        assert '<html' in _admin_html().lower()
        print("PASS: bundled application and admin resources loaded")
        return
    uvicorn.run(app, host=CFG.host, port=CFG.port, loop="asyncio", http="h11")


if __name__ == "__main__":
    os.chdir(ROOT)
    try:
        serve() if FROZEN or "--serve" in sys.argv else bootstrap()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
