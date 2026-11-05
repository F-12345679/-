"""
先读出 Neon 里现有的 users，再把10个测试用户合并进去（不覆盖）。
"""
import psycopg2
from psycopg2.extras import Json as PgJson

DATABASE_URL = "postgresql://neondb_owner:npg_oGwF6fyaIU2n@ep-silent-band-aoss09t1-pooler.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

test_users = {
    "U001": {"user_id": "U001", "nickname": "小林", "contact": "微信 xiaolin_ai", "contact_type": "微信", "school": "湖南第一师范学院", "major": "数据科学与大数据技术", "free_time": ["随时"], "gender_preference": "不限", "interests": ["人工智能", "数据分析"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000001"},
    "U002": {"user_id": "U002", "nickname": "阿May", "contact": "QQ 123456789", "contact_type": "QQ", "school": "湖南第一师范学院", "major": "汉语言文学", "free_time": ["周末", "周五晚上"], "gender_preference": "不限", "interests": ["写作", "演讲", "产品设计"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000002"},
    "U003": {"user_id": "U003", "nickname": "老周", "contact": "邮箱 zhou@test.com", "contact_type": "邮箱", "school": "中南大学", "major": "计算机科学与技术", "free_time": ["工作日晚上"], "gender_preference": "男", "interests": ["Java", "后端", "数据库"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000003"},
    "U004": {"user_id": "U004", "nickname": "小鹿", "contact": "微信 xiaolu_design", "contact_type": "微信", "school": "湖南大学", "major": "视觉传达设计", "free_time": ["随时"], "gender_preference": "不限", "interests": ["UI设计", "插画", "海报"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000004"},
    "U005": {"user_id": "U005", "nickname": "阿杰", "contact": "QQ 987654321", "contact_type": "QQ", "school": "湖南师范大学", "major": "软件工程", "free_time": ["周末全天"], "gender_preference": "不限", "interests": ["前端", "React", "小程序"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000005"},
    "U006": {"user_id": "U006", "nickname": "小陈", "contact": "微信 chen_data", "contact_type": "微信", "school": "湖南第一师范学院", "major": "数学与应用数学", "free_time": ["周二周四下午"], "gender_preference": "女", "interests": ["数学建模", "统计", "Python"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000006"},
    "U007": {"user_id": "U007", "nickname": "Momo", "contact": "邮箱 momo@test.com", "contact_type": "邮箱", "school": "湘潭大学", "major": "市场营销", "free_time": ["随时"], "gender_preference": "不限", "interests": ["新媒体运营", "文案", "短视频"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000007"},
    "U008": {"user_id": "U008", "nickname": "大伟", "contact": "微信 dawei_robot", "contact_type": "微信", "school": "长沙理工大学", "major": "自动化", "free_time": ["周末"], "gender_preference": "男", "interests": ["机器人", "嵌入式", "C++"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000008"},
    "U009": {"user_id": "U009", "nickname": "甜甜", "contact": "QQ 555666777", "contact_type": "QQ", "school": "湖南第一师范学院", "major": "英语", "free_time": ["周一三五下午"], "gender_preference": "女", "interests": ["翻译", "口译", "国际交流"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000009"},
    "U010": {"user_id": "U010", "nickname": "阿泽", "contact": "微信 aze_ai", "contact_type": "微信", "school": "中南大学", "major": "人工智能", "free_time": ["工作日晚上", "周末"], "gender_preference": "不限", "interests": ["深度学习", "计算机视觉", "PyTorch"], "is_teamed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "phone": "15500000010"},
}

test_intents = {
    "I001": {"intent_id": "I001", "user_id": "U001", "competition": "大创赛", "is_innovation": True, "skills": ["Python", "算法", "数据清洗"], "wanted_skills": ["文档写作", "PPT", "产品设计"], "team_size": 2, "available_period": {"raw": "这个学期", "start_date": "2026-11-05", "end_date": "2027-02-28"}, "preferred_role": "算法与数据分析", "has_idea": True, "willing_offline": True, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
    "I002": {"intent_id": "I002", "user_id": "U002", "competition": "大创赛", "is_innovation": True, "skills": ["文档写作", "PPT", "项目管理"], "wanted_skills": ["Python", "算法", "数据分析"], "team_size": 1, "available_period": {"raw": "这个学期", "start_date": "2026-11-05", "end_date": "2027-02-28"}, "preferred_role": "文档与汇报", "has_idea": False, "willing_offline": True, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
    "I003": {"intent_id": "I003", "user_id": "U003", "competition": "挑战杯", "is_innovation": True, "skills": ["Java", "Spring Boot", "数据库"], "wanted_skills": ["前端", "UI设计"], "team_size": 2, "available_period": {"raw": "这个学期", "start_date": "2026-11-05", "end_date": "2027-03-31"}, "preferred_role": "后端开发", "has_idea": True, "willing_offline": True, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
    "I004": {"intent_id": "I004", "user_id": "U004", "competition": "挑战杯", "is_innovation": True, "skills": ["UI设计", "海报", "插画"], "wanted_skills": ["Java", "后端", "产品"], "team_size": 1, "available_period": {"raw": "这个学期", "start_date": "2026-11-05", "end_date": "2027-03-31"}, "preferred_role": "视觉设计", "has_idea": False, "willing_offline": True, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
    "I005": {"intent_id": "I005", "user_id": "U005", "competition": "互联网+", "is_innovation": True, "skills": ["前端", "React", "小程序"], "wanted_skills": ["后端", "运营", "文案"], "team_size": 2, "available_period": {"raw": "这个学期", "start_date": "2026-11-05", "end_date": "2027-04-30"}, "preferred_role": "前端开发", "has_idea": True, "willing_offline": True, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
    "I006": {"intent_id": "I006", "user_id": "U006", "competition": "数学建模", "is_innovation": False, "skills": ["Python", "统计", "MATLAB"], "wanted_skills": ["写作", "编程"], "team_size": 2, "available_period": {"raw": "国赛期间", "start_date": "2026-11-05", "end_date": "2026-12-15"}, "preferred_role": "建模与算法", "has_idea": False, "willing_offline": True, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
    "I007": {"intent_id": "I007", "user_id": "U007", "competition": "互联网+", "is_innovation": True, "skills": ["新媒体运营", "文案", "短视频"], "wanted_skills": ["前端", "后端", "技术开发"], "team_size": 1, "available_period": {"raw": "这个学期", "start_date": "2026-11-05", "end_date": "2027-04-30"}, "preferred_role": "运营与推广", "has_idea": False, "willing_offline": False, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
    "I008": {"intent_id": "I008", "user_id": "U008", "competition": "RoboMaster", "is_innovation": False, "skills": ["C++", "嵌入式", "硬件"], "wanted_skills": ["机械", "视觉", "Python"], "team_size": 3, "available_period": {"raw": "全年备赛", "start_date": "2026-11-05", "end_date": "2027-08-31"}, "preferred_role": "嵌入式开发", "has_idea": True, "willing_offline": True, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
    "I009": {"intent_id": "I009", "user_id": "U009", "competition": "英语演讲比赛", "is_innovation": False, "skills": ["英语", "口译", "演讲"], "wanted_skills": ["主持", "多媒体"], "team_size": 1, "available_period": {"raw": "11月", "start_date": "2026-11-05", "end_date": "2026-11-30"}, "preferred_role": "英语演讲", "has_idea": False, "willing_offline": True, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
    "I010": {"intent_id": "I010", "user_id": "U010", "competition": "大创赛", "is_innovation": True, "skills": ["深度学习", "PyTorch", "计算机视觉"], "wanted_skills": ["前端", "产品", "PPT"], "team_size": 2, "available_period": {"raw": "这个学期", "start_date": "2026-11-05", "end_date": "2027-02-28"}, "preferred_role": "AI算法", "has_idea": True, "willing_offline": True, "competition_range": None, "status": "matching", "team_confirmed": False, "created_at": "2026-11-05 10:00:00", "updated_at": "2026-11-05 10:00:00", "last_reminded_at": None, "last_reminded_response_at": None, "excluded_reason": None},
}

conn = psycopg2.connect(DATABASE_URL)
with conn:
    with conn.cursor() as cur:
        # 读出现有 users
        cur.execute("SELECT data FROM json_store WHERE _id = 'users'")
        row = cur.fetchone()
        users_data = row[0] if row else {"next_seq": 1, "users": {}}
        existing_users = users_data.get("users", {})
        print(f"现有 users: {len(existing_users)} 个")
        for uid, u in existing_users.items():
            print(f"  - {uid}: {u.get('nickname','?')}")

        # 合并测试用户（不覆盖已有的）
        for uid, u in test_users.items():
            if uid not in existing_users:
                existing_users[uid] = u
        users_data["users"] = existing_users
        users_data["next_seq"] = max(users_data.get("next_seq", 1), 11)

        cur.execute(
            "INSERT INTO json_store (_id, data) VALUES (%s, %s) "
            "ON CONFLICT (_id) DO UPDATE SET data = EXCLUDED.data",
            ("users", PgJson(users_data)),
        )

        # 读出现有 intents
        cur.execute("SELECT data FROM json_store WHERE _id = 'intents'")
        row = cur.fetchone()
        intents_data = row[0] if row else {"next_seq": 1, "intents": {}}
        existing_intents = intents_data.get("intents", {})
        print(f"现有 intents: {len(existing_intents)} 条")

        for iid, it in test_intents.items():
            if iid not in existing_intents:
                existing_intents[iid] = it
        intents_data["intents"] = existing_intents
        intents_data["next_seq"] = max(intents_data.get("next_seq", 1), 11)

        cur.execute(
            "INSERT INTO json_store (_id, data) VALUES (%s, %s) "
            "ON CONFLICT (_id) DO UPDATE SET data = EXCLUDED.data",
            ("intents", PgJson(intents_data)),
        )

print(f"\n完成！现在 users 共 {len(existing_users)} 个，intents 共 {len(existing_intents)} 条")
