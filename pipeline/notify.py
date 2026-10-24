"""pipeline/notify.py — 统一通知出口.

- 日志: 高亮"分隔横线 + 标题 + 内容"块, 在 pipeline.log 里一眼看到
- 桌面: macOS osascript 弹通知 (非 mac 或 osascript 缺失自动降级为空操作)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

from core.logger import get_logger

log = get_logger(__name__)

_OSASCRIPT = shutil.which("osascript") if sys.platform == "darwin" else None

# 桌面提醒总开关: 默认关闭(2026-07-10 用户要求停止 8000 服务所有提醒).
# 需要恢复时设环境变量 FB_NOTIFY_DESKTOP=1 即可.
_DESKTOP_ENABLED = os.environ.get("FB_NOTIFY_DESKTOP", "0") == "1"


def banner(title: str, *lines: str, highlight: bool = False) -> None:
    """在 pipeline.log 里打一段显眼的横线分隔块."""
    mark = "█" if highlight else "═"
    bar = mark * 60
    log.info("")
    log.info(bar)
    log.info(f"  {title}")
    for ln in lines:
        log.info(f"  {ln}")
    log.info(bar)


def desktop(title: str, body: str, sound: bool = True) -> None:
    """macOS 桌面通知. 非 mac 静默跳过."""
    if not _DESKTOP_ENABLED:
        return
    if not _OSASCRIPT:
        return
    # 转义 " 防止 AppleScript 注入
    esc_title = title.replace('"', '\\"')
    esc_body = body.replace('"', '\\"')
    snd = ' sound name "Glass"' if sound else ""
    script = f'display notification "{esc_body}" with title "{esc_title}"{snd}'
    try:
        subprocess.run([_OSASCRIPT, "-e", script], check=False,
                       capture_output=True, timeout=5)
    except Exception:
        pass


def both(title: str, summary: str, *extra_lines: str, sound: bool = True,
         highlight: bool = False) -> None:
    """同时发日志 banner + 桌面通知."""
    banner(title, summary, *extra_lines, highlight=highlight)
    desktop(title, summary, sound=sound)
