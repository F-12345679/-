# -*- coding: utf-8 -*-
"""
matcher.py — 匹配算法核心（完全由 Python 实现，绝不调用大模型算分）

算法分两步：
第一步 硬约束（一票否决）：
  - 双方意向都是创新类比赛；
  - 可组队时间段有交集；
  - 人数满足：下限 <= 我方已有几人 + 对方已有几人 <= 上限（区间按比赛动态查表）；
  - 对方意向状态为 matching 且未过期（调用方传入候选池时已过滤，这里再兜底）。
第二步 软约束加权：
  - 技能匹配 45%：Jaccard = |我方希望队友擅长 ∩ 对方掌握技能| / |并集|
  - 学校/地域 10%
  - 空闲时间 5%、兴趣方向 5%、性别偏好 3%、
    希望担任角色 2%、是否已有创意 2%、是否愿意线下见面 2%（未填则该维度不计入）
  - 匹配分 = Σ(得分 × 权重) / Σ(实际计入的权重) × 100，归一化到 0-100
  - 按匹配分降序排序；推荐列表 = 分数 > 0，备选列表 = 通过硬约束但分数 = 0

数据结构：
  - 用户/意向来自 data_pool 的哈希表；
  - 候选人检索使用“技能 -> 意向集合”的倒排索引；
  - 排序使用 Python 内置 sorted（Timsort），复杂度 O(n log n)。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from data_pool import dedupe_list, parse_date, to_bool, to_int

# ----------------------------------------------------------------------
# 字段清单（必填 9 项 / 可选 6 项）
# ----------------------------------------------------------------------
REQUIRED_FIELDS: List[Tuple[str, str]] = [
    ("competition", "比赛名称"),
    ("nickname", "昵称"),
    ("contact", "联系方式（微信/QQ/邮箱任一）"),
    ("major", "专业"),
    ("skills", "掌握技术"),
    ("wanted_skills", "希望队友擅长"),
    ("team_size", "已有几人"),
    ("school", "学校"),
    ("available_period", "可组队时间段"),
]

OPTIONAL_FIELDS: List[Tuple[str, str]] = [
    ("free_time", "空闲时间"),
    ("gender_preference", "性别偏好"),
    ("interests", "兴趣方向"),
    ("preferred_role", "希望担任角色"),
    ("has_idea", "是否已有项目创意"),
    ("willing_offline", "是否愿意线下见面"),
]

# 软约束权重（总和 0.85）+ 保底 15 分
WEIGHTS: Dict[str, float] = {
    "技能匹配": 0.25,
    "学校/地域": 0.20,
    "空闲时间": 0.10,
    "兴趣方向": 0.10,
    "性别偏好": 0.05,
    "希望担任角色": 0.05,
    "是否已有创意": 0.05,
    "是否愿意线下见面": 0.05,
}

BASE_SCORE = 15.0

DEFAULT_TABLE: Dict[str, Any] = {
    "competitions": {},
    "default_range": {"min": 3, "max": 5},
    "innovation_keywords": ["创新", "创业", "学术", "科技", "建模", "机器人"],
    "non_innovation_keywords": ["篮球", "足球", "演讲", "歌唱", "电竞", "考证"],
}

# 用于判断“同地”的省级/直辖市/主要城市关键词
REGION_KEYWORDS = [
    "北京", "上海", "天津", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "宁夏", "新疆", "西藏", "内蒙古", "香港", "澳门", "台湾",
    "长沙", "广州", "深圳", "武汉", "成都", "南京", "杭州", "西安",
    "哈尔滨", "长春", "沈阳", "大连", "青岛", "济南", "郑州", "合肥",
    "福州", "厦门", "南昌", "昆明", "贵阳", "南宁", "兰州", "石家庄",
    "太原", "呼和浩特", "海口", "银川", "西宁", "拉萨", "苏州", "无锡",
    "宁波", "温州",
]

# 常见高校 -> 所在省级行政区（用于“同地加分”，解决校名本身不含省名的情况）
UNIVERSITY_REGION = {
    "中南大学": "湖南",
    "湖南大学": "湖南",
    "湖南师范大学": "湖南",
    "湖南第一师范学院": "湖南",
    "国防科技大学": "湖南",
    "长沙理工大学": "湖南",
    "湖南农业大学": "湖南",
    "中南林业科技大学": "湖南",
    "北京大学": "北京",
    "清华大学": "北京",
    "中国人民大学": "北京",
    "北京航空航天大学": "北京",
    "北京理工大学": "北京",
    "复旦大学": "上海",
    "上海交通大学": "上海",
    "同济大学": "上海",
    "浙江大学": "浙江",
    "南京大学": "江苏",
    "东南大学": "江苏",
    "武汉大学": "湖北",
    "华中科技大学": "湖北",
    "四川大学": "四川",
    "电子科技大学": "四川",
    "西安交通大学": "陕西",
    "西北工业大学": "陕西",
    "中山大学": "广东",
    "华南理工大学": "广东",
    "哈尔滨工业大学": "黑龙江",
    "吉林大学": "吉林",
    "大连理工大学": "辽宁",
    "山东大学": "山东",
    "厦门大学": "福建",
    "重庆大学": "重庆",
    "天津大学": "天津",
    "南开大学": "天津",
}


def _regions_of(school: str) -> Set[str]:
    """抽取学校所属地域：直接包含省份/城市关键词，或通过常见高校映射。"""
    text = str(school or "")
    regions: Set[str] = set()
    for region in REGION_KEYWORDS:
        if region in text:
            # 城市归一到省级会丢失粒度，这里统一用“包含同一关键词”判断
            regions.add(region)
    for uni, region in UNIVERSITY_REGION.items():
        if uni in text:
            regions.add(region)
    return regions


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------
def load_competition_table(path: Optional[str] = None) -> Dict[str, Any]:
    """读取比赛人数区间表；文件不存在时返回内置默认表。"""
    if not path:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "competition_table.json")
    if not os.path.exists(path):
        return dict(DEFAULT_TABLE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_TABLE)
        data.setdefault("competitions", {})
        data.setdefault("default_range", {"min": 3, "max": 5})
        data.setdefault("innovation_keywords", [])
        data.setdefault("non_innovation_keywords", [])
        return data
    except (OSError, ValueError):
        return dict(DEFAULT_TABLE)


def _norm_name(name: Any) -> str:
    """比赛名称归一化：去空格、全角加号、常见前缀。"""
    text = str(name or "").lower().replace("＋", "+")
    text = re.sub(r"\s+", "", text)
    for word in ("大学生", "全国", "中国国际", "中国"):
        text = text.replace(word, "")
    return text


COMPETITION_GROUPS: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "大创赛",
        (
            "大创赛",
            "大创",
            "大学生创新创业训练计划",
            "大学生创新创业训练项目",
            "大创项目",
            "创新创业训练计划",
        ),
    ),
    (
        "中国国际大学生创新创业大赛",
        (
            "互联网+",
            "互联网＋",
            "国创",
            "国创赛",
            "中国国际大学生创新创业大赛",
            "中国国际大学生创新大赛",
            "中国国际大学生创新大赛2026",
            "中国国际大学生创新大赛（2026）",
            "中国国际互联网+大学生创新创业大赛",
            "互联网+大学生创新创业大赛",
            "互联网+大赛",
            "互联网+比赛",
            "互联网加",
        ),
    ),
    (
        "挑战杯",
        (
            "挑战杯",
            "挑战杯中国大学生创业计划竞赛",
            "挑战杯创业计划竞赛",
            "挑战杯大学生创业大赛",
            "小挑",
            "创业计划竞赛",
            "挑战杯课外学术科技作品竞赛",
            "挑战杯课外学术科技作品大赛",
            "课外学术科技作品竞赛",
            "大挑",
            "学术科技作品竞赛",
        ),
    ),
]

PREFERRED_CANONICAL_TABLE = {
    "大创赛": "中国国际大学生创新大赛（2026）",
    "中国国际大学生创新创业大赛": "中国国际大学生创新大赛（2026）",
    "挑战杯": "“挑战杯”中国大学生创业计划竞赛",
}


def _canonical_key(value: Any) -> str:
    text = str(value or "").lower().replace("＋", "+")
    text = re.sub(r"[\s“”\"'（）()【】\[\]、，,·\-—_/]+", "", text)
    return text


_CANONICAL_LOOKUP: Dict[str, str] = {}
for _canonical, _aliases in COMPETITION_GROUPS:
    for _alias in _aliases:
        _CANONICAL_LOOKUP[_canonical_key(_alias)] = _canonical


def normalize_competition_name(name: Any, table: Optional[Dict[str, Any]] = None) -> str:
    """把简称/别名归一化到统一比赛名；未收录名称保持原样。"""
    key = _canonical_key(name)
    if not key:
        return ""
    if key in _CANONICAL_LOOKUP:
        return _CANONICAL_LOOKUP[key]
    for alias_key, canonical in _CANONICAL_LOOKUP.items():
        if len(alias_key) >= 4 and (alias_key in key or key in alias_key):
            return canonical
    if table:
        info = competition_info(name, table)
        if info is not None:
            return info[0]
    return str(name or "").strip()


def _norm_token(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def missing_required(fields: Dict[str, Any]) -> List[Tuple[str, str]]:
    """对比必填清单，返回缺失项 [(key, 中文名), ...]。判断逻辑全部在代码里。"""
    missing: List[Tuple[str, str]] = []
    for key, label in REQUIRED_FIELDS:
        value = (fields or {}).get(key)
        if key in ("skills", "wanted_skills"):
            if not dedupe_list(value):
                missing.append((key, label))
        elif key == "team_size":
            number = to_int(value)
            if number is None or number < 1:
                missing.append((key, label))
        elif key == "available_period":
            if isinstance(value, dict):
                if not str(value.get("raw") or "").strip():
                    missing.append((key, label))
            elif _is_empty(value):
                missing.append((key, label))
        else:
            if _is_empty(value):
                missing.append((key, label))
    return missing


def detect_contact_type(contact: Any) -> str:
    """识别联系方式类型，便于展示与校验。"""
    text = str(contact or "").lower()
    if not text.strip():
        return ""
    if "@" in text or "邮箱" in text or "email" in text:
        return "邮箱"
    if "qq" in text:
        return "QQ"
    if text.isdigit() and 5 <= len(text) <= 12:
        return "QQ"
    if "微信" in text or "wx" in text or "wechat" in text:
        return "微信"
    return "联系方式"


def jaccard(a: Iterable[Any], b: Iterable[Any]) -> float:
    """
    Jaccard 相似度 = |A ∩ B| / |A ∪ B|。
    用 set 自动去重，时间复杂度 O(|A| + |B|)。
    """
    set_a = {_norm_token(x) for x in (a or []) if _norm_token(x)}
    set_b = {_norm_token(x) for x in (b or []) if _norm_token(x)}
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


# ----------------------------------------------------------------------
# 比赛区间 / 创新类判断
# ----------------------------------------------------------------------
def competition_info(name: Any, table: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """在比赛表里查名称（支持别名/包含关系）。
    返回 (标准比赛名, 配置) 或 None。
    """
    if not name:
        return None
    competitions = (table or {}).get("competitions", {}) or {}
    raw_name = str(name or "").strip()
    preferred = PREFERRED_CANONICAL_TABLE.get(raw_name)
    if preferred and preferred in competitions:
        return preferred, competitions[preferred]
    target = _norm_name(name)

    # 1) 精确/别名匹配
    for key, info in competitions.items():
        candidates = [key] + list((info or {}).get("aliases", []) or [])
        for cand in candidates:
            if target == _norm_name(cand):
                return key, info
    # 2) 包含匹配（例如“大创比赛”包含别名“大创”）
    if len(target) >= 2:
        for key, info in competitions.items():
            candidates = [key] + list((info or {}).get("aliases", []) or [])
            for cand in candidates:
                norm_cand = _norm_name(cand)
                # “创新大赛”这类过短且过于通用的别名只做精确匹配，
                # 避免把“火星创新大赛”等未收录比赛误判为已知比赛。
                generic_alias = len(norm_cand) <= 4 and norm_cand.endswith("大赛")
                if (
                    len(norm_cand) >= 2
                    and (
                        target in norm_cand
                        or (norm_cand in target and not generic_alias)
                    )
                ):
                    return key, info
    return None


def get_competition_range(
    name: Any,
    table: Dict[str, Any],
    stored_range: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    取比赛人数区间：
    1) 用户/AI 已确认过的 stored_range 优先；
    2) 再查比赛表；
    3) 查不到用 default_range，并标记 source=default（界面需让用户确认）。
    """
    if isinstance(stored_range, dict):
        low = to_int(stored_range.get("min"))
        high = to_int(stored_range.get("max"))
        if low and high and 1 <= low <= high:
            return {"min": low, "max": high, "source": "confirmed",
                    "name": name, "matched_name": name}
    canonical = normalize_competition_name(name, table)
    if canonical == "挑战杯":
        return {"min": 2, "max": 15, "source": "table",
                "name": name, "matched_name": "挑战杯"}
    info = competition_info(canonical or name, table)
    if info is not None:
        matched_name, cfg = info
        low = to_int(cfg.get("min"))
        high = to_int(cfg.get("max"))
        if low and high and 1 <= low <= high:
            return {"min": low, "max": high, "source": "table",
                    "name": name, "matched_name": matched_name}
    default = (table or {}).get("default_range", {}) or {}
    low = to_int(default.get("min")) or 3
    high = to_int(default.get("max")) or 5
    if low > high:
        low, high = high, low
    return {"min": low, "max": high, "source": "default",
            "name": name, "matched_name": None}


