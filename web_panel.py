#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Web 管理面板：把指令能力做成后台界面（频道 / 版块 / 帖子 / 评论 / 成员 / 定时发帖 / 插件管理员）。

所有路由默认复用后台登录鉴权（auth=True）。
"""

import asyncio
from typing import Any, Dict, List

from aiohttp import web

from core.plugin.decorators import on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page

from . import feed_scheduler
from .腾讯频道 import (
    BASE_DIR,
    _extract_json,
    _get_self_user_id,
    _load_admins,
    _load_users,
    _normalize_rate_limit,
    _run_cli,
    _save_admins,
    add_user,
    remove_user,
    set_user_nickname,
    switch_user,
)

PAGE_KEY = "txpd-panel"


# ==================== CLI 动作白名单 ====================
# 每个动作: base=CLI 子命令, required/optional=参数名→CLI flag, extra=固定附加参数

ACTIONS: Dict[str, Dict[str, Any]] = {
    # ── 查询 ──
    "guilds": {"base": ["manage", "get-my-join-guild-info"]},
    "guild-info": {"base": ["manage", "get-guild-info"], "required": {"guild_id": "--guild-id"}},
    "guild-share-url": {"base": ["manage", "get-guild-share-url"], "required": {"guild_id": "--guild-id"}},
    "channels": {"base": ["manage", "get-guild-channel-list"], "required": {"guild_id": "--guild-id"}},
    "members": {"base": ["manage", "get-guild-member-list"], "required": {"guild_id": "--guild-id"}, "optional": {"next_page_token": "--next-page-token"}},
    "user-info": {"base": ["manage", "get-user-info"], "optional": {"guild_id": "--guild-id", "tiny_id": "--tiny-id"}},
    "join-setting": {"base": ["manage", "get-join-guild-setting"], "required": {"guild_id": "--guild-id"}},
    "search-guild": {"base": ["manage", "search-guild-content"], "required": {"keyword": "--keyword"}, "optional": {"scope": "--scope"}},
    "feeds": {"base": ["feed", "get-guild-feeds"], "required": {"guild_id": "--guild-id"}, "optional": {"get_type": "--get-type", "feed_attach_info": "--feed-attach-info"}, "defaults": {"get_type": "2"}},
    "feed-detail": {"base": ["feed", "get-feed-detail"], "required": {"feed_id": "--feed-id"}, "optional": {"guild_id": "--guild-id"}},
    "feed-share-url": {"base": ["feed", "get-feed-share-url"], "required": {"feed_id": "--feed-id"}},
    "feed-search": {"base": ["feed", "search-guild-feeds"], "required": {"guild_id": "--guild-id", "keyword": "--keyword"}, "optional": {"next_page_cookie": "--next-page-cookie"}},
    "comments": {"base": ["feed", "get-feed-comments"], "required": {"feed_id": "--feed-id"}, "optional": {"guild_id": "--guild-id", "channel_id": "--channel-id", "attach_info": "--attach-info"}},
    "replies": {"base": ["feed", "get-next-page-replies"], "required": {"feed_id": "--feed-id", "comment_id": "--comment-id"}, "optional": {"guild_id": "--guild-id", "channel_id": "--channel-id", "attach_info": "--attach-info"}},
    "notices": {"base": ["feed", "get-notices"], "optional": {"attach_info": "--attach-info"}},
    "login-status": {"base": ["login", "status"]},
    "login": {"base": ["login"]},
    "login-force": {"base": ["login"], "yes": True},
    "login-poll": {"base": ["login", "poll-token"]},
    "logout": {"base": ["login", "logout"]},
    # ── 频道管理 ──
    "join-guild": {"base": ["manage", "join-guild"], "required": {"guild_id": "--guild-id"}},
    "leave-guild": {"base": ["manage", "leave-guild"], "required": {"guild_id": "--guild-id"}, "yes": True},
    "add-admin": {"base": ["manage", "add-admin"], "required": {"guild_id": "--guild-id", "tiny_ids": "--tiny-ids"}, "yes": True},
    "remove-admin": {"base": ["manage", "remove-admin"], "required": {"guild_id": "--guild-id", "tiny_ids": "--tiny-ids"}, "yes": True},
    "mute": {"base": ["manage", "modify-member-shut-up"], "required": {"guild_id": "--guild-id", "tiny_id": "--tiny-id", "time_stamp": "--time-stamp"}},
    "kick": {"base": ["manage", "kick-guild-member"], "required": {"guild_id": "--guild-id", "tiny_id": "--tiny-id"}, "yes": True},
    "create-channel": {"base": ["manage", "create-channel"], "required": {"guild_id": "--guild-id", "channel_name": "--channel-name"}},
    "modify-channel": {"base": ["manage", "modify-channel"], "required": {"guild_id": "--guild-id", "channel_id": "--channel-id", "channel_name": "--channel-name"}},
    "delete-channel": {"base": ["manage", "delete-channel"], "required": {"guild_id": "--guild-id", "channel_ids": "--channel-ids"}},
    "update-guild-name": {"base": ["manage", "update-guild-info"], "required": {"guild_id": "--guild-id", "guild_name": "--guild-name"}},
    "update-guild-profile": {"base": ["manage", "update-guild-info"], "required": {"guild_id": "--guild-id", "guild_profile": "--guild-profile"}},
    "set-join-type": {"base": ["manage", "update-join-guild-setting"], "required": {"guild_id": "--guild-id", "join_type": "--join-type"}},
    # ── 帖子与评论 ──
    "do-comment": {"base": ["feed", "do-comment"], "required": {"feed_id": "--feed-id", "feed_create_time": "--feed-create-time", "content": "--content"}, "optional": {"guild_id": "--guild-id", "channel_id": "--channel-id"}},
    "do-reply": {"base": ["feed", "do-reply"], "required": {"feed_id": "--feed-id", "comment_id": "--comment-id", "feed_author_id": "--feed-author-id", "feed_create_time": "--feed-create-time", "comment_author_id": "--comment-author-id", "comment_create_time": "--comment-create-time", "content": "--content"}, "optional": {"guild_id": "--guild-id", "channel_id": "--channel-id", "target_reply_id": "--target-reply-id", "target_user_id": "--target-user-id"}, "replier": True},
    "del-comment": {"base": ["feed", "do-comment", "--comment-type", "0"], "required": {"feed_id": "--feed-id", "comment_id": "--comment-id", "comment_author_id": "--comment-author-id", "feed_create_time": "--feed-create-time"}, "optional": {"guild_id": "--guild-id", "channel_id": "--channel-id"}, "yes": True},
    "del-feed": {"base": ["feed", "del-feed"], "required": {"feed_id": "--feed-id", "guild_id": "--guild-id", "channel_id": "--channel-id", "create_time": "--create-time"}, "yes": True},
    "like-feed": {"base": ["feed", "do-feed-prefer"], "required": {"feed_id": "--feed-id", "action": "--action"}, "optional": {"guild_id": "--guild-id", "channel_id": "--channel-id"}},
    "like-comment": {"base": ["feed", "do-like"], "required": {"like_type": "--like-type", "feed_id": "--feed-id", "comment_id": "--comment-id", "feed_author_id": "--feed-author-id", "feed_create_time": "--feed-create-time", "comment_author_id": "--comment-author-id"}, "optional": {"guild_id": "--guild-id", "channel_id": "--channel-id"}},
    "set-essence": {"base": ["feed", "set-feed-essence"], "required": {"feed_id": "--feed-id", "action": "--action"}},
    "top-feed": {"base": ["feed", "top-feed"], "required": {"feed_id": "--feed-id", "user_id": "--user-id", "create_time": "--create-time", "guild_id": "--guild-id", "action": "--action"}, "optional": {"top_type": "--top-type"}, "defaults": {"top_type": "1"}},
}


def _build_action_args(action: str, params: Dict[str, Any], user: str = "") -> Any:
    spec = ACTIONS.get(action)
    if not spec:
        return {"error": f"未知操作: {action}"}
    args: List[str] = list(spec["base"])
    defaults = spec.get("defaults") or {}
    for key, flag in (spec.get("required") or {}).items():
        value = str(params.get(key) or defaults.get(key) or "").strip()
        if not value:
            return {"error": f"缺少参数: {key}"}
        args += [flag, value]
    for key, flag in (spec.get("optional") or {}).items():
        value = str(params.get(key) or defaults.get(key) or "").strip()
        if value:
            args += [flag, value]
    if spec.get("replier"):
        replier_id = str(params.get("replier_id") or "").strip() or _get_self_user_id(str(params.get("guild_id") or "").strip() or None, user or None) or _get_self_user_id(None, user or None)
        if not replier_id:
            return {"error": "无法获取自己的用户ID，请先在面板执行一次「用户资料」"}
        args += ["--replier-id", replier_id]
    if spec.get("yes"):
        args.append("--yes")
    args.append("--json")
    return {"args": args}


async def _run_cli_json(args: List[str], user: str = "") -> Dict[str, Any]:
    ok, output = await asyncio.to_thread(_run_cli, args, None, user or None)
    output = _normalize_rate_limit(output)
    data = _extract_json(output)
    result: Dict[str, Any] = {"success": ok}
    if data is not None:
        result["data"] = data
    else:
        result["raw"] = output.strip()[-2000:]
    return result


async def _json_body(request: web.Request) -> Dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ==================== 路由 ====================

@register_route("POST", "/api/ext/txpd/cli")
async def api_cli(request: web.Request):
    body = await _json_body(request)
    action = str(body.get("action") or "").strip()
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    user = str(body.get("user") or "").strip()
    built = _build_action_args(action, params, user)
    if "error" in built:
        return web.json_response({"success": False, "message": built["error"]})
    return web.json_response(await _run_cli_json(built["args"], user))


# ==================== 账号槽位 ====================

@register_route("GET", "/api/ext/txpd/users")
async def api_get_users(request: web.Request):
    data = _load_users()
    return web.json_response({"success": True, "data": data})


@register_route("POST", "/api/ext/txpd/users")
async def api_manage_users(request: web.Request):
    body = await _json_body(request)
    op = str(body.get("op") or "").strip()
    name = str(body.get("name") or "").strip()
    if op == "add":
        ok, msg = add_user(name)
    elif op == "delete":
        ok, msg = remove_user(name)
    elif op == "switch":
        ok, msg = switch_user(name)
    else:
        ok, msg = False, "未知操作"
    return web.json_response({"success": ok, "message": msg, "data": _load_users()})


@register_route("POST", "/api/ext/txpd/users/status")
async def api_user_status(request: web.Request):
    """查询指定槽位的登录状态与昵称（昵称成功时顺带缓存）。"""
    body = await _json_body(request)
    name = str(body.get("name") or "").strip()
    if not name or name not in _load_users()["users"]:
        return web.json_response({"success": False, "message": f"槽位「{name}」不存在"})
    status = await _run_cli_json(["login", "status", "--json"], name)
    nickname = ""
    info = await _run_cli_json(["manage", "get-user-info", "--json"], name)
    payload = info.get("data") if isinstance(info.get("data"), dict) else {}
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if isinstance(payload, dict):
        nickname = str(payload.get("nickname") or payload.get("nick") or "").strip()
    if nickname:
        set_user_nickname(name, nickname)
    return web.json_response({"success": True, "data": {"name": name, "status": status, "nickname": nickname}})


@register_route("GET", "/api/ext/txpd/history")
async def api_get_history(request: web.Request):
    return web.json_response({"success": True, "data": {"history": feed_scheduler.load_history()}})


@register_route("GET", "/api/ext/txpd/admins")
async def api_get_admins(request: web.Request):
    return web.json_response({"success": True, "data": {"admins": _load_admins()}})


@register_route("POST", "/api/ext/txpd/admins")
async def api_save_admins(request: web.Request):
    body = await _json_body(request)
    raw = body.get("admins")
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        return web.json_response({"success": False, "message": "参数格式错误"})
    admins = [str(x).strip() for x in raw if str(x or "").strip() and not str(x).strip().startswith("#")]
    if not admins:
        return web.json_response({"success": False, "message": "管理员列表不能为空（至少保留一个）"})
    if not _save_admins(admins):
        return web.json_response({"success": False, "message": "保存失败"})
    return web.json_response({"success": True, "message": f"已保存 {len(admins)} 个管理员", "data": {"admins": _load_admins()}})


@register_route("GET", "/api/ext/txpd/schedules")
async def api_list_schedules(request: web.Request):
    return web.json_response({"success": True, "data": {
        "schedules": feed_scheduler.load_schedules(),
        "cron_examples": [{"expr": e, "desc": d} for e, d in feed_scheduler.CRON_EXAMPLES],
    }})


@register_route("POST", "/api/ext/txpd/schedules/save")
async def api_save_schedule(request: web.Request):
    body = await _json_body(request)
    normalized = feed_scheduler.normalize_schedule(body)
    if "error" in normalized:
        return web.json_response({"success": False, "message": normalized["error"]})
    schedules = feed_scheduler.load_schedules()
    index = next((i for i, s in enumerate(schedules) if s.get("id") == normalized["id"]), None)
    if index is not None:
        normalized["last_run"] = schedules[index].get("last_run", "")
        normalized["last_result"] = schedules[index].get("last_result", "")
        schedules[index] = normalized
    else:
        schedules.append(normalized)
    if not feed_scheduler.save_schedules(schedules):
        return web.json_response({"success": False, "message": "保存失败"})
    feed_scheduler.record_history({**normalized, "kind": "schedule"})
    return web.json_response({"success": True, "message": "保存成功", "data": {"schedule": normalized}})


@register_route("POST", "/api/ext/txpd/schedules/delete")
async def api_delete_schedule(request: web.Request):
    body = await _json_body(request)
    schedule_id = str(body.get("id") or "").strip()
    if not schedule_id:
        return web.json_response({"success": False, "message": "缺少计划ID"})
    schedules = [s for s in feed_scheduler.load_schedules() if s.get("id") != schedule_id]
    if not feed_scheduler.save_schedules(schedules):
        return web.json_response({"success": False, "message": "删除失败"})
    return web.json_response({"success": True, "message": "删除成功"})


@register_route("POST", "/api/ext/txpd/schedules/toggle")
async def api_toggle_schedule(request: web.Request):
    body = await _json_body(request)
    schedule_id = str(body.get("id") or "").strip()
    schedules = feed_scheduler.load_schedules()
    for schedule in schedules:
        if schedule.get("id") == schedule_id:
            schedule["enabled"] = not schedule.get("enabled", False)
            break
    else:
        return web.json_response({"success": False, "message": "计划不存在"})
    if not feed_scheduler.save_schedules(schedules):
        return web.json_response({"success": False, "message": "保存失败"})
    return web.json_response({"success": True, "message": "操作成功"})


@register_route("POST", "/api/ext/txpd/schedules/run")
async def api_run_schedule(request: web.Request):
    body = await _json_body(request)
    schedule_id = str(body.get("id") or "").strip()
    schedule = next((s for s in feed_scheduler.load_schedules() if s.get("id") == schedule_id), None)
    if not schedule:
        return web.json_response({"success": False, "message": "计划不存在"})
    result = await feed_scheduler.run_schedule(schedule)
    return web.json_response({"success": result["ok"], "message": result["message"]})


@register_route("POST", "/api/ext/txpd/publish")
async def api_publish_feed(request: web.Request):
    """立即发帖（与定时发帖同一套参数：format=text/md/html + images/videos）。"""
    body = await _json_body(request)
    normalized = feed_scheduler.normalize_schedule({**body, "cron": "* * * * *"})
    if "error" in normalized:
        return web.json_response({"success": False, "message": normalized["error"]})
    feed_scheduler.record_history({**normalized, "kind": "publish"})
    result = await asyncio.to_thread(feed_scheduler.run_schedule_sync, normalized)
    return web.json_response({"success": result["ok"], "message": result["message"]})


# ==================== 页面注册 ====================

@on_load
def _register_panel():
    register_page(
        key=PAGE_KEY,
        label="腾讯频道",
        source="plugin",
        source_name="txpd",
        html_file=str(BASE_DIR / "web" / "page.html"),
        icon="message-square",
    )


@on_unload
def _unregister_panel():
    try:
        unregister_page(PAGE_KEY)
    except Exception:
        pass
