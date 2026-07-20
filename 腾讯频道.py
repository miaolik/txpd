#!/usr/bin/env python
# -*- coding: utf-8 -*-

import functools
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import shutil

from core.plugin.decorators import handler


BASE_DIR = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform.startswith("win")
LOCAL_NPM_DIR = BASE_DIR / ".cli" / "node_modules"
LOCAL_CLI_BINS = (
    LOCAL_NPM_DIR / ".bin" / "tencent-channel-cli",
    LOCAL_NPM_DIR / "tencent-channel-cli-linux-x64" / "bin" / "tencent-channel-cli",
    LOCAL_NPM_DIR / "tencent-channel-cli-linux-arm64" / "bin" / "tencent-channel-cli",
    LOCAL_NPM_DIR / "tencent-channel-cli" / "bin" / "tencent-channel-cli",
)


def _cli_env(user: Optional[str] = None) -> Dict[str, str]:
    """CLI 子进程环境。多账号模式下每个账号槽位用独立的 HOME/USERPROFILE
    （users/槽位名）隔离 ~/.qqcli 登录态；未创建任何槽位时保持原有行为：
    Windows 用系统环境，Linux/macOS 在 HOME 缺失或不可写时回退到插件目录 .home。"""
    env = dict(os.environ)
    name = _safe_user_name(user) or get_current_user()
    if name:
        home = _user_home(name)
        try:
            home.mkdir(parents=True, exist_ok=True)
        except Exception:
            return env
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        return env
    if IS_WINDOWS:
        return env
    home = env.get("HOME", "")
    if not home or not os.path.isdir(home) or not os.access(home, os.W_OK):
        fallback = BASE_DIR / ".home"
        try:
            fallback.mkdir(exist_ok=True)
        except Exception:
            return env
        env["HOME"] = str(fallback)
    return env


def _ensure_executable(path: Path) -> None:
    try:
        if not os.access(path, os.X_OK):
            path.chmod(path.stat().st_mode | 0o755)
    except Exception:
        pass


def _resolve_cli() -> Optional[str]:
    """CLI 查找顺序：插件目录内置二进制（Windows: exe/cmd；Linux/macOS: linux-x64 等）
    → 插件目录本地 npm 安装（.cli）→ PATH（npm install -g tencent-channel-cli）。"""
    if IS_WINDOWS:
        local_names = ("tencent-channel-cli.exe", "tencent-channel-cli.cmd", "tencent-channel-cli")
        path_names = ("tencent-channel-cli", "tencent-channel-cli.cmd")
    else:
        local_names = (
            "tencent-channel-cli-linux-x64",
            "tencent-channel-cli-linux-arm64",
            "tencent-channel-cli-macos-x64",
            "tencent-channel-cli-macos-arm64",
            "tencent-channel-cli",
        )
        path_names = ("tencent-channel-cli",)
    for name in local_names:
        p = BASE_DIR / name
        if p.is_file():
            if not IS_WINDOWS:
                _ensure_executable(p)
            return str(p)
    if not IS_WINDOWS:
        for p in LOCAL_CLI_BINS:
            if p.is_file():
                _ensure_executable(p)
                return str(p)
    for name in path_names:
        found = shutil.which(name)
        if found and not found.lower().endswith(".ps1"):
            return found
    return None
PLUGIN_SETTINGS = BASE_DIR / "plugin_settings.json"
TOKEN_STORE = BASE_DIR / "token_store.json"
ADMINS_FILE = BASE_DIR / "admins.txt"
DEFAULT_ADMINS = ["538389445D765D2988BFE31506C54799"]
USERS_DIR = BASE_DIR / "users"
USERS_FILE = BASE_DIR / "users.json"


# ==================== 账号槽位（多账号登录） ====================
# 每个槽位对应 users/<槽位名>/ 目录，作为 CLI 子进程的 HOME，
# 登录态（~/.qqcli）与 token_store.json 按槽位隔离。
# 没有任何槽位时保持旧版单账号行为（系统 HOME + 插件目录 token_store.json）。

def _safe_user_name(name: Any) -> str:
    """槽位名校验：去除路径分隔符等危险字符，最长 32 字。非法返回空串。"""
    value = str(name or "").strip()
    if not value or value in (".", ".."):
        return ""
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', value):
        return ""
    return value[:32]


def _load_users() -> Dict[str, Any]:
    data = _read_json_file(USERS_FILE, {})
    users = data.get("users") if isinstance(data.get("users"), list) else []
    users = [u for u in (_safe_user_name(x) for x in users) if u]
    current = _safe_user_name(data.get("current"))
    if current not in users:
        current = users[0] if users else ""
    nicknames = data.get("nicknames") if isinstance(data.get("nicknames"), dict) else {}
    return {"current": current, "users": users, "nicknames": nicknames}


def _save_users(data: Dict[str, Any]) -> None:
    _write_json_file(USERS_FILE, data)


def list_users() -> List[str]:
    return _load_users()["users"]


def get_current_user() -> str:
    """当前账号槽位名；空字符串表示未创建任何槽位（旧版单账号模式）。"""
    return _load_users()["current"]


def _user_home(user: str) -> Path:
    return USERS_DIR / user


def add_user(name: Any) -> Tuple[bool, str]:
    user = _safe_user_name(name)
    if not user:
        return False, '槽位名无效（不能含 \\ / : * ? " < > | 等字符，最长 32 字）'
    data = _load_users()
    if user in data["users"]:
        return False, f"槽位「{user}」已存在"
    if len(data["users"]) >= 20:
        return False, "账号槽位最多 20 个，请先删除不用的槽位"
    try:
        _user_home(user).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"创建槽位目录失败：{e}"
    first = not data["users"]
    data["users"].append(user)
    if not data["current"]:
        data["current"] = user
    _save_users(data)
    if first:
        _migrate_legacy_login(user)
    return True, f"已创建账号槽位「{user}」" + ("（已设为当前槽位，并尝试迁移原有登录态）" if first else "")


def remove_user(name: Any) -> Tuple[bool, str]:
    user = _safe_user_name(name)
    data = _load_users()
    if not user or user not in data["users"]:
        return False, f"槽位「{name}」不存在"
    data["users"].remove(user)
    data["nicknames"].pop(user, None)
    if data["current"] == user:
        data["current"] = data["users"][0] if data["users"] else ""
    _save_users(data)
    try:
        shutil.rmtree(_user_home(user), ignore_errors=True)
    except Exception:
        pass
    return True, f"已删除账号槽位「{user}」" + (f"，当前槽位：{data['current'] or '无'}" if data["current"] != user else "")


def switch_user(name: Any) -> Tuple[bool, str]:
    user = _safe_user_name(name)
    data = _load_users()
    if not user or user not in data["users"]:
        return False, f"槽位「{name}」不存在，先用「频道添加账号 名称」创建"
    data["current"] = user
    _save_users(data)
    return True, f"已切换到账号槽位「{user}」"


def set_user_nickname(user: str, nickname: str) -> None:
    data = _load_users()
    if user in data["users"] and str(nickname or "").strip():
        data["nicknames"][user] = str(nickname).strip()
        _save_users(data)


def _migrate_legacy_login(user: str) -> None:
    """创建第一个槽位时，把旧版单账号的登录态（~/.qqcli）与 token_store 复制进槽位，
    避免升级后需要重新扫码。失败不影响使用（重新登录即可）。"""
    home = _user_home(user)
    try:
        legacy_homes = []
        if IS_WINDOWS:
            legacy_homes = [os.environ.get("USERPROFILE", ""), os.environ.get("HOME", "")]
        else:
            legacy_homes = [os.environ.get("HOME", ""), str(BASE_DIR / ".home")]
        for legacy in legacy_homes:
            src = Path(legacy) / ".qqcli" if legacy else None
            if src and src.is_dir():
                dst = home / ".qqcli"
                if not dst.exists():
                    shutil.copytree(src, dst)
                break
        if TOKEN_STORE.exists():
            dst_store = home / "token_store.json"
            if not dst_store.exists():
                shutil.copyfile(TOKEN_STORE, dst_store)
    except Exception:
        pass


def _load_admins() -> List[str]:
    """读取插件管理员列表（一行一个，# 开头为注释），文件不存在时用默认管理员初始化。"""
    try:
        if not ADMINS_FILE.exists():
            ADMINS_FILE.write_text("\n".join(DEFAULT_ADMINS) + "\n", encoding="utf-8")
        lines = ADMINS_FILE.read_text(encoding="utf-8").splitlines()
        admins = [x.strip() for x in lines if x.strip() and not x.strip().startswith("#")]
        return admins or list(DEFAULT_ADMINS)
    except Exception:
        return list(DEFAULT_ADMINS)


