"""Core module — import submodules lazily to avoid cascading failures when optional deps are missing."""

__all__ = [
    'setup_logging',
    'get_logger',
]

from .logger import setup_logging, get_logger