def judge_innovation_local(name: Any, table: Dict[str, Any]) -> str:
    """
    本地辅助判断（大模型不可用时的兜底）：
    返回 "yes" / "no" / "unknown"。
    正式流程优先让 DeepSeek 判断，本函数只做预筛和降级。
    """
    info = competition_info(name, table)
    if info is not None:
        cfg = info[1] or {}
        if cfg.get("innovation") is True:
            return "yes"
        if cfg.get("innovation") is False:
            return "no"

    text = str(name or "")
    if not text.strip():
        return "unknown"
    for kw in (table or {}).get("non_innovation_keywords", []) or []:
        if kw and kw in text:
            return "no"
    for kw in (table or {}).get("innovation_keywords", []) or []:
        if kw and kw in text:
            return "yes"
    return "unknown"


# ----------------------------------------------------------------------
# 时间段 / 学校 / 可选维度
# ----------------------------------------------------------------------
def _period_parts(period: Any) -> Tuple[Optional[date], Optional[date], str]:
    if isinstance(period, dict):
        start = parse_date(period.get("start_date"))
        end = parse_date(period.get("end_date"))
        raw = str(period.get("raw") or "").strip()
    else:
        start = None
        end = None
        raw = str(period or "").strip()
    if start and end and start > end:
        start, end = end, start
    return start, end, raw


