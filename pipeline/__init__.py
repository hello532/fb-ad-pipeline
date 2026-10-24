"""FB Ad Library 扫描流水线 V9.0 — 稳定版.

V9.0 变更要点:
- 修复 daemon/API 关键异常 (NameError, UnicodeEncodeError-surrogate, JSON 写入)
- 全链路空值 → 默认占位 (—/未知/暂无/0%) — 前端不再出现 None/undefined
- Excel 输出精准化: 百分比/金额/日期统一格式, 新增"数据完整度"列
- scan_result 新增 data_quality_pct (0-100), metadata.scan_version=9.0
- FastAPI 统一错误响应 + /version 元数据端点 + 输出 null 清洗
"""
__version__ = "9.0.0"
VERSION = __version__           # 向后兼容别名
PIPELINE_VERSION = __version__
RELEASE_NAME = "V9.0 稳定版"
