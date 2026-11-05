"""
修复：把超管"吃货"加回 Neon users 表。
用法：py restore_admin.py
"""
import psycopg2
from psycopg2.extras import Json as PgJson

DATABASE_URL = "postgresql://neondb_owner:npg_oGwF6fyaIU2n@ep-silent-band-aoss09t1-pooler.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

admin_user = {
    "user_id": "U000",
    "nickname": "吃货",
    "contact": "",
    "contact_type": "",
    "school": "",
    "major": "",
    "free_time": ["随时"],
    "gender_preference": "不限",
    "interests": [],
    "is_teamed": False,
    "created_at": "2026-11-05 10:00:00",
    "updated_at": "2026-11-05 10:00:00",
    "phone": "13627350695",
    "avatar": "",
}

conn = psycopg2.connect(DATABASE_URL)
with conn:
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM json_store WHERE _id = 'users'")
        row = cur.fetchone()
        data = row[0] if row else {"next_seq": 11, "users": {}}
        # 把超管加回去
        data["users"]["U000"] = admin_user
        # 同时把 admins 表也加回去（如果有）
        cur.execute(
            "INSERT INTO json_store (_id, data) VALUES (%s, %s) "
            "ON CONFLICT (_id) DO UPDATE SET data = EXCLUDED.data",
            ("users", PgJson(data)),
        )
        # admins 表
        cur.execute("SELECT data FROM json_store WHERE _id = 'admins'")
        admin_row = cur.fetchone()
        admin_data = admin_row[0] if admin_row else {"admins": ["13627350695"]}
        if "13627350695" not in admin_data.get("admins", []):
            admin_data.setdefault("admins", []).append("13627350695")
        cur.execute(
            "INSERT INTO json_store (_id, data) VALUES (%s, %s) "
            "ON CONFLICT (_id) DO UPDATE SET data = EXCLUDED.data",
            ("admins", PgJson(admin_data)),
        )
print("Done: 超管吃货已加回。")
