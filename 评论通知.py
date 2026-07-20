#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评论/回复私聊提醒 + 快速回复 + 评论列表图片渲染。

轮询每个账号槽位的互动消息（feed get-notices），发现新的评论/回复后：
1. 主动私聊 admins.txt 里的每个管理员（发帖人视角：有人评论了帖子、
   或有人回复了评论都会提醒）；
2. 同时抓取该帖子的完整评论列表，用 Pillow 渲染成图片随通知发给管理员
   （未安装 Pillow 或缺中文字体时自动回退为文字列表）；
3. 管理员发送「评论回复 内容」即可直接回复最近一条提醒的评论/回复
   （自动用对应账号槽位的身份调用 feed do-reply）。

指令：评论通知 开启/关闭（默认开启）。
"""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.plugin.decorators import on_load, on_unload

from .腾讯频道 import (
    BASE_DIR,
    _extract_json,
    _get_self_user_id,
    _get_switch,
    _load_admins,
    _load_users,
    _normalize_rate_limit,
    _read_json_file,
    _run_cli,
    _set_switch,
    _text,
    _user_home,
    _write_json_file,
    admin_handler,
)

POLL_INTERVAL = 60
SEEN_MAX = 300
NOTIFY_TEXT_LIMIT = 120
_TASK_NAME = "txpd_comment_notify"

# 最近一次提醒的回复上下文（快速回复用）
_LAST_CTX: Dict[str, Any] = {}


def notify_enabled() -> bool:
    return _get_switch("comment_notify_enabled", True)


def _get_sender():
    import core.bot.manager as _mgr

    ref = getattr(_mgr, "_bot_manager_ref", None)
    if not ref or not getattr(ref, "_bots", None):
        return None
    try:
        return next(iter(ref._bots.values())).sender
    except Exception:
        return None


# ==================== 已读状态（按槽位隔离） ====================

def _seen_file(user: str) -> Path:
    base = _user_home(user) if user else BASE_DIR
    return base / "comment_notify_seen.json"


def _load_seen(user: str) -> List[str]:
    data = _read_json_file(_seen_file(user), {})
    seen = data.get("seen")
    return [str(x) for x in seen] if isinstance(seen, list) else []


def _save_seen(user: str, seen: List[str]) -> None:
    _write_json_file(_seen_file(user), {"seen": seen[-SEEN_MAX:]})


def _notice_key(item: Dict[str, Any]) -> str:
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ==================== 互动消息解析 ====================

def _payload_of(output: str) -> Dict[str, Any]:
    data = _extract_json(_normalize_rate_limit(output))
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def _notice_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = payload.get("items") or payload.get("list") or payload.get("notices")
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def _notice_text(item: Dict[str, Any]) -> str:
    return str(item.get("content") or item.get("summary") or item.get("title") or item.get("desc") or item.get("msg") or "").strip()


def _notice_feed(item: Dict[str, Any]) -> Tuple[str, str]:
    feed_id = str(item.get("feed_id") or item.get("feedId") or "").strip()
    guild_id = str(item.get("guild_id") or item.get("guildId") or "").strip()
    return feed_id, guild_id


def _is_comment_notice(item: Dict[str, Any]) -> bool:
    """评论/回复类通知；类型字段缺失时按文案关键词判断，仍不确定则按有帖子ID处理。"""
    kind = str(item.get("type") or item.get("notice_type") or item.get("noticeType") or "").lower()
    if kind:
        if any(x in kind for x in ("comment", "reply")):
            return True
        if any(x in kind for x in ("like", "prefer", "follow", "at")):
            return False
    text = _notice_text(item)
    if any(x in text for x in ("评论", "回复")):
        return True
    if any(x in text for x in ("赞", "关注")):
        return False
    return bool(_notice_feed(item)[0])


# ==================== 评论列表抓取 ====================

def _text_of(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return str(value or "").strip()


def _comment_content(item: Dict[str, Any]) -> str:
    content = _text_of(item.get("content"))
    if not content:
        rich = item.get("content_richtext") or item.get("contentRichtext") or item.get("rich_text")
        if isinstance(rich, dict):
            content = _text_of(rich.get("text"))
    return content or "-"


_NICK_KEYS = ("author_nick", "poster_nick", "nick", "nickname", "nick_name", "user_nick")
_AUTHOR_ID_KEYS = ("author_id", "poster_id", "comment_author_id", "reply_author_id", "authorId", "user_id", "tinyid", "poster_tiny_id")


def _item_nick(item: Dict[str, Any]) -> str:
    for key in _NICK_KEYS:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    for sub_key in ("poster", "author", "user", "user_info", "poster_info"):
        sub = item.get(sub_key)
        if isinstance(sub, dict):
            for key in _NICK_KEYS:
                value = str(sub.get(key) or "").strip()
                if value:
                    return value
    return ""


def _item_author_id(item: Dict[str, Any]) -> str:
    for key in _AUTHOR_ID_KEYS:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    for sub_key in ("poster", "author", "user", "user_info", "poster_info"):
        sub = item.get(sub_key)
        if isinstance(sub, dict):
            for key in ("id",) + _AUTHOR_ID_KEYS:
                value = str(sub.get(key) or "").strip()
                if value:
                    return value
    return ""


def _create_time(item: Dict[str, Any]) -> int:
    for key in ("create_time_raw", "comment_create_time", "reply_create_time", "create_time", "createTime"):
        raw = str(item.get(key) or "").strip()
        if raw.isdigit():
            return int(raw)
    return 0


def _fetch_feed_context(feed_id: str, guild_id: str, user: str) -> Dict[str, Any]:
    """抓取帖子标题/作者/发帖时间与完整评论列表（含每条评论的回复）。"""
    ctx: Dict[str, Any] = {"feed_id": feed_id, "guild_id": guild_id, "title": "", "feed_author_id": "", "feed_create_time": "", "channel_id": "", "comments": []}
    detail_args = ["feed", "get-feed-detail", "--feed-id", feed_id, "--json"]
    if guild_id:
        detail_args[2:2] = ["--guild-id", guild_id]
    ok, output = _run_cli(detail_args, user=user or None)
    if ok:
        payload = _payload_of(output)
        feed_obj = payload.get("feed") if isinstance(payload.get("feed"), dict) else payload
        if isinstance(feed_obj, dict):
            ctx["title"] = _text_of(feed_obj.get("title")) or _text_of(feed_obj.get("content"))[:30]
            ctx["feed_author_id"] = str(feed_obj.get("author_id") or feed_obj.get("authorId") or feed_obj.get("feed_author_id") or "").strip()
            ctx["feed_create_time"] = str(feed_obj.get("create_time_raw") or feed_obj.get("feed_create_time") or feed_obj.get("create_time") or feed_obj.get("createTime") or "").strip()
            ctx["channel_id"] = str(feed_obj.get("channel_id") or feed_obj.get("channelId") or "").strip()
    # --reply-list-num 预加载楼中楼回复（默认只带 1 条，最大 10）
    args = ["feed", "get-feed-comments", "--feed-id", feed_id, "--reply-list-num", "10"]
    if guild_id:
        args += ["--guild-id", guild_id]
    args += ["--json"]
    ok, output = _run_cli(args, user=user or None)
    if ok:
        payload = _payload_of(output)
        items = payload.get("comments") or payload.get("items") or payload.get("list")
        if isinstance(items, list):
            ctx["comments"] = [x for x in items if isinstance(x, dict)]
    return ctx


def _newest_target(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """在评论及其回复中找最新一条，作为「评论回复」的目标。"""
    best: Optional[Dict[str, Any]] = None
    best_ts = -1
    for comment in ctx["comments"]:
        cid = str(comment.get("comment_id") or comment.get("commentId") or "").strip()
        comment_author = _item_author_id(comment)
        comment_ts = str(comment.get("comment_create_time") or comment.get("create_time_raw") or comment.get("create_time") or comment.get("createTime") or "").strip()
        base = {
            "feed_id": ctx["feed_id"],
            "guild_id": ctx["guild_id"],
            "comment_id": cid,
            "feed_author_id": ctx["feed_author_id"],
            "feed_create_time": ctx["feed_create_time"],
            "comment_author_id": comment_author,
            "comment_create_time": comment_ts,
        }
        ts = _create_time(comment)
        if cid and comment_author and comment_ts and ts >= best_ts:
            best_ts = ts
            best = {**base, "nick": _item_nick(comment), "content": _comment_content(comment)}
        replies = comment.get("replies") or comment.get("reply_list") or comment.get("replyList")
        if not isinstance(replies, list):
            continue
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            rid = str(reply.get("reply_id") or reply.get("replyId") or "").strip()
            reply_author = _item_author_id(reply)
            rts = _create_time(reply)
            if cid and comment_author and comment_ts and rid and reply_author and rts >= best_ts:
                best_ts = rts
                best = {
                    **base,
                    "target_reply_id": rid,
                    "target_user_id": reply_author,
                    "target_user_nick": _item_nick(reply),
                    "nick": _item_nick(reply),
                    "content": _comment_content(reply),
                }
    return best


# ==================== 评论列表图片渲染 ====================

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _find_font() -> Optional[str]:
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    for pattern in ("*CJK*", "wqy*", "msyh*", "simhei*", "PingFang*"):
        for base in ("/usr/share/fonts", "C:/Windows/Fonts", "/System/Library/Fonts"):
            root = Path(base)
            if root.is_dir():
                for found in root.rglob(pattern):
                    if found.suffix.lower() in (".ttc", ".ttf", ".otf"):
                        return str(found)
    return None


def _wrap_text(text: str, width: int) -> List[str]:
    lines: List[str] = []
    for raw_line in str(text or "").splitlines() or [""]:
        current = ""
        current_w = 0
        for ch in raw_line:
            w = 1 if ord(ch) < 128 else 2
            if current_w + w > width:
                lines.append(current)
                current, current_w = ch, w
            else:
                current += ch
                current_w += w
        lines.append(current)
    return lines or [""]


def render_comments_image(ctx: Dict[str, Any]) -> Optional[bytes]:
    """把帖子完整评论列表渲染成 PNG；Pillow/字体缺失时返回 None（回退文字）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    font_path = _find_font()
    if not font_path:
        return None
    import io

    font = ImageFont.truetype(font_path, 22)
    font_small = ImageFont.truetype(font_path, 18)
    font_title = ImageFont.truetype(font_path, 26)

    width, pad, line_h = 720, 24, 30
    rows: List[Tuple[str, Any, str]] = []  # (text, font, color)
    title = ctx.get("title") or ctx.get("feed_id") or ""
    rows.append((f"帖子评论 · {title}"[:40], font_title, "#1a1a1a"))
    rows.append((f"共 {len(ctx['comments'])} 条评论", font_small, "#888888"))
    rows.append(("", font_small, "#888888"))
    for comment in ctx["comments"]:
        nick = _item_nick(comment) or "未知用户"
        rows.append((f"● {nick}", font, "#2b5aa0"))
        for line in _wrap_text(_comment_content(comment), 52):
            rows.append((f"   {line}", font, "#1a1a1a"))
        replies = comment.get("replies") or comment.get("reply_list") or comment.get("replyList")
        if isinstance(replies, list):
            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                reply_nick = _item_nick(reply) or "未知用户"
                rows.append((f"    ↳ {reply_nick}", font_small, "#4a7a4a"))
                for line in _wrap_text(_comment_content(reply), 54):
                    rows.append((f"       {line}", font_small, "#333333"))
        rows.append(("", font_small, "#888888"))

    height = pad * 2 + line_h * len(rows)
    image = Image.new("RGB", (width, max(height, 160)), "#ffffff")
    draw = ImageDraw.Draw(image)
    y = pad
    for text, row_font, color in rows:
        if text:
            draw.text((pad, y), text, font=row_font, fill=color)
        y += line_h
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _comments_as_text(ctx: Dict[str, Any]) -> str:
    lines = [f"📋 评论列表（共 {len(ctx['comments'])} 条）"]
    for comment in ctx["comments"][:20]:
        nick = _item_nick(comment) or "未知用户"
        lines.append(f"● {nick}：{_comment_content(comment)[:NOTIFY_TEXT_LIMIT]}")
        replies = comment.get("replies") or comment.get("reply_list") or comment.get("replyList")
        if isinstance(replies, list):
            for reply in replies[:5]:
                if not isinstance(reply, dict):
                    continue
                reply_nick = _item_nick(reply) or "未知用户"
                lines.append(f"　↳ {reply_nick}：{_comment_content(reply)[:NOTIFY_TEXT_LIMIT]}")
    return "\n".join(lines)


