import psycopg2

DATABASE_URL = "postgresql://neondb_owner:npg_oGwF6fyaIU2n@ep-silent-band-aoss09t1-pooler.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

conn = psycopg2.connect(DATABASE_URL)
with conn:
    with conn.cursor() as cur:
        cur.execute("SELECT _id FROM json_store ORDER BY _id")
        rows = cur.fetchall()
        print("=== Neon json_store 里的 key ===")
        for r in rows:
            print(" -", r[0])
        if not rows:
            print("(空的！什么都没有)")
        # 查 users 里有多少人
        cur.execute("SELECT data->'users' FROM json_store WHERE _id = 'users'")
        row = cur.fetchone()
        if row and row[0]:
            users = row[0].get("users", {})
            print(f"\n=== users 里有 {len(users)} 个用户 ===")
            for uid, u in users.items():
                print(f"  {uid}: {u.get('nickname','?')}  phone={u.get('phone','?')}")
        else:
            print("\n(users 表是空的)")
