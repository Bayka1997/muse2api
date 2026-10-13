"""Run: python tests/test_collect_api.py. ASGI mocks only; no Muse or Chromium."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_async_images import Client
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def check(module):
    account = module.store.add_account({"hatch_sess": "secret-cookie"}, "Account B")
    task = module.store.create_task("video", "A cat walks through neon rain")
    tid = task["id"]
    module.store.update_task(tid, status="failed", error="等待生成超时",
                             account=account["id"], account_label="Account B",
                             muse_url="https://muse.ai/thread/rendered-1?token=hidden")
    calls = []

    def collect_rendered(cookies, url, expect="video", expires=None, account_id=None, timeout=60):
        calls.append({"cookies": cookies, "url": url, "expect": expect, "account_id": account_id})
        return {"filename": "kept.mp4", "size": 4, "kind": "video"}

    module.engine.collect_rendered = collect_rendered
    module.engine.start = lambda: None
    module.engine.close_current_page = lambda: None
    def generate(*_args, **_kwargs):
        raise AssertionError("prompt was sent again")

    module.engine.generate = generate

    async with Client(module.app, {"Authorization": "Bearer collect-test"}) as client:
        response = await client.request("POST", "/admin/tasks/" + tid + "/collect",
                                        headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401 and calls == []
        response = await client.request("POST", "/admin/tasks/" + tid + "/collect")
        assert response.status_code == 200, (response.status_code, response.body)
        body = response.json()
        assert body["status"] == "completed"
        assert body["url"].endswith("/v1/media/kept.mp4")
        assert body["video"]["url"] == body["url"]
        assert body.get("error") in (None, "")
        assert calls == [{
            "cookies": {"hatch_sess": "secret-cookie"},
            "url": "https://muse.ai/thread/rendered-1?token=hidden",
            "expect": "video",
            "account_id": account["id"],
        }]
        assert "A cat walks" not in str(calls)
        response = await client.request("POST", "/admin/tasks/" + tid + "/collect")
        assert response.status_code == 200 and len(calls) == 1

        bare = module.store.create_task("video", "No conversation")
        module.store.update_task(bare["id"], status="failed", account=account["id"])
        response = await client.request("POST", "/admin/tasks/" + bare["id"] + "/collect")
        assert response.status_code == 409 and len(calls) == 1
        response = await client.request("POST", "/admin/tasks/missing/collect")
        assert response.status_code == 404
    print("PASS: failed rendered videos are collected from the saved account thread without submitting the prompt again.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="muse-collect-api-") as directory:
        os.environ.update(MUSE2API_HOME=directory, MUSE2API_PROFILE_ROOT=directory, MUSE2API_KEY="collect-test")
        import app
        asyncio.run(check(app))
