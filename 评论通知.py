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

指令：评论通知 开启/关闭（默认开启）；私信冷却 秒数（同一人多条私信在
冷却窗口内合并成一条提醒，默认 10 秒，0 为不合并）。
"""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.plugin.decorators import on_load, on_unload

from .腾讯频道 import (
    BASE_DIR,
    _extract_json,
    _get_self_user_id,
    _get_setting,
    _get_switch,
    _load_admins,
    _load_users,
    _normalize_rate_limit,
    _read_json_file,
    _run_cli,
    _set_setting,
    _set_switch,
    _text,
    _user_home,
    _write_json_file,
    admin_handler,
)

POLL_INTERVAL = 60
SEEN_MAX = 300
NOTIFY_TEXT_LIMIT = 120
CTX_MAX = 50
DM_MERGE_WINDOW_DEFAULT = 10
DM_MERGE_WINDOW_MAX = 600
_TASK_NAME = "txpd_comment_notify"

# 提醒上下文按编号保存（「评论回复 编号 内容」/「私信回复 编号 内容」）
_CTXS: Dict[int, Dict[str, Any]] = {}
_CTX_SEQ = [0]


def _push_ctx(ctx: Dict[str, Any]) -> int:
    _CTX_SEQ[0] += 1
    seq = _CTX_SEQ[0]
    _CTXS[seq] = ctx
    while len(_CTXS) > CTX_MAX:
        _CTXS.pop(min(_CTXS), None)
    return seq


def _latest_ctx(kind: str) -> Optional[Dict[str, Any]]:
    for seq in sorted(_CTXS, reverse=True):
        if _CTXS[seq].get("kind") == kind:
            return _CTXS[seq]
    return None


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
    return str(item.get("content") or item.get("content_text") or item.get("text") or item.get("summary") or item.get("title") or item.get("desc") or item.get("msg") or "").strip()


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
    raw = item.get("content")
    if isinstance(raw, dict) and isinstance(raw.get("images"), list) and raw["images"]:
        content = (content + " " if content else "") + "[图片]"
    return content or "-"


_NICK_KEYS = ("author_nick", "poster_nick", "nick", "nickname", "nick_name", "user_nick")
_AUTHOR_ID_KEYS = ("author_id", "poster_id", "comment_author_id", "reply_author_id", "authorId", "user_id", "tinyid", "poster_tiny_id")


def _item_nick(item: Dict[str, Any]) -> str:
    for key in _NICK_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    author = item.get("author")
    if isinstance(author, str) and author.strip():
        return author.strip()
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


def _iter_targets(ctx: Dict[str, Any]):
    """遍历评论及楼中楼回复，产出 (时间戳, 回复目标上下文)。"""
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
        if not (cid and comment_author and comment_ts):
            continue
        yield _create_time(comment), {**base, "nick": _item_nick(comment), "content": _comment_content(comment)}
        replies = comment.get("replies") or comment.get("replies_preview") or comment.get("reply_list") or comment.get("replyList")
        if not isinstance(replies, list):
            continue
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            rid = str(reply.get("reply_id") or reply.get("replyId") or "").strip()
            reply_author = _item_author_id(reply)
            if not (rid and reply_author):
                continue
            yield _create_time(reply), {
                **base,
                "target_reply_id": rid,
                "target_user_id": reply_author,
                "target_user_nick": _item_nick(reply),
                "nick": _item_nick(reply),
                "content": _comment_content(reply),
            }


def _newest_target(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """在评论及其回复中找最新一条，作为「评论回复」的目标。"""
    best: Optional[Dict[str, Any]] = None
    best_ts = -1
    for ts, target in _iter_targets(ctx):
        if ts >= best_ts:
            best_ts = ts
            best = target
    return best


def _target_from_item(item: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按通知里的 reply_id/comment_id 精确定位到对应那条评论/回复，
    避免多条新通知都显示成最新一条；定位不到时退回最新一条。"""
    reply_id = str(item.get("reply_id") or item.get("replyId") or item.get("target_reply_id") or "").strip()
    comment_id = str(item.get("comment_id") or item.get("commentId") or "").strip()
    if reply_id:
        for _, target in _iter_targets(ctx):
            if target.get("target_reply_id") == reply_id:
                return target
    if comment_id:
        candidates = [t for _, t in _iter_targets(ctx) if t.get("comment_id") == comment_id and not t.get("target_reply_id")]
        if candidates:
            return candidates[-1]
    # 真实 get-notices 没有 comment_id/reply_id，按通知摘要里冒号后的正文匹配
    summary = _notice_text(item)
    for sep in (":", "："):
        if sep in summary:
            summary = summary.split(sep, 1)[1].strip()
            break
    if summary:
        matched = [(ts, t) for ts, t in _iter_targets(ctx) if t.get("content", "").strip() == summary]
        if matched:
            return max(matched, key=lambda x: x[0])[1]
    return _newest_target(ctx)


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
        replies = comment.get("replies") or comment.get("replies_preview") or comment.get("reply_list") or comment.get("replyList")
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
        replies = comment.get("replies") or comment.get("replies_preview") or comment.get("reply_list") or comment.get("replyList")
        if isinstance(replies, list):
            for reply in replies[:5]:
                if not isinstance(reply, dict):
                    continue
                reply_nick = _item_nick(reply) or "未知用户"
                lines.append(f"　↳ {reply_nick}：{_comment_content(reply)[:NOTIFY_TEXT_LIMIT]}")
    return "\n".join(lines)


