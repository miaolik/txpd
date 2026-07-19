#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""环境自检：Linux/macOS 下自动补全 tencent-channel-cli（npm 全局安装）。

Windows 使用插件目录内置 tencent-channel-cli.exe，无需处理。
"""

import asyncio
import shutil
import sys
from pathlib import Path

from core.plugin.decorators import on_load

BASE_DIR = Path(__file__).resolve().parent

CLI_NAME = "tencent-channel-cli"
NPM_INSTALL_TIMEOUT = 600


def _cli_available() -> bool:
    for name in (f"{CLI_NAME}.exe", f"{CLI_NAME}.cmd", CLI_NAME):
        if (BASE_DIR / name).exists():
            return True
    for name in (CLI_NAME, f"{CLI_NAME}.cmd"):
        found = shutil.which(name)
        if found and not found.lower().endswith(".ps1"):
            return True
    return False


async def _npm_install_cli() -> None:
    npm = shutil.which("npm")
    if not npm:
        print(f"[txpd] 未找到 npm，无法自动安装 {CLI_NAME}，请先安装 Node.js/npm")
        return
    print(f"[txpd] 检测到 {CLI_NAME} 缺失，正在执行 npm install -g {CLI_NAME} ...")
    try:
        proc = await asyncio.create_subprocess_exec(
            npm, "install", "-g", CLI_NAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=NPM_INSTALL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            print(f"[txpd] npm install -g {CLI_NAME} 超时（{NPM_INSTALL_TIMEOUT}s）")
            return
        if proc.returncode == 0 and _cli_available():
            print(f"[txpd] {CLI_NAME} 安装成功")
        else:
            tail = (out or b"").decode("utf-8", "ignore").strip().splitlines()[-5:]
            print(f"[txpd] {CLI_NAME} 安装失败 (exit={proc.returncode}): " + " | ".join(tail))
    except Exception as e:
        print(f"[txpd] 自动安装 {CLI_NAME} 出错: {e}")


@on_load
async def ensure_cli_env():
    """插件加载时检测系统环境：非 Windows 且缺少 CLI 时后台自动补全。"""
    if sys.platform.startswith("win"):
        return
    if _cli_available():
        return
    asyncio.create_task(_npm_install_cli())
