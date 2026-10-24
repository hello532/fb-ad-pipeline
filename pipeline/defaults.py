"""defaults.py — V9.0 统一空值处理与显示格式化.

目标: 任何流向 Excel / API / 日志的值都不会是 None / '' / [] / {},
而是显式的"暂无"/"未知"/"-"/"0%"等占位, 避免前端渲染 "null" / "undefined".
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# 统一占位符
DASH = "—"            # 无值/不适用
UNKNOWN = "未知"      # 已尝试提取但失败
NONE_LABEL = "暂无"    # 集合为空 (无竞品/无广告等)
ZERO_PCT = "0%"
ZERO_MONEY = "$0"
_ILLEGAL_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(v: str) -> str:
    """Remove XML control chars rejected by Excel/openpyxl."""
    return _ILLEGAL_XML_CHARS_RE.sub("", v)


def is_empty(v: Any) -> bool:
    """None / '' / [] / {} / (空白字符串) 都视为空."""
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) == 0
    return False


def nn(v: Any, default: Any = DASH) -> Any:
    """Not-null: 空值返回 default, 否则原样."""
    return default if is_empty(v) else v


def safe_str(v: Any, default: str = DASH) -> str:
    """保证返回非空 str. 将 None/空容器转为 default, 其它 str() 之."""
    if is_empty(v):
        return default
    if isinstance(v, str):
        s = clean_text(v).strip()
        return s if s else default
    s = clean_text(str(v)).strip()
    return s if s else default


def safe_int(v: Any, default: int = 0) -> int:
    """容错整数: 能转就转, 否则 default."""
    if v is None or v == '':
        return default
    try:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip().replace(',', '').replace('$', '').replace('%', '')
        return int(float(s)) if s else default
    except (ValueError, TypeError):
        return default


def safe_float(v: Any, default: float = 0.0) -> float:
    if v is None or v == '':
        return default
    try:
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace(',', '').replace('$', '').replace('%', '')
        return float(s) if s else default
    except (ValueError, TypeError):
        return default


def safe_pct(v: Any, decimals: int = 0, default: str = ZERO_PCT) -> str:
    """百分比规范化. 输入可为:
       - 0.45 / 0.4578 / 45 / '45%' / '45.8' → '45%' / '45.8%'
       - None / '' / 负数 / 非数 → default
    """
    if is_empty(v):
        return default
    try:
        if isinstance(v, str):
            s = v.strip()
            if s.endswith('%'):
                num = float(s[:-1])
            else:
                num = float(s.replace(',', ''))
                if 0 <= num <= 1:
                    num *= 100
        else:
            num = float(v)
            if 0 <= num <= 1:
                num *= 100
        if num < 0:
            return default
        return f"{num:.{decimals}f}%"
    except (ValueError, TypeError):
        return default


def safe_money(v: Any, currency: str = 'USD', default: str = DASH) -> str:
    """金额/区间格式化. 接受:
       - dict {lower, upper, currency} → 'USD 1000-5000'
       - 数字 → 'USD 1000'
       - 字符串原样 (已格式化)
    """
    if is_empty(v):
        return default
    if isinstance(v, dict):
        cur = str(v.get('currency') or currency).strip() or currency
        lo = safe_int(v.get('lower'))
        hi = safe_int(v.get('upper'))
        if lo == 0 and hi == 0:
            return default
        if lo == hi:
            return f"{cur} {lo:,}"
        return f"{cur} {lo:,}-{hi:,}"
    if isinstance(v, (int, float)):
        n = safe_int(v)
        return f"{currency} {n:,}" if n else default
    s = safe_str(v, '').strip()
    return s if s else default


def safe_date(v: Any, default: str = DASH) -> str:
    """日期/时间字符串规范. 保留到秒, 去掉时区 T 符号以便读."""
    if is_empty(v):
        return default
    s = safe_str(v, default)
    if s == default:
        return default
    return s.replace('T', ' ')[:19]


def safe_list_join(v: Any, sep: str = ', ', limit: Optional[int] = None,
                   default: str = NONE_LABEL) -> str:
    """列表 → 拼接字符串. 空列表返回 default."""
    if is_empty(v):
        return default
    if isinstance(v, (list, tuple, set)):
        items = [safe_str(x, '').strip() for x in v]
        items = [x for x in items if x]
        if not items:
            return default
        if limit and limit > 0:
            items = items[:limit]
        return sep.join(items)
    s = safe_str(v, '').strip()
    return s if s else default


def safe_dict_keys_join(v: Any, sep: str = ', ', limit: Optional[int] = None,
                        default: str = NONE_LABEL) -> str:
    """字典的 keys 拼接 (常用于 platform_distribution → 'facebook, instagram')."""
    if is_empty(v):
        return default
    if isinstance(v, dict):
        return safe_list_join(list(v.keys()), sep=sep, limit=limit, default=default)
    return safe_list_join(v, sep=sep, limit=limit, default=default)


def safe_dict_kv_join(v: Any, kv_sep: str = ':', item_sep: str = ', ',
                      limit: Optional[int] = None, default: str = NONE_LABEL) -> str:
    """字典的 k:v 对拼接 (用于 media_mix → 'image:5, video:3')."""
    if is_empty(v):
        return default
    if not isinstance(v, dict):
        return safe_str(v, default)
    parts = [f"{safe_str(k, '-')}{kv_sep}{safe_str(val, '-')}" for k, val in v.items()]
    parts = [p for p in parts if p and not p.endswith(kv_sep + '-')]
    if not parts:
        return default
    if limit and limit > 0:
        parts = parts[:limit]
    return item_sep.join(parts)


def completeness_score(fields: Iterable[Any]) -> int:
    """对一组字段计算完整度百分比 (0-100). 用于 Excel '数据完整度' 列."""
    items = list(fields)
    if not items:
        return 0
    filled = sum(1 for v in items if not is_empty(v))
    return int(round(filled / len(items) * 100))


def scrub_output(obj: Any, default: Any = DASH) -> Any:
    """递归将 None → default (字符串字段). 保持数字 0 和 bool False 不变.
    对 dict / list 深度遍历. 用于 API 响应统一清洗.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return {k: scrub_output(v, default) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_output(x, default) for x in obj]
    if isinstance(obj, str):
        cleaned = clean_text(obj).strip()
        return cleaned if cleaned else default
    return obj
