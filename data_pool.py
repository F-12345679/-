# -*- coding: utf-8 -*-
"""
data_pool.py — 参赛池数据层（用户表 + 参赛意向表 + 邀请 + 站内留言）

设计说明（答辩可讲）：
1. 用户表 users：以 user_id 为键的哈希表（Python dict），查找/更新 O(1)。
   昵称只用于展示，允许重名，不作为身份标识；user_id 由系统自增生成。
2. 参赛意向表 intents：同样以 intent_id 为键的哈希表，一个 user_id 可以对应多条意向。
   匹配在“意向”层面进行，候选池 = status == "matching" 且未过期的意向。
3. 倒排索引：skill -> set(intent_id)，由 build_inverted_index() 基于当前意向动态构建，
   用于按“希望队友擅长”的技能快速取候选。构建 O(意向数 × 平均技能数)，
   查询单个技能 O(1)，多个技能取并集 O(命中数)。
4. 邀请表 invitations：invitation_id -> 邀请记录，含状态机与双方组队确认标记。
5. 消息表 messages：以 "intentA|intentB" 为键（两意向 ID 排序后拼接）存留言列表，
   只有邀请状态为 accepted 时才允许写入。
6. 持久化：JSON 文件，写入采用“临时文件 + os.replace”避免半截文件。
   不使用数据库，符合课设要求。

所有时间字段统一用字符串 "YYYY-MM-DD HH:MM:SS"，方便 JSON 序列化与字典序比较。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import neon_store
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ISO_FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"

# 用户基础信息只允许这些字段（登录昵称/手机号/联系方式/学校/专业/空闲时间/性别偏好/兴趣）
USER_FIELDS = [
    "nickname",
    "phone",
    "contact",
    "contact_type",
    "school",
    "major",
    "free_time",
    "gender_preference",
    "interests",
]

# 意向中允许写入的字段
INTENT_FIELDS = [
    "competition",
    "is_innovation",
    "skills",
    "wanted_skills",
    "team_size",
    "available_period",
    "preferred_role",
    "has_idea",
    "willing_offline",
    "competition_range",
]

CHINESE_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def now_str() -> str:
    """当前时间字符串。"""
    return datetime.now().strftime(ISO_FMT)


def today_str() -> str:
    return date.today().strftime(DATE_FMT)


def parse_date(value: Any) -> Optional[date]:
    """尽量从字符串中解析日期，支持 2026-01-15 / 2026/1/15 / 2026年1月15日。"""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    m = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def to_int(value: Any) -> Optional[int]:
    """把 '2人'、'两'、' 3 ' 等转成 int，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    m = re.search(r"-?\d+", text)
    if m:
        try:
            return int(m.group())
        except ValueError:
            return None
    # 处理“两个人/三人”这类中文数字
    if text in CHINESE_NUM:
        return CHINESE_NUM[text]
    for ch, num in CHINESE_NUM.items():
        if ch in text:
            return num
    return None