def periods_overlap(period_a: Any, period_b: Any) -> bool:
    """
    判断两个可组队时间段是否有交集。
    优先用解析出的日期区间；没有日期时用文本/关键词兜底。
    日期区间交集判断复杂度 O(1)。
    """
    start_a, end_a, raw_a = _period_parts(period_a)
    start_b, end_b, raw_b = _period_parts(period_b)

    # 明确写了不限/长期/全年，直接视为有交集
    for raw in (raw_a, raw_b):
        if any(k in raw for k in ("不限", "长期", "全年", "随时", "都可以")):
            return True

    # 双方都有日期信息
    if (start_a or end_a) and (start_b or end_b):
        low_a = start_a or date.min
        low_b = start_b or date.min
        high_a = end_a or date.max
        high_b = end_b or date.max
        return max(low_a, low_b) <= min(high_a, high_b)

    # 文本完全相同
    if raw_a and raw_a == raw_b:
        return True

    # 常见同义时间段
    groups = [
        ("这个学期", "本学期", "这学期", "这一学期", "当前学期"),
        ("寒假", "假期"),
        ("暑假",),
        ("上半年", "春季", "这半学期"),
        ("下半年", "秋季"),
        ("接下来三个月", "未来三个月", "三个月内", "近期"),
    ]
    for group in groups:
        if any(k in raw_a for k in group) and any(k in raw_b for k in group):
            return True

    # 文本无法解析时放行，避免把真实可组队的人误杀；界面仍会展示原文供人工判断
    return True


