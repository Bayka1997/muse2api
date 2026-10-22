"""muse2api — 把 Muse(muse.ai) 网页免费账号的对话/生图/生视频额度反代成 API。

OpenAI 兼容:
  POST /v1/chat/completions          （文本 / 代码对话，支持 stream，可接 Codex）
  POST /v1/images/generations
  POST /v1/videos  +  GET /v1/videos/{task_id}
  GET  /v1/models
  GET  /v1/media/{name}

管理（前端账号池管理页面在 GET /）:
  GET    /admin/status
  GET    /admin/accounts
  POST   /admin/accounts            （单条 / 批量文本 / 批量数组）
  PATCH  /admin/accounts/{id}       （改标签、启用/禁用）
  DELETE /admin/accounts/{id}
  POST   /admin/accounts/{id}/test  （真实打开 muse.ai 验证会话是否有效）
  POST   /admin/accounts/{id}/relogin
  GET    /admin/tasks               （任务记录）
  DELETE /admin/tasks/{id}  |  POST /admin/tasks/clear
  GET    /admin/media  |  POST /admin/media/delete  |  DELETE /admin/media/{name}
  GET    /admin/extension           （浏览器扩展 zip，用来取 cookie）
  GET    /admin/cookie-helper       （命令行取 cookie 脚本，进阶）
"""
from __future__ import annotations

from typing import Any

import asyncio
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
import io
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
import zipfile
from fastapi import Body, Depends, FastAPI, Form, Header, HTTPException, Request, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import BaseModel, Field

from config import CFG
from engine import (ESSENTIAL_COOKIES, MuseAuthError,
                    MuseEngine, MuseGenerationError, file_is_page_clip,
                    missing_essential_cookies)
from store import Store, account_expiry, min_expiry

import sys
log = logging.getLogger("muse2api")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)

CFG.ensure_dirs()
# Keep multipart upload spools and processing scratch files beside the app data.
# The default Windows TEMP drive may be much smaller than the project's data drive.
tempfile.tempdir = CFG.data_dir
app = FastAPI(title="muse2api", version="1.5.4")
_ASSEMBLY_DOWNLOAD_LOCK = threading.Lock()
_ASSEMBLY_DOWNLOADS: dict[str, tuple[str, float]] = {}
_ASSEMBLY_JOBS: dict[str, dict] = {}


def _assembly_state_path(kind: str, key: str) -> str | None:
    """Shared, atomic state files keep assembly jobs visible across workers."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", str(key or "")):
        return None
    directory = os.path.join(CFG.data_dir, "video-assembly-state", kind)
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{key}.json")


def _write_assembly_state(kind: str, key: str, value: dict) -> None:
    path = _assembly_state_path(kind, key)
    if not path:
        return
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _read_assembly_state(kind: str, key: str) -> dict | None:
    path = _assembly_state_path(kind, key)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None

# Cookie 助手脚本从 muse.ai 页面发起导入请求，需要放行该来源；
# 浏览器扩展从 chrome-extension:// 发起，也一并放行。
#
# 这里直接放行所有来源：本服务用 Bearer Key 鉴权、不依赖 Cookie，
# 放行来源不会带来越权风险；反之如果把来源限死，浏览器端的智能体
# （Open WebUI / LobeChat / 各种 Web 客户端）会被 CORS 拦住用不了。
_origins = [o.strip() for o in (CFG.cors_origins or "").split(",") if o.strip()]
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["*"],
                   expose_headers=["*"],
                   max_age=600)

store = Store(CFG)
engine = MuseEngine(CFG)


class ConcurrencyLock:
    """多账号并发调度锁。
    由 CFG.max_concurrency 限制最大并发槽位；
    同时提供与标准 Lock 兼容的 acquire / release / locked 接口，
    确保向下兼容历史单锁断言与管理端操作。"""
    def __init__(self, limit: int = 2, stop_when_idle: bool = False):
        self.limit = max(1, limit)
        self.stop_when_idle = stop_when_idle
        self._cond = threading.Condition()
        self._active_count = 0
        self._stopping = False

    def set_limit(self, limit: int):
        with self._cond:
            self.limit = max(1, int(limit))
            self._cond.notify_all()

    def acquire(self, blocking: bool = True, timeout: float | None = -1) -> bool:
        if timeout is None:
            timeout = -1
        deadline = None if timeout < 0 else time.monotonic() + timeout
        with self._cond:
            while self._active_count >= self.limit or self._stopping:
                if not blocking:
                    return False
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._active_count += 1
            return True

    def release(self):
        with self._cond:
            if self._active_count > 0:
                self._active_count -= 1
            stop = self.stop_when_idle and self._active_count == 0
            if stop:
                self._stopping = True
            else:
                self._cond.notify_all()
        try:
            if stop:
                engine.stop()
        except Exception:
            log.warning("Failed to stop idle Chromium", exc_info=True)
        finally:
            if stop:
                with self._cond:
                    self._stopping = False
                    self._cond.notify_all()

    def locked(self) -> bool:
        with self._cond:
            return self._active_count > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class AccountPoolManager:
    """Mỗi tài khoản có một hạn mức riêng. Tài khoản này đầy slot không chặn tài khoản khác."""
    def __init__(self, store_instance, per_account: int = 10):
        self.store = store_instance
        self.per_account = max(1, per_account)
        self.busy_counts: dict[str, int] = {}
        self.cond = threading.Condition()

    @property
    def busy_accounts(self) -> set[str]:
        return {aid for aid, count in self.busy_counts.items() if count > 0}

    def _slots(self, account_id: str) -> int:
        return self.busy_counts.get(account_id, 0)

    def _live(self) -> list[dict]:
        refs = getattr(self.store, "list_usable_account_refs", None)
        if refs:
            return refs()
        return [a for a in self.store.accounts if a.get("enabled", True) and a.get("cookies")]

    def _take(self, account_id: str):
        self.busy_counts[account_id] = self._slots(account_id) + 1

    def _choose(self, preferred_id: str | None, force_rotate: bool) -> str | None:
        live = self._live()
        open_accounts = [a for a in live if self._slots(a["id"]) < self.per_account]
        if not open_accounts:
            return None
        if force_rotate and preferred_id and any(a["id"] != preferred_id for a in open_accounts):
            open_accounts = [a for a in open_accounts if a["id"] != preferred_id]
        open_accounts.sort(key=lambda a: (self._slots(a["id"]), a.get("last_used") or 0, a["id"]))
        return open_accounts[0]["id"]

    def acquire(self, account_id: str | None = None, preferred_id: str | None = None,
                rotate: bool = True, force_rotate: bool = False,
                timeout: float = 60.0, record_use: bool = True,
                allow_overflow: bool = False) -> dict:
        del rotate  # Việc chia đều dựa trên số video đang chạy, không dồn vào một cookie.
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    live = self._live()
                    full = sum(1 for a in live if self._slots(a["id"]) >= self.per_account)
                    raise MuseGenerationError(
                        f"Hết thời gian chờ: {full}/{len(live)} tài khoản đang chạy đủ {self.per_account} video.")

                if account_id:
                    acc = self.store.get_account(account_id)
                    if not acc or not acc.get("enabled", True):
                        raise MuseAuthError(f"指定账号 {account_id} 不存在或已禁用")
                    missing = missing_essential_cookies(acc.get("cookies") or {})
                    if missing:
                        raise MuseAuthError("Selected account is missing essential cookies: " + ", ".join(missing))
                    if self._slots(account_id) < self.per_account or allow_overflow:
                        self._take(account_id)
                        return acc
                else:
                    chosen = self._choose(preferred_id, force_rotate)
                    if chosen:
                        acc = self.store.pick_account(
                            preferred_id=chosen, rotate=False, force_rotate=False, record_use=record_use)
                        if not acc or acc["id"] != chosen:
                            acc = self.store.get_account(chosen)
                        if acc and self._slots(acc["id"]) < self.per_account:
                            self._take(acc["id"])
                            return acc

                if not self._live():
                    invalid = [a for a in self.store.list_accounts()
                               if a.get("enabled", True) and a.get("cookie_count")
                               and not a.get("essential_ok")]
                    if invalid:
                        raise MuseAuthError("Enabled accounts are missing essential cookies; re-import complete cookies")
                    raise MuseAuthError("没有可用账号，请先在管理页面导入 cookie")

                self.cond.wait(timeout=min(remaining, 0.5))

    def release(self, account_id: str | None):
        if not account_id:
            return
        with self.cond:
            count = self.busy_counts.get(account_id, 0)
            if count <= 1:
                self.busy_counts.pop(account_id, None)
            else:
                self.busy_counts[account_id] = count - 1
            self.cond.notify_all()


GEN_LOCK = ConcurrencyLock(limit=CFG.max_concurrency, stop_when_idle=True)
ACCOUNT_POOL = AccountPoolManager(store, per_account=CFG.per_account_concurrency)
IMAGE_TASK_LOCK = threading.Lock()
TASK_EXECUTOR = ThreadPoolExecutor(max_workers=10,
                                   thread_name_prefix="muse-task")
TASK_CANCEL_EVENTS: dict[str, threading.Event] = {}
TASK_CANCEL_LOCK = threading.RLock()
CHAT_STREAM_LIMIT = threading.BoundedSemaphore(20)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_CONCURRENCY = 10


def _register_task_cancel(tid: str) -> threading.Event:
    event = threading.Event()
    with TASK_CANCEL_LOCK:
        TASK_CANCEL_EVENTS[tid] = event
    return event


def _forget_task_cancel(tid: str):
    with TASK_CANCEL_LOCK:
        TASK_CANCEL_EVENTS.pop(tid, None)


def _task_cancel_requested(tid: str) -> bool:
    with TASK_CANCEL_LOCK:
        event = TASK_CANCEL_EVENTS.get(tid)
    return bool(event and event.is_set())


# ------------------------- OpenAI 风格的错误响应 -------------------------
# 各类智能体基本都按 OpenAI 的 {"error": {"message": ...}} 取错误信息；
# FastAPI 默认返回的是 {"detail": ...}，客户端会读不到原因、只显示"未知错误"。
# 所以 /v1/* 统一转成 OpenAI 格式，管理接口保持原样（前端依赖 detail）。
def _err_type(status: int) -> str:
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"error": {
            "message": "请求参数校验失败：" + str(exc.errors())[:400],
            "type": "invalid_request_error", "param": None, "code": 422}})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

MODELS = [
    {"id": "muse-spark", "object": "model", "owned_by": "muse",
     "description": "Muse Spark —— 文本 / 代码对话（网页免费额度，支持流式）"},
    {"id": "muse-image", "object": "model", "owned_by": "muse",
     "description": "Muse Image —— 文生图 / 图像编辑（网页免费额度）"},
    {"id": "muse-video", "object": "model", "owned_by": "muse",
     "description": "Muse Video —— 文生视频 / 图生视频（网页免费额度）"},
]

# 下游（Codex / Cline / 各种客户端）习惯按 OpenAI、Anthropic 的名字传模型，
# 这里统一映射到 muse 的真实能力上。
#
# 说明：muse.ai 网页是自动路由的 agent，对外**没有可枚举的模型清单**，
# 能稳定调用的就是三条真实能力 —— Muse Spark（语言/代码）、
# Muse Image（生图）、Muse Video（生视频）。别名只是让下游不用改配置。
MODEL_ALIASES = {
    # ---- 文本 / 代码 → muse-spark ----
    "muse-text": "muse-spark", "muse-chat": "muse-spark", "muse-llm": "muse-spark",
    "koda": "muse-spark",
    "gpt-3.5-turbo": "muse-spark", "gpt-4": "muse-spark", "gpt-4-turbo": "muse-spark",
    "gpt-4o": "muse-spark", "gpt-4o-mini": "muse-spark", "gpt-4.1": "muse-spark",
    "gpt-4.1-mini": "muse-spark", "gpt-5": "muse-spark", "gpt-5-codex": "muse-spark",
    "o1": "muse-spark", "o1-mini": "muse-spark", "o3": "muse-spark",
    "o3-mini": "muse-spark", "o4-mini": "muse-spark",
    "codex": "muse-spark", "codex-mini-latest": "muse-spark",
    "claude-3-5-sonnet": "muse-spark", "claude-3-5-sonnet-latest": "muse-spark",
    "claude-3-7-sonnet": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-opus": "muse-spark",
    "claude-3-haiku": "muse-spark",
    "deepseek-chat": "muse-spark", "deepseek-coder": "muse-spark",
    "deepseek-reasoner": "muse-spark", "qwen-coder": "muse-spark",
    "gemini-2.5-pro": "muse-spark", "gemini-2.5-flash": "muse-spark",
    # ---- 生图 → muse-image ----
    "muse-img": "muse-image", "dall-e": "muse-image", "dall-e-3": "muse-image",
    "gpt-image-1": "muse-image", "flux": "muse-image", "midjourney": "muse-image",
    # ---- 生视频 → muse-video ----
    "muse-vid": "muse-video", "muse-videos": "muse-video",
    "sora": "muse-video", "sora-2": "muse-video", "veo": "muse-video",
    "veo-3": "muse-video", "kling": "muse-video", "runway": "muse-video",
}


def resolve_model(name: str | None, default: str = "muse-image") -> str:
    n = (name or "").strip().lower()
    return MODEL_ALIASES.get(n, n or default)


# ------------------------- 鉴权 -------------------------
def auth(authorization: str | None = Header(default=None)):
    if not CFG.api_key:
        return True
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <key>")
    parts = authorization.split(None, 1)
    token = parts[1].strip() if len(parts) > 1 else ""
    if not token or token != CFG.api_key:
        raise HTTPException(401, "API key 无效")
    return True


# ------------------------- 请求模型 -------------------------

def _renew_and_persist(acc_id: str, wake_vm: bool = True, force: bool = False) -> dict | None:
    """调用 /api/session 续签账号 cookie 并写回 store，返回最新账号 dict。
    近期（10分钟内）已续签且状态正常的账号直接复用，避免每次请求阻塞 2~3 秒 HTTP 往返。"""
    acc = store.get_account(acc_id)
    if not acc or not acc.get("cookies"):
        return acc
    now = time.time()
    last_sync = max(
        int(acc.get("synced_at") or 0),
        int(acc.get("last_keepalive") or 0),
        int(engine._last_http_renew.get(acc_id, 0)),
    )
    if not force and acc.get("ok") is True and (now - last_sync) < 600:
        engine._last_http_renew[acc_id] = last_sync
        return acc
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=wake_vm)
        engine._last_http_renew[acc_id] = now
        if res.get("cookies"):
            store.update_account(acc_id, cookies=res["cookies"],
                                 cookies_exp=res.get("cookies_exp"),
                                 ok=True if res.get("ok") else acc.get("ok"),
                                 synced_at=int(now))
            store.touch_keepalive(acc_id, True, f"会话正常 (VM: {res.get('vm_state') or 'RUNNING'})")
    except MuseAuthError as exc:
        store.mark(acc_id, False, str(exc))
        raise
    except Exception as exc:
        log.warning("HTTP 预续签账号 %s 异常: %s", acc_id, exc)
    return store.get_account(acc_id)


def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None):
    """在独立线程中执行 chat_stream 并持有 GEN_LOCK，通过 Queue 往外吐。
    无论下游客户端何时断连、异常或超时，stop_event + finally 块保证 100% 立即释放 GEN_LOCK，绝不死锁。
    若首字前遇到单账号 VM 卡死或会话异常，自动切换下一个健康账号重试一次。"""
    import queue
    if not CHAT_STREAM_LIMIT.acquire(blocking=False):
        raise MuseGenerationError("Quá nhiều luồng chat đang chờ; thử lại sau.")
    q = queue.Queue(maxsize=16)
    stop_event = threading.Event()

    def emit(item):
        while not stop_event.is_set():
            try:
                q.put(item, timeout=0.25)
                return True
            except queue.Full:
                continue
        return False

    def worker():
        cur_id = account_id
        cur_cookies = cookies
        cur_exp = expires
        last_exc = None
        leased_id = None
        try:
            for attempt in range(2):
                if stop_event.is_set():
                    return
                if attempt > 0:
                    if leased_id:
                        ACCOUNT_POOL.release(leased_id)
                        leased_id = None
                    try:
                        alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=timeout)
                    except Exception:
                        break
                    if not alt or alt["id"] == cur_id:
                        if alt:
                            leased_id = alt["id"]
                        break
                    cur_id = alt["id"]
                    leased_id = cur_id
                    cur_cookies = alt["cookies"]
                    cur_exp = alt.get("cookies_exp")
                    log.info("【对话自动切号】切换到备用账号 %s (%s) 重试...", alt.get("label"), cur_id)
                elif cur_id:
                    ACCOUNT_POOL.acquire(account_id=cur_id, timeout=timeout)
                    leased_id = cur_id

                yielded = False
                try:
                    if cur_id:
                        refreshed = _renew_and_persist(cur_id, wake_vm=True, force=(attempt > 0))
                        if refreshed:
                            cur_cookies = refreshed["cookies"]
                            cur_exp = refreshed.get("cookies_exp")
                    with GEN_LOCK:
                        if stop_event.is_set():
                            return
                        engine.start()
                        for chunk in engine.chat_stream(
                            cur_cookies, prompt, cur_exp, timeout,
                            account_id=cur_id, stop_event=stop_event
                        ):
                            yielded = True
                            if not emit(("data", chunk)):
                                return
                            if stop_event.is_set():
                                return
                    if cur_id:
                        store.mark(cur_id, True, "")
                        _sync_cookies(cur_id)
                    return
                except MuseAuthError as exc:
                    last_exc = exc
                    if cur_id:
                        store.mark(cur_id, False, str(exc))
                    engine.close_current_page()
                    if yielded:
                        break
                except Exception as exc:
                    last_exc = exc
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                    if yielded:
                        break
            if last_exc is not None:
                emit(("error", last_exc))
        finally:
            if leased_id:
                ACCOUNT_POOL.release(leased_id)
            engine.close_current_page()
            emit(("done", None))
            CHAT_STREAM_LIMIT.release()

    threading.Thread(target=worker, daemon=True).start()

    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()


class ImageRequest(BaseModel):
    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str | None = None
    aspect_ratio: str | None = None
    response_format: str = "url"      # url | b64_json
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    reference_image: str | None = None
    account_id: str | None = None
    async_: bool = Field(False, alias="async")


class VideoRequest(BaseModel):
    prompt: str
    model: str = "muse-video"
    duration: int | None = None
    size: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    timeout: int | None = None
    extra: str | None = None
    image: Any = None
    image_url: Any = None
    reference_image: str | None = None
    account_id: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None


class ChatRequest(BaseModel):
    """OpenAI Chat Completions 请求。

    对第三方客户端要尽量宽松：不认识字段一律忽略（Pydantic 默认行为），
    只挑我们真正用得上的读 —— 否则各种智能体各传各的参数就会 422。
    """
    model: str = "muse-spark"
    messages: list[ChatMessage] = []
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    prompt: str | None = None       # 兼容把 prompt 直接放顶层的客户端
    # 下面这些声明出来只是为了「能读到」，muse.ai 端不做对应处理
    tools: list | None = None
    tool_choice: object | None = None
    response_format: object | None = None
    stream_options: object | None = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API（新版 Codex 默认走这个）。"""
    model: str = "muse-spark"
    input: str | list | None = None
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    timeout: int | None = None
    tools: list | None = None
    store: bool | None = None