def to_bool(value: Any) -> Optional[bool]:
    """把自然语言中的是/否转成 bool，无法判断返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("是", "有", "愿意", "可以", "能", "true", "yes", "y", "1"):
        return True
    if text in ("否", "没有", "不愿意", "不可以", "不能", "false", "no", "n", "0", "无"):
        return False
    if any(k in text for k in ("愿意", "已有", "有创意", "会去", "可以线下")):
        return True
    if any(k in text for k in ("不愿意", "没有", "无创意", "不线下", "不想")):
        return False
    return None


def dedupe_list(value: Any) -> List[str]:
    """
    把字符串或列表规范成去重后的字符串列表。
    技能去重时忽略大小写与空格，保留首次出现的写法。
    例：["Python", "python", " Python ", "C"] -> ["Python", "C"]
    """
    if value is None:
        return []
    if isinstance(value, str):
        # 注意不按 "/" 切分，避免把 C/C++ 拆坏
        parts = re.split(r"[,，、;；\n\r\t]+|\s{2,}", value)
        parts = [p.strip() for p in parts if p and p.strip()]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(p).strip() for p in value if str(p).strip()]
    else:
        parts = [str(value).strip()]
    seen: Set[str] = set()
    result: List[str] = []
    for part in parts:
        key = part.lower().replace(" ", "")
        if key and key not in seen:
            seen.add(key)
            result.append(part)
    return result


def normalize_period(value: Any) -> Dict[str, Optional[str]]:
    """
    意向时间段统一成 {"raw": 原文, "start_date": "YYYY-MM-DD"|None, "end_date": ...}。
    兼容直接传字符串或传 dict。
    """
    if isinstance(value, dict):
        raw = value.get("raw") or value.get("text") or value.get("desc") or ""
        start = value.get("start_date") or value.get("start")
        end = value.get("end_date") or value.get("end")
        return {
            "raw": str(raw).strip(),
            "start_date": str(start).strip() if start else None,
            "end_date": str(end).strip() if end else None,
        }
    text = str(value).strip() if value is not None else ""
    return {"raw": text, "start_date": None, "end_date": None}


def detect_contact_type(contact: str) -> str:
    """根据联系方式文本猜测类型，只用于展示。"""
    if not contact:
        return ""
    text = str(contact).lower()
    if "@" in text or "邮箱" in text or "email" in text:
        return "邮箱"
    if "qq" in text:
        return "QQ"
    if text.isdigit() and 5 <= len(text) <= 12:
        return "QQ"
    if "微信" in text or "wx" in text or "wechat" in text:
        return "微信"
    return "联系方式"


def pair_key(intent_a: str, intent_b: str) -> str:
    """两个意向的会话键：固定按字典序拼接，保证 A-B 与 B-A 得到同一个键。"""
    return "|".join(sorted([str(intent_a), str(intent_b)]))


class DataPool:
    """
    参赛池数据访问对象。
    文件：
      users.json        用户表
      intents.json      参赛意向表
      invitations.json  邀请记录（含组队确认状态）
      messages.json     站内留言
    """

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

        self.users_path = os.path.join(self.base_dir, "users.json")
        self.intents_path = os.path.join(self.base_dir, "intents.json")
        self.invitations_path = os.path.join(self.base_dir, "invitations.json")
        self.messages_path = os.path.join(self.base_dir, "messages.json")

        # 哈希表：id -> 记录
        self.users: Dict[str, Dict[str, Any]] = {}
        self.intents: Dict[str, Dict[str, Any]] = {}
        self.invitations: Dict[str, Dict[str, Any]] = {}
        self.messages: Dict[str, List[Dict[str, Any]]] = {}

        # 自增主键
        self.next_user_seq = 1
        self.next_intent_seq = 1
        self.next_invitation_seq = 1
        self.next_message_seq = 1

        self._load()

    # ------------------------------------------------------------------
    # 读写 JSON
    # ------------------------------------------------------------------
    @staticmethod
    def _read_json(path: str, default: Any) -> Any:
        key = neon_store.file_key(path)
        if neon_store.is_configured():
            # 云端模式：从 Neon 读，未读到才用默认值
            return neon_store.load_json(key, default)
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            # 文件损坏时退回默认值，避免整个应用无法启动
            return default

    @staticmethod
    def _write_json(path: str, data: Any) -> None:
        """
        写入数据：云端模式先写 Neon（成功即返回）；否则写本地 JSON（先临时文件再原子替换）。
        Windows 下文件被占用时会抛 PermissionError，这里做多次重试；
        仍失败则退回直接写入，最后仍失败只记录错误、不打断页面运行。
        """
        key = neon_store.file_key(path)
        if neon_store.save_json(key, data):
            return
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        last_error: Optional[BaseException] = None
        for attempt in range(5):
            tmp = "%s.%d.%d.tmp" % (path, os.getpid(), attempt)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
                return
            except OSError as exc:
                last_error = exc
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                time.sleep(0.15 * (attempt + 1))

        # 原子替换持续失败时，尝试直接覆盖原文件
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            return
        except OSError as exc:
            last_error = exc

        print(
            "[data_pool] 保存失败（数据仍保留在内存中）：%s" % last_error,
            file=sys.stderr,
        )

    def _load(self) -> None:
        users_data = self._read_json(self.users_path, {"next_seq": 1, "users": {}})
        self.users = users_data.get("users", {}) or {}
        self.next_user_seq = int(users_data.get("next_seq", 1))

        intents_data = self._read_json(self.intents_path, {"next_seq": 1, "intents": {}})
        self.intents = intents_data.get("intents", {}) or {}
        self.next_intent_seq = int(intents_data.get("next_seq", 1))

        inv_data = self._read_json(self.invitations_path, {"next_seq": 1, "invitations": {}})
        self.invitations = inv_data.get("invitations", {}) or {}
        self.next_invitation_seq = int(inv_data.get("next_seq", 1))

        msg_data = self._read_json(
            self.messages_path, {"next_seq": 1, "messages": {}}
        )
        self.messages = msg_data.get("messages", {}) or {}
        self.next_message_seq = int(msg_data.get("next_seq", 1))

        # 如果手工改过文件/序号落后，自动把序号推到已有最大编号之后，避免主键冲突
        self.next_user_seq = max(self.next_user_seq, self._max_seq(self.users.keys(), "U") + 1)
        self.next_intent_seq = max(self.next_intent_seq, self._max_seq(self.intents.keys(), "I") + 1)
        self.next_invitation_seq = max(
            self.next_invitation_seq, self._max_seq(self.invitations.keys(), "IV") + 1
        )
        self.next_message_seq = max(self.next_message_seq, self._max_msg_id() + 1)

    @staticmethod
    def _max_seq(keys: Iterable[str], prefix: str) -> int:
        best = 0
        for key in keys:
            m = re.fullmatch(re.escape(prefix) + r"(\d+)", str(key))
            if m:
                best = max(best, int(m.group(1)))
        return best

    def _max_msg_id(self) -> int:
        best = 0
        for msgs in self.messages.values():
            for msg in msgs:
                m = re.fullmatch(r"M(\d+)", str(msg.get("message_id", "")))
                if m:
                    best = max(best, int(m.group(1)))
        return best

    def _save_users(self) -> None:
        self._write_json(
            self.users_path, {"next_seq": self.next_user_seq, "users": self.users}
        )

    def _save_intents(self) -> None:
        self._write_json(
            self.intents_path, {"next_seq": self.next_intent_seq, "intents": self.intents}
        )

    def _save_invitations(self) -> None:
        self._write_json(
            self.invitations_path,
            {"next_seq": self.next_invitation_seq, "invitations": self.invitations},
        )

    def _save_messages(self) -> None:
        self._write_json(
            self.messages_path,
            {"next_seq": self.next_message_seq, "messages": self.messages},
        )

    # ------------------------------------------------------------------
    # 用户表
    # ------------------------------------------------------------------
    def list_users(self) -> List[str]:
        """返回按 user_id 排序的用户 ID 列表。"""
        return sorted(self.users.keys())

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        return self.users.get(user_id)

    def _find_user_by_contact(self, contact: str) -> Optional[str]:
        """
        按联系方式查找用户（仅兼容旧数据，身份判定不再以联系方式为准）。
        """
        target = str(contact or "").strip().lower().replace(" ", "")
        if not target:
            return None
        for uid, user in self.users.items():
            if str(user.get("contact", "")).strip().lower().replace(" ", "") == target:
                return uid
        return None

    def find_user_by_nickname(self, nickname: str) -> Optional[str]:
        """按昵称查找用户。昵称唯一，是判定同一用户的依据。"""
        target = str(nickname or "").strip().lower()
        if not target:
            return None
        for uid, user in self.users.items():
            if str(user.get("nickname", "")).strip().lower() == target:
                return uid
        return None

    def upsert_user(self, data: Dict[str, Any]) -> str:
        """
        创建用户；若昵称已存在则更新该用户。
        昵称唯一，身份以昵称为准；手机号不限制唯一。
        """
        nickname = str(data.get("nickname") or "").strip()
        contact = str(data.get("contact") or "").strip()
        exists = self.find_user_by_nickname(nickname)
        if not exists and not nickname:
            # 极少数旧调用没有传昵称时，退回按联系方式兼容
            exists = self._find_user_by_contact(contact)
        if exists:
            return self.update_user(exists, data)

        user_id = "U%03d" % self.next_user_seq
        self.next_user_seq += 1
        user = {
            "user_id": user_id,
            "nickname": nickname,
            "phone": str(data.get("phone") or "").strip(),
            "contact": contact,
            "contact_type": data.get("contact_type") or detect_contact_type(contact),
            "school": str(data.get("school") or "").strip(),
            "major": str(data.get("major") or "").strip(),
            "free_time": dedupe_list(data.get("free_time")),
            "gender_preference": str(data.get("gender_preference") or "").strip(),
            "interests": dedupe_list(data.get("interests")),
            "is_teamed": False,
            "created_at": now_str(),
            "updated_at": now_str(),
        }
        self.users[user_id] = user
        self._save_users()
        return user_id

    def update_user(self, user_id: str, data: Dict[str, Any]) -> str:
        """更新用户基础信息，空值不覆盖已有值。"""
        user = self.users.get(user_id)
        if user is None:
            raise KeyError("用户不存在: %s" % user_id)
        for key in USER_FIELDS:
            if key not in data:
                continue
            value = data[key]
            if value is None or value == "" or value == []:
                continue
            if key in ("free_time", "interests"):
                user[key] = dedupe_list(value)
            else:
                user[key] = str(value).strip()
        if data.get("contact") and not data.get("contact_type"):
            user["contact_type"] = detect_contact_type(user.get("contact", ""))
        user["updated_at"] = now_str()
        self._save_users()
        return user_id

    # ------------------------------------------------------------------
    # 参赛意向表
    # ------------------------------------------------------------------
    def create_intent(self, user_id: str, data: Dict[str, Any]) -> str:
        """为某个用户新增一条参赛意向，初始状态 matching。"""
        if user_id not in self.users:
            raise KeyError("用户不存在: %s" % user_id)

        team_size = to_int(data.get("team_size"))
        if team_size is None or team_size < 1:
            team_size = 1

        intent_id = "I%03d" % self.next_intent_seq
        self.next_intent_seq += 1
        intent = {
            "intent_id": intent_id,
            "user_id": user_id,
            "competition": str(data.get("competition") or "").strip(),
            "is_innovation": bool(data.get("is_innovation", True)),
            "skills": dedupe_list(data.get("skills")),
            "wanted_skills": dedupe_list(data.get("wanted_skills")),
            "team_size": team_size,
            "available_period": normalize_period(data.get("available_period")),
            "preferred_role": str(data.get("preferred_role") or "").strip(),
            "has_idea": to_bool(data.get("has_idea")),
            "willing_offline": to_bool(data.get("willing_offline")),
            "competition_range": data.get("competition_range") or None,
            "status": "matching",  # matching / teamed / expired
            "team_confirmed": False,
            "created_at": now_str(),
            "updated_at": now_str(),
            "last_reminded_at": None,
            "last_reminded_response_at": None,
            "excluded_reason": None,
        }
        self.intents[intent_id] = intent
        self._save_intents()
        return intent_id

    def update_intent(self, intent_id: str, data: Dict[str, Any]) -> str:
        intent = self.intents.get(intent_id)
        if intent is None:
            raise KeyError("意向不存在: %s" % intent_id)
        for key in INTENT_FIELDS:
            if key in data:
                value = data[key]
                if key == "skills" or key == "wanted_skills":
                    intent[key] = dedupe_list(value)
                elif key == "available_period":
                    intent[key] = normalize_period(value)
                elif key == "team_size":
                    number = to_int(value)
                    intent[key] = number if number and number >= 1 else intent.get(key, 1)
                elif key == "has_idea" or key == "willing_offline":
                    intent[key] = to_bool(value)
                elif value is not None:
                    intent[key] = value
        intent["updated_at"] = now_str()
        self._save_intents()
        return intent_id

    def get_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        return self.intents.get(intent_id)

    def list_intents(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        items = list(self.intents.values())
        if status is not None:
            items = [x for x in items if x.get("status") == status]
        return sorted(items, key=lambda x: x.get("intent_id", ""))

    def list_user_intents(
        self, user_id: str, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        items = [x for x in self.intents.values() if x.get("user_id") == user_id]
        if status is not None:
            items = [x for x in items if x.get("status") == status]
        return sorted(items, key=lambda x: x.get("intent_id", ""))

    def is_intent_expired(self, intent: Dict[str, Any], today: Optional[date] = None) -> bool:
        """按可组队时间段的结束日期判断是否过期。未填结束日期视为不过期。"""
        period = intent.get("available_period") or {}
        end = parse_date(period.get("end_date"))
        if end is None:
            return False
        return end < (today or date.today())

    def list_matching_intents(self, today: Optional[date] = None) -> List[Dict[str, Any]]:
        """候选池：状态 matching 且未过期。"""
        today = today or date.today()
        result = []
        for intent in self.intents.values():
            if intent.get("status") == "matching" and not self.is_intent_expired(intent, today):
                result.append(intent)
        return sorted(result, key=lambda x: x.get("intent_id", ""))

    def expire_intents(self, today: Optional[date] = None) -> List[str]:
        """
        过期清理：超过可组队时间段截止日期且仍未组队成功的意向，
        状态改为 expired，从候选池移除，但资料保留。
        复杂度 O(意向数)。
        """
        today = today or date.today()
        expired = []
        for intent in self.intents.values():
            if intent.get("status") != "matching":
                continue
            if self.is_intent_expired(intent, today):
                intent["status"] = "expired"
                intent["excluded_reason"] = "超过可组队时间段截止日期"
                intent["updated_at"] = now_str()
                expired.append(intent["intent_id"])
        if expired:
            self._save_intents()
        return expired

    def build_inverted_index(self) -> Dict[str, Set[str]]:
        """
        构建“技能 -> 意向 ID 集合”的倒排索引。
        只索引 matching 且未过期的意向。
        时间复杂度 O(意向数 × 平均技能数)，空间 O(技能-意向对数量)。
        """
        index: Dict[str, Set[str]] = {}
        for intent in self.list_matching_intents():
            for skill in intent.get("skills", []):
                key = str(skill).strip().lower().replace(" ", "")
                if not key:
                    continue
                index.setdefault(key, set()).add(intent["intent_id"])
        return index

    def search_intents_by_skills(self, skills: Iterable[str]) -> Set[str]:
        """按技能列表查倒排索引，返回命中的意向 ID 并集。"""
        index = self.build_inverted_index()
        hits: Set[str] = set()
        for skill in skills or []:
            key = str(skill).strip().lower().replace(" ", "")
            if key:
                hits |= index.get(key, set())
        return hits

    # ------------------------------------------------------------------
    # 组队确认与提醒
    # ------------------------------------------------------------------
    def generate_team_reminders(
        self, interval_hours: int = 24, now: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        对仍在池中、未组队、且距上次提醒超过 interval_hours 的意向生成提醒。
        组队提醒由界面上的“检查组队提醒”按钮触发；默认 24 小时。
        返回本次被提醒的记录列表。
        """
        now = now or datetime.now()
        deadline = now - timedelta(hours=interval_hours)
        reminded: List[Dict[str, Any]] = []
        changed = False
        for intent in self.intents.values():
            if intent.get("status") != "matching":
                continue
            if self.is_intent_expired(intent, now.date()):
                continue
            last = intent.get("last_reminded_at")
            last_dt = None
            if last:
                try:
                    last_dt = datetime.strptime(str(last), ISO_FMT)
                except ValueError:
                    last_dt = None
            if last_dt is not None and last_dt > deadline:
                continue
            intent["last_reminded_at"] = now.strftime(ISO_FMT)
            intent["updated_at"] = now.strftime(ISO_FMT)
            changed = True
            user = self.users.get(intent.get("user_id", ""), {})
            reminded.append(
                {
                    "intent_id": intent["intent_id"],
                    "user_id": intent.get("user_id"),
                    "nickname": user.get("nickname", ""),
                    "competition": intent.get("competition", ""),
                    "reminded_at": intent["last_reminded_at"],
                }
            )
        if changed:
            self._save_intents()
        return reminded

    def needs_team_response(self, intent: Dict[str, Any]) -> bool:
        """是否有一条尚未回应的组队提醒。"""
        if intent.get("status") != "matching":
            return False
        last = intent.get("last_reminded_at")
        if not last:
            return False
        resp = intent.get("last_reminded_response_at")
        return not resp or str(resp) < str(last)

    def respond_team_reminder(
        self, intent_id: str, teamed: bool, now: Optional[datetime] = None
    ) -> str:
        """
        用户回应组队提醒：
        - 已组队：intent 状态改为 teamed，从候选池移除；
        - 未组队：状态保持 matching，继续留在池中。
        now 参数便于测试时推进时间。
        """
        intent = self.intents.get(intent_id)
        if intent is None:
            raise KeyError("意向不存在: %s" % intent_id)
        stamp = (now or datetime.now()).strftime(ISO_FMT)
        intent["last_reminded_response_at"] = stamp
        intent["team_confirmed"] = bool(teamed)
        intent["updated_at"] = stamp
        if teamed:
            intent["status"] = "teamed"
            user = self.users.get(intent.get("user_id", ""))
            if user is not None:
                user["is_teamed"] = True
                user["updated_at"] = stamp
                self._save_users()
        self._save_intents()
        return intent["status"]

    # ------------------------------------------------------------------
    # 邀请状态机
    # ------------------------------------------------------------------
    def create_invitation(self, from_intent_id: str, to_intent_id: str) -> Dict[str, Any]:
        """
        发起邀请。状态机：pending -> accepted / rejected。
        同一对意向若已有 pending 或 accepted 的邀请，直接返回旧记录，避免重复。
        """
        if from_intent_id == to_intent_id:
            raise ValueError("不能邀请自己")
        f = self.intents.get(from_intent_id)
        t = self.intents.get(to_intent_id)
        if f is None or t is None:
            raise KeyError("邀请涉及的意向不存在")
        if f.get("user_id") == t.get("user_id"):
            raise ValueError("不能邀请同一个用户")
        if f.get("status") != "matching" or t.get("status") != "matching":
            raise ValueError("只有匹配中的意向才能发起邀请")

        for existing in self.get_invitations_between(from_intent_id, to_intent_id):
            if existing.get("status") in ("pending", "accepted"):
                return existing

        inv_id = "IV%03d" % self.next_invitation_seq
        self.next_invitation_seq += 1
        invitation = {
            "invitation_id": inv_id,
            "from_intent": from_intent_id,
            "to_intent": to_intent_id,
            "from_user": f["user_id"],
            "to_user": t["user_id"],
            "status": "pending",  # pending / accepted / rejected
            # 组队需要双方各自点击确认；键为 user_id
            "team_confirm": {f["user_id"]: False, t["user_id"]: False},
            "created_at": now_str(),
            "updated_at": now_str(),
        }
        self.invitations[inv_id] = invitation
        self._save_invitations()
        return invitation

    def get_invitation(self, invitation_id: str) -> Optional[Dict[str, Any]]:
        return self.invitations.get(invitation_id)

    def get_invitations_between(
        self, intent_a: str, intent_b: str
    ) -> List[Dict[str, Any]]:
        """返回这对意向之间的所有邀请，按更新时间倒序（最新在前）。"""
        items = []
        for inv in self.invitations.values():
            pair = {inv.get("from_intent"), inv.get("to_intent")}
            if pair == {intent_a, intent_b}:
                items.append(inv)
        return sorted(items, key=lambda x: x.get("updated_at", ""), reverse=True)

    def get_invitation_between(
        self, intent_a: str, intent_b: str
    ) -> Optional[Dict[str, Any]]:
        """返回这对意向之间最新的一条邀请（UI 展示用）。"""
        items = self.get_invitations_between(intent_a, intent_b)
        return items[0] if items else None

    def list_invitations_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        items = [
            inv
            for inv in self.invitations.values()
            if user_id in (inv.get("from_user"), inv.get("to_user"))
        ]
        return sorted(items, key=lambda x: x.get("updated_at", ""), reverse=True)

    def respond_invitation(
        self, invitation_id: str, accept: bool, user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """被邀请人同意/拒绝。只有 pending 状态可变更。"""
        inv = self.invitations.get(invitation_id)
        if inv is None:
            raise KeyError("邀请不存在: %s" % invitation_id)
        if inv.get("status") != "pending":
            return inv
        if user_id is not None and user_id != inv.get("to_user"):
            raise ValueError("只有被邀请人才能处理该邀请")
        inv["status"] = "accepted" if accept else "rejected"
        inv["updated_at"] = now_str()
        self._save_invitations()
        return inv

    def is_chat_open(self, intent_a: str, intent_b: str) -> bool:
        """站内留言是否开放：只要这对意向存在任意一条 accepted 邀请即可。"""
        return any(
            inv.get("status") == "accepted"
            for inv in self.get_invitations_between(intent_a, intent_b)
        )

    def confirm_team(self, invitation_id: str, user_id: str) -> Dict[str, Any]:
        """
        组队确认：双方各自调用一次。
        两边都确认后，两条意向都标记为 teamed，并从候选池移除。
        """
        inv = self.invitations.get(invitation_id)
        if inv is None:
            raise KeyError("邀请不存在: %s" % invitation_id)
        if inv.get("status") != "accepted":
            raise ValueError("只有已同意的邀请才能确认组队")
        if user_id not in (inv.get("from_user"), inv.get("to_user")):
            raise ValueError("当前用户不属于该邀请")

        inv.setdefault("team_confirm", {})
        inv["team_confirm"][user_id] = True
        inv["updated_at"] = now_str()

        both = all(
            inv["team_confirm"].get(uid, False)
            for uid in (inv.get("from_user"), inv.get("to_user"))
        )
        if both:
            for intent_id in (inv.get("from_intent"), inv.get("to_intent")):
                intent = self.intents.get(intent_id)
                if intent is not None:
                    intent["status"] = "teamed"
                    intent["team_confirmed"] = True
                    intent["updated_at"] = now_str()
            for uid in (inv.get("from_user"), inv.get("to_user")):
                user = self.users.get(uid)
                if user is not None:
                    user["is_teamed"] = True
                    user["updated_at"] = now_str()
            self._save_users()
            self._save_intents()
        self._save_invitations()
        return {"invitation": inv, "both_confirmed": both}

    # ------------------------------------------------------------------
    # 站内留言
    # ------------------------------------------------------------------
    def send_message(
        self, intent_a: str, intent_b: str, from_user_id: str, text: str
    ) -> Dict[str, Any]:
        """只有邀请已同意（accepted）时才允许站内留言。"""
        if not self.is_chat_open(intent_a, intent_b):
            raise ValueError("对方尚未同意邀请，暂时不能站内留言")
        text = str(text or "").strip()
        if not text:
            raise ValueError("留言内容不能为空")
        key = pair_key(intent_a, intent_b)
        msg_id = "M%04d" % self.next_message_seq
        self.next_message_seq += 1
        message = {
            "message_id": msg_id,
            "from_user": from_user_id,
            "text": text,
            "created_at": now_str(),
        }
        self.messages.setdefault(key, []).append(message)
        self._save_messages()
        return message

    def get_messages(self, intent_a: str, intent_b: str) -> List[Dict[str, Any]]:
        return list(self.messages.get(pair_key(intent_a, intent_b), []))


# ----------------------------------------------------------------------
# 示例数据：用于本地测试/答辩，不依赖大模型
# ----------------------------------------------------------------------
def seed_demo_data(pool: DataPool) -> int:
    """
    写入 4 个示例用户与 4 条参赛意向（若联系方式已存在则跳过）。
    返回新写入的意向数量。
    """
    end = (date.today() + timedelta(days=120)).strftime(DATE_FMT)
    start = date.today().strftime(DATE_FMT)
    demos = [
        {
            "user": {
                "nickname": "小林",
                "contact": "微信：xiaolin_2026",
                "contact_type": "微信",
                "school": "湖南第一师范学院",
                "major": "数据科学与大数据技术",
                "free_time": ["周三晚上", "周末"],
                "gender_preference": "不限",
                "interests": ["人工智能", "数据分析"],
            },
            "intent": {
                "competition": "大创赛",
                "is_innovation": True,
                "skills": ["Python", "算法", "数据清洗"],
                "wanted_skills": ["文档写作", "PPT", "产品设计"],
                "team_size": 2,
                "available_period": {"raw": "这个学期", "start_date": start, "end_date": end},
                "preferred_role": "算法与数据分析",
                "has_idea": True,
                "willing_offline": True,
            },
        },
        {
            "user": {
                "nickname": "阿May",
                "contact": "QQ：123456789",
                "contact_type": "QQ",
                "school": "湖南第一师范学院",
                "major": "汉语言文学",
                "free_time": ["周末", "周五晚上"],
                "gender_preference": "不限",
                "interests": ["写作", "演讲", "产品设计"],
            },
            "intent": {
                "competition": "大创赛",
                "is_innovation": True,
                "skills": ["文档写作", "PPT", "项目管理"],
                "wanted_skills": ["Python", "算法", "数据分析"],
                "team_size": 1,
                "available_period": {"raw": "这个学期", "start_date": start, "end_date": end},
                "preferred_role": "文档与汇报",
                "has_idea": False,
                "willing_offline": True,
            },
        },
        {
            "user": {
                "nickname": "老周",
                "contact": "邮箱：zhou@example.com",
                "contact_type": "邮箱",
                "school": "中南大学",
                "major": "软件工程",
                "free_time": ["工作日晚上"],
                "gender_preference": "男",
                "interests": ["后端开发", "开源"],
            },
            "intent": {
                "competition": "挑战杯",
                "is_innovation": True,
                "skills": ["Java", "Spring Boot", "数据库"],
                "wanted_skills": ["前端", "UI设计"],
                "team_size": 2,
                "available_period": {"raw": "接下来三个月", "start_date": start, "end_date": end},
                "preferred_role": "后端开发",
                "has_idea": True,
                "willing_offline": False,
            },
        },
        {
            "user": {
                "nickname": "小夏",
                "contact": "微信：xiaoxia_design",
                "contact_type": "微信",
                "school": "湖南师范大学",
                "major": "视觉传达设计",
                "free_time": ["周末", "周三下午"],
                "gender_preference": "女",
                "interests": ["设计", "摄影", "品牌"],
            },
            "intent": {
                "competition": "互联网+",
                "is_innovation": True,
                "skills": ["UI设计", "Figma", "PPT"],
                "wanted_skills": ["前端", "后端", "产品经理"],
                "team_size": 2,
                "available_period": {"raw": "这个学期", "start_date": start, "end_date": end},
                "preferred_role": "UI设计",
                "has_idea": False,
                "willing_offline": True,
            },
        },
    ]

    created = 0
    for item in demos:
        uid = pool.upsert_user(item["user"])
        # 已有同联系方式的用户时，避免重复插入相同意向
        exists = any(
            i.get("competition") == item["intent"]["competition"]
            for i in pool.list_user_intents(uid)
        )
        if not exists:
            pool.create_intent(uid, item["intent"])
            created += 1
    return created