def school_score(school_a: Any, school_b: Any) -> float:
    """
    学校/地域得分：
    - 同校：1.0
    - 学校名互相包含（如“湖南一师”与“湖南第一师范学院”难以直接包含，靠地域兜底）：0.8
    - 同省/直辖市：0.6
    - 其他：0.0
    """
    a = str(school_a or "").strip().lower().replace(" ", "")
    b = str(school_b or "").strip().lower().replace(" ", "")
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    if _regions_of(a) & _regions_of(b):
        return 0.6
    return 0.0


def _list_score(value_a: Any, value_b: Any) -> Optional[float]:
    """列表型可选维度：任一方未填返回 None（不计入权重）。"""
    a = dedupe_list(value_a)
    b = dedupe_list(value_b)
    if not a or not b:
        return None
    return jaccard(a, b)


def _gender_score(pref_a: Any, pref_b: Any) -> Optional[float]:
    """
    性别偏好：双方都填了才计入。
    任一方写“不限”视为兼容；否则偏好一致得 1 分。
    """
    a = str(pref_a or "").strip()
    b = str(pref_b or "").strip()
    if not a or not b:
        return None
    if "不限" in a or "不限" in b or "都可以" in a or "都可以" in b:
        return 1.0
    return 1.0 if a == b else 0.0


