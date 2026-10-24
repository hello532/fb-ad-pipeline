"""pipeline/shards.py — 按月分片 + 跨分片透明查询.

策略:
- pipeline.db 始终是"当前月"写库, 用 meta 表里的 current_month 记录它属于哪个月
- 进程首次打开连接时, 如果 current_month != 今月, 把 pipeline.db 重命名到
  shards/pipeline-YYYY-MM.db, 并新建空库
- 读连接 ATTACH 所有分片, 建临时 view (v_brands / v_brand_keywords /
  v_brand_page_names / v_learned_keywords / v_scans) 作 UNION ALL, 查询走 view
- SQLite 默认 ATTACH 上限 10; 超出抛明确错误, 让使用者归档/合并老分片
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core.logger import get_logger

from .paths import NEW_DIR

log = get_logger(__name__)

SHARDS_DIR = Path(os.getenv("FB_SHARDS_DIR", NEW_DIR / "shards"))
SHARDS_DIR.mkdir(exist_ok=True)

SHARDED_TABLES = [
    "brands", "brand_keywords", "brand_page_names",
    "learned_keywords", "scans", "ad_archive",
]

META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_rotation_lock = threading.Lock()
_rotation_done = False


def _current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def _shard_name(month: str) -> str:
    return f"pipeline-{month}.db"


def list_shards() -> List[Path]:
    """按月份升序列出所有归档分片."""
    return sorted(SHARDS_DIR.glob("pipeline-*.db"))


def ensure_rotation(db_file: Path, schema_sql: str) -> bool:
    """进程级幂等 rotation. 调用者需在 schema 应用后调用.

    返回 True 表示本次发生了 rotation.
    """
    global _rotation_done
    if _rotation_done:
        return False
    with _rotation_lock:
        if _rotation_done:
            return False
        _rotation_done = True
        return _do_rotation(db_file, schema_sql)


def _do_rotation(db_file: Path, schema_sql: str) -> bool:
    """执行轮转. 读 meta.current_month, 如果 != 今月则归档."""
    now = _current_month()

    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.executescript(META_TABLE_SQL)
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'current_month'"
        ).fetchone()
        stored = row[0] if row else None

        if stored is None:
            # 首次运行: 看库里有没有实际数据来决定 stored 的月份
            has_data = False
            for t in SHARDED_TABLES:
                try:
                    n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    if n:
                        has_data = True
                        break
                except sqlite3.OperationalError:
                    pass
            stored = now if not has_data else _guess_month_from_data(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('current_month', ?)",
                (stored,),
            )
            conn.commit()

        if stored == now:
            return False

        # 需要 rotate
        archive_path = SHARDS_DIR / _shard_name(stored)
        log.info(f"shards: rotating {db_file.name} ({stored}) -> {archive_path.name}, starting new {now}")
    finally:
        conn.close()

    # 关闭后才能 rename
    if archive_path.exists():
        log.info(f"shards: archive {archive_path.name} already exists; skipping rotation to avoid overwrite")
        return False
    shutil.move(str(db_file), str(archive_path))

    # 新库: 创建空 schema + meta
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.executescript(schema_sql)
        conn.executescript(META_TABLE_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('current_month', ?)",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def _guess_month_from_data(conn: sqlite3.Connection) -> str:
    """meta 缺失时, 从 scans.scan_date 或 brands.last_seen 推断当前月份."""
    for sql in (
        "SELECT MAX(scan_date) FROM scans",
        "SELECT MAX(last_seen) FROM brands",
        "SELECT MAX(learned_date) FROM learned_keywords",
    ):
        try:
            row = conn.execute(sql).fetchone()
            if row and row[0]:
                # ISO date -> YYYY-MM
                return str(row[0])[:7]
        except sqlite3.OperationalError:
            continue
    return _current_month()


def attach_and_build_views(conn: sqlite3.Connection, *,
                           months: Optional[List[str]] = None) -> int:
    """为只读查询准备跨分片 view. 返回 attach 的分片数.

    months: 如果提供, 只 ATTACH 匹配的月份分片 (格式 'YYYY-MM'),
            减少 UNION ALL 节点数, 加速查询. None = 全部.
    """
    shards = list_shards()
    if months:
        filtered = []
        for s in shards:
            # shard name: pipeline-YYYY-MM.db → extract YYYY-MM
            stem = s.stem  # pipeline-2026-04
            shard_month = stem.replace("pipeline-", "", 1)
            if shard_month in months:
                filtered.append(s)
        shards = filtered

    if len(shards) > 8:
        log.info(
            f"shards: {len(shards)} 个历史分片, 接近 SQLite ATTACH 上限 10, "
            f"建议尽快归档合并最老的几个"
        )
    if len(shards) >= 10:
        raise RuntimeError(
            f"历史分片 {len(shards)} 个 >= SQLite ATTACH 上限. "
            f"请合并 {SHARDS_DIR} 下最老的几个到 archive.db 再重试."
        )

    alias_names = ["main"]
    for shard in shards:
        alias = "s_" + shard.stem.replace("-", "_")   # s_pipeline_2026_04
        try:
            conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(shard),))
            alias_names.append(alias)
        except sqlite3.OperationalError as e:
            log.info(f"shards: attach {shard.name} 失败: {e}")

    for tbl in SHARDED_TABLES:
        parts = [f"SELECT * FROM {a}.{tbl}" for a in alias_names]
        union = " UNION ALL ".join(parts)
        conn.execute(f"DROP VIEW IF EXISTS v_{tbl}")
        conn.execute(f"CREATE TEMP VIEW v_{tbl} AS {union}")

    return len(shards)


def months_for_days(days: int) -> List[str]:
    """返回过去 N 天覆盖的月份列表, 用于选择性 ATTACH."""
    if days <= 0:
        return []
    from datetime import timedelta
    months = set()
    now = datetime.now()
    for d in range(0, days, 28):
        m = (now - timedelta(days=d)).strftime("%Y-%m")
        months.add(m)
    return sorted(months)


def consolidate_shards(archive_name: str = "archive-pre-2026",
                        keep_months: int = 3) -> int:
    """合并老的分片到单个 archive.db, 减少 ATTACH 数量.

    keep_months: 保留最近 N 个月的分片不合并.
    返回: 合并掉的分片数.
    """
    threshold = _current_month()
    # 计算保留截止月份
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=keep_months * 31)).strftime("%Y-%m")

    shards = list_shards()
    to_merge = []
    to_keep = []
    for s in shards:
        shard_month = s.stem.replace("pipeline-", "", 1)
        if shard_month < cutoff:
            to_merge.append(s)
        else:
            to_keep.append(s)

    if not to_merge:
        return 0

    archive_path = SHARDS_DIR / f"{archive_name}.db"
    log.info(f"shards: consolidating {len(to_merge)} old shards into {archive_path.name}")

    # 如果 archive 已存在, ATTACH 并 INSERT
    if archive_path.exists():
        conn = sqlite3.connect(str(archive_path))
        conn.execute("PRAGMA busy_timeout=5000")
        for s in to_merge:
            alias = "s_" + s.stem.replace("-", "_")
            try:
                conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(s),))
                for tbl in SHARDED_TABLES:
                    conn.execute(f"INSERT OR IGNORE INTO {tbl} SELECT * FROM {alias}.{tbl}")
                conn.execute(f"DETACH DATABASE {alias}")
            except Exception as e:
                log.warning(f"shards: merge {s.name} → archive failed: {e}")
        conn.commit()
        conn.close()
    else:
        # 创建 archive 并复制第一个分片的结构
        import shutil
        shutil.copy2(str(to_merge[0]), str(archive_path))
        conn = sqlite3.connect(str(archive_path))
        conn.execute("PRAGMA busy_timeout=5000")
        for s in to_merge[1:]:
            alias = "s_" + s.stem.replace("-", "_")
            try:
                conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(s),))
                for tbl in SHARDED_TABLES:
                    conn.execute(f"INSERT OR IGNORE INTO {tbl} SELECT * FROM {alias}.{tbl}")
                conn.execute(f"DETACH DATABASE {alias}")
            except Exception as e:
                log.warning(f"shards: merge {s.name} → archive failed: {e}")
        conn.commit()
        conn.close()

    # 删除已合并的旧分片
    for s in to_merge:
        s.unlink()
        log.info(f"shards: removed {s.name} (consolidated)")

    return len(to_merge)
