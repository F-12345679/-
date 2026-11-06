# -*- coding: utf-8 -*-
"""
llm_api.py — DeepSeek 调用封装

职责边界（非常重要）：
  DeepSeek 只负责三件事：
    1. 理解用户自然语言、提取字段；
    2. 判断比赛是否属于创新类；
    3. 把代码算出的“缺失字段清单/确认信息”翻译成自然中文。
  匹配度、硬约束、排序全部在 matcher.py 用 Python 完成，本文件绝不计算匹配分。

API Key 从项目根目录 .env 读取（python-dotenv），不写死在代码里。
"""

from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import matcher
from data_pool import dedupe_list, parse_date, to_bool, to_int

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL = "deepseek-v3"

# 进页面第一句（写死）
OPENING_MESSAGE = (
    "你好，我是组队匹配智能体。你可以随便和我聊，问我有哪些创新类比赛，"
    "也可以直接说“我想参加大创赛”，我来帮你找队友。"
)

# 必填齐全后的可选引导（写死）
OPTIONAL_PROMPT = (
    "如果您对以下可选项有需求也可以告诉我：空闲时间、性别偏好、兴趣方向、"
    "希望担任角色、是否已有项目创意、是否愿意线下见面；如果没有或不确定，直接说'没有'就行。"
)

# 字段中文别名 -> 规范字段名（兼容大模型可能返回中文键）
KEY_ALIASES = {
    "比赛名称": "competition", "比赛": "competition", "赛事": "competition",
    "昵称": "nickname", "称呼": "nickname",
    "联系方式": "contact", "微信": "contact", "qq": "contact", "QQ": "contact",
    "邮箱": "contact", "电话": "contact", "手机": "contact",
    "专业": "major",
    "掌握技术": "skills", "技能": "skills", "擅长": "skills", "掌握技能": "skills",
    "希望队友擅长": "wanted_skills", "期望技能": "wanted_skills",
    "队友技能": "wanted_skills", "想找的技能": "wanted_skills",
    "已有几人": "team_size", "人数": "team_size", "队伍人数": "team_size",
    "学校": "school", "院校": "school",
    "可组队时间段": "available_period", "时间段": "available_period",
    "组队时间": "available_period", "可组队时间": "available_period",
    "空闲时间": "free_time",
    "性别偏好": "gender_preference",
    "兴趣方向": "interests", "兴趣": "interests",
    "希望担任角色": "preferred_role", "角色": "preferred_role",
    "是否已有项目创意": "has_idea", "已有创意": "has_idea", "项目创意": "has_idea",
    "是否愿意线下见面": "willing_offline", "线下见面": "willing_offline",
}

ALL_KEYS = [
    "competition", "nickname", "contact", "major", "skills", "wanted_skills",
    "team_size", "school", "available_period", "free_time", "gender_preference",
    "interests", "preferred_role", "has_idea", "willing_offline",
]

_client = None


# ----------------------------------------------------------------------
# 环境 / 客户端
# ----------------------------------------------------------------------
def _manual_load_env(path: str) -> None:
    """python-dotenv 不可用时的兜底：只解析 KEY=VALUE 行。"""
    if not os.path.exists(path):
        return
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


try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(ENV_PATH)
except Exception:
    _manual_load_env(ENV_PATH)


def get_api_key() -> str:
    """读取 DeepSeek Key（不在界面/日志中输出具体内容）。"""
    return str(os.environ.get("DEEPSEEK_API_KEY", "")).strip()


def has_api_key() -> bool:
    return bool(get_api_key())


def get_client():
    """延迟创建 OpenAI 兼容客户端，避免没有安装 openai 时导入即报错。"""
    global _client
    if _client is None:
        key = get_api_key()
        if not key:
            raise RuntimeError("未在项目根目录 .env 中读取到 DEEPSEEK_API_KEY")
        from openai import OpenAI  # type: ignore

        _client = OpenAI(api_key=key, base_url=BASE_URL, timeout=45)
    return _client


def _extract_json(content: str) -> Dict[str, Any]:
    """从模型输出里抠出 JSON（兼容 ```json 包裹）。"""
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


def _chat_json(system: str, user: str, temperature: float = 0.2) -> Dict[str, Any]:
    try:
        client = get_client()
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content or ""
        return _extract_json(content)
    except Exception as exc:  # 网络/额度/Key 问题都不要让页面崩掉
        return {"_error": str(exc)}


