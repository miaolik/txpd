#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日志清理模块：定时清理插件产生的日志文件。

每天定时清理以下日志文件：
1. users/<用户名>/.qqcli/subscription/daemon.log
2. users/<用户名>/.qqcli/logs/tencent-channel-cli.log
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List

from core.base.logger import PLUGIN, get_logger

logger = get_logger(PLUGIN, "日志清理")

BASE_DIR = Path(__file__).resolve().parent
USERS_DIR = BASE_DIR / "users"

# 日志文件相对路径列表
LOG_PATHS = [
    ".qqcli/subscription/daemon.log",
    ".qqcli/logs/tencent-channel-cli.log",
]

# 清理时间：每天凌晨 3 点（格式：HH:MM）
CLEANUP_TIME = "03:00"


def get_all_user_dirs() -> List[Path]:
    """获取所有用户目录。"""
    if not USERS_DIR.exists():
        return []
    return [d for d in USERS_DIR.iterdir() if d.is_dir()]


def cleanup_log_file(log_path: Path) -> bool:
    """清理单个日志文件。
    
    Returns:
        True: 文件已清理或不存在
        False: 清理失败
    """
    if not log_path.exists():
        return True
    
    try:
        # 获取文件大小（用于日志）
        size = log_path.stat().st_size
        
        # 清空文件内容（保留文件）
        log_path.write_text("", encoding="utf-8")
        
        logger.info(f"已清理日志文件: {log_path} (原大小: {size / 1024:.1f} KB)")
        return True
    except Exception as exc:
        logger.warning(f"清理日志文件失败: {log_path} - {exc}")
        return False


def cleanup_all_logs() -> None:
    """清理所有用户的日志文件。"""
    logger.info("开始定时日志清理任务")
    
    user_dirs = get_all_user_dirs()
    if not user_dirs:
        logger.info("未找到用户目录，跳过清理")
        return
    
    total_cleaned = 0
    total_failed = 0
    
    for user_dir in user_dirs:
        user_name = user_dir.name
        logger.debug(f"检查用户 {user_name} 的日志文件")
        
        for rel_path in LOG_PATHS:
            log_file = user_dir / rel_path
            if cleanup_log_file(log_file):
                total_cleaned += 1
            else:
                total_failed += 1
    
    logger.info(f"日志清理任务完成: 成功 {total_cleaned} 个, 失败 {total_failed} 个")


async def log_cleanup_loop() -> None:
    """日志清理主循环：每天定时执行清理任务。"""
    logger.info(f"日志清理服务已启动，每天 {CLEANUP_TIME} 执行清理")
    
    last_cleanup_date = ""
    
    while True:
        try:
            # 等待到下一分钟的整点
            sleep_time = 60 - time.time() % 60
            await asyncio.sleep(sleep_time)
            
            # 获取当前时间
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            current_date = now.strftime("%Y-%m-%d")
            
            # 检查是否到达清理时间且今天尚未清理
            if current_time == CLEANUP_TIME and current_date != last_cleanup_date:
                cleanup_all_logs()
                last_cleanup_date = current_date
        
        except asyncio.CancelledError:
            logger.info("日志清理服务已停止")
            break
        except Exception as exc:
            logger.error(f"日志清理任务异常: {exc}")
            # 发生异常后等待 1 小时再重试
            await asyncio.sleep(3600)


# 用于存储清理任务
_cleanup_task: asyncio.Task = None


def start_log_cleanup() -> None:
    """启动日志清理服务。"""
    global _cleanup_task
    
    if _cleanup_task is not None and not _cleanup_task.done():
        logger.warning("日志清理服务已在运行")
        return
    
    _cleanup_task = asyncio.create_task(log_cleanup_loop())
    logger.info("日志清理服务已创建")


def stop_log_cleanup() -> None:
    """停止日志清理服务。"""
    global _cleanup_task
    
    if _cleanup_task is not None and not _cleanup_task.done():
        _cleanup_task.cancel()
        logger.info("日志清理服务停止请求已发送")