# ==================== 昵称补查 ====================

_NICK_CACHE: Dict[Tuple[str, str], str] = {}


def _lookup_nick(user: str, guild_id: str, tiny_id: str) -> str:
    """评论接口不带昵称时用 get-user-info 按 tiny_id 补查（按槽位缓存）。"""
    if not tiny_id:
        return ""
    key = (user, tiny_id)
    if key in _NICK_CACHE:
        return _NICK_CACHE[key]
    args = ["manage", "get-user-info", "--tiny-id", tiny_id, "--json"]
    if guild_id:
        args[2:2] = ["--guild-id", guild_id]
    ok, output = _run_cli(args, user=user or None)
    nick = ""
    if ok:
        payload = _payload_of(output)
        nick = _item_nick(payload)
        if not nick and isinstance(payload.get("member"), dict):
            nick = _item_nick(payload["member"])
    _NICK_CACHE[key] = nick
    if len(_NICK_CACHE) > 500:
        _NICK_CACHE.pop(next(iter(_NICK_CACHE)), None)
    return nick


def _fill_missing_nicks(ctx: Dict[str, Any], user: str, limit: int = 10) -> None:
    """给评论列表里缺昵称的作者补查昵称（写入 author_nick 供渲染使用）。"""
    guild_id = str(ctx.get("guild_id") or "")
    done = 0
    for comment in ctx["comments"]:
        entries = [comment]
        replies = comment.get("replies") or comment.get("replies_preview") or comment.get("reply_list") or comment.get("replyList")
        if isinstance(replies, list):
            entries += [r for r in replies if isinstance(r, dict)]
        for entry in entries:
            if _item_nick(entry):
                continue
            if done >= limit:
                return
            done += 1
            nick = _lookup_nick(user, guild_id, _item_author_id(entry))
            if nick:
                entry["author_nick"] = nick


# ==================== 通知发送 ====================

async def _dm_admins(text: str, image: Optional[bytes] = None, fallback_text: str = "", buttons: Optional[List[Any]] = None) -> None:
    """逐个私聊管理员，全部走主动消息推送（不关联 msg_id）。"""
    sender = _get_sender()
    if not sender:
        return
    for admin in _load_admins():
        try:
            await sender.send_to_user(admin, text, buttons=buttons)
            if image:
                await sender.send_image("user", admin, image, "")
            elif fallback_text:
                await sender.send_to_user(admin, fallback_text)
        except Exception:
            pass


async def _notify_admins(user: str, item: Dict[str, Any], ctx: Dict[str, Any], target: Optional[Dict[str, Any]], seq: int, with_image: bool = True) -> None:
    who = (target or {}).get("nick") or "有人"
    what = (target or {}).get("content") or _notice_text(item)
    lines = [
        f"💬 频道评论提醒 #{seq}" + (f"｜账号：{user}" if user else ""),
        f"帖子：{ctx.get('title') or ctx.get('feed_id')}",
        f"{who}：{str(what)[:NOTIFY_TEXT_LIMIT]}",
    ]
    if target:
        lines.append(f"发送「评论回复 {seq} 内容」回复TA（不带编号默认回最新一条）")
    image = await asyncio.to_thread(render_comments_image, ctx) if with_image else None
    fallback = _comments_as_text(ctx) if (with_image and not image and ctx["comments"]) else ""
    await _dm_admins("\n".join(lines), image, fallback)


