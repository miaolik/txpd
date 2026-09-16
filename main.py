#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""腾讯频道插件入口：指令 / Web 管理面板 / 定时发帖调度器 / 日志清理。"""

from . import env_setup  # noqa: F401
from . import 腾讯频道  # noqa: F401
from . import feed_scheduler  # noqa: F401
from . import 评论通知  # noqa: F401
from . import web_panel  # noqa: F401
from . import log_cleaner  # noqa: F401

# 启动日志清理服务
log_cleaner.start_log_cleanup()

__plugin_meta__ = {
    "name": "腾讯频道",
    "description": "腾讯频道管理：指令 + Web 管理面板 + 定时发帖（Cron） + 日志清理，仅限插件管理员使用",
    "version": "3.1.0",
    "author": "miaolik",
}
