> 发布版说明：账户余额等个人数值已替换为占位数值；测试计数、结论与判定未改动。

# ACCEPTANCE —— Aegis Token Monitor v0.1 验收记录

验收人：Hermes（Aegis 总管，独立执行，不采信 Agent 自述）
日期：2026-09-11
结论：**PASS → 冻结 v0.1**

## 一、Hermes 终验（全部为我本人执行的真实命令与真实输出）

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 单元测试 | `python -m pytest tests -q` | **341 passed**（P1 81 → P2 195 → P3 271 → P4 322 → P5 341） |
| 真机面板 | `python run.py` | 四 provider 全部 `ok`，四行齐全 |
| 机器可读输出 | `python run.py --json` | `json.loads` 通过，结构符合设计 §7.1 |
| 自检脚本 | `python tools/selfcheck.py` | `NO_SECRET_LEAK: PASS`、`OVERALL: PASS` |
| 数据真实性交叉核对 | 我另写脚本：4 家面板数值 vs 直连原始源 | **4/4 MATCH**（见下） |
| 密钥审计（追踪文件） | `git ls-files \| xargs grep` | 仅命中测试用假串（`sk-cp-...xxxx`），**无真实密钥** |
| 运行时输出审计 | `run.py --json \| grep -c "sk-\|Bearer"` | `0` |
| 缓存不变量（我独立写脚本） | 失败不覆盖 / stale 保时间戳 / 永不 live+缓存 | 3/3 PASS |
| git 卫生 | `git status --ignored` | `probe_raw/`、`cache/`、`logs/` 均未入库 |

数据真实性交叉核对明细（面板 vs 原始源，同一时刻）：

```
codex    源 5h 0.00 / wk 53.00      面板 0.00 / 53.00            MATCH
zai      源 5h 0.00 / wk 96.00      面板 0.00 / 96.00            MATCH
minimax  源 5h 剩 69 → 已用 31      面板 31.00                   MATCH
deepseek 源余额 70.00 CNY           面板 70.00 CNY                MATCH
```

真机面板（2026-09-11 16:19，冻结版本）：

```
Aegis Token Monitor                                   2026-09-11 16:19:24 (+08:00)
PROVIDER  5h used/rem      5h RESET        WK used/rem      WK RESET        BALANCE     STATUS  SOURCE         UPDATED
codex     0.0% / 100.0%    09-11 21:19     53.0% / 47.0%    09-15 13:37     —           ok      codexbar-cli   16:19:22
zai       0.0% / 100.0%    —               96.0% / 4.0%     09-12 14:14     —           ok      zai-api        ~16:19
minimax   31.0% / 69.0%    09-11 18:00     17.0% / 83.0%    09-14 08:00     —           ok      minimax-api    ~16:19
deepseek  —     / —        —               —     / —        —               70.00 CNY   ok      deepseek-api   ~16:19
[codex] reset-credits: 1 reset credit available
~ = 本地抓取时间（源未提供数据时刻）
```

## 二、验收阶段发现并修复的缺陷（设计阶段未预见）

**D-006（阻塞级，Hermes 发现）**：z.ai 5 小时窗口刚重置、用量为 0 时，官方接口**整体省略 `percentage` 与 `nextResetTime` 键**，原实现判为 `unexpected_shape` → 整个 provider 掉线。
→ 修复（P5）：缺字段按"窗口已重置"处理（`currentValue/usage` 计算或 0.0），`resets_at=None`，`unexpected_shape` 收紧到真正的结构错误。**修复后 5h 显示 `0.0% / 100.0%`、RESET 为 `—`，weekly 照常解析。**

**D-007（可读性）**：MiniMax / DeepSeek 接口不返回数据时刻 → UPDATED 列显示 `—`。
→ 修复（P5）：UI 显示本地抓取时刻并加 `~` 前缀区分；JSON 的 `updated_at` 保持 `null`（**源真相不许被污染**）。

## 三、CC 独立 Review 结论（第二轮，独立于 Hermes 验收）

**PASS**（34 轮，`logs/cc_review.json`）。8 项逐条 PASS，均附代码证据（文件:行）。
非阻塞发现 3 条，留 v0.2：

1. **低**：`ui.py:270,275` 把 CodexBar 的 `reset_description` 直接拼进面板底注，未过 `_scrub()`（行/尾注已过）。若上游在该字段回显异常文本，理论上可能上屏。
2. **低**：`cache.py:399 _opt_window` 对损坏缓存条目（缺 `used_percent`）回退 0/0 而非丢弃，仅手工损坏可触发。
3. **记录**：`run.py:71` error 且无缓存时 `reliability="live"`，语义略怪但无数据随行，不违反"live+缓存"禁令。

CC 认为最值得担心：MiniMax 非公开接口 + 硬跨度断言（18,000,000ms）——平台若合法调整窗口长度会整体转 error，建议 v0.2 改为"记录警告 + 降级展示"。

## 四、分工记录（对应项目第十六条：本次额外验收 Agent 分工）

| Agent | 实际做了什么 | 证据 |
| --- | --- | --- |
| **Hermes** | ① 命脉调查：真机打通 4 家数据源、复现 CodexBar 的 MiniMax 失效、找到 remains 替代接口、摸清凭据位置与"带前缀 key 会 401"的坑；② 项目骨架 + README 硬约束 + DECISIONS；③ Route Review（7 任务→4、收回终验权、砍退避状态机、加解释器版本约束、凭据 fallback、fixtures 脱敏）；④ 每轮派工与交接；⑤ 逐轮独立验收（pytest/真机调用/缓存不变量/密钥审计）；⑥ 抓出 D-006 并驱动修复；⑦ 4 家数据与原始源交叉核对；⑧ 终验与冻结 | README.md 硬约束、ROUTE_REVIEW.md、本文件 |
| **Claude Code + GLM** | ① 产出 `docs/ARCHITECTURE.md`（28KB，12 问答齐 + 子任务拆分 + 测试设计）；② 独立 Review → PASS + 3 条低危发现 + 冻结意见 | `docs/ARCHITECTURE.md`、`logs/cc_review.json` |
| **Pi + MiniMax-M3** | ① P1 模型/归一化/凭据；② P2 net + 三个直连 reader；③ P3 Codex reader + 缓存；④ P4 CLI/面板/run.bat/自检；⑤ P5 修复 D-006/D-007。合计 5 轮，代码 100% 由 Pi 产出 | `logs/pi_P1..P5.json`、git 提交 4a1431c / 27180f9 / edfc242 / bca8769 / 02725b7 |
| **Codex** | **v0.1 内未使用**（本机集成项按 Route Review 后置到 v0.2） | — |

**是否存在 ROUTING_IMBALANCE：否。** 主体代码（约 2400 行含测试、5 个来源模块、341 个测试）全部由 Pi 产出；Hermes 未写任何业务代码，只写了 README/DECISIONS/ROUTE_REVIEW/施工单/一次性的独立验收脚本（不属于项目代码）。

## 五、版本冻结

- v0.1 冻结（git tag `v0.1.0`）。后续开发按 D-001 复制为 `v0.2` 新目录，不原地覆盖。
- v0.2 候选清单（按优先级）：
  1. `_extras_footer` 过 `_scrub()`（CC 低危发现 1，3 行）
  2. MiniMax 窗口跨度断言改为"记录警告 + 降级"（CC 最担心项）
  3. 缓存损坏条目丢弃而非 0/0（CC 低危发现 2）
  4. 桌面快捷方式 / 开机启动 / 托盘（Codex 的活）
  5. CC Switch `proxy_request_logs` 的历史用量与费用视图