# ==================== 轮询 ====================

async def _poll_slot(user: str) -> None:
    ok, output = await asyncio.to_thread(_run_cli, ["feed", "get-notices", "--json"], None, user or None)
    if not ok:
        return
    items = _notice_items(_payload_of(output))
    if not items:
        if not _seen_file(user).exists():
            _save_seen(user, [])
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
    imaged_feeds: set = set()
    for _, item in fresh:
        if not _is_comment_notice(item):
            continue
        feed_id, guild_id = _notice_feed(item)
        if not feed_id:
            continue
        ctx = await asyncio.to_thread(_fetch_feed_context, feed_id, guild_id, user)
        await asyncio.to_thread(_fill_missing_nicks, ctx, user)
        target = _target_from_item(item, ctx)
        if target and not target.get("nick"):
            author = target.get("target_user_id") or target.get("comment_author_id") or ""
            target["nick"] = await asyncio.to_thread(_lookup_nick, user, str(ctx.get("guild_id") or ""), str(author))
        seq = _push_ctx({**(target or {"feed_id": feed_id}), "kind": "comment", "user": user})
        # 同一帖子在一轮轮询里只发一次评论列表图片，避免重复
        await _notify_admins(user, item, ctx, target, seq, with_image=feed_id not in imaged_feeds)
        imaged_feeds.add(feed_id)


# ==================== 频道私信提醒 ====================

def _seed_subscription(user: str) -> None:
    """check-notices 需要本地订阅标记；直接写槽位自己的订阅状态文件，
    无需 CLI 的 OpenClaw 推送通道。"""
    base = _user_home(user) if user else Path.home()
    path = base / ".qqcli" / "subscription" / "state.json"
    try:
        if path.exists():
            data = _read_json_file(path, {})
            # CLI 可能自己写入 active:false（订阅未开启），必须改写为 true
            if isinstance(data, dict) and data.get("active") is True:
                return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"active": True, "subscribed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}), encoding="utf-8")
    except Exception:
        pass


def _is_dm_notice(item: Dict[str, Any]) -> bool:
    source = str(item.get("source") or "").lower()
    if source:
        return source == "dm"
    kind = str(item.get("type") or item.get("notice_type") or item.get("noticeType") or "").lower()
    if "dm" in kind or "private" in kind or "私信" in kind:
        return True
    return bool(item.get("peer_tiny_id") or item.get("peerTinyId") or item.get("from_tiny_id"))


def _dm_fields(item: Dict[str, Any]) -> Dict[str, str]:
    peer = str(item.get("peer_tiny_id") or item.get("peerTinyId") or item.get("from_tiny_id") or item.get("tiny_id") or item.get("poster_tiny_id") or item.get("sender_tiny_id") or "").strip()
    guild = str(item.get("source_guild_id") or item.get("sourceGuildId") or item.get("guild_id") or item.get("guildId") or "").strip()
    return {"peer_tiny_id": peer, "source_guild_id": guild, "ref": str(item.get("ref") or "").strip()}


_SENT_DM: List[Tuple[str, float]] = []


def _remember_sent_dm(text: str) -> None:
    now = time.time()
    _SENT_DM.append((text, now))
    _SENT_DM[:] = [(t, ts) for t, ts in _SENT_DM if now - ts < 600][-20:]


def _is_own_sent_dm(text: str) -> bool:
    """自己发出的私信也会出现在通知里，避免回声提醒。"""
    now = time.time()
    return any(t == text and now - ts < 600 for t, ts in _SENT_DM)


def _dm_notice_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """check-notices 的私信在 new_notices 列表里（source=dm）；兼容其它列表字段。"""
    for key in ("new_notices", "new_dm_notices", "dm_notices"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict) and _is_dm_notice(x)]
    return [x for x in _notice_items(payload) if _is_dm_notice(x)]