# ==================== 通知发送 ====================

async def _notify_admins(user: str, item: Dict[str, Any], ctx: Dict[str, Any], target: Optional[Dict[str, Any]]) -> None:
    sender = _get_sender()
    if not sender:
        return
    who = (target or {}).get("nick") or "有人"
    what = (target or {}).get("content") or _notice_text(item)
    lines = [
        "💬 频道评论提醒" + (f"｜账号：{user}" if user else ""),
        f"帖子：{ctx.get('title') or ctx.get('feed_id')}",
        f"{who}：{str(what)[:NOTIFY_TEXT_LIMIT]}",
    ]
    if target:
        lines.append("直接发送「评论回复 内容」即可回复TA")
    text = "\n".join(lines)
    image = await asyncio.to_thread(render_comments_image, ctx)
    for admin in _load_admins():
        try:
            await sender.send_to_user(admin, text)
            if image:
                await sender.send_image("user", admin, image, "")
            elif ctx["comments"]:
                await sender.send_to_user(admin, _comments_as_text(ctx))
        except Exception:
            pass


# ==================== 轮询 ====================

async def _poll_slot(user: str) -> None:
    ok, output = await asyncio.to_thread(_run_cli, ["feed", "get-notices", "--json"], None, user or None)
    if not ok:
        return
    items = _notice_items(_payload_of(output))
    if not items:
        return
    seen = _load_seen(user)
    first_run = not seen and not _seen_file(user).exists()
    seen_set = set(seen)
    fresh: List[Tuple[str, Dict[str, Any]]] = []
    for item in items:
        key = _notice_key(item)
        if key not in seen_set:
            fresh.append((key, item))
    if not fresh:
        return
    seen.extend(key for key, _ in fresh)
    _save_seen(user, seen)
    if first_run:
        # 首次启动只登记历史通知，不补发提醒
        return
    for _, item in fresh:
        if not _is_comment_notice(item):
            continue
        feed_id, guild_id = _notice_feed(item)
        if not feed_id:
            continue
        ctx = await asyncio.to_thread(_fetch_feed_context, feed_id, guild_id, user)
        target = _newest_target(ctx)
        if target:
            _LAST_CTX.clear()
            _LAST_CTX.update({**target, "user": user})
        await _notify_admins(user, item, ctx, target)


