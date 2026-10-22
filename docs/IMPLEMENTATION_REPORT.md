# IMPLEMENTATION_REPORT —— Aegis Token Monitor v0.2

实施日期：2026-09-12 · 实施：Claude Code（Opus 4.8）· 授权：少爷（"今天正式授权开始第二版施工"）

## 一、交付范围与结论

方案 A 全量交付：**PySide6 Widgets 桌面窗口 + 四 Provider 2×2 仪表盘 + 采集服务 + SQLite 额度历史 + 七日工作足迹 + 详情/历史页**。实际 Token 统计经数据源闸门调查后**诚实判为 NOT AVAILABLE YET**（D-009）。

```
V1 归档：releases/Aegis-Token-Monitor-v0.1.0-20260912-1458.zip（48 文件，
         逐文件 SHA256 == git tag v0.1.0 树；SHA256 f1ce5add…0a6c59）
V1 改动：无（git diff HEAD 为空；未执行任何 reset/clean）
V2 代码：独立目录 v0.2/，独立 git 仓库（首个提交 b033b4f，75 文件）
```

## 二、三个批次执行记录

### Batch 1：归档 + 隔离 + 数据通道 + 历史基础 + Token 调查
- v0.1 归档（白名单 = tag v0.1.0 树，见 `releases/ARCHIVE_MANIFEST.md` 与差异记录）。
- v0.2 隔离：源码/测试复制核对；独立 `.venv`（Python 3.12.10 + PySide6 6.9.1 + pytest 8.4.2，锁定于 `requirements-ui.txt`）；独立 `data/ cache/ logs/ out/`。
- 服务层：`services/contracts.py`（三维状态 + 时间信封 + 安全错误映射）、`services/validation.py`（缓存严格验证）、`services/monitor.py`（单写者协调）、`services/proclife.py`（Job Object）。
- 历史层：`history/store.py`（SQLite + backup + 保留策略）、`history/activity.py`（保守足迹规则）。
- Token 调查：实时 CC Switch 库只读白名单查询 → 闸门 2/6 不通过 → `docs/TOKEN_SOURCE_CAPABILITY.md`。

### Batch 2：完整方案 A
- `desktop/theme.py`（DESIGN.md 常量唯一来源）、`adapter.py`（纯函数 VM）、`widgets.py`（ProviderCard/BalanceCard/StatusChip/RemainingBar/FootprintStrip/RefreshButton）、`pages.py`（详情/历史/QPainter 折线）、`app.py`（主窗口/导航/历史查询 worker）。
- `run_gui.py` + `run_gui.bat`（独立 venv 启动）+ `run.bat`（CLI，独立于 v0.1）。
- 实际 Token 功能：按闸门结果不启用，UI 显示"尚未接入"。

### Batch 3：集成验收与修整
- 新增测试 42 个（monitor/history/adapter/security/integration）；离线 **380 passed**；live **3 passed**。
- 真机端到端：服务层 live 4/4 MATCH 原始源；历史入账 4/4。
- 安全：哨兵注入测试 + live 全链 NO_SECRET_LEAK PASS。
- 关闭回收：0.28s，无 CodexBar 残留；迟到结果丢弃（stop 守卫）。
- 故障注入：锁库/只读/损坏库/历史线程故障/None reader/异常 reader —— 实时卡片全部不受影响。
- DPI：100%/125%/150% 截图（`docs/screenshots/`）；最小尺寸卡片 420→350 实测。
- 视觉自查修复 2 项：卡片固定宽→可缩（防最小窗口溢出）、"历史›"边界 divider→control-border。
- 浸泡：10 分钟真实源 600s，RSS 27.0→32.3MB（+5.4MB，目标 ≤30MB 增长，无持续增长趋势），8 条观测（与 300s TTL 一致）。
- 修复的实测缺陷：sqlite 跨线程连接（check_same_thread）、Job Object 句柄 ctypes 截断、availability 判定未反映最近尝试结果、失败后 0.5s 重试风暴、activity 缺 DayMark 导入、`_render_cards` 残留代码。

## 三、对设计文档的偏离（全部有记录）

| 偏离 | 理由 | 证据/记录 |
| --- | --- | --- |
| multiprocessing 采集 → 线程池 + Job Object | reader 已有硬超时；进程边界唯一实际风险（CodexBar 孙进程残留）由 Job 直接解决且更简单 | D-008；`test_security_v02.py` 集成用例；真机 0.28s 关闭无残留；ARCHITECTURE.md 已同步附注 |
| Token 统计不接入 | 闸门 2/6 不通过（semantics 无权威口径、provider 伪 ID） | D-009 + TOKEN_SOURCE_CAPABILITY.md |
| 缓存损坏条目拒绝而非 0/0 | v0.1 CC 低危发现 2 的 v0.2 修复 | D-010 + `test_security_v02.py` |
| extras 自由文本不进桌面 UI | v0.1 CC 低危发现 1 的 v0.2 修复（白名单 id/title/used%） | `adapter.py` / `pages.py` |

## 四、最终测试数字（本轮实测，非历史数据）

| 项 | 结果 |
| --- | --- |
| 离线测试 `pytest tests -m "not live" -q`（v0.2 venv） | **380 passed, 3 deselected** |
| 其中 v0.1 继承回归 | 338 passed（与 v0.1 全量 341 中 338 离线 + 3 live 对应） |
| v0.2 新增 | 42 个（monitor 8 / history 12 / adapter 11 / security 5 / integration 4 / 其余补充断言） |
| live 测试（真机单独跑） | 3 passed |
| 真实源交叉核对 | 4/4 MATCH（服务层数值 vs 独立直连原始 API） |
| Secret 审计 | NO_SECRET_LEAK: PASS（3 个真实 key 及尾 8 字符 × 全输出/缓存/历史） |
| 冷启动 | 10/10 ≤ 0.21s（目标 ≤2s） |
| 关闭回收 | 0.28s，无残留 |
| 10min 浸泡 | +5.4MB RSS，无崩溃，观测数符合 TTL 预期 |
| 200 轮加速故障循环 | PASS |

## 五、已知限制（KNOWN_LIMITATIONS）

1. **8 小时稳定性未执行** —— 明确待验收；已用 10min 浸泡 + 加速故障循环替代，不能宣称等价。
2. Token 统计（今日/7d/30d/贡献/里程碑）：NOT AVAILABLE YET，接入条件见 TOKEN_SOURCE_CAPABILITY.md。
3. 跨屏 DPI、真实 Windows 睡眠唤醒：单屏/时间窗口限制未测。
4. v0.2 的 `run.bat`（CLI）与 GUI 共享本版 `cache/usage_latest.json` —— 同一版本内 CLI 与 GUI 不应同时运行写缓存（v0.1 冻结版的 cache 完全独立，互不影响）。
5. 足迹判定为保守规则（宁可漏报不误报）；无变化不等于未工作。
6. 系统缩放为 125% 的机器上，"100%/150%" 截图通过 QT_SCALE_FACTOR 进程内模拟（不改 Windows 配置的约束下唯一可行方式），Windows 原生 per-monitor DPI 行为由 Qt6 默认栈处理。

## 六、启动方式

- **双击 `v0.2/run_gui.bat`**（固定使用 `v0.2/.venv`，pythonw 无窗口启动）。
- CLI：`v0.2/run.bat`（默认 `--watch`）。
- v0.1 的 `run.bat` / 源码 / cache 完全未动。
