#!/bin/bash
# 直接 Python 定时直跑：不经过 127.0.0.1:8000。
# 仅 V3 趋势三清单去重刷新（Google Trends 产品 / AMZ123 新榜 / FB+Shopify 适配），
# 直接把扫描词 append 进 pipeline.db。
cd "${FBPIPE_HOME:-$HOME/.local/share/fbpipe}" || exit 1
exec "${PYTHON:-python3}" fb-scan-pipeline-v8.py \
  --v3-trend-lists --v3-trend-append-scan 2>&1