def _role_score(role_a: Any, role_b: Any) -> Optional[float]:
    """
    希望担任角色：双方都填了才计入。
    角色不同视为互补（1.0），完全相同视为角色冲突（0.0）。
    """
    a = str(role_a or "").strip().lower()
    b = str(role_b or "").strip().lower()
    if not a or not b:
        return None
    return 0.0 if a == b else 1.0


def _idea_score(has_idea_a: Any, has_idea_b: Any) -> Optional[float]:
    """
    是否已有创意：任一方未填则不计入。
    - 对方已有创意：1.0（能带来项目）
    - 我方有、对方没有：0.5（我方提供创意）
    - 双方都没有：0.0
    """
    a = to_bool(has_idea_a)
    b = to_bool(has_idea_b)
    if a is None or b is None:
        return None
    if b is True:
        return 1.0
    if a is True:
        return 0.5
    return 0.0


def _bool_score(value_a: Any, value_b: Any) -> Optional[float]:
    """布尔型可选维度：双方都填了且一致得 1 分。"""
    a = to_bool(value_a)
    b = to_bool(value_b)
    if a is None or b is None:
        return None
    return 1.0 if a == b else 0.0


def score_pair(
    host_intent: Dict[str, Any],
    host_user: Dict[str, Any],
    cand_intent: Dict[str, Any],
    cand_user: Dict[str, Any],
) -> Dict[str, Any]:
    """
    计算两个“意向”的匹配分（15-100）。
    硬约束通过后保底 15 分；其余 85 分按软约束权重计算。
    未填的可选维度为 None，不计入分母。
    """
    skill = jaccard(
        host_intent.get("wanted_skills", []),
        cand_intent.get("skills", []),
    )
    school = school_score(host_user.get("school"), cand_user.get("school"))
    free_time = _list_score(host_user.get("free_time"), cand_user.get("free_time"))
    interests = _list_score(host_user.get("interests"), cand_user.get("interests"))
    gender = _gender_score(
        host_user.get("gender_preference"), cand_user.get("gender_preference")
    )
    role = _role_score(
        host_intent.get("preferred_role"), cand_intent.get("preferred_role")
    )
    idea = _idea_score(host_intent.get("has_idea"), cand_intent.get("has_idea"))
    offline = _bool_score(
        host_intent.get("willing_offline"), cand_intent.get("willing_offline")
    )

    dims: List[Tuple[str, float, Optional[float]]] = [
        ("技能匹配", WEIGHTS["技能匹配"], skill),
        ("学校/地域", WEIGHTS["学校/地域"], school),
        ("空闲时间", WEIGHTS["空闲时间"], free_time),
        ("兴趣方向", WEIGHTS["兴趣方向"], interests),
        ("性别偏好", WEIGHTS["性别偏好"], gender),
        ("希望担任角色", WEIGHTS["希望担任角色"], role),
        ("是否已有创意", WEIGHTS["是否已有创意"], idea),
        ("是否愿意线下见面", WEIGHTS["是否愿意线下见面"], offline),
    ]

    applied_weight = 0.0
    weighted_sum = 0.0
    breakdown: Dict[str, Dict[str, Any]] = {}
    for label, weight, value in dims:
        applied = value is not None
        if applied:
            applied_weight += weight
            weighted_sum += weight * float(value)
        breakdown[label] = {
            "score": round(float(value), 4) if applied else None,
            "weight": weight,
            "weighted": round(weight * float(value), 4) if applied else None,
            "applied": applied,
        }

    if applied_weight <= 0:
        ratio = 0.0
    else:
        ratio = weighted_sum / applied_weight
    score = BASE_SCORE + ratio * (100.0 - BASE_SCORE)
    return {
        "score": round(score, 1),
        "breakdown": breakdown,
        "applied_weight": round(applied_weight, 4),
        "base_score": BASE_SCORE,
    }


