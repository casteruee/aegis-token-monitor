# TOKEN_SOURCE_CAPABILITY —— 实际 Token 数据源调查（v0.2 Batch 1）

调查日期：2026-09-12 · 调查方式：只读 SQLite（`mode=ro`）+ 白名单字段聚合查询
调查对象：`~/.cc-switch/cc-switch.db` 的 `proxy_request_logs`（**实时库**，非 probe_raw 旧副本）
结论：**闸门未通过 —— Token 统计本版 NOT AVAILABLE YET**（理由与证据如下）

---

## 一、库内事实（2026-09-12 实测，全部只读查询）

`proxy_request_logs` 共 7,288+ 行（调查期间仍在增长，库为活跃实时库）。

### 1.1 时间与覆盖

| 事实 | 值 | 证据 |
| --- | --- | --- |
| `created_at` 单位 | **epoch 秒**（非毫秒） | 样本 `1788954716` 等 10 位整数 |
| 覆盖范围 | 2026-08-14 → 2026-09-12（约 30 天，滚动） | `MIN/MAX(created_at)` |
| 24h 内活跃 | 391 行 | 已确认仍在写入 |

### 1.2 四类 data_source 与 Provider 归属

| data_source | 行数 | provider_id | 时间范围 | 模型 |
| --- | --- | --- | --- | --- |
| `codex_session` | 3,975 | `_codex_session`（伪 ID） | 08-15 → 09-12（今天仍活跃） | gpt-5.6-sol / codex-auto-review 等 |
| `session_log` | 2,116 | `_session`（伪 ID） | 08-26 → 09-08 | MiniMax-M3 (786) / deepseek-v4-pro (625) / glm-5.3 (569) / glm-5.3-flash (116) / kimi (20) |
| `pi_session` | 740 | `minimax` | 09-09 → 09-11 | MiniMax-M3 |
| `proxy` | ~463 | **真实 provider UUID** | 09-09 → 09-12（活跃） | glm-5.3-flash (459) / MiniMax-M3 (2) |

- **`request_id` 全表唯一**（7,292/7,292）；proxy 与 session_log 之间
  **精确 request_id 碰撞 = 0**；时间范围互补（session_log 止于 09-08，proxy
  始于 09-09）→ proxy 与 session_log 当前**没有发现双计**。
- 但 Codex / Claude Code 会话导入行用**伪 provider_id**（`_codex_session` /
  `_session`），按 provider 归属必须**从 model 名反推** —— 本项目闸门明确
  要求"不能按 model 名猜 Provider"。
- DeepSeek：**仅**存在于 session_log（625 行），且止于 **2026-09-05** ——
  当前窗口无 DeepSeek 记录。

### 1.3 token 字段与 semantics

- 字段：`input_tokens` / `output_tokens` / `cache_read_tokens` /
  `cache_creation_tokens` + `input_token_semantics`。
- semantics 分布：**0 → 6,091 行（codex_session + session_log）；2 → 1,201 行
  （pi_session + proxy）**。
- **枚举含义无法从数据自证**：
  - 若假设"2 = input 包含 cache"，则 input 应 ≥ cache_read；实测 sem=2 的
    avg(input)=4,927 **远小于** avg(cache_read)=69,787 —— 与包含假设矛盾；
  - sem=0 的 avg(input)=73,749 同样小于 avg(cache_read)=100,327；
  - 仅 12/13 行出现 `input == cache_read(±creation)` 的包含特征。
  - 结论：**没有权威口径来源（CC Switch 源码/文档），无法定义互不重复的
    normalized total**。闸门第 3 条（input/cache 包含关系）不通过。

### 1.4 其它闸门项

| 闸门条目（设计 §12.2） | 状态 |
| --- | --- |
| 1. created_at 单位/覆盖/保留 | ✅ epoch 秒，~30 天滚动；保留策略未知 |
| 2. provider_id 稳定映射 | ❌ 伪 ID（`_session`/`_codex_session`）需按 model 反推 |
| 3. input/cache 包含关系 | ❌ semantics 枚举含义无权威来源，无法自证 |
| 4. 请求去重 / 迟到修订 | ⚠️ `session_usage_dedup` 只覆盖 pi_session（740 行）；session 导入的流式修订/迟到数据规则未知 |
| 5. 失败请求 usage | ⚠️ status 499×1 / 500×9；其 token 是否可靠未验证 |
| 6. 跨源重复（rollup vs 明细） | `usage_daily_rollups` **0 行**（未启用）；单权威源可选明细表 |

### 1.5 CodexBar `cost --json` 候选

README 提及该命令输出"本地 Claude/Codex 会话 30 天 token 成本"。本轮未接入：
其口径是**会话累计 + 成本估算**，与请求级去重规则、能否区分 Codex/Claude、
是否与 CC Switch 会话导入重叠，均未验证 —— 维持"仅候选，不当 Codex Token
总量"的既有判断。

---

## 二、结论：本版交付边界

**闸门 6 条中 2 条明确不通过、2 条部分通过 → 不接入真实 Token 统计。**

本版交付（不受闸门影响）：

1. 额度观测历史（SQLite，成功采集才入账）；
2. 七日观测足迹（额度变化判定，保守规则，见 `aegis/history/activity.py`）；
3. UI 明确显示 **"Token 统计尚未接入"**，不显示 0 或假数。

**将来接入条件**（全部满足才重新评审）：

1. 从 CC Switch 源码/文档确认 `input_token_semantics` 0/2 的权威定义，并确认
   四个 token 字段的包含关系；
2. provider 归属有稳定字段（或官方确认 model→provider 映射表）；
3. `usage_daily_rollups` 启用或有请求级幂等导入方案（水位 + upsert + 迟到修订）；
4. DeepSeek 覆盖现状书面确认（当前实时窗口无记录，历史止于 09-05）；
5. 跨源（proxy / session_log / pi_session / codex_session）去重方案验证。

满足后按设计 §12.2 加 `usage_events` / `import_state` / `milestone_seen`
三表与导入器；**基础版不预建这些表**（设计 §12.2 明确允许）。

## 三、安全说明

- 全部查询经 `sqlite3.connect("file:...?mode=ro", uri=True)` 只读执行；
- 未读取 `settings_config` / `api_key` / 任何凭据列；未导出整库；
- 本文档仅含聚合统计、伪 ID、模型名与时间戳，无凭据内容。
