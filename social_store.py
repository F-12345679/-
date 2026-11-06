# -*- coding: utf-8 -*-
"""social_store.py — 在核心数据层之外扩展的社交数据。

不修改 matcher.py / data_pool.py，不改动原有匹配算法与核心数据表，
只把“好友、群聊、通知、管理员、比赛反馈、组队关系、匹配快照、已读状态”
等扩展数据保存在 app_extra.json 中，供 main.py 使用。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import neon_store
from data_pool import dedupe_list

ISO_FMT = "%Y-%m-%d %H:%M:%S"

_DEFAULT_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">'
    '<circle cx="48" cy="48" r="48" fill="#d9f2e3"/>'
    '<circle cx="48" cy="37" r="16" fill="#ffffff"/>'
    '<path d="M19 83c4-19 16-27 29-27s25 8 29 27" fill="#ffffff"/>'
    "</svg>"
)
DEFAULT_AVATAR = "data:image/svg+xml;base64," + base64.b64encode(
    _DEFAULT_SVG.encode("utf-8")
).decode("ascii")


def now_str() -> str:
    return datetime.now().strftime(ISO_FMT)


def clean_skills(value: Any) -> List[str]:
    """技能口语化清洗：会一点Python -> Python，并去重。

    去掉“会 / 会一点 / 会一些 / 懂 / 懂一点 / 略懂 / 了解 / 掌握 / 熟悉 /
    擅长 / 基本会 / 稍微会”等前缀，按顿号、逗号等分隔后去重。
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，、;；\n\r\t]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            parts.extend(re.split(r"[,，、;；\n\r\t]+", str(item)))
    else:
        parts = [str(value)]

    prefix_re = re.compile(
        r"^\s*(?:会一点|会一些|基本会|稍微会|会点|懂一点|略懂|会|懂|了解|掌握|熟悉|擅长|能)\s*",
        re.IGNORECASE,
    )
    cleaned: List[str] = []
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        previous = None
        while text and text != previous:
            previous = text
            text = prefix_re.sub("", text, count=1).strip()
        text = text.strip(" ：:，,。；;、")
        if text:
            cleaned.append(text)
    return dedupe_list(cleaned)


def build_contact(qq: str = "", wechat: str = "", email: str = "") -> str:
    """把三个联系方式框合并成展示/匹配用字符串。"""
    pieces = []
    if str(qq or "").strip():
        pieces.append("QQ：" + str(qq).strip())
    if str(wechat or "").strip():
        pieces.append("微信：" + str(wechat).strip())
    if str(email or "").strip():
        pieces.append("邮箱：" + str(email).strip())
    return "；".join(pieces)


def contact_type_of(contact: str) -> str:
    if not contact:
        return ""
    text = str(contact).lower()
    if "@" in text or "邮箱" in text:
        return "邮箱"
    if "qq" in text:
        return "QQ"
    if "微信" in text:
        return "微信"
    return "联系方式"