class AccountRequest(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_header: str | None = None
    batch: str | None = None          # 多行文本，每行一个账号
    expires: dict[str, int] = Field(default_factory=dict)   # cookie 名 -> 过期时间戳


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None


class AccountOrderRequest(BaseModel):
    ids: list[str]


# ------------------------- 提示词构造 -------------------------
def build_image_prompt(r: ImageRequest) -> str:
    has_ref = bool(r.reference_image or r.image or r.images)
    if has_ref:
        parts = [f"Generate or edit an image using only the reference image uploaded with this request: {r.prompt.strip()}"]
    else:
        parts = [f"Create a new image from text only. No reference image is provided; do not search previous albums or ask for an original image. Generate a new image from scratch based on this description: {r.prompt.strip()}"]
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()

    if any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920")):
        parts.append("Composition and aspect ratio: strictly 9:16 vertical portrait, full-screen phone framing with height greater than width. Keep the composition vertical; do not generate a landscape image.")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        parts.append("Composition and aspect ratio: 16:9 widescreen landscape.")
    elif any(k in ar or k in sz for k in ("1:1", "square", "正方形", "1024x1024")):
        parts.append("Composition and aspect ratio: 1:1 square.")
    elif any(k in ar or k in sz for k in ("4:3", "4/3")):
        parts.append("Composition and aspect ratio: 4:3.")
    elif any(k in ar or k in sz for k in ("3:4", "3/4")):
        parts.append("Composition and aspect ratio: 3:4 portrait.")
    elif r.aspect_ratio:
        parts.append(f"Composition and aspect ratio: {r.aspect_ratio}.")
    elif r.size:
        parts.append(f"Size/aspect ratio: {r.size}.")

    if has_ref:
        parts.append("Clean image requirement: remove all text, watermarks, signatures, corner marks, and logos from the reference image. Output a clean image without text, watermarks, or logos.")

    if r.extra:
        parts.append(r.extra)
    return "\n".join(parts)


def _content_text(content) -> str:
    """把 OpenAI 的 content 归一成纯文本（兼容多模态 list 形式）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif t == "image_url":
                    parts.append("[图片]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def build_chat_prompt(messages: list[ChatMessage]) -> str:
    """把 messages 拼成发给 muse.ai 的一段提示词。

    muse.ai 网页本身是个带上下文的会话，但本 API 是无状态的（每次可能落到
    不同账号/页面），所以把历史拼进 prompt 最可控 —— 这正好匹配 Codex 这类
    「每轮都带全量历史」的客户端。
    """
    system, turns = [], []
    for m in messages:
        role = (m.role or "").strip().lower()
        text = _content_text(m.content).strip()
        if role == "tool":
            # 工具执行结果 → 当成"用户提供的信息"发过去
            if text:
                turns.append(("user", "【工具执行结果】\n" + text))
            continue
        if not text:
            # 部分客户端的 assistant 消息只带 tool_calls、没有正文
            if role == "assistant" and m.tool_calls:
                turns.append(("assistant", "【请求调用工具】" + json.dumps(
                    m.tool_calls, ensure_ascii=False)[:600]))
            continue
        if role in ("system", "developer"):
            system.append(text)
        else:
            turns.append((role, text))

    # 单轮且无系统指令 → 直接发原文，最贴近自然对话
    if len(turns) == 1 and not system and turns[0][0] == "user":
        return turns[0][1]

    parts = []
    if system:
        sys_text = "\n\n".join(system)
        sys_text = sys_text.replace("danger-full-access", "standard-workspace-access")
        parts.append(f"背景与任务设定：\n{sys_text}")
    for role, text in turns:
        label = "助手" if role == "assistant" else "用户"
        parts.append(f"{label}：\n{text}")
    return "\n\n".join(parts)


# ------------------------- 工具调用（function calling）适配 -------------------------
# muse.ai 的网页模型**不会**返回结构化的 tool_calls，所以这里做一层协议适配：
#   1) 请求带 tools 时，把工具定义翻译成提示词里的【工具调用协议】；
#   2) 模型按协议输出 ```json {"tool": "...", "arguments": {...}} ```；
#   3) 我们把这段解析回 OpenAI 的 tool_calls 交给下游 agent。
#
# 注意：这是"尽力适配"而非保证 —— 目标模型是通用对话模型，没有针对
# function calling 做微调，遵守协议的程度需要实测观察。
_TOOL_PROTOCOL_HEAD = """你可以根据需要调用以下工具来协助用户完成任务。
若需调用工具，请直接输出如下格式的 JSON 代码块（不要包含其他多余解释）：
```json
{"name": "<工具名>", "arguments": {<参数>}}
```
如果需要调用多个工具，请输出包含多个对象的 JSON 数组。
如果无需调用工具，请直接用自然语言回答。

可用工具列表：
"""


def _describe_params(params) -> str:
    """把 JSON Schema 的参数描述成易读的多行文本。"""
    if not isinstance(params, dict):
        return "      （无参数）"
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if not props:
        return "      （无参数）"
    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        lines.append("      - %s (%s, %s)%s" % (
            name, spec.get("type") or "any",
            "必填" if name in required else "可选",
            (" " + spec["description"]) if spec.get("description") else ""))
    return "\n".join(lines)


def build_tools_prompt(tools: list | None) -> str:
    """把 tools 定义翻译成提示词片段（没有工具时返回空串）。

    同时兼容 Chat Completions 的 `{"type":"function","function":{...}}`
    和 Responses API 的 `{"type":"function","name":...,"parameters":...}`。
    """
    if not tools:
        return ""
    items = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        desc = fn.get("description") or ""
        items.append("%d. %s%s\n   参数：\n%s" % (
            len(items) + 1, fn["name"], (" — " + desc) if desc else "",
            _describe_params(fn.get("parameters"))))
    if not items:
        return ""
    return _TOOL_PROTOCOL_HEAD + "\n".join(items) + "\n"


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")


def _as_tool_calls(obj) -> list[dict] | None:
    """把解析出的 JSON 转成 OpenAI tool_calls；不像工具调用就返回 None。"""
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = item.get("tool") or item.get("name") or item.get("function")
        if not isinstance(name, str) or not name:
            return None
        args = item.get("arguments")
        if args is None:
            args = item.get("parameters") or item.get("args") or {}
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": "call_" + uuid.uuid4().hex[:20], "type": "function",
                    "function": {"name": name, "arguments": args}})
    return out or None


def parse_tool_calls(text: str) -> tuple[list[dict] | None, str]:
    """从模型输出里抽出工具调用。

    返回 (tool_calls, 剩余文本)；抽不到就返回 (None, 原文)。
    支持多种形态：
    1) ```json ... ``` 代码块；
    2) 裸 JSON（或带有前导 json/JSON 关键字）；
    3) 文本中内嵌的完整 JSON 对象或数组。
    """
    if not text:
        return None, text
    for m in reversed(list(_FENCE_RE.finditer(text))):
        try:
            calls = _as_tool_calls(json.loads(m.group(1).strip()))
        except Exception:
            continue
        if calls:
            return calls, (text[:m.start()] + text[m.end():]).strip()

    stripped = text.strip()
    clean_stripped = re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", stripped).rstrip("`").strip()
    if clean_stripped.startswith(("{", "[")):
        try:
            calls = _as_tool_calls(json.loads(clean_stripped))
            if calls:
                return calls, ""
        except Exception:
            pass

    m_json = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m_json:
        try:
            calls = _as_tool_calls(json.loads(m_json.group(1).strip()))
            if calls:
                rest = (text[:m_json.start()] + text[m_json.end():]).strip()
                if re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", rest).strip("`").strip() == "":
                    rest = ""
                return calls, rest
        except Exception:
            pass

    return None, text


def build_video_prompt(r: VideoRequest) -> str:
    user_prompt = r.prompt.strip()
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()
    dur = r.duration or 6
    has_ref = bool(r.reference_image or r.image or r.image_url)

    is_vertical = any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920"))

    parts = []
    if is_vertical:
        if has_ref:
            parts.append(f"Use only the reference image uploaded with this request as the first frame. Do not use historical images or any other image. Generate a strictly 9:16 vertical portrait image-to-video animation, 720x1280, full-screen phone framing with height greater than width. Start from the attached reference image and continue its action. Do not generate landscape framing or black borders. Duration: exactly {dur} seconds. Description: {user_prompt}")
        else:
            parts.append(f"Create a new video from text only. Do not reference any historical images or previous context. Generate a strictly 9:16 vertical portrait video, 720x1280, full-screen phone framing with height greater than width. Keep the composition vertical; do not generate landscape framing or black borders. Duration: exactly {dur} seconds. Description: {user_prompt}")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        if has_ref:
            parts.append(f"Use only the reference image uploaded with this request as the first frame. Do not use historical images or any other image. Generate a 16:9 widescreen landscape image-to-video animation. Start from the attached reference image and continue its action. Duration: exactly {dur} seconds. Description: {user_prompt}")
        else:
            parts.append(f"Create a new video from text only. Do not reference any historical images or previous context. Generate a 16:9 widescreen landscape video. Duration: exactly {dur} seconds. Description: {user_prompt}")
    else:
        if has_ref:
            parts.append(f"Use only the reference image uploaded with this request as the first frame. Do not use historical images or any other image. Generate an image-to-video animation. Start from the attached reference image and continue its action. Duration: exactly {dur} seconds. Description: {user_prompt}")
        else:
            parts.append(f"Create a new video from text only. Do not reference any historical images or previous context. Generate a video. Duration: exactly {dur} seconds. Description: {user_prompt}")

    if r.resolution:
        parts.append(f"Resolution/quality: {r.resolution}.")
    if r.extra:
        parts.append(r.extra)
    return "\n".join(parts)


def media_url(name: str) -> str:
    """媒体地址。

    配了 public_base 就返回**绝对 URL** —— OpenAI 兼容客户端（以及各类智能体
    平台）拿到 data[].url 后一般会直接渲染或下载，相对路径会被解析到客户端
    自己的域名上，导致 404。public_base 为空时退回相对路径。
    """
    base = _public_base()
    return f"{base}/v1/media/{name}" if base else f"/v1/media/{name}"


# ------------------------- cookie 解析 -------------------------
def parse_cookie_text(text: str) -> dict[str, str]:
    """把 `a=1; b=2` / JSON / Set-Cookie 行解析成 dict。"""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        import json
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def parse_batch(text: str) -> list[tuple[str, dict]]:
    """批量导入：每行 `标签 | cookie串`，标签可省略。"""
    out: list[tuple[str, dict]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        body = line
        if "|" in line:
            head, tail = line.split("|", 1)
            if "=" not in head:          # `|` 出现在 cookie 里时不算分隔符
                label, body = head.strip(), tail.strip()
        cookies = parse_cookie_text(body)
        if cookies:
            out.append((label, cookies))
    return out


# ------------------------- 核心生成 -------------------------
def _sync_cookies(acc_id: str) -> dict:
    """生成完从浏览器读回 cookie 并写回账号池。

    实测结论：**核心 cookie 的 expires 不会被使用行为续期**（hatch_vml 固定
    约 2 天就到期），这里同步回来的主要是那些每次访问都会重新下发的非核心
    cookie（_fbp / wd / dpr 等）以及可能新增的条目，避免账号信息比实际更旧。

    注意：这个函数**绝不能把生成结果搞失败** —— 生成已经成功了，
    同步只是锦上添花，出任何问题都只记一条日志。
    """
    try:
        live = engine.read_cookies()
        if not live:
            return {"synced": 0}
        acc = store.get_account(acc_id)
        if not acc:
            return {"synced": 0}
        cur = dict(acc.get("cookies") or {})
        new_vals = {k: v["value"] for k, v in live.items() if v.get("value")}
        # 有效期要「合并」而不是「覆盖」：会话 cookie 读回的 expires 是 -1，
        # 若整体覆盖会把原有有效期记录清空（实测踩过这个坑）。
        exps = dict(acc.get("cookies_exp") or {})
        exps.update({k: v["expires"] for k, v in live.items()
                     if _pos(v.get("expires"))})
        changed = sum(1 for k, v in new_vals.items() if cur.get(k) != v)
        store.update_account(acc_id, cookies={**cur, **new_vals},
                             cookies_exp=exps, synced_at=int(time.time()))
        updated = store.get_account(acc_id) or {}
        return {"synced": len(new_vals), "changed": changed,
                "expires_at": updated.get("expires_at")}
    except Exception as exc:  # noqa: BLE001
        log.warning("cookie 同步失败（不影响本次生成）: %s", exc)
        return {"synced": 0, "error": str(exc)[:200]}


def _pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _run_generation(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    on_acquired=None, on_context=None, task_id=None,
                    cancel_event: threading.Event | None = None,
                    expected_duration: int = 10) -> tuple[dict, str | None]:
    # Browser ownership covers account selection, retry and cleanup, not just generate().
    # The generation clock starts after an account is free. Queue time must not consume it.
    lock_deadline = time.monotonic() + max(1, timeout)
    while True:
        if cancel_event and cancel_event.is_set():
            raise MuseGenerationError("Task cancelled by user")
        remaining = lock_deadline - time.monotonic()
        if remaining <= 0:
            raise MuseGenerationError("等待浏览器队列超时，请稍后重试")
        if GEN_LOCK.acquire(timeout=min(0.25, remaining)):
            break
    try:
        if cancel_event and cancel_event.is_set():
            raise MuseGenerationError("Task cancelled by user")
        if on_acquired:
            try:
                on_acquired()
            except Exception:
                pass
        return _run_generation_locked(prompt, kind, timeout, account_id,
                                      on_progress, reference_image, deadline=None,
                                      on_context=on_context, task_id=task_id,
                                      cancel_event=cancel_event,
                                      expected_duration=expected_duration)
    finally:
        if task_id:
            views = getattr(engine, "task_views", None)
            if views:
                # Free the isolated browser context as soon as generation ends.
                views.forget(task_id)
            if getattr(engine, "_local", None):
                engine._local.task_view_id = None
        GEN_LOCK.release()


def _run_generation_locked(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None, deadline=None, on_context=None,
                    task_id=None, cancel_event: threading.Event | None = None,
                    expected_duration: int = 10) -> tuple[dict, str | None]:
    # Several batch rows can wait behind the few signed-in accounts. Give them
    # time for a few finished videos before failing, then give the winner a full generation budget.
    account_wait = max(1, timeout) * 4 if deadline is None else max(1, int(deadline - time.monotonic()))
    preferred_id = None if kind == "video" and not account_id else getattr(engine, "current_acc_id", None)
    account_deadline = time.monotonic() + account_wait
    while True:
        if cancel_event and cancel_event.is_set():
            raise MuseGenerationError("Task cancelled by user")
        account_remaining = account_deadline - time.monotonic()
        if account_remaining <= 0:
            raise MuseGenerationError("Account wait timed out")
        try:
            acc = ACCOUNT_POOL.acquire(account_id=account_id,
                                       preferred_id=preferred_id,
                                       timeout=min(0.5, account_remaining))
            break
        except MuseGenerationError:
            if cancel_event and cancel_event.is_set():
                raise MuseGenerationError("Task cancelled by user")
            if time.monotonic() >= account_deadline:
                raise
    if deadline is None:
        deadline = time.monotonic() + max(1, timeout)
    cur_acc = acc
    last_exc = None
    try:
        for attempt in range(2):
            if cancel_event and cancel_event.is_set():
                raise MuseGenerationError("Task cancelled by user")
            if deadline is not None and time.monotonic() >= deadline:
                raise MuseGenerationError("任务总等待时限已到，停止重试")
            if attempt > 0:
                if last_exc and "未产出媒体附件，仅返回了文本回复" in str(last_exc):
                    break
                old_id = cur_acc["id"] if cur_acc else None
                if old_id:
                    ACCOUNT_POOL.release(old_id)
                cur_acc = None
                rem = int(deadline - time.monotonic()) if deadline is not None else timeout
                if rem <= 0:
                    break
                if account_id:
                    # Explicit pinning for paired image-to-video jobs must survive retries.
                    cur_acc = ACCOUNT_POOL.acquire(account_id=account_id, timeout=rem)
                    if not cur_acc:
                        break
                else:
                    alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=rem)
                    if not alt or (old_id and alt["id"] == old_id):
                        if alt:
                            cur_acc = alt
                        break
                    cur_acc = alt
                log.info("【生图/视频自动切号】切换到备用账号 %s (%s) 重试...", cur_acc.get("label"), cur_acc["id"])
            try:
                refreshed = _renew_and_persist(cur_acc["id"], wake_vm=True, force=(attempt > 0))
                if refreshed:
                    cur_acc = refreshed
                engine.start()
                remaining = int(deadline - time.monotonic()) if deadline is not None else timeout
                if remaining <= 0:
                    raise MuseGenerationError("任务总等待时限已到，停止重试")
                # A later attempt must not replace the account that already owns a thread
                # until that new attempt has submitted its own conversation.
                if on_context and attempt == 0:
                    on_context({"account": cur_acc["id"], "account_label": cur_acc.get("label", cur_acc["id"])})
                def record_context():
                    if not on_context:
                        return
                    try:
                        context = dict(engine.generation_context() or {})
                    except Exception:  # noqa: BLE001
                        return
                    if not context.get("muse_url"):
                        return
                    context["account"] = cur_acc["id"]
                    context["account_label"] = cur_acc.get("label", cur_acc["id"])
                    on_context(context)
                def report_progress(progress):
                    record_context()
                    if on_progress:
                        on_progress(progress)
                if task_id:
                    engine._ensure_local().task_view_id = task_id
                try:
                    reference = reference_image() if callable(reference_image) else reference_image
                    res = engine.generate(cur_acc["cookies"], prompt, expect=kind,
                                          timeout=remaining, expires=cur_acc.get("cookies_exp"),
                                          account_id=cur_acc["id"], on_progress=report_progress,
                                          reference_image=reference, stop_event=cancel_event,
                                          expected_duration=expected_duration)
                finally:
                    # Save the conversation link before failure cleanup navigates away or closes the tab.
                    record_context()
                store.mark(cur_acc["id"], True, "")
                _sync_cookies(cur_acc["id"])
                return res, cur_acc["id"]
            except MuseAuthError as exc:
                last_exc = exc
                store.mark(cur_acc["id"], False, str(exc))
                engine.close_current_page()
            except MuseGenerationError as exc:
                if cancel_event and cancel_event.is_set():
                    raise MuseGenerationError("Task cancelled by user") from exc
                last_exc = exc
                if "MUSE_ACTION_REQUIRED:" in str(exc):
                    raise
                store.mark(cur_acc["id"], True, f"任务异常: {str(exc)[:60]}")
                try:
                    if task_id:
                        engine.close_current_page()
                    else:
                        engine.reset_thread()
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                engine.close_current_page()
                last_exc = MuseGenerationError(f"生成失败: {exc}")
        raise last_exc
    finally:
        if cur_acc:
            ACCOUNT_POOL.release(cur_acc.get("id"))
        engine.close_current_page()


# ------------------------- 基础接口 -------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": int(time.time())}


@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.browser and
                                    (engine.proc is None or engine.proc.poll() is None))}


@app.get("/v1/models")
def models(_=Depends(auth)):
    return {"object": "list", "data": MODELS}


# ------------------------- 生图 -------------------------
def _image_response(req: ImageRequest, res: dict) -> dict:
    item = {"revised_prompt": req.prompt, "url": media_url(res["filename"]),
            "size": req.size or "auto", "kind": res["kind"], "bytes": res["size"]}
    source_url = res.get("muse_media_url") or res.get("source_url")
    if source_url:
        item["muse_media_url"] = source_url
    if req.response_format == "b64_json":
        fpath = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        with open(fpath, "rb") as f:
            item["b64_json"] = base64.b64encode(f.read()).decode()
        item.pop("url", None)
    return {"created": int(time.time()), "data": [item]}


def _queue_image(req: ImageRequest, prompt: str, reference_image: str | None,
                 idempotency_key: str | None = None):
    """Opt-in polling avoids reverse-proxy timeouts; no generation is repeated by polling."""
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "reference": reference_image},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with IMAGE_TASK_LOCK:
        if key_hash:
            existing = store.find_task("image_request_key", key_hash)
            if existing:
                if existing.get("image_request_hash") != request_hash:
                    raise HTTPException(409, "Idempotency-Key reused with different request")
                return JSONResponse(status_code=202, content={
                    "id": existing["id"], "task_id": existing["id"],
                    "object": "image.task", "status": existing["status"],
                    "progress": existing.get("progress", 0),
                    "created_at": existing["created_at"]})
        # ponytail: one Chromium worker; cap admission instead of adding a broker.
        pending = store.count_tasks(statuses=("queued", "processing"))
        if pending >= 30:
            raise HTTPException(429, "Generation queue is full; retry later")
        task = store.create_task("image", req.prompt)
        tid = task["id"]
        cancel_event = _register_task_cancel(tid)
        store.update_task(tid, api_prompt=prompt, size=req.size,
                          response_format=req.response_format, progress=0,
                          image_request_key=key_hash, image_request_hash=request_hash,
                          timeout=req.timeout or CFG.image_timeout,
                          requested_account_id=req.account_id or None)
        store.set_task_input(tid, reference_image=reference_image)

    def worker():
        timing = {"t0": time.time()}
        def _on_acquired():
            timing["t0"] = time.time()
            store.update_task(tid, status="processing", progress=5)
        try:
            task_data = store.get_task(tid) or {}
            res, acc_id = _run_generation(
                task_data.get("api_prompt") or task_data.get("prompt") or "", "image",
                task_data.get("timeout") or CFG.image_timeout,
                account_id=task_data.get("requested_account_id"),
                reference_image=lambda: store.get_task_input(tid).get("reference_image"),
                on_progress=lambda p: store.update_task(tid, progress=p),
                on_acquired=_on_acquired,
                on_context=lambda context: store.update_task(tid, **context), task_id=tid,
                cancel_event=cancel_event)
            if cancel_event.is_set():
                store.update_task(tid, status="cancelled", error="Cancelled by user")
                return
            # Store only media metadata, not large base64 payloads or reference credentials.
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - timing["t0"], 1),
                              url=media_url(res["filename"]),
                              result={**{k: res[k] for k in ("filename", "size", "kind")},
                                      "url": media_url(res["filename"]),
                                      "muse_media_url": res.get("source_url", "")})
        except Exception as exc:  # noqa: BLE001
            store.update_task(tid, status="cancelled" if cancel_event.is_set() else "failed",
                              error="Cancelled by user" if cancel_event.is_set() else str(exc),
                              elapsed=round(time.time() - timing["t0"], 1))
        finally:
            store.delete_task_input(tid)
            _forget_task_cancel(tid)

    TASK_EXECUTOR.submit(worker)
    return JSONResponse(status_code=202, content={
        "id": tid, "task_id": tid, "object": "image.task", "status": "queued",
        "progress": 0, "created_at": task["created_at"]})


@app.post("/v1/images/tasks")
def create_image_task(req: ImageRequest,
                      idempotency_key: str | None = Header(default=None), _=Depends(auth)):
    ref_img = req.reference_image or req.image
    if isinstance(ref_img, dict):
        ref_img = ref_img.get("url") or ref_img.get("b64_json")
    if not ref_img and req.images:
        first = req.images[0]
        ref_img = first.get("image_url") or first.get("url") if isinstance(first, dict) else first
    return _queue_image(req, build_image_prompt(req), ref_img, idempotency_key)


@app.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, _=Depends(auth)):
    task = store.get_task(task_id)
    if not task or task.get("kind") != "image":
        raise HTTPException(404, "image task 不存在")
    out = dict(task)
    if out.get("status") == "completed":
        req = ImageRequest(prompt=out["prompt"], size=out.get("size"),
                           response_format=out.get("response_format") or "url")
        out.update(_image_response(req, out["result"]))
    return out


@app.post("/v1/images/generations")
async def images_generations(req: ImageRequest, _=Depends(auth)):
    ref_img = req.reference_image or req.image
    if isinstance(ref_img, dict):
        ref_img = ref_img.get("url") or ref_img.get("b64_json")
    if not ref_img and req.images and isinstance(req.images, list):
        first = req.images[0]
        ref_img = first.get("image_url") or first.get("url") if isinstance(first, dict) else first

    prompt = build_image_prompt(req)
    timeout = req.timeout or CFG.image_timeout
    if req.async_:
        return _queue_image(req, prompt, ref_img)
    try:
        res, _acc = await asyncio.to_thread(_run_generation, prompt, "image", timeout, reference_image=ref_img)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _image_response(req, res)


@app.post("/v1/images/edits")
async def images_edits(request: Request, _=Depends(auth)):
    """OpenAI 兼容的图生图/图像编辑接口，兼容 multipart/form-data 与 application/json。"""
    content_type = request.headers.get("content-type", "").lower()
    prompt = ""
    model = "muse-image"
    size = None
    aspect_ratio = None
    response_format = "url"
    timeout = None
    ref_image_data = None
    async_mode = False

    if "multipart/form-data" in content_type:
        form = await request.form()
        async_mode = form.get("async", False)
        prompt = form.get("prompt") or ""
        model = form.get("model") or "muse-image"
        size = form.get("size")
        aspect_ratio = form.get("aspect_ratio")
        response_format = form.get("response_format") or "url"
        timeout_val = form.get("timeout")
        if timeout_val:
            try:
                timeout = int(timeout_val)
            except ValueError:
                pass
        img_field = form.get("image")
        if img_field and hasattr(img_field, "read"):
            content = await img_field.read()
            ref_mime = getattr(img_field, "content_type", "image/png") or "image/png"
            ref_image_data = f"data:{ref_mime};base64,{base64.b64encode(content).decode('ascii')}"
        elif isinstance(img_field, str):
            ref_image_data = img_field
    else:
        body = await request.json()
        async_mode = body.get("async", False)
        prompt = body.get("prompt") or ""
        model = body.get("model") or "muse-image"
        size = body.get("size")
        aspect_ratio = body.get("aspect_ratio")
        response_format = body.get("response_format") or "url"
        timeout = body.get("timeout")
        img_val = body.get("image")
        if isinstance(img_val, dict):
            ref_image_data = img_val.get("url") or img_val.get("b64_json")
        elif isinstance(img_val, str):
            ref_image_data = img_val
        if not ref_image_data and body.get("images") and isinstance(body.get("images"), list):
            first = body["images"][0]
            if isinstance(first, dict):
                ref_image_data = first.get("image_url") or first.get("url")
            elif isinstance(first, str):
                ref_image_data = first
        if not ref_image_data:
            ref_image_data = body.get("reference_image") or body.get("image_url")

    if not prompt:
        prompt = "Generate an image based on this reference image."

    req_obj = ImageRequest(
        prompt=prompt,
        model=model,
        size=size,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        timeout=timeout,
        reference_image=ref_image_data,
        **{"async": async_mode}
    )
    full_prompt = build_image_prompt(req_obj)
    gen_timeout = timeout or CFG.image_timeout
    if req_obj.async_:
        return _queue_image(req_obj, full_prompt, ref_image_data)
    try:
        res, _acc = await asyncio.to_thread(_run_generation, full_prompt, "image", gen_timeout, reference_image=ref_image_data)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc

    return _image_response(req_obj, res)


# ------------------------- 生视频（异步任务） -------------------------
@app.post("/v1/videos")
@app.post("/v1/videos/generations")
async def create_video(req: VideoRequest, _=Depends(auth)):
    ref_img = None
    if req.reference_image:
        ref_img = req.reference_image
    elif req.image_url:
        ref_img = req.image_url if isinstance(req.image_url, str) else (req.image_url.get("url") if isinstance(req.image_url, dict) else None)
    elif req.image:
        ref_img = req.image.get("url") if isinstance(req.image, dict) else req.image

    prompt = build_video_prompt(req)
    timeout = req.timeout or CFG.video_timeout
    with IMAGE_TASK_LOCK:
        if store.count_tasks(statuses=("queued", "processing")) >= 30:
            raise HTTPException(429, "Generation queue is full; retry later")
        task = store.create_task("video", req.prompt)
        tid = task["id"]
        cancel_event = _register_task_cancel(tid)
        store.update_task(tid, api_prompt=prompt, duration=req.duration or 6,
                          timeout=timeout, requested_account_id=req.account_id or None)
        store.update_task(tid, progress=10)
        store.set_task_input(tid, reference_image=ref_img)

    def worker():
        timing = {"t0": time.time()}
        def _on_acquired():
            timing["t0"] = time.time()
            store.update_task(tid, status="processing", progress=15,
                              processing_started_at=timing["t0"])
        try:
            task_data = store.get_task(tid) or {}
            def prog_cb(p):
                store.update_task(tid, progress=p)
            res, acc_id = _run_generation(task_data.get("api_prompt") or task_data.get("prompt") or "",
                                          "video", task_data.get("timeout") or CFG.video_timeout,
                                          account_id=task_data.get("requested_account_id"), on_progress=prog_cb,
                                          reference_image=lambda: store.get_task_input(tid).get("reference_image"), on_acquired=_on_acquired,
                                          on_context=lambda context: store.update_task(tid, **context), task_id=tid,
                                          cancel_event=cancel_event,
                                          expected_duration=task_data.get("duration") or 6)
            if cancel_event.is_set():
                store.update_task(tid, status="cancelled", error="Cancelled by user")
                return
            if _video_file_saved(tid):
                return
            vurl = media_url(res["filename"])
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - timing["t0"], 1),
                              url=vurl,
                              video={"url": vurl},
                              result={"url": vurl,
                                      "filename": res["filename"],
                                      "bytes": res["size"], "kind": res["kind"]})
        except Exception as exc:  # noqa: BLE001
            if _video_file_saved(tid):
                return
            store.update_task(tid, status="cancelled" if cancel_event.is_set() else "failed",
                              elapsed=round(time.time() - timing["t0"], 1), error=str(exc))
        finally:
            store.delete_task_input(tid)
            _forget_task_cancel(tid)

    TASK_EXECUTOR.submit(worker)
    return {"id": task["id"], "task_id": task["id"], "object": "video.task", "status": "queued",
            "progress": 10, "created_at": task["created_at"]}


@app.get("/v1/videos/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str, _=Depends(auth)):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "task 不存在")
    _schedule_rendered_harvest(t)
    out = dict(t)
    status = out.get("status")
    if status == "failed":
        with _COLLECTING_LOCK:
            recovering = task_id in _COLLECTING_TASKS
        if recovering:
            out["status"] = "processing"
            out["progress"] = max(out.get("progress", 0) or 0, 92)
            status = "processing"
    if status in ("succeeded", "success", "done"):
        out["status"] = "completed"
    if out.get("status") == "completed":
        out["progress"] = 100
    elif out.get("status") == "processing":
        elapsed = time.time() - out.get("created_at", time.time())
        calc_prog = min(92, int(20 + elapsed * 1.1))
        out["progress"] = max(out.get("progress", 0) or 0, calc_prog)
    vurl = out.get("url")
    if not vurl and isinstance(out.get("result"), dict):
        vurl = out["result"].get("url")
    if vurl:
        out["url"] = vurl
        if "video" not in out:
            out["video"] = {"url": vurl}
    return out


# ------------------------- 对话（OpenAI 兼容） -------------------------
def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chat_chunk(cid: str, created: int, model: str, delta: dict,
                finish: str | None = None) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _want_usage(stream_options) -> bool:
    """下游（LangChain、部分 SDK / 智能体框架）会带
    `stream_options.include_usage=true`，要求在 [DONE] 之前补一个
    `choices: []` + `usage` 的分片。不发的话少数框架会一直等 usage 而卡住。"""
    if isinstance(stream_options, dict):
        return bool(stream_options.get("include_usage"))
    return False


def _pace_text(text: str, chunk_size: int = 2, delay: float = 0.012):
    """把大块文本平滑切分为仿原生 LLM 打字机的微流式 Token。
    若传入文本本身微小（<= 3 字），直接秒级放行，零延迟；
    若传入文本是大块（如 DOM 批量刷新），按每 chunk_size 字间隔 delay 平滑输出。
    """
    if not text:
        return
    if len(text) <= 3 or delay <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        if i + chunk_size < len(text):
            time.sleep(delay)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):
    """OpenAI 兼容的对话接口 —— 各类智能体客户端都能接。

    说明：muse.ai 网页是**自动路由**的 agent，界面上没有模型选择器，所有请求
    都会落到同一个网页助手（自称 Koda，底层是 Muse 系列语言模型）。所以
    `model` 字段只用于兼容下游，不影响路由结果。

    请求带 `tools` 时，会注入【工具调用协议】并把模型输出的 JSON 解析回
    `tool_calls`（muse.ai 没有原生 function calling，这层属于协议适配）。

    对话与生图/生视频共用同一个 Chromium 进程；并发任务使用独立 BrowserContext 与页面。
    """
    prompt = build_chat_prompt(req.messages) or (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "messages 为空")

    # 工具调用协议默认不注入 —— 实测 muse.ai 的助手会明确拒绝输出"伪工具调用"，
    # 注入反而污染正常回答；详见 config.py 的 tool_protocol 注释。
    tool_note = build_tools_prompt(req.tools) if CFG.tool_protocol else ""
    if tool_note:
        # 放在末尾：开头是 agent 自己的 system 提示，夹在中间容易被忽略；
        # 紧贴用户消息之前，模型对末尾指令的遵守度明显更高。
        prompt = prompt + "\n\n" + tool_note

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id = acc["id"]
    cookies = acc["cookies"]
    expires = acc.get("cookies_exp")

    def tool_call_deltas(calls: list[dict]) -> list[list[dict]]:
        """按 OpenAI 习惯分两片发：先 id/name，再 arguments 全文。"""
        head = [{"index": i, "id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": ""}}
                for i, c in enumerate(calls)]
        body = [{"index": i, "function": {"arguments": c["function"]["arguments"]}}
                for i, c in enumerate(calls)]
        return [head, body]

    if req.stream:
        def sync_stream():
            try:
                # 握手建立瞬间立即发送 role: assistant 首包，让下游客户端秒级捕获光标
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))

                stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id)
                if not tool_note:
                    for chunk in stream_gen:
                        for piece in _pace_text(chunk):
                            yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                    yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                else:
                    buf, mode = "", None
                    for chunk in stream_gen:
                        if mode == "text":
                            for piece in _pace_text(chunk):
                                yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            continue
                        buf += chunk
                        if mode is None:
                            head = buf.lstrip()
                            if len(head) >= 2:
                                if head[0] in "{[" or head.startswith("```"):
                                    mode = "maybe_tool"
                                else:
                                    mode = "text"
                                    for piece in _pace_text(buf):
                                        yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                                    buf = ""
                    if mode == "text":
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                    else:
                        calls, rest = parse_tool_calls(buf)
                        if calls:
                            if rest:
                                for piece in _pace_text(rest):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            for piece in tool_call_deltas(calls):
                                yield _sse(_chat_chunk(cid, created, model, {"tool_calls": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="tool_calls"))
                        else:
                            if buf:
                                for piece in _pace_text(buf):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                if _want_usage(req.stream_options):
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                yield "data: [DONE]\n\n"
                store.mark(acc_id, True, "")
            except MuseAuthError as exc:
                store.mark(acc_id, False, str(exc))
                yield _sse({"error": {"message": str(exc), "type": "auth_error", "code": 401}})
            except MuseGenerationError as exc:
                store.mark(acc_id, True, f"助手超时: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
                yield _sse({"error": {"message": str(exc), "type": "server_error", "code": 502}})
            except Exception as exc:
                yield _sse({"error": {"message": f"内部错误: {exc}", "type": "server_error", "code": 500}})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if tool_note:
        calls, rest = parse_tool_calls(text)
        if calls:
            return {"id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": rest or None,
                                             "tool_calls": calls}}],
                    "usage": usage}
    return {"id": cid, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage}


def _responses_messages(req: ResponsesRequest) -> list[ChatMessage]:
    """把 Responses API 的 instructions / input 归一成 messages。"""
    msgs: list[ChatMessage] = []
    if req.instructions:
        msgs.append(ChatMessage(role="system", content=req.instructions))
    inp = req.input
    if isinstance(inp, str):
        msgs.append(ChatMessage(role="user", content=inp))
        return msgs
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                msgs.append(ChatMessage(role="user", content=item))
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if item.get("role"):                      # message 形态
                msgs.append(ChatMessage(role=item["role"],
                                        content=item.get("content")))
            elif itype == "input_text":
                msgs.append(ChatMessage(role="user", content=item.get("text")))
            elif itype == "function_call_output":
                msgs.append(ChatMessage(role="user", content="【工具执行结果】\n"
                                        + str(item.get("output") or "")))
            elif itype == "function_call":
                msgs.append(ChatMessage(role="assistant", content="【请求调用工具】"
                                        + str(item.get("name") or "")))
    return msgs


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/v1/responses")
async def responses_api(req: ResponsesRequest, _=Depends(auth)):
    """OpenAI Responses API —— 新版 Codex 默认走这个端点。

    只实现 Codex 实际用到的子集：文本进 → 文本出（含流式事件）。
    muse.ai 不会返回结构化的 tool_calls，关于工具调用的边界见 README。
    """
    prompt = build_chat_prompt(_responses_messages(req))
    if not prompt:
        raise HTTPException(400, "input 为空")

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id, cookies, expires = acc["id"], acc["cookies"], acc.get("cookies_exp")

    def envelope(status: str, text: str = "") -> dict:
        done = status == "completed"
        return {"id": rid, "object": "response", "created_at": created,
                "status": status, "model": model,
                "output": [{"type": "message", "id": mid, "role": "assistant",
                            "status": "completed" if done else "in_progress",
                            "content": ([{"type": "output_text", "text": text,
                                          "annotations": []}] if text else [])}],
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0}}

    if req.stream:
        def sync_stream():
            try:
                yield _sse_event("response.created", {
                    "type": "response.created",
                    "response": envelope("in_progress")})
                yield _sse_event("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "in_progress", "content": []}})
                yield _sse_event("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": mid,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "",
                             "annotations": []}})
                full = ""
                for chunk in safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id):
                    full += chunk
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": mid,
                        "output_index": 0, "content_index": 0, "delta": chunk})
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": mid,
                    "output_index": 0, "content_index": 0, "text": full})
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": full,
                                          "annotations": []}]}})
                yield _sse_event("response.completed", {
                    "type": "response.completed",
                    "response": envelope("completed", full)})
            except Exception as exc:  # noqa: BLE001
                log.warning("responses 流式失败: %s", exc)
                yield _sse_event("response.failed", {
                    "type": "response.failed",
                    "response": envelope("failed")})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    return envelope("completed", text)


@app.get("/v1/media/{name}")
def get_media(name: str, download: bool = False):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    return FileResponse(p, filename=name if download else None)


def _ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise HTTPException(500, f"Không tìm thấy FFmpeg: {exc}")


def _ffmpeg_duration(ffmpeg: str, path: str) -> float:
    import subprocess
    result = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise ValueError(f"Không đọc được thời lượng audio: {os.path.basename(path)}")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


@app.post("/admin/video-assembly")
def assemble_video(files: list[UploadFile] = File(...), job_id: str = Form(...),
                   mode: str = Form("segments"), _=Depends(auth)):
    """Nhận một thư mục dự án, chia thời lượng audio cho các cảnh con và ghép thành MP4."""
    import csv
    import subprocess
    from pathlib import PurePosixPath

    if len(files) > 1000:
        raise HTTPException(413, "Thư mục có quá nhiều file (tối đa 1000).")
    work = None
    final_path = None
    assembled = False
    with _ASSEMBLY_DOWNLOAD_LOCK:
        _ASSEMBLY_JOBS[job_id] = {"status": "processing", "progress": 1, "stage": "upload", "started_at": time.time()}
        _write_assembly_state("jobs", job_id, _ASSEMBLY_JOBS[job_id])
    def update_job(**values):
        with _ASSEMBLY_DOWNLOAD_LOCK:
            current = _ASSEMBLY_JOBS.setdefault(job_id, {"status": "processing", "started_at": time.time()})
            current.update(values)
            _write_assembly_state("jobs", job_id, current)
    try:
        work = tempfile.mkdtemp(prefix="muse-assembly-", dir=CFG.data_dir)
        for upload in files:
            rel = PurePosixPath((upload.filename or "").replace("\\", "/"))
            if rel.is_absolute() or ".." in rel.parts:
                continue
            target = os.path.join(work, *rel.parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as out:
                shutil.copyfileobj(upload.file, out)
            upload.file.close()
        audio_dir = next((os.path.join(root, d) for root, dirs, _ in os.walk(work)
                          for d in dirs if d.lower() == "audio"), None)
        video_dir = next((os.path.join(root, d) for root, dirs, _ in os.walk(work)
                          for d in dirs if d.lower() == "video"), None)
        timeline_path = next((os.path.join(root, name) for root, _, names in os.walk(work)
                              for name in names if name.lower() == "timeline.csv"), None)
        if not audio_dir or not video_dir or not timeline_path:
            raise HTTPException(400, "Cần chọn folder chứa timeline.csv, audio/ và video/.")
        with open(timeline_path, "rb") as f:
            timeline_bytes = f.read()
        if timeline_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
            timeline_text = timeline_bytes.decode("utf-16")
        else:
            try:
                timeline_text = timeline_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                # Excel on Windows may save CSV files in the local ANSI code page.
                timeline_text = timeline_bytes.decode("cp1252", errors="replace")
        rows = list(csv.DictReader(io.StringIO(timeline_text, newline="")))
        if mode not in {"segments", "prompt_timeline"}:
            raise HTTPException(400, "Kiểu ghép video không hợp lệ.")
        if not rows:
            raise HTTPException(400, "timeline.csv không có dòng dữ liệu.")
        # A prompt timeline is self describing. Auto-detect it so clients running
        # an older admin page (or omitting the mode form field) still use the
        # per-prompt audio timing columns instead of the legacy segment schema.
        timeline_columns = set(rows[0])
        prompt_timing_columns = {"audio_start_sec", "audio_end_sec", "display_duration_sec"}
        if prompt_timing_columns.issubset(timeline_columns):
            mode = "prompt_timeline"
        if mode == "prompt_timeline":
            required = {"audio_start_sec", "audio_end_sec", "display_duration_sec"}
            if not required.issubset(timeline_columns) or not ({"video_file", "source_file"} & timeline_columns):
                raise HTTPException(400, "Timeline mới cần audio_start_sec, audio_end_sec, display_duration_sec và video_file hoặc source_file.")
        elif not {"segment", "scene_start", "scene_end"}.issubset(rows[0]):
            raise HTTPException(400, "timeline.csv cần có các cột segment, scene_start, scene_end.")
        if mode == "prompt_timeline":
            total_clips = len(rows)
        else:
            total_clips = sum(int(row["scene_end"]) - int(row["scene_start"]) + 1 for row in rows)
        update_job(progress=2, stage="probe", total=total_clips, completed=0)
        audio_files = {}
        for name in os.listdir(audio_dir):
            match = re.match(r"^(\d+)(?:[_. -].*)?\.(mp3|wav|m4a|aac|ogg|flac)$", name, re.I)
            if match:
                audio_files[int(match.group(1))] = os.path.join(audio_dir, name)
        video_files = {}
        for name in os.listdir(video_dir):
            match = re.match(r"^(\d+)(?:[_. -].*)?\.(mp4|mov|mkv|webm|m4v)$", name, re.I)
            if match:
                video_files[int(match.group(1))] = os.path.join(video_dir, name)
        ffmpeg = _ffmpeg_executable()
        # Keep the original assembly output profile: 1280x720 at 30 fps.
        source_sizes = []
        source_has_audio = {}
        for source in video_files.values():
            probe = subprocess.run([ffmpeg, "-i", source], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
            match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", probe.stderr)
            if match:
                source_sizes.append((int(match.group(1)), int(match.group(2))))
                source_has_audio[source] = bool(re.search(r"Audio:", probe.stderr))
        if not source_sizes and mode != "prompt_timeline":
            raise HTTPException(400, "Không đọc được độ phân giải của các video nguồn.")
        target_width, target_height = 1280, 720
        update_job(progress=4, stage="encode")
        intermediates = []
        completed_clips = 0
        if mode == "prompt_timeline":
            path_root = os.path.dirname(timeline_path)
            video_media_files = [os.path.join(root, name) for root, _, names in os.walk(video_dir)
                                 for name in names if name.lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".m4v"))]
            video_media_files.sort(key=lambda path: (
                int((re.search(r"\d+", os.path.basename(path)) or ["999999999"])[0]),
                os.path.relpath(path, video_dir).casefold()))
            def resolve_media(value, expected_dir):
                raw = (value or "").replace("\\", "/").strip()
                rel = PurePosixPath(raw)
                if not raw or rel.is_absolute() or ".." in rel.parts:
                    raise HTTPException(400, f"Đường dẫn media không hợp lệ trong timeline: {value}")
                candidates = [os.path.join(path_root, *rel.parts)]
                if rel.parts and rel.parts[0].lower() == expected_dir:
                    candidates.append(os.path.join(path_root, *rel.parts[1:]))
                candidates.extend(os.path.join(expected_dir == "audio" and audio_dir or video_dir, *rel.parts[-1:]))
                for candidate in candidates:
                    if os.path.isfile(candidate):
                        return candidate
                raise HTTPException(400, f"Không tìm thấy file {expected_dir} trong timeline: {value}")

            for idx, row in enumerate(rows, 1):
                try:
                    audio_start = float(row["audio_start_sec"])
                    audio_end = float(row["audio_end_sec"])
                    display_duration = float(row["display_duration_sec"])
                    source_in = float(row.get("source_in_sec") or 0)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"Thời gian không hợp lệ ở dòng timeline {idx + 1}.")
                if audio_start < 0 or source_in < 0 or audio_end <= audio_start or display_duration <= 0:
                    raise HTTPException(400, f"Khoảng thời gian không hợp lệ ở dòng timeline {idx + 1}.")
                video_value = row.get("video_file") or row.get("source_file") or ""
                try:
                    source = resolve_media(video_value, "video")
                except HTTPException:
                    # Timelines may refer to scene_0001.mp4 while the project
                    # stores the same prompt clips as 001.mp4, 002.mp4, etc.
                    scene_number = re.search(r"(\d+)", os.path.basename(video_value))
                    video_index = int(scene_number.group(1)) if scene_number else idx
                    numbered_matches = []
                    for candidate in video_media_files:
                        candidate_name = os.path.basename(candidate)
                        number_match = re.match(r"^(\d+)(?:[-_. ]|$)", candidate_name)
                        if number_match and int(number_match.group(1)) == video_index:
                            numbered_matches.append(candidate)
                    exact_numbered = [candidate for candidate in numbered_matches
                                      if os.path.splitext(os.path.basename(candidate))[0].isdigit()]
                    if exact_numbered:
                        source = exact_numbered[0]
                    elif numbered_matches:
                        source = numbered_matches[0]
                    elif 1 <= video_index <= len(video_media_files):
                        source = video_media_files[video_index - 1]
                    else:
                        raise HTTPException(400, f"Không tìm thấy video '{video_value}' và không có video thứ tự {video_index:03d} trong thư mục video/.")
                piece = os.path.join(work, f"prompt-{idx:05d}.mp4")
                duration = display_duration
                if source not in source_has_audio:
                    probe = subprocess.run([ffmpeg, "-i", source], capture_output=True, text=True,
                                           encoding="utf-8", errors="replace")
                    source_has_audio[source] = bool(re.search(r"Audio:", probe.stderr))
                cmd = [ffmpeg, "-y", "-ss", f"{source_in:.6f}", "-i", source]
                if source_has_audio[source]:
                    cmd.extend(["-map", "0:a:0", "-af", "apad"])
                else:
                    cmd.extend(["-f", "lavfi", "-t", f"{duration:.6f}", "-i",
                                "anullsrc=channel_layout=stereo:sample_rate=48000", "-map", "1:a:0"])
                cmd.extend(["-map", "0:v:0", "-t", f"{duration:.6f}",
                            "-vf", f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,crop={target_width}:{target_height},fps=30,format=yuv420p",
                            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                            "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", piece])
                run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if run.returncode:
                    raise HTTPException(500, f"Không xử lý được prompt {idx}: {run.stderr[-1200:]}")
                intermediates.append(piece)
                completed_clips += 1
                update_job(progress=min(94, 4 + int(90 * completed_clips / max(1, total_clips))),
                           stage="encode", completed=completed_clips, total=total_clips)
        else:
            video_uses = {}
            for row in rows:
                for scene_id in range(int(row["scene_start"]), int(row["scene_end"]) + 1):
                    video_uses[scene_id] = video_uses.get(scene_id, 0) + 1
            intermediates = []
            completed_clips = 0
            for row in rows:
                idx = int(row["segment"])
                if idx not in audio_files:
                    raise HTTPException(400, f"Thiếu audio bắt đầu bằng {idx:03d}_ trong folder audio.")
                start, end = int(row["scene_start"]), int(row["scene_end"])
                scene_ids = list(range(start, end + 1))
                missing = [n for n in scene_ids if n not in video_files]
                if missing:
                    raise HTTPException(400, f"Thiếu video cảnh: {', '.join(f'{n:03d}' for n in missing)}.")
                audio = audio_files[idx]
                duration = _ffmpeg_duration(ffmpeg, audio)
                if duration <= 0:
                    raise HTTPException(400, f"Audio {idx:03d} có thời lượng không hợp lệ.")
                # Prompts request 8-second clips. Never reuse/loop a longer source
                # to fill an audio allocation; take at most its first 8 seconds.
                piece_duration = min(duration / len(scene_ids), 8.0)
                pieces = []
                for pos, scene_id in enumerate(scene_ids):
                    piece = os.path.join(work, f"piece-{idx:04d}-{pos:04d}.mp4")
                    source = video_files[scene_id]
                    cmd = [ffmpeg, "-y", "-i", source]
                    if not source_has_audio.get(source, False):
                        cmd.extend(["-f", "lavfi", "-t", f"{piece_duration:.6f}", "-i",
                                    "anullsrc=channel_layout=stereo:sample_rate=48000"])
                    cmd.extend(["-t", f"{piece_duration:.6f}", "-map", "0:v:0"])
                    cmd.extend(["-map", "0:a:0"] if source_has_audio.get(source, False) else ["-map", "1:a:0"])
                    cmd.extend(["-vf", f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,crop={target_width}:{target_height},fps=30,format=yuv420p",
                           "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                           "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2"])
                    if source_has_audio.get(source, False):
                        cmd.extend(["-af", "apad"])
                    cmd.extend(["-movflags", "+faststart", piece])
                    run = subprocess.run(cmd, capture_output=True, text=True,
                                         encoding="utf-8", errors="replace")
                    if run.returncode:
                        raise HTTPException(500, f"Không xử lý được video {scene_id:03d}: {run.stderr[-1200:]}")
                    pieces.append(piece)
                    completed_clips += 1
                    update_job(progress=min(88, 4 + int(84 * completed_clips / max(1, total_clips))),
                               stage="encode", completed=completed_clips, total=total_clips)
                    video_uses[scene_id] -= 1
                    if video_uses[scene_id] == 0:
                        try:
                            os.remove(video_files[scene_id])
                        except OSError:
                            pass
                concat_file = os.path.join(work, f"concat-{idx:04d}.txt")
                with open(concat_file, "w", encoding="utf-8") as f:
                    for piece in pieces:
                        f.write("file '" + piece.replace("\\", "/").replace("'", "'\\''") + "'\n")
                group = os.path.join(work, f"group-{idx:04d}.mp4")
                run = subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
                                      "-c", "copy", group], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace")
                if run.returncode:
                    raise HTTPException(500, f"Không ghép được phân cảnh {idx:03d}: {run.stderr[-1200:]}")
                for path in [*pieces, concat_file]:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                try:
                    os.remove(audio)
                except OSError:
                    pass
                intermediates.append(group)
                update_job(progress=min(94, 88 + int(6 * len(intermediates) / max(1, len(rows)))), stage="audio/concat")
        final_name = f"assembly-{uuid.uuid4().hex[:12]}.mp4"
        final_path = os.path.join(CFG.media_dir, final_name)
        os.makedirs(CFG.media_dir, exist_ok=True)
        final_list = os.path.join(work, "final.txt")
        update_job(progress=96, stage="finalize")
        with open(final_list, "w", encoding="utf-8") as f:
            for group in intermediates:
                f.write("file '" + group.replace("\\", "/").replace("'", "'\\''") + "'\n")
        run = subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", final_list,
                              "-c", "copy", "-movflags", "+faststart", final_path], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        if run.returncode:
            raise HTTPException(500, f"Không xuất được video hoàn chỉnh: {run.stderr[-1200:]}")
        download_token = uuid.uuid4().hex + uuid.uuid4().hex
        expires_at = time.time() + 30 * 60
        with _ASSEMBLY_DOWNLOAD_LOCK:
            for old_token, (_, expiry) in list(_ASSEMBLY_DOWNLOADS.items()):
                if expiry <= time.time():
                    _ASSEMBLY_DOWNLOADS.pop(old_token, None)
            _ASSEMBLY_DOWNLOADS[download_token] = (final_path, expires_at)
            _write_assembly_state("downloads", download_token,
                                  {"path": final_path, "expires_at": expires_at})
        assembled = True
        with _ASSEMBLY_DOWNLOAD_LOCK:
            _ASSEMBLY_JOBS[job_id] = {"status": "completed", "download_url": f"/admin/video-assembly/download/{download_token}",
                                      "filename": final_name, "size": os.path.getsize(final_path), "expires_at": expires_at,
                                      "progress": 100, "stage": "completed"}
            _write_assembly_state("jobs", job_id, _ASSEMBLY_JOBS[job_id])
        return {"download_url": f"/admin/video-assembly/download/{download_token}",
                "filename": final_name, "size": os.path.getsize(final_path)}
    except HTTPException as exc:
        with _ASSEMBLY_DOWNLOAD_LOCK:
            _ASSEMBLY_JOBS[job_id] = {"status": "failed", "error": str(exc.detail)}
            _write_assembly_state("jobs", job_id, _ASSEMBLY_JOBS[job_id])
        if "No space left on device" in str(exc.detail):
            raise HTTPException(507, "Ổ đĩa tạm hoặc ổ đĩa dữ liệu đã hết dung lượng. Hãy giải phóng dung lượng rồi thử ghép lại.")
        raise
    except OSError as exc:
        with _ASSEMBLY_DOWNLOAD_LOCK:
            _ASSEMBLY_JOBS[job_id] = {"status": "failed", "error": str(exc)}
            _write_assembly_state("jobs", job_id, _ASSEMBLY_JOBS[job_id])
        if getattr(exc, "errno", None) == 28:
            raise HTTPException(507, "Ổ đĩa tạm hoặc ổ đĩa dữ liệu đã hết dung lượng. Hãy giải phóng dung lượng rồi thử ghép lại.")
        raise HTTPException(400, str(exc))
    except Exception as exc:
        with _ASSEMBLY_DOWNLOAD_LOCK:
            _ASSEMBLY_JOBS[job_id] = {"status": "failed", "error": str(exc)}
            _write_assembly_state("jobs", job_id, _ASSEMBLY_JOBS[job_id])
        raise HTTPException(400, str(exc))
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
        if final_path and not assembled:
            try:
                os.remove(final_path)
            except OSError:
                pass


@app.get("/admin/video-assembly/jobs/{job_id}")
def get_video_assembly_job(job_id: str, _=Depends(auth)):
    with _ASSEMBLY_DOWNLOAD_LOCK:
        job = _ASSEMBLY_JOBS.get(job_id) or _read_assembly_state("jobs", job_id)
        if job:
            _ASSEMBLY_JOBS[job_id] = job
        if job and job.get("expires_at", time.time() + 1) <= time.time():
            _ASSEMBLY_JOBS.pop(job_id, None)
            job = None
        return dict(job) if job else {"status": "not_found"}


@app.get("/admin/video-assembly/download/{token}")
def download_assembled_video(token: str):
    with _ASSEMBLY_DOWNLOAD_LOCK:
        item = _ASSEMBLY_DOWNLOADS.get(token)
        if not item:
            saved = _read_assembly_state("downloads", token)
            if saved and saved.get("path"):
                item = (saved["path"], float(saved.get("expires_at") or 0))
                _ASSEMBLY_DOWNLOADS[token] = item
        if item and item[1] <= time.time():
            _ASSEMBLY_DOWNLOADS.pop(token, None)
            item = None
    if not item or not os.path.isfile(item[0]):
        raise HTTPException(404, "Liên kết tải video không tồn tại hoặc đã hết hạn.")
    path = item[0]
    return FileResponse(path, filename=os.path.basename(path), media_type="video/mp4")


# ------------------------- 管理：总览 -------------------------
@app.get("/admin/status")
def admin_status(_=Depends(auth)):
    base = _public_base()
    return {
        "accounts": store.list_accounts(),
        "stats": store.stats(),
        "tasks": store.list_tasks(20),
        "media_count": len(os.listdir(CFG.media_dir))
        if os.path.isdir(CFG.media_dir) else 0,
        "browser_running": bool(engine.browser and
                                (engine.proc is None or engine.proc.poll() is None)),
        "essential_cookies": list(ESSENTIAL_COOKIES),
        "essential_cookie_alternatives": [],
        "base_url": f"{base}/v1" if base else "",
        "config": {"site": CFG.site_url, "cdp_port": CFG.cdp_port,
                   "max_concurrency": CFG.max_concurrency,
                   "per_account_concurrency": CFG.per_account_concurrency,
                   "image_timeout": CFG.image_timeout,
                   "video_timeout": CFG.video_timeout,
                   "host": CFG.host, "port": CFG.port,
                   "public_base": base},
    }


@app.patch("/admin/config/concurrency")
def update_concurrency(payload: dict = Body(...), _=Depends(auth)):
    value = payload.get("max_concurrency")
    if isinstance(value, bool):
        raise HTTPException(400, "Số tác vụ đồng thời phải nằm trong khoảng 1–10")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "Số tác vụ đồng thời phải nằm trong khoảng 1–10")
    if not 1 <= value <= MAX_CONCURRENCY:
        raise HTTPException(400, "Số tác vụ đồng thời phải nằm trong khoảng 1–10")
    try:
        _persist_env("MUSE2API_CONCURRENCY", str(value))
    except OSError as exc:
        raise HTTPException(500, f"Không lưu được cấu hình: {exc}") from exc
    CFG.max_concurrency = value
    GEN_LOCK.set_limit(value)
    return {"ok": True, "max_concurrency": value,
            "message": "Đã áp dụng ngay; tác vụ đang chạy vẫn tiếp tục bình thường."}


# ------------------------- 管理：账号池 -------------------------
@app.get("/admin/accounts")
def list_accounts(_=Depends(auth)):
    return {"accounts": store.list_accounts(), "stats": store.stats()}


@app.put("/admin/accounts/order")
def reorder_accounts(req: AccountOrderRequest, _=Depends(auth)):
    return {"ids": store.reorder_accounts(req.ids)}


@app.post("/admin/accounts")
def add_account(req: AccountRequest, _=Depends(auth)):
    added: list[dict] = []

    if req.batch:
        for label, cookies in parse_batch(req.batch):
            acc = store.add_account(cookies, label)
            missing = missing_essential_cookies(cookies)
            added.append({"id": acc["id"], "label": acc["label"],
                          "cookie_count": len(cookies),
                          "expires_at": acc.get("expires_at"),
                          "essential_ok": not missing,
                          "essential_missing": missing})

    cookies = dict(req.cookies)
    if req.cookie_header:
        cookies.update(parse_cookie_text(req.cookie_header))
    if cookies:
        acc = store.add_account(cookies, req.label, cookies_exp=req.expires)
        missing = missing_essential_cookies(cookies)
        added.append({"id": acc["id"], "label": acc["label"],
                      "cookie_count": len(cookies),
                      "expires_at": acc.get("expires_at"),
                      "essential_ok": not missing,
                      "essential_missing": missing})

    if not added:
        raise HTTPException(400, "未解析到任何 cookie，请检查格式")

    # Validate each account independently; cookies from separate accounts cannot be combined.
    incomplete = [account for account in added if not account["essential_ok"]]
    missing = sorted({name for account in incomplete
                      for name in account["essential_missing"]})
    return {"added": added, "count": len(added),
            "essential_missing": missing,
            "incomplete_count": len(incomplete),
            "warning": (f"缺少核心 cookie：{', '.join(missing)}；{len(incomplete)}/{len(added)} 个账号不完整"
                        if missing else "")}


@app.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(auth)):
    acc = store.update_account(aid, label=req.label, enabled=req.enabled)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return {k: v for k, v in acc.items() if k != "cookies"} | {
        "cookie_count": len(acc.get("cookies", {}))}


@app.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(auth)):
    ok = store.delete_account(aid)
    if not ok:
        raise HTTPException(404, "账号不存在")
    return {"deleted": True, "id": aid}


@app.post("/admin/accounts/{aid}/test")
async def test_account(aid: str, _=Depends(auth)):
    """真实打开 muse.ai 验证该账号 cookie 是否仍可登录。"""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK:
            try:
                engine.start()
                engine.refresh(acc["cookies"], acc.get("cookies_exp"))
                synced = _sync_cookies(aid)
                quota = None
                quota_error = None
                try:  # Session validity and quota readability are independent results.
                    quota = engine.quota(acc["cookies"],
                                         acc.get("cookies_exp"), account_id=aid)
                    if quota.get("found"):
                        quota["checked_at"] = int(time.time())
                        store.update_account(aid, quota=quota)
                    else:
                        quota_error = "额度面板未读到有效数据；旧缓存未覆盖"
                        quota = None
                except Exception as exc:  # noqa: BLE001
                    quota_error = f"{type(exc).__name__}；额度缓存未更新"
                store.mark(aid, True, "会话有效" + ("；额度读取失败" if quota_error else ""))
                return {"ok": True, "message": "会话有效，可正常生成",
                        "synced": synced, "quota": quota,
                        "quota_error": quota_error}
            except MuseAuthError as exc:
                store.mark(aid, False, str(exc)[:200])
                return {"ok": False, "message": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                store.touch_keepalive(aid, None, f"测试未确认（保留账号状态）: {str(exc)[:200]}")
                return {"ok": False, "message": str(exc)[:200]}
            finally:
                engine.close_current_page()

    res = await asyncio.to_thread(_probe)
    if not res["ok"]:
        return JSONResponse(res, status_code=200)
    return res


@app.post("/admin/accounts/{aid}/relogin")
async def relogin_account(aid: str, _=Depends(auth)):
    return await test_account(aid, _)


@app.post("/admin/accounts/{aid}/cookies")
def update_cookies(aid: str, payload: dict = Body(...), _=Depends(auth)):
    """更新某个账号的 cookie（用于会话过期后补新 cookie）。"""
    cookies = dict(payload.get("cookies") or {})
    if payload.get("cookie_header"):
        cookies.update(parse_cookie_text(payload["cookie_header"]))
    if not cookies:
        raise HTTPException(400, "未解析到 cookie")
    exp = {k: int(v) for k, v in (payload.get("expires") or {}).items()
           if _pos(v)}
    # 补入的是「全新会话」的 cookie，有效期估算锚点必须重置到当下，
    # 否则会沿用旧会话的锚点，把剩余天数算少。
    acc = store.update_account(aid, cookies=cookies, ok=None,
                               note="已更新 cookie",
                               expiry_anchor=int(time.time()),
                               cookies_exp=exp or None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "id": aid, "cookie_count": len(cookies),
            "expires_at": acc.get("expires_at")}


@app.post("/admin/relogin")
def relogin(_=Depends(auth)):
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    try:
        with GEN_LOCK:
            engine.start()
            engine.refresh(acc["cookies"], acc.get("cookies_exp"))
        return {"ok": True, "account": acc["id"]}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    finally:
        engine.close_current_page()


# ------------------------- 管理：额度 -------------------------
@app.post("/admin/accounts/{aid}/quota")
async def query_quota(aid: str, _=Depends(auth)):
    """打开 muse.ai 的 Settings 面板读该账号的额度（实时），并缓存到账号记录。

    返回例：{"plan":"Free plan","weekly_reset":"Sep 30",
             "weekly_used_pct":1,"extra_left":"2B tokens left",
             "extra_used_pct":0,"extra_expires":"never"}
    """
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK:
            try:
                engine.start()
                q = engine.quota(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
                if not q.get("found"):
                    raise MuseGenerationError("额度面板未读到有效数据；旧缓存未覆盖")
                q["checked_at"] = int(time.time())
                store.update_account(aid, quota=q)
                return q
            finally:
                engine.close_current_page()

    try:
        return await asyncio.to_thread(_probe)
    except MuseAuthError as exc:
        store.mark(aid, False, str(exc)[:200])
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"额度查询失败: {exc}") from exc


@app.post("/admin/quota")
async def query_any_quota(_=Depends(auth)):
    """用当前最久未用的可用账号查一次额度（同池账号共享同一 muse.ai 计划的
    通常只有一人使用时够用；多账号时建议按账号查）。"""
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    return await query_quota(acc["id"], _)


# ------------------------- 管理：接入信息 / API Key -------------------------
def _public_base() -> str:
    return (CFG.public_base or "").rstrip("/")


@app.get("/admin/apikey")
def get_apikey(_=Depends(auth)):
    base = _public_base()
    base_url = f"{base}/v1" if base else ""
    return {"api_key": CFG.api_key, "base_url": base_url,
            "models_url": f"{base}/v1/models" if base else "",
            "media_url": f"{base}/v1/media/{{name}}" if base else "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    """生成新的 API Key，写入 .env 并立即生效（不用重启）。"""
    import secrets
    new_key = "m2a_" + secrets.token_hex(24)
    old = CFG.api_key
    CFG.api_key = new_key
    _persist_env("MUSE2API_KEY", new_key)
    return {"ok": True, "api_key": new_key, "previous": old,
            "message": "已生成新 Key 并立即生效；旧 Key 已失效，请更新下游项目"}


def _persist_env(key: str, value: str):
    """把配置写回 .env（保留其它行，原子替换）。"""
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            found = True
    if not found:
        lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    os.replace(tmp, path)


# ------------------------- 管理：Cookie 获取 -------------------------
@app.get("/admin/cookie-helper")
def cookie_helper(download: int = 0):
    """返回本机取 cookie 的助手脚本（进阶方式，需要装 Python）。"""
    p = os.path.join(BASE_DIR, "tools", "get_muse_cookie.py")
    if not os.path.isfile(p):
        raise HTTPException(404, "助手脚本缺失")
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="get_muse_cookie.py"'
    return FileResponse(p, media_type="text/x-python", headers=headers)


@app.get("/admin/extension")
def extension_zip():
    """把浏览器扩展打包成 zip 返回（推荐方式，零命令行）。

    用户下载后解压 → chrome://extensions 开发者模式加载 → 点一下就导入 cookie。
    """
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(src)):
            p = os.path.join(src, name)
            if os.path.isfile(p):
                z.write(p, os.path.join("muse2api-extension", name))
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="muse2api-extension.zip"',
                 "Cache-Control": "no-store"})


@app.get("/admin/extension/files")
def extension_files():
    """列出扩展目录内容（前端展示用）。"""
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    return {"files": sorted(f for f in os.listdir(src)
                            if os.path.isfile(os.path.join(src, f)))}


# ------------------------- 管理：任务 / 媒体 -------------------------
@app.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(auth)):
    return {"tasks": store.list_tasks(limit)}


@app.delete("/admin/tasks/{tid}")
def del_task(tid: str, _=Depends(auth)):
    if not store.delete_task(tid):
        raise HTTPException(404, "任务不存在")
    return {"deleted": True}


@app.post("/admin/tasks/{tid}/cancel")
def cancel_task(tid: str, _=Depends(auth)):
    task = store.get_task(tid)
    if not task or task.get("kind") not in ("image", "video"):
        raise HTTPException(404, "Generation task not found")
    if task.get("status") not in ("queued", "processing"):
        return {"cancelled": False, "status": task.get("status"), "stop_clicked": False}
    with TASK_CANCEL_LOCK:
        event = TASK_CANCEL_EVENTS.get(tid)
    # A persisted queued/processing row can outlive the in-memory worker registry
    # (for example after a server restart). Still stop its isolated Muse session
    # and clear the stale running state so the prompt can be retried.
    if event:
        event.set()
    stop_clicked = False
    try:
        stop_clicked = bool(engine.task_views.cancel_generation(tid))
    except Exception as exc:  # noqa: BLE001
        log.info("Muse stop control unavailable for task %s: %s", tid, exc)
    store.update_task(tid, status="cancelled", error="Cancelled by user")
    return {"cancelled": True, "status": "cancelled", "stop_clicked": stop_clicked}


@app.post("/admin/tasks/clear")
def clear_tasks(payload: dict = Body(default={}), _=Depends(auth)):
    return {"removed": store.clear_tasks(int(payload.get("keep") or 0))}


@app.get("/admin/media")
def admin_media(_=Depends(auth)):
    d = CFG.media_dir
    items = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            items.append({"name": name, "url": media_url(name), "bytes": os.path.getsize(p),
                          "mtime": int(os.path.getmtime(p)),
                          "kind": "video" if ext in (".mp4", ".webm", ".mov") else "image"})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"media": items, "count": len(items)}


@app.post("/admin/media/delete")
def delete_media(payload: dict = Body(default={}), _=Depends(auth)):
    names = payload.get("names", [])
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise HTTPException(400, "names 必须是文件名数组")
    d = CFG.media_dir
    removed = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or "/" in name or "\\" in name or ".." in name:
            errors.append(f"非法文件名: {name}")
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed += 1
            except OSError as e:
                errors.append(f"{name}: {str(e)}")
    return {"removed": removed, "errors": errors}


@app.delete("/admin/media/{name}")
def delete_single_media(name: str, _=Depends(auth)):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    try:
        os.remove(p)
        return {"status": "ok", "deleted": name}
    except OSError as e:
        raise HTTPException(500, f"删除失败: {e}")


# ------------------------- Exact task browser viewer -------------------------
def _task_session_info(task_id):
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "Không tìm thấy tác vụ.")
    try:
        view = engine.task_views.info(task_id)
    except FileNotFoundError:
        return {"available":False, "task_id":task_id, "status":task.get("status"),
                "account_label":task.get("account_label"),
                "can_reopen":bool(task.get("account") and task.get("muse_url") and task.get("status") in ("failed","completed","succeeded")),
                "message":"Phiên gốc chưa được tạo hoặc đã hết thời gian giữ. Có thể mở lại nếu tác vụ có tài khoản và URL Muse."}
    if task.get("account") and view["account_id"] != task["account"]:
        raise HTTPException(409, "Phiên còn giữ thuộc lần thử trước. Không mở nhầm tài khoản của lần thử hiện tại.")
    account = store.get_account(view["account_id"]) or {}
    return {"available":True, "task_id":task_id, "status":task.get("status"),
            "account_label":account.get("label", task.get("account_label", view["account_id"])),
            "active":view["active"], "can_scroll":view["can_scroll"], "expires_in":view["expires_in"],
            "reopened":bool(task.get("session_reopened"))}


@app.get("/admin/tasks/{task_id}/session")
def task_session_info(task_id: str, _=Depends(auth)):
    return _task_session_info(task_id)


@app.get("/admin/tasks/{task_id}/session/screenshot")
def task_session_screenshot(task_id: str, _=Depends(auth)):
    if not _task_session_info(task_id)["available"]:
        raise HTTPException(410, "Phiên trình duyệt đã đóng hoặc chưa được tạo.")
    try:
        data = engine.task_views.screenshot(task_id)
    except FileNotFoundError as exc:
        raise HTTPException(410, "Phiên trình duyệt không còn tồn tại.") from exc
    except RuntimeError as exc:
        raise HTTPException(502, "Không đọc được hình ảnh của phiên trình duyệt.") from exc
    return Response(data, media_type="image/jpeg", headers={"Cache-Control":"no-store"})


@app.post("/admin/tasks/{task_id}/session/scroll")
def task_session_scroll(task_id: str, payload: dict = Body(...), _=Depends(auth)):
    if not _task_session_info(task_id)["available"]:
        raise HTTPException(410, "Phiên trình duyệt không còn tồn tại.")
    try:
        delta = max(-1200, min(1200, int(payload.get("delta",0))))
        engine.task_views.scroll(task_id, delta)
    except (TypeError,ValueError) as exc:
        raise HTTPException(400, "Giá trị cuộn không hợp lệ.") from exc
    except PermissionError as exc:
        raise HTTPException(409, "Chỉ xem khi tác vụ đang chạy; có thể cuộn sau khi tác vụ kết thúc.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(410, "Phiên trình duyệt không còn tồn tại.") from exc
    except RuntimeError as exc:
        raise HTTPException(502, "Không cuộn được phiên trình duyệt.") from exc
    return {"ok":True}


@app.post("/admin/tasks/{task_id}/session/reopen")
async def task_session_reopen(task_id: str, _=Depends(auth)):
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "Không tìm thấy tác vụ.")
    if task.get("status") not in ("completed","failed","succeeded"):
        raise HTTPException(409, "Tác vụ còn chạy. Hãy xem phiên gốc.")
    account = store.get_account(task.get("account"))
    if not account or not account.get("cookies") or not task.get("muse_url"):
        raise HTTPException(409, "Tác vụ chưa có đủ tài khoản và URL để mở lại.")
    def reopen():
        if _task_session_info(task_id)["available"]:
            return
        if not GEN_LOCK.acquire(timeout=20):
            raise MuseGenerationError("Browser is busy; try again later.")
        reserved = None
        try:
            reserved = ACCOUNT_POOL.acquire(account_id=account["id"], timeout=20, record_use=False)
            if not _task_session_info(task_id)["available"]:
                engine.reopen_task_view(task_id, reserved, task["muse_url"])
                store.update_task(task_id, session_reopened=True)
        finally:
            if reserved:
                ACCOUNT_POOL.release(account["id"])
            GEN_LOCK.release()
    try:
        await asyncio.to_thread(reopen)
    except (MuseAuthError,MuseGenerationError,ValueError) as exc:
        raise HTTPException(502, "Không mở lại được phiên của tài khoản này: " + str(exc)) from exc
    return _task_session_info(task_id)


_COLLECTING_TASKS: set[str] = set()
_COLLECTING_LOCK = threading.Lock()


def _collect_accounts(preferred_id: str | None) -> list[dict]:
    """The account stored with the task first, then the other signed-in cookies."""
    ordered = []
    seen = set()

    def add(account):
        if not account or not account.get("id") or account["id"] in seen or not account.get("cookies"):
            return
        if account.get("enabled", True) is False:
            return
        seen.add(account["id"])
        ordered.append(account)

    add(store.get_account(preferred_id) if preferred_id else None)
    for item in store.list_accounts():
        if item.get("enabled", True) is False or item.get("essential_ok") is False or not item.get("cookie_count"):
            continue
        add(store.get_account(item.get("id")))
    return ordered


def _video_file_saved(task_id: str) -> bool:
    task = store.get_task(task_id) or {}
    if task.get("status") not in ("completed", "succeeded"):
        return False
    if task.get("url"):
        return True
    result = task.get("result")
    return isinstance(result, dict) and bool(result.get("url"))


def _task_video_duration(task: dict) -> int:
    """Get the requested duration, including tasks created before it was stored separately."""
    try:
        duration = int(task.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration > 0:
        return duration
    prompt = str(task.get("api_prompt") or task.get("prompt") or "")
    match = re.search(r"Duration:\s*exactly\s*(\d+)\s*seconds", prompt, re.IGNORECASE)
    return int(match.group(1)) if match else 6


def _collect_from_account(account: dict, muse_url: str, kind: str, timeout: int = 60,
                          lock_timeout: int = 120, account_wait: int = 60,
                          expected_duration: int = 10) -> dict:
    reserved = None
    worker_engine = None
    worker_profile = None
    try:
        reserved = ACCOUNT_POOL.acquire(account_id=account["id"], timeout=account_wait,
                                        record_use=False, allow_overflow=True)
        # Each recovery gets its own short-lived Chromium process/profile/CDP port.
        # It can run beside all configured generation workers and is fully closed
        # and removed after this single rendered file has been retrieved.
        os.makedirs(CFG.profile_root, exist_ok=True)
        worker_profile = tempfile.mkdtemp(prefix="muse2api-recovery-", dir=CFG.profile_root)
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            worker_port = port_probe.getsockname()[1]
        worker_cfg = copy.copy(CFG)
        worker_cfg.cdp_port = worker_port
        worker_cfg.base_dir = worker_profile
        worker_cfg.profile_root = os.path.join(worker_profile, "profiles")
        worker_cfg.ensure_dirs()
        worker_engine = MuseEngine(worker_cfg)
        worker_engine.start()
        result = worker_engine.collect_rendered(
            reserved["cookies"], muse_url, expect=kind, timeout=timeout,
            expires=reserved.get("cookies_exp"), account_id=reserved["id"],
            expected_duration=expected_duration)
        os.makedirs(CFG.media_dir, exist_ok=True)
        destination = os.path.join(CFG.media_dir, result["filename"])
        shutil.move(result["path"], destination)
        result["path"] = destination
        return result
    finally:
        if worker_engine:
            try:
                worker_engine.stop()
            except Exception:
                log.warning("Could not stop temporary video-recovery worker", exc_info=True)
        if worker_profile:
            shutil.rmtree(worker_profile, ignore_errors=True)
        if reserved:
            ACCOUNT_POOL.release(reserved["id"])


_HARVEST_TURN = threading.Lock()
_RECOVERY_WORKER_LOCK = threading.Lock()
_HARVEST_NEXT: dict[str, float] = {}
_HARVEST_MISS: dict[str, int] = {}


def _download_rendered(task: dict, muse_url: str, quick: bool = False):
    accounts = _collect_accounts(task.get("account"))
    if quick:
        accounts = accounts[:1]
    if not accounts:
        raise MuseGenerationError("Không còn cookie của tài khoản đã chạy prompt này.")
    kind = "video" if task.get("kind") == "video" else "image"
    timeout = 60
    lock_timeout = 8 if quick else 120
    account_wait = 8 if quick else 60
    if not _RECOVERY_WORKER_LOCK.acquire(timeout=lock_timeout):
        raise MuseGenerationError("Worker lấy video khác đang chạy. Hãy thử lại sau.")
    try:
        errors = []
        for account in accounts:
            try:
                return _collect_from_account(
                    account, muse_url, kind, timeout=timeout,
                    lock_timeout=lock_timeout, account_wait=account_wait,
                    expected_duration=_task_video_duration(task)), account
            except MuseGenerationError as exc:
                message = str(exc)
                if "đang bận" in message or "忙" in message:
                    raise
                errors.append(message)
        raise MuseGenerationError(errors[0] if errors else "Không lấy được file đã render.")
    finally:
        _RECOVERY_WORKER_LOCK.release()


def _harvest_ready(task: dict, now: float) -> bool:
    if "MUSE_ACTION_REQUIRED:" in str(task.get("error") or ""):
        return False
    if task.get("kind") != "video" or task.get("status") not in ("processing", "failed"):
        return False
    if not _safe_muse_thread(task.get("muse_url")) or not task.get("account"):
        return False
    started_at = task.get("processing_started_at") or task.get("created_at") or 0
    if now - float(started_at) < 45:
        return False
    return now >= _HARVEST_NEXT.get(task["id"], 0)


def _pick_harvest_task(preferred: dict | None) -> dict | None:
    """One row used to be retried forever and the finished videos behind it stayed at 92%."""
    now = time.time()
    if preferred and _harvest_ready(preferred, now) and _HARVEST_MISS.get(preferred["id"], 0) == 0:
        return preferred
    best = None
    best_rank = None
    for task in store.list_tasks(None, kind="video", statuses=("processing", "failed")):
        if not _harvest_ready(task, now):
            continue
        rank = (_HARVEST_MISS.get(task["id"], 0), float(task.get("updated_at") or 0))
        if best_rank is None or rank < best_rank:
            best, best_rank = task, rank
    if best:
        return best
    if preferred and _harvest_ready(preferred, now):
        return preferred
    return None


def _schedule_rendered_harvest(task: dict) -> None:
    """A stuck processing row may already have a rendered video. Download it without a new prompt."""
    picked = _pick_harvest_task(task)
    if not picked:
        return
    task_id = picked["id"]
    muse_url = _safe_muse_thread(picked.get("muse_url"))
    with _COLLECTING_LOCK:
        if task_id in _COLLECTING_TASKS:
            return
        _COLLECTING_TASKS.add(task_id)

    def run():
        try:
            current = store.get_task(task_id) or {}
            if current.get("status") not in ("processing", "failed") or _video_file_saved(task_id):
                return
            if not _HARVEST_TURN.acquire(blocking=False):
                return
            try:
                result, account = _download_rendered(current, muse_url, quick=True)
                if account.get("id") != current.get("account"):
                    store.update_task(task_id, account=account.get("id"),
                                      account_label=account.get("label") or account.get("id"))
                    current = store.get_task(task_id) or current
                if (store.get_task(task_id) or {}).get("status") in ("processing", "failed"):
                    _collected_task_payload(current, result)
                _HARVEST_MISS.pop(task_id, None)
                _HARVEST_NEXT.pop(task_id, None)
            finally:
                _HARVEST_TURN.release()
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            log.info("Chưa lấy được video đã render của %s: %s", task_id, message)
            if "đang bận" in message or "忙" in message:
                _HARVEST_NEXT[task_id] = time.time() + 20
                return
            misses = _HARVEST_MISS.get(task_id, 0) + 1
            _HARVEST_MISS[task_id] = misses
            _HARVEST_NEXT[task_id] = time.time() + 90
            current = store.get_task(task_id) or {}
            age = time.time() - float(current.get("created_at") or 0)
            if (misses >= 2 and current.get("status") == "processing"
                    and "chưa có video" in message and age >= CFG.video_timeout):
                store.update_task(
                    task_id, status="failed", progress=0,
                    error="Muse chưa gắn file video trong cuộc trò chuyện này. Bấm Lấy video đã render để kiểm tra lại, không gửi prompt mới.")
        finally:
            with _COLLECTING_LOCK:
                _COLLECTING_TASKS.discard(task_id)

    threading.Thread(target=run, daemon=True).start()


def _task_media_path(task: dict) -> str:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    name = str((result or {}).get("filename") or "")
    if not name:
        name = str(task.get("url") or "").rsplit("/", 1)[-1].split("?", 1)[0]
    if not name or "/" in name or "\\" in name or ".." in name:
        return ""
    return os.path.join(CFG.media_dir, name)


def _drop_page_clip(task: dict) -> bool:
    """A shared page clip is not a finished thread. Clear it so the row can be collected again."""
    path = _task_media_path(task)
    if not file_is_page_clip(path):
        return False
    try:
        os.remove(path)
    except OSError:
        pass
    store.update_task(
        task["id"], status="failed", progress=0, url=None, result=None, video=None,
        error="File đã lưu không phải video của cuộc trò chuyện. Bấm Nối lại để tải video trong thread, không gửi prompt mới.")
    return True


def _collected_task_payload(task: dict, res: dict) -> None:
    url = media_url(res["filename"])
    kind = "video" if task.get("kind") == "video" else "image"
    fields = {"status": "completed", "progress": 100, "error": None, "url": url,
              "result": {"url": url, "filename": res["filename"],
                         "bytes": res["size"], "kind": res.get("kind") or kind}}
    if kind == "video":
        fields["video"] = {"url": url}
    store.update_task(task["id"], **fields)


def _safe_muse_thread(value: str | None) -> str:
    from urllib.parse import urlsplit, urlunsplit
    parsed = urlsplit(str(value or "").strip())
    if (parsed.scheme == "https" and parsed.hostname in ("muse.ai", "www.muse.ai")
            and re.fullmatch(r"/thread/[^/]+/?", parsed.path)
            and parsed.path.rstrip("/") != "/thread/new"):
        return urlunsplit(("https", parsed.hostname, parsed.path.rstrip("/"), "", ""))
    return ""


@app.post("/admin/tasks/{task_id}/collect")
async def collect_rendered_task(task_id: str, payload: dict | None = Body(default=None), _=Depends(auth)):
    """Download media Muse already rendered. This does not submit the prompt again."""
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "Không tìm thấy tác vụ.")
    if task.get("status") in ("completed", "succeeded") and (
            task.get("url") or (isinstance(task.get("result"), dict) and task["result"].get("url"))):
        if not _drop_page_clip(task):
            return dict(task)
        task = store.get_task(task_id) or task
    if task.get("status") not in ("failed",):
        raise HTTPException(409, "Chỉ lấy file đã render của tác vụ đang báo thất bại.")
    payload = payload or {}
    muse_url = _safe_muse_thread(task.get("muse_url")) or _safe_muse_thread(payload.get("muse_url"))
    if not muse_url:
        raise HTTPException(409, "Tác vụ chưa có tài khoản và URL cuộc trò chuyện để lấy file.")
    if not task.get("muse_url"):
        store.update_task(task_id, muse_url=muse_url)
        task = store.get_task(task_id) or task
    if not _collect_accounts(task.get("account")):
        raise HTTPException(409, "Không còn cookie của tài khoản đã chạy prompt này.")
    with _COLLECTING_LOCK:
        if task_id in _COLLECTING_TASKS:
            raise HTTPException(409, "Đang lấy file của tác vụ này.")
        _COLLECTING_TASKS.add(task_id)

    def collect():
        return _download_rendered(task, muse_url, quick=False)

    try:
        result, account = await asyncio.to_thread(collect)
        if account.get("id") != task.get("account") or account.get("label") != task.get("account_label"):
            store.update_task(task_id, account=account.get("id"),
                              account_label=account.get("label") or account.get("id"))
            task = store.get_task(task_id) or task
        _collected_task_payload(task, result)
    except (MuseAuthError, MuseGenerationError, ValueError) as exc:
        raise HTTPException(502, "Không lấy được file đã render: " + str(exc)) from exc
    finally:
        with _COLLECTING_LOCK:
            _COLLECTING_TASKS.discard(task_id)
    updated = store.get_task(task_id) or {}
    return dict(updated)


async def _session_view_cleanup():
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(engine.task_views.prune)
        except Exception:
            log.warning("Không dọn được phiên xem trình duyệt", exc_info=True)


# ------------------------- 前端页面 -------------------------
def _admin_html() -> str:
    p = os.path.join(BASE_DIR, "admin.html")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset=utf-8><body style='background:#0b0f19;"
                "color:#e6e8ee;font-family:system-ui;padding:40px'>"
                "<h2>muse2api</h2><p>管理页面文件 admin.html 缺失。</p>"
                "<p>接口可用：<code>/v1/images/generations</code>、"
                "<code>/v1/videos</code></p></body>")


@app.get("/", response_class=HTMLResponse)
def index():
    return _admin_html()


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return _admin_html()



# ------------------------- 账号自动保活与静默续期 -------------------------
KEEPALIVE_LOCK = asyncio.Lock()
KEEPALIVE_STATE = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "next_run": None,
}


def _probe_account_sync(aid: str, check_quota: bool = False) -> dict:
    """通过 muse.ai/api/session 触发 Meta 网关签发新 hatch_vml (+48h) / hatch_sess (+30d) 并唤醒云端 VM。"""
    acc = store.get_account(aid)
    if not acc or not acc.get("cookies"):
        return {"ok": False, "id": aid, "label": (acc or {}).get("label", aid), "error": "账号无有效 cookie"}
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=True)
        store.update_account(
            aid,
            cookies=res["cookies"],
            cookies_exp=res["cookies_exp"],
            ok=True if res.get("ok") else False,
            synced_at=int(time.time()),
        )
        vm_state = res.get("vm_state") or "RUNNING"
        store.touch_keepalive(aid, True, f"会话有效 · 自动保活 (VM: {vm_state})")
        quota = acc.get("quota")
        if check_quota and GEN_LOCK.acquire(blocking=False):
            try:
                engine.start()
                quota = engine.quota(res["cookies"], res["cookies_exp"],
                                     account_id=aid)
                if not quota.get("found"):
                    raise MuseGenerationError("额度面板未读到有效数据；旧缓存未覆盖")
                quota["checked_at"] = int(time.time())
                store.update_account(aid, quota=quota)
            except Exception as qe:  # noqa: BLE001
                log.warning("读取账号 %s 额度失败: %s", aid, qe)
            finally:
                engine.close_current_page()
                GEN_LOCK.release()
        updated = store.get_account(aid) or {}
        return {
            "ok": True,
            "id": aid,
            "label": acc.get("label", aid),
            "vm_id": res.get("vm_id"),
            "vm_state": vm_state,
            "wake_ok": res.get("wake_ok"),
            "expires_at": updated.get("expires_at"),
            "quota": quota,
        }
    except MuseAuthError as exc:
        store.touch_keepalive(aid, False, f"保活认证失败: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        store.touch_keepalive(aid, None, f"保活未确认（保留账号状态）: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": str(exc)}


async def run_keepalive_all(force: bool = False) -> dict:
    """执行账号保活：force=True 时强制刷新所有启用账号，否则刷新临期/未检账号并保持 VM 热备。"""
    async with KEEPALIVE_LOCK:
        now = int(time.time())
        KEEPALIVE_STATE["running"] = True
        KEEPALIVE_STATE["last_run"] = now
        results = []
        skipped = []

        try:
            include_disabled = getattr(CFG, "keepalive_disabled_accounts", False)
            accounts = [a for a in store.list_accounts() if include_disabled or a.get("enabled", True)]
            for a in accounts:
                aid = a["id"]
                label = a.get("label", aid)
                exp_at = a.get("expires_at")
                last_ka = a.get("last_keepalive") or 0

                needs_run = (
                    force
                    or not last_ka
                    or ((now - last_ka) > 6 * 3600 and
                        (not exp_at or exp_at - now < 36 * 3600 or
                         a.get("ok") is not True))
                )
                if not needs_run:
                    skipped.append({"id": aid, "label": label, "reason": "会话充足且近期已保活"})
                    continue

                log.info("【自动保活】正在为账号 %s (%s) 执行静默续期与 VM 唤醒...", label, aid)
                res = await asyncio.to_thread(_probe_account_sync, aid, False)
                results.append(res)
                log.info("【自动保活】账号 %s 执行结果: ok=%s expires_at=%s", label, res.get("ok"), res.get("expires_at"))
                await asyncio.sleep(0.5)

            summary = {
                "checked_at": now,
                "refreshed_count": len(results),
                "refreshed": results,
                "skipped_count": len(skipped),
                "skipped": skipped,
            }
            KEEPALIVE_STATE["last_result"] = summary
            KEEPALIVE_STATE["next_run"] = now + 900
            return summary
        finally:
            KEEPALIVE_STATE["running"] = False


def _warmup_browser_sync():
    """后台静默预热浏览器与首个可用账号的 WebSocket 隧道，使重启后首条请求也秒回。"""
    if getattr(engine, "current_acc_id", None) and engine.page is not None:
        return
    if not GEN_LOCK.acquire(blocking=False):
        return
    acc = None
    try:
        acc = ACCOUNT_POOL.acquire(rotate=False, timeout=0.1, record_use=False)
        log.info("【浏览器预热】正在后台预热账号 %s (%s) 的热备标签页...", acc.get("label"), acc["id"])
        refreshed = _renew_and_persist(acc["id"], wake_vm=True, force=False) or acc
        engine.start()
        engine.ensure_page(refreshed["cookies"], refreshed.get("cookies_exp"), account_id=acc["id"])
        log.info("【浏览器预热】账号 %s (%s) 热备标签页与 WebSocket 已就绪", acc.get("label"), acc["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("【浏览器预热】预热异常: %s", exc)
    finally:
        # Warmup runs in a worker thread; do not retain its account reservation or page.
        engine.close_current_page()
        if acc:
            ACCOUNT_POOL.release(acc["id"])
        GEN_LOCK.release()


async def _keepalive_loop():
    """后台常驻守护任务：每 15 分钟轮询一次账号健康状态并保持 VM 热备。"""
    log.info("【自动保活守护进程】已启动，检测周期: 15 分钟")
    while True:
        try:
            await run_keepalive_all(force=False)
        except Exception as e:  # noqa: BLE001
            log.error("【自动保活守护进程】轮询异常: %s", e)
        await asyncio.sleep(900)


@app.post("/admin/accounts/keepalive")
async def trigger_keepalive_all(force: bool = True, _=Depends(auth)):
    """管理员手动触发一次全账号保活续期。"""
    if KEEPALIVE_STATE["running"]:
        return {"status": "busy", "message": "保活任务正在执行中，请稍候"}
    return await run_keepalive_all(force=force)


@app.post("/admin/accounts/{aid}/keepalive")
async def trigger_keepalive_single(aid: str, _=Depends(auth)):
    """手动针对单个账号执行保活续期。"""
    return await asyncio.to_thread(_probe_account_sync, aid, False)


@app.get("/admin/keepalive/status")
def get_keepalive_status(_=Depends(auth)):
    """获取保活守护协程状态。"""
    return KEEPALIVE_STATE


# ------------------------- 仓库实时更新检测、通知与一键在线升级 -------------------------
@app.on_event("startup")
async def _startup():
    for task in store.list_tasks(None, statuses=("queued", "processing")):
        if task.get("kind") == "video":
            _drop_page_clip(task)
        if task.get("kind") in ("image", "video") and task.get("status") in ("queued", "processing"):
            if task.get("kind") == "video" and _safe_muse_thread(task.get("muse_url")):
                store.delete_task_input(task["id"])
                continue
            store.update_task(task["id"], status="failed", error="服务重启中断了任务，请重新提交")
    store.prune_task_inputs()
    if not CFG.api_key:
        import secrets
        new_key = "m2a_" + secrets.token_hex(24)
        CFG.api_key = new_key
        _persist_env("MUSE2API_KEY", new_key)
        log.info("🔑 未检测到 MUSE2API_KEY，已自动生成初始密钥: %s", new_key)
    asyncio.create_task(_keepalive_loop())
    asyncio.create_task(_session_view_cleanup())


@app.on_event("shutdown")
def _shutdown():
    engine.stop()
