#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定时发帖调度器：标准 5 段 Cron (分 时 日 月 周)，支持 * , - / 语法。

计划任务存储在插件目录 feed_schedules.json，通过 Web 面板管理。
内容支持纯文本 / Markdown / HTML（HTML 自动转换为 Markdown 后发布），
可附带图片与视频（本地路径或 URL）。
"""

import asyncio
import datetime
import html.parser
import json
import re
import uuid
from typing import Any, Dict, List, Optional

from core.plugin.decorators import on_load, on_unload

from .腾讯频道 import BASE_DIR, _extract_json, _normalize_rate_limit, _run_cli

SCHEDULES_FILE = BASE_DIR / "feed_schedules.json"
HISTORY_FILE = BASE_DIR / "post_history.json"
HISTORY_MAX = 30

CRON_EXAMPLES = [
    ("0 9 * * *", "每天9点"),
    ("*/30 * * * *", "每30分钟"),
    ("0 12 * * 1", "每周一12点"),
]


# ==================== Cron 解析 ====================

def _parse_cron_field(field: str, lo: int, hi: int) -> set:
    """解析单个 cron 字段, 返回允许值集合。支持 * , - / 语法。"""
    values = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        rng = part
        if "/" in part:
            rng, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError(f"步长无效: {part}")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(rng)
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                values.add(v)
    return values


def cron_valid(expr: str) -> bool:
    if not expr or not isinstance(expr, str):
        return False
    parts = expr.split()
    if len(parts) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    try:
        for part, (lo, hi) in zip(parts, ranges):
            if not _parse_cron_field(part, lo, hi):
                return False
        return True
    except (ValueError, TypeError):
        return False


def cron_match(expr: str, dt: datetime.datetime) -> bool:
    """判断时间 dt 是否匹配标准 5 段 cron 表达式 (分 时 日 月 周)。"""
    if not expr or not isinstance(expr, str):
        return False
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    try:
        if dt.minute not in _parse_cron_field(minute, 0, 59):
            return False
        if dt.hour not in _parse_cron_field(hour, 0, 23):
            return False
        if dt.month not in _parse_cron_field(month, 1, 12):
            return False
        # cron 周: 0/7=周日 .. 6=周六; Python weekday(): 周一=0..周日=6
        cron_dow = (dt.weekday() + 1) % 7
        dom_set = _parse_cron_field(dom, 1, 31)
        dow_set = _parse_cron_field(dow, 0, 7)
        if 7 in dow_set:
            dow_set.add(0)
        dom_restricted = dom.strip() != "*"
        dow_restricted = dow.strip() != "*"
        if dom_restricted and dow_restricted:
            return dt.day in dom_set or cron_dow in dow_set
        if dom_restricted:
            return dt.day in dom_set
        if dow_restricted:
            return cron_dow in dow_set
        return True
    except (ValueError, TypeError):
        return False


# ==================== HTML → Markdown ====================

class _HtmlToMarkdown(html.parser.HTMLParser):
    """将常见 HTML 标签转换为 Markdown（标题/加粗/斜体/链接/图片/列表/引用/代码等）。"""

    _HEADINGS = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self._href: Optional[str] = None
        self._list_stack: List[str] = []
        self._in_pre = False

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag in self._HEADINGS:
            self.out.append("\n" + self._HEADINGS[tag])
        elif tag in ("b", "strong"):
            self.out.append("**")
        elif tag in ("i", "em"):
            self.out.append("*")
        elif tag in ("del", "s", "strike"):
            self.out.append("~~")
        elif tag == "a":
            self._href = attrs_d.get("href") or ""
            self.out.append("[")
        elif tag == "img":
            src = attrs_d.get("src") or ""
            alt = attrs_d.get("alt") or ""
            if src:
                self.out.append(f"![{alt}]({src})")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "p":
            self.out.append("\n\n")
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            self.out.append("\n")
        elif tag == "li":
            marker = "1. " if self._list_stack and self._list_stack[-1] == "ol" else "- "
            self.out.append("\n" + marker)
        elif tag == "blockquote":
            self.out.append("\n> ")
        elif tag == "code" and not self._in_pre:
            self.out.append("`")
        elif tag == "pre":
            self._in_pre = True
            self.out.append("\n```\n")
        elif tag == "hr":
            self.out.append("\n---\n")

    def handle_endtag(self, tag):
        if tag in self._HEADINGS or tag == "p":
            self.out.append("\n")
        elif tag in ("b", "strong"):
            self.out.append("**")
        elif tag in ("i", "em"):
            self.out.append("*")
        elif tag in ("del", "s", "strike"):
            self.out.append("~~")
        elif tag == "a":
            self.out.append(f"]({self._href or ''})")
            self._href = None
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self.out.append("\n")
        elif tag == "code" and not self._in_pre:
            self.out.append("`")
        elif tag == "pre":
            self._in_pre = False
            self.out.append("\n```\n")

    def handle_data(self, data):
        if self._in_pre:
            self.out.append(data)
        else:
            self.out.append(re.sub(r"[ \t]*\n[ \t]*", " ", data))

    def result(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html_text: str) -> str:
    parser = _HtmlToMarkdown()
    parser.feed(html_text or "")
    parser.close()
    return parser.result()


# ==================== 计划任务存储 ====================

def load_schedules() -> List[Dict[str, Any]]:
    try:
        if SCHEDULES_FILE.exists():
            data = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def save_schedules(schedules: List[Dict[str, Any]]) -> bool:
    try:
        SCHEDULES_FILE.write_text(json.dumps(schedules, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def load_history() -> List[Dict[str, Any]]:
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def record_history(entry: Dict[str, Any]) -> None:
    """记录一条发帖/定时发帖的编辑历史（最新在前，同内容去重，最多保留 HISTORY_MAX 条）。"""
    try:
        item = {
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "kind": str(entry.get("kind") or "publish"),
            "user": str(entry.get("user") or ""),
            "guild_id": str(entry.get("guild_id") or ""),
            "channel_id": str(entry.get("channel_id") or ""),
            "title": str(entry.get("title") or ""),
            "format": str(entry.get("format") or "text"),
            "content": str(entry.get("content") or ""),
            "images": [str(x) for x in (entry.get("images") or [])],
            "videos": [str(x) for x in (entry.get("videos") or [])],
        }
        history = load_history()
        key = (item["guild_id"], item["channel_id"], item["title"], item["content"])
        history = [h for h in history if (str(h.get("guild_id") or ""), str(h.get("channel_id") or ""), str(h.get("title") or ""), str(h.get("content") or "")) != key]
        history.insert(0, item)
        HISTORY_FILE.write_text(json.dumps(history[:HISTORY_MAX], ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def normalize_schedule(body: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化一条计划任务，返回 {'error': ...} 或规范化后的任务。"""
    cron = str(body.get("cron") or "").strip()
    if not cron_valid(cron):
        return {"error": "Cron 表达式无效，需为 5 段：分 时 日 月 周（支持 * , - /）"}
    guild_id = str(body.get("guild_id") or "").strip()
    channel_id = str(body.get("channel_id") or "").strip()
    if not guild_id or not channel_id:
        return {"error": "必须填写频道ID (guild_id) 和版块ID (channel_id)"}
    content = str(body.get("content") or "").strip()
    if not content:
        return {"error": "内容不能为空"}
    fmt = str(body.get("format") or "text").strip().lower()
    if fmt not in ("text", "md", "html"):
        return {"error": "格式必须是 text / md / html"}

    def _str_list(key: str) -> List[str]:
        raw = body.get(key) or []
        if isinstance(raw, str):
            raw = re.split(r"[\n,]+", raw)
        return [str(x).strip() for x in raw if str(x or "").strip()]

    return {
        "id": str(body.get("id") or "").strip() or uuid.uuid4().hex[:12],
        "name": str(body.get("name") or "").strip() or "未命名计划",
        "user": str(body.get("user") or "").strip(),
        "cron": cron,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "format": fmt,
        "title": str(body.get("title") or "").strip(),
        "content": content,
        "images": _str_list("images"),
        "videos": _str_list("videos"),
        "enabled": bool(body.get("enabled", True)),
        "last_run": body.get("last_run") or "",
        "last_result": body.get("last_result") or "",
    }


