"""
neon_store.py — 云端 JSON 存储桥接（Neon Postgres）

把原本存在本机 JSON 文件里的数据，改为可存到 Neon（Postgres jsonb）：
- 若配置了环境变量 DATABASE_URL / NEON_DATABASE_URL（Neon 连接串），读写走 Neon；
- 否则回退到本地 JSON 文件（本地开发/测试完全不受影响）。

一张表 json_store(_id text PRIMARY KEY, data jsonb)，_id 即文件名（users/intents/invitations/messages/app_extra）。
"""

import copy
import json
import os
import sys
import time

try:
    import psycopg2
    from psycopg2.extras import Json as PgJson
except Exception:  # 未安装 psycopg2 时回退本地，不阻塞启动
    psycopg2 = None
    PgJson = None


def _load_env_file() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path)
        return
    except Exception:
        pass
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_env_file()

try:
    import streamlit as _st  # type: ignore

    for _key in ("DATABASE_URL", "NEON_DATABASE_URL"):
        if not os.environ.get(_key):
            try:
                _value = _st.secrets[_key]
            except Exception:
                _value = ""
            if _value:
                os.environ[_key] = str(_value)
except Exception:
    pass

_DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
_conn = None
_CACHE: dict = {}
_CACHE_TTL = 30.0


def is_configured() -> bool:
    """是否启用了云端存储（有连接串且 psycopg2 可用）。"""
    return bool(_DATABASE_URL) and psycopg2 is not None


def _get_conn():
    global _conn
    if _conn is None or getattr(_conn, "closed", True):
        _conn = psycopg2.connect(_DATABASE_URL)
    return _conn


def _reset_conn() -> None:
    global _conn
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None


def _ensure_table() -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS json_store (_id text PRIMARY KEY, data jsonb)"
            )
        conn.commit()


def invalidate_cache(key: str = "") -> None:
    if key:
        _CACHE.pop(key, None)
    else:
        _CACHE.clear()


def _is_empty_payload(data) -> bool:
    if data is None:
        return True
    if not isinstance(data, dict):
        return False
    for key, value in data.items():
        if key == "next_seq":
            continue
        if value:
            return False
    return True


def _log_users(data, source: str) -> None:
    users = (data or {}).get("users", {}) if isinstance(data, dict) else {}
    avatars = sum(1 for user in users.values() if isinstance(user, dict) and user.get("avatar"))
    print(
        "[neon_store] %s users=%d avatars=%d" % (source, len(users), avatars),
        file=sys.stderr,
    )


def load_json_checked(key: str, default):
    """读取 JSON，返回 (是否读取成功, 数据)。30 秒内复用缓存，空数据不缓存。"""
    if not is_configured():
        return True, default
    now = time.time()
    cached = _CACHE.get(key)
    if cached is not None and now - cached[0] <= _CACHE_TTL:
        if key == "users":
            _log_users(cached[1], "缓存命中")
        return True, copy.deepcopy(cached[1])
    last_error = None
    for attempt in range(3):
        try:
            _ensure_table()
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT data FROM json_store WHERE _id = %s", (key,))
                    row = cur.fetchone()
            if row is None or row[0] is None:
                if key == "users":
                    _log_users(default, "读到空数据")
                if cached is not None:
                    return True, copy.deepcopy(cached[1])
                return True, copy.deepcopy(default)
            data = row[0]
            if _is_empty_payload(data):
                if key == "users":
                    _log_users(data, "读到空数据")
                if cached is not None:
                    return True, copy.deepcopy(cached[1])
                return True, copy.deepcopy(data)
            _CACHE[key] = (now, copy.deepcopy(data))
            if key == "users":
                _log_users(data, "Neon读取")
            return True, copy.deepcopy(data)
        except Exception as exc:
            last_error = exc
            _reset_conn()
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
                continue
    if cached is not None:
        print(
            "[neon_store] 读取失败 %s，使用上次缓存: %s" % (key, last_error),
            file=sys.stderr,
        )
        if key == "users":
            _log_users(cached[1], "失败回退缓存")
        return True, copy.deepcopy(cached[1])
    print("[neon_store] 读取失败 %s: %s" % (key, last_error), file=sys.stderr)
    return False, default


def load_json(key: str, default):
    """从 Neon 读一份 JSON；未启用/读取失败/无此 key 时返回 default。"""
    _ok, data = load_json_checked(key, default)
    return data


def save_json(key: str, data) -> bool:
    """把一份 JSON 写入 Neon（存在则覆盖）。成功返回 True；失败返回 False。"""
    if not is_configured():
        return False
    for attempt in range(3):
        try:
            _ensure_table()
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO json_store (_id, data) VALUES (%s, %s) "
                        "ON CONFLICT (_id) DO UPDATE SET data = EXCLUDED.data",
                        (key, PgJson(data)),
                    )
                conn.commit()
            _CACHE.pop(key, None)
            return True
        except Exception as exc:
            _reset_conn()
            print(
                "[neon_store] 写入失败 %s（第 %d 次）: %s" % (key, attempt + 1, exc),
                file=sys.stderr,
            )
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
    return False


def file_key(path: str) -> str:
    """把本地 JSON 路径转成存储 key（去目录、去 .json 后缀）。"""
    base = os.path.basename(path)
    if base.endswith(".json"):
        base = base[: -len(".json")]
    return base or "data"
