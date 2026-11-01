# -*- coding: utf-8 -*-
"""
test_case.py — 核心模块测试（不依赖大模型、不依赖 streamlit）

覆盖：
  1. Jaccard / 重复技能去重
  2. 必填字段缺失判断
  3. 比赛人数区间查表 + 未知比赛默认值
  4. 创新类比赛本地判断
  5. 时间段交集
  6. 正常匹配（推荐 + 备选）
  7. 人数不足 / 超上限
  8. 无候选人
  9. 匹配不到人后资料仍入库
 10. 组队成功后从候选池移除
 11. 过期清理
 12. 组队提醒（按间隔）+ 已组队 / 未组队回应
 13. 倒排索引检索
 14. 邀请状态机 + 站内留言 + 双方组队确认

运行：
    python test_case.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Tuple

import matcher
from data_pool import DataPool, ISO_FMT

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TABLE = matcher.load_competition_table(os.path.join(BASE_DIR, "competition_table.json"))

PASS_COUNT = 0
FAIL_COUNT = 0
FAIL_DETAILS: List[str] = []


def check(name: str, condition: Any, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print("[PASS] %s" % name)
    else:
        FAIL_COUNT += 1
        FAIL_DETAILS.append("%s %s" % (name, detail))
        print("[FAIL] %s %s" % (name, detail))


def _future(days: int = 90) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def _past(days: int = 1) -> str:
    return (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def make_user(pool: DataPool, nickname: str, contact: str, school: str = "湖南第一师范学院",
              major: str = "数据科学", **kwargs: Any) -> str:
    data = {
        "nickname": nickname,
        "contact": contact,
        "school": school,
        "major": major,
    }
    data.update(kwargs)
    return pool.upsert_user(data)


def make_intent(pool: DataPool, user_id: str, competition: str = "大创赛",
                skills: Any = None, wanted: Any = None, team_size: int = 2,
                start: str = None, end: str = None, raw: str = "这个学期",
                **kwargs: Any) -> str:
    data = {
        "competition": competition,
        "is_innovation": True,
        "skills": skills if skills is not None else ["Python", "算法"],
        "wanted_skills": wanted if wanted is not None else ["文档写作", "PPT"],
        "team_size": team_size,
        "available_period": {
            "raw": raw,
            "start_date": start or _today(),
            "end_date": end or _future(),
        },
    }
    data.update(kwargs)
    return pool.create_intent(user_id, data)


def candidate_intents(pool: DataPool) -> List[Dict[str, Any]]:
    return pool.list_matching_intents()


def full_fields() -> Dict[str, Any]:
    return {
        "competition": "大创赛",
        "nickname": "小林",
        "contact": "微信：xiaolin",
        "major": "数据科学",
        "skills": ["Python", "算法"],
        "wanted_skills": ["文档写作"],
        "team_size": 2,
        "school": "湖南第一师范学院",
        "available_period": {"raw": "这个学期", "start_date": _today(), "end_date": _future()},
    }


# ----------------------------------------------------------------------
def test_basic_algorithms(tmp: str) -> None:
    print("\n=== 基础算法 ===")
    check("Jaccard 1/3", abs(matcher.jaccard(["Python", "C"], ["Python", "Java"]) - 1 / 3) < 1e-9)
    check("Jaccard 去重", abs(matcher.jaccard(["Python", "python", "C"], ["python"]) - 0.5) < 1e-9)
    check("Jaccard 空集", matcher.jaccard([], []) == 0.0)

    check("联系方式识别-微信", matcher.detect_contact_type("微信：abc") == "微信")
    check("联系方式识别-QQ", matcher.detect_contact_type("QQ 123456") == "QQ")
    check("联系方式识别-邮箱", matcher.detect_contact_type("a@b.com") == "邮箱")

    fields = full_fields()
    check("必填齐全", matcher.missing_required(fields) == [])
    missing = dict(matcher.missing_required({k: v for k, v in fields.items() if k != "contact"}))
    check("缺联系方式被识别", "contact" in missing, str(missing))
    missing = dict(matcher.missing_required({**fields, "skills": []}))
    check("空技能列表被识别", "skills" in missing, str(missing))

    info = matcher.get_competition_range("大创赛", TABLE)
    check("已知比赛区间查表", info["source"] == "table" and info["min"] == 3 and info["max"] == 5, str(info))
    info2 = matcher.get_competition_range("不存在的比赛", TABLE)
    check("未知比赛用默认区间", info2["source"] == "default" and info2["min"] == 3, str(info2))
    info3 = matcher.get_competition_range("不存在的比赛", TABLE, {"min": 4, "max": 6})
    check("用户确认区间优先", info3["source"] == "confirmed" and info3["min"] == 4, str(info3))

    check("创新类-大创", matcher.judge_innovation_local("大创赛", TABLE) == "yes")
    check("创新类-篮球", matcher.judge_innovation_local("校园篮球赛", TABLE) == "no")
    check("创新类-未知", matcher.judge_innovation_local("某某杯", TABLE) == "unknown")

    p1 = {"start_date": "2026-01-01", "end_date": "2026-06-30", "raw": ""}
    p2 = {"start_date": "2026-05-01", "end_date": "2026-08-01", "raw": ""}
    p3 = {"start_date": "2026-07-01", "end_date": "2026-09-01", "raw": ""}
    check("时间段有交集", matcher.periods_overlap(p1, p2) is True)
    check("时间段无交集", matcher.periods_overlap(p1, p3) is False)

    check("同校得分", matcher.school_score("湖南第一师范学院", "湖南第一师范学院") == 1.0)
    check("同省得分", matcher.school_score("湖南第一师范学院", "中南大学") == 0.6)
    check("异地得分", matcher.school_score("湖南第一师范学院", "北京大学") == 0.0)


# ----------------------------------------------------------------------
def test_normal_match(tmp: str) -> None:
    print("\n=== 正常匹配 / 推荐 + 备选 ===")
    pool = DataPool(tmp)

    host_uid = make_user(
        pool, "小林", "微信：xiaolin", school="湖南第一师范学院", major="数据科学",
        free_time=["周三晚上", "周末"], interests=["人工智能", "数据分析"],
        gender_preference="不限",
    )
    host_iid = make_intent(
        pool, host_uid, skills=["Python", "算法"], wanted=["文档写作", "Python"],
        team_size=2, preferred_role="算法", has_idea=True, willing_offline=True,
    )

    cand_uid = make_user(
        pool, "阿May", "QQ：123456789", school="湖南第一师范学院", major="汉语言文学",
        free_time=["周末", "周五晚上"], interests=["写作", "产品设计"],
        gender_preference="不限",
    )
    cand_iid = make_intent(
        pool, cand_uid, skills=["Python", "文档写作", "C语言"], wanted=["算法"],
        team_size=1, preferred_role="文档与汇报", has_idea=False, willing_offline=True,
    )

    other_uid = make_user(
        pool, "老周", "邮箱：zhou@example.com", school="北京大学", major="软件工程",
    )
    other_iid = make_intent(
        pool, other_uid, skills=["Java", "Spring"], wanted=["前端"], team_size=1,
    )

    result = matcher.find_candidates(
        pool.get_intent(host_iid), pool.get_user(host_uid),
        candidate_intents(pool), pool.users, TABLE,
        inverted_index=pool.build_inverted_index(),
    )
    rec_ids = [x["intent_id"] for x in result["recommended"]]
    alt_ids = [x["intent_id"] for x in result["alternatives"]]

    check("推荐列表包含技能匹配候选人", cand_iid in rec_ids, str(rec_ids))
    check("备选列表包含低分候选人", other_iid in alt_ids, str(alt_ids))
    check("推荐分 > 0", all(x["score"] > 0 for x in result["recommended"]))
    check("备选分 = 0", all(x["score"] == 0 for x in result["alternatives"]))
    check("分数不超过 100", all(0 <= x["score"] <= 100 for x in result["recommended"]))
    check("还差人数计算正确", result["needed"] == 1, str(result["needed"]))
    check("候选人卡片带联系方式", all(x["contact"] for x in result["recommended"]))
    check("倒排索引命中技能候选人", any(x["in_index"] for x in result["recommended"]))
    check("推荐顺序降序", rec_ids == [x["intent_id"] for x in sorted(
        result["recommended"], key=lambda y: -y["score"])])

    # 检查分数明细里未填维度不计入
    cand_item = [x for x in result["recommended"] if x["intent_id"] == cand_iid][0]
    applied = {k: v["applied"] for k, v in cand_item["breakdown"].items()}
    check("全维度已计入", all(applied.values()), str(applied))


def test_team_size_constraints(tmp: str) -> None:
    print("\n=== 人数硬约束 ===")
    pool = DataPool(tmp)
    host_uid = make_user(pool, "主机", "微信：host")
    host_iid = make_intent(pool, host_uid, team_size=1, skills=["Python"], wanted=["Python"])
    cand_uid = make_user(pool, "候选人", "微信：cand")
    cand_iid = make_intent(pool, cand_uid, team_size=1, skills=["Python"], wanted=["Python"])
    result = matcher.find_candidates(
        pool.get_intent(host_iid), pool.get_user(host_uid),
        [pool.get_intent(cand_iid)], pool.users, TABLE,
    )
    check("人数不足被淘汰", result["hard_pass"] == 0, str(result))

    pool2 = DataPool(os.path.join(tmp, "pool2"))
    host_uid2 = make_user(pool2, "主机2", "微信：host2")
    host_iid2 = make_intent(pool2, host_uid2, team_size=4, skills=["Python"], wanted=["Python"])
    cand_uid2 = make_user(pool2, "候选人2", "微信：cand2")
    cand_iid2 = make_intent(pool2, cand_uid2, team_size=4, skills=["Python"], wanted=["Python"])
    result2 = matcher.find_candidates(
        pool2.get_intent(host_iid2), pool2.get_user(host_uid2),
        [pool2.get_intent(cand_iid2)], pool2.users, TABLE,
    )
    check("人数超上限被淘汰", result2["hard_pass"] == 0, str(result2))


def test_no_candidates_but_stored(tmp: str) -> None:
    print("\n=== 无候选人仍入库 ===")
    pool = DataPool(tmp)
    uid = make_user(pool, "孤独选手", "微信：lonely")
    iid = make_intent(pool, uid, team_size=2)
    result = matcher.find_candidates(
        pool.get_intent(iid), pool.get_user(uid), [], pool.users, TABLE,
    )
    check("无候选人时推荐为空", result["recommended"] == [] and result["alternatives"] == [])
    check("无候选人时资料仍在意向表", pool.get_intent(iid) is not None)
    check("无候选人时状态仍为匹配中", pool.get_intent(iid)["status"] == "matching")
    check("无候选人时用户表仍有该用户", pool.get_user(uid) is not None)


def test_duplicate_skills(tmp: str) -> None:
    print("\n=== 重复技能 ===")
    pool = DataPool(tmp)
    uid = make_user(pool, "重复技能", "微信：dup")
    iid = make_intent(pool, uid, skills=["Python", "python", "PYTHON", "C"],
                      wanted=["文档", "文档", "PPT"])
    intent = pool.get_intent(iid)
    check("技能去重", len(intent["skills"]) == 2, str(intent["skills"]))
    check("期望技能去重", len(intent["wanted_skills"]) == 2, str(intent["wanted_skills"]))


def test_teamed_removed(tmp: str) -> None:
    print("\n=== 组队成功从候选池移除 ===")
    pool = DataPool(tmp)
    host_uid = make_user(pool, "甲", "微信：jia")
    host_iid = make_intent(pool, host_uid, team_size=2)
    cand_uid = make_user(pool, "乙", "微信：yi")
    cand_iid = make_intent(pool, cand_uid, team_size=1)

    inv = pool.create_invitation(host_iid, cand_iid)
    pool.respond_invitation(inv["invitation_id"], True, cand_uid)
    first = pool.confirm_team(inv["invitation_id"], host_uid)
    check("单方确认不算组队", first["both_confirmed"] is False)
    second = pool.confirm_team(inv["invitation_id"], cand_uid)
    check("双方确认组队成功", second["both_confirmed"] is True)
    check("甲方意向已标记组队", pool.get_intent(host_iid)["status"] == "teamed")
    check("乙方意向已标记组队", pool.get_intent(cand_iid)["status"] == "teamed")
    check("组队后不在匹配池", cand_iid not in [x["intent_id"] for x in pool.list_matching_intents()])

    result = matcher.find_candidates(
        pool.get_intent(host_iid), pool.get_user(host_uid),
        pool.list_matching_intents(), pool.users, TABLE,
    )
    check("组队后不再出现在候选", result["recommended"] == [] and result["alternatives"] == [])


def test_expiry(tmp: str) -> None:
    print("\n=== 过期清理 ===")
    pool = DataPool(tmp)
    host_uid = make_user(pool, "甲", "微信：exp-host")
    host_iid = make_intent(pool, host_uid, team_size=2, end=_future())
    cand_uid = make_user(pool, "乙", "微信：exp-cand")
    cand_iid = make_intent(pool, cand_uid, team_size=1, end=_past())

    expired = pool.expire_intents()
    check("过期意向被清理", cand_iid in expired, str(expired))
    check("过期状态为 expired", pool.get_intent(cand_iid)["status"] == "expired")
    check("过期意向不在匹配池", cand_iid not in [x["intent_id"] for x in pool.list_matching_intents()])
    check("过期资料仍保留", pool.get_intent(cand_iid) is not None)

    result = matcher.find_candidates(
        pool.get_intent(host_iid), pool.get_user(host_uid),
        pool.list_matching_intents(), pool.users, TABLE,
    )
    check("过期后不参与匹配", result["recommended"] == [] and result["alternatives"] == [])


def test_reminders(tmp: str) -> None:
    print("\n=== 组队提醒 ===")
    pool = DataPool(tmp)
    uid = make_user(pool, "提醒用户", "微信：remind")
    iid = make_intent(pool, uid, team_size=2)

    first = pool.generate_team_reminders(24)
    check("首次生成提醒", any(x["intent_id"] == iid for x in first), str(first))
    check("提醒待回应", pool.needs_team_response(pool.get_intent(iid)))
    second = pool.generate_team_reminders(24)
    check("24小时内不重复提醒", all(x["intent_id"] != iid for x in second), str(second))

    later = datetime.now() + timedelta(days=2)
    third = pool.generate_team_reminders(24, now=later)
    check("超过间隔再次提醒", any(x["intent_id"] == iid for x in third), str(third))

    status = pool.respond_team_reminder(iid, False, now=later + timedelta(hours=1))
    check("回应未组队仍在池中", status == "matching")
    check("回应后不再提示", pool.needs_team_response(pool.get_intent(iid)) is False)

    status2 = pool.respond_team_reminder(iid, True, now=later + timedelta(hours=2))
    check("回应已组队被移除", status2 == "teamed")
    check("已组队用户标记", pool.get_user(uid)["is_teamed"] is True)


def test_inverted_index(tmp: str) -> None:
    print("\n=== 倒排索引 ===")
    pool = DataPool(tmp)
    uid = make_user(pool, "索引用户", "微信：index")
    iid = make_intent(pool, uid, skills=["Python", "C++"], wanted=["文档"])
    index = pool.build_inverted_index()
    check("技能索引存在", "python" in index, str(index.keys()))
    check("技能映射到意向", iid in index.get("python", set()))
    hits = pool.search_intents_by_skills(["Python"])
    check("技能检索命中", iid in hits, str(hits))
    check("未命中技能返回空", pool.search_intents_by_skills(["Rust"]) == set())


def test_invitation_and_chat(tmp: str) -> None:
    print("\n=== 邀请状态机 / 留言 / 双方组队 ===")
    pool = DataPool(tmp)
    host_uid = make_user(pool, "甲", "微信：inv-a")
    host_iid = make_intent(pool, host_uid, team_size=2)
    cand_uid = make_user(pool, "乙", "微信：inv-b")
    cand_iid = make_intent(pool, cand_uid, team_size=1)

    inv = pool.create_invitation(host_iid, cand_iid)
    inv_again = pool.create_invitation(host_iid, cand_iid)
    check("重复邀请返回同一条", inv["invitation_id"] == inv_again["invitation_id"])
    check("邀请初始待确认", inv["status"] == "pending")

    rejected = pool.respond_invitation(inv["invitation_id"], False, cand_uid)
    check("拒绝邀请", rejected["status"] == "rejected")
    check("拒绝后不能留言", pool.is_chat_open(host_iid, cand_iid) is False)

    try:
        pool.send_message(host_iid, cand_iid, host_uid, "在吗")
        check("拒绝后留言应报错", False)
    except ValueError:
        check("拒绝后留言应报错", True)

    inv2 = pool.create_invitation(host_iid, cand_iid)
    check("拒绝后可以重新邀请", inv2["invitation_id"] != inv["invitation_id"])
    pool.respond_invitation(inv2["invitation_id"], True, cand_uid)
    check("同意后开放留言", pool.is_chat_open(host_iid, cand_iid) is True)

    pool.send_message(host_iid, cand_iid, host_uid, "你好，想一起参加大创赛")
    pool.send_message(cand_iid, host_iid, cand_uid, "可以，我们聊聊分工")
    messages = pool.get_messages(host_iid, cand_iid)
    check("留言双向可读", len(messages) == 2, str(messages))
    check("留言内容正确", messages[0]["text"].startswith("你好"))

    first = pool.confirm_team(inv2["invitation_id"], host_uid)
    check("单方组队确认未完成", first["both_confirmed"] is False)
    check("单方确认后仍是匹配中", pool.get_intent(host_iid)["status"] == "matching")
    second = pool.confirm_team(inv2["invitation_id"], cand_uid)
    check("双方确认后组队完成", second["both_confirmed"] is True)
    check("双方意向均为 teamed",
          pool.get_intent(host_iid)["status"] == "teamed"
          and pool.get_intent(cand_iid)["status"] == "teamed")


def test_score_optional(tmp: str) -> None:
    print("\n=== 可选维度权重 ===")
    host_intent = {
        "wanted_skills": ["Python"], "preferred_role": "算法",
        "has_idea": True, "willing_offline": True,
    }
    host_user = {
        "school": "湖南第一师范学院", "free_time": ["周末"],
        "interests": ["AI"], "gender_preference": "不限",
    }
    cand_intent = {
        "skills": ["Python"], "preferred_role": "文档",
        "has_idea": True, "willing_offline": True,
    }
    cand_user = {
        "school": "湖南第一师范学院", "free_time": ["周末"],
        "interests": ["AI"], "gender_preference": "不限",
    }
    data = matcher.score_pair(host_intent, host_user, cand_intent, cand_user)
    check("全维度得分 100", data["score"] == 100.0, str(data["score"]))
    check("未填维度不计入检查", all(v["applied"] for v in data["breakdown"].values()))

    # 可选字段全空：只按技能 + 学校归一化，仍可到 100
    data2 = matcher.score_pair(
        {"wanted_skills": ["Python"], "preferred_role": "", "has_idea": None, "willing_offline": None},
        {"school": "湖南第一师范学院", "free_time": [], "interests": [], "gender_preference": ""},
        {"skills": ["Python"], "preferred_role": "", "has_idea": None, "willing_offline": None},
        {"school": "湖南第一师范学院", "free_time": [], "interests": [], "gender_preference": ""},
    )
    check("可选未填时归一化到 100", data2["score"] == 100.0, str(data2["score"]))
    check("可选未填维度 applied=False",
          data2["breakdown"]["空闲时间"]["applied"] is False
          and data2["breakdown"]["兴趣方向"]["applied"] is False)


def main() -> None:
    if not os.path.exists(os.path.join(BASE_DIR, "competition_table.json")):
        print("警告：未找到 competition_table.json，将使用内置默认表")

    tmp = tempfile.mkdtemp(prefix="team_match_test_")
    try:
        test_basic_algorithms(tmp)
        test_normal_match(os.path.join(tmp, "normal"))
        test_team_size_constraints(os.path.join(tmp, "size"))
        test_no_candidates_but_stored(os.path.join(tmp, "none"))
        test_duplicate_skills(os.path.join(tmp, "dup"))
        test_teamed_removed(os.path.join(tmp, "teamed"))
        test_expiry(os.path.join(tmp, "expiry"))
        test_reminders(os.path.join(tmp, "remind"))
        test_inverted_index(os.path.join(tmp, "index"))
        test_invitation_and_chat(os.path.join(tmp, "inv"))
        test_score_optional(os.path.join(tmp, "score"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 50)
    print("测试结果：通过 %d 项，失败 %d 项" % (PASS_COUNT, FAIL_COUNT))
    if FAIL_COUNT:
        print("失败项：")
        for item in FAIL_DETAILS:
            print("  - " + item)
        sys.exit(1)
    print("全部测试通过。")
    sys.exit(0)


if __name__ == "__main__":
    main()
