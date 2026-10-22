"""账号池与任务存储（JSON 落盘，原子写）。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

_LOCK = threading.Lock()

# 决定账号生死的核心 cookie（与 engine.ESSENTIAL_COOKIES 保持一致）
ESSENTIAL_COOKIES = ("hatch_sess", "hatch_vml", "hatch_native_auth_device")


def _has_essential_cookies(cookies: dict) -> bool:
    return all((cookies or {}).get(name) for name in ESSENTIAL_COOKIES)


def _is_pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


# hatch_vml 固定约 2 天，且不因使用而延长 —— 它就是决定账号寿命的那条 cookie。
# 实测对照：生成前后 synced_at 变了、也捞到新 cookie，但 4 条核心 cookie 的
# expires 一动不动。所以拿不到 hatch_vml 的 expires 时，按「导入时间 + 2 天」估算，
# 而不是退回到 hatch_native_auth_device 的 30 天（那个不决定生死，会误导用户）。
VML_TTL = 2 * 86400


def min_expiry(cookies_exp: dict | None) -> int | None:
    """核心 cookie 里最早过期的那个（通用兜底，不区分哪条决定寿命）。"""
    if not cookies_exp:
        return None
    vals = []
    for name in ESSENTIAL_COOKIES:
        v = cookies_exp.get(name)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if not vals:
        for v in cookies_exp.values():
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
    return min(vals) if vals else None


def account_expiry(cookies_exp: dict | None,
                   base_ts: int | None = None) -> int | None:
    """账号实际失效时间。

    优先级：
      1) hatch_vml 的真实 expires（最准）；
      2) 拿不到就用 base_ts(导入/同步时刻) + VML_TTL 估算 —— 因为寿命就是 2 天；
      3) 都没有再退回核心 cookie 最早过期。
    """
    ce = cookies_exp or {}
    vml = ce.get("hatch_vml")
    if _is_pos(vml):
        return int(vml)
    if base_ts:
        return int(base_ts) + VML_TTL
    return min_expiry(ce)


def _read(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self._task_lock = threading.RLock()
        self._init_task_db()

    @contextmanager
    def _connect_tasks(self):
        conn = sqlite3.connect(self.cfg.tasks_db_file, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_task_db(self):
        os.makedirs(os.path.dirname(self.cfg.tasks_db_file), exist_ok=True)
        with self._task_lock, self._connect_tasks() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, kind TEXT, status TEXT,
                created_at INTEGER, updated_at INTEGER, payload TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS tasks_created_idx ON tasks(created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS tasks_kind_status_idx ON tasks(kind, status)")
            conn.execute("CREATE TABLE IF NOT EXISTS task_inputs (task_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS task_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, last_used REAL,
                use_count INTEGER, payload TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS accounts_enabled_idx ON accounts(enabled, last_used)")
            conn.execute("CREATE TABLE IF NOT EXISTS account_meta (key TEXT PRIMARY KEY, value TEXT)")
            migrated = conn.execute("SELECT 1 FROM task_meta WHERE key='json_migrated'").fetchone()
            if not migrated:
                legacy = _read(self.cfg.tasks_file, {})
                if isinstance(legacy, dict):
                    for task_id, task in legacy.items():
                        if not isinstance(task, dict):
                            continue
                        task = dict(task)
                        task.setdefault("id", task_id)
                        conn.execute("INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                                     (str(task_id), task.get("kind"), task.get("status"),
                                      int(task.get("created_at") or 0), int(task.get("updated_at") or 0),
                                      json.dumps(task, ensure_ascii=False)))
                conn.execute("INSERT INTO task_meta(key,value) VALUES('json_migrated','1')")
            migrated_accounts = conn.execute("SELECT 1 FROM account_meta WHERE key='json_migrated'").fetchone()
            if not migrated_accounts:
                legacy_accounts = _read(self.cfg.accounts_file, [])
                if isinstance(legacy_accounts, list):
                    for account in legacy_accounts:
                        if not isinstance(account, dict) or not account.get("id"):
                            continue
                        account = dict(account)
                        cookies_exp = account.get("cookies_exp") or {}
                        if _is_pos(cookies_exp.get("hatch_vml")):
                            account["expires_at"] = int(cookies_exp["hatch_vml"])
                        conn.execute("INSERT OR IGNORE INTO accounts VALUES (?, ?, ?, ?, ?)",
                                     (str(account["id"]), int(bool(account.get("enabled", True))),
                                      account.get("last_used"), int(account.get("use_count") or 0),
                                      json.dumps(account, ensure_ascii=False)))
                conn.execute("INSERT INTO account_meta(key,value) VALUES('json_migrated','1')")

    # ---------- 账号 ----------
    def list_usable_account_refs(self) -> list[dict]:
        """Return scheduling metadata only; do not pull credential payloads into RAM."""
        query = """SELECT id, last_used, use_count, json_extract(payload,'$.ok') AS ok
                   FROM accounts WHERE enabled=1
                   AND json_type(payload,'$.cookies')='object'
                   AND json_extract(payload,'$.cookies.hatch_sess') IS NOT NULL AND json_extract(payload,'$.cookies.hatch_sess')!=''
                   AND json_extract(payload,'$.cookies.hatch_vml') IS NOT NULL AND json_extract(payload,'$.cookies.hatch_vml')!=''
                   AND json_extract(payload,'$.cookies.hatch_native_auth_device') IS NOT NULL AND json_extract(payload,'$.cookies.hatch_native_auth_device')!=''"""
        with self._connect_tasks() as conn:
            return [dict(row) for row in conn.execute(query).fetchall()]

    @property
    def accounts(self) -> list[dict]:
        """Compatibility accessor; reads from SQLite and does not keep a cache."""
        with self._connect_tasks() as conn:
            rows = conn.execute("SELECT payload FROM accounts").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    @accounts.setter
    def accounts(self, values: list[dict]):
        """Compatibility setter for maintenance tools; persisted directly to SQLite."""
        with _LOCK, self._task_lock, self._connect_tasks() as conn:
            conn.execute("DELETE FROM accounts")
            for account in values or []:
                if not isinstance(account, dict) or not account.get("id"):
                    continue
                conn.execute("INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
                             (str(account["id"]), int(bool(account.get("enabled", True))),
                              account.get("last_used"), int(account.get("use_count") or 0),
                              json.dumps(account, ensure_ascii=False)))

    def add_account(self, cookies: dict, label: str = "",
                    cookies_exp: dict | None = None) -> dict:
        with _LOCK, self._task_lock, self._connect_tasks() as conn:
            aid = uuid.uuid4().hex[:12]
            exp = {k: int(v) for k, v in (cookies_exp or {}).items() if _is_pos(v)}
            now = int(time.time())
            account = {"id": aid, "label": label or aid, "cookies": cookies,
                       "cookies_exp": exp, "expires_at": account_expiry(exp, now),
                       "expiry_anchor": None, "enabled": True, "created_at": now,
                       "last_used": None, "last_keepalive": None, "use_count": 0,
                       "ok": None, "note": "", "synced_at": None}
            conn.execute("INSERT INTO accounts VALUES (?, 1, NULL, 0, ?)",
                         (aid, json.dumps(account, ensure_ascii=False)))
            return account

    def list_accounts(self) -> list[dict]:
        query = """SELECT json_remove(payload,'$.cookies') AS payload,
                   (SELECT COUNT(*) FROM json_each(accounts.payload,'$.cookies')) AS cookie_count,
                   (json_extract(payload,'$.cookies.hatch_sess') IS NOT NULL AND json_extract(payload,'$.cookies.hatch_sess')!='' AND
                    json_extract(payload,'$.cookies.hatch_vml') IS NOT NULL AND json_extract(payload,'$.cookies.hatch_vml')!='' AND
                    json_extract(payload,'$.cookies.hatch_native_auth_device') IS NOT NULL AND json_extract(payload,'$.cookies.hatch_native_auth_device')!='') AS essential_ok
                   FROM accounts"""
        with self._connect_tasks() as conn:
            rows = conn.execute(query).fetchall()
        out = []
        for row in rows:
            item = json.loads(row["payload"])
            cookies_exp = item.get("cookies_exp") or {}
            if _is_pos(cookies_exp.get("hatch_vml")):
                item["expires_at"] = int(cookies_exp["hatch_vml"])
            item["cookie_count"] = row["cookie_count"]
            item["essential_ok"] = bool(row["essential_ok"])
            item["expires_estimated"] = not _is_pos(cookies_exp.get("hatch_vml"))
            out.append(item)
        order = self._account_order()
        ranks = {aid: index for index, aid in enumerate(order)}
        out.sort(key=lambda item: (ranks.get(item["id"], len(ranks)), item["id"]))
        return out

    def _account_order(self, conn=None) -> list[str]:
        if conn is None:
            with self._connect_tasks() as db_conn:
                return self._account_order(db_conn)
        row = conn.execute("SELECT value FROM account_meta WHERE key='display_order'").fetchone()
        saved = json.loads(row["value"]) if row else []
        existing = [row["id"] for row in conn.execute("SELECT id FROM accounts ORDER BY rowid").fetchall()]
        existing_set = set(existing)
        saved_set = set()
        ordered = []
        for aid in saved:
            if aid in existing_set and aid not in saved_set:
                ordered.append(aid)
                saved_set.add(aid)
        return ordered + [aid for aid in existing if aid not in saved_set]

    def reorder_accounts(self, ids: list[str]) -> list[str]:
        with _LOCK, self._task_lock, self._connect_tasks() as conn:
            existing = [row["id"] for row in conn.execute("SELECT id FROM accounts ORDER BY rowid").fetchall()]
            requested = [aid for aid in ids if aid in set(existing)]
            order = requested + [aid for aid in existing if aid not in requested]
            conn.execute("INSERT INTO account_meta(key,value) VALUES('display_order',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (json.dumps(order),))
            return order

    def delete_account(self, aid: str) -> bool:
        with _LOCK, self._task_lock, self._connect_tasks() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
            if cur.rowcount:
                order = [item for item in self._account_order(conn) if item != aid]
                conn.execute("INSERT INTO account_meta(key,value) VALUES('display_order',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (json.dumps(order),))
            return cur.rowcount > 0

    def pick_account(self, preferred_id: str | None = None, rotate: bool = True,
                     force_rotate: bool = False, exclude_id: str | None = None,
                     exclude_ids: set[str] | list[str] | None = None,
                     record_use: bool = True) -> dict | None:
        with _LOCK, self._task_lock, self._connect_tasks() as conn:
            live = [dict(row) for row in conn.execute(
                """SELECT id, last_used, use_count, json_extract(payload,'$.ok') AS ok
                   FROM accounts WHERE enabled=1
                   AND json_type(payload,'$.cookies')='object'
                   AND EXISTS (SELECT 1 FROM json_each(accounts.payload,'$.cookies'))""").fetchall()]
            if not live:
                return None
            blocked = set(exclude_ids or [])
            if exclude_id:
                blocked.add(exclude_id)
            if blocked:
                available = [a for a in live if a["id"] not in blocked]
                if available:
                    live = available
                elif exclude_ids is not None:
                    return None
                elif len(live) > 1:
                    live = [a for a in live if a["id"] != exclude_id] or live
            chosen = None
            if preferred_id and not force_rotate:
                for ref in live:
                    if ref["id"] == preferred_id and ref.get("ok") is not False:
                        sticky_n = getattr(self, "_sticky_count", 0) if getattr(self, "_sticky_id", None) == preferred_id else 0
                        if not rotate or sticky_n < 15:
                            self._sticky_id, self._sticky_count = preferred_id, sticky_n + 1
                            chosen = ref
                            break
            if chosen is None:
                healthy = [a for a in live if a.get("ok") is not False]
                candidates = healthy if healthy else live
                order = self._account_order(conn)
                ranks = {aid: index for index, aid in enumerate(order)}
                candidates.sort(key=lambda a: (a.get("last_used") or 0.0,
                                               a.get("use_count") or 0,
                                               ranks.get(a["id"], len(ranks))))
                chosen = candidates[0]
                self._sticky_id, self._sticky_count = chosen["id"], 1
            row = conn.execute("SELECT payload FROM accounts WHERE id=?", (chosen["id"],)).fetchone()
            if not row:
                return None
            account = json.loads(row["payload"])
            if record_use:
                account["last_used"] = time.time()
                account["use_count"] = account.get("use_count", 0) + 1
                self._save_account(conn, account)
            return account

    @staticmethod
    def _save_account(conn, account: dict):
        conn.execute("UPDATE accounts SET enabled=?, last_used=?, use_count=?, payload=? WHERE id=?",
                     (int(bool(account.get("enabled", True))), account.get("last_used"),
                      int(account.get("use_count") or 0), json.dumps(account, ensure_ascii=False),
                      account["id"]))

    def touch_keepalive(self, aid: str, ok: bool | None = True, note: str = ""):
        with _LOCK, self._task_lock, self._connect_tasks() as conn:
            row = conn.execute("SELECT payload FROM accounts WHERE id=?", (aid,)).fetchone()
            if not row:
                return
            account = json.loads(row["payload"])
            now_ts = int(time.time())
            if ok is not None:
                account["ok"] = ok
            if ok is True:
                account["last_keepalive"] = now_ts
                account["expires_at"] = account_expiry(
                    account.get("cookies_exp"), account.get("expiry_anchor") or account.get("created_at"))
            if note:
                account["note"] = note[:300]
            account["checked_at"] = now_ts
            self._save_account(conn, account)

    def mark(self, aid: str, ok: bool, note: str = ""):
        with _LOCK, self._task_lock, self._connect_tasks() as conn:
            row = conn.execute("SELECT payload FROM accounts WHERE id=?", (aid,)).fetchone()
            if not row:
                return
            account = json.loads(row["payload"])
            account["ok"] = ok
            account["note"] = note[:300]
            account["checked_at"] = int(time.time())
            self._save_account(conn, account)

    def get_account(self, aid: str) -> dict | None:
        with self._connect_tasks() as conn:
            row = conn.execute("SELECT payload FROM accounts WHERE id=?", (aid,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def update_account(self, aid: str, **kw) -> dict | None:
        with _LOCK, self._task_lock, self._connect_tasks() as conn:
            row = conn.execute("SELECT payload FROM accounts WHERE id=?", (aid,)).fetchone()
            if not row:
                return None
            account = json.loads(row["payload"])
            for key, value in kw.items():
                if value is not None:
                    account[key] = value
            if "cookies_exp" in kw and kw["cookies_exp"] is not None:
                expires = account["cookies_exp"]
                if _is_pos(expires.get("hatch_vml")):
                    account["expiry_anchor"] = None
                elif not account.get("expiry_anchor"):
                    account["expiry_anchor"] = int(time.time())
                account["expires_at"] = account_expiry(
                    expires, account.get("expiry_anchor") or account.get("created_at"))
            self._save_account(conn, account)
            return account

    def stats(self) -> dict:
        now = int(time.time())
        total = self.count_accounts()
        enabled = self.count_accounts(enabled=True)
        with self._connect_tasks() as conn:
            enabled_accounts = conn.execute(
                """SELECT json_extract(payload,'$.ok') AS ok,
                          json_extract(payload,'$.expires_at') AS expires_at
                   FROM accounts WHERE enabled=1""").fetchall()
        bad = sum(1 for a in enabled_accounts if a["ok"] == 0)
        healthy = sum(1 for a in enabled_accounts if a["ok"] == 1)
        expiring = sum(1 for a in enabled_accounts
                       if a["expires_at"] and a["expires_at"] - now < 3 * 86400)
        expired = sum(1 for a in enabled_accounts
                      if a["expires_at"] and a["expires_at"] <= now)
        return {"total": total, "enabled": enabled, "disabled": total - enabled,
                "healthy": healthy, "error": bad,
                "expiring": expiring, "expired": expired}

    def count_accounts(self, enabled: bool | None = None) -> int:
        query, params = "SELECT COUNT(*) FROM accounts", []
        if enabled is not None:
            query += " WHERE enabled=?"
            params.append(int(enabled))
        with self._connect_tasks() as conn:
            return int(conn.execute(query, params).fetchone()[0])

    # ---------- 任务 ----------
    def create_task(self, kind: str, prompt: str) -> dict:
        with self._task_lock, self._connect_tasks() as conn:
            tid = "task_" + uuid.uuid4().hex[:20]
            now = int(time.time())
            task = {"id": tid, "object": "task", "kind": kind, "prompt": prompt,
                    "status": "queued", "created_at": now, "updated_at": now,
                    "result": None, "error": None}
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                         (tid, kind, task["status"], now, now,
                          json.dumps(task, ensure_ascii=False)))
            return task

    def update_task(self, tid: str, **kw):
        with self._task_lock, self._connect_tasks() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE id=?", (tid,)).fetchone()
            if not row:
                return None
            task = json.loads(row["payload"])
            task.update(kw)
            task["updated_at"] = int(time.time())
            conn.execute("UPDATE tasks SET kind=?, status=?, created_at=?, updated_at=?, payload=? WHERE id=?",
                         (task.get("kind"), task.get("status"), int(task.get("created_at") or 0),
                          task["updated_at"], json.dumps(task, ensure_ascii=False), tid))
            return task

    def get_task(self, tid: str) -> dict | None:
        with self._connect_tasks() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE id=?", (tid,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_tasks(self, limit: int | None = 50, *, kind: str | None = None,
                   statuses: tuple[str, ...] | None = None) -> list[dict]:
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if statuses is not None:
            if not statuses:
                return []
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        query = "SELECT payload FROM tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))
        with self._connect_tasks() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def find_task(self, field: str, value):
        if field not in {"image_request_key"}:
            raise ValueError("Unsupported task lookup field")
        with self._connect_tasks() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE json_extract(payload, ?) = ? ORDER BY created_at DESC LIMIT 1",
                               ("$." + field, value)).fetchone()
        return json.loads(row["payload"]) if row else None

    def count_tasks(self, *, kind: str | None = None,
                    statuses: tuple[str, ...] | None = None) -> int:
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if statuses is not None:
            if not statuses:
                return 0
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        query = "SELECT COUNT(*) FROM tasks" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        with self._connect_tasks() as conn:
            return int(conn.execute(query, params).fetchone()[0])

    def set_task_input(self, tid: str, **values) -> None:
        with self._task_lock, self._connect_tasks() as conn:
            conn.execute("INSERT INTO task_inputs(task_id,payload) VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload",
                         (tid, json.dumps(values, ensure_ascii=False)))

    def get_task_input(self, tid: str) -> dict:
        with self._connect_tasks() as conn:
            row = conn.execute("SELECT payload FROM task_inputs WHERE task_id=?", (tid,)).fetchone()
        return json.loads(row["payload"]) if row else {}

    def delete_task_input(self, tid: str | None = None) -> int:
        with self._task_lock, self._connect_tasks() as conn:
            if tid is None:
                cur = conn.execute("DELETE FROM task_inputs")
            else:
                cur = conn.execute("DELETE FROM task_inputs WHERE task_id=?", (tid,))
            return max(0, cur.rowcount)

    def prune_task_inputs(self) -> int:
        with self._task_lock, self._connect_tasks() as conn:
            cur = conn.execute("DELETE FROM task_inputs WHERE task_id NOT IN (SELECT id FROM tasks WHERE status IN ('queued','processing'))")
            return max(0, cur.rowcount)

    def delete_task(self, tid: str) -> bool:
        with self._task_lock, self._connect_tasks() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
            conn.execute("DELETE FROM task_inputs WHERE task_id=?", (tid,))
            return cur.rowcount > 0

    def clear_tasks(self, keep: int = 0) -> int:
        keep = max(0, int(keep))
        with self._task_lock, self._connect_tasks() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id NOT IN (SELECT id FROM tasks ORDER BY created_at DESC LIMIT ?)",
                               (keep,))
            conn.execute("DELETE FROM task_inputs WHERE task_id NOT IN (SELECT id FROM tasks)")
            return max(0, cur.rowcount)
