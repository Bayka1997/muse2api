"""Run: python tests/test_collect_rendered.py. No Chrome and no prompt is sent."""
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import types
fake_cdp = types.ModuleType("cdp")
fake_cdp.CDP = object
fake_cdp.http_json = lambda *args, **kwargs: {}
sys.modules["cdp"] = fake_cdp
import engine as engine_module

MuseEngine = engine_module.MuseEngine
MuseGenerationError = engine_module.MuseGenerationError


class Page:
    def __init__(self):
        self.navs = []

    def send(self, method, params=None, timeout=None):
        assert method == "Page.navigate"
        self.navs.append(params["url"])


def engine_for(tmp):
    cfg = SimpleNamespace(profile_dir=os.path.join(tmp, "profile"),
                          media_dir=os.path.join(tmp, "media"),
                          download_dir=os.path.join(tmp, "downloads"),
                          data_dir=os.path.join(tmp, "data"),
                          cdp_port=19210, login_wait=1)
    os.makedirs(cfg.media_dir)
    engine = MuseEngine(cfg)
    page = Page()
    engine.page = page
    engine.ensure_page = lambda *args, **kwargs: None
    engine._wait_ws_ready = lambda *args, **kwargs: True
    engine._scroll_bottom = lambda: None
    engine._send = lambda prompt: (_ for _ in ()).throw(AssertionError("sent prompt"))
    engine.reset_thread = lambda **kwargs: (_ for _ in ()).throw(AssertionError("reset thread"))
    engine._download_fallback = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("download fallback"))
    return engine, page


def main():
    original_sleep = engine_module.time.sleep
    engine_module.time.sleep = lambda _seconds: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            engine, page = engine_for(tmp)
            calls = {"n": 0}

            def attachments():
                calls["n"] += 1
                if calls["n"] == 1:
                    return [{"tid": "presentation-video", "hasVideo": False,
                             "src": "https://cdn.example/poster.jpg", "vSrc": ""}]
                return [{"tid": "presentation-video", "hasVideo": True,
                         "src": "https://cdn.example/poster.jpg",
                         "vSrc": "https://cdn.example/clip.mp4", "w": 640, "h": 360}]

            extracted = {}

            def extract(src, expect="image", retries=4):
                extracted["src"] = src
                extracted["expect"] = expect
                return b"videobytes", "video/mp4", src

            engine.attachments = attachments
            engine.extract_bytes = extract
            result = engine.collect_rendered(
                {"hatch_sess": "secret"}, "https://muse.ai/thread/abc?token=hidden",
                expect="video", account_id="account-b")
            assert page.navs == ["https://muse.ai/thread/abc"]
            assert extracted == {"src": "https://cdn.example/clip.mp4", "expect": "video"}
            assert result["kind"] == "video" and result["size"] == len(b"videobytes")
            assert open(result["path"], "rb").read() == b"videobytes"

        with tempfile.TemporaryDirectory() as tmp:
            engine, page = engine_for(tmp)
            engine.attachments = lambda: [{"tid": "presentation-image", "hasVideo": False,
                                           "src": "https://cdn.example/poster.jpg", "vSrc": ""}]
            original_time = engine_module.time.time
            clock = {"now": 1000.0}

            def later():
                clock["now"] += 30
                return clock["now"]

            engine_module.time.time = later
            try:
                engine.collect_rendered({"hatch_sess": "secret"}, "https://muse.ai/thread/abc",
                                        expect="video", account_id="account-b", timeout=20)
            except MuseGenerationError as exc:
                assert "chưa có video" in str(exc)
                assert page.navs == ["https://muse.ai/thread/abc"]
                assert os.listdir(engine.cfg.media_dir) == []
            else:
                raise AssertionError("poster image was saved as a video")
            finally:
                engine_module.time.time = original_time

            engine.attachments = lambda: [{"tid": "video", "hasVideo": True,
                                           "src": "https://cdn.example/page.mp4",
                                           "vSrc": "https://cdn.example/page.mp4", "w": 480, "h": 480}]
            engine.extract_bytes = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("page player was downloaded"))
            clock = {"now": 1000.0}

            def later_again():
                clock["now"] += 30
                return clock["now"]

            engine_module.time.time = later_again
            try:
                engine.collect_rendered({"hatch_sess": "secret"}, "https://muse.ai/thread/abc",
                                        expect="video", account_id="account-b", timeout=20)
            except MuseGenerationError as exc:
                assert "chưa có video" in str(exc)
                assert os.listdir(engine.cfg.media_dir) == []
            else:
                raise AssertionError("page player was treated as the thread video")
            finally:
                engine_module.time.time = original_time

            def thread_attachments():
                return [{"tid": "video", "hasVideo": True, "vSrc": "https://cdn.example/page.mp4",
                         "src": "https://cdn.example/page.mp4"},
                        {"tid": "thread-video", "hasVideo": True,
                         "vSrc": "https://cdn.example/scene.mp4",
                         "src": "https://cdn.example/scene.mp4"}]

            extracted_thread = {}

            def extract_thread(src, expect="image", retries=4):
                extracted_thread["src"] = src
                return b"scene-video", "video/mp4", src

            engine.attachments = thread_attachments
            engine._resource_videos = lambda: [{
                "tid": "thread-video", "hasVideo": True,
                "vSrc": "https://muse.ai/videos/avatars/hatch.mp4",
                "src": "https://muse.ai/videos/avatars/hatch.mp4", "w": 0, "h": 0,
            }]
            engine.extract_bytes = extract_thread
            result = engine.collect_rendered(
                {"hatch_sess": "secret"}, "https://muse.ai/thread/abc",
                expect="video", account_id="account-b")
            assert extracted_thread["src"] == "https://cdn.example/scene.mp4"
            assert open(result["path"], "rb").read() == b"scene-video"

            engine.attachments = lambda: [{
                "tid": "hatch-chat-attachment-presentation-video", "hasVideo": True,
                "src": "blob:https://muse.ai/page", "vSrc": "blob:https://muse.ai/page",
                "w": 480, "h": 480,
            }]
            assert engine._rendered_media("video") is None
            engine.attachments = lambda: [{
                "tid": "hatch-chat-attachment-presentation-video", "hasVideo": True,
                "src": "blob:https://muse.ai/scene", "vSrc": "blob:https://muse.ai/scene",
                "w": 1280, "h": 720,
            }]
            picked = engine._rendered_media("video")
            assert picked["vSrc"] == "blob:https://muse.ai/scene"

            try:
                engine.collect_rendered({"hatch_sess": "secret"}, "https://evil.test/thread/abc")
            except ValueError:
                assert page.navs and all(item == "https://muse.ai/thread/abc" for item in page.navs)
            else:
                raise AssertionError("unsafe thread URL was opened")
    finally:
        engine_module.time.sleep = original_sleep
    print("PASS: rendered video is downloaded from its own thread without sending the prompt or saving a poster.")


if __name__ == "__main__":
    main()