# ==================== 发帖执行 ====================

def build_publish_args(schedule: Dict[str, Any]) -> List[str]:
    args = [
        "feed", "publish-feed",
        "--guild-id", str(schedule["guild_id"]),
        "--channel-id", str(schedule["channel_id"]),
    ]
    title = str(schedule.get("title") or "").strip()
    if title:
        args += ["--title", title]
    fmt = schedule.get("format") or "text"
    content = str(schedule.get("content") or "")
    if fmt == "md":
        args += ["--markdown-content", content]
    elif fmt == "html":
        args += ["--markdown-content", html_to_markdown(content)]
    else:
        args += ["--content", content]
    for image in schedule.get("images") or []:
        args += ["--image", image]
    for video in schedule.get("videos") or []:
        args += ["--video", video]
    args += ["--json"]
    return args


def run_schedule_sync(schedule: Dict[str, Any]) -> Dict[str, Any]:
    """执行一条计划任务（同步，在线程池中调用），用计划指定的账号槽位发帖。"""
    ok, output = _run_cli(build_publish_args(schedule), user=str(schedule.get("user") or "").strip() or None)
    output = _normalize_rate_limit(output)
    data = _extract_json(output)
    message = ""
    if isinstance(data, dict):
        message = str(data.get("message") or data.get("msg") or "")
        share_url = None
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        if isinstance(payload, dict):
            share_url = payload.get("share_url") or payload.get("shareUrl")
        if share_url:
            message = (message + " " + str(share_url)).strip()
    if not message:
        message = output.strip()[:200]
    return {"ok": ok, "message": message}


async def run_schedule(schedule: Dict[str, Any]) -> Dict[str, Any]:
    result = await asyncio.to_thread(run_schedule_sync, schedule)
    schedules = load_schedules()
    for item in schedules:
        if item.get("id") == schedule.get("id"):
            item["last_run"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            item["last_result"] = ("成功" if result["ok"] else "失败") + f"：{result['message']}"[:300]
            break
    save_schedules(schedules)
    return result


async def _run_due_tasks(now: datetime.datetime):
    for schedule in load_schedules():
        if not schedule.get("enabled"):
            continue
        if cron_match(schedule.get("cron", ""), now):
            try:
                await run_schedule(schedule)
            except Exception:
                pass


async def _scheduler_loop():
    """对齐整分钟轮询, 每分钟检查一次到期的定时发帖任务。"""
    while True:
        now = datetime.datetime.now()
        sleep_secs = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(max(sleep_secs, 1))
        await _run_due_tasks(datetime.datetime.now())


_SCHEDULER_TASK_NAME = "txpd_feed_scheduler"


def _cancel_existing_schedulers():
    """取消残留的调度器任务, 防止热重载导致多个调度器并存而重复发帖。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for task in asyncio.all_tasks(loop):
        if task.get_name() == _SCHEDULER_TASK_NAME and not task.done():
            task.cancel()


@on_load
async def _start_scheduler():
    _cancel_existing_schedulers()
    asyncio.create_task(_scheduler_loop(), name=_SCHEDULER_TASK_NAME)


@on_unload
def _stop_scheduler():
    _cancel_existing_schedulers()