def _chat_text(system: str, user: str, temperature: float = 0.7) -> str:
    try:
        client = get_client()
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
        return str(completion.choices[0].message.content or "").strip()
    except Exception:
        return ""


# ----------------------------------------------------------------------
# 时间段解析
# ----------------------------------------------------------------------
def default_period_end(now: Optional[date] = None) -> date:
    """默认截止日：本学期末（1 月 15 日 / 6 月 30 日）。"""
    today = now or date.today()
    if today.month <= 1:
        return date(today.year, 1, 15)
    if today.month <= 7:
        return date(today.year, 6, 30)
    return date(today.year + 1, 1, 15)


def _infer_year(month: int, day: int, today: date, ref: Optional[date] = None) -> int:
    """没有年份的月日：默认按今年理解，若已明显过去则顺延到明年。"""
    base = ref or today
    try:
        candidate = date(base.year, month, day)
    except ValueError:
        return base.year
    if candidate < base - timedelta(days=30):
        return base.year + 1
    return base.year


def _safe_make_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_period_text(text: str, now: Optional[date] = None) -> Dict[str, Optional[str]]:
    """
    把“可组队时间段”自然语言解析成 {raw, start_date, end_date}。
    支持：
      - 2026-10-01 到 2027-01-15 / 2026年10月到2027年1月
      - 12.1到12.8 / 12.1-12.8 / 12月1日到12月8日（不带年份）
      - 这个学期 / 本学期 / 这学期
      - 寒假 / 暑假
      - 接下来三个月 / 未来三个月
      - 这个月
    解析不出来时 end_date 留空，由界面提示用户手填或使用默认值。
    """
    today = now or date.today()
    raw = str(text or "").strip()
    start: Optional[date] = None
    end: Optional[date] = None

    # 日期区间（带年份）
    m = re.search(
        r"(\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?)"
        r"\s*(?:到|至|~|～|-|—|－|—)\s*"
        r"(\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?)",
        raw,
    )
    if m:
        start = parse_date(m.group(1))
        end = parse_date(m.group(2))
    else:
        # 月日区间（不带年份）：12.1到12.8 / 12.1-12.8 / 12月1日到12月8日
        m = re.search(
            r"(\d{1,2})\s*(?:月|[./])\s*(\d{1,2})\s*日?"
            r"\s*(?:到|至|~|～|—|－|-)\s*"
            r"(\d{1,2})\s*(?:月|[./])\s*(\d{1,2})\s*日?",
            raw,
        )
        if m:
            sm, sd, em, ed = (int(m.group(i)) for i in range(1, 5))
            syear = _infer_year(sm, sd, today)
            start = _safe_make_date(syear, sm, sd)
            eyear = _infer_year(em, ed, today, start)
            end = _safe_make_date(eyear, em, ed)
            if start and end and end < start:
                end = _safe_make_date(eyear + 1, em, ed)
        if start is None:
            m = re.search(r"(\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?)", raw)
            if m:
                start = parse_date(m.group(1))

    if end is None:
        if any(k in raw for k in ("这个学期", "本学期", "这学期", "这一学期", "当前学期")):
            start = start or today
            end = default_period_end(today)
        elif "寒假" in raw:
            year = today.year + (1 if today.month >= 9 else 0)
            start = start or today
            end = date(year, 2, 28)
        elif "暑假" in raw:
            start = start or today
            end = date(today.year, 8, 31)
        elif any(k in raw for k in ("接下来三个月", "未来三个月", "三个月内", "近三个月")):
            start = start or today
            end = today + timedelta(days=90)
        elif "这个月" in raw:
            start = start or today
            end = date(today.year, today.month,
                       calendar.monthrange(today.year, today.month)[1])

    return {
        "raw": raw,
        "start_date": start.strftime("%Y-%m-%d") if start else None,
        "end_date": end.strftime("%Y-%m-%d") if end else None,
    }


