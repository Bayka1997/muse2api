"""Run: python tests/test_session_views.py. No Chrome, network or accounts."""
from __future__ import annotations

import base64
import ast
from pathlib import Path
import sys
import threading
import time
import types
import unittest

# Execute the complete production module with its external transport substituted,
# so these tests also run on a machine without requests or websocket-client.
SOURCE = Path(__file__).resolve().parents[1] / "session_views.py"
tree = ast.parse(SOURCE.read_text())
tree.body = [node for node in tree.body
             if not (isinstance(node, ast.ImportFrom) and node.module == "cdp")]
module = types.ModuleType("_tested_session_views")
module.CDP = object
sys.modules[module.__name__] = module
exec(compile(tree, str(SOURCE), "exec"), module.__dict__)
TaskSessionViews = module.TaskSessionViews


JPEG = b"\xff\xd8fake-jpeg\xff\xd9"


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeCDP:
    def __init__(self, owner, url, timeout):
        self.owner = owner
        self.url = url
        self.timeout = timeout
        self.calls = []
        self.closed = False

    def send(self, method, params=None, timeout=None):
        assert not self.closed
        self.calls.append((method, params, timeout))
        if self.owner.failure:
            raise RuntimeError("Target no longer exists")
        if method == "Page.bringToFront":
            return {"result": {}}
        if method == "Page.captureScreenshot":
            with self.owner.lock:
                self.owner.inflight += 1
                self.owner.peak = max(self.owner.peak, self.owner.inflight)
            try:
                if self.owner.delay:
                    time.sleep(self.owner.delay)
                return self.owner.response
            finally:
                with self.owner.lock:
                    self.owner.inflight -= 1
        if method == "Page.getLayoutMetrics":
            return {"result": {"cssLayoutViewport": {"clientWidth": 1000, "clientHeight": 800}}}
        if method == "Input.dispatchMouseEvent":
            return {"result": {}}
        raise AssertionError("Unexpected CDP command: " + method)

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self):
        self.connections = []
        self.response = {"result": {"data": base64.b64encode(JPEG).decode()}}
        self.failure = False
        self.delay = 0
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0

    def __call__(self, url, timeout):
        cdp = FakeCDP(self, url, timeout)
        self.connections.append(cdp)
        return cdp


class TaskSessionViewTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.factory = FakeFactory()
        self.disposed = []
        self.manager = TaskSessionViews(19210, self.disposed.append,
                                      cdp_factory=self.factory, clock=self.clock,
                                      ttl=10, max_retained=2)

    def tearDown(self):
        self.manager.close()

    def bind(self, name):
        return self.manager.bind("task-" + name, "target-" + name,
                                 "context-" + name, "account-" + name)

    def test_exact_target_account_and_no_login_data(self):
        self.bind("a")
        self.bind("b")
        self.assertEqual(self.manager.info("task-a")["account_id"], "account-a")
        self.assertEqual(self.manager.info("task-b")["target_id"], "target-b")
        self.assertEqual(self.manager.screenshot("task-b"), JPEG)
        self.assertEqual(self.factory.connections[0].url,
                         "ws://127.0.0.1:19210/devtools/page/target-b")
        self.assertEqual([call[0] for call in self.factory.connections[0].calls],
                         ["Page.bringToFront", "Page.captureScreenshot"])
        self.assertTrue(self.manager.owns_context("context-a"))
        self.assertEqual(len(self.factory.connections), 1)
        self.assertNotIn("cookies", self.manager.info("task-a"))
        self.assertNotIn("ws_url", self.manager.info("task-a"))
        _, params, _ = self.factory.connections[0].calls[1]
        self.assertEqual(params, {"format": "jpeg", "quality": 70,
                                  "captureBeyondViewport": False})
        with self.assertRaises(FileNotFoundError):
            self.manager.screenshot("missing")
        self.assertEqual(len(self.factory.connections), 1)

    def test_invalid_or_shared_target_is_rejected(self):
        for target in ("../other", "target?token=secret", "", "target/other"):
            with self.assertRaises(ValueError):
                self.manager.bind("task-a", target, "context-a", "account-a")
        with self.assertRaises(ValueError):
            self.manager.bind("task-a", "target-a", "", "account-a")
        self.bind("a")
        with self.assertRaises(ValueError):
            self.manager.bind("task-b", "target-b", "context-a", "account-b")
        self.assertEqual(self.factory.connections, [])

    def test_finished_context_expires_while_active_context_survives(self):
        self.bind("a")
        self.bind("b")
        self.manager.screenshot("task-a")
        connection = self.factory.connections[0]
        self.assertTrue(self.manager.finish("task-a"))
        self.assertFalse(self.manager.finish("never-bound"))
        info = self.manager.info("task-a")
        self.assertFalse(info["active"])
        self.assertTrue(info["retained"])
        self.assertEqual(info["expires_in"], 10)
        self.clock.now += 10
        self.assertEqual(self.manager.prune(), 1)
        self.assertTrue(connection.closed)
        self.assertEqual(self.disposed, ["context-a"])
        self.assertTrue(self.manager.owns_context("context-b"))
        self.assertFalse(self.manager.owns_context("context-a"))
        with self.assertRaises(FileNotFoundError):
            self.manager.info("task-a")

    def test_oldest_terminal_context_evicted_and_finish_does_not_extend_ttl(self):
        self.bind("active")
        for name in ("a", "b", "c"):
            self.bind(name)
            self.manager.finish("task-" + name)
            self.clock.now += 1
        self.assertEqual(self.disposed, ["context-a"])
        self.assertTrue(self.manager.owns_context("context-active"))
        self.assertTrue(self.manager.owns_context("context-b"))
        expires_in = self.manager.info("task-b")["expires_in"]
        self.manager.finish("task-b")
        self.assertEqual(self.manager.info("task-b")["expires_in"], expires_in)

    def test_bind_replacement_disposes_only_previous_context(self):
        self.bind("a")
        self.manager.screenshot("task-a")
        old_connection = self.factory.connections[0]
        self.manager.bind("task-a", "target-retry", "context-retry", "account-b")
        self.assertEqual(self.disposed, ["context-a"])
        self.assertTrue(old_connection.closed)
        self.assertEqual(self.manager.info("task-a")["account_id"], "account-b")
        self.manager.screenshot("task-a")
        self.assertTrue(self.factory.connections[-1].url.endswith("/target-retry"))
        self.manager.bind("task-a", "target-retry", "context-retry", "account-b")
        self.assertEqual(self.disposed, ["context-a"])
        self.manager.bind("task-a", "target-next", "context-retry", "account-b")
        self.assertEqual(self.disposed, ["context-a"])

    def test_capture_failure_does_not_fall_back_to_another_target(self):
        self.bind("a")
        self.bind("b")
        self.factory.failure = True
        with self.assertRaises(RuntimeError) as caught:
            self.manager.screenshot("task-a")
        self.assertNotIn("ws://", str(caught.exception))
        self.assertEqual(len(self.factory.connections), 1)
        self.assertTrue(self.factory.connections[0].url.endswith("/target-a"))
        self.assertTrue(self.factory.connections[0].closed)
        self.factory.failure = False
        self.assertEqual(self.manager.screenshot("task-a"), JPEG)
        self.assertTrue(self.factory.connections[-1].url.endswith("/target-a"))

    def test_capture_bytes_are_validated_and_size_limited(self):
        self.bind("a")
        for response in ({}, {"result": {"data": "invalid###"}},
                         {"result": {"data": base64.b64encode(b"not JPEG").decode()}},
                         {"result": {"data": "A" * (12 << 20)}}):
            self.factory.response = response
            with self.assertRaises(RuntimeError):
                self.manager.screenshot("task-a")

    def test_scroll_is_terminal_only_and_bounded(self):
        self.bind("a")
        with self.assertRaises(PermissionError):
            self.manager.scroll("task-a", 50)
        self.assertEqual(self.factory.connections, [])
        self.manager.finish("task-a")
        self.manager.scroll("task-a", 10000)
        _, params, _ = self.factory.connections[0].calls[-1]
        self.assertEqual(params, {"type": "mouseWheel", "x": 500.0,
                                  "y": 400.0, "deltaX": 0, "deltaY": 2400})
        for invalid in (True, "50", float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.manager.scroll("task-a", invalid)

    def test_same_target_requests_serialized_different_targets_independent(self):
        self.bind("a")
        self.bind("b")
        self.factory.delay = 0.02
        errors = []
        barrier = threading.Barrier(6)

        def capture(task):
            try:
                barrier.wait()
                self.assertEqual(self.manager.screenshot(task), JPEG)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=capture, args=("task-a",)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.factory.peak, 1)
        self.assertEqual(len(self.factory.connections), 1)
        self.factory.peak = 0
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=capture, args=(task,)) for task in ("task-a", "task-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.factory.peak, 2)

    def test_forget_and_close_dispose_contexts_once(self):
        self.bind("a")
        self.bind("b")
        self.assertTrue(self.manager.forget("task-a"))
        self.assertFalse(self.manager.forget("task-a"))
        self.manager.close()
        self.manager.close()
        self.assertCountEqual(self.disposed, ["context-a", "context-b"])
        self.assertFalse(self.manager.owns_context("context-b"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