class SocialStore:
    """扩展社交数据存储（JSON 持久化）。"""

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        self.path = os.path.join(self.base_dir, "app_extra.json")
        self.key = neon_store.file_key(self.path)
        self.data: Dict[str, Any] = {}
        self.cloud_read_failed = False
        self._load()

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------
    @staticmethod
    def _default_data() -> Dict[str, Any]:
        return {
            "next_group_seq": 1,
            "next_notification_seq": 1,
            "next_feedback_seq": 1,
            "next_team_seq": 1,
            "next_group_message_seq": 1,
            "next_direct_message_seq": 1,
            "direct_messages": {},
            "admins": [],
            "admin_invites": {},
            "friends": {},
            "groups": {},
            "notifications": {},
            "feedback": {},
            "members": {},
            "team_links": [],
            "snapshots": {},
            "reads": {},
            "min_prompted": {},
            "deleted_users": [],
        }

    def _load(self) -> None:
        default = self._default_data()
        self.cloud_read_failed = False
        if neon_store.is_configured():
            # 云端模式：从 Neon 读整份数据；读取失败时保留当前内存数据
            ok, loaded = neon_store.load_json_checked(self.key, None)
            if not ok:
                self.cloud_read_failed = True
                return
            if not isinstance(loaded, dict):
                loaded = {}
            if not loaded and self.data:
                print(
                    "[social_store] 云端读到空数据，保留当前内存数据 admins=%d"
                    % len(self.data.get("admins") or []),
                    file=sys.stderr,
                )
                return
        else:
            if not os.path.exists(self.path):
                self.data = default
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    loaded = {}
            except (OSError, ValueError):
                loaded = {}
        for key, value in default.items():
            loaded.setdefault(key, value)
        self.data = loaded
        print(
            "[social_store] 加载完成 admins=%d deleted=%d"
            % (
                len(self.data.get("admins") or []),
                len(self.data.get("deleted_users") or []),
            ),
            file=sys.stderr,
        )

    def reload(self) -> bool:
        """强制从 Neon/本地重新全量读取；失败时保留当前内存数据并返回 False。"""
        self._load()
        return not self.cloud_read_failed

    def _save(self) -> None:
        if neon_store.is_configured():
            if self.cloud_read_failed:
                raise RuntimeError("云端数据读取失败，已停止写入以保护现有数据。")
            if neon_store.save_json(self.key, self.data):
                return
            raise RuntimeError("Neon 写入失败，请检查网络或数据库连接后重试。")
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        last_error: Optional[BaseException] = None
        for attempt in range(5):
            tmp = "%s.%d.%d.tmp" % (self.path, os.getpid(), attempt)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return
            except OSError as exc:
                last_error = exc
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                time.sleep(0.12 * (attempt + 1))
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(payload)
        except OSError:
            print("[social_store] 保存失败：%s" % last_error)

    # ------------------------------------------------------------------
    # 管理员
    # ------------------------------------------------------------------
    def is_admin(self, user_id: str) -> bool:
        return bool(user_id) and user_id in (self.data.get("admins") or [])

    def add_admin(self, user_id: str) -> None:
        admins = self.data.setdefault("admins", [])
        if user_id and user_id not in admins:
            admins.append(user_id)
            self._save()

    def remove_admin(self, user_id: str) -> None:
        admins = self.data.setdefault("admins", [])
        if user_id in admins:
            admins.remove(user_id)
            self._save()

    def add_admin_invite(self, target_uid: str, inviter_uid: str) -> None:
        self.data.setdefault("admin_invites", {})[target_uid] = inviter_uid
        self._save()

    def get_admin_inviter(self, target_uid: str) -> Optional[str]:
        return (self.data.get("admin_invites") or {}).get(target_uid)

    def accept_admin_invite(self, target_uid: str) -> Optional[str]:
        inviter = (self.data.get("admin_invites") or {}).pop(target_uid, None)
        self.add_admin(target_uid)
        self._save()
        return inviter

    def reject_admin_invite(self, target_uid: str) -> Optional[str]:
        inviter = (self.data.get("admin_invites") or {}).pop(target_uid, None)
        self._save()
        return inviter

    # ------------------------------------------------------------------
    # 好友
    # ------------------------------------------------------------------
    def add_friend(self, user_a: str, user_b: str) -> None:
        if not user_a or not user_b or user_a == user_b:
            return
        friends = self.data.setdefault("friends", {})
        for a, b in ((user_a, user_b), (user_b, user_a)):
            lst = friends.setdefault(a, [])
            if b not in lst:
                lst.append(b)
        self._save()

    def remove_friend(self, user_a: str, user_b: str) -> None:
        friends = self.data.setdefault("friends", {})
        for a, b in ((user_a, user_b), (user_b, user_a)):
            lst = friends.get(a) or []
            if b in lst:
                lst.remove(b)
        self._save()

    def list_friends(self, user_id: str) -> List[str]:
        return list((self.data.get("friends") or {}).get(user_id, []))

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------
    def notify(
        self,
        user_id: str,
        text: str,
        kind: str = "info",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not user_id:
            return {}
        seq = int(self.data.get("next_notification_seq", 1))
        self.data["next_notification_seq"] = seq + 1
        note = {
            "notification_id": "N%04d" % seq,
            "text": str(text or ""),
            "kind": kind,
            "meta": dict(meta or {}),
            "read": False,
            "created_at": now_str(),
        }
        self.data.setdefault("notifications", {}).setdefault(user_id, []).append(note)
        self._save()
        return note

    def list_notifications(self, user_id: str) -> List[Dict[str, Any]]:
        items = list((self.data.get("notifications") or {}).get(user_id, []))
        return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)

    def unread_notifications(self, user_id: str) -> List[Dict[str, Any]]:
        return [n for n in self.list_notifications(user_id) if not n.get("read")]

    def mark_notification_read(self, notification_id: str, user_id: str) -> None:
        for note in (self.data.get("notifications") or {}).get(user_id, []):
            if note.get("notification_id") == notification_id:
                note["read"] = True
                self._save()
                return

    def mark_all_notifications_read(self, user_id: str) -> None:
        changed = False
        for note in (self.data.get("notifications") or {}).get(user_id, []):
            if not note.get("read"):
                note["read"] = True
                changed = True
        if changed:
            self._save()

    def find_notification(
        self, user_id: str, notification_id: str
    ) -> Optional[Dict[str, Any]]:
        for note in (self.data.get("notifications") or {}).get(user_id, []):
            if note.get("notification_id") == notification_id:
                return note
        return None

    def find_unread_by_kind(
        self, user_id: str, kind: str, meta_key: Optional[str] = None, meta_value: Any = None
    ) -> List[Dict[str, Any]]:
        result = []
        for note in self.list_notifications(user_id):
            if note.get("read") or note.get("kind") != kind:
                continue
            if meta_key is not None:
                if (note.get("meta") or {}).get(meta_key) != meta_value:
                    continue
            result.append(note)
        return result

    # ------------------------------------------------------------------
    # 已读状态（邀请 / 留言 / 历史新增）
    # ------------------------------------------------------------------
    def _read_state(self, user_id: str) -> Dict[str, Any]:
        reads = self.data.setdefault("reads", {})
        state = reads.setdefault(user_id, {})
        state.setdefault("invitations", [])
        state.setdefault("messages", {})
        state.setdefault("groups", {})
        state.setdefault("direct", {})
        state.setdefault("history", {})
        return state

    def mark_invitations_seen(self, user_id: str, invitation_ids: Iterable[str]) -> None:
        state = self._read_state(user_id)
        seen = state.setdefault("invitations", [])
        changed = False
        for iid in invitation_ids or []:
            if iid and iid not in seen:
                seen.append(iid)
                changed = True
        if changed:
            self._save()

    def invitation_seen(self, user_id: str, invitation_id: str) -> bool:
        return invitation_id in (self._read_state(user_id).get("invitations") or [])

    def mark_messages_read(self, user_id: str, pair: str, stamp: str) -> None:
        state = self._read_state(user_id)
        messages = state.setdefault("messages", {})
        if stamp and str(stamp) > str(messages.get(pair, "")):
            messages[pair] = stamp
            self._save()

    def last_message_read(self, user_id: str, pair: str) -> str:
        return str((self._read_state(user_id).get("messages") or {}).get(pair, ""))

    def mark_group_read(self, user_id: str, group_id: str, stamp: str) -> None:
        state = self._read_state(user_id)
        groups = state.setdefault("groups", {})
        if stamp and str(stamp) > str(groups.get(group_id, "")):
            groups[group_id] = stamp
            self._save()

    def last_group_read(self, user_id: str, group_id: str) -> str:
        return str((self._read_state(user_id).get("groups") or {}).get(group_id, ""))

    def mark_history_seen(self, user_id: str, intent_id: str, count: int) -> None:
        state = self._read_state(user_id)
        state.setdefault("history", {})[intent_id] = int(count)
        self._save()

    def history_seen_count(self, user_id: str, intent_id: str) -> int:
        try:
            return int((self._read_state(user_id).get("history") or {}).get(intent_id, 0))
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # 组队关系 / 成员
    # ------------------------------------------------------------------
    def add_member(self, intent_id: str, user_id: str) -> None:
        if not intent_id or not user_id:
            return
        members = self.data.setdefault("members", {})
        lst = members.setdefault(intent_id, [])
        if user_id not in lst:
            lst.append(user_id)
            self._save()

    def remove_member(self, intent_id: str, user_id: str) -> None:
        members = self.data.setdefault("members", {})
        lst = members.get(intent_id) or []
        if user_id in lst:
            lst.remove(user_id)
            self._save()

    def list_members(self, intent_id: str) -> List[str]:
        return list((self.data.get("members") or {}).get(intent_id, []))

    def create_team_link(self, invitation: Dict[str, Any], competition: str) -> None:
        links = self.data.setdefault("team_links", [])
        inv_id = invitation.get("invitation_id")
        for link in links:
            if link.get("invitation_id") == inv_id and link.get("active", True):
                return
        seq = int(self.data.get("next_team_seq", 1))
        self.data["next_team_seq"] = seq + 1
        links.append(
            {
                "link_id": "T%04d" % seq,
                "invitation_id": inv_id,
                "user_a": invitation.get("from_user"),
                "user_b": invitation.get("to_user"),
                "intent_a": invitation.get("from_intent"),
                "intent_b": invitation.get("to_intent"),
                "competition": competition,
                "active": True,
                "created_at": now_str(),
            }
        )
        self._save()

    def find_team_link(self, invitation_id: str) -> Optional[Dict[str, Any]]:
        for link in self.data.get("team_links", []):
            if link.get("invitation_id") == invitation_id and link.get("active", True):
                return link
        return None

    def cancel_team_link(self, invitation_id: str) -> Optional[Dict[str, Any]]:
        link = self.find_team_link(invitation_id)
        if link is not None:
            link["active"] = False
            link["cancelled_at"] = now_str()
            self._save()
        return link

    def list_team_links(self, user_id: str) -> List[Dict[str, Any]]:
        return [
            link
            for link in self.data.get("team_links", [])
            if link.get("active", True) and user_id in (link.get("user_a"), link.get("user_b"))
        ]

    # ------------------------------------------------------------------
    # 最低/最高人数提醒状态
    # ------------------------------------------------------------------
    def is_min_prompted(self, intent_id: str) -> bool:
        return intent_id in (self.data.get("min_prompted") or {})

    def set_min_prompted(self, intent_id: str, value: str = "asked") -> None:
        self.data.setdefault("min_prompted", {})[intent_id] = value
        self._save()

    def clear_min_prompted(self, intent_id: str) -> None:
        if intent_id in (self.data.get("min_prompted") or {}):
            self.data["min_prompted"].pop(intent_id, None)
            self._save()

    # ------------------------------------------------------------------
    # 匹配快照
    # ------------------------------------------------------------------
    def save_snapshot(self, intent_id: str, candidates: List[Dict[str, Any]]) -> None:
        self.data.setdefault("snapshots", {})[intent_id] = {
            "saved_at": now_str(),
            "candidates": list(candidates or []),
        }
        self._save()

    def get_snapshot(self, intent_id: str) -> Dict[str, Any]:
        return dict((self.data.get("snapshots") or {}).get(intent_id) or {})

    # ------------------------------------------------------------------
    # 群聊
    # ------------------------------------------------------------------
    def create_group(
        self,
        name: str,
        competition: str,
        owner_uid: str,
        member_uids: Iterable[str],
    ) -> Dict[str, Any]:
        seq = int(self.data.get("next_group_seq", 1))
        self.data["next_group_seq"] = seq + 1
        members = []
        for uid in [owner_uid] + list(member_uids or []):
            if uid and uid not in members:
                members.append(uid)
        group = {
            "group_id": "G%03d" % seq,
            "name": str(name or "").strip() or "组队群聊",
            "competition": str(competition or "").strip(),
            "owner": owner_uid,
            "members": members,
            "messages": [],
            "created_at": now_str(),
        }
        self.data.setdefault("groups", {})[group["group_id"]] = group
        self._save()
        return group

    def list_groups(self, user_id: str) -> List[Dict[str, Any]]:
        groups = []
        for group in (self.data.get("groups") or {}).values():
            if user_id in (group.get("members") or []):
                groups.append(group)
        return sorted(groups, key=lambda x: x.get("created_at", ""), reverse=True)

    def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        return (self.data.get("groups") or {}).get(group_id)

    def send_group_message(self, group_id: str, from_uid: str, text: str) -> Dict[str, Any]:
        group = self.get_group(group_id)
        if group is None:
            raise KeyError("群聊不存在")
        if from_uid not in (group.get("members") or []):
            raise ValueError("您不在该群聊中")
        text = str(text or "").strip()
        if not text:
            raise ValueError("消息不能为空")
        seq = int(self.data.get("next_group_message_seq", 1))
        self.data["next_group_message_seq"] = seq + 1
        message = {
            "message_id": "GM%04d" % seq,
            "from_user": from_uid,
            "text": text,
            "created_at": now_str(),
        }
        group.setdefault("messages", []).append(message)
        self._save()
        return message

    # ------------------------------------------------------------------
    # 好友单聊（没有已同意邀请时也能直接私聊）
    # ------------------------------------------------------------------
    @staticmethod
    def _direct_key(user_a: str, user_b: str) -> str:
        return "|".join(sorted([str(user_a or ""), str(user_b or "")]))

    def get_direct_messages(self, user_a: str, user_b: str) -> List[Dict[str, Any]]:
        key = self._direct_key(user_a, user_b)
        return list((self.data.get("direct_messages") or {}).get(key, []))

    def send_direct_message(
        self, from_uid: str, to_uid: str, text: str
    ) -> Dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            raise ValueError("消息不能为空")
        if not from_uid or not to_uid or from_uid == to_uid:
            raise ValueError("聊天对象无效")
        seq = int(self.data.get("next_direct_message_seq", 1))
        self.data["next_direct_message_seq"] = seq + 1
        message = {
            "message_id": "DM%04d" % seq,
            "from_user": from_uid,
            "to_user": to_uid,
            "text": text,
            "created_at": now_str(),
        }
        key = self._direct_key(from_uid, to_uid)
        self.data.setdefault("direct_messages", {}).setdefault(key, []).append(message)
        self._save()
        return message

    def mark_direct_read(self, user_id: str, peer_id: str, stamp: str) -> None:
        state = self._read_state(user_id)
        direct = state.setdefault("direct", {})
        key = self._direct_key(user_id, peer_id)
        if stamp and str(stamp) > str(direct.get(key, "")):
            direct[key] = stamp
            self._save()

    def last_direct_read(self, user_id: str, peer_id: str) -> str:
        key = self._direct_key(user_id, peer_id)
        return str((self._read_state(user_id).get("direct") or {}).get(key, ""))

    # ------------------------------------------------------------------
    # 比赛收录反馈
    # ------------------------------------------------------------------
    def submit_feedback(
        self,
        competition: str,
        min_size: int,
        max_size: int,
        submitter_uid: str,
        submitter_name: str,
    ) -> Dict[str, Any]:
        seq = int(self.data.get("next_feedback_seq", 1))
        self.data["next_feedback_seq"] = seq + 1
        feedback = {
            "feedback_id": "F%04d" % seq,
            "competition": str(competition or "").strip(),
            "min": int(min_size),
            "max": int(max_size),
            "submitter_uid": submitter_uid,
            "submitter_name": submitter_name,
            "status": "pending",
            "created_at": now_str(),
            "resolved_at": None,
            "resolved_by": None,
        }
        self.data.setdefault("feedback", {})[feedback["feedback_id"]] = feedback
        self._save()
        return feedback

    def list_feedback(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        items = list((self.data.get("feedback") or {}).values())
        if status is not None:
            items = [x for x in items if x.get("status") == status]
        return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)

    def get_feedback(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        return (self.data.get("feedback") or {}).get(feedback_id)

    def resolve_feedback(self, feedback_id: str, status: str, admin_uid: str) -> None:
        feedback = self.get_feedback(feedback_id)
        if feedback is None:
            return
        feedback["status"] = status
        feedback["resolved_at"] = now_str()
        feedback["resolved_by"] = admin_uid
        self._save()

    # ------------------------------------------------------------------
    # 删除用户时清理扩展数据
    # ------------------------------------------------------------------
    def cleanup_user(self, user_id: str) -> None:
        friends = self.data.setdefault("friends", {})
        friends.pop(user_id, None)
        for uid, lst in list(friends.items()):
            if user_id in lst:
                friends[uid] = [x for x in lst if x != user_id]
        for iid, members in list((self.data.get("members") or {}).items()):
            self.data["members"][iid] = [x for x in members if x != user_id]
        for group in (self.data.get("groups") or {}).values():
            group["members"] = [x for x in (group.get("members") or []) if x != user_id]
        for key in list((self.data.get("direct_messages") or {}).keys()):
            if user_id in key.split("|"):
                self.data["direct_messages"].pop(key, None)
        for link in self.data.get("team_links", []):
            if user_id in (link.get("user_a"), link.get("user_b")):
                link["active"] = False
        admins = self.data.setdefault("admins", [])
        if user_id in admins:
            admins.remove(user_id)
        self.data.setdefault("admin_invites", {}).pop(user_id, None)
        deleted = self.data.setdefault("deleted_users", [])
        if user_id not in deleted:
            deleted.append(user_id)
        self._save()

    def is_deleted(self, user_id: str) -> bool:
        return user_id in (self.data.get("deleted_users") or [])