async def _poll_loop() -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        if not notify_enabled():
            continue
        slots = _load_users()["users"] or [""]
        for user in slots:
            try:
                await _poll_slot(user)
            except Exception:
                pass


# ==================== 指令 ====================

@admin_handler(r"^评论通知\s*(开启|关闭)$", ignore_at_check=True)
async def handle_notify_toggle(event, match):
    enabled = match.group(1) == "开启"
    _set_switch("comment_notify_enabled", enabled)
    await event.reply(f"✅ 评论提醒已{'开启' if enabled else '关闭'}（每 {POLL_INTERVAL} 秒轮询各账号槽位的互动消息）")


@admin_handler(r"^评论回复\s+.+$", ignore_at_check=True)
async def handle_quick_reply(event, match):
    if not _LAST_CTX:
        await event.reply("暂无可回复的评论提醒（收到新的评论提醒后再试）")
        return
    parts = _text(event).split(None, 1)
    content = parts[1].strip() if len(parts) >= 2 else ""
    if not content:
        await event.reply("格式：评论回复 内容")
        return
    ctx = dict(_LAST_CTX)
    user = str(ctx.get("user") or "")
    replier_id = _get_self_user_id(ctx.get("guild_id") or None, user or None)
    if not replier_id:
        await event.reply("无法获取自己的用户ID，请先执行一次「频道用户资料」")
        return
    args = [
        "feed", "do-reply",
        "--feed-id", ctx["feed_id"],
        "--comment-id", ctx["comment_id"],
        "--replier-id", replier_id,
        "--feed-author-id", ctx["feed_author_id"],
        "--feed-create-time", ctx["feed_create_time"],
        "--comment-author-id", ctx["comment_author_id"],
        "--comment-create-time", ctx["comment_create_time"],
        "--content", content,
        "--json",
    ]
    if ctx.get("target_reply_id") and ctx.get("target_user_id"):
        args[2:2] = ["--target-reply-id", ctx["target_reply_id"], "--target-user-id", ctx["target_user_id"]]
        if ctx.get("target_user_nick"):
            args[2:2] = ["--target-user-nick", ctx["target_user_nick"]]
    ok, output = await asyncio.to_thread(_run_cli, args, None, user or None)
    output = _normalize_rate_limit(output)
    nick = ctx.get("target_user_nick") or ctx.get("nick") or "对方"
    if ok:
        await event.reply(f"✅ 已回复 {nick}：{content[:60]}")
    else:
        await event.reply(f"❌ 回复失败：{str(output)[:200]}")


# ==================== 生命周期 ====================

def _cancel_existing_tasks() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for task in asyncio.all_tasks(loop):
        if task.get_name() == _TASK_NAME and not task.done():
            task.cancel()


@on_load
async def _start_poller():
    _cancel_existing_tasks()
    asyncio.create_task(_poll_loop(), name=_TASK_NAME)


@on_unload
def _stop_poller():
    _cancel_existing_tasks()
