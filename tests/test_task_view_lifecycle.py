"""Run: python tests/test_task_view_lifecycle.py. Fake CDP; no runtime packages."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
import threading
import time
import types
import unittest
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def session_manager_class():
    path = ROOT / "session_views.py"
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ImportFrom) and node.module == "cdp")]
    module = types.ModuleType("_lifecycle_session_views")
    module.CDP = object
    sys.modules[module.__name__] = module
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module.TaskSessionViews


TaskSessionViews = session_manager_class()


class GenerationError(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


engine_tree = ast.parse((ROOT / "engine.py").read_text())
original_class = next(node for node in engine_tree.body
                      if isinstance(node, ast.ClassDef) and node.name == "MuseEngine")
methods = {"_ensure_local", "page", "current_acc_id", "current_context_id", "current_target_id",
           "_dispose_view_context", "bind_task_view", "_close_page", "close_current_page",
           "_open_page", "stop", "generate", "generation_context"}
engine_class = ast.ClassDef(name="MuseEngine", bases=[], keywords=[], decorator_list=[],
                           body=[node for node in original_class.body
                                 if isinstance(node, ast.FunctionDef) and node.name in methods])
log = types.SimpleNamespace(warning=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None)
namespace = {"threading": threading, "time": time, "re": re, "urlsplit": urlsplit,
             "urlunsplit": urlunsplit, "log": log, "CDP": object,
             "MuseGenerationError": GenerationError}
exec(compile(ast.fix_missing_locations(ast.Module(body=[engine_class], type_ignores=[])),
             "<production engine lifecycle>", "exec"), namespace)
MuseEngine = namespace["MuseEngine"]


class Page:
    def __init__(self, url="https://muse.ai/thread/fixture"):
        self.url = url
        self.closed = False

    def js(self, expression):
        if expression == "window.location.href":
            return self.url
        raise AssertionError("Unexpected page JavaScript")

    def close(self):
        self.closed = True


class Browser:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.fail_target_creation = False

    def send(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == "Target.createBrowserContext":
            return {"result": {"browserContextId": "partial-context"}}
        if method == "Target.createTarget" and self.fail_target_creation:
            raise RuntimeError("Unable to create isolated target")
        if method == "Target.disposeBrowserContext":
            return {"result": {}}
        raise AssertionError("Unexpected browser command " + method)

    def close(self):
        self.closed = True


def make_engine():
    engine = MuseEngine()
    engine._local = threading.local()
    engine._start_lock = threading.Lock()
    engine._contexts_lock = threading.Lock()
    engine._active_contexts = set()
    engine.browser = Browser()
    engine.proc = None
    engine.cfg = types.SimpleNamespace(cdp_port=19210)
    engine.task_views = TaskSessionViews(19210, engine._dispose_view_context)
    return engine


def attach_page(engine, suffix="a", account_id=None):
    page = Page("https://muse.ai/thread/thread-" + suffix)
    engine.page = page
    engine.current_context_id = "context-" + suffix
    engine.current_acc_id = account_id or "account-" + suffix
    engine._local.target_id = "target-" + suffix
    engine._active_contexts.add("context-" + suffix)
    return page


def disposed_contexts(browser):
    return [params["browserContextId"] for method, params in browser.calls
            if method == "Target.disposeBrowserContext"]


class TaskViewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()

    def tearDown(self):
        self.engine.task_views.close()

    def test_registered_context_retained_worker_socket_closed(self):
        page = attach_page(self.engine)
        self.engine._local.task_view_id = "task-a"
        self.engine.bind_task_view()
        self.engine.close_current_page()
        self.assertTrue(page.closed)
        self.assertIsNone(self.engine.page)
        self.assertIsNone(self.engine.current_context_id)
        self.assertIsNone(self.engine.current_acc_id)
        self.assertIsNone(self.engine.current_target_id)
        self.assertTrue(self.engine.task_views.owns_context("context-a"))
        self.assertEqual(disposed_contexts(self.engine.browser), [])
        self.assertEqual(self.engine._active_contexts, {"context-a"})
        self.assertEqual(self.engine.task_views.info("task-a")["target_id"], "target-a")

    def test_unregistered_context_disposed(self):
        page = attach_page(self.engine)
        self.engine.close_current_page()
        self.assertTrue(page.closed)
        self.assertEqual(disposed_contexts(self.engine.browser), ["context-a"])
        self.assertEqual(self.engine._active_contexts, set())
        self.assertIsNone(self.engine.current_context_id)

    def test_default_context_target_cannot_be_bound(self):
        attach_page(self.engine)
        self.engine.current_context_id = None
        self.engine._local.task_view_id = "task-a"
        self.engine.bind_task_view()
        with self.assertRaises(FileNotFoundError):
            self.engine.task_views.info("task-a")

    def test_failed_isolated_creation_cleans_partial_context_without_fallback(self):
        self.engine._local.task_view_id = "task-a"
        self.engine.browser.fail_target_creation = True
        with self.assertRaises(GenerationError):
            self.engine._open_page()
        self.assertEqual(disposed_contexts(self.engine.browser), ["partial-context"])
        self.assertIsNone(self.engine.current_context_id)
        self.assertEqual(self.engine._active_contexts, set())
        self.assertIsNone(self.engine.current_target_id)
        with self.assertRaises(FileNotFoundError):
            self.engine.task_views.info("task-a")

    def test_viewer_without_browser_fails_before_any_shared_context_fallback(self):
        self.engine.browser = None
        self.engine._local.task_view_id = "task-a"
        with self.assertRaises(GenerationError):
            self.engine._open_page()

    def test_stop_disposes_retained_and_unregistered_contexts(self):
        browser = self.engine.browser
        self.engine.task_views.bind("task-a", "target-a", "context-a", "account-a")
        self.engine.task_views.bind("task-b", "target-b", "context-b", "account-b")
        self.engine._active_contexts.update(("context-a", "context-b", "context-other"))
        self.engine.task_views.finish("task-a")
        self.engine.stop()
        self.assertCountEqual(disposed_contexts(browser), ["context-a", "context-b", "context-other"])
        self.assertTrue(browser.closed)
        self.assertIsNone(self.engine.browser)
        self.assertEqual(self.engine._active_contexts, set())
        with self.assertRaises(FileNotFoundError):
            self.engine.task_views.info("task-b")

    def test_failed_generation_retry_records_urls_and_retains_exact_last_account(self):
        accounts = [
            {"id": "account-a", "label": "Muse A", "cookies": {}},
            {"id": "account-b", "label": "Muse B", "cookies": {}},
        ]
        acquired = iter(accounts)
        released = []
        pages = []
        resets = []
        snapshots = []
        marks = []

        def ensure_page(cookies, expires, account_id):
            suffix = account_id.removeprefix("account-")
            pages.append(attach_page(self.engine, suffix, account_id))
            return pages[-1]

        def wait_attachment(*args, **kwargs):
            info = self.engine.task_views.info("task-video")
            self.assertEqual(info["account_id"], self.engine.current_acc_id)
            self.assertEqual(info["target_id"], self.engine.current_target_id)
            raise GenerationError("Video generation timed out")

        self.engine.start = lambda: None
        self.engine.ensure_page = ensure_page
        self.engine.reset_thread = lambda **kwargs: resets.append(self.engine.current_target_id)
        self.engine._scroll_bottom = lambda: None
        self.engine._clear_attachments = lambda: None
        self.engine.attachments = lambda: []
        self.engine._agent_count = lambda: 0
        self.engine._send = lambda prompt: "clicked"
        self.engine._wait_attachment = wait_attachment
        semaphore = types.SimpleNamespace(acquire=lambda **kwargs: True,
                                         release=lambda: released.append("semaphore"))
        namespace = {
            "time": time, "engine": self.engine, "GEN_LOCK": semaphore,
            "store": types.SimpleNamespace(tasks={}, mark=lambda *args: marks.append(args)),
            "ACCOUNT_POOL": types.SimpleNamespace(acquire=lambda **kwargs: next(acquired),
                                                  release=released.append),
            "_renew_and_persist": lambda *args, **kwargs: None,
            "_sync_cookies": lambda *args: None,
            "MuseAuthError": AuthError, "MuseGenerationError": GenerationError, "log": log,
        }
        app_tree = ast.parse((ROOT / "app.py").read_text())
        functions = [node for node in app_tree.body
                     if isinstance(node, ast.FunctionDef)
                     and node.name in ("_run_generation", "_run_generation_locked")]
        exec(compile(ast.Module(body=functions, type_ignores=[]), "<production generation wrapper>", "exec"),
             namespace)
        with self.assertRaisesRegex(GenerationError, "timed out"):
            namespace["_run_generation"]("prompt", "video", 30,
                                         on_context=snapshots.append, task_id="task-video")
        info = self.engine.task_views.info("task-video")
        self.assertEqual(info["account_id"], "account-b")
        self.assertEqual(info["target_id"], "target-b")
        self.assertTrue(info["retained"])
        self.assertFalse(info["active"])
        self.assertEqual(disposed_contexts(self.engine.browser), ["context-a"])
        self.assertTrue(all(page.closed for page in pages))
        self.assertEqual(resets, ["target-a", "target-b"])
        self.assertIn({"muse_url": "https://muse.ai/thread/thread-a"}, snapshots)
        self.assertEqual(snapshots[-1], {"muse_url": "https://muse.ai/thread/thread-b"})
        self.assertEqual(released, ["account-a", "account-b", "semaphore"])
        self.assertIsNone(self.engine._local.task_view_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
