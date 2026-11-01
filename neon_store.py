"""
neon_store.py — 云端 JSON 存储桥接（Neon Postgres）

把原本存在本机 JSON 文件里的数据，改为可存到 Neon（Postgres jsonb）：
- 若配置了环境变量 DATABASE_URL / NEON_DATABASE_URL（Neon 连接串），读写走 Neon；
- 否则回退到本地 JSON 文件（本地开发/测试完全不受影响）。

一张表 json_store(_id text PRIMARY KEY, data jsonb)，_id 即文件名（users/intents/invitations/messages/app_extra）。
"""

import json
import os

try:
    import psycopg2
    from psycopg2.extras import Json as PgJson
except Exception:  # 未安装 psycopg2 时回退本地，不阻塞启动
    psycopg2 = None
    PgJson = None

_DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
_conn = None


def is_configured() -> bool:
    """是否启用了云端存储（有连接串且 psycopg2 可用）。"""
    return bool(_DATABASE_URL) and psycopg2 is not None


def _get_conn():
    global _conn
    if _conn is None or getattr(_conn, "closed", True):
        _conn = psycopg2.connect(_DATABASE_URL)
    return _conn


def _ensure_table() -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS json_store (_id text PRIMARY KEY, data jsonb)"
            )
        conn.commit()


def load_json(key: str, default):
    """从 Neon 读一份 JSON；未启用/读取失败/无此 key 时返回 default。"""
    if not is_configured():
        return default
    try:
        _ensure_table()
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM json_store WHERE _id = %s", (key,))
                row = cur.fetchone()
        if row is None or row[0] is None:
            return default
        return row[0]
    except Exception:
        return default


def save_json(key: str, data) -> bool:
    """把一份 JSON 写入 Neon（存在则覆盖）。成功返回 True；失败返回 False（调用方回退本地）。"""
    if not is_configured():
        return False
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
        return True
    except Exception:
        return False


def file_key(path: str) -> str:
    """把本地 JSON 路径转成存储 key（去目录、去 .json 后缀）。"""
    base = os.path.basename(path)
    if base.endswith(".json"):
        base = base[: -len(".json")]
    return base or "data"