# ----------------------------------------------------------------------
# 字段规范化
# ----------------------------------------------------------------------
def _norm_competition(value: Any, table: Dict[str, Any]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    info = matcher.competition_info(text, table)
    if info is not None:
        return info[0]
    return text


def normalize_extraction(raw: Dict[str, Any], table: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把模型输出规范成统一字段名与类型，只保留有值的字段。"""
    table = table or matcher.load_competition_table(
        os.path.join(BASE_DIR, "competition_table.json")
    )
    data: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return data

    for raw_key, value in raw.items():
        key = KEY_ALIASES.get(str(raw_key).strip(), str(raw_key).strip())
        if key not in ALL_KEYS:
            continue
        if value is None:
            continue
        if key in ("skills", "wanted_skills", "free_time", "interests"):
            values = dedupe_list(value)
            if values:
                data[key] = values
        elif key == "team_size":
            number = to_int(value)
            if number is not None and number >= 1:
                data[key] = number
        elif key in ("has_idea", "willing_offline"):
            boolean = to_bool(value)
            if boolean is not None:
                data[key] = boolean
        elif key == "available_period":
            if isinstance(value, dict):
                period = {
                    "raw": str(value.get("raw") or value.get("text") or "").strip(),
                    "start_date": value.get("start_date") or value.get("start"),
                    "end_date": value.get("end_date") or value.get("end"),
                }
                if period["raw"] and (not period["start_date"] or not period["end_date"]):
                    parsed = parse_period_text(period["raw"])
                    period["start_date"] = period["start_date"] or parsed["start_date"]
                    period["end_date"] = period["end_date"] or parsed["end_date"]
                if period["raw"]:
                    data[key] = period
            elif str(value).strip():
                data[key] = parse_period_text(str(value).strip())
        else:
            text = str(value).strip()
            if text:
                if key == "competition":
                    text = _norm_competition(text, table)
                data[key] = text
    return data


def merge_fields(current: Dict[str, Any], new_fields: Dict[str, Any]) -> Dict[str, Any]:
    """把本轮提取到的字段合并进已收集字段（时间段做字典合并）。"""
    merged = dict(current or {})
    for key, value in (new_fields or {}).items():
        if value is None or value == "" or value == []:
            continue
        if key == "available_period":
            if isinstance(value, str):
                value = parse_period_text(value)
            if isinstance(value, dict):
                old = merged.get(key)
                if isinstance(old, str):
                    old = parse_period_text(old)
                if not isinstance(old, dict):
                    old = {}
                period = {
                    "raw": value.get("raw") or old.get("raw") or "",
                    "start_date": value.get("start_date") or old.get("start_date"),
                    "end_date": value.get("end_date") or old.get("end_date"),
                }
                merged[key] = period
            continue
        merged[key] = value
    return merged


# ----------------------------------------------------------------------
# 字段提取
# ----------------------------------------------------------------------
def fallback_extract(text: str, table: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    规则兜底提取：当 DeepSeek 不可用时仍能完成基本流程。
    只做保守匹配，不猜测用户没说的信息。
    """
    table = table or matcher.load_competition_table(
        os.path.join(BASE_DIR, "competition_table.json")
    )
    raw_text = str(text or "").strip()
    out: Dict[str, Any] = {}
    if not raw_text:
        return out

    # 比赛名称：先匹配已知比赛/别名
    competitions = (table or {}).get("competitions", {}) or {}
    for name, info in competitions.items():
        candidates = [name] + list((info or {}).get("aliases", []) or [])
        hit = next((c for c in candidates if c and c in raw_text), None)
        if hit:
            out["competition"] = name
            break
    if "competition" not in out:
        m = re.search(
            r"(?:参加|报名|想打|想做|参加的是)\s*"
            r"([\u4e00-\u9fa5A-Za-z0-9+]{2,20}?(?:比赛|大赛|赛|杯))",
            raw_text,
        )
        if m:
            out["competition"] = m.group(1)

    # 昵称
    m = re.search(
        r"(?:叫我|昵称(?:是|叫)?|名字(?:是|叫)?|我叫)\s*"
        r"([\u4e00-\u9fa5A-Za-z0-9_\-]{1,20})",
        raw_text,
    )
    if m:
        out["nickname"] = m.group(1)

    # 联系方式：邮箱优先，其次微信/QQ
    m = re.search(r"[A-Za-z0-9_.+\-]+@[A-Za-z0-9\-]+\.[A-Za-z0-9.\-]+", raw_text)
    if m:
        out["contact"] = m.group()
    else:
        m = re.search(
            r"(?:微信|QQ|qq|邮箱|email)\s*(?:是|为|：|:)?\s*"
            r"([A-Za-z0-9_\-\.@]{3,40})",
            raw_text,
        )
        if m:
            out["contact"] = m.group(1)

    # 已有几人
    m = re.search(
        r"(?:已有|已经有|已经|现有|队伍已有|我们已有|我们已经有)\s*"
        r"([0-9一二三四五六七八九十两]+)\s*(?:个)?人",
        raw_text,
    )
    if m:
        number = to_int(m.group(1))
        if number is not None:
            out["team_size"] = number

    # 专业
    m = re.search(
        r"专业(?:是|为|：|:)?\s*([\u4e00-\u9fa5A-Za-z]{2,20}?)(?:专业|，|,|。|；|;|$)",
        raw_text,
    )
    if not m:
        m = re.search(r"([\u4e00-\u9fa5]{2,12})专业", raw_text)
    if m:
        out["major"] = m.group(1).strip()

    # 学校
    m = re.search(
        r"(?:学校(?:是|在)?|就读于|来自)\s*"
        r"([\u4e00-\u9fa5A-Za-z]{2,25}?)(?:，|,|。|；|;|$)",
        raw_text,
    )
    if not m:
        known_schools = [
            "湖南第一师范学院", "湖南一师", "中南大学", "湖南大学", "湖南师范大学",
            "北京大学", "清华大学", "复旦大学", "上海交通大学", "浙江大学",
        ]
        hit = next((s for s in known_schools if s in raw_text), None)
        if hit:
            out["school"] = hit
    else:
        out["school"] = m.group(1).strip()

    # 希望队友擅长：先抽出来并从文本中剔除，避免被“擅长”正则误当成自己的技能
    working_text = raw_text
    m = re.search(
        r"(?:想找|希望找|希望队友|找一个|找一位|缺|需要)\s*(?:一个|一位)?\s*"
        r"([^，,。；;]+?)\s*(?:的)?(?:队友|同学|人|伙伴)",
        working_text,
    )
    if not m:
        m = re.search(
            r"希望队友(?:擅长|会|能)?\s*([^，,。；;]+)",
            working_text,
        )
    if m:
        wanted_text = m.group(1).strip()
        wanted_text = re.sub(r"^(?:会|擅长|能)", "", wanted_text)
        wanted = [w.strip() for w in re.split(r"[、和及]|以及", wanted_text) if w.strip()]
        if wanted:
            out["wanted_skills"] = wanted
        working_text = working_text[:m.start()] + " " + working_text[m.end():]

    # 掌握技能：擅长/会/掌握 后面的内容
    m = re.search(r"(?:擅长|掌握|熟悉|会)\s*([^，,。；;]+)", working_text)
    if m:
        skills = re.split(r"[、和及]|以及", m.group(1))
        skills = [s.strip() for s in skills if s.strip()]
        if skills:
            out["skills"] = skills

    # 可组队时间段
    m = re.search(
        r"(?:可组队时间段|组队时间段|可组队时间|组队时间|时间段)"
        r"\s*(?:是|为|在|：|:)?\s*([^，,。；;]+)",
        raw_text,
    )
    if m:
        out["available_period"] = parse_period_text(m.group(1).strip())
    else:
        for keyword in ("这个学期", "本学期", "寒假", "暑假", "接下来三个月", "未来三个月"):
            if keyword in raw_text:
                out["available_period"] = parse_period_text(keyword)
                break

    # 可选字段
    m = re.search(r"空闲时间(?:是|为|：|:)?\s*([^，,。；;]+)", raw_text)
    if m:
        out["free_time"] = [x.strip() for x in re.split(r"[、和及]", m.group(1)) if x.strip()]
    m = re.search(r"性别偏好(?:是|为|：|:)?\s*([^，,。；;]{1,10})", raw_text)
    if m:
        out["gender_preference"] = m.group(1).strip()
    m = re.search(r"兴趣(?:方向)?(?:是|为|：|:)?\s*([^，,。；;]+)", raw_text)
    if m:
        out["interests"] = [x.strip() for x in re.split(r"[、和及]", m.group(1)) if x.strip()]
    m = re.search(r"(?:希望担任|担任|想做)\s*([^，,。；;]+?)(?:角色|，|,|。|$)", raw_text)
    if m:
        out["preferred_role"] = m.group(1).strip()
    if any(k in raw_text for k in ("已有创意", "有创意", "有项目", "有想法")):
        out["has_idea"] = True
    if any(k in raw_text for k in ("没有创意", "没创意", "没有项目")):
        out["has_idea"] = False
    if any(k in raw_text for k in ("愿意线下", "可以线下", "接受线下")):
        out["willing_offline"] = True
    if any(k in raw_text for k in ("不愿意线下", "不想线下", "不能线下")):
        out["willing_offline"] = False

    return normalize_extraction(out, table)


def extract_fields(
    user_text: str,
    collected: Optional[Dict[str, Any]] = None,
    history: Optional[Iterable[Dict[str, Any]]] = None,
    table: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    从用户最新发言中抽取字段。AI 优先，失败时用规则兜底。
    返回的字典只包含本轮明确提到的字段。
    """
    table = table or matcher.load_competition_table(
        os.path.join(BASE_DIR, "competition_table.json")
    )
    collected = collected or {}

    if not has_api_key():
        return fallback_extract(user_text, table)

    history_text = ""
    if history:
        lines = []
        for msg in list(history)[-8:]:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = str(msg.get("content", "")).replace("\n", " ")
            lines.append("%s：%s" % (role, content[:200]))
        history_text = "\n".join(lines)

    system = (
        "你是“创新项目组队匹配智能体”的信息抽取器。你只做信息抽取，"
        "绝对不要计算任何匹配度或推荐结果。\n"
        "从用户最新发言中抽取字段，只输出 JSON，不要输出解释。\n"
        "只抽取用户明确说出的信息，不猜测、不编造；没提到的字段用 null。\n"
        "字段结构：\n"
        "{\n"
        '  "competition": "比赛名称，字符串",\n'
        '  "nickname": "昵称",\n'
        '  "contact": "联系方式原文（微信/QQ/邮箱）",\n'
        '  "major": "专业",\n'
        '  "skills": ["用户自己掌握的技术"],\n'
        '  "wanted_skills": ["希望队友擅长的技术"],\n'
        '  "team_size": 整数（已有几人，含本人）,\n'
        '  "school": "学校",\n'
        '  "available_period": {"raw": "原文", "start_date": "YYYY-MM-DD或null", "end_date": "YYYY-MM-DD或null"},\n'
        '  "free_time": ["空闲时间"],\n'
        '  "gender_preference": "性别偏好",\n'
        '  "interests": ["兴趣方向"],\n'
        '  "preferred_role": "希望担任角色",\n'
        '  "has_idea": true/false/null,\n'
        '  "willing_offline": true/false/null\n'
        "}\n"
        "已经收集到的信息只作参考，不要把旧信息当成用户本轮新说的内容重复输出；"
        "但如果用户本轮明确修改了某个字段，请输出新值。"
    )
    user = (
        "已知比赛名单（可用于规范比赛名称）：%s\n"
        "当前已收集字段：%s\n"
        "最近对话：\n%s\n\n"
        "用户最新发言：%s"
        % (
            "、".join(list((table or {}).get("competitions", {}).keys())),
            json.dumps(collected, ensure_ascii=False),
            history_text or "（无）",
            user_text,
        )
    )

    raw = _chat_json(system, user, temperature=0.1)
    if raw.get("_error"):
        return fallback_extract(user_text, table)

    data = normalize_extraction(raw, table)
    fallback = fallback_extract(user_text, table)
    for key, value in fallback.items():
        if key not in data:
            data[key] = value
    return data


# ----------------------------------------------------------------------
# 创新类比赛判断
# ----------------------------------------------------------------------
def _verdict_from_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value or "").strip().lower()
    if text in ("yes", "true", "1", "是", "属于", "创新类"):
        return "yes"
    if text in ("no", "false", "0", "否", "不属于", "非创新类"):
        return "no"
    if any(k in text for k in ("不是", "不属于", "非创新")):
        return "no"
    if any(k in text for k in ("属于", "是创新")):
        return "yes"
    return "uncertain"


def judge_innovation(
    competition: str, table: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    判断比赛是否属于创新类（AI 主判断，本地规则兜底）。
    返回 {"is_innovation": "yes"|"no"|"uncertain", "reason": str, "source": "ai"|"local"}。
    """
    table = table or matcher.load_competition_table(
        os.path.join(BASE_DIR, "competition_table.json")
    )
    local = matcher.judge_innovation_local(competition, table)
    known_match = matcher.competition_info(competition, table) is not None
    if not has_api_key():
        reasons = {
            "yes": "该比赛在常见创新类比赛名单中，或名称包含创新/创业/学术科技等关键词。",
            "no": "该比赛名称包含体育、文娱、演讲、考证等非创新类关键词。",
            "unknown": "当前无法确定它是否属于创新类比赛。",
        }
        return {"is_innovation": local, "reason": reasons.get(local, ""), "source": "local"}

    competitions = (table or {}).get("competitions", {}) or {}
    known = []
    for name, info in competitions.items():
        aliases = (info or {}).get("aliases", []) or []
        known.append("%s（别名：%s）" % (name, "、".join(aliases) if aliases else "无"))

    system = (
        "你是创新类比赛判定器。创新类比赛指“创新创业 / 学术科技 / 学科竞赛”类赛事，"
        "例如大创赛、挑战杯、互联网+、三创赛、数学建模、电子设计竞赛、机器人大赛、"
        "服务外包创新创业大赛等。纯文娱、体育竞技、演讲、知识竞答、技能考证等不属于创新类。\n"
        "只输出 JSON：{\"is_innovation\":\"yes|no|uncertain\",\"reason\":\"一句话理由\"}。\n"
        "如果比赛名称明确属于创新类或明确不属于，就给 yes/no；拿不准就给 uncertain。"
    )
    user = (
        "常见创新类比赛名单：%s\n"
        "创新类关键词：%s\n"
        "非创新类关键词：%s\n"
        "待判断比赛名称：%s"
        % (
            "；".join(known),
            "、".join((table or {}).get("innovation_keywords", []) or []),
            "、".join((table or {}).get("non_innovation_keywords", []) or []),
            competition,
        )
    )
    raw = _chat_json(system, user, temperature=0.0)
    if raw.get("_error"):
        return {"is_innovation": local, "reason": "大模型调用失败，已使用本地规则判断。",
                "source": "local"}

    verdict = _verdict_from_value(raw.get("is_innovation"))
    reason = str(raw.get("reason") or "").strip()
    source = "ai"

    # 本地规则与模型冲突时的处理：
    # - 名称明确命中常见创新类比赛名单（不是仅靠关键词），以名单为准；
    # - 明确命中体育/文娱/考证等非创新关键词，以本地规则为准。
    if known_match and verdict == "no":
        verdict = "yes"
        source = "local"
        reason = "该比赛在常见创新类比赛名单中。"
    elif local == "no" and verdict == "yes":
        verdict = "no"
        source = "local"
        reason = "该比赛名称包含体育、文娱、演讲、考证等非创新类关键词。"

    # 模型拿不准但本地规则很确定时，采用本地结论
    if verdict == "uncertain" and local in ("yes", "no"):
        verdict = local
        source = "local"
        reason = reason or "根据本地比赛名单/关键词判断。"
    if verdict == "uncertain" and not reason:
        reason = "暂时无法确定该比赛是否属于创新类，需要向用户确认。"

    return {"is_innovation": verdict, "reason": reason, "source": source}


# ----------------------------------------------------------------------
# 追问话术 / 确认话术
# ----------------------------------------------------------------------
def generate_missing_question(
    missing_labels: List[str], collected: Optional[Dict[str, Any]] = None
) -> str:
    """
    DeepSeek 只负责把代码算出的缺失字段列表翻译成自然中文。
    要求一次问完所有缺失项，不要一轮问一个。
    """
    labels = [str(x) for x in (missing_labels or []) if str(x).strip()]
    if not labels:
        return "信息已经齐全，我现在开始为您匹配队友。"

    if has_api_key():
        system = (
            "你是“创新项目组队匹配智能体”的追问助手。"
            "代码已经算出缺失字段，你只负责用自然、口语化的中文一次性问完，"
            "不要询问列表之外的信息，不要计算匹配度，不要输出 JSON。"
            "语气友好、简洁，可以给一个简短示例。"
            "如果缺失项包含联系方式，必须提醒用户提供微信、QQ 或邮箱中的任意一个。"
        )
        user = (
            "本次缺失字段：%s\n"
            "已收集信息：%s\n"
            "请用一段话一次问完这些缺失项。"
            % ("、".join(labels), json.dumps(collected or {}, ensure_ascii=False))
        )
        text = _chat_text(system, user)
        if text:
            return text

    # 兜底模板
    return (
        "为了帮您匹配队友，还需要您补充以下信息：%s。"
        "您可以直接用一句话告诉我；如果缺失联系方式，请提供微信、QQ 或邮箱中的任意一个。"
        % "、".join(labels)
    )


def generate_range_question(competition: str, comp_range: Dict[str, Any]) -> str:
    """查不到比赛时，给出默认人数区间并请用户确认。"""
    low = comp_range.get("min", 3)
    high = comp_range.get("max", 5)
    return (
        "比赛人数区间表里暂时没有收录「%s」。我先按 %d-%d 人估算，可以吗？"
        "如果不行，请直接告诉我人数区间（例如 3-5）。"
        % (competition, low, high)
    )


def generate_chat_reply(
    user_text: str,
    history: Optional[Iterable[Dict[str, Any]]] = None,
    collected: Optional[Dict[str, Any]] = None,
    table: Optional[Dict[str, Any]] = None,
) -> str:
    """让助手像豆包/DeepSeek 一样自由对话（开放问答、解释、闲聊、连续追问）。

    当用户没有给出明确参赛信息时，由这里生成自然回复，而不是固定引导话术。
    """
    text = str(user_text or "").strip()
    if not text:
        return ""

    table = table or matcher.load_competition_table(
        os.path.join(BASE_DIR, "competition_table.json")
    )
    competitions = list((table or {}).get("competitions", {}).keys())
    history_text = ""
    if history:
        lines = []
        for msg in list(history)[-10:]:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = str(msg.get("content", "")).replace("\n", " ")
            lines.append("%s：%s" % (role, content[:300]))
        history_text = "\n".join(lines)

    if has_api_key():
        system = (
            "你是“创新项目组队匹配智能体”，也是一个可以自由对话的中文 AI 助手，"
            "回答风格自然、亲切、简洁，像豆包/DeepSeek 一样。\n"
            "你可以回答任何开放问题、解释概念、闲聊、连续追问；不要每次都把话题"
            "硬拉回填表，也不要用固定模板。\n"
            "但当你判断用户想找创新类比赛队友、或提到了明确比赛名称时，可以自然地"
            "引导他说出比赛名称，并告诉他本平台可以帮他匹配队友。\n"
            "如果用户问有哪些热门创新类比赛，可以结合名单给出具体推荐。\n"
            "不要编造不在名单里的比赛人数区间；不确定时如实说明。\n"
            "不要输出 JSON，不要提及自己是“信息抽取器”。"
        )
        user = (
            "平台已收录的创新类比赛：%s\n"
            "当前对话已收集的参赛信息：%s\n"
            "最近对话：\n%s\n\n"
            "用户刚刚说：%s\n"
            "请直接给出你的自然回复。"
            % (
                "、".join(competitions),
                json.dumps(collected or {}, ensure_ascii=False),
                history_text or "（无）",
                text,
            )
        )
        reply = _chat_text(system, user, temperature=0.8)
        if reply:
            return reply

    # 没有 Key 或调用失败时的兜底：按话题给出不同回复，避免固定复读同一句
    lowered = text.lower()
    names = [
        "中国国际大学生创新大赛（原互联网+）",
        "“挑战杯”中国大学生创业计划竞赛",
        "全国大学生数学建模竞赛",
        "全国大学生电子设计竞赛",
        "全国大学生机器人大赛 RoboMaster",
        "三创赛",
    ]

    if len(text) <= 12 and (
        any(k in text for k in ("你好", "您好", "嗨", "哈喽", "在吗"))
        or lowered.strip() in ("hi", "hello", "hey", "hi there", "hello there")
    ):
        return (
            "你好呀～我在。想了解创新类比赛、聊聊组队思路，或者只是随便聊两句都可以。"
            "如果你想找队友，直接告诉我比赛名称就行。"
        )
    if any(k in text for k in ("谢谢", "多谢", "感谢", "辛苦了", "感谢你")):
        return "不客气～需要我帮你推荐比赛或找队友时，随时叫我。"
    if any(k in text for k in ("你是谁", "你叫什么", "你能做什么", "你会什么", "你有什么功能", "介绍一下你")):
        return (
            "我是“创新项目组队匹配智能体”。我可以帮你：\n"
            "1. 推荐和介绍创新类比赛；\n"
            "2. 根据你的技能、时间和需求匹配队友；\n"
            "3. 回答比赛相关的问题，也可以陪你聊聊组队想法。"
        )
    if any(k in text for k in ("找队友", "组队", "匹配队友", "队友", "找人")):
        return (
            "可以呀～你想参加哪一场比赛？告诉我比赛名称或简称，"
            "再说说你已经掌握的技能和可组队的时间，我就能帮你匹配志同道合的队友。"
        )
    if any(k in text for k in ("热门", "哪些比赛", "推荐", "有什么比赛", "比赛", "赛事", "竞赛")):
        return (
            "比较热门的创新类比赛有：%s。\n"
            "你想参加哪一场？直接告诉我比赛名称或简称，我就可以帮你匹配队友。"
            % "、".join(names)
        )

    knowledge = [
        (
            ("大创", "互联网+", "创新大赛"),
            "大创赛通常指中国国际大学生创新大赛（原“互联网+”），"
            "是一项面向大学生的创新创业类赛事，既看重项目创意，也看重落地和团队协作。"
            "你想参加哪一届、现在队伍有几个人？",
        ),
        (
            ("挑战杯",),
            "“挑战杯”分为课外学术科技作品竞赛和创业计划竞赛，"
            "前者偏学术科技作品，后者偏创业项目和商业计划。你更想参加哪一个方向？",
        ),
        (
            ("数学建模", "数模"),
            "全国大学生数学建模竞赛通常 3 人一队，需要在几天内完成建模、求解和论文写作。"
            "常见分工是建模、编程和论文写作，你想找哪个方向的队友？",
        ),
        (
            ("电子设计", "电赛"),
            "全国大学生电子设计竞赛偏硬件与嵌入式开发，通常需要电路设计、程序开发和报告撰写能力。"
            "你目前擅长哪一块？",
        ),
        (
            ("机器人", "robomaster", "机甲"),
            "RoboMaster 等机器人比赛通常需要机械、电控、算法和运营等多个方向协作，"
            "团队规模一般比普通学科竞赛更大。你想参加哪一类机器人比赛？",
        ),
        (
            ("三创赛",),
            "三创赛即全国大学生电子商务“创新、创意及创业”挑战赛，"
            "偏创新创业项目，适合有商业策划、产品设计和开发能力的同学组队。",
        ),
    ]
    for keys, answer in knowledge:
        if any(k in text for k in keys):
            return answer

    if any(k in text for k in ("怎么", "如何", "方法", "技巧", "建议")):
        return (
            "关于「%s」，可以先拆成目标、现有资源和下一步行动三部分来想。"
            "如果你说的是组队或比赛准备，告诉我具体比赛和你现在的进度，我可以给出更具体的建议。"
            % text[:40]
        )
    if any(k in text for k in ("为什么", "是什么", "什么是", "区别", "解释", "介绍")):
        return (
            "你问的是「%s」。我这边暂时没能连上在线大模型做深入解释，"
            "不过你可以继续补充背景，或者直接说你想参加的比赛，我先把组队这件事帮你梳理清楚。"
            % text[:40]
        )
    if len(text) <= 24:
        return (
            "我在听～你刚刚说「%s」。想继续聊这个话题，还是想让我帮你推荐比赛、找队友？"
            % text
        )
    return (
        "我听到你说「%s」了～你可以继续展开，也可以直接告诉我你想参加的比赛名称，"
        "我来帮你匹配队友。" % text[:40]
    )


def parse_yes_no(text: str) -> Optional[bool]:
    """解析用户的是/否回答。"""
    t = str(text or "").strip().lower()
    if not t:
        return None
    negatives = ("不是", "不属于", "不可以", "不行", "不对", "不要", "否", "不能",
                 "没有", "不需要", "无", "no", "nope", "n")
    positives = ("是", "可以", "好的", "好", "对", "确认", "属于", "行", "嗯",
                 "yes", "ok", "y", "没问题", "正确")
    if any(k in t for k in negatives):
        return False
    if any(k in t for k in positives):
        return True
    return None


def is_decline_optional(text: str) -> bool:
    """判断用户是否在回答可选信息时说“没有/不确定”。"""
    t = str(text or "").strip()
    if not t:
        return False
    if len(t) <= 12 and any(k in t for k in ("没有", "无", "不需要", "不用", "不确定", "不知道", "无所谓")):
        return True
    return t in ("没有", "没有了", "没有需求", "暂无", "都行", "随便")


def parse_range_text(text: str) -> Optional[Dict[str, int]]:
    """从“3-5”“3到5人”里解析人数区间。"""
    t = str(text or "")
    m = re.search(r"(\d+)\s*(?:-|~|～|到|至|—|－)\s*(\d+)", t)
    if not m:
        return None
    low, high = int(m.group(1)), int(m.group(2))
    if low > high:
        low, high = high, low
    if low < 1:
        return None
    return {"min": low, "max": high}
