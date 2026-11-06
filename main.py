# -*- coding: utf-8 -*-
"""main.py — Streamlit 主程序（翠绿清新版）

页面：
  1. 对话与匹配：登录 -> 识别比赛 -> 表单 -> 确认 -> 匹配结果 -> 历史参赛信息
  2. 邀请与沟通：收到的邀请、微信式聊天、好友列表、群聊
  3. 组队确认与提醒：人数提醒、组队提醒、取消组队、管理员邀请通知
  4. 管理中心（仅管理员）：用户管理、比赛收录反馈

匹配分数、硬约束、排序全部来自 matcher.py；本文件不参与算分。
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import re
from datetime import date, datetime

import streamlit as st
import streamlit.components.v1 as components

import llm_api
import matcher
from data_pool import DataPool, dedupe_list, pair_key
from social_store import (
    DEFAULT_AVATAR,
    SocialStore,
    build_contact,
    clean_skills,
    contact_type_of,
    now_str,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUPER_NICKNAME = "吃货"
SUPER_PHONE = "13627350695"

st.set_page_config(
    page_title="创新项目组队匹配智能体",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ----------------------------------------------------------------------
# 资源 / HTML 小工具
# ----------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _read_asset(path: str) -> bytes:
    if not os.path.exists(path):
        return b""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


@st.cache_data(show_spinner=False)
def _asset_data_uri(path: str, mime: str) -> str:
    data = _read_asset(path)
    if not data:
        return ""
    return "data:%s;base64,%s" % (
        mime,
        base64.b64encode(data).decode("ascii"),
    )


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _avatar_src(user_or_id) -> str:
    if isinstance(user_or_id, dict):
        user = user_or_id
    else:
        user = pool.get_user(str(user_or_id or "")) or {}
    src = str(user.get("avatar") or "").strip()
    return src if src.startswith("data:image/") else DEFAULT_AVATAR


def _avatar_html(user_or_id, size: int = 40) -> str:
    return (
        '<img class="avatar-img" src="%s" style="width:%dpx;height:%dpx" alt="头像"/>'
        % (_esc(_avatar_src(user_or_id)), int(size), int(size))
    )


def _user_name(user_or_id) -> str:
    if isinstance(user_or_id, dict):
        user = user_or_id
    else:
        user = pool.get_user(str(user_or_id or "")) or {}
    return str(user.get("nickname") or "未命名")


def _user_contact(user: dict) -> str:
    pieces = []
    if user.get("qq"):
        pieces.append("QQ：" + str(user.get("qq")))
    if user.get("wechat"):
        pieces.append("微信：" + str(user.get("wechat")))
    if user.get("email"):
        pieces.append("邮箱：" + str(user.get("email")))
    if pieces:
        return "；".join(pieces)
    return str(user.get("contact") or "")


def _get_flash() -> None:
    text = st.session_state.pop("flash_message", None)
    if text:
        st.markdown(
            '<div class="floating-toast">%s</div>' % _esc(text),
            unsafe_allow_html=True,
        )


def _set_flash(text: str) -> None:
    st.session_state.flash_message = str(text or "")


def _bubble_html(
    content: str,
    avatar_src: str,
    name: str,
    stamp: str = "",
    mine: bool = False,
    avatar_size: int = 42,
) -> str:
    meta = _esc(name)
    if stamp:
        meta += ' <span class="bubble-time">%s</span>' % _esc(stamp)
    body = _esc(content).replace("\n", "<br/>")
    return (
        '<div class="bubble-row%s">'
        '<img class="avatar-img" src="%s" style="width:%dpx;height:%dpx" alt="头像"/>'
        '<div class="bubble-body"><div class="bubble-meta">%s</div>'
        '<div class="bubble-text">%s</div></div></div>'
        % (
            " mine" if mine else "",
            _esc(avatar_src),
            int(avatar_size),
            int(avatar_size),
            meta,
            body,
        )
    )


def _html_table(rows) -> str:
    body = "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (_esc(k), _esc(v)) for k, v in rows
    )
    return '<table class="pretty-table">%s</table>' % body


def _plain_text(value) -> str:
    """把可能混入的 HTML 标签去掉，只保留可读文字，避免标题显示成标签字符串。"""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_competition_name(value) -> str:
    fn = getattr(matcher, "normalize_competition_name", None)
    if callable(fn):
        try:
            return fn(value, table)
        except Exception:
            pass
    return _plain_text(value)


def _competition_display(value) -> str:
    text = _plain_text(value)
    if not text:
        return ""
    return _normalize_competition_name(text)


def _period_display(period) -> str:
    """可组队时间段统一展示：原文（截止 真实解析日期）。"""
    if not isinstance(period, dict):
        text = _plain_text(period)
        return text or "未填写"
    raw = _plain_text(period.get("raw") or "")
    end = _plain_text(period.get("end_date") or "")
    if raw and end:
        return "%s（截止 %s）" % (raw, end)
    return raw or (("截止 " + end) if end else "未填写")


def _render_bg_music(height: int = 1, on_light: bool = False) -> None:
    """背景音乐：按钮和音频都直接注入父页面，点击可真正暂停/继续。"""
    uri = _asset_data_uri(os.path.join(BASE_DIR, "assets", "bg_music.mp3"), "audio/mpeg")
    if not uri:
        return
    template = """
    <style>
      html, body { margin: 0; padding: 0; background: transparent; overflow: hidden; }
    </style>
    <script>
    (function () {
      var p = window.parent, d = null;
      try { d = p.document; } catch (e) { return; }
      var storage = null;
      try { storage = p.localStorage; } catch (e) {}
      var audio = p.__bgmAudio;
      try {
        if (!audio) {
          audio = p.__bgmAudio = new p.Audio("__MUSIC__");
          audio.loop = true;
          audio.preload = 'auto';
        }
      } catch (e) { return; }
      var onLight = __ONLIGHT__;
      var btn = p.__bgmBtn;
      if (!btn || !btn.parentNode) {
        btn = d.createElement('button');
        btn.type = 'button';
        btn.id = '__bgmToggleBtn';
        d.body.appendChild(btn);
        p.__bgmBtn = btn;
      }
      btn.style.cssText = 'position:fixed;left:18px;bottom:18px;z-index:2147483000;' +
        'border:1px solid #a7f3d0;border-radius:999px;padding:10px 18px;' +
        'background:rgba(224,250,236,.97);color:#0b7a51;font-size:14px;font-weight:800;' +
        'cursor:pointer;box-shadow:0 8px 22px rgba(6,80,52,.22);' +
        (onLight ? '' : '');
      var want = p.__bgmWant;
      if (typeof want !== 'boolean') {
        try { want = storage ? storage.getItem('bgm_on') !== '0' : true; } catch (e) { want = true; }
      }
      p.__bgmWant = want;
      function paint() {
        btn.textContent = want ? '⏸ 暂停音乐' : '▶ 播放音乐';
        btn.title = want ? '点击暂停背景音乐' : '点击继续播放背景音乐';
      }
      function play() {
        if (!want) return;
        try {
          var result = audio.play();
          if (result && result.then) result.then(paint).catch(function () { paint(); });
        } catch (e) {}
        paint();
      }
      function pause() {
        try { audio.pause(); } catch (e) {}
        paint();
      }
      function persist() {
        try { if (storage) storage.setItem('bgm_on', want ? '1' : '0'); } catch (e) {}
      }
      function onClick(e) {
        e.preventDefault();
        e.stopPropagation();
        want = !want;
        p.__bgmWant = want;
        persist();
        if (want) play(); else pause();
        paint();
      }
      try { if (p.__bgmCleanup) p.__bgmCleanup(); } catch (e) {}
      function cleanup() {
        try {
          btn.removeEventListener('click', onClick);
          d.removeEventListener('pointerdown', firstInteraction, true);
          d.removeEventListener('keydown', firstInteraction, true);
        } catch (e) {}
      }
      btn.addEventListener('click', onClick);
      audio.onplay = paint;
      audio.onpause = paint;
      var firstInteraction = function () {
        if (want && audio.paused) play();
      };
      d.addEventListener('pointerdown', firstInteraction, true);
      d.addEventListener('keydown', firstInteraction, true);
      p.__bgmCleanup = cleanup;
      if (want) { setTimeout(play, 120); } else { pause(); }
      paint();
    })();
    </script>
    """
    components.html(
        template.replace("__MUSIC__", uri).replace(
            "__ONLIGHT__", "true" if on_light else "false"
        ),
        height=height,
    )


def _render_voice_mic(height: int = 2) -> None:
    """在聊天输入框最右侧注入小麦克风：长按说话，松开把中文识别结果填入输入框。"""
    template = """
    <style>
      html, body { margin: 0; padding: 0; background: transparent; overflow: hidden; }
    </style>
    <script>
    (function () {
      var parentWin = null, parentDoc = null;
      try { parentWin = window.parent; parentDoc = window.parent.document; } catch (e) {}
      var scope = parentDoc || document;
      var SR = null;
      try {
        SR = (parentWin && (parentWin.SpeechRecognition || parentWin.webkitSpeechRecognition))
             || window.SpeechRecognition || window.webkitSpeechRecognition;
      } catch (e) {}
      if (!SR) return;

      var mic = null, rec = null, listening = false, finalText = '', interimText = '', target = null;
      var spaceTimer = null, spaceRecording = false;

      function isEditable(el) {
        return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
      }
      function findChatInput() {
        var containers = scope.querySelectorAll('[class*="st-key-chat-input"]');
        for (var i = 0; i < containers.length; i++) {
          if (containers[i].offsetParent === null) continue;
          var input = containers[i].querySelector('input[type="text"], input:not([type]), textarea');
          if (input && !input.disabled && input.offsetParent !== null) return input;
        }
        var inputs = scope.querySelectorAll('input[type="text"], textarea');
        for (var j = 0; j < inputs.length; j++) {
          var aria = inputs[j].getAttribute('aria-label') || '';
          if (aria === '输入你的参赛需求' || aria === '群消息' || aria.indexOf('发给 ') === 0) {
            if (!inputs[j].disabled && inputs[j].offsetParent !== null) return inputs[j];
          }
        }
        return null;
      }
      function fill(el, text) {
        if (!el) return;
        var proto = el.tagName === 'TEXTAREA'
          ? (parentWin ? parentWin.HTMLTextAreaElement.prototype : HTMLTextAreaElement.prototype)
          : (parentWin ? parentWin.HTMLInputElement.prototype : HTMLInputElement.prototype);
        var desc = Object.getOwnPropertyDescriptor(proto, 'value');
        var base = el.value || '';
        if (!base.trim()) base = '';
        var value = base ? base + ' ' + text : text;
        if (desc && desc.set) desc.set.call(el, value);
        else el.value = value;
        var EventCtor = (parentWin && parentWin.Event) ? parentWin.Event : Event;
        el.dispatchEvent(new EventCtor('input', { bubbles: true }));
        el.dispatchEvent(new EventCtor('change', { bubbles: true }));
        try { el.focus(); } catch (e) {}
      }
      function paint(on) {
        if (!mic) return;
        mic.textContent = on ? '●' : '🎤';
        mic.style.background = on ? '#34c759' : 'rgba(240,253,244,.88)';
        mic.style.color = on ? '#ffffff' : '#059669';
        mic.style.borderColor = on ? '#2ebd85' : '#a7f3d0';
        mic.style.boxShadow = on
          ? '0 0 0 3px rgba(52,199,89,.20), 0 2px 7px rgba(6,80,52,.18)'
          : '0 2px 6px rgba(6,80,52,.12)';
        mic.title = on ? '正在听，松开结束' : '按住说话，松开转文字';
      }
      function flush() {
        var text = (finalText + interimText).trim();
        finalText = '';
        interimText = '';
        if (!text) return;
        var el = target && target.offsetParent !== null ? target : findChatInput();
        if (el) fill(el, text);
      }
      function start() {
        if (listening) return;
        target = findChatInput();
        if (!target) return;
        finalText = '';
        interimText = '';
        listening = true;
        paint(true);
        try {
          rec = new SR();
          rec.lang = 'zh-CN';
          rec.continuous = true;
          rec.interimResults = true;
          rec.onresult = function (event) {
            interimText = '';
            for (var i = event.resultIndex; i < event.results.length; i++) {
              var res = event.results[i];
              if (res.isFinal) finalText += res[0].transcript;
              else interimText += res[0].transcript;
            }
          };
          rec.onerror = function (event) {
            if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
              listening = false;
              paint(false);
            }
          };
          rec.onend = function () {
            if (listening) {
              try { rec.start(); } catch (e) {}
            } else {
              flush();
            }
          };
          rec.start();
        } catch (e) {
          listening = false;
          paint(false);
        }
      }
      function stop() {
        if (!listening) return;
        listening = false;
        paint(false);
        try { if (rec) rec.stop(); } catch (e) {}
        flush();
      }
      function ensureMic() {
        var input = findChatInput();
        if (!input) return false;
        var wrapper = input.closest ? input.closest('div[data-baseweb="input"]') : null;
        if (!wrapper) wrapper = input.parentElement;
        if (!wrapper) return false;
        if (mic && mic.parentNode) mic.parentNode.removeChild(mic);
        mic = parentDoc.createElement('button');
        mic.type = 'button';
        mic.setAttribute('aria-label', '语音输入');
        mic.title = '按住说话，松开转文字';
        mic.textContent = '🎤';
        mic.style.cssText = 'position:absolute;right:6px;top:50%;transform:translateY(-50%);' +
          'width:28px;height:28px;padding:0;border:1px solid #a7f3d0;border-radius:50%;' +
          'background:rgba(240,253,244,.88);color:#059669;font-size:14px;line-height:1;' +
          'display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:10;' +
          'box-shadow:0 2px 6px rgba(6,80,52,.12);';
        wrapper.style.position = 'relative';
        input.style.paddingRight = '40px';
        wrapper.appendChild(mic);
        mic.addEventListener('pointerdown', function (e) {
          e.preventDefault();
          e.stopPropagation();
          start();
        });
        mic.addEventListener('click', function (e) {
          e.preventDefault();
          e.stopPropagation();
        });
        if (parentWin) parentWin.__voiceMicBtn = mic;
        return true;
      }
      var pointerUp = function () { if (listening) stop(); };
      var spaceDown = function (e) {
        if (e.code !== 'Space') return;
        if (e.target && (e.target.tagName === 'BUTTON' || e.target.tagName === 'A' || e.target.tagName === 'SELECT')) return;
        if (isEditable(e.target) && String(e.target.value || '').trim()) return;
        if (spaceTimer) return;
        spaceTimer = setTimeout(function () {
          spaceTimer = null;
          spaceRecording = true;
          start();
        }, 260);
      };
      var spaceUp = function (e) {
        if (e.code !== 'Space') return;
        if (spaceTimer) { clearTimeout(spaceTimer); spaceTimer = null; }
        if (spaceRecording) {
          e.preventDefault();
          spaceRecording = false;
          stop();
        }
      };
      try { if (parentWin && parentWin.__voiceCleanup) parentWin.__voiceCleanup(); } catch (e) {}
      function cleanup() {
        if (spaceTimer) { clearTimeout(spaceTimer); spaceTimer = null; }
        if (listening) { listening = false; try { if (rec) rec.abort(); } catch (e) {} }
        try { if (mic && mic.parentNode) mic.parentNode.removeChild(mic); } catch (e) {}
        try { if (target) target.style.paddingRight = ''; } catch (e) {}
        try {
          if (parentDoc) {
            parentDoc.removeEventListener('pointerup', pointerUp, true);
            parentDoc.removeEventListener('keydown', spaceDown, true);
            parentDoc.removeEventListener('keyup', spaceUp, true);
          }
        } catch (e) {}
        document.removeEventListener('pointerup', pointerUp, true);
        document.removeEventListener('keydown', spaceDown, true);
        document.removeEventListener('keyup', spaceUp, true);
        if (parentWin) {
          parentWin.__voiceMicBtn = null;
          parentWin.__voiceCleanup = null;
        }
      }
      if (parentDoc) {
        parentDoc.addEventListener('pointerup', pointerUp, true);
        parentDoc.addEventListener('keydown', spaceDown, true);
        parentDoc.addEventListener('keyup', spaceUp, true);
        try { parentWin.__voiceCleanup = cleanup; } catch (e) {}
      }
      document.addEventListener('pointerup', pointerUp, true);
      document.addEventListener('keydown', spaceDown, true);
      document.addEventListener('keyup', spaceUp, true);
      var tries = 0;
      function attach() {
        tries += 1;
        if (!ensureMic() && tries < 20) setTimeout(attach, 250);
      }
      attach();
    })();
    </script>
    """
    components.html(template, height=height)


def _render_page_summary(text: str) -> None:
    st.markdown(f'<div class="page-summary">{text}</div>', unsafe_allow_html=True)


# ----------------------------------------------------------------------
# 全局样式
# ----------------------------------------------------------------------
def inject_global_style() -> None:
    st.markdown(
        """
        <style>
        :root {
            --leaf-900: #14532d;
            --leaf-700: #047857;
            --leaf-500: #10b981;
            --leaf-200: #bbf7d0;
            --leaf-100: #dcfce7;
            --leaf-50: #f0fdf4;
        }
        .stApp {
            background:
                radial-gradient(circle at 8% 6%, rgba(167, 243, 208, 0.45) 0%, rgba(167, 243, 208, 0) 40%),
                radial-gradient(circle at 92% 12%, rgba(253, 230, 138, 0.28) 0%, rgba(253, 230, 138, 0) 36%),
                radial-gradient(circle at 80% 92%, rgba(196, 181, 253, 0.22) 0%, rgba(196, 181, 253, 0) 38%),
                linear-gradient(140deg, #f2fdf6 0%, #e4f8ec 48%, #f8fffb 100%);
        }
        [data-testid="stAppViewContainer"] { background: transparent; }
        .block-container {
            padding-top: 1.1rem;
            padding-bottom: 4rem;
            max-width: 1420px;
        }
        h1, h2, h3, h4, h5 { color: var(--leaf-900); }
        [data-testid="stSidebar"] {
            background: rgba(255, 255, 255, 0.72);
            backdrop-filter: blur(16px);
            border-right: 1px solid #cfefdb;
        }
        [data-testid="stHeader"] { background: rgba(255,255,255,0.35); }
        .hero {
            background: rgba(255, 255, 255, 0.72);
            backdrop-filter: blur(14px);
            border: 1px solid #c9ecd7;
            border-radius: 22px;
            padding: 20px 26px;
            margin-bottom: 16px;
            box-shadow: 0 12px 30px rgba(16, 185, 129, 0.08);
        }
        .hero h1 { font-size: 1.72rem; margin: 0 0 6px 0; }
        .hero p { margin: 0; color: #3f6b55; line-height: 1.75; }
        div[data-testid="stMetric"] {
            background: rgba(255, 255, 255, 0.9);
            border: 1px solid #d9f3e3;
            border-radius: 16px;
            padding: 12px 14px;
            box-shadow: 0 4px 14px rgba(16, 185, 129, 0.06);
        }
        div[data-testid="stMetric"] label { color: #4b7a62 !important; }
        div[data-testid="stMetricValue"] { color: #065f46 !important; }
        div[data-testid="stProgress"] > div > div > div > div {
            background: linear-gradient(90deg, #34d399 0%, #059669 100%);
        }
        .stButton > button {
            border-radius: 999px;
            border: 1px solid #b7e4c7;
            background: rgba(255, 255, 255, 0.92);
            color: #166534;
            font-weight: 600;
            transition: all 0.15s ease;
        }
        .stButton > button:hover {
            border-color: #10b981;
            color: #047857;
            transform: translateY(-1px);
            box-shadow: 0 6px 16px rgba(16, 185, 129, 0.16);
        }
        .stButton > button[kind="primary"],
        button[data-testid="stBaseButton-primary"] {
            background: linear-gradient(135deg, #34d399 0%, #059669 100%);
            border: none;
            color: #ffffff;
            box-shadow: 0 8px 20px rgba(16, 185, 129, 0.28);
        }
        div[data-testid="stForm"] {
            background: rgba(255, 255, 255, 0.78);
            backdrop-filter: blur(10px);
            border: 1px solid #d7f0e1;
            border-radius: 20px;
            padding: 18px 18px 10px 18px;
        }
        .stTextInput input, .stNumberInput input, .stTextArea textarea,
        div[data-baseweb="select"] > div {
            background: #ffffff !important;
            border-radius: 12px !important;
            border-color: #cbeed8 !important;
        }
        .stTextInput input:focus, .stNumberInput input:focus,
        .stTextArea textarea:focus {
            border-color: #10b981 !important;
            box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.15) !important;
        }
        div[data-testid="stExpander"] {
            background: rgba(255, 255, 255, 0.72);
            border: 1px solid #d7f0e1;
            border-radius: 16px;
        }
        .contact-box {
            background: #effbf3;
            border-left: 4px solid #10b981;
            border-radius: 10px;
            padding: 9px 13px;
            margin: 8px 0;
            color: #14532d;
        }
        .small-muted, .bubble-time {
            color: #6b8f7b;
            font-size: 0.8rem;
            font-weight: 400;
        }
        .avatar-img {
            border-radius: 50%;
            object-fit: cover;
            border: 2px solid rgba(255, 255, 255, 0.9);
            box-shadow: 0 2px 8px rgba(16, 185, 129, 0.18);
            flex: 0 0 auto;
        }
        .account-head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
        .account-head .name { font-weight: 700; color: #14532d; font-size: 1.02rem; }
        .account-meta {
            color: #4b7a62;
            font-size: 0.86rem;
            line-height: 1.85;
            margin: 6px 0 10px 2px;
        }
        .hero-user { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
        .pill-note {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 999px;
            background: rgba(16, 185, 129, 0.12);
            color: #047857;
            font-size: 0.85rem;
        }

        /* 聊天气泡：发送方稍深绿，接收方浅绿/白 */
        div[class*="st-key-chat-mine-"] {
            background: linear-gradient(135deg, #a7f3d0 0%, #6ee7b7 100%);
            border: 1px solid #5eead4;
            border-radius: 18px 18px 4px 18px;
            padding: 10px 14px;
            box-shadow: 0 5px 16px rgba(16, 185, 129, 0.14);
        }
        div[class*="st-key-chat-theirs-"] {
            background: rgba(255, 255, 255, 0.95);
            border: 1px solid #d7f0e1;
            border-radius: 18px 18px 18px 4px;
            padding: 10px 14px;
            box-shadow: 0 4px 14px rgba(16, 185, 129, 0.07);
        }
        div[class*="st-key-chat-mine-"] p,
        div[class*="st-key-chat-theirs-"] p { margin: 0.1rem 0; }
        div[class*="st-key-chat-mine-"] * { color: #064e3b; }
        .bubble-row { display: flex; gap: 10px; align-items: flex-start; }
        .bubble-row.mine { flex-direction: row-reverse; text-align: right; }
        .bubble-body { flex: 1 1 auto; min-width: 0; }
        .bubble-meta { font-size: 0.78rem; color: #5b826d; margin-bottom: 3px; }
        .bubble-text { font-size: 0.96rem; line-height: 1.65; word-break: break-word; }

        /* 聊天输入框：比背景更明显的翠绿底 */
        div[class*="st-key-chat-input-"] {
            background: rgba(236, 253, 245, 0.9);
            border: 1px solid #a7f3d0;
            border-radius: 20px;
            padding: 10px 12px 4px 12px;
        }
        div[class*="st-key-chat-input-"] input {
            background: #d1fae5 !important;
            border-color: #6ee7b7 !important;
        }
        div[class*="st-key-invite-note-"] {
            background: #eafaf0;
            border: 1px dashed #86efac;
            border-radius: 12px;
            padding: 8px 12px;
            margin: 6px 0;
        }

        /* 顶部三个椭圆高亮点击块 */
        div[class*="st-key-nav_"] button {
            min-height: 3.25rem;
            border-radius: 999px !important;
            border: none !important;
            white-space: nowrap;
            font-size: 0.98rem;
            font-weight: 700;
            letter-spacing: 0.01em;
        }
        div[class*="st-key-nav_chat"] button {
            background: linear-gradient(135deg, #d9f99d 0%, #a3e635 100%);
            color: #365314;
        }
        div[class*="st-key-nav_inv"] button {
            background: linear-gradient(135deg, #fed7aa 0%, #fdba74 100%);
            color: #7c2d12;
        }
        div[class*="st-key-nav_team"] button {
            background: linear-gradient(135deg, #ddd6fe 0%, #c4b5fd 100%);
            color: #4c1d95;
        }
        div[class*="st-key-nav_admin"] button {
            background: linear-gradient(135deg, #fef08a 0%, #fde047 100%);
            color: #713f12;
        }
        div[class*="st-key-nav_"][class*="active"] button {
            outline: 3px solid rgba(16, 185, 129, 0.38);
            border: 2px solid #059669 !important;
            box-shadow: 0 8px 22px rgba(5, 150, 105, 0.26);
            transform: translateY(-2px) scale(1.012);
            font-weight: 800;
        }

        /* 历史参赛信息卡片 */
        .history-card {
            background: rgba(255,255,255,0.74);
            border: 1px solid #d7f0e1;
            border-radius: 18px;
            padding: 14px 16px;
            margin: 8px 0;
        }
        .history-card .title { font-weight: 700; color: #14532d; font-size: 1.02rem; }
        .new-dot { color: #ef4444; font-weight: 700; }

        .pretty-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            border: 1px solid #d7f0e1;
            border-radius: 16px;
            overflow: hidden;
            background: rgba(255,255,255,0.9);
            margin: 8px 0 14px 0;
        }
        .pretty-table th, .pretty-table td {
            padding: 10px 14px;
            border-bottom: 1px solid #e8f8ee;
            text-align: left;
            font-size: 0.94rem;
        }
        .pretty-table th {
            width: 34%;
            background: #f0fdf4;
            color: #047857;
            font-weight: 600;
        }
        .pretty-table tr:last-child th, .pretty-table tr:last-child td { border-bottom: none; }

        .floating-toast {
            position: fixed;
            left: 50%;
            bottom: 30px;
            transform: translateX(-50%);
            z-index: 99999;
            padding: 10px 20px;
            border-radius: 999px;
            background: rgba(16, 185, 129, 0.20);
            border: 1px solid #a7f3d0;
            color: #065f46;
            font-size: 0.92rem;
            backdrop-filter: blur(10px);
            animation: toastFade 5s ease forwards;
            pointer-events: none;
        }
        @keyframes toastFade {
            0% { opacity: 0; transform: translate(-50%, 8px); }
            10% { opacity: 1; transform: translate(-50%, 0); }
            78% { opacity: 1; }
            100% { opacity: 0; }
        }
        .topic-hint { font-size: 0.82rem; color: #5b826d; margin: 2px 0 4px 2px; }
        .mini-label { font-size: 0.8rem; color: #5b826d; }

        /* ================= 新版主页：左侧浅绿导航 + 右侧暖白内容 ================= */
        .stApp { background: #f5f7f5 !important; }
        .stApp::before {
            content: "";
            position: fixed;
            inset: 0;
            background-image: url("__BG_IMAGE__");
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
            opacity: 0.14;
            pointer-events: none;
            z-index: 0;
        }
        [data-testid="stAppViewContainer"] {
            position: relative;
            z-index: 1;
            background: transparent !important;
        }
        [data-testid="stHeader"] { background: transparent !important; }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #f2fdf6 0%, #e4f8ec 48%, #f8fffb 100%) !important;
            border-right: 1px solid #d6efe1 !important;
            box-shadow: 10px 0 34px rgba(11, 92, 62, 0.07);
        }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] .stCaption,
        [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
            color: #1F5F43 !important;
        }
        [data-testid="stSidebar"] .stButton > button {
            background: rgba(46, 189, 133, 0.12);
            border: 1px solid rgba(46, 189, 133, 0.28);
            color: #1F5F43;
        }
        [data-testid="stSidebar"] .stButton > button:hover {
            background: rgba(46, 189, 133, 0.2);
            border-color: rgba(46, 189, 133, 0.5);
            color: #1F5F43;
        }
        [data-testid="stSidebar"] summary,
        [data-testid="stSidebar"] summary *,
        [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] * {
            color: #1F5F43 !important;
        }
        [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
            background: rgba(46, 189, 133, 0.08) !important;
            border-color: rgba(46, 189, 133, 0.3) !important;
        }
        .sidebar-section-title {
            color: #1F5F43;
            font-weight: 800;
            font-size: 1.02rem;
            letter-spacing: 0.04em;
            margin: 2px 0 8px 2px;
        }
        .account-card {
            background: rgba(255, 255, 255, 0.7);
            border: 1px solid rgba(46, 189, 133, 0.24);
            border-radius: 18px;
            padding: 13px 14px 11px 14px;
            margin: 2px 0 14px 0;
            backdrop-filter: blur(12px);
            box-shadow: inset 0 1px 0 rgba(255,255,255,.8), 0 10px 24px rgba(6, 80, 52, 0.08);
        }
        .account-card .account-row { display: flex; align-items: center; gap: 12px; }
        .account-card .name-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .account-card .name { font-weight: 800; font-size: 1.06rem; color: #1F5F43; }
        .account-card .phone-line { margin-top: 9px; font-size: 0.82rem; color: rgba(31, 95, 67, 0.85); }
        .admin-tag {
            display: inline-block;
            padding: 2px 9px;
            border-radius: 999px;
            background: rgba(46, 189, 133, 0.16);
            color: #1F5F43;
            font-size: 0.72rem;
            font-weight: 800;
            letter-spacing: .04em;
        }
        div[class*="st-key-navbtn_"] button {
            min-height: 3.05rem;
            width: 100%;
            justify-content: flex-start;
            text-align: left;
            border-radius: 16px !important;
            padding-left: 16px !important;
            font-size: 0.97rem;
            font-weight: 700;
            letter-spacing: 0.01em;
            background: rgba(46, 189, 133, 0.1) !important;
            border: 1px solid rgba(46, 189, 133, 0.24) !important;
            color: #1F5F43 !important;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.6);
            transition: all 0.18s ease;
        }
        div[class*="st-key-navbtn_"] button:hover {
            background: rgba(46, 189, 133, 0.2) !important;
            border-color: rgba(46, 189, 133, 0.45) !important;
            transform: translateY(-1px);
        }
        div[class*="st-key-navbtn_"][class*="active"] button {
            background: linear-gradient(145deg, rgba(236,253,245,.95) 0%, rgba(187,247,208,.82) 48%, rgba(167,243,208,.9) 100%) !important;
            color: #15803d !important;
            border: 1px solid rgba(134, 239, 172, 0.9) !important;
            backdrop-filter: blur(16px) saturate(150%);
            box-shadow: inset 0 2px 0 rgba(255,255,255,.92), inset 0 -8px 18px rgba(255,255,255,.38),
                        0 12px 26px rgba(5, 78, 51, 0.12);
            transform: translateY(-1px);
            position: relative;
            overflow: hidden;
        }
        div[class*="st-key-navbtn_"][class*="active"] button::after {
            content: "";
            position: absolute;
            left: 8%;
            right: 8%;
            top: 2px;
            height: 42%;
            border-radius: 14px;
            background: linear-gradient(180deg, rgba(255,255,255,.85) 0%, rgba(255,255,255,0) 100%);
            pointer-events: none;
        }

        /* 微信式聊天气泡：小头像 + 气泡并排 */
        div[class*="st-key-chat-mine-"],
        div[class*="st-key-chat-theirs-"] {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0 !important;
        }
        .bubble-row { display: flex; gap: 9px; align-items: flex-start; justify-content: flex-start; }
        .bubble-row.mine { flex-direction: row-reverse; }
        .bubble-body { flex: 0 1 auto; max-width: 76%; min-width: 0; }
        .bubble-meta { font-size: 0.76rem; color: #5b826d; margin-bottom: 4px; }
        .bubble-row.mine .bubble-meta { text-align: right; }
        .bubble-text {
            display: inline-block;
            text-align: left;
            background: #ffffff;
            border: 1px solid #e2eee7;
            border-radius: 4px 16px 16px 16px;
            padding: 9px 13px;
            font-size: 0.96rem;
            line-height: 1.65;
            word-break: break-word;
            box-shadow: 0 4px 12px rgba(16, 85, 58, 0.06);
        }
        .bubble-row.mine .bubble-text {
            background: linear-gradient(135deg, #d8f7e4 0%, #9ff0c6 100%);
            border-color: #8fe6b8;
            border-radius: 16px 4px 16px 16px;
            text-align: left;
        }
        .bubble-row.mine .bubble-body { display: flex; flex-direction: column; align-items: flex-end; }
        .bubble-row.mine .bubble-text { align-self: flex-end; }

        /* 玻璃质感卡片 */
        .glass-panel {
            background: rgba(255, 255, 255, 0.66);
            border: 1px solid rgba(255, 255, 255, 0.9);
            border-radius: 20px;
            padding: 16px 18px;
            box-shadow: 0 12px 30px rgba(21, 82, 58, 0.08);
            backdrop-filter: blur(10px);
        }
        .history-title { font-weight: 800; color: #14532d; font-size: 1.02rem; margin-bottom: 2px; }
        .friend-row-name { font-weight: 700; color: #14532d; }
        .friend-row-sub { color: #6b8f7b; font-size: 0.8rem; }
        .chat-target-line { font-weight: 700; color: #0f5132; margin: 2px 0 6px 2px; }
        .voice-hint { color: #6b8f7b; font-size: 0.78rem; }
        .page-summary {
            background: rgba(224, 242, 229, 0.6);
            border: 1px solid rgba(167, 230, 195, 0.5);
            border-radius: 12px;
            padding: 10px 16px;
            margin: 8px 0 16px 0;
            font-size: 14px;
            color: #3a5a45;
        }

        /* 创建群聊：淡绿毛玻璃卡片 */
        div[class*="st-key-group-create-card"] {
            background: rgba(220, 250, 236, 0.60) !important;
            backdrop-filter: blur(16px) saturate(140%);
            border: 1px solid rgba(167, 243, 208, 0.78) !important;
            border-radius: 20px !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.68),
                        0 12px 30px rgba(6, 80, 52, 0.08) !important;
        }
        div[class*="st-key-group-create-card"] div[data-testid="stForm"] {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
        }
        div[class*="st-key-group-create-card"] .stTextInput div[data-baseweb="input"] {
            background: rgba(255,255,255,0.38) !important;
            border: 1px solid #a7f3d0 !important;
            border-radius: 12px !important;
        }
        div[class*="st-key-group-create-card"] .stTextInput div[data-baseweb="base-input"],
        div[class*="st-key-group-create-card"] .stTextInput input {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
        }
        div[class*="st-key-group-create-card"] div[data-baseweb="select"] > div {
            background: rgba(255,255,255,0.38) !important;
            border-color: #a7f3d0 !important;
            border-radius: 10px !important;
        }

        /* 手机端：放大汉堡菜单并显示“菜单”文字 */
        [data-testid="stSidebarCollapsedControl"] {
            z-index: 1000000;
        }
        [data-testid="stSidebarCollapsedControl"] button {
            min-width: 88px !important;
            min-height: 46px !important;
            padding: 0 14px !important;
            border-radius: 14px !important;
            background: rgba(220, 250, 236, 0.96) !important;
            border: 1px solid #a7f3d0 !important;
            box-shadow: 0 6px 18px rgba(6, 80, 52, 0.18) !important;
            display: flex !important;
            align-items: center !important;
            gap: 6px !important;
        }
        [data-testid="stSidebarCollapsedControl"] button::after {
            content: "菜单";
            color: #0b7a51;
            font-size: 14px;
            font-weight: 800;
        }
        [data-testid="stSidebarCollapsedControl"] svg {
            width: 20px;
            height: 20px;
            color: #0b7a51;
        }
        </style>
        """.replace(
            "__BG_IMAGE__",
            _asset_data_uri(os.path.join(BASE_DIR, "assets", "bg_image.png"), "image/png"),
        ),
        unsafe_allow_html=True,
    )


# ----------------------------------------------------------------------
# 数据层 / 会话状态
# ----------------------------------------------------------------------
def get_pool() -> DataPool:
    if "pool" not in st.session_state:
        pool_obj = DataPool(BASE_DIR)
        pool_obj.expire_intents()
        st.session_state.pool = pool_obj
    return st.session_state.pool


def get_table():
    if "table" not in st.session_state:
        st.session_state.table = matcher.load_competition_table(
            os.path.join(BASE_DIR, "competition_table.json")
        )
    return st.session_state.table


def get_store() -> SocialStore:
    if "store" not in st.session_state:
        st.session_state.store = SocialStore(BASE_DIR)
    return st.session_state.store


def append_chat(role: str, content: str) -> None:
    st.session_state.chat.append({"role": role, "content": content})


def init_chat(keep_collected: bool = False) -> None:
    st.session_state.chat = [{"role": "assistant", "content": llm_api.OPENING_MESSAGE}]
    if not keep_collected:
        st.session_state.collected = {}
    st.session_state.stage = "chatting"
    st.session_state.host_intent_id = None
    st.session_state.match_result = None
    st.session_state.form_competition = None
    st.session_state.pending_collected = None
    st.session_state.unknown_competition = None
    st.session_state.last_error = None
    st.session_state.form_version = int(st.session_state.get("form_version", 0)) + 1


pool = get_pool()
table = get_table()
store = get_store()


def _reload_store() -> bool:
    """兼容旧缓存对象：没有 reload() 时直接重新实例化 SocialStore。"""
    global store
    reload_fn = getattr(store, "reload", None)
    if callable(reload_fn):
        try:
            return bool(reload_fn())
        except Exception:
            return False
    try:
        fresh = SocialStore(BASE_DIR)
        store = fresh
        st.session_state.store = fresh
        return True
    except Exception:
        return False


def _reload_pool() -> bool:
    """兼容旧缓存对象：没有 reload() 时直接重新实例化 DataPool。"""
    global pool
    reload_fn = getattr(pool, "reload", None)
    ok = False
    if callable(reload_fn):
        try:
            ok = bool(reload_fn())
        except Exception:
            ok = False
    else:
        try:
            fresh = DataPool(BASE_DIR)
            pool = fresh
            st.session_state.pool = fresh
            ok = True
        except Exception:
            ok = False
    if ok:
        ensure = globals().get("_ensure_super_admin")
        if callable(ensure):
            try:
                ensure()
            except Exception:
                pass
    return ok


if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "current_user_id" not in st.session_state:
    st.session_state.current_user_id = None
if "chat" not in st.session_state:
    init_chat()
if "last_expired" not in st.session_state:
    st.session_state.last_expired = None
if "page" not in st.session_state:
    st.session_state.page = "chat"


def _is_super_admin(user: dict) -> bool:
    return (
        str(user.get("nickname") or "").strip() == SUPER_NICKNAME
        and str(user.get("phone") or "").strip() == SUPER_PHONE
    )


def _ensure_super_admin() -> None:
    found = False
    for uid, user in pool.users.items():
        if _is_super_admin(user):
            try:
                store.add_admin(uid)
            except Exception:
                pass
            found = True
    if found or getattr(pool, "cloud_read_failed", False):
        return
    try:
        uid = pool.find_user_by_nickname(SUPER_NICKNAME)
        if uid:
            user = pool.get_user(uid) or {}
            user["phone"] = SUPER_PHONE
            user["updated_at"] = now_str()
            pool._save_users()
        else:
            uid = pool.upsert_user(
                {
                    "nickname": SUPER_NICKNAME,
                    "phone": SUPER_PHONE,
                    "free_time": ["随时"],
                    "gender_preference": "不限",
                }
            )
        store.add_admin(uid)
    except Exception:
        pass


_ensure_super_admin()


def _repair_explicit_periods() -> None:
    """把历史数据里“12.1到12.8”这类可重新解析的时间段修正为真实截止日。"""
    explicit = re.compile(r"\d{1,2}\s*(?:月|[./])\s*\d{1,2}")
    changed = False
    for intent in pool.intents.values():
        period = intent.get("available_period")
        if not isinstance(period, dict):
            continue
        raw = str(period.get("raw") or "").strip()
        if not raw or not explicit.search(raw):
            continue
        parsed = llm_api.parse_period_text(raw)
        if not parsed.get("end_date"):
            continue
        new_start = parsed.get("start_date") or period.get("start_date")
        new_end = parsed.get("end_date")
        if period.get("start_date") != new_start or period.get("end_date") != new_end:
            period["start_date"] = new_start
            period["end_date"] = new_end
            changed = True
    if changed:
        pool._save_intents()


_repair_explicit_periods()


def _find_user_by_phone(phone: str):
    target = str(phone or "").strip()
    if not target:
        return None
    for uid, user in pool.users.items():
        if str(user.get("phone") or "").strip() == target:
            return uid
    return None


def _compress_avatar_data_uri(data_uri: str, max_size: int = 256) -> str:
    """头像入库前压缩到适合展示的尺寸，避免 Neon users 行过大导致读取超时。"""
    if not data_uri.startswith("data:image/") or data_uri.startswith("data:image/svg"):
        return data_uri
    try:
        _header, payload = data_uri.split(",", 1)
        raw = base64.b64decode(payload)
        from PIL import Image  # type: ignore

        image = Image.open(io.BytesIO(raw))
        image.thumbnail((max_size, max_size))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=82, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode(
            "ascii"
        )
    except Exception:
        return data_uri


def _save_avatar(user_id: str, data_uri: str) -> None:
    user = pool.get_user(user_id)
    if user is None or not data_uri:
        return
    user["avatar"] = _compress_avatar_data_uri(data_uri)
    user["updated_at"] = now_str()
    pool._save_users()


def _read_avatar_upload(uploaded) -> str:
    if uploaded is None:
        return ""
    data = uploaded.getvalue()
    if not data:
        return ""
    if len(data) > 3 * 1024 * 1024:
        st.error("头像图片请不要超过 3MB。")
        return ""
    mime = getattr(uploaded, "type", "") or "image/png"
    if not str(mime).startswith("image/"):
        mime = "image/png"
    data_uri = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
    return _compress_avatar_data_uri(data_uri)


def _is_admin(user_id: str) -> bool:
    return store.is_admin(user_id)


def _latest_intent(user_id: str):
    intents = pool.list_user_intents(user_id)
    if not intents:
        return None
    valid = [i for i in intents if i.get("status") != "deleted"]
    return (valid or intents)[-1]


# ----------------------------------------------------------------------
# 登录页
# ----------------------------------------------------------------------
NICKNAME_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9_]{1,20}$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def inject_login_style() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background: transparent !important;
        }
        [data-testid="stAppViewContainer"],
        [data-testid="stHeader"] {
            background: transparent !important;
        }
        section[data-testid="stSidebar"], div[data-testid="stSidebar"] {
            display: none;
        }
        .bg-video {
            position: fixed;
            inset: 0;
            width: 100vw;
            height: 100vh;
            object-fit: cover;
            z-index: -2;
        }
        .bg-shade {
            position: fixed;
            inset: 0;
            z-index: -1;
            background:
                linear-gradient(180deg, rgba(255,255,255,0.22) 0%, rgba(236,253,245,0.34) 55%, rgba(236,253,245,0.42) 100%);
            pointer-events: none;
        }
        .block-container {
            position: relative;
            z-index: 3;
        }
        .login-hero {
            text-align: center;
            padding: 4.2rem 1rem 1.1rem 1rem;
        }
        .login-brand {
            display: inline-block;
            padding: 6px 16px;
            border-radius: 999px;
            background: rgba(255,255,255,0.72);
            border: 1px solid rgba(167, 243, 208, 0.9);
            color: #047857;
            letter-spacing: 0.06em;
            font-size: 0.9rem;
            margin-bottom: 16px;
            backdrop-filter: blur(8px);
        }
        .login-hero h1 {
            font-size: 2.55rem;
            line-height: 1.28;
            margin: 0 0 12px 0;
            color: #ffffff;
            letter-spacing: 0.04em;
            text-shadow: 0 3px 24px rgba(6, 78, 59, 0.55);
        }
        .login-hero p {
            margin: 0 auto;
            max-width: 660px;
            color: #f0fdf4;
            line-height: 1.8;
            text-shadow: 0 2px 14px rgba(6, 78, 59, 0.55);
        }
        div[class*="st-key-login-card"] {
            background: linear-gradient(155deg, rgba(236,253,245,0.72) 0%, rgba(209,250,229,0.52) 100%);
            backdrop-filter: blur(22px) saturate(140%);
            border: 1px solid rgba(255, 255, 255, 0.78);
            border-radius: 26px;
            padding: 26px 28px 18px 28px;
            box-shadow:
                inset 0 2px 2px rgba(255,255,255,.85),
                inset 0 -14px 28px rgba(52,211,153,.16),
                inset 0 0 0 1px rgba(167,243,208,.35),
                0 20px 46px rgba(6,78,59,.16);
            position: relative;
            overflow: hidden;
        }
        div[class*="st-key-login-card"]::before {
            content: "";
            position: absolute;
            left: 5%;
            right: 5%;
            top: -22%;
            height: 68%;
            border-radius: 50%;
            background: radial-gradient(ellipse at 50% 0%, rgba(255,255,255,.75) 0%, rgba(255,255,255,.18) 45%, rgba(255,255,255,0) 75%);
            filter: blur(2px);
            pointer-events: none;
        }
        div[class*="st-key-login-card"]::after {
            content: "";
            position: absolute;
            left: 9%;
            right: 9%;
            bottom: -20%;
            height: 46%;
            border-radius: 50%;
            background: radial-gradient(ellipse at 50% 100%, rgba(110,231,183,.26) 0%, rgba(110,231,183,0) 70%);
            pointer-events: none;
        }
        div[class*="st-key-login-card"] h4 {
            color: #065f46;
            margin-bottom: 2px;
        }
        div[class*="st-key-login-card"] label,
        div[class*="st-key-login-card"] p {
            color: #1f4d39;
        }
        div[class*="st-key-login-card"] .stButton > button,
        div[class*="st-key-login-card"] .stFormSubmitButton > button {
            background: linear-gradient(135deg, #34d399 0%, #059669 100%);
            border: none;
            color: #ffffff;
            font-weight: 700;
            min-height: 2.8rem;
        }
        div[class*="st-key-login-card"] div[data-testid="stForm"] {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            backdrop-filter: none !important;
            padding: 2px 0 0 0 !important;
        }
        div[class*="st-key-login-card"] .stTextInput div[data-baseweb="input"] {
            background: rgba(255,255,255,0.30) !important;
            border: 1px solid #a7f3d0 !important;
            border-radius: 12px !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.55) !important;
        }
        div[class*="st-key-login-card"] .stTextInput div[data-baseweb="base-input"] {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
        }
        div[class*="st-key-login-card"] .stTextInput div[data-baseweb="input"]:focus-within {
            border-color: #34d399 !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.5),
                        0 0 0 2px rgba(52, 211, 153, 0.18) !important;
        }
        div[class*="st-key-login-card"] .stTextInput input {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            color: #14532d !important;
        }
        div[class*="st-key-login-card"] .stTextInput input::placeholder {
            color: rgba(20, 83, 45, 0.48) !important;
        }
        div[class*="st-key-login-card"] [data-testid="stFileUploaderDropzone"] {
            background: rgba(255,255,255,0.28) !important;
            border: 1px dashed rgba(52, 211, 153, 0.65) !important;
            border-radius: 14px !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.45) !important;
        }
        div[class*="st-key-login-card"] [data-testid="stFileUploaderDropzone"] * {
            color: #14532d !important;
        }
        div[class*="st-key-login-card"] [data-testid="stFileUploaderDropzone"] button {
            background: rgba(255,255,255,0.42) !important;
            border: 1px solid #a7f3d0 !important;
            border-radius: 10px !important;
            color: #15803d !important;
            box-shadow: none !important;
        }
        div[class*="st-key-login-card"] [data-testid="stFileUploaderFile"] {
            background: transparent !important;
        }
        .login-music {
            margin-top: 6px;
            opacity: 0.9;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


AUTH_SALT = "team-match-neon-2026"


def _user_is_deleted(user_id: str) -> bool:
    user = pool.get_user(user_id)
    if user is not None:
        return bool(user.get("deleted")) or bool(user.get("is_deleted"))
    return store.is_deleted(user_id)


def render_admin_invite_banner() -> None:
    inviter_uid = store.get_admin_inviter(current_uid)
    if not inviter_uid:
        return
    inviter = pool.get_user(inviter_uid) or {}
    if not _is_super_admin(inviter):
        store.reject_admin_invite(current_uid)
        return
    st.warning(
        "『%s』邀请你成为管理员。同意后左侧会出现“管理后台”入口。"
        % (inviter.get("nickname") or "超级管理员")
    )
    c1, c2 = st.columns(2)
    if c1.button(
        "同意成为管理员",
        key="banner_admin_accept",
        use_container_width=True,
        type="primary",
    ):
        store.accept_admin_invite(current_uid)
        st.session_state.is_admin = True
        st.session_state.admin_flag_user = current_uid
        for note in store.find_unread_by_kind(current_uid, "admin_invite"):
            store.mark_notification_read(note.get("notification_id"), current_uid)
        store.notify(
            inviter_uid,
            "『%s』已同意成为管理员。" % (current_user.get("nickname") or "对方"),
            "admin_accepted",
        )
        _set_flash("你已成为管理员。")
        st.rerun()
    if c2.button("暂不", key="banner_admin_reject", use_container_width=True):
        store.reject_admin_invite(current_uid)
        for note in store.find_unread_by_kind(current_uid, "admin_invite"):
            store.mark_notification_read(note.get("notification_id"), current_uid)
        _set_flash("已忽略管理员邀请。")
        st.rerun()


def _auth_token(user_id: str, user: dict) -> str:
    material = "%s|%s|%s|%s" % (
        user_id,
        user.get("nickname") or "",
        user.get("phone") or "",
        AUTH_SALT,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _query_param(name: str) -> str:
    try:
        value = st.query_params.get(name, "")
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "")
    except Exception:
        return ""


def _remember_login(user_id: str, user: dict) -> None:
    try:
        st.query_params["uid"] = user_id
        st.query_params["token"] = _auth_token(user_id, user)
    except Exception:
        pass


def _clear_remembered_login() -> None:
    try:
        st.query_params.pop("uid", None)
        st.query_params.pop("token", None)
    except Exception:
        try:
            st.query_params.clear()
        except Exception:
            pass


def _restore_login_from_query() -> None:
    if st.session_state.get("logged_in"):
        return
    user_id = _query_param("uid")
    token = _query_param("token")
    if not user_id or not token:
        return
    user = pool.get_user(user_id)
    if user is None or _user_is_deleted(user_id):
        return
    if _auth_token(user_id, user) != token:
        return
    _reload_store()
    if _is_super_admin(user):
        store.add_admin(user_id)
    st.session_state.is_admin = _is_admin(user_id)
    st.session_state.admin_flag_user = user_id
    st.session_state.logged_in = True
    st.session_state.current_user_id = user_id
    st.session_state.page = "chat"
    st.session_state.login_error = None
    st.session_state.pending_phone_uid = None
    st.session_state.need_avatar_confirm = not bool(user.get("avatar"))
    init_chat()


def perform_login(user_id: str) -> None:
    _reload_pool()
    _reload_store()
    user = pool.get_user(user_id) or {}
    if _is_super_admin(user):
        store.add_admin(user_id)
    st.session_state.is_admin = _is_admin(user_id)
    st.session_state.admin_flag_user = user_id
    _remember_login(user_id, user)
    st.session_state.logged_in = True
    st.session_state.current_user_id = user_id
    st.session_state.page = "chat"
    st.session_state.login_error = None
    st.session_state.pending_phone_uid = None
    st.session_state.need_avatar_confirm = not bool(user.get("avatar"))
    st.session_state.pop("conversation_choice", None)
    st.session_state.pop("group_choice", None)
    init_chat()
    st.rerun()


def logout() -> None:
    _clear_remembered_login()
    st.session_state.logged_in = False
    st.session_state.current_user_id = None
    st.session_state.page = "chat"
    st.session_state.login_error = None
    st.session_state.pending_phone_uid = None
    st.session_state.need_avatar_confirm = False
    st.session_state.pop("is_admin", None)
    st.session_state.pop("admin_flag_user", None)
    st.session_state.pop("conversation_choice", None)
    st.session_state.pop("group_choice", None)
    init_chat()
    st.rerun()


@st.dialog("该手机号已注册")
def phone_exists_dialog(user_id: str) -> None:
    user = pool.get_user(user_id) or {}
    st.markdown(
        "该手机号已注册，原账号名称为 **%s**。"
        % _esc(user.get("nickname") or "未命名")
    )
    st.markdown("是否为您的账号？")
    c1, c2 = st.columns(2)
    if c1.button("是，进入原账号", use_container_width=True, type="primary"):
        st.session_state.pending_phone_uid = None
        perform_login(user_id)
    if c2.button("否", use_container_width=True):
        st.session_state.pending_phone_uid = None
        st.session_state.login_error = (
            "该手机号已注册，无法再次注册，请更换手机号。"
        )
        st.rerun()


@st.dialog("默认头像确认")
def post_login_avatar_dialog() -> None:
    st.write("您还没有设置头像，是否使用默认头像？")
    c1, c2 = st.columns(2)
    if c1.button("是，使用默认头像", use_container_width=True, type="primary"):
        st.session_state.need_avatar_confirm = False
        _save_avatar(current_uid, DEFAULT_AVATAR)
        _set_flash("已使用默认头像。")
        st.rerun()
    if c2.button("否，我去设置头像", use_container_width=True):
        st.session_state.need_avatar_confirm = False
        _set_flash("请在左侧“更换头像”中上传头像。")
        st.rerun()


def handle_login_submit(nickname: str, phone: str, avatar_data: str) -> None:
    nickname = str(nickname or "").strip()
    phone = str(phone or "").strip()
    st.session_state.login_error = None

    if not phone:
        st.session_state.login_error = "请填写手机号。"
        return
    if not PHONE_RE.fullmatch(phone):
        st.session_state.login_error = "请输入正确的 11 位手机号。"
        return

    existing_uid = _find_user_by_phone(phone)
    if existing_uid:
        if avatar_data:
            _save_avatar(existing_uid, avatar_data)
        st.session_state.pending_phone_uid = existing_uid
        st.rerun()
        return

    if not nickname:
        st.session_state.login_error = "请填写昵称。"
        return
    if not NICKNAME_RE.fullmatch(nickname):
        st.session_state.login_error = "昵称只能包含中文、数字、英文字母和下划线，长度 1-20 位。"
        return

    same_nick_uid = pool.find_user_by_nickname(nickname)
    if same_nick_uid:
        user = pool.get_user(same_nick_uid) or {}
        stored_phone = str(user.get("phone") or "").strip()
        if stored_phone and stored_phone != phone:
            st.session_state.login_error = (
                "昵称「%s」已被使用，请更换昵称。" % nickname
            )
            return
        if not stored_phone:
            user["phone"] = phone
            user["updated_at"] = now_str()
            pool._save_users()
            if avatar_data:
                _save_avatar(same_nick_uid, avatar_data)
            perform_login(same_nick_uid)
            return

    user_id = pool.upsert_user({"nickname": nickname, "phone": phone})
    if avatar_data:
        _save_avatar(user_id, avatar_data)
    perform_login(user_id)


def render_login() -> None:
    inject_login_style()
    pending_phone_uid = st.session_state.get("pending_phone_uid")
    if pending_phone_uid:
        if pool.get_user(pending_phone_uid):
            phone_exists_dialog(pending_phone_uid)
        else:
            st.session_state.pending_phone_uid = None
    video_uri = _asset_data_uri(os.path.join(BASE_DIR, "assets", "bg_video.mp4"), "video/mp4")
    if video_uri:
        st.markdown(
            '<video class="bg-video" autoplay muted loop playsinline>'
            '<source src="%s" type="video/mp4"/></video><div class="bg-shade"></div>'
            % video_uri,
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<div class="bg-shade"></div>', unsafe_allow_html=True)

    st.markdown(
        """
        <div class="login-hero">
          <div class="login-brand">创新项目组队匹配智能体</div>
          <h1>找到志同道合的队友</h1>
          <p>说出你想参加的比赛，填写一次组队需求表，确认后由匹配算法推荐队友。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _, middle, _ = st.columns([1, 1.3, 1])
    with middle:
        with st.container(key="login-card"):
            st.markdown("#### 登录 / 注册")
            st.caption("昵称唯一，手机号唯一；一个手机号只能对应一个账号。")
            if st.session_state.get("login_error"):
                st.error(st.session_state.login_error)
            with st.form("login_form", clear_on_submit=False):
                avatar_file = st.file_uploader(
                    "头像（可上传，不设置将使用默认头像）",
                    type=["png", "jpg", "jpeg", "webp"],
                    key="login_avatar",
                )
                nickname = st.text_input(
                    "昵称",
                    placeholder="中文、字母、数字或下划线，1-20 位",
                )
                phone = st.text_input("手机号", placeholder="请输入 11 位手机号")
                submitted = st.form_submit_button(
                    "开始匹配", use_container_width=True, type="primary"
                )
            if submitted:
                avatar_data = _read_avatar_upload(avatar_file)
                handle_login_submit(nickname, phone, avatar_data)

            st.caption("背景音乐（页面会自动尝试播放；若被浏览器拦截，点一下页面即可开始）")
            _render_bg_music(on_light=True)


_restore_login_from_query()

if not st.session_state.get("logged_in") or not st.session_state.get("current_user_id"):
    render_login()
    st.stop()

if not pool.get_user(st.session_state.current_user_id):
    _reload_pool()

if not pool.get_user(st.session_state.current_user_id):
    if _user_is_deleted(st.session_state.current_user_id):
        logout()
    st.warning("用户资料正在从云端同步，请稍后刷新页面重试。")
    st.stop()


inject_global_style()
_render_bg_music(on_light=True)


# ----------------------------------------------------------------------
# 页面通用：侧边栏、导航、通知、弹窗
# ----------------------------------------------------------------------
current_uid = st.session_state.get("current_user_id")
current_user = pool.get_user(current_uid) or {}
if (
    st.session_state.get("admin_flag_user") != current_uid
    or "is_admin" not in st.session_state
):
    st.session_state.is_admin = _is_admin(current_uid)
    st.session_state.admin_flag_user = current_uid
current_is_admin = bool(st.session_state.get("is_admin"))
render_admin_invite_banner()


def _accepted_invitations(user_id: str):
    return [
        inv
        for inv in pool.list_invitations_for_user(user_id)
        if inv.get("status") == "accepted"
    ]


def _opposite(inv: dict, uid: str):
    if inv.get("from_user") == uid:
        return inv.get("to_user"), inv.get("from_intent"), inv.get("to_intent")
    return inv.get("from_user"), inv.get("to_intent"), inv.get("from_intent")


def _message_unread_count(user_id: str) -> int:
    count = 0
    for inv in _accepted_invitations(user_id):
        _, my_iid, other_iid = _opposite(inv, user_id)
        if not my_iid or not other_iid:
            continue
        pair = pair_key(my_iid, other_iid)
        last_read = store.last_message_read(user_id, pair)
        for msg in pool.get_messages(my_iid, other_iid):
            if msg.get("from_user") == user_id:
                continue
            if str(msg.get("created_at", "")) > str(last_read):
                count += 1
    for group in store.list_groups(user_id):
        gid = group.get("group_id")
        last_read = store.last_group_read(user_id, gid)
        for msg in group.get("messages") or []:
            if msg.get("from_user") == user_id:
                continue
            if str(msg.get("created_at", "")) > last_read:
                count += 1
    return count


def _invitation_unread_count(user_id: str) -> int:
    count = 0
    for inv in pool.list_invitations_for_user(user_id):
        if (
            inv.get("to_user") == user_id
            and inv.get("status") == "pending"
            and not store.invitation_seen(user_id, inv.get("invitation_id", ""))
        ):
            count += 1
    return count


def _notification_unread_count(user_id: str) -> int:
    return len(store.unread_notifications(user_id))


INV_NOTICE_KINDS = {"team_accepted", "invite_rejected", "group_created"}


def _inv_notice_unread_count(user_id: str) -> int:
    return sum(
        1
        for note in store.unread_notifications(user_id)
        if note.get("kind") in INV_NOTICE_KINDS
    )


def _pair_unread_count(user_id: str, my_iid: str, other_iid: str) -> int:
    pair = pair_key(my_iid or "", other_iid or "")
    last_read = store.last_message_read(user_id, pair)
    return sum(
        1
        for msg in pool.get_messages(my_iid or "", other_iid or "")
        if msg.get("from_user") != user_id
        and str(msg.get("created_at", "")) > last_read
    )


def _group_unread_count(user_id: str, group_id: str) -> int:
    group = store.get_group(group_id) or {}
    last_read = store.last_group_read(user_id, group_id)
    return sum(
        1
        for msg in group.get("messages") or []
        if msg.get("from_user") != user_id
        and str(msg.get("created_at", "")) > last_read
    )


def _direct_unread_count(user_id: str, peer_id: str) -> int:
    last_read = store.last_direct_read(user_id, peer_id)
    return sum(
        1
        for msg in store.get_direct_messages(user_id, peer_id)
        if msg.get("from_user") != user_id
        and str(msg.get("created_at", "")) > last_read
    )


def _friend_unread_count(user_id: str, peer_id: str) -> int:
    total = _direct_unread_count(user_id, peer_id)
    for inv in _accepted_invitations(user_id):
        other = inv.get("to_user") if inv.get("from_user") == user_id else inv.get("from_user")
        if other != peer_id:
            continue
        _, my_iid, other_iid = _opposite(inv, user_id)
        if my_iid and other_iid:
            total += _pair_unread_count(user_id, my_iid, other_iid)
    return total


def _friend_unread_total(user_id: str) -> int:
    return sum(
        _friend_unread_count(user_id, fid) for fid in store.list_friends(user_id)
    )


def _group_unread_total(user_id: str) -> int:
    return sum(
        _group_unread_count(user_id, g.get("group_id", ""))
        for g in store.list_groups(user_id)
    )


def _history_unread_total(user_id: str) -> int:
    total = 0
    for intent in pool.list_user_intents(user_id):
        if intent.get("status") == "deleted":
            continue
        iid = intent.get("intent_id", "")
        new_items = _new_candidates_for(intent, user_id)
        seen = store.history_seen_count(user_id, iid)
        total += max(0, len(new_items) - seen)
    return total


def _teamed_user_ids(user_id: str):
    """与当前用户已经组队（active team_links 或 accepted 邀请）的所有用户。"""
    ids = set()
    for link in store.list_team_links(user_id):
        other = link.get("user_b") if link.get("user_a") == user_id else link.get("user_a")
        if other and other != user_id:
            ids.add(other)
    for inv in _accepted_invitations(user_id):
        other = inv.get("to_user") if inv.get("from_user") == user_id else inv.get("from_user")
        if other and other != user_id:
            ids.add(other)
    for intent in pool.list_user_intents(user_id):
        for member in store.list_members(intent.get("intent_id", "")):
            if member != user_id:
                ids.add(member)
    for group in store.list_groups(user_id):
        for member in group.get("members") or []:
            if member and member != user_id:
                ids.add(member)
    return ids


def _team_records_for(user_id: str):
    """把用户真实参与的所有队伍聚合出来（含被邀请加入的其他比赛队伍）。"""
    records = {}

    def ensure(key: str, competition: str):
        rec = records.get(key)
        if rec is None:
            rec = {
                "competition": competition or "未命名比赛",
                "user_ids": set(),
                "intents": set(),
                "invitation_ids": [],
                "sources": set(),
            }
            records[key] = rec
        return rec

    for inv in _accepted_invitations(user_id):
        host_iid = inv.get("from_intent") or ""
        host_intent = pool.get_intent(host_iid) or {}
        competition = _competition_display(host_intent.get("competition") or "")
        if not competition:
            other_iid = inv.get("to_intent") or ""
            competition = _competition_display((pool.get_intent(other_iid) or {}).get("competition") or "")
        key = "inv:%s" % (inv.get("invitation_id") or competition)
        rec = ensure(key, competition)
        rec["user_ids"].update(
            x for x in (inv.get("from_user"), inv.get("to_user")) if x
        )
        rec["intents"].update(
            x for x in (inv.get("from_intent"), inv.get("to_intent")) if x
        )
        rec["invitation_ids"].append(inv.get("invitation_id"))
        rec["sources"].add("invitation")

    for link in store.list_team_links(user_id):
        competition = _competition_display(link.get("competition") or "")
        key = "link:%s" % (link.get("link_id") or link.get("invitation_id") or competition)
        rec = ensure(key, competition)
        rec["user_ids"].update(
            x for x in (link.get("user_a"), link.get("user_b")) if x
        )
        rec["intents"].update(
            x for x in (link.get("intent_a"), link.get("intent_b")) if x
        )
        if link.get("invitation_id"):
            rec["invitation_ids"].append(link.get("invitation_id"))
        rec["sources"].add("link")

    # 组队群聊本身也代表一支实际在推进的队伍（例如被邀请加入的其他比赛队伍）
    for group in store.list_groups(user_id):
        members = [x for x in (group.get("members") or []) if pool.get_user(x)]
        competition = _competition_display(group.get("competition") or "")
        if len(members) < 2 or not competition:
            continue
        key = "group:%s" % (group.get("group_id") or competition)
        rec = ensure(key, competition)
        rec["user_ids"].update(members)
        rec["sources"].add("group")

    # 同一比赛里互相重叠的邀请/组队关系合并成同一支队伍
    merged = list(records.values())
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i], merged[j]
                if a.get("competition") != b.get("competition"):
                    continue
                if not (a["user_ids"] & b["user_ids"]):
                    continue
                a["user_ids"] |= b["user_ids"]
                a["intents"] |= b["intents"]
                a["invitation_ids"] += b["invitation_ids"]
                a["sources"] |= b["sources"]
                merged.pop(j)
                changed = True
                break
            if changed:
                break

    for rec in merged:
        rec["user_ids"].add(user_id)
        rec["members"] = [
            uid for uid in rec["user_ids"] if pool.get_user(uid)
        ]
    return sorted(
        merged,
        key=lambda r: (-len(r["members"]), r.get("competition") or ""),
    )


def _friend_competition_label(friend_id: str) -> str:
    """好友昵称后展示与 TA 组建的比赛名，找不到就留空。"""
    names = []
    for rec in _team_records_for(current_uid):
        if friend_id in rec.get("user_ids", set()):
            name = _competition_display(rec.get("competition") or "")
            if name and name not in names:
                names.append(name)
    return "、".join(names[:2])


def render_flash_and_dialogs() -> None:
    _get_flash()
    if st.session_state.get("need_avatar_confirm"):
        post_login_avatar_dialog()
        return
    request = st.session_state.get("dialog_request") or {}
    kind = request.get("kind")
    arg = request.get("arg")
    if kind == "cancel_team":
        cancel_team_dialog(arg)
    elif kind == "feedback":
        feedback_dialog()
    elif kind == "delete_user":
        delete_user_dialog(arg)


def open_dialog(kind: str, arg=None) -> None:
    st.session_state.dialog_request = {"kind": kind, "arg": arg}
    st.rerun()





def _remove_intent_from_pool(intent_id: str, reason: str) -> None:
    intent = pool.get_intent(intent_id)
    if intent is None:
        return
    intent["status"] = "removed"
    intent["excluded_reason"] = reason
    intent["updated_at"] = now_str()
    pool._save_intents()


def _competition_range_for(intent: dict):
    return matcher.get_competition_range(
        intent.get("competition", ""),
        table,
        intent.get("competition_range"),
    )


def _effective_team_size(intent_id: str, intent: dict) -> int:
    try:
        base = int(intent.get("team_size") or 1)
    except (TypeError, ValueError):
        base = 1
    return base + len(store.list_members(intent_id))


def _pending_invite_count(intent_id: str) -> int:
    return sum(
        1
        for inv in pool.invitations.values()
        if inv.get("from_intent") == intent_id and inv.get("status") == "pending"
    )


def evaluate_team_threshold(intent_id: str, user_id: str) -> None:
    intent = pool.get_intent(intent_id)
    if intent is None or intent.get("status") != "matching":
        return
    comp_range = _competition_range_for(intent)
    high = int(comp_range.get("max", 5))
    effective = _effective_team_size(intent_id, intent)
    competition = _competition_display(intent.get("competition", ""))

    if effective >= high:
        intent["status"] = "teamed"
        intent["team_confirmed"] = True
        intent["excluded_reason"] = "队伍已满员，已自动移出参赛池"
        intent["updated_at"] = now_str()
        pool._save_intents()
        user = pool.get_user(user_id)
        if user is not None:
            user["is_teamed"] = True
            user["updated_at"] = now_str()
            pool._save_users()
        store.notify(
            user_id,
            "队伍已满员（%d/%d），已自动移出参赛池。" % (effective, high),
            "max_reached",
            {"intent_id": intent_id, "competition": competition, "count": effective},
        )


def delete_user_record(target_uid: str) -> None:
    user = pool.get_user(target_uid) or {}
    if not user:
        return
    if _is_super_admin(user) or _is_admin(target_uid):
        return
    pool.users.pop(target_uid, None)
    pool._save_users()
    for intent in pool.intents.values():
        if intent.get("user_id") == target_uid:
            intent["status"] = "deleted"
            intent["excluded_reason"] = "用户已注销"
            intent["updated_at"] = now_str()
    pool._save_intents()
    for inv in pool.invitations.values():
        if target_uid in (inv.get("from_user"), inv.get("to_user")):
            inv["status"] = "cancelled"
            inv["updated_at"] = now_str()
    pool._save_invitations()
    store.cleanup_user(target_uid)


@st.dialog("注销用户")
def delete_user_dialog(target_uid: str) -> None:
    user = pool.get_user(target_uid) or {}
    if _is_super_admin(user) or _is_admin(target_uid):
        st.error("管理员身份的用户不能被注销。")
        return
    st.markdown(
        "确定要注销用户 **%s**（%s）并删除其资料吗？"
        % (_esc(user.get("nickname") or "未命名"), _esc(user.get("phone") or "无手机号"))
    )
    st.caption("注销后该用户的参赛意向会从参赛池移除，相关邀请会取消。")
    c1, c2 = st.columns(2)
    if c1.button("确认注销", use_container_width=True, type="primary"):
        st.session_state.dialog_request = None
        delete_user_record(target_uid)
        _set_flash("已注销该用户并删除资料。")
        st.rerun()
    if c2.button("取消", use_container_width=True):
        st.session_state.dialog_request = None
        st.rerun()


@st.dialog("伙伴资料", width="large")
def profile_dialog(user_id: str, intent_id: str = "") -> None:
    user = pool.get_user(user_id)
    if user is None:
        st.error("用户不存在或已注销。")
        return
    intent = pool.get_intent(intent_id) if intent_id else _latest_intent(user_id)
    intent = intent or {}
    st.markdown(
        '<div class="account-head">%s<div><div class="name">%s</div>'
        '<div class="small-muted">%s</div></div></div>'
        % (
            _avatar_html(user, 64),
            _esc(user.get("nickname") or "未命名"),
            _esc(_competition_display(intent.get("competition")) or "暂无参赛意向"),
        ),
        unsafe_allow_html=True,
    )
    period = intent.get("available_period") or {}
    period_text = ""
    if isinstance(period, dict):
        period_text = str(period.get("raw") or "")
        if period.get("end_date"):
            period_text += "（截止 %s）" % period.get("end_date")
    rows = [
        ("昵称", user.get("nickname") or "未填写"),
        ("手机号", user.get("phone") or "未填写"),
        ("联系方式", _user_contact(user) or "未填写"),
        ("QQ", user.get("qq") or "未填写"),
        ("微信", user.get("wechat") or "未填写"),
        ("邮箱", user.get("email") or "未填写"),
        ("专业", user.get("major") or "未填写"),
        ("学校", user.get("school") or "未填写"),
        ("掌握技术", "、".join(intent.get("skills") or []) or "未填写"),
        ("希望队友擅长", "、".join(intent.get("wanted_skills") or []) or "未填写"),
        ("已有几人", "%s 人" % intent.get("team_size", "未填写")),
        ("可组队时间段", period_text or "未填写"),
        ("空闲时间", "、".join(user.get("free_time") or []) or "未填写"),
        ("性别偏好", user.get("gender_preference") or "未填写"),
        ("兴趣方向", "、".join(user.get("interests") or []) or "未填写"),
        ("希望担任角色", intent.get("preferred_role") or "未填写"),
        (
            "是否已有项目创意",
            "未填写"
            if intent.get("has_idea") is None
            else ("是" if intent.get("has_idea") else "否"),
        ),
        (
            "是否愿意线下见面",
            "未填写"
            if intent.get("willing_offline") is None
            else ("是" if intent.get("willing_offline") else "否"),
        ),
    ]
    st.markdown(_html_table(rows), unsafe_allow_html=True)


@st.dialog("取消组队")
def cancel_team_dialog(invitation_id: str) -> None:
    inv = pool.get_invitation(invitation_id) or {}
    other_uid, _, other_iid = _opposite(inv, current_uid)
    other = pool.get_user(other_uid) or {}
    other_intent = pool.get_intent(other_iid) or {}
    st.markdown(
        "确定要解散与『%s』（%s）的组队吗？"
        % (
            _esc(other.get("nickname") or "未命名"),
            _esc(_competition_display(other_intent.get("competition") or "")),
        )
    )
    c1, c2 = st.columns(2)
    if c1.button("确认解散", use_container_width=True, type="primary"):
        st.session_state.dialog_request = None
        dissolve_team(inv)
        _set_flash("已解散组队，已回到参赛池继续匹配。")
        st.rerun()
    if c2.button("再想想", use_container_width=True):
        st.session_state.dialog_request = None
        st.rerun()


def dissolve_team(inv: dict) -> None:
    if not inv:
        return
    inv_id = inv.get("invitation_id")
    inv["status"] = "cancelled"
    inv["updated_at"] = now_str()
    pool._save_invitations()
    from_uid = inv.get("from_user")
    to_uid = inv.get("to_user")
    from_iid = inv.get("from_intent")
    to_iid = inv.get("to_intent")
    store.remove_member(from_iid, to_uid)
    store.remove_member(to_iid, from_uid)
    store.cancel_team_link(inv_id)
    store.clear_min_prompted(from_iid)
    store.clear_min_prompted(to_iid)
    for iid in (from_iid, to_iid):
        intent = pool.get_intent(iid)
        if intent is not None and intent.get("status") in ("removed", "teamed"):
            intent["status"] = "matching"
            intent["excluded_reason"] = None
            intent["team_confirmed"] = False
            intent["updated_at"] = now_str()
    pool._save_intents()
    other_uid = to_uid if from_uid == current_uid else from_uid
    if other_uid:
        store.notify(
            other_uid,
            "与『%s』的组队已解散。"
            % _esc((pool.get_user(current_uid) or {}).get("nickname") or "对方"),
            "team_cancelled",
            {"invitation_id": inv_id},
        )
    st.session_state.pop("conversation_choice", None)


def accept_invitation(inv: dict) -> None:
    inv_id = inv.get("invitation_id")
    if not inv_id:
        return
    pool.respond_invitation(inv_id, True, current_uid)
    inv = pool.get_invitation(inv_id) or inv
    inv.setdefault("team_confirm", {})
    inv["team_confirm"][inv.get("from_user")] = True
    inv["team_confirm"][inv.get("to_user")] = True
    pool._save_invitations()

    from_uid = inv.get("from_user")
    to_uid = inv.get("to_user")
    from_iid = inv.get("from_intent")
    to_iid = inv.get("to_intent")
    store.add_friend(from_uid, to_uid)
    store.add_member(from_iid, to_uid)
    store.add_member(to_iid, from_uid)
    competition = _competition_display(
        (pool.get_intent(from_iid) or {}).get("competition", "")
    )
    store.create_team_link(inv, competition)
    store.notify(
        from_uid,
        "『%s』已同意你的邀请，你们已正式组队并成为好友。"
        % ((pool.get_user(to_uid) or {}).get("nickname") or "对方"),
        "team_accepted",
        {"invitation_id": inv_id},
    )
    evaluate_team_threshold(from_iid, from_uid)
    evaluate_team_threshold(to_iid, to_uid)
    _set_flash("已同意邀请，正式组队成功，并已成为好友。")


def reject_invitation(inv: dict) -> None:
    inv_id = inv.get("invitation_id")
    if not inv_id:
        return
    pool.respond_invitation(inv_id, False, current_uid)
    store.notify(
        inv.get("from_user"),
        "『%s』拒绝了你的组队邀请。"
        % ((pool.get_user(current_uid) or {}).get("nickname") or "对方"),
        "invite_rejected",
        {"invitation_id": inv_id},
    )
    _set_flash("已拒绝该邀请。")


def send_invitation(host_iid: str, cand_iid: str, message: str = "") -> dict:
    host_intent = pool.get_intent(host_iid) or {}
    comp_range = _competition_range_for(host_intent)
    effective = _effective_team_size(host_iid, host_intent)
    if effective >= int(comp_range.get("max", 5)):
        evaluate_team_threshold(host_iid, host_intent.get("user_id"))
        raise ValueError("队伍已满员，已自动移出参赛池。")
    inv = pool.create_invitation(host_iid, cand_iid)
    message = str(message or "").strip()[:100]
    if message:
        inv["message"] = message
    if not inv.get("_notified"):
        host_uid = host_intent.get("user_id")
        cand_intent = pool.get_intent(cand_iid) or {}
        cand_uid = cand_intent.get("user_id")
        store.add_friend(host_uid, cand_uid)
        store.notify(
            cand_uid,
            "『%s』向你发送了组队邀请，可前往“邀请通知”查看。"
            % ((pool.get_user(host_uid) or {}).get("nickname") or "对方"),
            "invite",
            {"invitation_id": inv.get("invitation_id")},
        )
        inv["_notified"] = True
    pool._save_invitations()
    return inv


# ----------------------------------------------------------------------
# 左侧导航（我的账号 + 功能块）
# ----------------------------------------------------------------------
NAV_TEAM_KINDS = {
    "team_accepted",
    "team_cancelled",
    "max_reached",
    "admin_invite",
}


def _nav_badges():
    return {
        "friends": _friend_unread_total(current_uid),
        "groups": _group_unread_total(current_uid),
        "team": sum(
            1
            for note in store.unread_notifications(current_uid)
            if note.get("kind") in NAV_TEAM_KINDS
        ),
        "history": _history_unread_total(current_uid),
        "notices": _invitation_unread_count(current_uid)
        + _notification_unread_count(current_uid),
    }


def render_sidebar() -> str:
    page = st.session_state.get("page", "chat")
    if page in ("inv", "chat"):
        page = "chat"
    elif page == "admin" and not current_is_admin:
        page = "chat"
    st.session_state.page = page

    badges = _nav_badges()
    with st.sidebar:
        st.markdown(
            '<div class="sidebar-section-title">我的账号</div>',
            unsafe_allow_html=True,
        )
        admin_tag = '<span class="admin-tag">管理员</span>' if current_is_admin else ""
        st.markdown(
            '<div class="account-card">'
            '<div class="account-row">%s'
            '<div><div class="name-line"><span class="name">%s</span>%s</div></div>'
            '</div>'
            '<div class="phone-line">手机号：%s</div>'
            '</div>'
            % (
                _avatar_html(current_user, 56),
                _esc(current_user.get("nickname") or "未命名"),
                admin_tag,
                _esc(current_user.get("phone") or "未填写手机号"),
            ),
            unsafe_allow_html=True,
        )

        items = [
            ("friends", "好友列表", badges.get("friends", 0)),
            ("groups", "群聊", badges.get("groups", 0)),
            ("team", "我的组队", badges.get("team", 0)),
            ("history", "历史参赛信息", badges.get("history", 0)),
            ("notices", "邀请通知", badges.get("notices", 0)),
        ]
        if current_is_admin:
            items.append(("admin", "管理后台", 0))

        for key, label, badge in items:
            text = label + ("  🔴%d" % badge if badge else "")
            active = page == key
            btn_key = "navbtn_%s%s" % (key, "_active" if active else "")
            if st.button(text, key=btn_key, use_container_width=True):
                st.session_state.page = key
                st.rerun()

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
        with st.expander("更换头像"):
            side_upload = st.file_uploader(
                "上传头像",
                type=["png", "jpg", "jpeg", "webp"],
                key="side_avatar",
            )
            if side_upload is not None:
                side_data = _read_avatar_upload(side_upload)
                if side_data:
                    signature = hashlib.md5(side_data.encode("utf-8")).hexdigest()
                    if st.session_state.get("side_avatar_sig") != signature:
                        _save_avatar(current_uid, side_data)
                        st.session_state.side_avatar_sig = signature
                        _set_flash("头像已更新。")
                        st.rerun()
        if st.button("退出登录", use_container_width=True, key="logout_btn"):
            logout()

    return st.session_state.get("page", "chat")


def _go_back_to_chat() -> None:
    st.session_state.page = "chat"


def render_back_to_chat() -> None:
    if st.session_state.get("page", "chat") == "chat":
        return
    st.button(
        "← 回到智能体对话",
        key="back_to_chat",
        use_container_width=False,
        on_click=_go_back_to_chat,
    )


# ----------------------------------------------------------------------
# 对话与匹配
# ----------------------------------------------------------------------
DISPLAY_FIELDS = [
    ("nickname", "昵称（登录账号）"),
    ("competition", "比赛名称"),
    ("qq", "QQ"),
    ("wechat", "微信"),
    ("email", "邮箱"),
    ("contact", "联系方式（合并展示）"),
    ("major", "专业"),
    ("skills", "掌握技术"),
    ("wanted_skills", "希望队友擅长"),
    ("team_size", "已有几人"),
    ("school", "学校"),
    ("available_period", "可组队时间段"),
    ("free_time", "空闲时间"),
    ("gender_preference", "性别偏好"),
    ("interests", "兴趣方向"),
    ("preferred_role", "希望担任角色"),
    ("has_idea", "是否已有项目创意"),
    ("willing_offline", "是否愿意线下见面"),
]


def _is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _join_text(value) -> str:
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(x) for x in value)
    return str(value or "")


def _tri_index(value) -> int:
    if value is True:
        return 1
    if value is False:
        return 2
    return 0


def _tri_value(label: str):
    return {"不填": None, "是": True, "否": False}.get(label)


def _tri_label(value) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "不填"


def _csv_text(value) -> str:
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(x) for x in value if str(x).strip())
    return str(value or "")


def _date_value(value, default: date) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    return default


def _display_value(key: str, value) -> str:
    if key in ("skills", "wanted_skills", "free_time", "interests"):
        return "、".join(value) if value else "未填写"
    if key == "available_period":
        return _period_display(value)
    if key in ("has_idea", "willing_offline"):
        if value is True:
            return "是"
        if value is False:
            return "否"
        return "未填写"
    if key == "team_size":
        return "%s 人" % value if value else "未填写"
    return str(value).strip() if not _is_empty(value) else "未填写"


def render_chat_messages() -> None:
    for index, message in enumerate(st.session_state.chat):
        role = message.get("role")
        content = str(message.get("content", ""))
        mine = role == "user"
        key = "chat-%s-main-%d" % ("mine" if mine else "theirs", index)
        with st.container(key=key):
            st.markdown(
                _bubble_html(
                    content,
                    _avatar_src(current_user) if mine else DEFAULT_AVATAR,
                    _user_name(current_user) if mine else "组队助手",
                    mine=mine,
                    avatar_size=44,
                ),
                unsafe_allow_html=True,
            )


def _popular_competitions_text() -> str:
    names = [
        "中国国际大学生创新大赛（原互联网+，3-15 人）",
        "“挑战杯”中国大学生创业计划竞赛（3-15 人）",
        "全国大学生数学建模竞赛（3 人）",
        "全国大学生电子设计竞赛（3 人）",
        "全国大学生机器人大赛 RoboMaster（5-10 人）",
        "三创赛（3-5 人）",
    ]
    return "当前比较热门的创新类比赛有：\n" + "\n".join("· " + x for x in names) + (
        "\n您想参加哪一场？直接告诉我比赛名称或简称即可，例如“我想参加大创赛”。"
    )


def _looks_like_participation(text: str) -> bool:
    return any(
        k in text
        for k in ("参加", "报名", "参赛", "组队", "匹配", "找队友", "帮我找", "想打", "加入")
    )


def _looks_like_open_question(text: str) -> bool:
    """问“什么是大创赛/大创赛几个人”这类问题时不直接进入填表。"""
    action_markers = (
        "参加",
        "报名",
        "参赛",
        "组队",
        "匹配",
        "找队友",
        "帮我找",
        "想打",
        "加入",
    )
    if any(k in text for k in action_markers):
        return False
    question_markers = (
        "是什么",
        "什么是",
        "介绍",
        "解释",
        "区别",
        "怎么",
        "如何",
        "为什么",
        "有哪些",
        "几个人",
        "多少人",
        "吗",
        "？",
        "?",
    )
    return any(k in text for k in question_markers)


def handle_user_text(text: str) -> None:
    text = str(text or "").strip()
    if not text:
        return
    st.session_state.last_error = None
    append_chat("user", text)

    previous_stage = st.session_state.get("stage", "chatting")
    collected = st.session_state.get("collected") or {}
    new_fields = llm_api.extract_fields(text, collected, st.session_state.chat, table)
    this_competition = str(new_fields.get("competition") or "").strip()
    if this_competition and _looks_like_open_question(text):
        this_competition = ""
    if not this_competition and collected.get("competition"):
        if _looks_like_participation(text) and not _looks_like_open_question(text):
            this_competition = str(collected.get("competition") or "").strip()

    # 只有本轮明确提到比赛时才开始新一轮匹配；开放问题不会打断当前聊天。
    if previous_stage == "matched" and this_competition:
        collected = {}
        st.session_state.match_result = None
        st.session_state.host_intent_id = None
        st.session_state.form_competition = None
        st.session_state.pending_collected = None

    collected = llm_api.merge_fields(collected, new_fields)
    st.session_state.collected = collected

    if not this_competition:
        reply = llm_api.generate_chat_reply(
            text,
            history=st.session_state.chat,
            collected=collected,
            table=table,
        )
        if not reply:
            reply = (
                "我在听～你刚刚说「%s」。想继续聊这个话题，还是想让我帮你推荐比赛、找队友？"
                % text[:40]
            )
        append_chat("assistant", reply)
        if previous_stage == "matched":
            st.session_state.stage = "matched"
        return

    competition = this_competition
    info = matcher.competition_info(competition, table)
    if info is not None:
        competition = info[0]
        collected["competition"] = competition
        st.session_state.collected = collected
        comp_range = matcher.get_competition_range(competition, table)
        st.session_state.form_competition = competition
        st.session_state.form_version = int(st.session_state.get("form_version", 0)) + 1
        st.session_state.stage = "form"
        append_chat(
            "assistant",
            "已识别到「%s」属于创新类比赛，人数区间为 %s-%s 人。"
            "请填写下面的组队需求表单，提交后确认信息即可开始匹配。"
            % (competition, comp_range.get("min"), comp_range.get("max")),
        )
        return

    verdict = llm_api.judge_innovation(competition, table)
    if verdict.get("is_innovation") == "no":
        append_chat(
            "assistant",
            "您说的『%s』看起来不属于创新创业/学术科技/学科竞赛类的比赛哦。"
            "本平台专注为创新类比赛匹配队友（如中国国际大学生创新大赛、挑战杯、数学建模等）。"
            "您要改报一个创新类的比赛吗？我可以帮您匹配队友。" % competition,
        )
        collected.pop("competition", None)
        st.session_state.collected = collected
        st.session_state.stage = "chatting"
        return

    st.session_state.unknown_competition = competition
    st.session_state.stage = "unknown"
    append_chat(
        "assistant",
        "「%s」不在系统已收录的比赛名单中，暂时无法自动确认人数区间。"
        "这个比赛系统暂时没有收录。如果您需要，可以向管理员反馈申请添加。"
        % competition,
    )


@st.dialog("提交比赛收录反馈", width="small")
def feedback_dialog() -> None:
    competition = st.session_state.get("unknown_competition") or ""
    st.caption("提交后管理员会收到反馈，超级管理员也会收到提醒。")
    with st.form("feedback_form"):
        name = st.text_input("比赛名称", value=competition)
        c1, c2 = st.columns(2)
        with c1:
            low = st.number_input("最少人数", min_value=1, max_value=50, value=3)
        with c2:
            high = st.number_input("最多人数", min_value=1, max_value=50, value=5)
        submitted = st.form_submit_button("提交反馈", use_container_width=True, type="primary")
    if st.button("取消反馈", use_container_width=True):
        st.session_state.dialog_request = None
        st.rerun()
    if submitted:
        name = str(name or "").strip()
        if not name:
            st.error("请填写比赛名称。")
            return
        if int(low) > int(high):
            low, high = high, low
        feedback = store.submit_feedback(
            name,
            int(low),
            int(high),
            current_uid,
            current_user.get("nickname") or "",
        )
        admins = set(store.data.get("admins") or [])
        for uid, user in pool.users.items():
            if _is_super_admin(user):
                admins.add(uid)
        for uid in admins:
            if uid == current_uid:
                continue
            store.notify(
                uid,
                "收到新的比赛收录反馈：%s（%s-%s 人），请到管理中心查看。"
                % (name, low, high),
                "competition_feedback",
                {"feedback_id": feedback.get("feedback_id")},
            )
        append_chat(
            "assistant",
            "已把「%s」的人数区间反馈给管理员，超级管理员也会收到提醒。"
            % name,
        )
        st.session_state.unknown_competition = None
        st.session_state.stage = "chatting"
        st.session_state.collected = {}
        st.session_state.dialog_request = None
        _set_flash("反馈已提交，感谢您的补充。")
        st.rerun()


def render_unknown() -> None:
    competition = st.session_state.get("unknown_competition") or ""
    st.warning(
        "「%s」暂时没有收录。您可以向管理员反馈申请添加，"
        "或先用默认人数区间继续匹配。" % competition
    )
    c1, c2, c3 = st.columns([1.2, 1.4, 1.2])
    if c1.button("向管理员反馈", use_container_width=True, type="primary"):
        open_dialog("feedback")
    if c2.button("先按默认 3-5 人继续匹配", use_container_width=True):
        st.session_state.form_competition = competition
        st.session_state.collected["competition"] = competition
        st.session_state.stage = "form"
        st.session_state.form_version = int(st.session_state.get("form_version", 0)) + 1
        st.rerun()
    if c3.button("暂不反馈", use_container_width=True):
        st.session_state.unknown_competition = None
        st.session_state.collected.pop("competition", None)
        st.session_state.stage = "chatting"
        st.rerun()


def render_intent_form() -> None:
    collected = st.session_state.get("collected") or {}
    form_competition = st.session_state.get("form_competition") or collected.get(
        "competition", ""
    )

    st.markdown("---")
    st.subheader("填写组队需求")
    st.caption("带「必填」的项目需要填写；带「（选填）」的项目不填可直接跳过。")
    st.caption("表单内容会自动保留，切走再回来不会丢失。")

    version = int(st.session_state.get("form_version", 0))
    widget_keys = [
        "intent_competition",
        "intent_qq",
        "intent_wechat",
        "intent_email",
        "intent_major",
        "intent_skills",
        "intent_wanted_skills",
        "intent_team_size",
        "intent_school",
        "intent_period_start",
        "intent_period_end",
        "intent_free_time",
        "intent_gender",
        "intent_interests",
        "intent_role",
        "intent_has_idea",
        "intent_offline",
    ]
    if st.session_state.get("intent_form_state_version") != version:
        for key in widget_keys:
            st.session_state.pop(key, None)
        st.session_state.intent_form_state_version = version
        st.session_state.intent_form_draft = {}

    draft = st.session_state.get("intent_form_draft") or {}
    period_draft = draft.get("available_period") or collected.get("available_period") or {}

    defaults = {
        "competition": draft.get("competition", form_competition),
        "qq": draft.get("qq", collected.get("qq", "")),
        "wechat": draft.get("wechat", collected.get("wechat", "")),
        "email": draft.get("email", collected.get("email", "")),
        "major": draft.get("major", collected.get("major", "")),
        "skills": draft.get("skills", _csv_text(collected.get("skills"))),
        "wanted_skills": draft.get(
            "wanted_skills", _csv_text(collected.get("wanted_skills"))
        ),
        "team_size": draft.get("team_size", collected.get("team_size", 1)),
        "school": draft.get("school", collected.get("school", "")),
        "free_time": draft.get("free_time", _csv_text(collected.get("free_time"))),
        "gender_preference": draft.get(
            "gender_preference", collected.get("gender_preference", "")
        ),
        "interests": draft.get("interests", _csv_text(collected.get("interests"))),
        "preferred_role": draft.get(
            "preferred_role", collected.get("preferred_role", "")
        ),
        "has_idea": draft.get("has_idea", collected.get("has_idea")),
        "willing_offline": draft.get(
            "willing_offline", collected.get("willing_offline")
        ),
    }

    def ensure_widget(key: str, default) -> None:
        if key not in st.session_state:
            st.session_state[key] = default

    ensure_widget("intent_competition", str(defaults["competition"] or ""))
    ensure_widget("intent_qq", str(defaults["qq"] or ""))
    ensure_widget("intent_wechat", str(defaults["wechat"] or ""))
    ensure_widget("intent_email", str(defaults["email"] or ""))
    ensure_widget("intent_major", str(defaults["major"] or ""))
    ensure_widget("intent_skills", str(defaults["skills"] or ""))
    ensure_widget("intent_wanted_skills", str(defaults["wanted_skills"] or ""))
    try:
        team_size_default = int(defaults["team_size"] or 1)
    except (TypeError, ValueError):
        team_size_default = 1
    ensure_widget("intent_team_size", max(1, min(20, team_size_default)))
    ensure_widget("intent_school", str(defaults["school"] or ""))
    ensure_widget(
        "intent_period_start",
        _date_value(
            draft.get("period_start") or period_draft.get("start_date"),
            date.today(),
        ),
    )
    ensure_widget(
        "intent_period_end",
        _date_value(
            draft.get("period_end") or period_draft.get("end_date"),
            llm_api.default_period_end(),
        ),
    )
    ensure_widget("intent_free_time", str(defaults["free_time"] or ""))
    ensure_widget("intent_gender", str(defaults["gender_preference"] or ""))
    ensure_widget("intent_interests", str(defaults["interests"] or ""))
    ensure_widget("intent_role", str(defaults["preferred_role"] or ""))
    ensure_widget("intent_has_idea", _tri_label(defaults["has_idea"]))
    ensure_widget("intent_offline", _tri_label(defaults["willing_offline"]))

    with st.container():
        st.markdown("##### 必填项")
        competition = st.text_input(
            "比赛名称（必填）",
            key="intent_competition",
            placeholder="例如：大创赛",
        )
        st.markdown("**联系方式（必填，QQ / 微信 / 邮箱至少填写一项）**")
        cq, cw, ce = st.columns(3)
        with cq:
            qq = st.text_input("QQ", key="intent_qq", placeholder="例如：123456789")
        with cw:
            wechat = st.text_input(
                "微信", key="intent_wechat", placeholder="例如：xiaolin_2026"
            )
        with ce:
            email = st.text_input(
                "邮箱", key="intent_email", placeholder="例如：name@example.com"
            )
        major = st.text_input(
            "专业（必填）", key="intent_major", placeholder="例如：数据科学与大数据技术"
        )
        skills = st.text_input(
            "掌握技术（必填）",
            key="intent_skills",
            placeholder="Python、算法、数据分析",
        )
        wanted_skills = st.text_input(
            "希望队友擅长（必填）",
            key="intent_wanted_skills",
            placeholder="例如：文档写作、PPT、UI设计",
        )
        c1, c2 = st.columns([1, 2])
        with c1:
            team_size_input = st.number_input(
                "已有几人（必填）",
                min_value=1,
                max_value=20,
                step=1,
                key="intent_team_size",
            )
        with c2:
            school = st.text_input(
                "学校（必填）",
                key="intent_school",
                placeholder="例如：湖南第一师范学院",
            )
        st.markdown("**可组队时间段（必填）**")
        pc1, pc2 = st.columns(2)
        with pc1:
            period_start = st.date_input("开始日期", key="intent_period_start")
        with pc2:
            period_end = st.date_input("结束日期", key="intent_period_end")
        period_input = ""
        if period_start and period_end:
            period_input = "%s 到 %s" % (
                period_start.strftime("%Y-%m-%d"),
                period_end.strftime("%Y-%m-%d"),
            )

        st.markdown("##### 选填项（不填可直接跳过）")
        free_time = st.text_input(
            "空闲时间（选填）", key="intent_free_time", placeholder="例如：周三晚上、周末"
        )
        gender_preference = st.text_input(
            "性别偏好（选填）", key="intent_gender", placeholder="例如：不限 / 男 / 女"
        )
        interests = st.text_input(
            "兴趣方向（选填）", key="intent_interests", placeholder="例如：人工智能、产品设计、写作"
        )
        preferred_role = st.text_input(
            "希望担任角色（选填）", key="intent_role", placeholder="例如：算法与数据分析 / 文档与汇报"
        )
        c3, c4 = st.columns(2)
        with c3:
            has_idea_label = st.selectbox(
                "是否已有项目创意（选填）",
                ["不填", "是", "否"],
                key="intent_has_idea",
            )
        with c4:
            offline_label = st.selectbox(
                "是否愿意线下见面（选填）",
                ["不填", "是", "否"],
                key="intent_offline",
            )

        submitted = st.button(
            "提交并生成确认信息", use_container_width=True, type="primary"
        )

    st.session_state.intent_form_draft = {
        "competition": str(competition or ""),
        "qq": str(qq or ""),
        "wechat": str(wechat or ""),
        "email": str(email or ""),
        "major": str(major or ""),
        "skills": str(skills or ""),
        "wanted_skills": str(wanted_skills or ""),
        "team_size": int(team_size_input),
        "school": str(school or ""),
        "free_time": str(free_time or ""),
        "gender_preference": str(gender_preference or ""),
        "interests": str(interests or ""),
        "preferred_role": str(preferred_role or ""),
        "has_idea": _tri_value(has_idea_label),
        "willing_offline": _tri_value(offline_label),
        "period_start": period_start.isoformat() if period_start else "",
        "period_end": period_end.isoformat() if period_end else "",
        "available_period": {
            "raw": period_input,
            "start_date": period_start.isoformat() if period_start else "",
            "end_date": period_end.isoformat() if period_end else "",
        },
    }

    if not submitted:
        return

    missing = []
    if not str(competition or "").strip():
        missing.append("比赛名称")
    if not any(str(x or "").strip() for x in (qq, wechat, email)):
        missing.append("联系方式（QQ / 微信 / 邮箱至少一项）")
    if not str(major or "").strip():
        missing.append("专业")
    cleaned_skills = clean_skills(skills)
    cleaned_wanted = clean_skills(wanted_skills)
    if not cleaned_skills:
        missing.append("掌握技术")
    if not cleaned_wanted:
        missing.append("希望队友擅长")
    if not str(school or "").strip():
        missing.append("学校")
    if not str(period_input or "").strip():
        missing.append("可组队时间段")
    if missing:
        st.error("请填写以下必填项：%s。" % "、".join(missing))
        return
    if period_start and period_end and period_start > period_end:
        st.error("结束日期不能早于开始日期，请重新选择。")
        return

    period_value = llm_api.parse_period_text(str(period_input).strip())
    if not period_value.get("end_date"):
        period_value["end_date"] = llm_api.default_period_end().strftime("%Y-%m-%d")
    if not period_value.get("start_date"):
        period_value["start_date"] = date.today().strftime("%Y-%m-%d")

    contact = build_contact(qq, wechat, email)
    pending = {
        "nickname": current_user.get("nickname", ""),
        "competition": str(competition).strip(),
        "qq": str(qq or "").strip(),
        "wechat": str(wechat or "").strip(),
        "email": str(email or "").strip(),
        "contact": contact,
        "contact_type": contact_type_of(contact),
        "major": str(major).strip(),
        "skills": cleaned_skills,
        "wanted_skills": cleaned_wanted,
        "team_size": int(team_size_input),
        "school": str(school).strip(),
        "available_period": period_value,
        "free_time": dedupe_list(free_time),
        "gender_preference": str(gender_preference or "").strip(),
        "interests": dedupe_list(interests),
        "preferred_role": str(preferred_role or "").strip(),
        "has_idea": _tri_value(has_idea_label),
        "willing_offline": _tri_value(offline_label),
    }
    st.session_state.pending_collected = pending
    st.session_state.stage = "confirm"
    st.rerun()


def render_confirmation() -> None:
    data = st.session_state.get("pending_collected") or {}
    st.markdown("---")
    st.subheader("请确认您的组队信息")
    st.caption("确认无误后点击“确认，开始匹配”；如需调整可返回修改。")

    rows = []
    for key, label in DISPLAY_FIELDS:
        if key == "contact" and not data.get("contact"):
            continue
        rows.append((label, _display_value(key, data.get(key))))
    st.markdown(_html_table(rows), unsafe_allow_html=True)

    c1, c2 = st.columns([1, 1])
    if c1.button("确认，开始匹配", type="primary", use_container_width=True):
        finalize_matching()
        st.rerun()
    if c2.button("返回修改", use_container_width=True):
        st.session_state.collected = dict(data)
        for key in (
            "intent_competition",
            "intent_qq",
            "intent_wechat",
            "intent_email",
            "intent_major",
            "intent_skills",
            "intent_wanted_skills",
            "intent_team_size",
            "intent_school",
            "intent_period_start",
            "intent_period_end",
            "intent_free_time",
            "intent_gender",
            "intent_interests",
            "intent_role",
            "intent_has_idea",
            "intent_offline",
        ):
            st.session_state.pop(key, None)
        st.session_state.stage = "form"
        st.rerun()


def _dedupe_items_by_user(items) -> list:
    """同一用户只保留匹配度最高的一条。"""
    best = {}
    for item in items or []:
        uid = str(item.get("user_id") or "")
        if not uid:
            continue
        try:
            score = float(item.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        if uid not in best or score > float(best[uid].get("score", 0) or 0):
            best[uid] = item
    return sorted(best.values(), key=lambda x: -float(x.get("score", 0) or 0))


def _snapshot_items(result: dict):
    items = []
    for item in _dedupe_items_by_user(
        list(result.get("recommended", [])) + list(result.get("alternatives", []))
    ):
        items.append(
            {
                "intent_id": item.get("intent_id"),
                "user_id": item.get("user_id"),
                "score": item.get("score", 0),
                "nickname": item.get("nickname", ""),
                "competition": item.get("competition", ""),
            }
        )
    return items


def finalize_matching() -> None:
    collected = st.session_state.get("pending_collected") or {}
    missing = matcher.missing_required(collected)
    if missing:
        append_chat(
            "assistant",
            "还缺少以下信息，暂时不能匹配：%s。"
            % "、".join(label for _, label in missing),
        )
        st.session_state.stage = "form"
        return

    competition = _normalize_competition_name(
        str(collected.get("competition", "")).strip()
    )
    collected["competition"] = competition
    comp_range = matcher.get_competition_range(competition, table)
    contact = str(collected.get("contact", "")).strip()
    user_data = {
        "nickname": collected.get("nickname", ""),
        "contact": contact,
        "contact_type": matcher.detect_contact_type(contact),
        "qq": collected.get("qq", ""),
        "wechat": collected.get("wechat", ""),
        "email": collected.get("email", ""),
        "school": collected.get("school", ""),
        "major": collected.get("major", ""),
        "free_time": collected.get("free_time", []),
        "gender_preference": collected.get("gender_preference", ""),
        "interests": collected.get("interests", []),
    }
    user_id = pool.update_user(current_uid, user_data)

    intent_data = {
        "competition": competition,
        "is_innovation": True,
        "skills": collected.get("skills", []),
        "wanted_skills": collected.get("wanted_skills", []),
        "team_size": collected.get("team_size", 1),
        "available_period": collected.get("available_period"),
        "preferred_role": collected.get("preferred_role", ""),
        "has_idea": collected.get("has_idea"),
        "willing_offline": collected.get("willing_offline"),
        "competition_range": comp_range,
    }
    intent_id = pool.create_intent(user_id, intent_data)

    pool.expire_intents()
    inverted = pool.build_inverted_index()
    result = matcher.find_candidates(
        pool.get_intent(intent_id),
        pool.get_user(user_id),
        pool.list_matching_intents(),
        pool.users,
        table,
        competition_range=comp_range,
        inverted_index=inverted,
    )
    store.save_snapshot(intent_id, _snapshot_items(result))

    st.session_state.collected = collected
    st.session_state.pending_collected = None
    st.session_state.host_intent_id = intent_id
    st.session_state.match_result = result
    st.session_state.stage = "matched"

    rec_count = len(result.get("recommended", []))
    alt_count = len(result.get("alternatives", []))
    summary = (
        "已把您的资料存入参赛池。按「%s」的人数区间 %s-%s 人，"
        "您已有 %s 人，还差 %s 人。"
        % (
            competition,
            comp_range.get("min"),
            comp_range.get("max"),
            result.get("host_team_size"),
            result.get("needed"),
        )
    )
    if rec_count or alt_count:
        summary += (
            " 共找到 %d 位符合硬约束的候选人：推荐 %d 位、备选 %d 位。"
            " 联系方式已直接展示在卡片里，也可以发送邀请后站内聊天。"
            % (rec_count + alt_count, rec_count, alt_count)
        )
    else:
        summary += " 当前暂无匹配对象，您的资料已存入参赛池，之后其他用户可能匹配到您。"
    append_chat("assistant", summary)


def render_candidate(item: dict, host_intent_id: str, group: str) -> None:
    cand_user = pool.get_user(item.get("user_id", "")) or {}
    with st.container(border=True):
        left, right = st.columns([5, 1])
        with left:
            st.markdown(
                '<div class="account-head">%s<div><div class="name">%s</div>'
                '<div class="small-muted">%s</div></div></div>'
                % (
                    _avatar_html(cand_user, 46),
                    _esc(item.get("nickname") or "未命名"),
                    _esc(item.get("intro", "")),
                ),
                unsafe_allow_html=True,
            )
        with right:
            st.metric("匹配度", "%.1f%%" % float(item.get("score", 0)))
        st.progress(min(100, int(round(float(item.get("score", 0))))))

        contact = _user_contact(cand_user) or item.get("contact") or "未提供"
        st.markdown(
            '<div class="contact-box"><b>联系方式：</b>%s</div>'
            % _esc(contact),
            unsafe_allow_html=True,
        )

        detail_bits = []
        if item.get("preferred_role"):
            detail_bits.append("希望担任：%s" % item["preferred_role"])
        if item.get("free_time"):
            detail_bits.append("空闲时间：%s" % "、".join(item["free_time"]))
        if item.get("interests"):
            detail_bits.append("兴趣：%s" % "、".join(item["interests"]))
        if item.get("has_idea") is not None:
            detail_bits.append("已有创意：%s" % ("是" if item["has_idea"] else "否"))
        if item.get("willing_offline") is not None:
            detail_bits.append(
                "愿意线下：%s" % ("是" if item["willing_offline"] else "否")
            )
        if item.get("combined_size"):
            detail_bits.append("合并后人数：%s" % item["combined_size"])
        if detail_bits:
            st.caption(" · ".join(detail_bits))

        c1, c2 = st.columns([1, 1.4])
        if c1.button(
            "查看资料",
            key="profile_%s_%s" % (group, item.get("intent_id")),
            use_container_width=True,
        ):
            profile_dialog(item.get("user_id", ""), item.get("intent_id", ""))
        with c2:
            with st.expander("查看匹配明细（各维度得分与权重）"):
                rows = []
                for label, info in (item.get("breakdown") or {}).items():
                    rows.append(
                        {
                            "维度": label,
                            "得分": "%.0f%%" % (float(info["score"]) * 100)
                            if info.get("applied")
                            else "未填",
                            "权重": "%.0f%%" % (float(info["weight"]) * 100),
                            "是否计入": "计入" if info.get("applied") else "不计入",
                        }
                    )
                st.table(rows)
                st.caption(
                    "匹配分 = Σ(维度得分 × 权重) / Σ(实际计入权重) × 100；"
                    "未填的可选维度不计入。"
                )

        inv = pool.get_invitation_between(host_intent_id, item.get("intent_id", ""))
        if inv and inv.get("status") == "accepted":
            st.success("已组队：可在“好友列表”里聊天。")
        elif inv and inv.get("status") == "pending":
            st.info("邀请已发送，等待对方同意。")
        elif inv and inv.get("status") == "rejected":
            st.warning("对方已拒绝上次邀请，可以再次发送邀请。")
        if not inv or inv.get("status") in ("rejected", "cancelled"):
            msg_key = "invmsg_%s_%s" % (group, item.get("intent_id"))
            msg = st.text_input(
                "邀请留言（选填，100 字以内）",
                key=msg_key,
                max_chars=100,
                placeholder="简单说一下分工、时间安排或想确认的信息...",
            )
            if st.button(
                "发送邀请",
                key="invite_%s_%s" % (group, item.get("intent_id")),
                use_container_width=True,
            ):
                try:
                    send_invitation(host_intent_id, item.get("intent_id", ""), msg)
                    _set_flash("邀请已发送，并已添加为好友。")
                    st.rerun()
                except Exception as exc:
                    st.error("发送失败：%s" % exc)


def render_match_results() -> None:
    result = st.session_state.get("match_result")
    if not result:
        return
    rng = result.get("range", {})
    st.markdown("---")
    st.subheader("匹配结果")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("比赛人数区间", "%s-%s 人" % (rng.get("min"), rng.get("max")))
    c2.metric("您已有", "%s 人" % result.get("host_team_size", 1))
    c3.metric("还差", "%s 人" % result.get("needed", 0))
    c4.metric("符合硬约束", "%s 人" % result.get("hard_pass", 0))

    st.caption(
        "候选池检查 %s 条意向；倒排索引技能命中 %s 条；"
        "推荐列表 = 匹配分 > 0，备选列表 = 满足硬约束但匹配分 = 0。"
        % (result.get("candidates_checked", 0), result.get("index_hits", 0))
    )

    host_intent_id = st.session_state.get("host_intent_id")
    recommended = _dedupe_items_by_user(result.get("recommended", []))
    recommended_users = {x.get("user_id") for x in recommended}
    alternatives = [
        x
        for x in _dedupe_items_by_user(result.get("alternatives", []))
        if x.get("user_id") not in recommended_users
    ]

    if not recommended and not alternatives:
        st.info(
            "当前暂无匹配对象，您的资料已存入参赛池，之后其他用户可能匹配到您。"
        )
        return

    st.markdown("### 推荐候选人（按匹配度从高到低）")
    if recommended:
        for item in recommended:
            render_candidate(item, host_intent_id, "rec")
    else:
        st.caption("暂无推荐候选人。")

    st.markdown("### 其他备选候选人（满足硬约束，匹配度 = 0）")
    if alternatives:
        for item in alternatives:
            render_candidate(item, host_intent_id, "alt")
    else:
        st.caption("暂无备选候选人。")


def _current_match_map(intent_id: str):
    intent = pool.get_intent(intent_id)
    if intent is None:
        return {}
    if intent.get("status") == "expired":
        return {}
    host_user = pool.get_user(intent.get("user_id", "")) or {}
    result = matcher.find_candidates(
        intent,
        host_user,
        pool.list_matching_intents(),
        pool.users,
        table,
        competition_range=_competition_range_for(intent),
        inverted_index=pool.build_inverted_index(),
    )
    mapping = {}
    for item in list(result.get("recommended", [])) + list(result.get("alternatives", [])):
        mapping[item.get("intent_id")] = item
    return mapping


def _new_candidates_for(intent: dict, user_id: str = ""):
    if intent.get("status") not in ("matching", "teamed"):
        return []
    uid = user_id or intent.get("user_id") or current_uid
    snapshot = store.get_snapshot(intent.get("intent_id", ""))
    seen = {x.get("intent_id") for x in snapshot.get("candidates", [])}
    teamed = _teamed_user_ids(uid)
    mapping = _current_match_map(intent.get("intent_id", ""))
    fresh = []
    for iid, item in mapping.items():
        if iid in seen:
            continue
        if float(item.get("score", 0)) <= 0:
            continue
        if item.get("user_id") in teamed:
            continue
        fresh.append(item)
    return _dedupe_items_by_user(fresh)


def rematch_intent(intent_id: str) -> int:
    intent = pool.get_intent(intent_id)
    if intent is None:
        return 0
    if intent.get("status") == "expired":
        _set_flash("该参赛意向已过期，无法重新匹配。")
        return 0
    if intent.get("status") != "matching":
        intent["status"] = "matching"
        intent["excluded_reason"] = None
        intent["updated_at"] = now_str()
        pool._save_intents()
    mapping = _current_match_map(intent_id)
    snapshot = store.get_snapshot(intent_id)
    old = list(snapshot.get("candidates", []))
    old_ids = {x.get("intent_id") for x in old}
    old_users = {x.get("user_id") for x in old}
    fresh = []
    for iid, item in mapping.items():
        if iid in old_ids or item.get("user_id") in old_users:
            continue
        fresh.append(
            {
                "intent_id": iid,
                "user_id": item.get("user_id"),
                "score": item.get("score", 0),
                "nickname": item.get("nickname", ""),
                "competition": item.get("competition", ""),
            }
        )
    old = _dedupe_items_by_user(old)
    fresh = _dedupe_items_by_user(fresh)
    store.save_snapshot(intent_id, old + fresh)
    store.mark_history_seen(current_uid, intent_id, 0)
    return len(fresh)


def render_history_detail() -> None:
    intent_id = st.session_state.get("history_view_intent")
    if not intent_id:
        return
    intent = pool.get_intent(intent_id)
    if intent is None:
        st.session_state.history_view_intent = None
        return
    snapshot = store.get_snapshot(intent_id)
    records = _dedupe_items_by_user(list(snapshot.get("candidates", [])))
    mapping = _current_match_map(intent_id)
    seen_ids = {x.get("intent_id") for x in records}
    seen_users = {x.get("user_id") for x in records}
    teamed = _teamed_user_ids(current_uid)
    fresh = _dedupe_items_by_user(
        [
            item
            for iid, item in mapping.items()
            if iid not in seen_ids
            and item.get("user_id") not in seen_users
            and item.get("user_id") not in teamed
        ]
    )

    st.markdown("---")
    header_left, header_right = st.columns([4, 1])
    with header_left:
        st.markdown(
            "### 匹配记录 · %s" % _competition_display(intent.get("competition") or ""),
            unsafe_allow_html=True,
        )
        st.caption(
            "已有 %s 人 · 历史匹配 %d 位 · 新加入 %d 位"
            % (intent.get("team_size", 1), len(records), len(fresh))
        )
    with header_right:
        if st.button("关闭匹配记录", key="hist_close", use_container_width=True):
            st.session_state.history_view_intent = None
            st.rerun()

    if not records and not fresh:
        st.info("还没有匹配记录，可以点“重新匹配”生成。")
        return

    def show_records(title: str, items) -> None:
        if not items:
            return
        st.markdown("### " + title)
        for rec in items:
            cand_iid = rec.get("intent_id")
            item = mapping.get(cand_iid)
            if item is None:
                cand_intent = pool.get_intent(cand_iid) or {}
                cand_user = pool.get_user(rec.get("user_id", "")) or {}
                item = {
                    "intent_id": cand_iid,
                    "user_id": rec.get("user_id", ""),
                    "nickname": cand_user.get("nickname") or rec.get("nickname") or "未命名",
                    "score": rec.get("score", 0),
                    "competition": cand_intent.get("competition") or rec.get("competition", ""),
                    "intro": "该意向当前不在匹配池中",
                }
            render_candidate(item, intent_id, "hist_%s" % cand_iid)

    show_records("历史匹配的伙伴", records)
    show_records("最近新加入、符合要求的伙伴", fresh)
    store.mark_history_seen(current_uid, intent_id, len(fresh))


def render_history_section() -> None:
    st.markdown("## 历史参赛信息")
    _render_page_summary(
        "可随时查看以前填过的参赛信息、当时的匹配记录，并对新人重新匹配。"
        "“最近新加入”只列出尚未与你组队的新伙伴。"
    )
    intents = pool.list_user_intents(current_uid)
    if not intents:
        st.info("还没有历史参赛信息。先在智能体对话里填写一次组队需求吧。")
        return
    status_map = {
        "matching": "匹配中",
        "teamed": "已组队",
        "expired": "已过期",
        "removed": "已从参赛池移除",
        "deleted": "已注销",
    }
    for intent in reversed(intents):
        iid = intent.get("intent_id", "")
        if intent.get("status") == "deleted":
            continue
        new_items = _new_candidates_for(intent)
        comp_range = _competition_range_for(intent)
        try:
            base_size = int(intent.get("team_size") or 1)
        except (TypeError, ValueError):
            base_size = 1
        effective = base_size + len(_intent_partner_ids(iid))
        st.markdown('<div class="history-card">', unsafe_allow_html=True)
        c1, c2 = st.columns([3, 1.35])
        with c1:
            st.markdown(
                "<div class='history-title'>%s</div>"
                % _esc(_competition_display(intent.get("competition") or "")),
                unsafe_allow_html=True,
            )
            st.caption(
                "状态：%s · 人数 %s-%s · 当前队伍 %s 人 · "
                "已邀请待确认 %s 人 · 已组队队友 %s 人"
                % (
                    status_map.get(intent.get("status"), intent.get("status")),
                    comp_range.get("min"),
                    comp_range.get("max"),
                    effective,
                    _pending_invite_count(iid),
                    len(_intent_partner_ids(iid)),
                )
            )
            seen_count = store.history_seen_count(current_uid, iid)
            unseen = max(0, len(new_items) - seen_count)
            if unseen:
                st.markdown(
                    '<span class="new-dot">🔴 最近有新人数更新：%d 位符合要求、匹配度高的新伙伴</span>'
                    % unseen,
                    unsafe_allow_html=True,
                )
        with c2:
            if st.button("查看匹配记录", key="hist_view_%s" % iid, use_container_width=True):
                st.session_state.history_view_intent = iid
                st.rerun()
            if st.button("重新匹配", key="hist_rematch_%s" % iid, use_container_width=True):
                count = rematch_intent(iid)
                st.session_state.history_view_intent = iid
                _set_flash("重新匹配完成，新增 %d 位伙伴，原有匹配已保留。" % count)
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
    render_history_detail()


def render_chat_tab() -> None:
    render_chat_messages()

    if st.session_state.get("last_error"):
        st.error(st.session_state.last_error)

    stage = st.session_state.get("stage", "chatting")
    if stage in ("form", "confirm"):
        st.caption("请在下方完成表单或确认操作。")
    else:
        if stage == "chatting":
            st.markdown('<div class="topic-hint">推荐话题</div>', unsafe_allow_html=True)
            t1, t2, _ = st.columns([1.7, 2.1, 3])
            if t1.button("有哪些热门的创新类比赛？", key="topic_hot", use_container_width=True):
                try:
                    with st.spinner("正在思考..."):
                        handle_user_text("有哪些热门的创新类比赛？")
                except Exception as exc:
                    st.session_state.last_error = "处理失败：%s" % exc
                st.rerun()
            if t2.button(
                "我想参加大创赛，帮我找队友",
                key="topic_dachuang",
                use_container_width=True,
            ):
                try:
                    with st.spinner("正在识别比赛信息..."):
                        handle_user_text("我想参加大创赛，帮我找队友")
                except Exception as exc:
                    st.session_state.last_error = "处理失败：%s" % exc
                st.rerun()

        with st.container(key="chat-input-main"):
            with st.form("chat_form", clear_on_submit=True):
                cols = st.columns([5, 1])
                prompt = cols[0].text_input(
                    "输入你的参赛需求",
                    key="chat_prompt_input",
                    label_visibility="collapsed",
                    placeholder="随便聊聊，或说：我想参加大创赛",
                )
                submitted = cols[1].form_submit_button("发送", use_container_width=True)
        _render_voice_mic()

        if submitted and str(prompt or "").strip():
            try:
                with st.spinner("正在思考..."):
                    handle_user_text(str(prompt).strip())
            except Exception as exc:
                st.session_state.last_error = "处理失败：%s" % exc
            st.rerun()

    if stage == "form":
        render_intent_form()
    elif stage == "confirm":
        render_confirmation()
    elif stage == "unknown":
        render_unknown()

    render_match_results()


# ----------------------------------------------------------------------
# 邀请与沟通
# ----------------------------------------------------------------------
def _intent_brief(intent_id: str) -> str:
    intent = pool.get_intent(intent_id) or {}
    user = pool.get_user(intent.get("user_id", "")) or {}
    return "「%s」· %s · 已有 %s 人 · 擅长 %s" % (
        _competition_display(intent.get("competition", "")),
        user.get("nickname", ""),
        intent.get("team_size", ""),
        "、".join(intent.get("skills", [])[:4]),
    )


def _friend_ids(user_id: str):
    ids = set(store.list_friends(user_id))
    for inv in pool.list_invitations_for_user(user_id):
        other = inv.get("to_user") if inv.get("from_user") == user_id else inv.get("from_user")
        if other and other != user_id and pool.get_user(other):
            ids.add(other)
    for group in store.list_groups(user_id):
        for member in group.get("members") or []:
            if member != user_id and pool.get_user(member):
                ids.add(member)
    return [uid for uid in ids if pool.get_user(uid)]


def _friend_chat_context(friend_id: str):
    """返回 (邀请, 我的意向, 对方意向, 是否已同意)。"""
    fallback = None
    for inv in pool.list_invitations_for_user(current_uid):
        other = inv.get("to_user") if inv.get("from_user") == current_uid else inv.get("from_user")
        if other != friend_id:
            continue
        if inv.get("from_user") == current_uid:
            my_iid, other_iid = inv.get("from_intent"), inv.get("to_intent")
        else:
            my_iid, other_iid = inv.get("to_intent"), inv.get("from_intent")
        if inv.get("status") == "accepted":
            return inv, my_iid, other_iid, True
        if fallback is None:
            fallback = (inv, my_iid, other_iid, False)
    if fallback is not None:
        return fallback
    mine = _latest_intent(current_uid) or {}
    theirs = _latest_intent(friend_id) or {}
    return None, mine.get("intent_id", ""), theirs.get("intent_id", ""), False


def render_friend_chat(friend_id: str) -> None:
    other = pool.get_user(friend_id) or {}
    if not other:
        st.caption("该好友已不存在。")
        return
    nickname = other.get("nickname") or "未命名"
    comp_label = _friend_competition_label(friend_id)
    inv, my_iid, other_iid, accepted = _friend_chat_context(friend_id)
    team_label = ("已组队 · " + comp_label) if accepted and comp_label else (
        "好友私聊" if not accepted else "已组队"
    )
    st.markdown(
        '<div class="account-head">%s<div><div class="name">%s</div>'
        '<div class="small-muted">%s</div></div></div>'
        % (_avatar_html(other, 44), _esc(nickname), _esc(team_label)),
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns([1, 1.2])
    if c1.button("查看资料", key="friend_chat_profile_%s" % friend_id, use_container_width=True):
        profile_dialog(friend_id, other_iid or (_latest_intent(friend_id) or {}).get("intent_id", ""))
    if accepted and inv and c2.button(
        "取消组队", key="friend_chat_cancel_%s" % friend_id, use_container_width=True
    ):
        open_dialog("cancel_team", inv.get("invitation_id"))

    if accepted:
        messages = pool.get_messages(my_iid or "", other_iid or "")
    else:
        messages = store.get_direct_messages(current_uid, friend_id)
    if not messages:
        st.caption("还没有消息，发一条开始聊天吧。")
    for msg in messages:
        mine = msg.get("from_user") == current_uid
        sender = current_user if mine else other
        key = "chat-%s-%s" % ("mine" if mine else "theirs", msg.get("message_id"))
        with st.container(key=key):
            st.markdown(
                _bubble_html(
                    msg.get("text", ""),
                    _avatar_src(sender),
                    _user_name(sender),
                    msg.get("created_at", ""),
                    mine=mine,
                    avatar_size=42,
                ),
                unsafe_allow_html=True,
            )
    if messages:
        stamp = max(str(m.get("created_at", "")) for m in messages)
        if accepted:
            had_unread = _pair_unread_count(current_uid, my_iid or "", other_iid or "")
            store.mark_messages_read(current_uid, pair_key(my_iid or "", other_iid or ""), stamp)
        else:
            had_unread = _direct_unread_count(current_uid, friend_id)
            store.mark_direct_read(current_uid, friend_id, stamp)
        if had_unread:
            st.rerun()

    st.markdown(
        '<div class="chat-target-line">发给：%s%s</div>'
        % (
            _esc(nickname),
            (" · " + _esc(comp_label)) if comp_label else "",
        ),
        unsafe_allow_html=True,
    )
    with st.container(key="chat-input-%s" % friend_id):
        with st.form(key="msg_form_%s" % friend_id):
            content = st.text_input(
                "发给 %s 的消息" % nickname,
                key="msg_text_%s" % friend_id,
                placeholder="输入消息，发给 %s…" % nickname,
                label_visibility="collapsed",
            )
            if st.form_submit_button("发送给 %s" % nickname, use_container_width=True):
                try:
                    if accepted:
                        pool.send_message(my_iid, other_iid, current_uid, content)
                    else:
                        store.send_direct_message(current_uid, friend_id, content)
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
    _render_voice_mic()


def render_friends_tab() -> None:
    render_back_to_chat()
    st.markdown("## 好友列表")
    st.caption("通讯录式好友列表：可查看资料，也可以直接点进和 TA 单独聊天。")
    friend_ids = _friend_ids(current_uid)
    friend_ids.sort(key=lambda uid: _user_name(uid))
    left, right = st.columns([1, 1.45])

    with left:
        with st.container(height=520, border=True):
            if not friend_ids:
                st.caption("还没有好友。发送或同意邀请后会自动成为好友。")
            for fid in friend_ids:
                user = pool.get_user(fid) or {}
                comp = _friend_competition_label(fid)
                unread = _friend_unread_count(current_uid, fid)
                label = user.get("nickname") or "未命名"
                if comp:
                    label += "·" + comp
                row = st.columns([0.7, 2.6, 0.9, 0.9])
                with row[0]:
                    st.markdown(_avatar_html(user, 38), unsafe_allow_html=True)
                with row[1]:
                    st.markdown(
                        '<div class="friend-row-name">%s%s</div>'
                        '<div class="friend-row-sub">%s</div>'
                        % (
                            _esc(user.get("nickname") or "未命名"),
                            (" · " + _esc(comp)) if comp else "",
                            ("🔴 %d 条未读 · " % unread if unread else "")
                            + _esc(user.get("phone") or "未填写手机号"),
                        ),
                        unsafe_allow_html=True,
                    )
                with row[2]:
                    if st.button("资料", key="friend_profile_%s" % fid, use_container_width=True):
                        intent = _latest_intent(fid)
                        profile_dialog(fid, (intent or {}).get("intent_id", ""))
                with row[3]:
                    if st.button("聊天", key="friend_chat_%s" % fid, use_container_width=True):
                        st.session_state.friend_chat = fid
                        st.rerun()

    with right:
        selected = st.session_state.get("friend_chat")
        if selected and pool.get_user(selected):
            render_friend_chat(selected)
        else:
            st.info("从左侧好友列表点“聊天”，即可进入和 TA 的单人聊天框。")
            st.caption("聊天输入框只会在选中好友后出现，避免出现不知道发给谁的输入框。")


def render_groups_tab() -> None:
    render_back_to_chat()
    top_left, top_right = st.columns([6, 1])
    with top_left:
        st.markdown("## 群聊")
        st.caption("群聊列表与创建群聊；点右上角小加号可以勾选好友建群。")
    with top_right:
        if st.button("＋", key="group_create_plus", use_container_width=True):
            st.session_state.show_group_create = not st.session_state.get(
                "show_group_create", False
            )
            st.rerun()

    friend_ids = _friend_ids(current_uid)
    friend_ids.sort(key=lambda uid: _user_name(uid))
    if st.session_state.get("show_group_create"):
        with st.container(border=True, key="group-create-card"):
            st.markdown("**创建群聊**")
            with st.form("group_create_form"):
                gname = st.text_input("群聊名称", placeholder="例如：大创赛冲奖小队")
                comp_choices = [c for c in (table.get("competitions") or {}).keys()]
                gcomp = st.selectbox(
                    "比赛",
                    comp_choices or ["中国国际大学生创新大赛"],
                )
                selected = st.multiselect(
                    "勾选要加入群聊的成员",
                    friend_ids,
                    format_func=lambda uid: _user_name(uid),
                )
                created = st.form_submit_button(
                    "创建群聊", use_container_width=True, type="primary"
                )
            if created:
                if not selected:
                    st.error("请至少勾选一位好友加入群聊。")
                else:
                    group = store.create_group(gname, gcomp, current_uid, selected)
                    for member in group.get("members") or []:
                        if member != current_uid:
                            store.notify(
                                member,
                                "你被邀请加入了群聊「%s」。"
                                % (group.get("name") or "组队群聊"),
                                "group_created",
                                {"group_id": group.get("group_id")},
                            )
                    st.session_state.show_group_create = False
                    _set_flash("群聊已创建。")
                    st.rerun()

    groups = store.list_groups(current_uid)
    left, right = st.columns([1, 1.45])
    with left:
        with st.container(height=480, border=True):
            if not groups:
                st.caption("还没有群聊，点右上角“＋”创建。")
            for g in groups:
                gid = g.get("group_id", "")
                unread = _group_unread_count(current_uid, gid)
                label = "%s · %s" % (
                    g.get("name"),
                    _competition_display(g.get("competition")),
                )
                if st.button(
                    label + ("  🔴%d" % unread if unread else ""),
                    key="group_open_%s" % gid,
                    use_container_width=True,
                ):
                    st.session_state.group_choice = gid
                    st.rerun()
    with right:
        chosen_group = st.session_state.get("group_choice")
        if not chosen_group or not store.get_group(chosen_group):
            st.info("从左侧群聊列表选择一个群，即可查看群消息。")
            return
        group = store.get_group(chosen_group) or {}
        st.markdown("**%s**" % _esc(group.get("name") or "群聊"))
        st.caption(
            "比赛：%s · 成员：%s"
            % (
                _esc(_competition_display(group.get("competition") or "")),
                "、".join(_user_name(uid) for uid in (group.get("members") or [])),
            )
        )
        messages = group.get("messages") or []
        with st.container(height=320, border=True):
            if not messages:
                st.caption("群聊还没有消息。")
            for msg in messages:
                mine = msg.get("from_user") == current_uid
                sender = pool.get_user(msg.get("from_user", "")) or {}
                key = "chat-%s-g-%s" % ("mine" if mine else "theirs", msg.get("message_id"))
                with st.container(key=key):
                    st.markdown(
                        _bubble_html(
                            msg.get("text", ""),
                            _avatar_src(sender),
                            _user_name(sender),
                            msg.get("created_at", ""),
                            mine=mine,
                            avatar_size=42,
                        ),
                        unsafe_allow_html=True,
                    )
        if messages:
            had_unread = _group_unread_count(current_uid, chosen_group)
            store.mark_group_read(
                current_uid,
                chosen_group,
                max(str(m.get("created_at", "")) for m in messages),
            )
            if had_unread:
                st.rerun()
        st.markdown(
            '<div class="chat-target-line">发给群聊：%s</div>'
            % _esc(group.get("name") or "群聊"),
            unsafe_allow_html=True,
        )
        with st.container(key="chat-input-group"):
            with st.form(key="group_msg_form_%s" % chosen_group):
                gcontent = st.text_input(
                    "群消息",
                    key="group_msg_text_%s" % chosen_group,
                    placeholder="发送群消息...",
                    label_visibility="collapsed",
                )
                if st.form_submit_button("发送到群聊", use_container_width=True):
                    try:
                        store.send_group_message(chosen_group, current_uid, gcontent)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        _render_voice_mic()


def render_notifications_block() -> None:
    notes = store.list_notifications(current_uid)
    if not notes:
        st.caption("暂无提醒与系统通知。")
        return
    st.markdown("### 提醒与通知 / 系统通知")
    for note in notes[:50]:
        kind = note.get("kind")
        with st.container(border=True):
            st.markdown(_esc(note.get("text") or ""))
            if kind == "admin_invite" and not note.get("read"):
                c1, c2 = st.columns(2)
                if c1.button(
                    "同意成为管理员",
                    key="admin_accept_%s" % note.get("notification_id"),
                    use_container_width=True,
                    type="primary",
                ):
                    inviter_uid = store.get_admin_inviter(current_uid)
                    inviter_user = pool.get_user(inviter_uid) or {}
                    if not _is_super_admin(inviter_user):
                        store.reject_admin_invite(current_uid)
                        store.mark_notification_read(note.get("notification_id"), current_uid)
                        _set_flash("该管理员邀请已失效：只有超级管理员可以分配管理员。")
                        st.rerun()
                    inviter = store.accept_admin_invite(current_uid)
                    store.mark_notification_read(note.get("notification_id"), current_uid)
                    if inviter:
                        store.notify(
                            inviter,
                            "『%s』已同意成为管理员。"
                            % (current_user.get("nickname") or "对方"),
                            "admin_accepted",
                        )
                    _set_flash("你已成为管理员。")
                    st.rerun()
                if c2.button(
                    "拒绝",
                    key="admin_reject_%s" % note.get("notification_id"),
                    use_container_width=True,
                ):
                    inviter = store.reject_admin_invite(current_uid)
                    store.mark_notification_read(note.get("notification_id"), current_uid)
                    if inviter:
                        store.notify(
                            inviter,
                            "『%s』拒绝成为管理员。"
                            % (current_user.get("nickname") or "对方"),
                            "admin_rejected",
                        )
                    st.rerun()
            elif not note.get("read"):
                store.mark_notification_read(note.get("notification_id"), current_uid)


def render_notices_tab() -> None:
    render_back_to_chat()
    st.markdown("## 邀请通知")
    _render_page_summary(
        "收到的邀请、我发出的邀请、等待同意 / 已同意 / 已拒绝状态，以及全部提醒与系统通知。"
    )
    invitations = pool.list_invitations_for_user(current_uid)
    pending_recv = [
        inv
        for inv in invitations
        if inv.get("to_user") == current_uid and inv.get("status") == "pending"
    ]
    sent_all = [
        inv
        for inv in invitations
        if inv.get("from_user") == current_uid
        and inv.get("status") in ("pending", "rejected", "accepted")
    ]

    if not pending_recv:
        st.caption("暂无待处理的邀请。")
    for inv in pending_recv:
        from_user = pool.get_user(inv.get("from_user", "")) or {}
        with st.container(border=True):
            st.markdown(
                '<div class="account-head">%s<div><div class="name">%s</div>'
                '<div class="small-muted">邀请你一起组队</div></div></div>'
                % (
                    _avatar_html(from_user, 44),
                    _esc(from_user.get("nickname") or "未命名"),
                ),
                unsafe_allow_html=True,
            )
            st.caption("对方意向：" + _intent_brief(inv.get("from_intent", "")))
            st.markdown(
                "对方联系方式：`%s`" % _esc(_user_contact(from_user) or "未提供")
            )
            if inv.get("message"):
                with st.container(key="invite-note-%s" % inv.get("invitation_id")):
                    st.markdown(
                        "**对方留言：** %s" % _esc(inv.get("message")),
                        unsafe_allow_html=True,
                    )
            c1, c2 = st.columns(2)
            if c1.button(
                "同意",
                key="accept_%s" % inv["invitation_id"],
                use_container_width=True,
                type="primary",
            ):
                accept_invitation(inv)
                st.rerun()
            if c2.button(
                "拒绝",
                key="reject_%s" % inv["invitation_id"],
                use_container_width=True,
            ):
                reject_invitation(inv)
                st.rerun()

    st.markdown(
        "<hr style='margin:14px 0 12px 0;border:none;border-top:1px solid rgba(167,230,195,.55)'/>",
        unsafe_allow_html=True,
    )
    if not sent_all:
        st.caption("暂无我发出的邀请。")
    status_text_map = {
        "pending": "等待对方同意",
        "accepted": "已同意",
        "rejected": "已拒绝",
    }
    for inv in sent_all:
        other_uid = inv.get("to_user")
        other_user = pool.get_user(other_uid) or {}
        st.write(
            "→ **%s** · %s · %s"
            % (
                other_user.get("nickname") or "未命名",
                _intent_brief(inv.get("to_intent", "")),
                status_text_map.get(inv.get("status"), inv.get("status")),
            )
        )
        if inv.get("message"):
            st.caption("我的留言：" + str(inv.get("message")))

    generic_unread = any(
        not note.get("read") and note.get("kind") not in ("admin_invite",)
        for note in store.list_notifications(current_uid)
    )
    unseen_pending = any(
        not store.invitation_seen(current_uid, inv.get("invitation_id", ""))
        for inv in pending_recv
    )
    render_notifications_block()

    store.mark_invitations_seen(
        current_uid, [inv.get("invitation_id") for inv in pending_recv]
    )
    if generic_unread or unseen_pending:
        st.rerun()


# ----------------------------------------------------------------------
# 我的组队
# ----------------------------------------------------------------------
def _intent_partner_ids(intent_id: str):
    """某条意向真实组到的队友（只统计与该意向同比赛的已组队关系）。"""
    intent = pool.get_intent(intent_id) or {}
    competition = _competition_display(intent.get("competition") or "")
    partners = set()

    def host_competition(inv):
        host_intent = pool.get_intent(inv.get("from_intent") or "") or {}
        return _competition_display(host_intent.get("competition") or "")

    for inv in _accepted_invitations(current_uid):
        if intent_id not in (inv.get("from_intent"), inv.get("to_intent")):
            continue
        if host_competition(inv) != competition:
            continue
        other = (
            inv.get("to_user")
            if inv.get("from_user") == current_uid
            else inv.get("from_user")
        )
        if other:
            partners.add(other)
    for link in store.list_team_links(current_uid):
        if intent_id not in (link.get("intent_a"), link.get("intent_b")):
            continue
        if _competition_display(link.get("competition") or "") != competition:
            continue
        other = (
            link.get("user_b")
            if link.get("user_a") == current_uid
            else link.get("user_a")
        )
        if other:
            partners.add(other)
    partners.discard(current_uid)
    return {uid for uid in partners if pool.get_user(uid)}


def render_team_tab() -> None:
    render_back_to_chat()
    st.markdown("## 我的组队")
    _render_page_summary("我的参赛意向、当前实际组队、组队确认与提醒都在这里。")

    st.markdown("### 当前实际组队")
    st.caption("包含我自己报名的队伍，也包含别人邀请我加入的其他比赛队伍。")
    teams = _team_records_for(current_uid)
    if not teams:
        st.caption("目前还没有实际组队。")
    for index, rec in enumerate(teams):
        members = rec.get("members") or []
        with st.container(border=True):
            st.markdown(
                "**%s** · 已组队 %d 人"
                % (_competition_display(rec.get("competition") or ""), len(members))
            )
            member_labels = []
            for uid in members:
                user = pool.get_user(uid) or {}
                member_labels.append(
                    "%s%s" % (
                        user.get("nickname") or "未命名",
                        "（我）" if uid == current_uid else "",
                    )
                )
            st.caption("成员：" + "、".join(member_labels))
            others = [uid for uid in members if uid != current_uid]
            if others:
                cols = st.columns(min(len(others), 4))
                for col, uid in zip(cols, others):
                    with col:
                        if st.button(
                            "查看 %s 资料" % _user_name(uid),
                            key="team_member_profile_%s_%s" % (index, uid),
                            use_container_width=True,
                        ):
                            profile_dialog(uid, _latest_intent(uid).get("intent_id", ""))
            invitation_ids = [x for x in rec.get("invitation_ids") if x]
            linked = None
            for inv_id in invitation_ids:
                inv = pool.get_invitation(inv_id) or {}
                if inv.get("status") == "accepted":
                    linked = inv
                    break
            if linked and st.button(
                "取消这支队伍的组队",
                key="team_record_cancel_%s" % index,
                use_container_width=False,
            ):
                open_dialog("cancel_team", linked.get("invitation_id"))
            st.caption("取消组队后会回到参赛池继续匹配，原有聊天记录会保留在站内。")

    st.markdown("### 过期清理")
    st.caption(
        "超过可组队时间段截止日期且未组队成功的意向，会自动从候选池移除，"
        "但资料保留在参赛池中。"
    )
    if st.button("立即运行过期清理", use_container_width=False):
        st.session_state.last_expired = pool.expire_intents()
        st.rerun()
    expired = st.session_state.get("last_expired")
    if expired:
        st.info("本次清理了 %d 条过期意向：%s" % (len(expired), "、".join(expired)))


# ----------------------------------------------------------------------
# 管理中心
# ----------------------------------------------------------------------
def _add_competition_to_table(name: str, low: int, high: int) -> bool:
    path = os.path.join(BASE_DIR, "competition_table.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"competitions": {}, "default_range": {"min": 3, "max": 5}}
    competitions = data.setdefault("competitions", {})
    competitions[name] = {
        "aliases": list(competitions.get(name, {}).get("aliases", []) or []),
        "min": int(low),
        "max": int(high),
        "innovation": True,
        "keywords": ["创新", "科技"],
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        st.session_state.table = matcher.load_competition_table(path)
        return True
    except OSError:
        return False


def render_admin_tab() -> None:
    pool_ok = _reload_pool()
    _reload_store()
    if not pool_ok:
        st.warning("云端用户读取失败，当前展示缓存数据；已停止写入，避免覆盖 Neon 数据。")
    if not current_is_admin:
        st.error("只有管理员可以进入管理中心。")
        return

    viewer_user = pool.get_user(current_uid) or current_user
    viewer_is_super = _is_super_admin(viewer_user)
    st.subheader("管理中心")
    if viewer_is_super:
        _render_page_summary("以好友列表式展示所有注册用户；可查看完整资料、分配/收回管理员、注销普通用户。")
    else:
        _render_page_summary("以好友列表式展示所有注册用户；你可以查看完整资料、注销普通用户。")

    st.markdown("### 未收录比赛反馈")
    pending_feedback = store.list_feedback("pending")
    if not pending_feedback:
        st.caption("暂无待处理的比赛收录反馈。")
    for fb in pending_feedback:
        with st.container(border=True):
            st.markdown(
                "**%s** · 建议人数 %s-%s 人 · 提交人：%s"
                % (
                    _esc(fb.get("competition") or ""),
                    fb.get("min"),
                    fb.get("max"),
                    _esc(fb.get("submitter_name") or fb.get("submitter_uid") or ""),
                )
            )
            st.caption("提交时间：%s" % (fb.get("created_at") or ""))
            c1, c2 = st.columns(2)
            if c1.button(
                "同意收录",
                key="fb_accept_%s" % fb.get("feedback_id"),
                use_container_width=True,
                type="primary",
            ):
                ok = _add_competition_to_table(
                    fb.get("competition", ""), fb.get("min", 3), fb.get("max", 5)
                )
                if ok:
                    store.resolve_feedback(fb.get("feedback_id"), "approved", current_uid)
                    store.notify(
                        fb.get("submitter_uid"),
                        "您反馈的比赛「%s」已被收录，现在可以正常匹配了。"
                        % (fb.get("competition") or ""),
                        "feedback_approved",
                    )
                    _set_flash("已收录该比赛。")
                    st.rerun()
                else:
                    st.error("写入比赛表失败，请检查文件权限。")
            if c2.button(
                "忽略",
                key="fb_reject_%s" % fb.get("feedback_id"),
                use_container_width=True,
            ):
                store.resolve_feedback(fb.get("feedback_id"), "rejected", current_uid)
                store.notify(
                    fb.get("submitter_uid"),
                    "您反馈的比赛「%s」暂未收录。" % (fb.get("competition") or ""),
                    "feedback_rejected",
                )
                st.rerun()

    st.markdown("### 注册用户")
    all_users = pool.list_users()
    with st.container(height=620, border=True):
        for uid in all_users:
            user = pool.get_user(uid) or {}
            if not user:
                continue
            user_intents = pool.list_user_intents(uid)
            active_intents = [i for i in user_intents if i.get("status") == "matching"]
            row = st.columns([0.7, 3.3, 2.4, 1.0, 1.1, 1.0])
            with row[0]:
                st.markdown(_avatar_html(user, 40), unsafe_allow_html=True)
            with row[1]:
                st.markdown(
                    "**%s**  \n<span class='small-muted'>%s · %s</span>"
                    % (
                        _esc(user.get("nickname") or "未命名"),
                        _esc(user.get("phone") or "未填写手机号"),
                        _esc(_user_contact(user) or "未填联系方式"),
                    ),
                    unsafe_allow_html=True,
                )
            with row[2]:
                st.caption(
                    "参赛意向 %d 条（匹配中 %d）%s"
                    % (
                        len(user_intents),
                        len(active_intents),
                        " · 管理员" if store.is_admin(uid) else "",
                    )
                )
            with row[3]:
                if st.button("资料", key="admin_profile_%s" % uid, use_container_width=True):
                    intent = _latest_intent(uid)
                    profile_dialog(uid, (intent or {}).get("intent_id", ""))
            with row[4]:
                if store.is_admin(uid):
                    if viewer_is_super and not _is_super_admin(user) and uid != current_uid:
                        if st.button("收回管理员", key="admin_revoke_%s" % uid, use_container_width=True):
                            store.remove_admin(uid)
                            store.notify(
                                uid,
                                "你的管理员身份已被超级管理员收回。",
                                "admin_removed",
                            )
                            _set_flash("已收回该用户的管理员身份。")
                            st.rerun()
                    else:
                        st.caption("已是管理员")
                elif not viewer_is_super:
                    st.caption("仅超级管理员可分配")
                elif store.get_admin_inviter(uid):
                    st.caption("已发出邀请")
                elif st.button("设为管理员", key="admin_set_%s" % uid, use_container_width=True):
                    store.add_admin_invite(uid, current_uid)
                    store.notify(
                        uid,
                        "管理员邀请你成为管理员，是否同意？",
                        "admin_invite",
                        {"inviter": current_uid},
                    )
                    _set_flash("已向该用户发出管理员邀请。")
                    st.rerun()
            with row[5]:
                if _is_super_admin(user) or _is_admin(uid) or uid == current_uid:
                    st.caption("不可注销")
                elif st.button("注销", key="admin_delete_%s" % uid, use_container_width=True):
                    open_dialog("delete_user", uid)
            st.markdown("<hr style='margin:6px 0;border:none;border-top:1px dashed #d7f0e1'/>", unsafe_allow_html=True)

    super_admins = [
        _user_name(uid) for uid, user in pool.users.items() if _is_super_admin(user)
    ]
    st.caption("超级管理员：%s（系统自动识别）" % ("、".join(super_admins) or "暂无"))


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
def render_history_tab() -> None:
    render_back_to_chat()
    render_history_section()


render_flash_and_dialogs()
page = render_sidebar()

if page == "friends":
    render_friends_tab()
elif page == "groups":
    render_groups_tab()
elif page == "team":
    render_team_tab()
elif page == "history":
    render_history_tab()
elif page == "notices":
    render_notices_tab()
elif page == "admin" and current_is_admin:
    render_admin_tab()
else:
    render_chat_tab()

