"""Offline regressions for MUSE page readiness and video delivery waits."""
import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import types
fake_cdp = types.ModuleType("cdp")
fake_cdp.CDP = object
fake_cdp.http_json = lambda *args, **kwargs: {}
sys.modules["cdp"] = fake_cdp
spec = importlib.util.spec_from_file_location("muse_engine_candidate", ROOT / "engine.py")
engine_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine_module)

tree = ast.parse((ROOT / "engine.py").read_text(encoding="utf-8"))
method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_wait_ws_ready")
expression = next(n.args[0].value for n in ast.walk(method)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "js")

for name, connecting, hydrated, disabled, expected in (
        ("empty_connected", False, True, False, "ready"),
        ("connecting", True, True, False, "connecting"),
        ("hydrating", False, False, False, "hydrating"),
        ("disabled_input", False, True, True, "input-disabled")):
    script = """const c=%s;const ta={disabled:c.disabled};const document={readyState:'complete',body:{innerText:c.connecting?'Connecting...':''},querySelector:s=>s==='textarea'?ta:s==='[data-hatch-shell-hydration-state]'?{getAttribute:()=>c.hydrated?'hydrated':'server'}:null};console.log(%s);""" % (
        json.dumps(dict(connecting=connecting, hydrated=hydrated, disabled=disabled)), expression)
    result = subprocess.check_output(["node", "-e", script], text=True).strip()
    assert result == expected, (name, result, expected)

with tempfile.TemporaryDirectory() as profile:
    engine = engine_module.MuseEngine(SimpleNamespace(profile_dir=profile, login_wait=2))
    clock = [0.0]
    engine_module.time = SimpleNamespace(time=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0] + n))
    engine.page = SimpleNamespace(js=lambda _: json.dumps({
        "cnt": 0, "txt": "", "stop": True, "tail": "Still sending"}))
    engine.attachments = lambda: []
    try:
        engine._wait_attachment("", 60, "video")
    except engine_module.MuseGenerationError as exc:
        assert "未成功送达" in str(exc)
        assert clock[0] < 32, clock[0]
    else:
        raise AssertionError("undelivered video request waited for the full timeout")

clock[0] = 0.0
engine.page = SimpleNamespace(js=lambda _: json.dumps({
    "cnt": 1, "txt": "Noted. Leaving this here while the shot is prepared.",
    "stop": False, "tail": ""}))
engine._scroll_bottom = lambda: None
engine.attachments = lambda: (
    [{"src": "scene.mp4", "vSrc": "scene.mp4", "tid": "video", "hasVideo": True, "w": 32, "h": 32}]
    if clock[0] >= 20 else [])
kept = engine._wait_attachment("", 60, "video", base_agent_cnt=0, base_att_cnt=0)
assert kept["vSrc"] == "scene.mp4", kept
assert clock[0] >= 20, clock[0]

clock[0] = 0.0
engine.page = SimpleNamespace(js=lambda _: json.dumps({
    "cnt": 1, "txt": "I cannot create that video because it violates the content policy.",
    "stop": False, "tail": ""}))
engine.attachments = lambda: []
missed = engine._wait_attachment("", 60, "video", base_agent_cnt=0, base_att_cnt=0)
assert missed is None
assert clock[0] >= 60, clock[0]

clock[0] = 0.0
try:
    engine._wait_attachment("", 60, "image", base_agent_cnt=0, base_att_cnt=0)
except engine_module.MuseGenerationError as exc:
    assert "仅返回文本" in str(exc)
    assert clock[0] < 20, clock[0]
else:
    raise AssertionError("a finished image text refusal should still fail fast")

print("PASS: composer readiness and undelivered video fast-fail")