def dm_merge_window() -> int:
    """同一人私信合并冷却窗口（秒），0 为不合并。"""
    try:
        value = int(_get_setting("dm_merge_window", DM_MERGE_WINDOW_DEFAULT))
    except (TypeError, ValueError):
        return DM_MERGE_WINDOW_DEFAULT
    return max(0, min(value, DM_MERGE_WINDOW_MAX))


# 待发送的私信提醒，按（账号槽位, 对方）分组缓冲，冷却窗口内合并成一条
_DM_PENDING: Dict[Tuple[str, str], Dict[str, Any]] = {}


async def _flush_dm_group(key: Tuple[str, str], delay: float) -> None:
    if delay > 0:
        await asyncio.sleep(delay)
    entry = _DM_PENDING.pop(key, None)
    if not entry:
        return
    user = entry["user"]
    fields = entry["fields"]
    nick = entry["nick"]
    contents: List[str] = entry["contents"]
    seq = _push_ctx({"kind": "dm", "user": user, "nick": nick, **fields})
    lines = [f"✉️ 频道私信提醒 #{seq}" + (f"｜账号：{user}" if user else "")]
    who = nick or "对方"
    if len(contents) == 1:
        lines.append(f"{who}：{contents[0][:NOTIFY_TEXT_LIMIT]}")
    else:
        lines.append(f"{who}（{len(contents)} 条）：")
        lines += [f"・{c[:NOTIFY_TEXT_LIMIT]}" for c in contents]
    buttons = None
    if fields["ref"] or (fields["peer_tiny_id"] and fields["source_guild_id"]):
        lines.append(f"发送「私信回复 {seq} 内容」回复TA（不带编号默认回最新一条）")
        # 指令按钮：点击后把「私信回复 N 」填入输入框，补上内容回车即可
        buttons = [[{"text": f"私信回复 {seq}", "data": f"私信回复 {seq} ", "type": 2}]]
    await _dm_admins("\n".join(lines), buttons=buttons)


async def _queue_dm_notice(user: str, fields: Dict[str, str], nick: str, content: str) -> None:
    """同一人在冷却窗口内的多条私信合并成一条提醒。"""
    window = dm_merge_window()
    peer = fields["peer_tiny_id"] or fields["ref"] or nick or "unknown"
    key = (user, peer)
    entry = _DM_PENDING.get(key)
    if entry is not None:
        entry["contents"].append(content)
        entry["nick"] = entry["nick"] or nick
        for k, v in fields.items():
            if v:
                entry["fields"][k] = v
        return
    _DM_PENDING[key] = {"user": user, "fields": dict(fields), "nick": nick, "contents": [content]}
    if window <= 0:
        await _flush_dm_group(key, 0)
    else:
        asyncio.create_task(_flush_dm_group(key, window))


async def _poll_dm_slot(user: str) -> None:
    _seed_subscription(user)
    ok, output = await asyncio.to_thread(_run_cli, ["manage", "check-notices", "--json"], None, user or None)
    if not ok:
        return
    payload = _payload_of(output)
    items = _dm_notice_items(payload)
    if not items:
        return
    # check-notices 本身是增量接口（CLI 自己维护基线），返回的都是新通知；
    # 本地 seen 只做去重，不做首次基线吞掉。
    seen_path = (_user_home(user) if user else BASE_DIR) / "dm_notify_seen.json"
    data = _read_json_file(seen_path, {})
    seen = [str(x) for x in data.get("seen", [])] if isinstance(data.get("seen"), list) else []
    seen_set = set(seen)
    fresh = [(k, item) for item in items if (k := _notice_key(item)) not in seen_set]
    if not fresh:
        return
    seen.extend(k for k, _ in fresh)
    _write_json_file(seen_path, {"seen": seen[-SEEN_MAX:]})
    for _, item in fresh:
        if _is_own_sent_dm(_notice_text(item)):
            continue
        fields = _dm_fields(item)
        # 通知项自带的昵称字段可能是自己账号的，优先用对方 tiny_id 反查
        nick = ""
        if fields["peer_tiny_id"]:
            nick = await asyncio.to_thread(_lookup_nick, user, fields["source_guild_id"], fields["peer_tiny_id"])
        if not nick:
            nick = _item_nick(item)
        content = _notice_text(item) or "-"
        await _queue_dm_notice(user, fields, nick, content)


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
            try:
                await _poll_dm_slot(user)
            except Exception:
                pass


