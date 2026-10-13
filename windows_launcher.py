"""Entry point for the standalone Windows build."""
import argparse
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import time
import urllib.request
import webbrowser


def open_when_ready(url, server):
    for _ in range(120):
        if server.should_exit:
            return
        if server.started:
            webbrowser.open(url)
            return
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description="Run muse2api locally")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.smoke_test:
        for asset in ("admin.html", "version.json", "extension/manifest.json",
                      "tools/get_muse_cookie.py"):
            if not (root / asset).is_file():
                raise RuntimeError(f"Missing bundled asset: {asset}")
    home = Path(os.environ.get("MUSE2API_HOME") or
                Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "muse2api")
    home.mkdir(parents=True, exist_ok=True)
    os.environ["MUSE2API_HOME"] = str(home)
    # Config reads .env before constructing CFG; these are only local defaults.
    from config import CFG
    if "MUSE2API_HOST" not in os.environ:
        CFG.host = "127.0.0.1"
    if "MUSE2API_KEY" not in os.environ:
        CFG.api_key = ""  # Startup generates and persists a unique key.
    from app import app
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(
        app, host=CFG.host, port=CFG.port, loop="asyncio", http="h11",
        ws="websockets", workers=1))
    host = "127.0.0.1" if CFG.host in ("0.0.0.0", "::") else CFG.host
    url = f"http://{host}:{CFG.port}/"
    if args.smoke_test:
        def check():
            try:
                for _ in range(120):
                    if server.started:
                        break
                    time.sleep(0.5)
                with urllib.request.urlopen(url, timeout=10) as response:
                    if response.status != 200 or b"muse2api" not in response.read().lower():
                        raise RuntimeError("Admin page smoke test failed")
                print("Smoke test passed", flush=True)
            except Exception as exc:
                print(f"Smoke test failed: {exc}", flush=True)
                os._exit(1)
            finally:
                server.should_exit = True
        threading.Thread(target=check, daemon=True).start()
    elif not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url, server), daemon=True).start()
    print(f"muse2api: {url}\nData: {home}\nPress Ctrl+C to stop.", flush=True)
    server.run()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
