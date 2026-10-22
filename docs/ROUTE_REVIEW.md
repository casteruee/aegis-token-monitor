# ROUTE REVIEW —— Hermes 对 CC 架构方案的审核（2026-09-11）

审核对象：`docs/ARCHITECTURE.md`（Claude Code + GLM 产出）
审核人：Hermes（Aegis 总管）
结论：**整体采纳，6 处修正**。CC 的派工建议不是最终命令，以下是最终派工依据。

## 一、采纳的部分（不改）

| 项 | 决定 | 理由 |
| --- | --- | --- |
| 技术栈 | Python 3.11+，**零第三方依赖**，`urllib` + `subprocess` | 反过度工程，避免 venv/依赖漂移 |
| Codex 源 | 复用 `codexbar-cli.exe usage -p codex --json`（唯一子进程特例） | ChatGPT OAuth 私有后端不可自写 |
| z.ai 源 | **直连** `api.z.ai/api/monitor/usage/quota/limit` | 复用 CodexBar 的收益不足以抵消凭据跨进程注入成本 |
| MiniMax 源 | **自写** `api.minimax.io/.../coding_plan/remains` | CodexBar 已证实不可用（0%） |
| DeepSeek 源 | **自写** `api.deepseek.com/user/balance`，5h/weekly = null | 官方无窗口概念，禁止伪造 |
| 归一化 | `used_percent` 为正则，`remaining = 100 - used`；翻转只准在 `normalize.py` | 防 z.ai(已用%) / MiniMax(剩余%) 混淆 |
| 时间 | 内部一律 UTC ISO8601，仅 UI 层转本地 | 单一真相 |
| 缓存 | `cache/usage_latest.json` + `reliability = live/cached/stale`；stale 必须保留**原始 updated_at** | 禁止把过期数据伪装成新鲜 |
| UI | 纯文本面板（ANSI 原地刷新），禁止任何前端框架 | v0.1 "可用 > 漂亮" |

## 二、修正（最终派工以此为准）

### M-1 任务粒度：7 → 4（合并，降交接损耗）

CC 拆的 T1~T7 粒度过细（Pi 是单次执行型，每次调用都要重新读上下文）。合并为 **P1~P4**，每个仍可独立验收：

- **P1** = CC 的 T1 + T2（模型/归一化 + 凭据只读）
- **P2** = CC 的 T3 + T4（net.py + z.ai / MiniMax / DeepSeek 三个自写 reader）
- **P3** = CC 的 T5 + 缓存模块（codex reader + cache.py）
- **P4** = CC 的 T6 剩余（run.py / ui.py / run.bat）+ 自检脚本

### M-2 最终验收权不下放（重要）

CC 把 T7「集成验收 + 安全审计」派给 Pi —— **驳回**。按本项目最高原则，最终验收归 Hermes，且不接受任何 Agent 的自述 PASS。
改为：Pi 只交付 `tools/selfcheck.py`（自检脚本，跑完把脱敏输出交 Hermes）；**真机端到端 + 密钥审计由 Hermes 亲自执行**。

### M-3 砍掉 429 退避状态机

CC 的 §9「429 → 间隔×3，连续 3 次停抓」属于 v0.1 不需要的状态。改为：**429/5xx → 本轮该 provider 直接进 error 态，下一轮自然重试**。不引入计数器。

### M-4 解释器版本硬约束（真机风险，CC 未检查）

本机实测存在 **三个 Python**：3.9.0a4（AppData）、3.11.16（uv）、3.12.10（E:\Python312，用户 PATH 首位，也是 `py` 默认）。风险：`datetime.fromisoformat` 对尾随 `Z` 的支持是 3.11 才有的，若被 3.9 执行会静默失败。

最终要求：
1. `run.bat` 与 `run.py` 必须**断言 Python ≥ 3.11**，否则打印明确中文错误并退出（run.bat 优先用 `py -3.12` / `py -3.11`，回退 `python`）。
2. 代码内**不得依赖 3.11 的 Z 解析行为**：统一 `s.replace("Z", "+00:00")` 后再 `fromisoformat`（3.9+ 全兼容）。测试要覆盖该路径。

### M-5 凭据来源加 fallback（提升可用性）

CC 的单点来源在文件漂移时会整体失败。最终优先级：

| Provider | 首选 | 备选 |
| --- | --- | --- |
| z.ai / GLM | `.cc-switch/cc-switch.db`（`mode=ro`，**已实测在 CC Switch 运行中可只读打开**）→ `app_type='claude'` 且 `name='Zhipu GLM en'` → `settings_config.env.ANTHROPIC_AUTH_TOKEN` | 无 |
| MiniMax | `~/.pi/agent/auth.json` → `minimax.key` | cc-switch.db → `app_type='claude'` 且 `name='MiniMax en'` → 同字段 |
| DeepSeek | `~/AppData/Local/hermes/config.yaml` → `custom_providers[name=deepseek].api_key` → 正则取 `sk-[A-Za-z0-9_\-]+` | cc-switch.db → `app_type='hermes'` 且 `name='DeepSeek'` → `api_key` → 同正则 |
| Codex | CodexBar 自理 | 无（本项目不碰 `~/.codex/auth.json`） |

注意：**cc-switch.db 里 `app_type='claude'` 的 DeepSeek token（尾号 gpIS）实测 401 无效**，不要拿它当 DeepSeek 的首选；只有 `hermes`/`codex` app 下的 DeepSeek key 有效。

### M-6 fixtures 必须脱敏

禁止把 `probe_raw/` 的真实响应体原样提交进 Git（含真实余额 `70.00`、`login_method: "ChatGPT Plus"`、真实时间戳组合）。要求：`tests/fixtures/` 的样例保留**真实结构 + 字段名**，数值改为等价但非真实的合成值；原始样例只留在 `probe_raw/`（已 gitignore），供 Hermes 验收比对。

## 三、Codex 的活（Windows / 本机）——v0.1 后置

| 任务 | 何时 | 风险 |
| --- | --- | --- |
| 桌面快捷方式 → `run.bat` | P4 验收通过后 | 低 |
| run.bat 解释器探测与兼容性加固 | 若 P4 产出的 run.bat 在真机双击失败 | 低 |
| 开机启动（shell:startup）、托盘化 | **v0.2 或少爷批准后** | 中（改动用户启动项，需先报批） |

不在 v0.1 主路径，避免 Codex 被当普通实现工用。

## 四、分工合理性自检（对应项目第十六条）

- 设计/施工单/Review → CC ✅
- 主体实现（4 个 reader、归一化、缓存、CLI、面板、测试）→ Pi ✅
- 本机集成（快捷方式/启动/托盘）→ Codex（后置，非必须）
- 命脉调查、路由、交接、真机验收 → Hermes ✅
- **不得出现**：Hermes 自己写主体代码 / Pi 空转。当前 Pi 承接约 80% 的代码工作量。