# ==================== 指令 ====================

@admin_handler(r"^评论通知\s*(开启|关闭)$", ignore_at_check=True)
async def handle_notify_toggle(event, match):
    enabled = match.group(1) == "开启"
    _set_switch("comment_notify_enabled", enabled)
    await event.reply(f"✅ 评论提醒已{'开启' if enabled else '关闭'}（每 {POLL_INTERVAL} 秒轮询各账号槽位的互动消息）")


@admin_handler(r"^私信冷却(\s+\d+)?$", ignore_at_check=True)
async def handle_dm_merge_window(event, match):
    raw = (match.group(1) or "").strip()
    if not raw:
        await event.reply(f"当前私信合并冷却：{dm_merge_window()} 秒（发送「私信冷却 秒数」修改，0 为不合并，最大 {DM_MERGE_WINDOW_MAX}）")
        return
    value = min(int(raw), DM_MERGE_WINDOW_MAX)
    _set_setting("dm_merge_window", value)
    await event.reply(f"✅ 私信合并冷却已设为 {value} 秒" + ("（不合并，每条单独提醒）" if value == 0 else f"（同一人 {value} 秒内的私信合并成一条）"))


def _parse_numbered(event) -> Tuple[Optional[int], str]:
    """解析「xx回复 [编号] 内容」，返回 (编号或None, 内容)。"""
    parts = _text(event).split(None, 1)
    rest = parts[1].strip() if len(parts) >= 2 else ""
    seq: Optional[int] = None
    sub = rest.split(None, 1)
    if sub and sub[0].isdigit():
        seq = int(sub[0])
        rest = sub[1].strip() if len(sub) >= 2 else ""
    return seq, rest


@admin_handler(r"^评论回复\s+.+$", ignore_at_check=True)
async def handle_quick_reply(event, match):
    seq, content = _parse_numbered(event)
    if seq is not None:
        found = _CTXS.get(seq)
        if not found or found.get("kind") != "comment":
            await event.reply(f"没有找到评论提醒 #{seq}（编号见提醒消息标题）")
            return
    else:
        found = _latest_ctx("comment")
        if not found:
            await event.reply("暂无可回复的评论提醒（收到新的评论提醒后再试）")
            return
    if not content:
        await event.reply("格式：评论回复 [编号] 内容")
        return
    ctx = dict(found)
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


@admin_handler(r"^私信回复\s+.+$", ignore_at_check=True)
async def handle_dm_reply(event, match):
    seq, content = _parse_numbered(event)
    if seq is not None:
        found = _CTXS.get(seq)
        if not found or found.get("kind") != "dm":
            await event.reply(f"没有找到私信提醒 #{seq}（编号见提醒消息标题）")
            return
    else:
        found = _latest_ctx("dm")
        if not found:
            await event.reply("暂无可回复的私信提醒（收到新的私信提醒后再试）")
            return
    if not content:
        await event.reply("格式：私信回复 [编号] 内容")
        return
    user = str(found.get("user") or "")
    if found.get("peer_tiny_id") and found.get("source_guild_id"):
        args = [
            "manage", "push-group-dm-msg",
            "--peer-tiny-id", found["peer_tiny_id"],
            "--source-guild-id", found["source_guild_id"],
            "--text", content,
            "--json",
        ]
    elif found.get("ref"):
        # 通知项没带对方 tinyID 时用 CLI 本地通知编号回复（自动查对方信息）
        args = ["manage", "push-group-dm-msg", "--ref", found["ref"], "--text", content, "--json"]
    else:
        await event.reply("这条私信提醒缺少对方信息，无法直接回复")
        return
    ok, output = await asyncio.to_thread(_run_cli, args, None, user or None)
    output = _normalize_rate_limit(output)
    nick = found.get("nick") or "对方"
    if ok:
        _remember_sent_dm(content)
        await event.reply(f"✅ 已私信回复 {nick}：{content[:60]}")
    else:
        await event.reply(f"❌ 私信回复失败：{str(output)[:200]}")


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
