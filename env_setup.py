#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""环境自检：Linux/macOS 下自动补全 tencent-channel-cli。

查找顺序：插件目录内置二进制（如 tencent-channel-cli-linux-x64）→ 插件目录
本地 npm 安装（.cli）→ PATH。缺失时优先 npm 安装到插件目录（无需 root），
失败再尝试 npm install -g。Windows 使用插件目录内置 tencent-channel-cli.exe，
无需处理。
"""

import asyncio
import shutil
import sys

from core.plugin.decorators import on_load

from .腾讯频道 import BASE_DIR, _resolve_cli

CLI_NAME = "tencent-channel-cli"
LOCAL_PREFIX = BASE_DIR / ".cli"
NPM_INSTALL_TIMEOUT = 600


async def _run_npm(npm: str, args: list) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        npm, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=NPM_INSTALL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return None, b""
    return proc.returncode, out or b""


async def _npm_install_cli() -> None:
    npm = shutil.which("npm")
    if not npm:
        print(
            f"[txpd] 未找到 npm，无法自动安装 {CLI_NAME}；"
            f"请安装 Node.js/npm，或将 {CLI_NAME}-linux-x64 二进制放入插件目录 {BASE_DIR}"
        )
        return
    print(f"[txpd] 检测到 {CLI_NAME} 缺失，正在安装到插件目录 {LOCAL_PREFIX} ...")
    attempts = [
        ["install", "--prefix", str(LOCAL_PREFIX), CLI_NAME],
        ["install", "-g", CLI_NAME],
    ]
    for args in attempts:
        try:
            code, out = await _run_npm(npm, args)
        except Exception as e:
            print(f"[txpd] 执行 npm {' '.join(args)} 出错: {e}")
            continue
        if code is None:
            print(f"[txpd] npm {' '.join(args)} 超时（{NPM_INSTALL_TIMEOUT}s）")
            continue
        if code == 0 and _resolve_cli():
            print(f"[txpd] {CLI_NAME} 安装成功: {_resolve_cli()}")
            return
        tail = out.decode("utf-8", "ignore").strip().splitlines()[-5:]
        print(f"[txpd] npm {' '.join(args)} 失败 (exit={code}): " + " | ".join(tail))
    print(
        f"[txpd] {CLI_NAME} 自动安装失败；可手动执行 npm install -g {CLI_NAME}，"
        f"或将 {CLI_NAME}-linux-x64 二进制放入插件目录 {BASE_DIR}"
    )


@on_load
async def ensure_cli_env():
    """插件加载时检测系统环境：非 Windows 且缺少 CLI 时后台自动补全。"""
    if sys.platform.startswith("win"):
        return
    if _resolve_cli():
        return
    asyncio.create_task(_npm_install_cli())
