# FB Pipeline 无人值守部署指南 (macOS launchd)

## 架构

```
┌─────────────────────────┐        ┌─────────────────────────┐
│ fbpipeline.daemon       │        │ fbpipeline.api          │
│ (launchd, KeepAlive)    │        │ (launchd, KeepAlive)    │
│                         │        │                         │
│ 循环:                   │ 写 DB  │ FastAPI @ 127.0.0.1:8000│
│  1. scrape 5 个关键词   │───────▶│  /brands /keywords /... │
│  2. process-raw         │        │                         │
│  3. update-excel        │        │                         │
│  4. update-intel        │        │                         │
│  5. rabbit-hole         │        │                         │
│  6. sleep 60 分钟       │        │                         │
└─────────────────────────┘        └─────────────────────────┘
       ▲
       │ 读 scan_queue + 浏览器 session
       │
┌─────────────────────────┐
│ .browser_profile/       │ ← 首次用 --login 手动登录
│ (cookies 持久化)        │
└─────────────────────────┘
```

## 一次性准备

```bash
cd $HOME/.claude/skills/NEW

# 1. 装依赖
pip install --break-system-packages -r requirements.txt playwright
python3 -m playwright install chromium

# 2. 首次登录 Facebook (headful, 浏览器会弹出)
python3 fb-scan-pipeline-v8.py --login
# >>> 在弹出的浏览器里登录, 完成后关闭浏览器

# 3. 测试采集 1 个关键词 (headful 观察)
python3 fb-scan-pipeline-v8.py --auto-scrape --scrape-limit 1 --scrape-headful
# 确认能正常保存 fb-ad-library-results/*.json

# 4. 测试 headless 模式
python3 fb-scan-pipeline-v8.py --auto-scrape --scrape-limit 1
```

## 部署 launchd

```bash
cp launchd/com.doi.fbpipeline.daemon.plist ~/Library/LaunchAgents/
cp launchd/com.doi.fbpipeline.api.plist ~/Library/LaunchAgents/

# 加载并启动
launchctl load ~/Library/LaunchAgents/com.doi.fbpipeline.daemon.plist
launchctl load ~/Library/LaunchAgents/com.doi.fbpipeline.api.plist

# 检查状态
launchctl list | grep fbpipeline
```

## 监控

```bash
tail -f logs/pipeline.log       # 业务日志
tail -f logs/daemon.err         # daemon stderr
tail -f logs/api.err            # api stderr

curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/scans?limit=5
```

## 停止 / 重启

```bash
launchctl unload ~/Library/LaunchAgents/com.doi.fbpipeline.daemon.plist
launchctl unload ~/Library/LaunchAgents/com.doi.fbpipeline.api.plist

# 改配置后重载:
launchctl unload ~/Library/LaunchAgents/com.doi.fbpipeline.daemon.plist
launchctl load ~/Library/LaunchAgents/com.doi.fbpipeline.daemon.plist
```

## 注意事项

1. **session 过期**: 定期检查 `logs/pipeline.log` 是否出现 "未登录或 session 过期"; 出现后再跑一次 `--login` 即可.
2. **机器必须开机**: launchd LaunchAgent 只在用户登录时运行. 要熄屏待机也运行需设置"节能-防止电脑进入睡眠".
3. **反爬风险**: headless 连续抓会被 FB 标记. 默认配置 `cycle-minutes=30` + 每轮 4 个关键词 + 基础 2-5 分钟 + 12% 概率 10-30 分钟长休, 理论吞吐 ~120-150/天. 如果被风控, 加大 `FB_DELAY_MAX_SEC` / `FB_LONG_BREAK_PROB` 或切换代理.
4. **调参不改代码**: plist 的 `EnvironmentVariables` 区块暴露了所有时序参数 (`FB_DAILY_QUOTA`/`FB_DELAY_*`/`FB_LONG_BREAK_*`/`FB_QUIET_*`), 改完 `launchctl unload/load` 即生效.
4. **无队列时跳过**: 如果 scan_queue 空了, daemon 会跳过抓取阶段只休眠. 用 `--rabbit-hole` 补充队列, 或手动编辑 `facebook-ad-keywords-scan.txt`.