def _save_admins(admins: List[str]) -> bool:
    cleaned: List[str] = []
    for item in admins:
        value = str(item or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        return False
    try:
        ADMINS_FILE.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def _is_plugin_admin(user_id: Any) -> bool:
    uid = str(user_id or "").strip()
    if not uid:
        return False
    return uid.upper() in {a.upper() for a in _load_admins()}


def admin_handler(pattern: str, **kwargs):
    """同 @handler，但仅允许 admins.txt 中的插件管理员触发。"""
    kwargs.pop("owner_only", None)

    def decorator(func):
        @functools.wraps(func)
        async def wrapped(event, match):
            if not _is_plugin_admin(getattr(event, "user_id", "")):
                return
            return await func(event, match)

        return handler(pattern, **kwargs)(wrapped)

    return decorator


ENCODE_CMD_INPUT = False

# 本地分页每页显示条数
MEMBER_PAGE_SIZE = 24
FEED_PAGE_SIZE = 10
GUILD_LIST_PAGE_SIZE = 10

WRITE_ACTIONS = {
    "manage": {
        "join-guild",
        "upload-guild-avatar",
        "update-guild-info",
        "create-theme-private-guild",
        "create-channel",
        "delete-channel",
        "modify-channel",
        "update-join-guild-setting",
        "push-group-dm-msg",
        "leave-guild",
        "modify-member-shut-up",
        "kick-guild-member",
        "add-admin",
        "remove-admin",
        "search-and-join",
        "deal-notice",
    },
    "feed": {
        "publish-feed",
        "alter-feed",
        "del-feed",
        "do-feed-prefer",
        "set-feed-essence",
        "push-essence-feed",
        "top-feed",
        "do-comment",
        "do-reply",
        "do-like",
        "move-feed",
        "quick-publish",
        "search-and-comment",
        "delete-and-mute",
    },
}




@admin_handler(r"^频道帮助$", ignore_at_check=True)
async def handle_help(event, match):
    await event.reply(_help_text())

@admin_handler(r"^频道清理缓存$", ignore_at_check=True)
async def handle_clear_cache(event, match):
    store = _load_token_store()
    token_fp = str(store.get("__token_fp__") or "").strip()
    _save_token_store({"__token_fp__": token_fp} if token_fp else {})
    await event.reply("已清理频道缓存（self id 与短令牌）")

@admin_handler(r"^频道清理短令牌$", ignore_at_check=True)
async def handle_clear_short_tokens(event, match):
    store = _load_token_store()
    keep = {k: v for k, v in store.items() if str(k).startswith("__")}
    _save_token_store(keep)
    await event.reply("已清理短令牌缓存")

@admin_handler(r"^频道开启预演$", ignore_at_check=True)
async def handle_preview_on(event, match):
    _set_switch("preview_enabled", True)
    await event.reply("✅ 已开启预演模式，有风险的写操作会自动追加 --dry-run，仅验证参数，不会实际执行。")

@admin_handler(r"^频道关闭预演$", ignore_at_check=True)
async def handle_preview_off(event, match):
    _set_switch("preview_enabled", False)
    await event.reply("✅ 已关闭预演模式。")

@admin_handler(r"^频道开启调试$", ignore_at_check=True)
async def handle_debug_on(event, match):
    _set_switch("debug_enabled", True)
    await event.reply("✅ 已开启调试模式，后续会在消息末尾追加返回 JSON 大代码块。")

@admin_handler(r"^频道关闭调试$", ignore_at_check=True)
async def handle_debug_off(event, match):
    _set_switch("debug_enabled", False)
    await event.reply("✅ 已关闭调试模式，后续仅显示文本结果。")

@admin_handler(r"^频道配置token\s+.+$", ignore_at_check=True)
async def handle_token_setup(event, match):
    parts = _text(event).split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await event.reply("格式：频道配置token <token>")
        return
    token = parts[1].strip()
    _invalidate_self_user_cache(_fingerprint_token(token))
    ok, output = await asyncio.to_thread(_run_cli, ["token", "setup", token])
    if not ok and _is_unknown_command(output):
        await event.reply("当前版本 CLI 不支持手动配置 token，请使用「频道登录」扫码授权登录")
        return
    await event.reply(_render_result("配置 token", ok, _normalize_rate_limit(output), ["token", "setup", token]))

@admin_handler(r"^频道自检$", ignore_at_check=True)
async def handle_self_check(event, match):
    settings = _read_plugin_settings()
    store = _load_token_store()
    token_fp = str(store.get("__token_fp__") or "").strip()
    self_cache = store.get("__self_user__") if isinstance(store.get("__self_user__"), dict) else {}
    short_token_count = len([k for k, v in store.items() if not str(k).startswith("__") and isinstance(v, dict)])

    ok, output = await asyncio.to_thread(_run_cli_compat, ["login", "status", "--json"], ["token", "verify"])
    normalized_output = _normalize_rate_limit(output)
    data = _extract_json(normalized_output)
    verify_ok = bool(ok)
    verify_message = ""
    token_source = ""
    valid_flag = ""

    if isinstance(data, dict):
        success = data.get("success")
        if isinstance(success, bool):
            verify_ok = success
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        for key in ("message", "msg", "error", "description"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if value:
                verify_message = str(value)
                break
        if not verify_message:
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                verify_message = str(error["message"])
        if not verify_message:
            for key in ("message", "msg", "error", "description"):
                value = data.get(key)
                if value:
                    verify_message = str(value)
                    break
        token_source = str(payload.get("tokenSource") or payload.get("token_source") or "").strip() if isinstance(payload, dict) else ""
        valid_value = payload.get("valid") if isinstance(payload, dict) else None
        if isinstance(valid_value, bool):
            valid_flag = "有效" if valid_value else "无效"
    elif str(normalized_output or "").strip():
        verify_message = str(normalized_output).strip()

    rows = [
        ["登录校验", "正常" if verify_ok else "失败"],
        ["预演模式", "开启" if bool(settings.get("preview_enabled", True)) else "关闭"],
        ["调试模式", "开启" if bool(settings.get("debug_enabled", False)) else "关闭"],
        ["token 指纹", "已记录" if token_fp else "未记录"],
        ["self id 缓存", f"{len(self_cache)} 项"],
        ["短令牌缓存", f"{short_token_count} 项"],
    ]
    if token_source:
        rows.append(["token 来源", token_source])
    if valid_flag:
        rows.append(["token 有效性", valid_flag])

    lines = [
        f"{'✅' if verify_ok else '❌'} 频道自检",
        *_table(["项目", "状态"], rows),
    ]
    if verify_message:
        lines.extend([
            "登录说明",
            f"- {verify_message}",
        ])
    lines.extend([
        "",
        "快捷操作：" + " ".join([
            _quick_cmd("频道列表"),
            _quick_cmd("频道帮助"),
            _quick_cmd("频道清理缓存", "清理缓存"),
            _quick_cmd("频道清理短令牌", "清理短令牌"),
        ])
    ])
    if _debug_enabled() and str(normalized_output or "").strip():
        lines.append("")
        lines.append(_json_block(normalized_output))
    await event.reply("\n".join([x for x in lines if x is not None]))

@admin_handler(r"^频道列表(?:\s+\S+)?$", ignore_at_check=True)
async def handle_guild_list(event, match):
    parts = _parts(event)
    token = parts[1] if len(parts) >= 2 else None
    if token and re.fullmatch(r"g[0-9a-f]+", token):
        payload = _load_token_payload(token, kind="guild_list_page")
        if not payload:
            await event.reply("频道列表翻页令牌无效或已过期，请重新打开频道列表后再试")
            return
        expires_at = int(payload.get("expires_at") or 0)
        if expires_at and expires_at <= int(time.time()):
            ok, output = await asyncio.to_thread(_run_cli, ["manage", "get-my-join-guild-info", "--json"])
            if ok:
                data = _extract_json(output)
                payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
                _refresh_guild_roles(payload)
            await event.reply(_render_result("频道列表", ok, output, ["manage", "get-my-join-guild-info", "--json"]))
            return
        page_idx = int(payload.get("page_index", 0))
        all_groups = payload.get("all_groups")
        if not isinstance(all_groups, list) or not all_groups:
            await event.reply("频道列表翻页数据已失效，请重新打开频道列表后再试")
            return
        ok_output = json.dumps({"success": True, "data": {
            "_guild_list_groups": all_groups,
            "_guild_list_page_index": page_idx,
            "_guild_list_expires_at": expires_at,
        }}, ensure_ascii=False, separators=(",", ":"))
        await event.reply(_render_result("频道列表", True, ok_output, [], guild_id=None))
        return
    ok, output = await asyncio.to_thread(_run_cli, ["manage", "get-my-join-guild-info", "--json"])
    if ok:
        data = _extract_json(output)
        payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
        _refresh_guild_roles(payload)
    await event.reply(_render_result("频道列表", ok, output, ["manage", "get-my-join-guild-info", "--json"]))

@admin_handler(r"^频道资料\s+\S+$", ignore_at_check=True)
async def handle_guild_info(event, match):
    guild_id = _parts(event)[1]
    # 同时获取频道资料和分享链接
    ok_info, output_info = await asyncio.to_thread(_run_cli, _with_preview(["manage", "get-guild-info", "--guild-id", guild_id, "--json"]))
    ok_share, output_share = await asyncio.to_thread(_run_cli, _with_preview(["manage", "get-guild-share-url", "--guild-id", guild_id, "--json"]))
    # 合并分享链接到资料数据中
    if ok_info:
        data = _extract_json(output_info)
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict) and ok_share:
                share_data = _extract_json(output_share)
                if isinstance(share_data, dict):
                    share_inner = share_data.get("data")
                    if isinstance(share_inner, dict):
                        share_url = share_inner.get("url") or share_inner.get("share_url")
                        if share_url:
                            inner["share_url"] = share_url
                output_info = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    await event.reply(_render_result("频道资料", ok_info, _normalize_rate_limit(output_info),
                                ["manage", "get-guild-info", "--guild-id", guild_id, "--json"], guild_id=guild_id))

@admin_handler(r"^频道解析\s+.+$", ignore_at_check=True)
async def handle_share_parse(event, match):
    text = _text(event)
    # 尝试从消息中提取 URL（支持 pd.qq.com 链接）
    url_match = re.search(r"https?://pd\.qq\.com/\S+", text)
    if url_match:
        url = url_match.group(0)
    else:
        # 回退到原来的方式：取命令后的参数
        parts = text.split(None, 1)
        if len(parts) < 2:
            await event.reply("格式：频道解析 <URL> 或发送包含腾讯频道链接的消息")
            return
        url = parts[1].strip()
    await _reply_cli(event, ["manage", "get-share-info", "--url", url, "--json"], title="解析分享链接")

@admin_handler(r"^频道版块\s+\S+$", ignore_at_check=True)
async def handle_channel_list(event, match):
    guild_id = _parts(event)[1]
    await _reply_cli(event, ["manage", "get-guild-channel-list", "--guild-id", guild_id, "--json"], title="频道版块", guild_id=guild_id)

@admin_handler(r"^频道创建版块\s+.+$", ignore_at_check=True)
async def handle_create_channel(event, match):
    m = re.match(r"^频道创建版块\s+(\S+)\s+(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道创建版块 <频道ID> <版块名称>")
        return
    guild_id, channel_name = m.groups()
    await _reply_cli(event, ["manage", "create-channel", "--guild-id", guild_id, "--channel-name", channel_name.strip(), "--json"], title="创建版块", guild_id=guild_id)

@admin_handler(r"^频道修改版块\s+.+$", ignore_at_check=True)
async def handle_modify_channel(event, match):
    m = re.match(r"^频道修改版块\s+(\S+)\s+(\S+)\s+(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道修改版块 <频道ID> <版块ID> <新名称>")
        return
    guild_id, channel_id, channel_name = m.groups()
    await _reply_cli(event, ["manage", "modify-channel", "--guild-id", guild_id, "--channel-id", channel_id, "--channel-name", channel_name.strip(), "--json"], title="修改版块", guild_id=guild_id)

@admin_handler(r"^频道删除版块\s+\S+\s+\S+$", ignore_at_check=True)
async def handle_delete_channel(event, match):
    parts = _parts(event)
    if len(parts) < 3:
        await event.reply("格式：频道删除版块 <频道ID> <版块ID>")
        return
    guild_id, channel_id = parts[1], parts[2]
    await _reply_cli(event, ["manage", "delete-channel", "--guild-id", guild_id, "--channel-ids", channel_id, "--json"], title="删除版块", guild_id=guild_id)

@admin_handler(r"^频道建频道\s+.+$", ignore_at_check=True)
async def handle_create_theme_guild(event, match):
    parts = _parts(event)
    if len(parts) < 3:
        await event.reply("格式：频道建频道 <头像路径> <主题>")
        return
    image_path = parts[1]
    theme = " ".join(parts[2:]).strip()
    await _reply_cli(event, ["manage", "create-theme-private-guild", "--image-path", image_path, "--theme", theme, "--json"], title="按主题创建频道")

@admin_handler(r"^频道创建\s+.+$", ignore_at_check=True)
async def handle_create_custom_guild(event, match):
    m = re.match(r"^频道创建\s+(.+?)\s+(公开|私密|public|private)\s+(.+?)\s*\|\s*(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道创建 <头像路径> <公开|私密> <频道名> | <简介>")
        return
    image_path, community_type, guild_name, guild_profile = m.groups()
    ctype = "private" if community_type in {"私密", "private"} else "public"
    await _reply_cli(event, ["manage", "create-theme-private-guild", "--image-path", image_path.strip(), "--community-type", ctype, "--guild-name", guild_name.strip(), "--guild-profile", guild_profile.strip(), "--json"], title="创建频道")

@admin_handler(r"^频道改名\s+\S+\s+.+$", ignore_at_check=True)
async def handle_update_guild_name(event, match):
    parts = _parts(event)
    guild_id = parts[1]
    name = " ".join(parts[2:]).strip()
    await _reply_cli(event, ["manage", "update-guild-info", "--guild-id", guild_id, "--guild-name", name, "--json"], title="频道改名", guild_id=guild_id)

@admin_handler(r"^频道改简介\s+\S+\s+.+$", ignore_at_check=True)
async def handle_update_guild_profile(event, match):
    parts = _parts(event)
    guild_id = parts[1]
    profile = " ".join(parts[2:]).strip()
    await _reply_cli(event, ["manage", "update-guild-info", "--guild-id", guild_id, "--guild-profile", profile, "--json"], title="频道改简介", guild_id=guild_id)

@admin_handler(r"^频道改头像\s+\S+\s+.+$", ignore_at_check=True)
async def handle_upload_guild_avatar(event, match):
    parts = _parts(event)
    guild_id = parts[1]
    image_path = " ".join(parts[2:]).strip()
    await _reply_cli(event, ["manage", "upload-guild-avatar", "--guild-id", guild_id, "--image-path", image_path, "--json"], title="频道改头像", guild_id=guild_id)

@admin_handler(r"^频道成员\s+\S+(?:\s+\S+)?$", ignore_at_check=True)
async def handle_member_list(event, match):
    parts = _parts(event)
    guild_id = parts[1]
    if len(parts) >= 3 and re.fullmatch(r"m[0-9a-f]+", parts[2]):
        payload = _load_token_payload(parts[2], kind="member_page")
        if not payload:
            await event.reply("成员翻页令牌无效或已过期，请重新打开成员列表后再试")
            return
        # 本地翻页：从缓存取全部成员数据，按 page_index 切片渲染
        if "all_members" in payload and "page_index" in payload:
            page_idx = payload["page_index"]
            all_members = payload["all_members"]
            next_idx = page_idx + 1
            page_slice = all_members[next_idx * MEMBER_PAGE_SIZE: (next_idx + 1) * MEMBER_PAGE_SIZE]
            if not page_slice:
                await event.reply("已经是最后一页了")
                return
            # 构造假 payload 给 _render_summary，只包含当前页的成员 + 翻页信息
            fake_payload = dict(payload.get("raw_payload", {}))
            # 用当前切片替换原始分组，让渲染逻辑正常工作
            for role_key in ("owners", "admins", "robots", "ai_members", "members"):
                fake_payload.pop(role_key, None)
            fake_payload["_local_page_items"] = page_slice
            fake_payload["_local_page_index"] = next_idx
            fake_payload["_local_total"] = len(all_members)
            fake_payload["_local_guild_id"] = payload.get("guild_id") or guild_id
            # 更新令牌的 page_index
            payload["page_index"] = next_idx
            new_token = _save_token_payload("member_page", payload)
            fake_payload["_local_next_token"] = new_token
            fake_payload["_local_prev_cmd"] = payload.get("prev_cmd", f"频道成员 {guild_id}")
            ok_output = json.dumps({"success": True, "data": fake_payload}, ensure_ascii=False, separators=(",", ":"))
            await event.reply(_render_result("频道成员", True, ok_output, [], guild_id=payload.get("guild_id") or guild_id))
            return
        # 旧式 API 翻页令牌兼容（API 正常工作时走这里）
        await _reply_cli(event, ["manage", "get-guild-member-list", "--guild-id", payload["guild_id"], "--next-page-token", payload["next_page_token"], "--json"], title="频道成员", guild_id=payload["guild_id"])
        return
    args = ["manage", "get-guild-member-list", "--guild-id", guild_id, "--json"]
    if len(parts) >= 3:
        next_page_token = " ".join(parts[2:]).strip()
        if next_page_token:
            args += ["--next-page-token", next_page_token]
    await _reply_cli(event, args, title="频道成员", guild_id=guild_id)

@admin_handler(r"^频道搜成员\s+\S+\s+.+$", ignore_at_check=True)
async def handle_member_search(event, match):
    parts = _parts(event)
    guild_id = parts[1]
    keyword = " ".join(parts[2:]).strip()
    await _reply_cli(event, ["manage", "guild-member-search", "--guild-id", guild_id, "--keyword", keyword, "--json"], title="频道搜成员", guild_id=guild_id)

@admin_handler(r"^频道用户资料(?:\s+\S+)?(?:\s+\S+)?$", ignore_at_check=True)
async def handle_user_info(event, match):
    parts = _parts(event)
    if len(parts) == 1:
        await _reply_cli(event, ["manage", "get-user-info", "--json"], title="用户资料")
        return
    if len(parts) == 2:
        guild_id = parts[1]
        await _reply_cli(event, ["manage", "get-user-info", "--guild-id", guild_id, "--json"], title="用户资料", guild_id=guild_id)
        return
    guild_id = parts[1]
    tiny_id = parts[2]
    await _reply_cli(event, ["manage", "get-user-info", "--guild-id", guild_id, "--tiny-id", tiny_id, "--json"], title="用户资料", guild_id=guild_id)

@admin_handler(r"^频道设置管理员\s+\S+\s+\S+$", ignore_at_check=True)
async def handle_add_admin(event, match):
    guild_id, tiny_id = _parts(event)[1], _parts(event)[2]
    await _reply_cli(event, ["manage", "add-admin", "--guild-id", guild_id, "--tiny-ids", tiny_id, "--yes", "--json"], title="设置管理员", guild_id=guild_id)

@admin_handler(r"^频道取消管理员\s+\S+\s+\S+$", ignore_at_check=True)
async def handle_remove_admin(event, match):
    guild_id, tiny_id = _parts(event)[1], _parts(event)[2]
    await _reply_cli(event, ["manage", "remove-admin", "--guild-id", guild_id, "--tiny-ids", tiny_id, "--yes", "--json"], title="取消管理员", guild_id=guild_id)

@admin_handler(r"^频道禁言\s+\S+\s+\S+\s+.+$", ignore_at_check=True)
async def handle_shut_up_member(event, match):
    m = re.match(r"^频道禁言\s+(\S+)\s+(\S+)\s+(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道禁言 <频道ID> <用户ID> <时长>")
        return
    guild_id, tiny_id, duration_text = m.groups()
    timestamp = _parse_duration_to_timestamp(duration_text.strip())
    if timestamp is None:
        await event.reply("禁言时长格式错误，示例：频道禁言 频道ID 用户ID 3天2小时5分钟10秒")
        return
    await _reply_cli(event, ["manage", "modify-member-shut-up", "--guild-id", guild_id, "--tiny-id", tiny_id, "--time-stamp", str(timestamp), "--json"], title="设置禁言", guild_id=guild_id)

@admin_handler(r"^频道解除禁言\s+\S+\s+\S+$", ignore_at_check=True)
async def handle_unshut_up_member(event, match):
    guild_id, tiny_id = _parts(event)[1], _parts(event)[2]
    await _reply_cli(event, ["manage", "modify-member-shut-up", "--guild-id", guild_id, "--tiny-id", tiny_id, "--time-stamp", "0", "--json"], title="解除禁言", guild_id=guild_id)

@admin_handler(r"^频道踢出\s+\S+\s+\S+$", ignore_at_check=True)
async def handle_kick_member(event, match):
    guild_id, tiny_id = _parts(event)[1], _parts(event)[2]
    await _reply_cli(event, ["manage", "kick-guild-member", "--guild-id", guild_id, "--tiny-id", tiny_id, "--yes", "--json"], title="踢出成员", guild_id=guild_id)

@admin_handler(r"^频道搜频道\s+.+$", ignore_at_check=True)
async def handle_search_guilds(event, match):
    parts = _parts(event)
    # 支持翻页令牌（g 开头）
    if len(parts) >= 2 and re.fullmatch(r"s[0-9a-f]+", parts[1]):
        payload = _load_token_payload(parts[1], kind="search_guild_page")
        if not payload:
            await event.reply("搜频道翻页令牌无效或已过期，请重新搜索后再试")
            return
        args = ["manage", "search-guild-content", "--scope", "channel"]
        if payload.get("keyword"):
            args += ["--keyword", payload["keyword"]]
        if payload.get("next_page_token"):
            args += ["--page-token", payload["next_page_token"]]
        args += ["--json"]
        await _reply_cli(event, args, title="搜频道")
        return
    keyword = _text(event).split(None, 1)[1].strip()
    await _reply_cli(event, ["manage", "search-guild-content", "--keyword", keyword, "--scope", "channel", "--json"], title="搜频道")

@admin_handler(r"^频道搜作者\s+.+$", ignore_at_check=True)
async def handle_search_authors(event, match):
    parts = _parts(event)
    # 支持翻页令牌（g 开头）
    if len(parts) >= 2 and re.fullmatch(r"s[0-9a-f]+", parts[1]):
        payload = _load_token_payload(parts[1], kind="search_guild_page")
        if not payload:
            await event.reply("搜作者翻页令牌无效或已过期，请重新搜索后再试")
            return
        args = ["manage", "search-guild-content", "--scope", "author"]
        if payload.get("keyword"):
            args += ["--keyword", payload["keyword"]]
        if payload.get("next_page_token"):
            args += ["--page-token", payload["next_page_token"]]
        args += ["--json"]
        await _reply_cli(event, args, title="搜作者")
        return
    keyword = _text(event).split(None, 1)[1].strip()
    await _reply_cli(event, ["manage", "search-guild-content", "--keyword", keyword, "--scope", "author", "--json"], title="搜作者")

@admin_handler(r"^频道全局搜帖\s+.+$", ignore_at_check=True)
async def handle_search_feeds_global(event, match):
    parts = _parts(event)
    # 支持翻页令牌（s 开头）
    if len(parts) >= 2 and re.fullmatch(r"s[0-9a-f]+", parts[1]):
        payload = _load_token_payload(parts[1], kind="search_feed_global_page")
        if not payload:
            await event.reply("全局搜帖翻页令牌无效或已过期，请重新搜索后再试")
            return
        args = ["manage", "search-guild-content", "--scope", "feed"]
        if payload.get("keyword"):
            args += ["--keyword", payload["keyword"]]
        if payload.get("next_page_token"):
            args += ["--page-token", payload["next_page_token"]]
        args += ["--json"]
        await _reply_cli(event, args, title="全局搜帖")
        return
    keyword = _text(event).split(None, 1)[1].strip()
    await _reply_cli(event, ["manage", "search-guild-content", "--keyword", keyword, "--scope", "feed", "--json"], title="全局搜帖")

@admin_handler(r"^频道加入\s+\S+$", ignore_at_check=True)
async def handle_join_guild(event, match):
    guild_id = _parts(event)[1]
    await _reply_cli(event, ["manage", "join-guild", "--guild-id", guild_id, "--json"], title="加入频道", guild_id=guild_id)

@admin_handler(r"^频道加入附言\s+.+$", ignore_at_check=True)
async def handle_join_guild_with_comment(event, match):
    m = re.match(r"^频道加入附言\s+(\S+)\s+(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道加入附言 <频道ID> <附言>")
        return
    guild_id, comment = m.groups()
    payload = {"guild_id": guild_id, "join_guild_comment": comment.strip()}
    await _reply_cli_json_stdin(event, ["manage", "join-guild", "--json"], payload, title="加入频道", guild_id=guild_id)

@admin_handler(r"^频道加入答题\s+.+$", ignore_at_check=True)
async def handle_join_guild_with_answers(event, match):
    m = re.match(r"^频道加入答题\s+(\S+)\s+(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道加入答题 <频道ID> <答案1|答案2|答案3>")
        return
    guild_id, answers_text = m.groups()
    answers = [x.strip() for x in answers_text.split("|") if x.strip()]
    if not answers:
        await event.reply("至少需要提供一个答案，格式：频道加入答题 <频道ID> <答案1|答案2|答案3>")
        return
    payload = {"guild_id": guild_id, "join_guild_answers": [{"answer": x} for x in answers]}
    await _reply_cli_json_stdin(event, ["manage", "join-guild", "--json"], payload, title="加入频道", guild_id=guild_id)

@admin_handler(r"^频道加入方式\s+\S+$", ignore_at_check=True)
async def handle_join_setting(event, match):
    guild_id = _parts(event)[1]
    await _reply_cli(event, ["manage", "get-join-guild-setting", "--guild-id", guild_id, "--json"], title="加入方式", guild_id=guild_id)

@admin_handler(r"^频道设直接加入\s+\S+$", ignore_at_check=True)
async def handle_join_setting_direct(event, match):
    guild_id = _parts(event)[1]
    await _reply_cli(event, ["manage", "update-join-guild-setting", "--guild-id", guild_id, "--join-type", "JOIN_GUILD_TYPE_DIRECT", "--json"], title="设置直接加入", guild_id=guild_id)

@admin_handler(r"^频道设审核加入\s+\S+$", ignore_at_check=True)
async def handle_join_setting_audit(event, match):
    guild_id = _parts(event)[1]
    await _reply_cli(event, ["manage", "update-join-guild-setting", "--guild-id", guild_id, "--join-type", "JOIN_GUILD_TYPE_ADMIN_AUDIT", "--json"], title="设置审核加入", guild_id=guild_id)

@admin_handler(r"^频道设禁止加入\s+\S+$", ignore_at_check=True)
async def handle_join_setting_disable(event, match):
    guild_id = _parts(event)[1]
    await _reply_cli(event, ["manage", "update-join-guild-setting", "--guild-id", guild_id, "--join-type", "JOIN_GUILD_TYPE_DISABLE", "--json"], title="设置禁止加入", guild_id=guild_id)

@admin_handler(r"^频道加入提问审核\s+.+$", ignore_at_check=True)
async def handle_join_setting_question_audit(event, match):
    m = re.match(r"^频道加入提问审核\s+(\S+)\s+(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道加入提问审核 <频道ID> <问题1|问题2>")
        return
    guild_id, questions_text = m.groups()
    questions = [x.strip() for x in questions_text.split("|") if x.strip()]
    if not questions:
        await event.reply("至少需要一个问题，格式：频道加入提问审核 <频道ID> <问题1|问题2>")
        return
    payload = {
        "guild_id": guild_id,
        "join_type": "JOIN_GUILD_TYPE_QUESTION_WITH_ADMIN_AUDIT",
        "setting": {"question": {"items": [{"title": x} for x in questions]}},
    }
    await _reply_cli_json_stdin(event, ["manage", "update-join-guild-setting", "--json"], payload, title="设置提问审核", guild_id=guild_id)

@admin_handler(r"^频道加入多题验证\s+.+$", ignore_at_check=True)
async def handle_join_setting_multi_question(event, match):
    m = re.match(r"^频道加入多题验证\s+(\S+)\s+(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道加入多题验证 <频道ID> <问题1=答案1|问题2=答案2>")
        return
    guild_id, body = m.groups()
    items = []
    for part in [x.strip() for x in body.split("|") if x.strip()]:
        if "=" not in part:
            await event.reply("格式错误，示例：频道加入多题验证 频道ID 1+1=?=2|你是谁?=管理员")
            return
        title, answer = part.rsplit("=", 1)
        if not title.strip() or not answer.strip():
            await event.reply("问题和答案都不能为空")
            return
        items.append({"title": title.strip(), "answer": answer.strip()})
    payload = {
        "guild_id": guild_id,
        "join_type": "JOIN_GUILD_TYPE_MULTI_QUESTION",
        "setting": {"question": {"items": items}},
    }
    await _reply_cli_json_stdin(event, ["manage", "update-join-guild-setting", "--json"], payload, title="设置多题验证", guild_id=guild_id)

@admin_handler(r"^频道加入测试题\s+.+$", ignore_at_check=True)
async def handle_join_setting_quiz(event, match):
    m = re.match(r"^频道加入测试题\s+(\S+)\s+(.+?)\s*\|\s*(.+?)\s*\|\s*(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道加入测试题 <频道ID> <题目> | <选项1,选项2,选项3> | <正确答案>")
        return
    guild_id, question, answers_text, correct_answer = m.groups()
    answers = [x.strip() for x in answers_text.split(",") if x.strip()]
    correct_answer = correct_answer.strip()
    if len(answers) < 2:
        await event.reply("测试题至少需要 2 个选项")
        return
    payload = {
        "guild_id": guild_id,
        "join_type": "JOIN_GUILD_TYPE_QUIZ",
        "setting": {
            "quiz": {
                "items": [{"question": question.strip(), "answers": answers, "correctAnswer": correct_answer}],
                "minAnswerNum": 1,
                "minCorrectAnswerNum": 1,
            }
        },
    }
    await _reply_cli_json_stdin(event, ["manage", "update-join-guild-setting", "--json"], payload, title="设置测试题", guild_id=guild_id)

@admin_handler(r"^频道私信\s+\S+\s+\S+\s+.+$", ignore_at_check=True)
async def handle_push_group_dm(event, match):
    parts = _parts(event)
    source_guild_id = parts[1]
    peer_tiny_id = parts[2]
    text = " ".join(parts[3:]).strip()
    await _reply_cli(event, ["manage", "push-group-dm-msg", "--source-guild-id", source_guild_id, "--peer-tiny-id", peer_tiny_id, "--text", text, "--json"], title="发送频道私信", guild_id=source_guild_id)

@admin_handler(r"^频道退出\s+\S+$", ignore_at_check=True)
async def handle_leave_guild(event, match):
    guild_id = _parts(event)[1]
    await _reply_cli(event, ["manage", "leave-guild", "--guild-id", guild_id, "--yes", "--json"], title="退出频道", guild_id=guild_id)

@admin_handler(r"^互动消息(?:\s+\S+)?(?:\s+\S+)?$", ignore_at_check=True)
async def handle_notices(event, match):
    parts = _parts(event)
    # 支持翻页令牌（n 开头）
    if len(parts) >= 2 and re.fullmatch(r"n[0-9a-f]+", parts[1]):
        payload = _load_token_payload(parts[1], kind="notice_page")
        if not payload:
            await event.reply("互动消息翻页令牌无效或已过期，请重新打开互动消息后再试")
            return
        args = ["feed", "get-notices"]
        if payload.get("guild_id"):
            args += ["--guild-id", payload["guild_id"]]
        args += ["--attach-info", payload["attach_info"], "--json"]
        await _reply_cli(event, args, title="互动消息", guild_id=payload.get("guild_id"))
        return
    args = ["feed", "get-notices"]
    if len(parts) >= 2:
        args += ["--guild-id", parts[1]]
    if len(parts) >= 3:
        args += ["--attach-info", parts[2]]
    args += ["--json"]
    await _reply_cli(event, args, title="互动消息", guild_id=parts[1] if len(parts) >= 2 else None)

@admin_handler(r"^频道帖子\s+\S+(?:\s+\S+)?$", ignore_at_check=True)
async def handle_feed_list(event, match):
    parts = _parts(event)
    guild_id = parts[1]
    if len(parts) >= 3 and re.fullmatch(r"f[0-9a-f]+", parts[2]):
        payload = _load_token_payload(parts[2], kind="feed_page")
        if not payload:
            await event.reply("帖子翻页令牌无效或已过期，请重新打开帖子列表后再试")
            return
        await _reply_cli(event, ["feed", "get-guild-feeds", "--guild-id", payload["guild_id"], "--get-type", str(payload.get("get_type") or 2), "--feed-attach-info", payload["attach_info"], "--json"], title="频道帖子", guild_id=payload["guild_id"])
        return
    args = ["feed", "get-guild-feeds", "--guild-id", guild_id, "--get-type", "2"]
    if len(parts) >= 3:
        args += ["--feed-attach-info", " ".join(parts[2:]).strip()]
    args += ["--json"]
    await _reply_cli(event, args, title="频道帖子", guild_id=guild_id)

@admin_handler(r"^频道搜帖\s+\S+\s+.+$", ignore_at_check=True)
async def handle_search_feeds(event, match):
    parts = _parts(event)
    guild_id = parts[1]
    if len(parts) >= 3 and re.fullmatch(r"f[0-9a-f]+", parts[2]):
        payload = _load_token_payload(parts[2], kind="search_feed_page")
        if not payload:
            await event.reply("搜帖翻页令牌无效或已过期，请重新打开搜索结果后再试")
            return
        keyword = str(payload.get("query") or payload.get("keyword") or "").strip()
        if not keyword:
            await event.reply("搜帖翻页参数缺失，请重新执行 频道搜帖 <频道ID> <关键词>")
            return
        await _reply_cli(event, ["feed", "search-guild-feeds", "--guild-id", payload["guild_id"], "--keyword", keyword, "--next-page-cookie", payload["next_page_cookie"], "--json"], title="频道搜帖", guild_id=payload["guild_id"])
        return
    keyword = " ".join(parts[2:]).strip()
    if not keyword:
        await event.reply("格式：频道搜帖 <频道ID> <关键词>")
        return
    await _reply_cli(event, ["feed", "search-guild-feeds", "--guild-id", guild_id, "--keyword", keyword, "--json"], title="频道搜帖", guild_id=guild_id)


@admin_handler(r"^帖子详情\s+\S+(?:\s+\S+)?$", ignore_at_check=True)
async def handle_feed_detail(event, match):
    parts = _parts(event)
    feed_id = parts[1]
    args = ["feed", "get-feed-detail", "--feed-id", feed_id, "--json"]
    guild_id = parts[2] if len(parts) >= 3 else None
    if guild_id:
        args[2:2] = ["--guild-id", guild_id]
    await _reply_cli(event, args, title="帖子详情", guild_id=guild_id)

@admin_handler(r"^帖子评论\s+\S+$", ignore_at_check=True)
async def handle_feed_comments(event, match):
    parts = _parts(event)
    if len(parts) < 2:
        await event.reply("格式：帖子评论 <帖子ID> [频道ID] 或 帖子评论 <评论翻页令牌>")
        return
    if re.fullmatch(r"c[0-9a-f]+", parts[1]):
        payload = _load_token_payload(parts[1], kind="comment_page")
        if not payload:
            await event.reply("评论翻页令牌无效或已过期，请重新打开评论列表后再试")
            return
        args = ["feed", "get-feed-comments", "--feed-id", payload["feed_id"]]
        if payload.get("guild_id"):
            args += ["--guild-id", payload["guild_id"]]
        if payload.get("channel_id"):
            args += ["--channel-id", payload["channel_id"]]
        args += ["--attach-info", payload["attach_info"], "--json"]
        await _reply_cli(event, args, title="帖子评论", guild_id=payload.get("guild_id"))
        return
    feed_id = parts[1]
    guild_id = parts[2] if len(parts) >= 3 else None
    args = ["feed", "get-feed-comments", "--feed-id", feed_id]
    if guild_id:
        args += ["--guild-id", guild_id]
    args += ["--json"]
    await _reply_cli(event, args, title="帖子评论", guild_id=guild_id)

@admin_handler(r"^帖子回复\s+.+$", ignore_at_check=True)
async def handle_reply_list(event, match):
    """帖子回复统一入口：回复令牌回复评论 / 翻页令牌看更多回复 / 显式参数查回复列表。"""
    parts = _parts(event)
    if len(parts) >= 2 and re.fullmatch(r"r[0-9a-f]+", parts[1]):
        token = parts[1]
        payload = _load_token_payload(token, kind="reply_page")
        if not payload:
            if _load_token_payload(token, kind="reply_comment"):
                if len(parts) >= 3:
                    await handle_reply_comment(event, match)
                else:
                    await event.reply("格式：帖子回复 <回复令牌> <内容>")
                return
            await event.reply("回复令牌无效或已过期，请重新打开评论列表后再试")
            return
        args = [
            "feed", "get-next-page-replies",
            "--feed-id", payload["feed_id"],
            "--comment-id", payload["comment_id"],
            "--guild-id", payload["guild_id"],
            "--channel-id", payload["channel_id"],
        ]
        attach_info = payload.get("attach_info")
        if attach_info:
            args += ["--attach-info", attach_info]
        args += ["--json"]
        await _reply_cli(event, args, title="评论回复", guild_id=payload.get("guild_id"))
        return
    if len(parts) < 5:
        await event.reply("格式：帖子回复 <回复令牌> <内容> 或 帖子回复 <帖子ID> <评论ID> <频道ID> <版块ID> [attach_info]")
        return
    feed_id, comment_id, guild_id, channel_id = parts[1], parts[2], parts[3], parts[4]
    args = [
        "feed", "get-next-page-replies",
        "--feed-id", feed_id,
        "--comment-id", comment_id,
        "--guild-id", guild_id,
        "--channel-id", channel_id,
    ]
    if len(parts) >= 6:
        args += ["--attach-info", " ".join(parts[5:]).strip()]
    args += ["--json"]
    await _reply_cli(event, args, title="评论回复", guild_id=guild_id)

@admin_handler(r"^帖子评论\s+\S+\s+\S+(?:\s+\S+)?(?:\s+.+)?$", ignore_at_check=True)
async def handle_publish_comment(event, match):
    parts = _parts(event)
    if len(parts) < 4:
        await event.reply("格式：帖子评论 <帖子ID> <帖子创建时间> [频道ID] [版块ID] <内容>")
        return
    feed_id = parts[1]
    feed_create_time = parts[2]
    guild_id = None
    channel_id = None
    content_start = 3
    if len(parts) >= 6:
        guild_id = parts[3]
        channel_id = parts[4]
        content_start = 5
    content = " ".join(parts[content_start:]).strip()
    if not content:
        await event.reply("格式：帖子评论 <帖子ID> <帖子创建时间> [频道ID] [版块ID] <内容>")
        return
    args = ["feed", "do-comment", "--feed-id", feed_id, "--feed-create-time", feed_create_time, "--content", content, "--json"]
    if guild_id and channel_id:
        args[2:2] = ["--guild-id", guild_id, "--channel-id", channel_id]
    await _reply_cli(event, args, title="发表评论", guild_id=guild_id)

@admin_handler(r"^帖子分享链接\s+\S+(?:\s+\S+)?$", ignore_at_check=True)
async def handle_feed_share(event, match):
    parts = _parts(event)
    feed_id = parts[1]
    args = ["feed", "get-feed-share-url", "--feed-id", feed_id, "--json"]
    guild_id = parts[2] if len(parts) >= 3 else None
    if guild_id:
        args[2:2] = ["--guild-id", guild_id]
    await _reply_cli(event, args, title="帖子分享链接", guild_id=guild_id)

@admin_handler(r"^频道发帖\s+\S+\s+\S+\s+.+$", ignore_at_check=True)
async def handle_publish_feed(event, match):
    parts = _parts(event)
    guild_id = parts[1]
    channel_id = parts[2]
    content = " ".join(parts[3:]).strip()
    await _reply_cli(event, ["feed", "publish-feed", "--guild-id", guild_id, "--channel-id", channel_id, "--content", content, "--json"], title="发布帖子", guild_id=guild_id)

@admin_handler(r"^频道长帖\s+.+$", ignore_at_check=True)
async def handle_publish_long_feed(event, match):
    m = re.match(r"^频道长帖\s+(\S+)\s+(\S+)\s+(.+?)\s*\|\s*(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道长帖 <频道ID> <版块ID> <标题> | <正文>")
        return
    guild_id, channel_id, title, content = m.groups()
    await _reply_cli(event, ["feed", "publish-feed", "--guild-id", guild_id, "--channel-id", channel_id, "--title", title.strip(), "--content", content.strip(), "--json"], title="发布长帖", guild_id=guild_id)

@admin_handler(r"^帖子点赞\s+\S+(?:\s+\S+\s+\S+)?$", ignore_at_check=True)
async def handle_feed_like(event, match):
    parts = _parts(event)
    feed_id = parts[1]
    args = ["feed", "do-feed-prefer", "--feed-id", feed_id, "--action", "1", "--json"]
    guild_id = parts[2] if len(parts) >= 4 else None
    channel_id = parts[3] if len(parts) >= 4 else None
    if guild_id and channel_id:
        args[2:2] = ["--guild-id", guild_id, "--channel-id", channel_id]
    await _reply_cli(event, args, title="帖子点赞", guild_id=guild_id)

@admin_handler(r"^帖子取消点赞\s+\S+(?:\s+\S+\s+\S+)?$", ignore_at_check=True)
async def handle_feed_unlike(event, match):
    parts = _parts(event)
    feed_id = parts[1]
    args = ["feed", "do-feed-prefer", "--feed-id", feed_id, "--action", "3", "--json"]
    guild_id = parts[2] if len(parts) >= 4 else None
    channel_id = parts[3] if len(parts) >= 4 else None
    if guild_id and channel_id:
        args[2:2] = ["--guild-id", guild_id, "--channel-id", channel_id]
    await _reply_cli(event, args, title="帖子取消点赞", guild_id=guild_id)

@admin_handler(r"^帖子评论回复\s+.+$", ignore_at_check=True)
async def handle_reply_comment(event, match):
    parts = _parts(event)
    if len(parts) >= 3 and re.fullmatch(r"r[0-9a-f]+", parts[1]):
        token = parts[1]
        payload = _load_token_payload(token, kind="reply_comment")
        if not payload:
            await event.reply("回复令牌无效或已过期，请重新打开评论列表后再试")
            return
        content = " ".join(parts[2:]).strip()
        if not content:
            await event.reply("格式：帖子评论回复 <回复令牌> <内容>")
            return
        replier_id = payload.get("replier_id") or _get_self_user_id(payload.get("guild_id")) or _get_self_user_id()
        if not replier_id:
            await event.reply("无法自动获取自己的用户ID，请先执行一次：频道用户资料 或 频道用户资料 <频道ID>")
            return
        args = [
            "feed", "do-reply",
            "--feed-id", payload["feed_id"],
            "--comment-id", payload["comment_id"],
            "--replier-id", replier_id,
            "--feed-author-id", payload["feed_author_id"],
            "--feed-create-time", payload["feed_create_time"],
            "--comment-author-id", payload["comment_author_id"],
            "--comment-create-time", payload["comment_create_time"],
            "--content", content,
            "--json",
        ]
        if payload.get("target_reply_id") and payload.get("target_user_id"):
            args[2:2] = ["--target-reply-id", payload["target_reply_id"], "--target-user-id", payload["target_user_id"]]
            if payload.get("target_user_nick"):
                args[2:2] = ["--target-user-nick", payload["target_user_nick"]]
        await _reply_cli(event, args, title="回复评论", guild_id=payload.get("guild_id"))
        return
    m = re.match(r"^帖子评论回复\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：帖子评论回复 <回复令牌> <内容>")
        return
    feed_id, comment_id, replier_id, feed_author_id, feed_create_time, comment_author_id, comment_create_time, content = m.groups()
    await _reply_cli(event, [
        "feed", "do-reply",
        "--feed-id", feed_id,
        "--comment-id", comment_id,
        "--replier-id", replier_id,
        "--feed-author-id", feed_author_id,
        "--feed-create-time", feed_create_time,
        "--comment-author-id", comment_author_id,
        "--comment-create-time", comment_create_time,
        "--content", content.strip(),
        "--json",
    ], title="回复评论")


@admin_handler(r"^删除评论\s+.+$", ignore_at_check=True)
async def handle_delete_comment(event, match):
    parts = _parts(event)
    if len(parts) >= 2 and re.fullmatch(r"d[0-9a-f]+", parts[1]):
        payload = _load_token_payload(parts[1], kind="delete_comment")
        if not payload:
            await event.reply("删除评论令牌无效或已过期，请重新打开评论列表后再试")
            return
        args = [
            "feed", "do-comment",
            "--comment-type", "0",
            "--feed-id", payload["feed_id"],
            "--comment-id", payload["comment_id"],
            "--comment-author-id", payload["comment_author_id"],
            "--feed-create-time", payload["feed_create_time"],
            "--yes",
            "--json",
        ]
        if payload.get("guild_id") and payload.get("channel_id"):
            args[2:2] = ["--guild-id", payload["guild_id"], "--channel-id", payload["channel_id"]]
        await _reply_cli(event, args, title="删除评论", guild_id=payload.get("guild_id"))
        return
    if len(parts) < 5:
        await event.reply("格式：删除评论 <删除令牌> 或 删除评论 <帖子ID> <评论ID> <评论作者ID> <帖子创建时间> [频道ID] [版块ID]")
        return
    feed_id, comment_id, comment_author_id, feed_create_time = parts[1], parts[2], parts[3], parts[4]
    args = [
        "feed", "do-comment",
        "--comment-type", "0",
        "--feed-id", feed_id,
        "--comment-id", comment_id,
        "--comment-author-id", comment_author_id,
        "--feed-create-time", feed_create_time,
        "--yes",
        "--json",
    ]
    guild_id = parts[5] if len(parts) >= 7 else None
    channel_id = parts[6] if len(parts) >= 7 else None
    if guild_id and channel_id:
        args[2:2] = ["--guild-id", guild_id, "--channel-id", channel_id]
    await _reply_cli(event, args, title="删除评论", guild_id=guild_id)

@admin_handler(r"^删除回复\s+.+$", ignore_at_check=True)
async def handle_delete_reply(event, match):
    parts = _parts(event)
    if len(parts) >= 2 and re.fullmatch(r"d[0-9a-f]+", parts[1]):
        payload = _load_token_payload(parts[1], kind="delete_reply")
        if not payload:
            await event.reply("删除回复令牌无效或已过期，请重新打开回复列表后再试")
            return
        args = [
            "feed", "do-reply",
            "--reply-type", "0",
            "--feed-id", payload["feed_id"],
            "--comment-id", payload["comment_id"],
            "--reply-id", payload["reply_id"],
            "--replier-id", payload["replier_id"],
            "--feed-author-id", payload["feed_author_id"],
            "--feed-create-time", payload["feed_create_time"],
            "--comment-author-id", payload["comment_author_id"],
            "--comment-create-time", payload["comment_create_time"],
            "--yes",
            "--json",
        ]
        if payload.get("guild_id") and payload.get("channel_id"):
            args[2:2] = ["--guild-id", payload["guild_id"], "--channel-id", payload["channel_id"]]
        await _reply_cli(event, args, title="删除回复", guild_id=payload.get("guild_id"))
        return
    if len(parts) < 9:
        await event.reply("格式：删除回复 <帖子ID> <评论ID> <回复ID> <回复作者ID> <帖子作者ID> <帖子创建时间> <评论作者ID> <评论创建时间> [频道ID] [版块ID]")
        return
    feed_id, comment_id, reply_id, replier_id, feed_author_id, feed_create_time = parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
    comment_author_id, comment_create_time = parts[7], parts[8]
    args = [
        "feed", "do-reply",
        "--reply-type", "0",
        "--feed-id", feed_id,
        "--comment-id", comment_id,
        "--reply-id", reply_id,
        "--replier-id", replier_id,
        "--feed-author-id", feed_author_id,
        "--feed-create-time", feed_create_time,
        "--comment-author-id", comment_author_id,
        "--comment-create-time", comment_create_time,
        "--yes",
        "--json",
    ]
    guild_id = parts[9] if len(parts) >= 11 else None
    channel_id = parts[10] if len(parts) >= 11 else None
    if guild_id and channel_id:
        args[2:2] = ["--guild-id", guild_id, "--channel-id", channel_id]
    await _reply_cli(event, args, title="删除回复", guild_id=guild_id)

@admin_handler(r"^回复点赞\s+.+$", ignore_at_check=True)
async def handle_like_reply(event, match):
    await _handle_reply_like(event, like_type="5", title="回复点赞")

@admin_handler(r"^回复取消点赞\s+.+$", ignore_at_check=True)
async def handle_unlike_reply(event, match):
    await _handle_reply_like(event, like_type="6", title="回复取消点赞")

@admin_handler(r"^评论点赞\s+.+$", ignore_at_check=True)
async def handle_like_comment(event, match):
    await _handle_comment_like(event, like_type="3", title="评论点赞")

@admin_handler(r"^评论取消点赞\s+.+$", ignore_at_check=True)
async def handle_unlike_comment(event, match):
    await _handle_comment_like(event, like_type="4", title="评论取消点赞")

@admin_handler(r"^帖子设精华\s+\S+$", ignore_at_check=True)
async def handle_feed_essence_on(event, match):
    feed_id = _parts(event)[1]
    await _reply_cli(event, ["feed", "set-feed-essence", "--feed-id", feed_id, "--action", "1", "--json"], title="帖子设精华")

@admin_handler(r"^帖子取消精华\s+\S+$", ignore_at_check=True)
async def handle_feed_essence_off(event, match):
    feed_id = _parts(event)[1]
    await _reply_cli(event, ["feed", "set-feed-essence", "--feed-id", feed_id, "--action", "2", "--json"], title="帖子取消精华")

@admin_handler(r"^帖子推送精华\s+\S+$", ignore_at_check=True)
async def handle_feed_push_essence(event, match):
    feed_id = _parts(event)[1]
    await _reply_cli(event, ["feed", "push-essence-feed", "--feed-id", feed_id, "--json"], title="帖子推送精华")

@admin_handler(r"^帖子删除\s+\S+\s+\S+\s+\S+\s+\S+$", ignore_at_check=True)
async def handle_delete_feed(event, match):
    parts = _parts(event)
    feed_id, guild_id, channel_id, create_time = parts[1], parts[2], parts[3], parts[4]
    await _reply_cli(event, ["feed", "del-feed", "--feed-id", feed_id, "--guild-id", guild_id, "--channel-id", channel_id, "--create-time", create_time, "--yes", "--json"], title="删除帖子", guild_id=guild_id)

@admin_handler(r"^帖子置顶\s+\S+\s+\S+\s+\S+\s+\S+$", ignore_at_check=True)
async def handle_top_feed(event, match):
    parts = _parts(event)
    feed_id, user_id, create_time, guild_id = parts[1], parts[2], parts[3], parts[4]
    await _reply_cli(event, ["feed", "top-feed", "--feed-id", feed_id, "--user-id", user_id, "--create-time", create_time, "--guild-id", guild_id, "--action", "1", "--top-type", "1", "--json"], title="帖子置顶", guild_id=guild_id)

@admin_handler(r"^帖子取消置顶\s+\S+\s+\S+\s+\S+\s+\S+$", ignore_at_check=True)
async def handle_untop_feed(event, match):
    parts = _parts(event)
    feed_id, user_id, create_time, guild_id = parts[1], parts[2], parts[3], parts[4]
    await _reply_cli(event, ["feed", "top-feed", "--feed-id", feed_id, "--user-id", user_id, "--create-time", create_time, "--guild-id", guild_id, "--action", "2", "--top-type", "1", "--json"], title="帖子取消置顶", guild_id=guild_id)

@admin_handler(r"^帖子修改\s+.+$", ignore_at_check=True)
async def handle_alter_feed(event, match):
    m = re.match(r"^帖子修改\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(短帖|长帖|1|2)\s+(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：帖子修改 <帖子ID> <频道ID> <版块ID> <创建时间> <短帖|长帖> <内容>\n长帖可用：帖子修改 帖子ID 频道ID 版块ID 创建时间 长帖 标题 | 正文")
        return
    feed_id, guild_id, channel_id, create_time, feed_type_text, body = m.groups()
    feed_type = "2" if feed_type_text in {"长帖", "2"} else "1"
    args = [
        "feed", "alter-feed",
        "--feed-id", feed_id,
        "--guild-id", guild_id,
        "--channel-id", channel_id,
        "--create-time", create_time,
        "--feed-type", feed_type,
        "--json",
    ]
    if feed_type == "2":
        if "|" not in body:
            await event.reply("长帖修改格式：帖子修改 <帖子ID> <频道ID> <版块ID> <创建时间> 长帖 <标题> | <正文>")
            return
        title, content = [x.strip() for x in body.split("|", 1)]
        if not title or not content:
            await event.reply("长帖标题和正文都不能为空")
            return
        args.extend(["--title", title, "--content", content])
    else:
        content = body.strip()
        if not content:
            await event.reply("短帖内容不能为空")
            return
        args.extend(["--content", content])
    await _reply_cli(event, args, title="修改帖子", guild_id=guild_id)

@admin_handler(r"^帖子移动\s+\S+\s+\S+\s+\S+\s+\S+$", ignore_at_check=True)
async def handle_move_feed(event, match):
    parts = _parts(event)
    feed_id, guild_id, original_channel_id, target_channel_id = parts[1], parts[2], parts[3], parts[4]
    await _reply_cli(event, [
        "feed", "move-feed",
        "--guild-id", guild_id,
        "--channel-id", target_channel_id,
        "--original-channel-id", original_channel_id,
        "--feed-id", feed_id,
        "--json",
    ], title="移动帖子", guild_id=guild_id)

# ===================== 腾讯频道 Skill 1.1.5 同步：扫码登录 / Markdown 帖 / 通知 =====================

@admin_handler(r"^频道账号列表$", ignore_at_check=True)
async def handle_user_list(event, match):
    data = _load_users()
    if not data["users"]:
        await event.reply("还没有任何账号槽位，发送「频道添加账号 名称」创建（如：频道添加账号 小号1）")
        return
    lines = ["👥 账号槽位列表（★ 为当前槽位）"]
    for user in data["users"]:
        mark = "★ " if user == data["current"] else "  "
        nick = data["nicknames"].get(user, "")
        lines.append(f"{mark}{user}" + (f"（{nick}）" if nick else ""))
    lines.append("切换：频道切换账号 名称｜查看状态：频道账号状态 名称")
    await event.reply("\n".join(lines))


@admin_handler(r"^频道添加账号\s+\S+$", ignore_at_check=True)
async def handle_user_add(event, match):
    name = _text(event).split(None, 1)[1].strip()
    ok, msg = add_user(name)
    if ok:
        msg += "\n登录：频道切换账号 名称 → 频道登录 → 扫码 → 频道登录确认"
    await event.reply(msg)


@admin_handler(r"^频道删除账号\s+\S+$", ignore_at_check=True)
async def handle_user_remove(event, match):
    name = _text(event).split(None, 1)[1].strip()
    ok, msg = remove_user(name)
    await event.reply(msg)


@admin_handler(r"^频道切换账号\s+\S+$", ignore_at_check=True)
async def handle_user_switch(event, match):
    name = _text(event).split(None, 1)[1].strip()
    ok, msg = switch_user(name)
    await event.reply(msg)


@admin_handler(r"^频道账号状态(?:\s+\S+)?$", ignore_at_check=True)
async def handle_user_status(event, match):
    parts = _text(event).split(None, 1)
    name = parts[1].strip() if len(parts) > 1 else get_current_user()
    if not name:
        await event.reply("还没有任何账号槽位，发送「频道添加账号 名称」创建")
        return
    if name not in list_users():
        await event.reply(f"槽位「{name}」不存在，发送「频道账号列表」查看")
        return
    ok, output = await asyncio.to_thread(_run_cli, ["login", "status", "--json"], None, name)
    await event.reply(_render_result(f"账号状态｜{name}", ok, _normalize_rate_limit(output), ["login", "status", "--json"]))


@admin_handler(r"^频道登录$", ignore_at_check=True)
async def handle_login(event, match):
    """扫码授权登录：返回授权链接和二维码路径，扫码后发「频道登录确认」领取 token。"""
    current = get_current_user()
    if not current:
        await event.reply(
            "⚠️ 还没有创建账号槽位。\n"
            "请先发送「频道添加账号 名称」创建一个槽位（如：频道添加账号 小号1），\n"
            "再发「频道登录」。每个槽位登录一个频道号，互不影响。"
        )
        return
    ok, output = await asyncio.to_thread(_run_cli, ["login", "--json"])
    data = _extract_json(_normalize_rate_limit(output))
    payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else (data if isinstance(data, dict) else {})
    uri = str(payload.get("verification_uri") or "").strip()
    qr = str(payload.get("qrcode_path") or "").strip()
    expires = payload.get("expires_in_s")
    if ok and uri:
        lines = [f"🔑 频道扫码登录（当前槽位：{current}）", "如需登录其他号，先发「频道添加账号 名称」和「频道切换账号 名称」", f"授权链接：<{uri}>"]
        if qr:
            lines.append(f"二维码图片：{qr}")
        if expires:
            lines.append(f"有效期：{expires} 秒")
        lines.append("扫码或打开链接授权后，发送「频道登录确认」完成登录")
        await event.reply("\n".join(lines))
        return
    await event.reply(_render_result("频道登录", ok, _normalize_rate_limit(output), ["login", "--json"]))

@admin_handler(r"^频道登录确认$", ignore_at_check=True)
async def handle_login_poll(event, match):
    _invalidate_self_user_cache()
    await _reply_cli(event, ["login", "poll-token", "--json"], title="频道登录确认")

@admin_handler(r"^频道登录状态$", ignore_at_check=True)
async def handle_login_status(event, match):
    await _reply_cli(event, ["login", "status", "--json"], title="频道登录状态")

@admin_handler(r"^频道退出登录$", ignore_at_check=True)
async def handle_login_logout(event, match):
    _invalidate_self_user_cache()
    await _reply_cli(event, ["login", "logout", "--json"], title="频道退出登录")

@admin_handler(r"^频道版本$", ignore_at_check=True)
async def handle_cli_version(event, match):
    await _reply_cli(event, ["version"], title="CLI 版本")

@admin_handler(r"^频道MD帖\s+\S+\s+\S+\s+.+$", ignore_at_check=True)
async def handle_publish_md_feed(event, match):
    """Markdown 短帖：正文按 Markdown 渲染（--markdown-content）。"""
    parts = _text(event).split(None, 3)
    guild_id, channel_id, content = parts[1], parts[2], parts[3].strip()
    await _reply_cli(event, ["feed", "publish-feed", "--guild-id", guild_id, "--channel-id", channel_id, "--markdown-content", content, "--json"], title="发布Markdown帖", guild_id=guild_id)

@admin_handler(r"^频道MD长帖\s+.+$", ignore_at_check=True)
async def handle_publish_md_long_feed(event, match):
    m = re.match(r"^频道MD长帖\s+(\S+)\s+(\S+)\s+(.+?)\s*\|\s*(.+)$", _text(event), re.S)
    if not m:
        await event.reply("格式：频道MD长帖 <频道ID> <版块ID> <标题> | <Markdown正文>")
        return
    guild_id, channel_id, title, content = m.groups()
    await _reply_cli(event, ["feed", "publish-feed", "--guild-id", guild_id, "--channel-id", channel_id, "--title", title.strip(), "--markdown-content", content.strip(), "--json"], title="发布Markdown长帖", guild_id=guild_id)

@admin_handler(r"^频道通知状态$", ignore_at_check=True)
async def handle_notices_status(event, match):
    await _reply_cli(event, ["manage", "notices-status", "--json"], title="通知状态")

@admin_handler(r"^频道检查通知$", ignore_at_check=True)
async def handle_check_notices(event, match):
    await _reply_cli(event, ["manage", "check-notices", "--json"], title="检查通知")

@admin_handler(r"^频道最近通知$", ignore_at_check=True)
async def handle_recent_notices(event, match):
    await _reply_cli(event, ["manage", "get-recent-notices", "--json"], title="最近通知")

@admin_handler(r"^评论通知\s+\d+\s+.+$", ignore_at_check=True)
async def handle_comment_by_ref(event, match):
    """按通知编号评论帖子本身（do-comment --ref）。"""
    parts = _text(event).split(None, 2)
    ref, content = parts[1], parts[2].strip()
    await _reply_cli(event, ["feed", "do-comment", "--ref", ref, "--content", content, "--json"], title="评论通知帖子")

@admin_handler(r"^回复通知\s+\d+\s+.+$", ignore_at_check=True)
async def handle_reply_by_ref(event, match):
    """按通知编号回复对方的评论（do-reply --ref）。"""
    parts = _text(event).split(None, 2)
    ref, content = parts[1], parts[2].strip()
    await _reply_cli(event, ["feed", "do-reply", "--ref", ref, "--content", content, "--json"], title="回复通知评论")

@admin_handler(r"^处理通知\s+\d+\s+(同意|拒绝)$", ignore_at_check=True)
async def handle_deal_notice(event, match):
    parts = _parts(event)
    ref, action = parts[1], parts[2]
    action_id = "agree" if action == "同意" else "refuse"
    await _reply_cli(event, ["manage", "deal-notice", "--ref", ref, "--action-id", action_id, "--json"], title=f"处理通知（{action}）")

@admin_handler(r"^私信通知回复\s+\d+\s+.+$", ignore_at_check=True)
async def handle_dm_reply_by_ref(event, match):
    parts = _text(event).split(None, 2)
    ref, content = parts[1], parts[2].strip()
    await _reply_cli(event, ["manage", "push-group-dm-msg", "--ref", ref, "--text", content, "--json"], title="回复私信通知")


def _text(event) -> str:
    return str(getattr(event, "content", "") or "").strip()


def _parts(event) -> List[str]:
    return _text(event).split()


def _read_json_file(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else default
    except Exception:
        pass
    return dict(default)


def _read_plugin_settings() -> Dict[str, Any]:
    data = _read_json_file(PLUGIN_SETTINGS, {})
    if data:
        return data
    legacy_preview = _read_json_file(BASE_DIR / "preview_settings.json", {})
    legacy_debug = _read_json_file(BASE_DIR / "debug_settings.json", {})
    merged = {
        "preview_enabled": bool(legacy_preview.get("__global__", True)),
        "debug_enabled": bool(legacy_debug.get("__global__", False)),
    }
    _write_json_file(PLUGIN_SETTINGS, merged)
    return merged


def _write_json_file(path: Path, data: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _token_store_path(user: Optional[str] = None) -> Path:
    name = _safe_user_name(user) or get_current_user()
    if name:
        home = _user_home(name)
        try:
            home.mkdir(parents=True, exist_ok=True)
        except Exception:
            return TOKEN_STORE
        return home / "token_store.json"
    return TOKEN_STORE


def _load_token_store(user: Optional[str] = None) -> Dict[str, Any]:
    return _read_json_file(_token_store_path(user), {})


def _save_token_store(data: Dict[str, Any], user: Optional[str] = None) -> None:
    _write_json_file(_token_store_path(user), data)


def _fingerprint_token(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    return f"{len(raw)}:{raw[:8]}:{raw[-8:]}"


def _invalidate_self_user_cache(token_fingerprint: Optional[str] = None, user: Optional[str] = None) -> None:
    store = _load_token_store(user)
    store.pop("__self_user__", None)
    store.pop("__guild_roles__", None)
    if token_fingerprint is not None:
        store["__token_fp__"] = str(token_fingerprint or "")
    _save_token_store(store, user)


def _refresh_guild_roles(payload: Optional[Dict[str, Any]]) -> None:
    if not isinstance(payload, dict):
        return
    role_cache: Dict[str, Any] = {}
    now = int(time.time())
    for key in ("created_guilds", "managed_guilds", "joined_guilds"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            gid = str(item.get("guild_id") or item.get("guildId") or "").strip()
            if not gid:
                continue
            role = str(item.get("role") or item.get("my_role") or item.get("myRole") or "").strip()
            role_cache[gid] = {"role": role, "expires_at": now + 7200}
    if role_cache:
        store = _load_token_store()
        store["__guild_roles__"] = role_cache
        _save_token_store(store)


def _sync_self_user_cache_with_token(user: Optional[str] = None) -> None:
    if "manage token show" in _UNSUPPORTED_CLI_CMDS:
        return
    ok, output = _run_cli(["manage", "token", "show", "--json"], user=user)
    if not ok:
        if _is_unknown_command(output):
            _UNSUPPORTED_CLI_CMDS.add("manage token show")
        return
    data = _extract_json(output)
    payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
    token_value = str(payload.get("token") or payload.get("access_token") or payload.get("raw") or "").strip()
    if not token_value:
        return
    current_fp = _fingerprint_token(token_value)
    store = _load_token_store(user)
    cached_fp = str(store.get("__token_fp__") or "").strip()
    if cached_fp != current_fp:
        _invalidate_self_user_cache(current_fp, user)


def _get_self_user_id(guild_id: Optional[str] = None, user: Optional[str] = None) -> Optional[str]:
    _sync_self_user_cache_with_token(user)
    store = _load_token_store(user)
    cache = store.get("__self_user__") if isinstance(store.get("__self_user__"), dict) else {}
    cache_key = str(guild_id or "__global__")
    cached_id = str(cache.get(cache_key) or cache.get("__global__") or "").strip()
    if cached_id:
        return cached_id

    args = ["manage", "get-user-info", "--json"]
    if guild_id:
        args[2:2] = ["--guild-id", guild_id]
    ok, output = _run_cli(args, user=user)
    nickname = ""
    if ok:
        data = _extract_json(output)
        payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
        user_id = str(payload.get("tinyid") or payload.get("tiny_id") or payload.get("tinyId") or payload.get("user_id") or payload.get("userId") or "").strip()
        nickname = str(payload.get("nickname") or payload.get("nick") or "").strip()
        if user_id:
            cache[cache_key] = user_id
            cache["__global__"] = user_id
            store["__self_user__"] = cache
            _save_token_store(store, user)
            return user_id
    if not nickname:
        return None

    ok_list, output_list = _run_cli(["manage", "get-my-join-guild-info", "--json"], user=user)
    if not ok_list:
        return None
    list_data = _extract_json(output_list)
    list_payload = list_data.get("data") if isinstance(list_data, dict) and isinstance(list_data.get("data"), dict) else {}

    def _collect(key: str) -> List[Dict[str, Any]]:
        items = list_payload.get(key)
        return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []

    created = _collect("created_guilds")
    joined = _collect("joined_guilds")
    managed = _collect("managed_guilds")

    ordered: List[Dict[str, Any]] = []
    seen = set()

    def _push(items: List[Dict[str, Any]]):
        for item in items:
            gid = str(item.get("guild_id") or item.get("guildId") or "").strip()
            if not gid or gid in seen:
                continue
            seen.add(gid)
            ordered.append(item)

    _push(created)
    _push(sorted([x for x in joined if int(x.get("member_count") or 10**9) < 30], key=lambda x: int(x.get("member_count") or 10**9)))
    _push(managed)
    remaining = created + joined + managed
    _push(sorted(remaining, key=lambda x: int(x.get("member_count") or 10**9)))

    matches: Dict[str, int] = {}
    for item in ordered:
        gid = str(item.get("guild_id") or item.get("guildId") or "").strip()
        if not gid:
            continue
        ok_search, output_search = _run_cli(["manage", "guild-member-search", "--guild-id", gid, "--keyword", nickname, "--json"], user=user)
        if not ok_search:
            continue
        search_data = _extract_json(output_search)
        search_payload = search_data.get("data") if isinstance(search_data, dict) and isinstance(search_data.get("data"), dict) else {}
        members = search_payload.get("members")
        if not isinstance(members, list):
            continue
        exact = []
        for member in members:
            if not isinstance(member, dict):
                continue
            member_nick = str(member.get("nickname") or member.get("nick") or "").strip()
            tiny_id = str(member.get("tinyid") or member.get("tiny_id") or member.get("tinyId") or "").strip()
            if member_nick == nickname and tiny_id:
                exact.append(tiny_id)
        if len(exact) == 1:
            tiny_id = exact[0]
            cache[cache_key] = tiny_id
            cache[gid] = tiny_id
            cache["__global__"] = tiny_id
            store["__self_user__"] = cache
            _save_token_store(store, user)
            return tiny_id
        for tiny_id in set(exact):
            matches[tiny_id] = matches.get(tiny_id, 0) + 1
            if matches[tiny_id] >= 2:
                cache[cache_key] = tiny_id
                cache[gid] = tiny_id
                cache["__global__"] = tiny_id
                store["__self_user__"] = cache
                _save_token_store(store, user)
                return tiny_id
    return None


def _get_guild_role(guild_id: Optional[str]) -> str:
    gid = str(guild_id or "").strip()
    if not gid:
        return ""
    store = _load_token_store()
    role_cache = store.get("__guild_roles__") if isinstance(store.get("__guild_roles__"), dict) else {}
    cached = role_cache.get(gid)
    if isinstance(cached, dict):
        expires_at = int(cached.get("expires_at") or 0)
        role = str(cached.get("role") or "").strip()
        if role and expires_at > int(time.time()):
            return role
    elif isinstance(cached, str) and cached.strip():
        return cached.strip()
    ok, output = _run_cli(["manage", "get-my-join-guild-info", "--json"])
    if not ok:
        return ""
    data = _extract_json(output)
    payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
    _refresh_guild_roles(payload)
    store = _load_token_store()
    role_cache = store.get("__guild_roles__") if isinstance(store.get("__guild_roles__"), dict) else {}
    cached = role_cache.get(gid)
    if isinstance(cached, dict):
        return str(cached.get("role") or "").strip()
    return ""


def _can_manage_members(guild_id: Optional[str]) -> Tuple[bool, bool]:
    role = _get_guild_role(guild_id)
    is_owner = any(x in role for x in ("频道主", "owner", "OWN"))
    is_admin = is_owner or any(x in role for x in ("管理员", "admin", "ADMIN"))
    return is_owner, is_admin


def _save_token_payload(kind: str, payload: Dict[str, Any]) -> str:
    data = _load_token_store()
    token = f"{kind[0]}{int(time.time() * 1000)}{uuid.uuid4().hex[:4]}"
    data[token] = {"kind": kind, "payload": payload}
    # 分别保留系统 key（__前缀）和用户 token，避免裁剪时误删系统缓存
    system_items = {k: v for k, v in data.items() if str(k).startswith("__")}
    user_items = [(k, v) for k, v in data.items() if not str(k).startswith("__")]
    kept = dict(user_items[-200:])
    kept.update(system_items)
    _save_token_store(kept)
    return token


def _load_token_payload(token: str, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
    data = _load_token_store()
    item = data.get(str(token or "").strip())
    if not isinstance(item, dict):
        return None
    if kind and item.get("kind") != kind:
        return None
    payload = item.get("payload")
    return payload if isinstance(payload, dict) else None


def _set_switch(key: str, enabled: bool) -> None:
    data = _read_plugin_settings()
    data[key] = bool(enabled)
    _write_json_file(PLUGIN_SETTINGS, data)


def _get_switch(key: str, default: bool) -> bool:
    data = _read_plugin_settings()
    return bool(data.get(key, default))


def _preview_enabled() -> bool:
    return _get_switch("preview_enabled", True)


def _debug_enabled() -> bool:
    return _get_switch("debug_enabled", False)


def _xml_escape(text: str) -> str:
    return str(text or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _quote_cmd(text: str) -> str:
    raw = str(text or "")
    if not ENCODE_CMD_INPUT:
        return raw
    return urllib.parse.quote(raw, safe="")


def _shrink_token(value: Optional[str], keep: int = 6) -> str:
    raw = str(value or "").strip()
    if len(raw) <= keep * 2 + 1:
        return raw
    return f"{raw[:keep]}~{raw[-keep:]}"


def _md_cell(text: Any) -> str:
    return str(text or "").replace("|", "¦").replace("\n", " ").strip()


def _table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    if not rows:
        return []
    head = "| " + " | ".join(_md_cell(x) for x in headers) + " |"
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = ["| " + " | ".join(_md_cell(x) for x in row) + " |" for row in rows]
    # 前后各两个空行，防止平台渲染粘连
    return ["", "", head, sep, *body, "", ""]


def _fit_cmd_text(text: str, max_len: int = 100) -> str:
    raw = str(text or "").strip()
    if len(raw) <= max_len:
        return raw
    return raw[: max_len - 3].rstrip() + "..."


def _truncate_display_text(text: str, max_width: int = 12) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    total = 0
    out = []
    for ch in raw:
        w = 1 if ord(ch) < 128 else 2
        if total + w > max_width:
            return "".join(out).rstrip() + "..."
        out.append(ch)
        total += w
    return "".join(out)


def _member_name(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return "未知成员"
    for key in ("昵称", "nick", "nickname", "name", "member_name", "memberName", "display_name", "displayName", "user_name", "userName", "uin_name", "uinName"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    for key in ("user", "member", "profile"):
        nested = item.get(key)
        if isinstance(nested, dict):
            for sub_key in ("昵称", "nick", "nickname", "name", "display_name", "displayName", "user_name", "userName"):
                value = nested.get(sub_key)
                if value not in (None, ""):
                    return str(value)
    return "未知成员"


def _member_chip(guild_id: Optional[str], tiny_id: Optional[str], nickname: Optional[str]) -> str:
    gid = str(guild_id or "").strip()
    tid = str(tiny_id or "").strip()
    name = _truncate_display_text(nickname or "未知成员", 12) or "未知成员"
    if not gid or not tid:
        return name
    return _quick_cmd(f"频道用户资料 {gid} {tid}", name)


def _pair_lines(parts: List[str], sep: str = "  |  ") -> List[str]:
    rows: List[str] = []
    current: List[str] = []
    for part in parts:
        if not part:
            continue
        current.append(part)
        if len(current) == 2:
            rows.append(sep.join(current))
            current = []
    if current:
        rows.append(sep.join(current))
    return rows


def _quick_cmd(text: str, show: Optional[str] = None, reference: Optional[bool] = None) -> str:
    raw = _fit_cmd_text(_quote_cmd(text), 100)
    if not raw:
        return ""
    attrs = [f'text="{_xml_escape(raw)}"']
    show_text = str(show or "").strip()
    if show_text:
        attrs.append(f'show="{_xml_escape(show_text[:100])}"')
    if isinstance(reference, bool):
        attrs.append(f'reference="{"true" if reference else "false"}"')
    return f"<qqbot-cmd-input {' '.join(attrs)} />"


_UNSUPPORTED_CLI_CMDS: set = set()


def _is_unknown_command(output: str) -> bool:
    return "unknown command" in str(output or "").lower()


def _run_cli_compat(primary: List[str], fallback: List[str], user: Optional[str] = None) -> Tuple[bool, str]:
    """兼容不同版本 CLI：primary 报 unknown command 时改用旧命令 fallback。"""
    ok, output = _run_cli(primary, user=user)
    if not ok and _is_unknown_command(output):
        return _run_cli(fallback, user=user)
    return ok, output


def _run_cli(args: List[str], stdin_text: Optional[str] = None, user: Optional[str] = None) -> Tuple[bool, str]:
    cli = _resolve_cli()
    if not cli:
        return False, (
            "未找到 tencent-channel-cli，请将 CLI 放入插件目录"
            + ("" if IS_WINDOWS else "（如 tencent-channel-cli-linux-x64 二进制）")
            + "或安装 Node.js/npm 后执行 npm install -g tencent-channel-cli"
        )
    try:
        proc = subprocess.run(
            [cli, *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(BASE_DIR),
            env=_cli_env(user),
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return False, "命令执行超时"
    except Exception as e:
        return False, f"命令执行失败：{e}"
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, stderr or stdout or f"命令执行失败，退出码 {proc.returncode}"
    return True, stdout or stderr or ""


def _with_preview(args: List[str]) -> List[str]:
    if len(args) < 2:
        return args
    domain, action = args[0], args[1]
    if domain in WRITE_ACTIONS and action in WRITE_ACTIONS[domain] and _preview_enabled():
        if "--dry-run" not in args and "-d" not in args:
            return [*args, "--dry-run"]
    return args


def _extract_json(text: str) -> Optional[Any]:
    body = str(text or "").strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception:
        pass
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", body)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _json_block(output: str) -> str:
    body = str(output or "").strip()
    if not body:
        return ""
    return f"\n```json 返回JSON\n{body}\n```\n"


def _ret_code(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        for key in ("retCode", "ret_code", "code"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return None


def _normalize_rate_limit(output: str) -> str:
    data = _extract_json(output)
    text = str(output or "")
    code = _ret_code(data)
    if code == "153" or "接口调用已超过申请的频率上限" in text:
        return "接口触发频率限制，请稍后再试"
    return output


async def _reply_cli(event, args: List[str], title: str, guild_id: Optional[str] = None):
    final_args = _with_preview(args)
    ok, output = await asyncio.to_thread(_run_cli, final_args)
    await event.reply(_render_result(title, ok, _normalize_rate_limit(output), final_args, guild_id=guild_id))


async def _reply_cli_json_stdin(event, args: List[str], payload: Dict[str, Any], title: str, guild_id: Optional[str] = None):
    final_args = _with_preview(args)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ok, output = await asyncio.to_thread(_run_cli, final_args, body)
    await event.reply(_render_result(title, ok, _normalize_rate_limit(output), final_args, guild_id=guild_id))


async def _handle_comment_like(event, like_type: str, title: str):
    parts = _parts(event)
    if len(parts) >= 2 and re.fullmatch(r"l[0-9a-f]+", parts[1]):
        payload = _load_token_payload(parts[1], kind="comment_like")
        if not payload:
            await event.reply("评论点赞令牌无效或已过期，请重新打开评论列表后再试")
            return
        args = ["feed", "do-like", "--like-type", like_type, "--feed-id", payload["feed_id"], "--comment-id", payload["comment_id"], "--feed-author-id", payload["feed_author_id"], "--feed-create-time", payload["feed_create_time"], "--comment-author-id", payload["comment_author_id"]]
        if payload.get("guild_id") and payload.get("channel_id"):
            args += ["--guild-id", payload["guild_id"], "--channel-id", payload["channel_id"]]
        args += ["--json"]
        await _reply_cli(event, args, title=title, guild_id=payload.get("guild_id"))
        return
    m = re.match(r"^(?:评论点赞|评论取消点赞)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(\S+)\s+(\S+))?$", _text(event), re.S)
    if not m:
        await event.reply("格式：评论点赞 <评论令牌> 或 评论点赞 <帖子ID> <评论ID> <帖子作者ID> <帖子创建时间> <评论作者ID> [频道ID] [版块ID]")
        return
    feed_id, comment_id, feed_author_id, feed_create_time, comment_author_id, guild_id, channel_id = m.groups()
    args = ["feed", "do-like", "--like-type", like_type, "--feed-id", feed_id, "--comment-id", comment_id, "--feed-author-id", feed_author_id, "--feed-create-time", feed_create_time, "--comment-author-id", comment_author_id]
    if guild_id and channel_id:
        args += ["--guild-id", guild_id, "--channel-id", channel_id]
    args += ["--json"]
    await _reply_cli(event, args, title=title, guild_id=guild_id)


async def _handle_reply_like(event, like_type: str, title: str):
    parts = _parts(event)
    if len(parts) >= 2 and re.fullmatch(r"l[0-9a-f]+", parts[1]):
        payload = _load_token_payload(parts[1], kind="reply_like")
        if not payload:
            await event.reply("回复点赞令牌无效或已过期，请重新打开回复列表后再试")
            return
        args = [
            "feed", "do-like",
            "--like-type", like_type,
            "--feed-id", payload["feed_id"],
            "--comment-id", payload["comment_id"],
            "--reply-id", payload["reply_id"],
            "--feed-author-id", payload["feed_author_id"],
            "--feed-create-time", payload["feed_create_time"],
            "--comment-author-id", payload["comment_author_id"],
            "--reply-author-id", payload["reply_author_id"],
        ]
        if payload.get("guild_id") and payload.get("channel_id"):
            args += ["--guild-id", payload["guild_id"], "--channel-id", payload["channel_id"]]
        args += ["--json"]
        await _reply_cli(event, args, title=title, guild_id=payload.get("guild_id"))
        return
    m = re.match(r"^(?:回复点赞|回复取消点赞)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(\S+)\s+(\S+))?$", _text(event), re.S)
    if not m:
        await event.reply("格式：回复点赞 <回复令牌> 或 回复点赞 <帖子ID> <评论ID> <回复ID> <帖子作者ID> <帖子创建时间> <评论作者ID> <回复作者ID> [频道ID] [版块ID]")
        return
    feed_id, comment_id, reply_id, feed_author_id, feed_create_time, comment_author_id, reply_author_id, guild_id, channel_id = m.groups()
    args = [
        "feed", "do-like",
        "--like-type", like_type,
        "--feed-id", feed_id,
        "--comment-id", comment_id,
        "--reply-id", reply_id,
        "--feed-author-id", feed_author_id,
        "--feed-create-time", feed_create_time,
        "--comment-author-id", comment_author_id,
        "--reply-author-id", reply_author_id,
    ]
    if guild_id and channel_id:
        args += ["--guild-id", guild_id, "--channel-id", channel_id]
    args += ["--json"]
    await _reply_cli(event, args, title=title, guild_id=guild_id)


def _parse_duration_to_timestamp(text: str) -> Optional[int]:
    raw = str(text or "").strip()
    if not raw:
        return None
    total = 0
    matched = False
    for value, unit in re.findall(r"(\d+)\s*(天|日|小时|时|分钟|分|秒)", raw):
        matched = True
        num = int(value)
        if unit in {"天", "日"}:
            total += num * 86400
        elif unit in {"小时", "时"}:
            total += num * 3600
        elif unit in {"分钟", "分"}:
            total += num * 60
        elif unit == "秒":
            total += num
    if not matched or total <= 0:
        return None
    return int(time.time()) + total


def _render_result(title: str, ok: bool, output: str, args: List[str], guild_id: Optional[str] = None) -> str:
    data = _extract_json(output)
    lines: List[str] = [f"{'✅' if ok else '❌'} {title}"]

    if "--dry-run" in args or "-d" in args:
        lines.append("模式：预演模式（仅验证参数，不实际执行）")

    success = data.get("success") if isinstance(data, dict) else None
    if isinstance(success, bool):
        for key in ("message", "msg", "error", "description"):
            value = data.get(key)
            if value:
                lines.append(f"说明：{value}")
                break
        nested = data.get("data")
        if isinstance(nested, dict):
            status = nested.get("status")
            if status:
                lines.append(f"状态：{status}")
            if nested.get("need_verification"):
                lines.append("需要验证：该频道加入需要附言或答题，请根据返回内容继续操作")
                if guild_id:
                    lines.append(_quick_cmd(f"频道加入附言 {guild_id} 我想加入这个频道", "加入附言"))
                    lines.append(_quick_cmd(f"频道加入答题 {guild_id} 答案1|答案2", "加入答题"))
            pending = nested.get("pending")
            if isinstance(pending, dict):
                hint = pending.get("hint")
                if hint:
                    lines.append(f"下一步：{hint}")
                resume = nested.get("resume_command") or pending.get("resume_command")
                if resume:
                    lines.append(_quick_cmd(resume, "继续执行"))
        summary_lines = _render_summary(title, data, guild_id=guild_id) or []
        # 如果 _render_summary 没有针对该 title 的专门处理（只返回了空或极少内容），
        # 用通用兜底渲染展示关键字段，避免用户看到原始 JSON
        has_real_content = any(l for l in summary_lines if l and not l.startswith("|") and not l.startswith("---"))
        if not has_real_content:
            summary_lines = _render_fallback_summary(data) or []
        lines.extend(summary_lines)
    else:
        brief = str(output or "").strip()
        if brief:
            lines.append(f"结果：{brief}")

    if _debug_enabled() and str(output or "").strip():
        lines.append("")
        lines.append(_json_block(output))

    return "\n".join([x for x in lines if x is not None])


def _render_summary(title: str, data: Dict[str, Any], guild_id: Optional[str] = None) -> List[str]:
    lines: List[str] = []
    payload = data.get("data") if isinstance(data.get("data"), dict) else data

    if title == "频道列表":
        local_groups = payload.get("_guild_list_groups")
        local_page_index = int(payload.get("_guild_list_page_index", 0) or 0)
        local_expires_at = int(payload.get("_guild_list_expires_at", 0) or 0)

        groups_to_render = []
        if isinstance(local_groups, list) and local_groups:
            groups_to_render = local_groups
        else:
            for key, label in (("created_guilds", "我创建的频道"), ("managed_guilds", "我管理的频道"), ("joined_guilds", "我加入的频道")):
                items = payload.get(key)
                if isinstance(items, list) and items:
                    pages = max(1, (len(items) + GUILD_LIST_PAGE_SIZE - 1) // GUILD_LIST_PAGE_SIZE)
                    groups_to_render.append({
                        "key": key,
                        "label": label,
                        "items": items,
                        "pages": pages,
                    })

        has_guild = False
        max_page = 1
        for group in groups_to_render:
            if not isinstance(group, dict):
                continue
            label = group.get("label") or "频道列表"
            items = group.get("items") if isinstance(group.get("items"), list) else []
            pages = int(group.get("pages", 1) or 1)
            max_page = max(max_page, pages)
            if not items:
                continue
            has_guild = True
            start = local_page_index * GUILD_LIST_PAGE_SIZE
            page_items = items[start:start + GUILD_LIST_PAGE_SIZE]
            if not page_items:
                continue
            end = min(start + len(page_items), len(items))
            total_pages = max(1, (len(items) + GUILD_LIST_PAGE_SIZE - 1) // GUILD_LIST_PAGE_SIZE)
            lines.append(f"{label}：{len(items)} 个（第 {start + 1}-{end} 条，第 {local_page_index + 1}/{total_pages} 页）")
            rows = []
            for item in page_items:
                name = item.get("guild_name") or item.get("name") or "未命名频道"
                item_gid = item.get("guild_id") or item.get("guildId")
                member_count = item.get("member_count") or item.get("memberCount") or "-"
                if item_gid:
                    name_cell = _quick_cmd(f"频道资料 {item_gid}", _truncate_display_text(name, 18))
                else:
                    name_cell = _truncate_display_text(name, 18)

                member_cmd = _quick_cmd(f"频道成员 {item_gid}", "成员") if item_gid else "-"
                search_member_cmd = _quick_cmd(f"频道搜成员 {item_gid} 关键词", "搜成员") if item_gid else "-"
                feed_cmd = _quick_cmd(f"频道帖子 {item_gid}", "帖子") if item_gid else "-"
                search_feed_cmd = _quick_cmd(f"频道搜帖 {item_gid} 关键词", "搜帖") if item_gid else "-"
                leave_cmd = _quick_cmd(f"频道退出 {item_gid}", "退出") if item_gid else "-"

                join_setting_cmd = "-"
                direct_join_cmd = "-"
                audit_join_cmd = "-"
                disable_join_cmd = "-"
                rename_cmd = "-"
                profile_cmd = "-"
                avatar_cmd = "-"
                if item_gid and group.get("key") in {"created_guilds", "managed_guilds"}:
                    join_setting_cmd = _quick_cmd(f"频道加入方式 {item_gid}", "加入方式")
                    direct_join_cmd = _quick_cmd(f"频道设直接加入 {item_gid}", "直加")
                    audit_join_cmd = _quick_cmd(f"频道设审核加入 {item_gid}", "审核")
                    disable_join_cmd = _quick_cmd(f"频道设禁止加入 {item_gid}", "禁止")
                    rename_cmd = _quick_cmd(f"频道改名 {item_gid} 新名称", "改名")
                    profile_cmd = _quick_cmd(f"频道改简介 {item_gid} 新简介", "改简介")
                    avatar_cmd = _quick_cmd(f"频道改头像 {item_gid} 图片路径", "改头像")

                rows.append([
                    name_cell,
                    member_count,
                    member_cmd,
                    search_member_cmd,
                    feed_cmd,
                    search_feed_cmd,
                    leave_cmd,
                    join_setting_cmd,
                    direct_join_cmd,
                    audit_join_cmd,
                    disable_join_cmd,
                    rename_cmd,
                    profile_cmd,
                    avatar_cmd,
                ])
            lines.extend(_table(["频道", "人数", "成员", "搜成员", "帖子", "搜帖", "退出", "加入方式", "直加", "审核", "禁止", "改名", "改简介", "改头像"], rows))

        if has_guild and not local_groups:
            local_expires_at = int(time.time()) + 7200

        if has_guild:
            if groups_to_render:
                prev_token = None
                next_token = None
                if local_page_index > 0:
                    prev_token = _save_token_payload("guild_list_page", {
                        "all_groups": groups_to_render,
                        "page_index": local_page_index - 1,
                        "expires_at": local_expires_at,
                    })
                if local_page_index + 1 < max_page:
                    next_token = _save_token_payload("guild_list_page", {
                        "all_groups": groups_to_render,
                        "page_index": local_page_index + 1,
                        "expires_at": local_expires_at,
                    })
                pager_ops = []
                if prev_token:
                    pager_ops.append(_quick_cmd(f"频道列表 {prev_token}", "上一页"))
                if next_token:
                    pager_ops.append(_quick_cmd(f"频道列表 {next_token}", "下一页"))
                if pager_ops:
                    lines.append("分页：" + " / ".join(pager_ops))
            lines.append("快捷操作：" + " / ".join([
                _quick_cmd("频道帮助"),
                _quick_cmd("频道自检"),
                _quick_cmd("频道创建 头像路径 公开 频道名 | 简介", "创建频道"),
                _quick_cmd("频道清理缓存", "清理缓存"),
            ]))
        return lines

    if title == "用户资料":
        uid = payload.get("tinyid") or payload.get("tiny_id") or payload.get("tinyId") or payload.get("user_id") or payload.get("userId") or payload.get("id") or payload.get("uid") or payload.get("open_id") or payload.get("openId")
        if uid:
            store = _load_token_store()
            cache = store.get("__self_user__") if isinstance(store.get("__self_user__"), dict) else {}
            cache_key = str(payload.get("guild_id") or payload.get("guildId") or guild_id or "__global__")
            cache[cache_key] = str(uid)
            store["__self_user__"] = cache
            _save_token_store(store)
        nick = str(payload.get("nickname") or payload.get("nick") or "").strip()
        current = get_current_user()
        if nick and current:
            set_user_nickname(current, nick)
        profile_items = [
            ("nickname", "昵称"),
            ("nick", "昵称"),
            ("tinyid", "用户ID"),
            ("tiny_id", "用户ID"),
            ("tinyId", "用户ID"),
            ("user_id", "用户ID"),
            ("userId", "用户ID"),
            ("id", "ID"),
            ("uid", "ID"),
            ("open_id", "OpenID"),
            ("openId", "OpenID"),
            ("gender", "性别"),
            ("country", "国家"),
            ("province", "省份/地区"),
            ("city", "城市"),
            ("joinTime_human", "加入时间"),
            ("join_time_human", "加入时间"),
            ("joinTime", "加入时间"),
            ("join_time", "加入时间"),
            ("role", "角色"),
            ("role_name", "角色"),
            ("isGuildAuthor", "是否创作者"),
            ("is_guild_author", "是否创作者"),
        ]
        rows: List[List[Any]] = []
        seen_labels = set()
        for key, label in profile_items:
            value = payload.get(key)
            if value not in (None, "", []):
                if label in seen_labels:
                    continue
                rows.append([label, str(value)])
                seen_labels.add(label)
        if rows:
            lines.extend(_table(["属性", "值"], rows))
        gid = payload.get("guild_id") or payload.get("guildId") or guild_id
        uid = payload.get("tinyid") or payload.get("tiny_id") or payload.get("tinyId") or payload.get("user_id") or payload.get("userId") or payload.get("id") or payload.get("uid")
        is_owner, is_admin = _can_manage_members(gid)
        if gid and uid:
            ops = [_quick_cmd(f"频道私信 {gid} {uid} 你好", "发送私信")]
            if is_owner:
                ops.append(_quick_cmd(f"频道设置管理员 {gid} {uid}", "设管理员"))
                ops.append(_quick_cmd(f"频道取消管理员 {gid} {uid}", "取消管理员"))
            if is_admin:
                ops.append(_quick_cmd(f"频道禁言 {gid} {uid} 1小时", "禁言"))
                ops.append(_quick_cmd(f"频道解除禁言 {gid} {uid}", "解禁"))
                ops.append(_quick_cmd(f"频道踢出 {gid} {uid}", "踢出"))
            lines.append("")
            lines.append(f"操作：{' / '.join(ops)}")
        return lines

    if title == "解析分享链接":
        gid = payload.get("guild_id") or payload.get("guildId")
        name = payload.get("guild_name") or payload.get("name") or "未知频道"
        rows = [["频道名", name]]
        if gid:
            rows.append(["频道ID", gid])
            lines.extend(_table(["属性", "值"], rows))
            ops = [
                _quick_cmd(f"频道资料 {gid}", "查看资料"),
                _quick_cmd(f"频道成员 {gid}", "成员"),
                _quick_cmd(f"频道帖子 {gid}", "帖子"),
            ]
            lines.append(f"操作：{' / '.join(ops)}")
        else:
            lines.extend(_table(["属性", "值"], rows))
        return lines

    if title in {"频道资料", "加入方式"}:
        # 加入方式的 joinType 在 setting 嵌套内，先提取
        setting = payload.get("setting")
        if isinstance(setting, dict):
            join_type = setting.get("joinType") or setting.get("join_type")
            if join_type and title == "加入方式":
                join_type_map = {
                    "JOIN_GUILD_TYPE_DIRECT": "直接加入",
                    "JOINGUILDTYPEDIRECT": "直接加入",
                    "JOIN_GUILD_TYPE_ADMIN_AUDIT": "管理员审核",
                    "JOINGUILDTYPEADMINAUDIT": "管理员审核",
                    "JOIN_GUILD_TYPE_DISABLE": "禁止加入",
                    "JOINGUILDTYPEDISABLE": "禁止加入",
                    "JOIN_GUILD_TYPE_QUESTION_WITH_ADMIN_AUDIT": "提问审核",
                    "JOINGUILDTYPEQUESTIONWITHADMINAUDIT": "提问审核",
                    "JOIN_GUILD_TYPE_MULTI_ANSWER_WITH_ADMIN_AUDIT": "多题验证",
                    "JOINGUILDTYPEMULTIANSWERWITHADMINAUDIT": "多题验证",
                    "JOIN_GUILD_TYPE_QUIZ": "测试题",
                    "JOINGUILDTYPEQUIZ": "测试题",
                }
                join_type_text = str(join_type).replace("_", "").upper()
                pretty_join_type = join_type_map.get(str(join_type), join_type_map.get(join_type_text, str(join_type)))
                lines.append(f"加入方式：{pretty_join_type}")
        pair_items: List[Tuple[str, str]] = []
        for key, label in (("guild_name", "频道名"), ("name", "频道名"), ("guild_number", "频道号"), ("guild_profile", "简介"), ("profile", "简介"), ("member_count", "成员数"), ("url", "链接"), ("share_url", "分享链接"), ("nick", "昵称"), ("nickname", "昵称"), ("tinyid", "用户ID"), ("tiny_id", "用户ID"), ("tinyId", "用户ID"), ("isGuildAuthor", "是否频道创作者"), ("is_guild_author", "是否频道创作者")):
            value = payload.get(key)
            if value not in (None, "", []):
                pair_items.append((label, str(value)))
        if pair_items:
            rows = [[label, value] for label, value in pair_items]
            lines.extend(_table(["属性", "值"], rows))
        if title == "频道资料":
            current_gid = payload.get("guild_id") or payload.get("guildId") or guild_id
            is_owner, is_admin = _can_manage_members(current_gid)
            read_ops = []
            write_ops = []
            if current_gid and is_owner:
                write_ops.extend([
                    _quick_cmd(f"频道改名 {current_gid} 新名称", "改名"),
                    _quick_cmd(f"频道改简介 {current_gid} 新简介", "改简介"),
                    _quick_cmd(f"频道改头像 {current_gid} 图片路径", "改头像"),
                    _quick_cmd(f"频道加入方式 {current_gid}", "加入方式"),
                ])
            if current_gid:
                read_ops.extend([
                    _quick_cmd(f"频道版块 {current_gid}", "版块"),
                    _quick_cmd(f"频道成员 {current_gid}", "成员"),
                    _quick_cmd(f"频道帖子 {current_gid}", "帖子"),
                ])
            if read_ops or write_ops:
                if read_ops:
                    lines.append("")
                    lines.append(f"查看：{' / '.join(read_ops)}")
                if write_ops:
                    lines.append(f"管理：{' / '.join(write_ops)}")
        if title == "加入方式":
            gid = payload.get("guild_id") or payload.get("guildId") or guild_id
            if gid:
                lines.append(_quick_cmd(f"频道设直接加入 {gid}", "设直接加入"))
                lines.append(_quick_cmd(f"频道设审核加入 {gid}", "设审核加入"))
                lines.append(_quick_cmd(f"频道设禁止加入 {gid}", "设禁止加入"))
                lines.append(_quick_cmd(f"频道加入提问审核 {gid} 你从哪知道这里?|你的用途是什么?", "提问审核"))
                lines.append(_quick_cmd(f"频道加入多题验证 {gid} 1+1=?=2|你是谁?=管理员", "多题验证"))
                lines.append(_quick_cmd(f"频道加入测试题 {gid} 1+1=? | 1,2,3 | 2", "测试题"))
        return lines

    if title == "频道版块":
        items = payload.get("channels") or payload.get("list") or payload.get("items")
        if isinstance(items, list):
            lines.append(f"版块数：{len(items)}")
            gid = payload.get("guild_id") or payload.get("guildId") or guild_id or "频道ID"
            rows: List[List[Any]] = []
            for item in items[:20]:
                name = item.get("channel_name") or item.get("name") or "未命名版块"
                cid = item.get("channel_id") or item.get("channelId")
                ctype = item.get("channel_type") or item.get("channelType") or item.get("type") or "-"
                ops = []
                if cid:
                    ops.extend([
                        _quick_cmd(f"频道修改版块 {gid} {cid} 新版块名", "修改"),
                        _quick_cmd(f"频道删除版块 {gid} {cid}", "删除"),
                        _quick_cmd(f"频道发帖 {gid} {cid} 内容", "发帖"),
                        _quick_cmd(f"频道长帖 {gid} {cid} 标题 | 正文", "长帖"),
                    ])
                display_name = _truncate_display_text(name, 18)
                cid_short = _shrink_token(cid) if cid else "-"
                rows.append([display_name, cid_short, ctype, " ".join(ops) if ops else "-"])
            if rows:
                lines.extend(_table(["版块名称", "ID", "类型", "操作"], rows))
        return lines

    if title in {"频道成员", "频道搜成员"}:
        gid = payload.get("guild_id") or payload.get("guildId") or guild_id or payload.get("_local_guild_id")
        next_page_token = payload.get("next_page_token") or payload.get("nextPageToken") or payload.get("nextpagetoken")
        match_count = payload.get("match_count") if title == "频道搜成员" else None
        has_more = payload.get("has_more") if title == "频道搜成员" else None

        # ── 本地翻页路径：handle_member_list 切片后注入的 _local_page_items ──
        local_page_items = payload.get("_local_page_items")
        local_page_index = payload.get("_local_page_index", 0)
        local_total = payload.get("_local_total", 0)
        local_next_token = payload.get("_local_next_token")
        local_prev_cmd = payload.get("_local_prev_cmd", f"频道成员 {gid}")
        if isinstance(local_page_items, list) and local_page_items:
            is_owner, is_admin = _can_manage_members(gid)
            page_start = local_page_index * MEMBER_PAGE_SIZE + 1
            page_end = min(page_start + MEMBER_PAGE_SIZE - 1, local_total)
            lines.append(f"成员数：{local_total}（第 {page_start}-{page_end} 条）")
            rows = []
            for item in local_page_items:
                tiny_id = item.get("tinyid") or item.get("tiny_id") or item.get("tinyId")
                name = _member_chip(gid, tiny_id, _member_name(item))
                member_role = str(item.get("role") or "").strip()
                ops = []
                if gid and tiny_id and is_admin:
                    ops.append(_quick_cmd(f"频道禁言 {gid} {tiny_id} 1小时", "禁言"))
                    ops.append(_quick_cmd(f"频道踢出 {gid} {tiny_id}", "踢出"))
                    if is_owner:
                        if "管理员" in member_role:
                            ops.append(_quick_cmd(f"频道取消管理员 {gid} {tiny_id}", "取消管理"))
                        elif "频道主" not in member_role:
                            ops.append(_quick_cmd(f"频道设置管理员 {gid} {tiny_id}", "设管理"))
                rows.append([name, " ".join(ops)])
            lines.extend(_table(["成员", "操作"], rows))
            # 本地下一页
            if page_end < local_total and local_next_token:
                lines.append(_quick_cmd(f"频道成员 {gid} {local_next_token}", "下一页"))
            lines.append(_quick_cmd(local_prev_cmd, "回到首页"))
            if gid:
                lines.append("快捷搜索：" + " / ".join([
                    _quick_cmd(f"频道搜成员 {gid} 关键词", "搜成员"),
                    _quick_cmd(f"频道搜帖 {gid} 关键词", "搜帖"),
                ]))
            lines.append("")
            return lines

        # ── 正常渲染路径（首次 API 返回或搜索结果） ──
        is_owner, is_admin = _can_manage_members(gid)
        grouped = [
            ("owners", "频道主"),
            ("admins", "管理员"),
            ("robots", "机器人"),
            ("ai_members", "机器人"),
            ("members", "普通成员"),
        ]
        rendered = False
        all_members: List[Dict[str, Any]] = []
        for key, label in grouped:
            items = payload.get(key)
            if not isinstance(items, list) or not items:
                continue
            rendered = True
            lines.append(f"{label}：{len(items)} 人")
            all_members.extend(items)  # 收集用于本地翻页
            rows = []
            for item in items[:MEMBER_PAGE_SIZE]:
                tiny_id = item.get("tinyid") or item.get("tiny_id") or item.get("tinyId")
                name = _member_chip(gid, tiny_id, _member_name(item))
                member_role = str(item.get("role") or "").strip()
                ops = []
                if gid and tiny_id and is_admin:
                    ops.append(_quick_cmd(f"频道禁言 {gid} {tiny_id} 1小时", "禁言"))
                    ops.append(_quick_cmd(f"频道踢出 {gid} {tiny_id}", "踢出"))
                    if is_owner:
                        if "管理员" in member_role:
                            ops.append(_quick_cmd(f"频道取消管理员 {gid} {tiny_id}", "取消管理"))
                        elif "频道主" not in member_role:
                            ops.append(_quick_cmd(f"频道设置管理员 {gid} {tiny_id}", "设管理"))
                rows.append([name, " ".join(ops)])
            lines.extend(_table(["成员", "操作"], rows))
        if rendered:
            if title == "频道搜成员" and match_count not in (None, ""):
                lines.insert(0, f"匹配数：{match_count}")
            if title == "频道搜成员" and has_more:
                lines.append("提示：搜索结果较多，请换更具体的关键词")
            # 本地翻页：缓存全部成员数据，生成本地翻页令牌（仅当总人数超过一页时）
            total_local = len(all_members)
            if gid and total_local > MEMBER_PAGE_SIZE:
                cache_payload = {
                    "guild_id": gid,
                    "all_members": all_members,
                    "page_index": 0,
                    "raw_payload": dict(payload),
                    "prev_cmd": f"频道成员 {gid}",
                }
                # 同时保留 API 翻页能力（本地数据不够时备用）
                if next_page_token:
                    cache_payload["next_page_token"] = next_page_token
                local_token = _save_token_payload("member_page", cache_payload)
                lines.append(_quick_cmd(f"频道成员 {gid} {local_token}", "下一页（本地）"))
            elif gid and next_page_token:
                # 不够一页但有 API 翻页令牌，走 API 翻页
                page_token = _save_token_payload("member_page", {"guild_id": gid, "next_page_token": next_page_token, "prev_cmd": f"频道成员 {gid}"})
                lines.append(_quick_cmd(f"频道成员 {gid} {page_token}", "下一页"))
            if gid:
                lines.append(_quick_cmd(f"频道成员 {gid}", "重新搜索"))
                lines.append("快捷搜索：" + " / ".join([
                    _quick_cmd(f"频道搜成员 {gid} 关键词", "搜成员"),
                    _quick_cmd(f"频道搜帖 {gid} 关键词", "搜帖"),
                ]))
                lines.append("")
            return lines

        items = payload.get("members") or payload.get("items") or payload.get("list")
        if isinstance(items, list):
            all_members_ungrouped = list(items)
            lines.append(f"{'匹配数' if title == '频道搜成员' else '成员数'}：{match_count if title == '频道搜成员' and match_count not in (None, '') else len(items)}")
            rows = []
            for item in items[:MEMBER_PAGE_SIZE]:
                tiny_id = item.get("tinyid") or item.get("tiny_id") or item.get("tinyId")
                name = _member_chip(gid, tiny_id, _member_name(item))
                member_role = str(item.get("role") or "").strip()
                ops = []
                if gid and tiny_id and is_admin:
                    ops.append(_quick_cmd(f"频道禁言 {gid} {tiny_id} 1小时", "禁言"))
                    ops.append(_quick_cmd(f"频道踢出 {gid} {tiny_id}", "踢出"))
                    if is_owner:
                        if "管理员" in member_role:
                            ops.append(_quick_cmd(f"频道取消管理员 {gid} {tiny_id}", "取消管理"))
                        elif "频道主" not in member_role:
                            ops.append(_quick_cmd(f"频道设置管理员 {gid} {tiny_id}", "设管理"))
                rows.append([name, " ".join(ops)])
            lines.extend(_table(["成员", "操作"], rows))
            # 未分组列表的本地翻页
            total_ungrouped = len(all_members_ungrouped)
            if gid and total_ungrouped > MEMBER_PAGE_SIZE:
                cache_payload = {
                    "guild_id": gid,
                    "all_members": all_members_ungrouped,
                    "page_index": 0,
                    "raw_payload": dict(payload),
                    "prev_cmd": f"频道成员 {gid}",
                }
                if next_page_token:
                    cache_payload["next_page_token"] = next_page_token
                local_token = _save_token_payload("member_page", cache_payload)
                lines.append(_quick_cmd(f"频道成员 {gid} {local_token}", "下一页（本地）"))
        if title == "频道搜成员" and has_more:
            lines.append("提示：搜索结果较多，请换更具体的关键词")
        if gid and next_page_token and len(all_members if rendered else (all_members_ungrouped if isinstance(items, list) else [])) <= MEMBER_PAGE_SIZE:
            page_token = _save_token_payload("member_page", {"guild_id": gid, "next_page_token": next_page_token, "prev_cmd": f"频道成员 {gid}"})
            lines.append(_quick_cmd(f"频道成员 {gid} {page_token}", "下一页"))
            lines.append(_quick_cmd(f"频道成员 {gid}", "重新搜索"))
            lines.append("快捷搜索：" + " / ".join([
                _quick_cmd(f"频道搜成员 {gid} 关键词", "搜成员"),
                _quick_cmd(f"频道搜帖 {gid} 关键词", "搜帖"),
            ]))
            lines.append("")
        return lines

    if title in {"频道帖子", "频道搜帖", "全局搜帖"}:
        items = payload.get("feeds") or payload.get("guild_feeds") or payload.get("items") or payload.get("list")
        if isinstance(items, list):
            if title == "频道搜帖":
                total = payload.get("total") or payload.get("match_count") or len(items)
                has_more = payload.get("has_more") or payload.get("hasMore")
                lines.append(f"匹配数：{total}")
                if isinstance(has_more, bool):
                    lines.append(f"还有更多：{'是' if has_more else '否'}")
            else:
                lines.append(f"帖子数：{len(items)}")
            rows = []
            for item in items[:10]:
                feed_id = item.get("feed_id") or item.get("feedId")
                item_gid = item.get("guild_id") or item.get("guildId") or item.get("guildid")
                create_time = item.get("create_time_raw") or item.get("create_time") or item.get("createTime")
                name = item.get("title") or item.get("content") or item.get("text") or "无标题帖子"
                if title == "频道搜帖":
                    author = item.get("author_nick") or item.get("nickname") or item.get("nick") or item.get("author") or "-"
                    rows.append([
                        _truncate_display_text(name, 24),
                        _truncate_display_text(author, 12),
                        " / ".join([
                            _quick_cmd(f"帖子详情 {feed_id}" + (f" {item_gid}" if item_gid else ""), "详情") if feed_id else "",
                            _quick_cmd(f"帖子评论 {feed_id}", "评论") if feed_id else "",
                            _quick_cmd(f"帖子评论 {feed_id} {create_time} 内容", "回复") if feed_id and create_time else "",
                        ]).strip(" /")
                    ])
                else:
                    ops = []
                    if feed_id:
                        ops.append(_quick_cmd(f"帖子详情 {feed_id}" + (f" {item_gid}" if item_gid else ""), "详情"))
                        ops.append(_quick_cmd(f"帖子评论 {feed_id}", "评论"))
                    if feed_id and create_time:
                        ops.append(_quick_cmd(f"帖子评论 {feed_id} {create_time} 内容", "回复"))
                    rows.append([_truncate_display_text(name, 24), " / ".join(ops)])
            if title == "频道搜帖":
                lines.extend(_table(["帖子", "作者", "操作"], rows))
            else:
                lines.extend(_table(["帖子", "操作"], rows))
        if title == "频道帖子":
            attach_info = payload.get("attach_info") or payload.get("feed_attach_info") or payload.get("feedAttachInfo")
            gid = payload.get("guild_id") or payload.get("guildId") or guild_id
            get_type = payload.get("get_type") or payload.get("getType") or 2
            if gid and attach_info:
                page_token = _save_token_payload("feed_page", {"guild_id": gid, "attach_info": attach_info, "get_type": get_type, "prev_cmd": f"频道帖子 {gid}"})
                lines.append(_quick_cmd(f"频道帖子 {gid} {page_token}", "下一页"))
                lines.append(_quick_cmd(f"频道帖子 {gid}", "重新搜索"))
        if title == "频道搜帖":
            next_page_cookie = payload.get("next_page_cookie") or payload.get("nextPageCookie") or payload.get("nextpagecookie")
            gid = payload.get("guild_id") or payload.get("guildId") or guild_id
            query = str(payload.get("query") or payload.get("keyword") or "").strip()
            if gid and next_page_cookie and query:
                page_token = _save_token_payload("search_feed_page", {"guild_id": gid, "query": query, "next_page_cookie": next_page_cookie, "prev_cmd": f"频道搜帖 {gid} {query}"})
                lines.append(_quick_cmd(f"频道搜帖 {gid} {page_token}", "下一页"))
                lines.append(_quick_cmd(f"频道搜帖 {gid} {query}", "重新搜索"))
        return lines

    if title == "帖子详情":
        # CLI 返回 {data: {feed: {...}}}，需要解包 feed 层
        feed_wrapper = payload.get("feed")
        if isinstance(feed_wrapper, dict):
            payload = feed_wrapper
        detail_items = [
            ("title", "标题"),
            ("content", "内容"),
            ("content_richtext", "富文本内容"),
            ("content_snippet", "内容摘要"),
            ("share_url", "帖子链接"),
            ("create_time", "时间"),
            ("author", "作者"),
            ("author_id", "作者ID"),
            ("channel_name", "版块"),
            ("channel_id", "版块ID"),
            ("guild_name", "频道"),
            ("guild_id", "频道ID"),
            ("feed_type", "帖子类型"),
            ("prefer_count", "点赞数"),
            ("comment_count", "评论数"),
        ]
        seen = set()
        has_detail = False
        rows: List[List[Any]] = []
        for key, label in detail_items:
            value = payload.get(key)
            if value in (None, "", []):
                continue
            if label in seen and label == "内容":
                continue
            display_value = str(value)
            # 内容字段截断到 300 字符避免刷屏
            if len(display_value) > 300:
                display_value = display_value[:300] + "..."
            rows.append([label, display_value])
            seen.add(label)
            has_detail = True
        if rows:
            lines.extend(_table(["属性", "值"], rows))
        # fallback：如果标准字段都没命中，遍历所有字段展示
        if not has_detail:
            for key, value in payload.items():
                if key in ("feed_id", "feedId", "guild_id", "guildId", "channel_id", "channelId",
                           "author_id", "authorId", "create_time_raw"):
                    continue
                if value is None or value == "" or value == []:
                    continue
                display_value = str(value)
                if len(display_value) > 300:
                    display_value = display_value[:300] + "..."
                lines.append(f"{key}：{display_value}")
        feed_id = payload.get("feed_id") or payload.get("feedId")
        gid = payload.get("guild_id") or payload.get("guildId") or guild_id
        cid = payload.get("channel_id") or payload.get("channelId")
        create_time = payload.get("create_time_raw") or payload.get("create_time") or payload.get("createTime")
        author_id = payload.get("author_id") or payload.get("authorId")
        if feed_id:
            lines.append(_quick_cmd(f"帖子评论 {feed_id}" + (f" {gid}" if gid else ""), "评论列表"))
            if create_time:
                lines.append(_quick_cmd(f"帖子评论 {feed_id} {create_time} 内容", "发表评论"))
            lines.append(_quick_cmd(f"帖子点赞 {feed_id}", "点赞"))
            lines.append(_quick_cmd(f"帖子取消点赞 {feed_id}", "取消点赞"))
        if feed_id and gid:
            lines.append(_quick_cmd(f"帖子分享链接 {feed_id} {gid}", "帖子链接"))
        if feed_id and gid and cid and create_time:
            lines.append(_quick_cmd(f"帖子删除 {feed_id} {gid} {cid} {create_time}", "删除帖子"))
        if feed_id and author_id and create_time and gid:
            lines.append(_quick_cmd(f"帖子置顶 {feed_id} {author_id} {create_time} {gid}", "置顶"))
            lines.append(_quick_cmd(f"帖子取消置顶 {feed_id} {author_id} {create_time} {gid}", "取消置顶"))
        return lines

    if title in {"帖子点赞", "帖子取消点赞"}:
        prefer_count = payload.get("preferCount") or payload.get("prefer_count")
        if prefer_count is not None:
            action_text = "已点赞" if title == "帖子点赞" else "已取消点赞"
            lines.extend(_table(["项目", "值"], [["结果", action_text], ["当前点赞数", str(prefer_count)]]))
            return lines

    if title == "帖子评论":
        items = payload.get("comments") or payload.get("items") or payload.get("list")
        feed_id = payload.get("feed_id") or payload.get("feedId")
        guild_id = payload.get("guild_id") or payload.get("guildId")
        feed_create_time = payload.get("create_time_raw") or payload.get("feed_create_time") or payload.get("create_time") or payload.get("createTime")
        feed_author_id_global = payload.get("feed_author_id") or payload.get("author_id") or payload.get("authorId") or payload.get("feedAuthorId")
        # 帖子级字段缺失时补查一次帖子详情，保证每条评论都能生成「回复」按钮
        if feed_id and (not feed_create_time or not feed_author_id_global):
            detail_args = ["feed", "get-feed-detail", "--feed-id", str(feed_id), "--json"]
            if guild_id:
                detail_args[2:2] = ["--guild-id", str(guild_id)]
            ok_detail, output_detail = _run_cli(detail_args)
            if ok_detail:
                detail_data = _extract_json(output_detail)
                detail_payload = detail_data.get("data") if isinstance(detail_data, dict) and isinstance(detail_data.get("data"), dict) else {}
                feed_obj = detail_payload.get("feed") if isinstance(detail_payload.get("feed"), dict) else detail_payload
                if isinstance(feed_obj, dict):
                    feed_create_time = feed_create_time or feed_obj.get("create_time_raw") or feed_obj.get("feed_create_time") or feed_obj.get("create_time") or feed_obj.get("createTime")
                    feed_author_id_global = feed_author_id_global or feed_obj.get("author_id") or feed_obj.get("authorId") or feed_obj.get("feed_author_id")
        if feed_id and feed_create_time:
            lines.append(_quick_cmd(f"帖子评论 {feed_id} {feed_create_time} 内容", "发表评论"))
        if isinstance(items, list):
            lines.append(f"评论数：{len(items)}")
            if not items:
                lines.append("暂无评论")
            rows = []
            for item in items[:15]:
                cid = item.get("comment_id") or item.get("commentId")
                nick = item.get("author_nick") or item.get("nick") or item.get("nickname") or "未知用户"
                content = item.get("content")
                if isinstance(content, dict):
                    display_content = str(content.get("text") or "").strip()
                else:
                    display_content = str(content or "").strip()
                rich_text = item.get("content_richtext") or item.get("contentRichtext") or item.get("rich_text")
                if (not display_content) and isinstance(rich_text, dict):
                    display_content = str(rich_text.get("text") or "").strip()
                display_content = display_content or "-"
                if len(display_content) > 60:
                    display_content = display_content[:60] + "..."
                feed_id = payload.get("feed_id") or payload.get("feedId") or item.get("feed_id") or item.get("feedId")
                feed_author_id = feed_author_id_global or item.get("feed_author_id")
                item_feed_create_time = feed_create_time or item.get("feed_create_time")
                comment_author_id = item.get("author_id") or item.get("comment_author_id") or item.get("authorId")
                comment_create_time = item.get("comment_create_time") or item.get("create_time_raw") or item.get("create_time") or item.get("createTime")
                guild_id = item.get("guild_id") or item.get("guildId") or payload.get("guild_id") or payload.get("guildId")
                channel_id = item.get("channel_id") or item.get("channelId") or payload.get("channel_id") or payload.get("channelId")
                attach_info = item.get("attach_info") or item.get("attachInfo")
                ops = []
                if feed_id and cid and feed_author_id and item_feed_create_time and comment_author_id:
                    like_token = _save_token_payload("comment_like", {
                        "feed_id": feed_id,
                        "comment_id": cid,
                        "feed_author_id": feed_author_id,
                        "feed_create_time": item_feed_create_time,
                        "comment_author_id": comment_author_id,
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                    })
                    delete_token = _save_token_payload("delete_comment", {
                        "feed_id": feed_id,
                        "comment_id": cid,
                        "comment_author_id": comment_author_id,
                        "feed_create_time": item_feed_create_time,
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                    })
                    ops.append(_quick_cmd(f"评论点赞 {like_token}", "点赞"))
                    ops.append(_quick_cmd(f"评论取消点赞 {like_token}", "取消点赞"))
                    ops.append(_quick_cmd(f"删除评论 {delete_token}", "删除"))
                if feed_id and cid and feed_author_id and item_feed_create_time and comment_author_id and comment_create_time:
                    reply_token = _save_token_payload("reply_comment", {
                        "feed_id": feed_id,
                        "comment_id": cid,
                        "feed_author_id": feed_author_id,
                        "feed_create_time": item_feed_create_time,
                        "comment_author_id": comment_author_id,
                        "comment_create_time": comment_create_time,
                        "guild_id": guild_id,
                    })
                    ops.append(_quick_cmd(f"帖子评论回复 {reply_token} ", "回复"))
                if feed_id and cid and guild_id and channel_id:
                    page_token = _save_token_payload("reply_page", {
                        "feed_id": feed_id,
                        "comment_id": cid,
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "attach_info": attach_info,
                    })
                    ops.append(_quick_cmd(f"帖子回复 {page_token}", "更多回复"))
                rows.append([_truncate_display_text(nick, 12), display_content, " / ".join(ops)])
            if rows:
                lines.extend(_table(["作者", "内容", "操作"], rows))
        attach_info = payload.get("attach_info") or payload.get("next_page_cookie") or payload.get("attachinfo")
        if attach_info and feed_id:
            comment_page_token = _save_token_payload("comment_page", {"feed_id": feed_id, "attach_info": attach_info, "guild_id": guild_id, "prev_cmd": f"帖子评论 {feed_id}" + (f" {guild_id}" if guild_id else "")})
            lines.append(_quick_cmd(f"帖子评论 {comment_page_token}", "下一页"))
            lines.append(_quick_cmd(f"帖子评论 {feed_id}" + (f" {guild_id}" if guild_id else ""), "回到首页"))
        return lines

    if title == "评论回复":
        items = payload.get("items") or payload.get("replies") or payload.get("list")
        if isinstance(items, list):
            lines.append(f"回复数：{len(items)}")
            feed_id = payload.get("feed_id") or payload.get("feedId")
            comment_id = payload.get("comment_id") or payload.get("commentId")
            guild_id = payload.get("guild_id") or payload.get("guildId")
            channel_id = payload.get("channel_id") or payload.get("channelId")
            feed_author_id = payload.get("feed_author_id") or payload.get("author_id")
            feed_create_time = payload.get("feed_create_time") or payload.get("create_time_raw")
            comment_author_id = payload.get("comment_author_id")
            comment_create_time = payload.get("comment_create_time")
            for item in items[:20]:
                reply_id = item.get("reply_id") or item.get("replyId")
                reply_author_id = item.get("author_id") or item.get("reply_author_id") or item.get("authorId")
                nick = item.get("nick") or item.get("nickname") or item.get("author_nick") or "未知用户"
                content = item.get("content") or ""
                ops = []
                if feed_id and comment_id and reply_id and feed_author_id and feed_create_time and comment_author_id and reply_author_id:
                    like_token = _save_token_payload("reply_like", {
                        "feed_id": feed_id,
                        "comment_id": comment_id,
                        "reply_id": reply_id,
                        "feed_author_id": feed_author_id,
                        "feed_create_time": feed_create_time,
                        "comment_author_id": comment_author_id,
                        "reply_author_id": reply_author_id,
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                    })
                    ops.append(_quick_cmd(f"回复点赞 {like_token}", "回复点赞"))
                    ops.append(_quick_cmd(f"回复取消点赞 {like_token}", "取消点赞"))
                if feed_id and comment_id and reply_id and reply_author_id and feed_author_id and feed_create_time and comment_author_id and comment_create_time:
                    delete_token = _save_token_payload("delete_reply", {
                        "feed_id": feed_id,
                        "comment_id": comment_id,
                        "reply_id": reply_id,
                        "replier_id": reply_author_id,
                        "feed_author_id": feed_author_id,
                        "feed_create_time": feed_create_time,
                        "comment_author_id": comment_author_id,
                        "comment_create_time": comment_create_time,
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                    })
                    ops.append(_quick_cmd(f"删除回复 {delete_token}", "删除回复"))
                if feed_id and comment_id and feed_author_id and feed_create_time and comment_author_id and comment_create_time:
                    reply_token = _save_token_payload("reply_comment", {
                        "feed_id": feed_id,
                        "comment_id": comment_id,
                        "feed_author_id": feed_author_id,
                        "feed_create_time": feed_create_time,
                        "comment_author_id": comment_author_id,
                        "comment_create_time": comment_create_time,
                        "guild_id": guild_id,
                        "target_reply_id": reply_id,
                        "target_user_id": reply_author_id,
                        "target_user_nick": nick,
                    })
                    show_text = f"继续回复 {_shrink_token(reply_id or comment_id)}"
                    ops.append(_quick_cmd(f"帖子评论回复 {reply_token} ", show_text))
                lines.append(f"- {nick}：{content[:80]}" + (f"（回复ID：{reply_id}）" if reply_id else "") + (f" {' '.join(ops)}" if ops else ""))
        attach_info = payload.get("attach_info") or payload.get("next_page_cookie")
        feed_id = payload.get("feed_id") or payload.get("feedId")
        comment_id = payload.get("comment_id") or payload.get("commentId")
        guild_id = payload.get("guild_id") or payload.get("guildId")
        channel_id = payload.get("channel_id") or payload.get("channelId")
        if attach_info and feed_id and comment_id and guild_id and channel_id:
            page_token = _save_token_payload("reply_page", {
                "feed_id": feed_id,
                "comment_id": comment_id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "attach_info": attach_info,
                "prev_cmd": f"帖子回复 {feed_id} {comment_id} {guild_id} {channel_id}",
            })
            lines.append(_quick_cmd(f"帖子回复 {page_token}", "下一页"))
            lines.append(_quick_cmd(f"帖子回复 {feed_id} {comment_id} {guild_id} {channel_id}", "回到首页"))
        return lines

    if title == "互动消息":
        items = payload.get("items") or payload.get("list") or payload.get("notices")
        if isinstance(items, list):
            lines.append(f"消息数：{len(items)}")
            for item in items[:15]:
                content = item.get("content") or item.get("title") or item.get("desc") or "互动消息"
                feed_id = item.get("feed_id") or item.get("feedId")
                gid = item.get("guild_id") or item.get("guildId")
                ops = []
                if feed_id:
                    ops.append(_quick_cmd(f"帖子详情 {feed_id}" + (f" {gid}" if gid else ""), "帖子详情"))
                lines.append(f"- {content[:100]}" + (f" {' '.join(ops)}" if ops else ""))
        attach_info = payload.get("attach_info") or payload.get("next_page_cookie")
        if attach_info:
            gid = payload.get("guild_id") or payload.get("guildId")
            page_token = _save_token_payload("notice_page", {"guild_id": gid, "attach_info": attach_info, "prev_cmd": f"互动消息 {gid}" if gid else "互动消息"})
            lines.append(_quick_cmd(f"互动消息 {page_token}", "下一页"))
            lines.append(_quick_cmd(f"互动消息" + (f" {gid}" if gid else ""), "回到首页"))
        return lines

    if title in {"搜频道", "搜作者"}:
        # 搜频道返回 channels/guilds 列表，搜作者返回 authors 列表
        items = payload.get("items") or payload.get("list") or payload.get("guilds") or payload.get("channels")
        authors = payload.get("authors")
        if isinstance(authors, list):
            lines.append(f"结果数：{len(authors)}")
            rows = []
            for author in authors[:10]:
                author_name = author.get("name") or "未知用户"
                author_id = author.get("author_id") or author.get("id") or ""
                ops = []
                if author_id:
                    ops.append(_quick_cmd(f"频道用户资料 {author_id}", "查看资料"))
                rows.append([_truncate_display_text(author_name, 25), " ".join(ops)])
            lines.extend(_table(["作者", "操作"], rows))
            next_page_token = payload.get("next_page_token") or payload.get("nextPageToken") or payload.get("nextpagetoken")
            keyword = payload.get("keyword") or ""
            if next_page_token:
                page_token = _save_token_payload("search_guild_page", {"next_page_token": next_page_token, "scope": "author", "keyword": keyword})
                lines.append(_quick_cmd(f"频道搜作者 {page_token}", "下一页"))
                lines.append(_quick_cmd(f"频道搜作者 {keyword}", "重新搜索"))
            return lines
        if isinstance(items, list):
            lines.append(f"结果数：{len(items)}")
            rows = []
            for item in items[:10]:
                name = item.get("guild_name") or item.get("name") or item.get("nick") or "未命名"
                guild_id = item.get("guild_id") or item.get("guildId")
                member_count = item.get("member_count") or item.get("memberCount") or "-"
                profile = item.get("profile") or item.get("guild_profile") or ""
                share_url = item.get("share_url") or item.get("shareUrl") or ""
                ops = []
                if guild_id:
                    ops.append(_quick_cmd(f"频道资料 {guild_id}", "资料"))
                    ops.append(_quick_cmd(f"频道成员 {guild_id}", "成员"))
                    ops.append(_quick_cmd(f"频道帖子 {guild_id}", "帖子"))
                if share_url:
                    ops.append(_quick_cmd(f"频道解析 {share_url}", "解析链接"))
                rows.append([
                    _truncate_display_text(name, 20),
                    member_count,
                    _truncate_display_text(profile, 30),
                    " ".join(ops)
                ])
            lines.extend(_table(["频道", "人数", "简介", "操作"], rows))
            # 翻页
            next_page_token = payload.get("next_page_token") or payload.get("nextPageToken") or payload.get("nextpagetoken")
            keyword = payload.get("keyword") or ""
            if next_page_token:
                scope = "channel" if title == "搜频道" else "author"
                base_cmd = f"频道{'搜频道' if title == '搜频道' else '搜作者'}"
                page_token = _save_token_payload("search_guild_page", {"next_page_token": next_page_token, "scope": scope, "keyword": keyword})
                lines.append(_quick_cmd(f"{base_cmd} {page_token}", "下一页"))
                lines.append(_quick_cmd(f"{base_cmd} {keyword}", "重新搜索"))
        elif not (payload.get("has_more") or payload.get("isEnd") or payload.get("next_page_token")):
            # 没有任何数据且没有翻页标记，说明是空结果
            lines.append("未找到匹配结果")
        return lines

    if title in {"全局搜帖"}:
        items = payload.get("feeds") or payload.get("items") or payload.get("list")
        if isinstance(items, list):
            lines.append(f"帖子数：{len(items)}")
            rows = []
            for item in items[:10]:
                feed_id = item.get("feed_id") or item.get("feedId") or ""
                title_text = item.get("title") or item.get("content") or "无标题帖子"
                guild_id = item.get("guild_id") or item.get("guildId") or ""
                ops = []
                if feed_id:
                    ops.append(_quick_cmd(f"帖子详情 {feed_id}" + (f" {guild_id}" if guild_id else ""), "详情"))
                    ops.append(_quick_cmd(f"帖子评论 {feed_id}", "评论"))
                rows.append([_truncate_display_text(title_text, 35), " ".join(ops)])
            lines.extend(_table(["帖子", "操作"], rows))
            # 翻页
            next_page_token = payload.get("next_page_token") or payload.get("nextPageToken") or payload.get("nextpagetoken")
            keyword = payload.get("keyword") or ""
            if next_page_token:
                page_token = _save_token_payload("search_feed_global_page", {"next_page_token": next_page_token, "keyword": keyword})
                lines.append(_quick_cmd(f"频道全局搜帖 {page_token}", "下一页"))
                lines.append(_quick_cmd(f"频道全局搜帖 {keyword}", "重新搜索"))
        elif not (payload.get("has_more") or payload.get("isEnd") or payload.get("next_page_token")):
            lines.append("未找到匹配帖子")
        return lines

    return lines


# ── 通用兜底渲染：当 _render_summary 没有针对该 title 的专门处理时使用 ──
def _render_fallback_summary(data: Dict[str, Any]) -> List[str]:
    """将 data 中的关键字段以友好的键值对形式展示，避免输出原始 JSON"""
    lines: List[str] = []
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(payload, dict):
        return lines

    # 跳过这些无意义的内部字段
    skip_keys = {
        "success", "retCode", "ret_code", "code", "message", "msg", "error",
        "description", "status", "need_verification", "pending", "resume_command",
    }

    # 字段标签映射：把 API 的 snake_case/camelCase 字段名转为中文友好显示
    LABEL_MAP = {
        # 频道相关
        "guild_id": "频道ID", "guildId": "频道ID", "guild_name": "频道名称", "name": "名称",
        "guild_number": "频道号", "guild_profile": "简介", "profile": "简介",
        "member_count": "成员数", "memberCount": "成员数", "url": "链接", "share_url": "分享链接",
        "shareUrl": "分享链接", "avatar": "头像", "guild_avatar": "频道头像",
        # 版块相关
        "channel_id": "版块ID", "channelId": "版块ID", "channel_name": "版块名称",
        "channelName": "版块名称", "channel_type": "版块类型", "channelType": "版块类型",
        # 帖子/内容相关
        "feed_id": "帖子ID", "feedId": "帖子ID", "title": "标题", "content": "内容",
        "content_richtext": "富文本内容", "content_snippet": "内容摘要",
        "create_time": "创建时间", "createTime": "创建时间", "create_time_raw": "创建时间",
        "share_url": "帖子链接", "feed_type": "帖子类型", "prefer_count": "点赞数",
        "comment_count": "评论数", "author": "作者", "author_id": "作者ID", "authorId": "作者ID",
        # 用户相关
        "tinyid": "用户ID", "tiny_id": "用户ID", "tinyId": "用户ID", "user_id": "用户ID",
        "userId": "用户ID", "nick": "昵称", "nickname": "昵称", "role": "角色",
        "role_name": "角色名称", "gender": "性别", "joinTime": "加入时间",
        "join_time": "加入时间", "isGuildAuthor": "是否创作者", "is_guild_author": "是否创作者",
        # 操作结果相关
        "feed_essence_status": "精华状态", "top_status": "置顶状态",
        "shut_up_expire_time": "禁言到期时间", "shut_up_expire_time_human": "禁言到期",
        # 分页相关
        "next_page_token": "下一页令牌", "nextPageToken": "下一页令牌",
        "has_more": "还有更多", "match_count": "匹配数量",
        # 列表容器（只显示数量）
        "channels": None, "list": None, "items": None, "feeds": None,
        "members": None, "comments": None, "replies": None,
        "owners": None, "admins": None, "robots": None, "ai_members": None,
    }

    pair_items: List[Tuple[str, str]] = []
    list_info: List[str] = []

    for key, value in payload.items():
        if key in skip_keys or value is None or value == "" or value == []:
            continue
        label = LABEL_MAP.get(key)
        # 列表类字段：只显示数量
        if label is None and isinstance(value, list):
            list_label = key.replace("_", " ").replace("ID", "ID")
            list_info.append(f"{list_label}：{len(value)} 条")
            continue
        # 有标签的字段
        if label:
            display_value = str(value)
            if len(display_value) > 200:
                display_value = display_value[:200] + "..."
            pair_items.append((label, display_value))
        elif not isinstance(value, (dict, list)):
            # 无映射的标量字段也展示
            display_value = str(value)
            if len(display_value) > 200:
                display_value = display_value[:200] + "..."
            pair_items.append((key, display_value))

    # 用 Markdown 表格展示键值对
    if pair_items:
        rows = [[label, value] for label, value in pair_items]
        lines.extend(_table(["属性", "值"], rows))
    for info in list_info:
        lines.append(info)
    return lines


def _help_text() -> str:
    lines = [
        "# 腾讯频道帮助",
        "",
        f"当前状态：预演 **{'开' if _preview_enabled() else '关'}** ｜ 调试 **{'开' if _debug_enabled() else '关'}**",
        "",
        "## 核心入口",
        *_table(["命令", "说明"], [
            [_quick_cmd("频道帮助"), "查看完整帮助"],
            [_quick_cmd("频道自检"), "检查登录与插件状态"],
            [_quick_cmd("频道配置token 你的token", "频道配置token"), "配置或替换 token"],
            [_quick_cmd("频道列表"), "查看我创建/管理/加入的频道"],
            [_quick_cmd("频道开启预演", "开启预演") + " / " + _quick_cmd("频道关闭预演", "关闭预演"), "切换预演模式"],
            [_quick_cmd("频道开启调试", "开启调试") + " / " + _quick_cmd("频道关闭调试", "关闭调试"), "切换调试模式"],
        ]),
        "## 频道与资料查询",
        *_table(["命令", "说明"], [
            [_quick_cmd("频道资料 频道ID", "频道资料"), "查看频道信息"],
            [_quick_cmd("频道版块 频道ID", "频道版块"), "查看版块列表"],
            [_quick_cmd("频道成员 频道ID", "频道成员"), "查看成员列表"],
            [_quick_cmd("频道帖子 频道ID", "频道帖子"), "查看帖子列表"],
            [_quick_cmd("频道用户资料", "我的资料"), "查看自己的资料"],
            [_quick_cmd("频道用户资料 频道ID 用户ID", "用户资料"), "查看指定用户资料"],
            [_quick_cmd("频道加入方式 频道ID", "加入方式"), "查看加入规则"],
        ]),
        "## 搜索",
        *_table(["命令", "说明"], [
            [_quick_cmd("频道搜帖 频道ID 关键词", "搜帖"), "在指定频道搜帖子"],
            [_quick_cmd("频道搜成员 频道ID 昵称", "搜成员"), "在指定频道搜成员"],
            [_quick_cmd("频道搜频道 关键词", "搜频道"), "全局搜索频道"],
            [_quick_cmd("频道搜作者 关键词", "搜作者"), "全局搜索作者"],
            [_quick_cmd("频道全局搜帖 关键词", "全局搜帖"), "全局搜索帖子"],
        ]),
        "## 帖子与互动",
        *_table(["命令", "说明"], [
            [_quick_cmd("帖子详情 帖子ID", "帖子详情"), "查看帖子详情"],
            [_quick_cmd("帖子评论 帖子ID", "评论列表"), "查看评论列表"],
            [_quick_cmd("帖子回复 r123456", "更多回复"), "查看评论下回复（翻页令牌）"],
            [_quick_cmd("互动消息", "互动消息"), "查看互动通知"],
            [_quick_cmd("帖子评论 帖子ID 帖子创建时间 内容", "发表评论"), "给帖子发表评论"],
            [_quick_cmd("帖子回复 r123456 回复内容", "回复某条"), "回复指定回复"],
            [_quick_cmd("帖子评论回复 r123456 回复内容", "回复评论"), "回复指定评论"],
            [_quick_cmd("帖子点赞 帖子ID", "点赞") + " / " + _quick_cmd("帖子取消点赞 帖子ID", "取消点赞"), "帖子点赞 / 取消点赞"],
            [_quick_cmd("帖子分享链接 帖子ID 频道ID", "帖子链接"), "获取帖子链接"],
            [_quick_cmd("帖子设精华 帖子ID", "设精华") + " / " + _quick_cmd("帖子取消精华 帖子ID", "取消精华"), "设置 / 取消精华"],
            [_quick_cmd("帖子置顶 帖子ID 作者ID 创建时间 版块ID", "置顶") + " / " + _quick_cmd("帖子取消置顶 帖子ID 作者ID 创建时间 版块ID", "取消置顶"), "设置 / 取消置顶"],
        ]),
        "## 发帖与版块管理",
        *_table(["命令", "说明"], [
            [_quick_cmd("频道发帖 频道ID 版块ID 内容", "发帖"), "发普通帖子"],
            [_quick_cmd("频道长帖 频道ID 版块ID 标题 | 正文", "长帖"), "发长帖，注意保留竖线"],
            [_quick_cmd("频道创建版块 频道ID 版块名", "创建版块"), "创建版块"],
            [_quick_cmd("频道修改版块 频道ID 版块ID 新版块名", "修改版块"), "修改版块名称"],
            [_quick_cmd("频道删除版块 频道ID 版块ID", "删除版块"), "删除版块"],
        ]),
        "## 成员与频道管理",
        *_table(["命令", "说明"], [
            [_quick_cmd("频道私信 来源频道ID 用户ID 内容", "发送私信"), "给成员发私信"],
            [_quick_cmd("频道禁言 频道ID 用户ID 1小时", "禁言") + " / " + _quick_cmd("频道解除禁言 频道ID 用户ID", "解禁"), "禁言 / 解除禁言"],
            [_quick_cmd("频道踢出 频道ID 用户ID", "踢出"), "移出成员"],
            [_quick_cmd("频道设置管理员 频道ID 用户ID", "设管理员") + " / " + _quick_cmd("频道取消管理员 频道ID 用户ID", "取消管理员"), "设置 / 取消管理员"],
            [_quick_cmd("频道改名 频道ID 新名称", "改名"), "修改频道名称"],
            [_quick_cmd("频道改简介 频道ID 新简介", "改简介"), "修改频道简介"],
            [_quick_cmd("频道改头像 频道ID 图片路径", "改头像"), "修改频道头像"],
        ]),
        "## 加入与创建频道",
        *_table(["命令", "说明"], [
            [_quick_cmd("频道创建 头像路径 公开 频道名 | 简介", "创建频道"), "创建自定义频道"],
            [_quick_cmd("频道建频道 头像路径 主题", "按主题建频道"), "按主题快速建频道"],
            [_quick_cmd("频道加入 频道ID", "加入频道") + " / " + _quick_cmd("频道退出 频道ID", "退出频道"), "加入 / 退出频道"],
            [_quick_cmd("频道加入附言 频道ID 我想加入这个频道", "加入附言"), "附言验证加入"],
            [_quick_cmd("频道加入答题 频道ID 答案1|答案2", "加入答题"), "答题验证加入"],
        ]),
        "## 多账号槽位",
        *_table(["命令", "说明"], [
            [_quick_cmd("频道账号列表"), "查看所有账号槽位及当前槽位"],
            [_quick_cmd("频道添加账号 名称", "添加账号") + " / " + _quick_cmd("频道删除账号 名称", "删除账号"), "创建 / 删除账号槽位"],
            [_quick_cmd("频道切换账号 名称", "切换账号"), "切换当前操作的账号（登录/发帖等都作用在当前槽位）"],
            [_quick_cmd("频道账号状态 名称", "账号状态"), "查看指定槽位的登录状态"],
        ]),
        "## 登录与通知（Skill 1.1.5）",
        *_table(["命令", "说明"], [
            [_quick_cmd("频道登录") + " / " + _quick_cmd("频道登录确认"), "扫码授权登录到当前槽位（扫码后发确认）"],
            [_quick_cmd("频道登录状态") + " / " + _quick_cmd("频道退出登录"), "查看登录状态 / 退出登录"],
            [_quick_cmd("频道版本"), "查看 CLI 版本"],
            [_quick_cmd("频道MD帖 频道ID 版块ID Markdown正文", "MD帖"), "发 Markdown 短帖"],
            [_quick_cmd("频道MD长帖 频道ID 版块ID 标题 | Markdown正文", "MD长帖"), "发 Markdown 长帖"],
            [_quick_cmd("频道通知状态") + " / " + _quick_cmd("频道检查通知") + " / " + _quick_cmd("频道最近通知"), "通知状态 / 增量检查 / 最近记录"],
            [_quick_cmd("评论通知 1 内容", "评论通知") + " / " + _quick_cmd("回复通知 1 内容", "回复通知"), "按通知编号评论帖子 / 回复评论"],
            [_quick_cmd("处理通知 1 同意", "同意申请") + " / " + _quick_cmd("处理通知 1 拒绝", "拒绝申请"), "按通知编号处理系统通知"],
            [_quick_cmd("私信通知回复 1 内容", "回复私信"), "按通知编号回复私信"],
        ]),
        "## 清理与说明",
        *_table(["命令", "说明"], [
            [_quick_cmd("频道清理缓存", "清理缓存"), "清理 self id 与短令牌缓存"],
            [_quick_cmd("频道清理短令牌", "清理短令牌"), "只清理短令牌缓存"],
        ]),
        "- 说明 1：列表、搜索、评论、回复等场景支持短令牌翻页。",
        "- 说明 2：开启预演后只校验参数，不会真正执行写操作。",
        "- 说明 3：开启调试后会额外附上原始返回内容，方便排查。",
        "- 说明 4：长帖命令请用 `标题 | 正文` 的格式，中间保留竖线。",
        "- 说明 5：本插件仅限管理员使用（插件目录 admins.txt，一行一个，可在 Web 后台「腾讯频道」页面维护）。",
        "- 说明 6：Web 后台「腾讯频道」页面支持界面化管理频道 / 帖子 / 评论，以及定时发帖（5 段 Cron）。",
    ]
    return "\n".join(lines)