# ----------------------------------------------------------------------
# 硬约束 / 候选检索
# ----------------------------------------------------------------------
def _intent_innovation(intent: Dict[str, Any], table: Dict[str, Any]) -> bool:
    flag = intent.get("is_innovation")
    if flag is not None:
        return bool(flag)
    return judge_innovation_local(intent.get("competition", ""), table) != "no"


def constraint_checks(
    host_intent: Dict[str, Any],
    host_user: Dict[str, Any],
    cand_intent: Dict[str, Any],
    cand_user: Dict[str, Any],
    table: Dict[str, Any],
    comp_range: Dict[str, Any],
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """逐条检查硬约束，返回可展示给用户的检查结果。"""
    today = today or date.today()
    checks: List[Dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append(
            {"name": name, "passed": bool(passed), "detail": str(detail or "")}
        )

    add(
        "不能匹配自己",
        cand_intent.get("intent_id") != host_intent.get("intent_id"),
        "当前意向：%s" % host_intent.get("intent_id", ""),
    )
    add(
        "不能匹配同一用户",
        cand_intent.get("user_id") != host_intent.get("user_id"),
        "当前用户：%s" % (host_user.get("nickname") or host_intent.get("user_id", "")),
    )
    status = cand_intent.get("status")
    add("对方状态必须是 matching", status == "matching", "当前状态：%s" % status)

    period = cand_intent.get("available_period") or {}
    end = parse_date(period.get("end_date")) if isinstance(period, dict) else None
    expired = end is not None and end < today
    add(
        "对方意向未过期",
        not expired,
        "截止日期：%s" % (end.isoformat() if end else "未填写"),
    )

    host_innov = _intent_innovation(host_intent, table)
    cand_innov = _intent_innovation(cand_intent, table)
    add(
        "双方都是创新类比赛",
        host_innov and cand_innov,
        "我方：%s；对方：%s" % ("是" if host_innov else "否", "是" if cand_innov else "否"),
    )

    host_comp = normalize_competition_name(host_intent.get("competition", ""), table)
    cand_comp = normalize_competition_name(cand_intent.get("competition", ""), table)
    add(
        "比赛名称归一化后一致",
        bool(host_comp) and bool(cand_comp) and host_comp == cand_comp,
        "我方：%s；对方：%s"
        % (host_comp or "未填写", cand_comp or "未填写"),
    )

    add(
        "可组队时间段有交集",
        periods_overlap(
            host_intent.get("available_period"), cand_intent.get("available_period")
        ),
        "我方：%s；对方：%s"
        % (
            _period_parts(host_intent.get("available_period"))[2] or "未填写",
            _period_parts(cand_intent.get("available_period"))[2] or "未填写",
        ),
    )

    host_size = to_int(host_intent.get("team_size")) or 1
    cand_size = to_int(cand_intent.get("team_size")) or 1
    combined = host_size + cand_size
    low = int(comp_range.get("min", 3))
    high = int(comp_range.get("max", 5))
    add(
        "合并人数不超过上限",
        combined <= high,
        "合并后 %d 人，上限 %d 人" % (combined, high),
    )
    add(
        "最低人数提示",
        True,
        "合并后 %d 人，最低 %d 人%s"
        % (
            combined,
            low,
            "；暂未达到最低人数，可先组队后继续招募" if combined < low else "",
        ),
    )
    return checks


def hard_constraints(
    host_intent: Dict[str, Any],
    host_user: Dict[str, Any],
    cand_intent: Dict[str, Any],
    cand_user: Dict[str, Any],
    table: Dict[str, Any],
    comp_range: Dict[str, Any],
    today: Optional[date] = None,
) -> Tuple[bool, List[str]]:
    """硬约束检查，返回 (是否通过, 不通过原因列表)。"""
    checks = constraint_checks(
        host_intent, host_user, cand_intent, cand_user, table, comp_range, today
    )
    reasons = [
        "%s%s" % (c["name"], ("：" + c["detail"]) if c.get("detail") else "")
        for c in checks
        if not c["passed"]
    ]
    return (not reasons, reasons)


def _candidate_item(
    cand_intent: Dict[str, Any],
    cand_user: Dict[str, Any],
    score_data: Dict[str, Any],
    host_size: int,
    in_index: bool,
) -> Dict[str, Any]:
    period = cand_intent.get("available_period") or {}
    item = {
        "intent_id": cand_intent.get("intent_id"),
        "user_id": cand_intent.get("user_id"),
        "nickname": cand_user.get("nickname", ""),
        "contact": cand_user.get("contact", ""),
        "contact_type": cand_user.get("contact_type", ""),
        "school": cand_user.get("school", ""),
        "major": cand_user.get("major", ""),
        "competition": cand_intent.get("competition", ""),
        "skills": list(cand_intent.get("skills", [])),
        "wanted_skills": list(cand_intent.get("wanted_skills", [])),
        "team_size": to_int(cand_intent.get("team_size")) or 1,
        "combined_size": host_size + (to_int(cand_intent.get("team_size")) or 1),
        "available_period": period,
        "free_time": list(cand_user.get("free_time", [])),
        "interests": list(cand_user.get("interests", [])),
        "gender_preference": cand_user.get("gender_preference", ""),
        "preferred_role": cand_intent.get("preferred_role", ""),
        "has_idea": cand_intent.get("has_idea"),
        "willing_offline": cand_intent.get("willing_offline"),
        "score": score_data["score"],
        "breakdown": score_data["breakdown"],
        "applied_weight": score_data.get("applied_weight", 0.0),
        "in_index": in_index,
    }
    intro_parts = []
    if item["school"]:
        intro_parts.append(item["school"])
    if item["major"]:
        intro_parts.append(item["major"])
    intro_parts.append("已有%d人" % item["team_size"])
    if item["skills"]:
        intro_parts.append("擅长：" + "、".join(item["skills"][:5]))
    if item["wanted_skills"]:
        intro_parts.append("希望队友擅长：" + "、".join(item["wanted_skills"][:5]))
    raw = (period or {}).get("raw") if isinstance(period, dict) else ""
    if raw:
        intro_parts.append("可组队：" + str(raw))
    item["intro"] = " · ".join(intro_parts)
    return item


def find_candidates(
    host_intent: Dict[str, Any],
    host_user: Dict[str, Any],
    candidate_intents: List[Dict[str, Any]],
    users: Dict[str, Dict[str, Any]],
    table: Dict[str, Any],
    competition_range: Optional[Dict[str, Any]] = None,
    now: Optional[date] = None,
    inverted_index: Optional[Dict[str, Set[str]]] = None,
) -> Dict[str, Any]:
    """
    从候选意向中筛出匹配结果。

    参数 candidate_intents 由 data_pool.list_matching_intents() 提供；
    inverted_index 由 data_pool.build_inverted_index() 提供（可选）。

    检索策略：
      1. 用倒排索引按“我方希望队友擅长”的技能取候选（主候选，命中技能）；
      2. 为保证“备选列表”完整，再线性扫描其余通过硬约束的意向；
      3. 全部打分后按分数降序排序，拆成 recommended / alternatives。
    复杂度：硬约束 + 打分 O(n)，排序 O(n log n)。
    """
    today = now or date.today()
    comp_range = competition_range or get_competition_range(
        host_intent.get("competition", ""), table,
        host_intent.get("competition_range"),
    )
    host_size = to_int(host_intent.get("team_size")) or 1
    needed = max(0, int(comp_range.get("min", 3)) - host_size)

    # 倒排索引：技能 -> 意向集合
    if inverted_index is None:
        inverted_index = {}
        for cand in candidate_intents:
            for skill in cand.get("skills", []) or []:
                key = _norm_token(skill)
                if key:
                    inverted_index.setdefault(key, set()).add(cand.get("intent_id"))
    index_hits: Set[str] = set()
    for skill in host_intent.get("wanted_skills", []) or []:
        index_hits |= inverted_index.get(_norm_token(skill), set())

    results: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []
    hard_pass = 0
    for cand_intent in candidate_intents:
        if (
            cand_intent.get("intent_id") == host_intent.get("intent_id")
            or cand_intent.get("user_id") == host_intent.get("user_id")
        ):
            continue
        cand_user = users.get(cand_intent.get("user_id", ""))
        if cand_user is None:
            diagnostics.append(
                {
                    "intent_id": cand_intent.get("intent_id"),
                    "user_id": cand_intent.get("user_id"),
                    "nickname": "未知用户",
                    "competition": cand_intent.get("competition", ""),
                    "checks": [
                        {
                            "name": "对方用户资料存在",
                            "passed": False,
                            "detail": "用户表中找不到该用户",
                        }
                    ],
                }
            )
            continue
        checks = constraint_checks(
            host_intent, host_user, cand_intent, cand_user, table, comp_range, today
        )
        if any(not c["passed"] for c in checks):
            diagnostics.append(
                {
                    "intent_id": cand_intent.get("intent_id"),
                    "user_id": cand_intent.get("user_id"),
                    "nickname": cand_user.get("nickname", ""),
                    "competition": cand_intent.get("competition", ""),
                    "checks": checks,
                }
            )
            continue
        hard_pass += 1
        score_data = score_pair(host_intent, host_user, cand_intent, cand_user)
        results.append(
            _candidate_item(
                cand_intent,
                cand_user,
                score_data,
                host_size,
                cand_intent.get("intent_id") in index_hits,
            )
        )

    # 按分数降序；同分时按 user_id / intent_id 稳定排序
    results.sort(key=lambda x: (-float(x["score"]), str(x["user_id"]), str(x["intent_id"])))

    # 同一个用户可能有多条意向，按 user_id 去重，保留分数最高的一条
    deduped: List[Dict[str, Any]] = []
    seen_users: Set[str] = set()
    for item in results:
        uid = str(item.get("user_id") or "")
        if uid in seen_users:
            continue
        seen_users.add(uid)
        deduped.append(item)
    results = deduped

    recommended = [x for x in results if float(x["score"]) > 0]
    alternatives = [x for x in results if float(x["score"]) <= 0]

    best_diagnostics: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    for diag in diagnostics:
        uid = str(diag.get("user_id") or "")
        failed = sum(1 for c in diag.get("checks", []) if not c.get("passed"))
        if uid not in best_diagnostics or failed < best_diagnostics[uid][0]:
            best_diagnostics[uid] = (failed, diag)
    diagnostics = [item[1] for item in best_diagnostics.values()]

    return {
        "recommended": recommended,
        "alternatives": alternatives,
        "diagnostics": diagnostics,
        "needed": needed,
        "range": comp_range,
        "host_team_size": host_size,
        "candidates_checked": len(candidate_intents),
        "hard_pass": hard_pass,
        "index_hits": len(index_hits),
    }
