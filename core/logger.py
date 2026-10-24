"""
Structured logging system for commercial-grade application.
Provides JSON formatted logs with structured metadata.
"""

import logging
import logging.config
import os
import sys
from typing import Optional, Dict, Any
from datetime import datetime

try:
    from pythonjsonlogger import jsonformatter
    _HAS_JSON_LOGGER = True
except ImportError:
    jsonformatter = None
    _HAS_JSON_LOGGER = False


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
    console: Optional[bool] = None,
) -> None:
    """
    Configure structured logging with rotation.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path for log output (RotatingFileHandler)
        max_bytes: Maximum log file size before rotation
        backup_count: Number of backup files to keep
        console: Force enable/disable stdout handler. If None, auto-detect:
                 disable when stdout is redirected (daemon/launchd) to avoid
                 unbounded daemon.out growth.
    """
    log_format = (
        '%(asctime)s|%(name)s|%(levelname)s|%(filename)s|'
        '%(funcName)s|%(lineno)d|%(message)s'
    )
    text_formatter = logging.Formatter(log_format)

    if _HAS_JSON_LOGGER:
        json_formatter = jsonformatter.JsonFormatter(
            '%(asctime)s %(levelname)s %(name)s %(message)s'
        )
    else:
        json_formatter = text_formatter

    if console is None:
        env_flag = os.getenv("FB_LOG_CONSOLE")
        if env_flag is not None:
            console = env_flag not in ("0", "false", "False", "")
        else:
            console = sys.stdout.isatty()

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    root.setLevel(level)

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(text_formatter)
        console_handler.setLevel(level)
        root.addHandler(console_handler)

    if log_file:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8',
            delay=True,
        )
        file_handler.setFormatter(json_formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)

    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('openpyxl').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)


def get_logger(name: Optional[str] = None):
    """
    Get a logger instance with automatic naming.

    Args:
        name: Optional logger name, defaults to caller's module

    Returns:
        Configured logger instance
    """
    if name is None:
        # Auto-detect calling module
        import inspect
        frame = inspect.stack()[1]
        module_name = frame.module
        name = f"{module_name}.{frame.function}"

    return logging.getLogger(name)