# Aegis Token Monitor v0.1 —— 架构设计

> 作者：Lead Architect（Claude Code + GLM）· 2026-09-11
> 依据：`README.md` HARD CONSTRAINTS（Hermes 真机实测 2026-09-11）、`DECISIONS.md` D-001~D-005、`probe_raw/` 真实样例。
> 本文档是 **设计 + Pi 施工单**，不含主体实现。环境已确认：`python --version` → **Python 3.11.16**。

---

## 0. 架构一句话

**一个 Python 3.11 标准库单进程 CLI：四个独立 Reader（Codex 复用 CodexBar 子进程，其余三家直连 HTTP）→ 纯函数归一化为统一 JSON 模型 → 落缓存 → 文本面板 / `--json` 输出；零第三方依赖，凭据只读、只在内存中流转。**

## 0.1 目录规划（目标形态）

```
token监控项目/
├── run.py                  # 入口：刷新 + 面板 / --json / --watch
├── run.bat                 # Windows 启动器
├── aegis/
│   ├── __init__.py
│   ├── model.py            # 统一数据模型（dataclass + to_dict）
│   ├── normalize.py        # 纯函数：原始 JSON → 统一模型（含 used/remaining 语义转换）
│   ├── credentials.py      # 凭据只读读取（cc-switch.db / pi auth.json / hermes config.yaml）
│   ├── net.py              # urllib GET 封装（超时、状态码、错误分类、脱敏）
│   ├── cache.py            # 缓存读写 + TTL/reliability 判定
│   ├── ui.py               # 文本面板渲染
│   └── providers/
│       ├── __init__.py     # READERS = {"codex": read_codex, ...}（普通 dict，不是插件系统）
│       ├── codex.py        # 子进程调 codexbar-cli -p codex
│       ├── zai.py          # 直连 z.ai quota 接口
│       ├── minimax.py      # 直连 remains 接口
│       └── deepseek.py     # 直连 balance 接口
├── tests/
│   ├── fixtures/           # 从 probe_raw/ 拷贝的脱敏真实样例（离线回归）
│   ├── test_normalize.py
│   ├── test_providers_offline.py
│   ├── test_credentials.py
│   ├── test_cache.py
│   └── test_secret_leak.py
├── cache/                  # 运行时缓存（.gitignore 覆盖）
├── probe_raw/              # 已存在，不进 Git
├── README.md / DECISIONS.md / docs/
```

`READERS` 就是一个普通字典——**不做**插件系统、抽象工厂、BaseReader 继承树。每个 reader 就是一个函数 `read(cfg) -> ProviderSnapshot`。

---

## 1. Codex 数据源（问题 1）

**取法（唯一途径，逐字复用 README 配方）：**

```
%USERPROFILE%\AppData\Local\Programs\CodexBar\codexbar-cli.exe usage -p codex --json
```

- **必须显式 `-p codex`**，禁止裸 `usage --json` 或 `-p all`（README 硬约束）。
- 调用方式：`subprocess.run([EXE, "usage", "-p", "codex", "--json"], capture_output=True, timeout=60)`。
- 凭据：CodexBar 自行读 `~/.codex/auth.json`，**本项目不碰该文件**。

**拿到什么**（以 `probe_raw/codex.json` 为准——注意返回是**数组**，取 `[0]`）：

| 原始字段 | 含义 | 映射目标 |
| --- | --- | --- |
| `usage.primary`：`used_percent` / `resets_at`（ISO Z）/ `window_minutes=300` / `reset_description` | 5 小时窗口，**used 语义** | `five_hour` |
| `usage.secondary`：同上，`window_minutes=10080` | 周窗口，**used 语义** | `weekly` |
| `usage.extra_rate_windows[]`：`id`（`codex-gpt-reserve` / `reset-credits`）、`title`、`window.used_percent`、`window.resets_at`、`window.reset_description`、`window.is_informational` | Credits/Reset 等附加窗口 | `extras[]`（原样透传这几个字段） |
| `usage.updated_at`（ISO Z，纳秒精度） | 数据时刻 | `updated_at`（截断到微秒） |
| `source = "oauth"` | 来源标记 | `source = "codexbar-cli"` |
| `pace{...}` | 节奏预估 | **v0.1 忽略**（明确不解析） |

Codex 的 `resets_at` 已是 ISO 字符串（含 Z），直接 `datetime.fromisoformat` 解析（Python 3.11 支持尾随 Z），存统一模型后由 UI 转本地时间显示。

## 2. z.ai / GLM 数据源（问题 2）

**选择：B —— 直连官方配额接口，不复用 CodexBar `-p zai`。**

```
GET https://api.z.ai/api/monitor/usage/quota/limit
Authorization: Bearer <key>
```

- key 来源（只读）：`%USERPROFILE%\.cc-switch\cc-switch.db` → 表 `providers`，`app_type='claude'` 且 `name='Zhipu GLM en'` → `settings_config`（JSON 文本）→ `env.ANTHROPIC_AUTH_TOKEN`。**以 `mode=ro` URI 打开 SQLite**。
- 备用域名：`https://open.bigmodel.cn/api/monitor/usage/quota/limit`（同结构）。仅在**连接层失败**（超时/DNS/拒绝连接）时切换重试一次；HTTP 4xx（如 401）**不**切换——换域名解决不了认证问题。
- 字段：`data.limits[]` 中按 `type=="CREDIT_LIMIT"` 过滤，`unit==3` → 5h 窗口，`unit==6` → weekly；`percentage` = **已用%**；`nextResetTime` = **epoch 毫秒**。

**为什么选 B：**

1. **复用收益不同**：CodexBar 对 Codex 的价值在于 ChatGPT OAuth 私有后端无法自写；z.ai 是标准公开 REST API，直连约 20 行代码，没有"必须复用"的理由。
2. **管道统一**：MiniMax、DeepSeek 反正必须自写 HTTP reader；z.ai 直连后三家共用同一个 `net.py`（超时、错误分类、脱敏），测试方式一致（fixture 离线回归），Codex 成为唯一的子进程特例——特例越少越不容易错。
3. **错误面可控**：`probe_raw/zai.json` 证明 CodexBar 的报错是面向人工的（"Set in Preferences → Providers or Z_AI_API_KEY"），程序化处理要靠字符串匹配，脆；直连时 401/超时/畸形 JSON 都是结构化信号。
4. **开销**：CodexBar 每次子进程启动是秒级；直连 <100ms。z.ai 与 5h 窗口同粒度刷新（300s），每次省 1~2 秒不是关键收益，但白拿。

**为什么不选 A（CodexBar `-p zai`）**：需要把 key 从 cc-switch 读出后注入子进程环境变量 `Z_AI_API_KEY`，凭据多经过一层进程边界，泄漏面变大；且该路径的归一化规则对我们是黑盒（字段语义变更只能靠运行时发现），直连的字段语义 README 已逐字给出。

## 3. MiniMax 数据源（问题 3）

**自写 Reader，直连 remains 接口（D-003，CodexBar 已证实不可用）：**

```
GET https://api.minimax.io/v1/api/openplatform/coding_plan/remains
Authorization: Bearer <sk-cp-... key>
```

- key 来源（只读）：`%USERPROFILE%\.pi\agent\auth.json` → JSON 字段 `minimax.key`。
- **禁止**使用备用域名 `api.minimaxi.com`（需 cookie，status_code 1004）。
- 响应校验链：`base_resp.status_code == 0` 否则报错（带 `status_msg`）→ 在 `model_remains[]` 中取 `model_name == "general"` 的**第一条**；找不到 general 或数组为空 → `UnexpectedShape` 错误。
- 字段映射（**remaining 语义**，见 §7 转换规则）：
  - `current_interval_remaining_percent` → 剩余%；`end_time`（epoch ms）→ `five_hour.resets_at`（窗口跨度 18,000,000 ms = 5h，用 `end_time - start_time` 断言确认）；
  - `current_weekly_remaining_percent` → 剩余%；`weekly_end_time` → `weekly.resets_at`（跨度 604,800,000 ms = 7d，同样断言）；
  - `current_interval_status` / `current_weekly_status`：原样记入 snapshot 的 `raw_status` 字段（v0.1 仅透传不解释，不据此改变展示逻辑）。

## 4. DeepSeek 数据源（问题 4）

**自写 Reader，balance-only（D-004）：**

```
GET https://api.deepseek.com/user/balance
Authorization: Bearer <sk-... key>
```

- key 来源（只读）：`%USERPROFILE%\AppData\Local\hermes\config.yaml` → `custom_providers` 中 `name: deepseek` 条目的 `api_key`。**该值可能带前缀文本，必须用正则 `sk-[A-Za-z0-9_\-]+` 提取**（实测带前缀直发 401）。config.yaml 无标准库解析器，**不引入 PyYAML**——用两段式正则（先定位 deepseek 块，再取 api_key 行，再提 sk- 段），对脱敏 fixture 做测试。
- 映射：`balance_infos[0]` → `balance{value: float(total_balance), currency}`；`is_available=false` → `status="error"`，error.type=`SERVICE_UNAVAILABLE`。
- **null 语义（硬约束）**：DeepSeek 官方没有 5h/Weekly/Reset 概念，统一模型中 `five_hour = null`、`weekly = null`，**禁止填充假数据**。UI 对 null 显示 `—`，注释"该服务无窗口概念"。`balance` 可能为 null 吗？接口失败时整个 snapshot 就是 error 态；成功但 `balance_infos` 为空/缺失 → `UnexpectedShape` 错误。即：**要么有 balance，要么是 error，不存在"成功但 balance=null"的状态**。

## 5. 是否继续复用 Win-CodexBar（问题 5）

**结论：复用，但边界收紧为"仅 Codex"。**

- **复用理由**（沿 D-002）：Codex 走 ChatGPT OAuth 私有后端，CodexBar 已稳定拿到 5h/Weekly/Reset/Credits，自写 = 高风险零收益。
- **复用边界**：只调用 `usage -p codex --json` 这一条命令；不使用 `dashboard` / `serve` / `cost`（v0.1 可选附加能力，本轮不做）；不读不改 CodexBar 的任何配置。
- **退出复用的条件**（沿 D-002）：CodexBar 升级后 CLI/JSON 结构变更，或 Codex OAuth 失效——届时报 `CodexSourceUnavailable`，面板显示该 provider 处于 error 态，不阻塞其他三家。

## 6. Reader 复用/自写判定表（问题 6）

| Provider | 判定 | 途径 | 凭据来源（只读） | 关键语义 |
| --- | --- | --- | --- | --- |
| Codex | **复用** | `codexbar-cli.exe usage -p codex --json`（子进程） | CodexBar 自理（`~/.codex/auth.json`，本项目不碰） | used%，ISO resets_at，返回是数组 |
| z.ai / GLM | **自写** | 直连 `api.z.ai/api/monitor/usage/quota/limit`（备用 bigmodel.cn） | cc-switch.db（mode=ro）→ `settings_config.env.ANTHROPIC_AUTH_TOKEN` | **percentage=已用%**，epoch **毫秒** |
| MiniMax | **自写** | 直连 `api.minimax.io/v1/api/openplatform/coding_plan/remains` | `~/.pi/agent/auth.json` → `minimax.key` | **remaining%=剩余%**，epoch 毫秒，取 `general` |
| DeepSeek | **自写** | 直连 `api.deepseek.com/user/balance` | hermes `config.yaml`（正则提 `sk-` 段） | 仅 balance，5h/weekly 置 null |

## 7. 数据归一化（问题 7）

### 7.1 统一模型（`aegis/model.py`，dataclass，序列化为 JSON）

```jsonc
{
  "generated_at": "2026-09-11T14:30:00+08:00",
  "providers": [
    {
      "provider": "codex | zai | minimax | deepseek",
      "status": "ok | error",
      "five_hour": {
        "used_percent": 55.0,
        "remaining_percent": 45.0,
        "resets_at": "2026-09-11T07:51:57Z",
        "window_minutes": 300
      },
      "weekly": { "...同构...": 0, "window_minutes": 10080 },
      "balance": null,
      "extras": [],
      "updated_at": "2026-09-11T06:23:51Z",
      "source": "codexbar-cli | zai-api | minimax-api | deepseek-api",
      "reliability": "live | cached | stale",
      "error": null
    }
  ]
}
```

字段说明：`five_hour`/`weekly` 无该概念时为 **null**（DeepSeek）；`balance` 仅 DeepSeek 为 `{"value": 70.00, "currency": "CNY"}`；`extras` 仅 Codex；`resets_at`/`updated_at` 一律 **UTC ISO8601**，`updated_at` 来自数据源，不是渲染时刻；`error` 仅 error 态非 null，形如 `{"type": "...", "message": "已脱敏"}`。

### 7.2 语义陷阱与转换规则（本节是全项目最容易出错的地方）

**内部统一以 `used_percent` 为正则，`remaining_percent = 100 - used_percent` 派生，两者都存。** 转换只允许发生在 `normalize.py` 的两个具名函数里：

```python
def from_used(used_pct: float) -> tuple[float, float]:
    """z.ai percentage、Codex used_percent：源就是'已用%'。"""
    used = float(used_pct)
    assert 0.0 <= used <= 100.0, f"used_percent out of range: {used}"
    return used, round(100.0 - used, 2)

def from_remaining(remaining_pct: float) -> tuple[float, float]:
    """MiniMax current_*_remaining_percent：源是'剩余%'，必须翻转。"""
    remaining = float(remaining_pct)
    assert 0.0 <= remaining <= 100.0, f"remaining_percent out of range: {remaining}"
    return round(100.0 - remaining, 2), remaining
```

| 源 | 字段 | 源语义 | 调用 |
| --- | --- | --- | --- |
| Codex | `primary.used_percent` / `secondary.used_percent` | 已用% | `from_used`（恒等） |
| z.ai | `limits[].percentage` | **已用%** | `from_used`（恒等） |
| MiniMax | `current_interval_remaining_percent` / `current_weekly_remaining_percent` | **剩余%** | `from_remaining`（**翻转**） |

**防混淆断言（三道保险）：**

1. 上面的 range 断言：越界即抛 `NormalizationError`，该 provider 进 error 态，**绝不让非法数值上屏**。
2. 交叉校验：在**测试**里对 fixture 验证转换方向——z.ai weekly `percentage=95` → `used≈95, remaining≈5`；MiniMax interval `remaining=93` → `used≈7, remaining≈93`。方向搞反测试必红。
3. 代码评审红线：reader 层禁止出现 `100 -` 字样的减法（翻转只准发生在 `normalize.py`）；`grep -n "100 -" aegis/providers/` 应为空。

### 7.3 时间归一化（RESET_TIME）

- **epoch 毫秒 → UTC ISO**：`datetime.fromtimestamp(ms / 1000, tz=timezone.utc)`（z.ai `nextResetTime`、MiniMax `end_time`/`weekly_end_time`）。
- **ISO 字符串**：Codex 的 `resets_at`/`updated_at` 用 `datetime.fromisoformat`（3.11 原生支持 `Z` 后缀），非 naive（必须带 tz），否则报错。
- 统一模型内**一律存 UTC ISO8601**；**仅 UI 渲染层**转本地时间（`astimezone()`）显示。
- 已验证的测试向量（供 fixture 断言）：
  - z.ai `1789112389135` → `2026-09-11T07:39:49Z`；
  - z.ai `1789193655997` → `2026-09-12T06:14:15.997Z`；
  - MiniMax `end_time=1789120800000` → `2026-09-11T10:00:00Z`（且 `end-start=18,000,000ms`，正是 5h 窗口）；
  - MiniMax `weekly_end_time=1789344000000` → `2026-09-14T00:00:00Z`（`weekly_end-weekly_start=604,800,000ms`，正是 7d）。

## 8. 缓存方案（问题 8）

- **位置**：`cache/usage_latest.json`（项目根 `cache/`，`.gitignore` 必须覆盖该目录；与 `probe_raw/` 一样不进 Git）。
- **格式**：统一模型全量 JSON，外加每个 provider 记录 `fetched_at_utc`（该 provider 最近一次**成功抓取**的时刻）。
- **写时机**：仅在某 provider 本轮抓取**成功**时更新它的条目。失败不写缓存、不清缓存。
- **TTL（=刷新间隔，见 §9）**：`--watch` 循环里，距 `fetched_at` 未到 TTL 的 provider 直接复用缓存条目，`reliability="cached"`；到期才重抓。
- **失败回退（防伪装核心规则）**：本轮某 provider 抓取失败时：
  1. 若缓存中**有**该 provider 的历史条目 → 展示缓存数据，但 **`reliability="stale"`、`updated_at` 保持缓存里的原值（绝不改成 now）、`status="error"` + `error{type, message}` 同屏标出失败原因**。过期数据永远以"stale + 原时间戳 + 错误说明"三件套示人，任何情况下不允许 `reliability="live"` 且数据来自缓存。
  2. 缓存也没有 → 该 provider 显示 error 态行：`status=error`，数据列全 `—`。
- **单次运行**（`python run.py`）默认全部重抓（不走 TTL 跳过）；`--json` 同理。这样验收命令看到的永远是尽力而为的最新数据。
- 缓存文件损坏/不可解析 → 视同无缓存，记 warning，不影响抓取。

## 9. 更新频率（问题 9）

| Provider | 刷新间隔 | 理由 |
| --- | --- | --- |
| Codex | 300s | CodexBar 子进程秒级开销 ×4/h 可接受；5h 窗口粒度下 5 分钟足够 |
| z.ai | 300s | 与 5h 窗口同粒度；公开配额接口，5min 频率远低于任何限流阈值 |
| MiniMax | 300s | 同上；remains 接口为轻量查询 |
| DeepSeek | 1800s | 余额只在调用扣费/充值时变化，且无窗口紧迫性；30min 足够，省 API 调用 |

`run.py --watch [interval]`：默认 300s 一轮，每轮只重抓"到期"的 provider（per-provider due time），未到期的复用缓存（`reliability="cached"`）。**rate-limit 保护**：收到 HTTP 429 时该 provider 退避为间隔 ×3，连续 3 次 429 → 停抓该 provider 并置 error 态，等人工重启。v0.1 不做指数退避曲线（反过度工程）。

## 10. UI 最小方案（问题 10）

**选择：纯文本面板（stdlib，无任何前端技术）。**

- 理由：① 验收命令就是 `python run.py`，文本面板与验收零距离，Pi 最不容易做错；② 零端口、零浏览器依赖、零并发问题，`--watch` 下就是终端里原地刷新（ANSI 清屏 + 重画，Windows Terminal 原生支持）；③ v0.1 原则"可用 > 漂亮"；④ 本地小服务/HTML 留给 v0.2（Codex 做"桌面启动器/托盘"阶段时再议，届时 `--json` 已经是现成数据源）。
- 面板布局（一屏四行 + 头尾）：

```
Aegis Token Monitor                    2026-09-11 14:30:05 (+08:00)
PROVIDER   5h used/rem      5h RESET        WK used/rem      WK RESET        BALANCE      STATUS  SOURCE         UPDATED
codex      55.0% / 45.0%    09-11 15:51     53.0% / 47.0%    09-15 13:37     —            ok      codexbar-cli   14:23:51
zai         1.0% / 99.0%    09-11 15:39     95.0% /  5.0%    09-12 14:14     —            ok      zai-api        14:30:02
minimax     7.0% / 93.0%    09-11 18:00     15.0% / 85.0%    09-14 08:00     —            ok      minimax-api    14:30:03
deepseek    —     / —       —               —     / —        —               70.00 CNY    ok      deepseek-api   14:30:04
```

- stale 条目加 `*STALE(抓取失败: timeout)` 尾注；error 态整行数据显示 `—`，STATUS 列显示错误类型缩写。
- Credits（Codex extras）在面板底部一行小字带过（如 `reset-credits: 1 available`）。
- **禁止**引入 Rich/Textual/Flask/任何前端框架。渲染就是 `str.format` 定宽列。

## 11. Windows 启动方式（问题 11）

- **v0.1**：项目根 `run.bat`（Pi 负责，随 T6 交付）：

```bat
@echo off
cd /d %~dp0
python run.py --watch
```

- 双击即用；`python` 已在 PATH（3.11.16 已确认）。不做 venv（零第三方依赖，系统 Python 即可——这是零依赖策略的直接红利）。
- **后期（交 Codex）**：桌面快捷方式指向 `run.bat`、开机启动（shell:startup）、托盘化。v0.1 不做。
- **由谁做**：`run.bat` → Pi（T6）；快捷方式/开机启动/托盘 → Codex（下一阶段）。

## 12. Pi 实施任务拆分（问题 12）

> 粒度按"MiniMax-M3 执行型 junior coder 一次能独立做完并自测"切分。**每个任务完成后必须 `python -m pytest tests/ -q` 全绿才算完成。**

---

### T1 统一模型 + 归一化纯函数

- **目标**：建立 `model.py`（dataclass + `to_dict()`）与 `normalize.py`（§7.2 的 `from_used`/`from_remaining`、epoch-ms→UTC ISO、ISO 解析、窗口跨度断言）。
- **输入**：本文档 §7；`tests/fixtures/` 下从 `probe_raw/` 拷入的 `codex.json`、`minimax_remains.json`、`zai_quota.json`（按 §2 样例构造）、`deepseek_balance.json`（按 §4 样例构造）。
- **输出**：可独立导入的纯函数模块，全部离线可测。
- **文件**：`aegis/__init__.py`、`aegis/model.py`、`aegis/normalize.py`、`tests/test_normalize.py`、`tests/fixtures/*`。
- **验收**：pytest 覆盖 §7.2 三道保险 + §7.3 四个时间向量；MiniMax remaining=93 → used=7.0 断言存在；z.ai percentage=95 → used=95.0 断言存在。
- **禁止**：任何 I/O（网络/文件/子进程）；翻转逻辑出现在本模块之外。

### T2 凭据只读模块

- **目标**：`credentials.py` 提供 `get_key(provider) -> str`，四个 provider 各自从 §6 表列位置只读读取；DeepSeek 走两段正则提取 `sk-` 段。
- **输入**：本文档 §6 凭据来源表；用**脱敏 fixture**（伪造路径 + 假 key `sk-TEST-xxxxxxxx`）测试。
- **输出**：cc-switch.db 以 `sqlite3.connect("file:...?mode=ro", uri=True)` 只读打开；pi auth.json 用 `json.load`；hermes config.yaml 用文本正则。
- **文件**：`aegis/credentials.py`、`tests/test_credentials.py`、`tests/fixtures/fake_hermes_config.yaml`、`tests/fixtures/fake_pi_auth.json`。
- **验收**：fixture 驱动下能正确提取；DeepSeek 带前缀的值只取 `sk-` 段；**NO_SECRET_LEAK**：用假 key 跑全流程后捕获 stdout/stderr/异常文本/`to_dict()` 输出，断言假 key 子串不出现。
- **禁止**：写入任何凭据文件；打印 key（哪怕截断的）；将 key 放入 URL query；缓存 key 到磁盘；`mode=rw` 打开 cc-switch.db。

### T3 HTTP 封装 + z.ai + DeepSeek Reader

- **目标**：`net.py`（urllib GET：10s 超时、非 200 → `HttpError{status}`、`URLError`/timeout → `NetworkError`、JSON 解析失败 → `ParseError`、错误消息只含 host+status+异常类名 + 响应体截断 200 字符的**脱敏**版本）+ `providers/zai.py`（unit 3/6 映射、`from_used`、备用域名仅在连接层失败时切换一次）+ `providers/deepseek.py`（balance 映射、5h/weekly=null、`is_available=false` → error）。
- **输入**：§2、§4；fixtures。
- **输出**：两个 reader 的 `read()` 在离线时用 monkeypatch 的假 `http_get` 走通全流程。
- **文件**：`aegis/net.py`、`aegis/providers/__init__.py`、`aegis/providers/zai.py`、`aegis/providers/deepseek.py`、`tests/test_providers_offline.py`。
- **验收**：JSON_PARSE（畸形 JSON fixture → ParseError）、NORMALIZATION（percentage 语义）、RESET_TIME（epoch ms 向量）、ERROR_HANDLING（401→AuthError、超时→NetworkError、bigmodel.cn 仅连接层失败时被调用）、NO_SECRET_LEAK。
- **禁止**：用 `requests`；把 key 写进日志；401 时去试备用域名。

### T4 MiniMax Reader

- **目标**：`providers/minimax.py`：调 remains 接口、`base_resp.status_code==0` 校验、取 `model_name=="general"`、`from_remaining` 翻转、`end_time`/`weekly_end_time` → resets_at、窗口跨度断言（5h=18,000,000ms / 7d=604,800,000ms，不符 → `UnexpectedShape`）。
- **输入**：§3；`tests/fixtures/minimax_remains.json`（即 probe_raw 全文，含 video 条目——正好测"过滤出 general"）。
- **文件**：`aegis/providers/minimax.py` + 对应测试。
- **验收**：fixture 回归：general 条目 → five_hour{used:7.0, remaining:93.0, resets_at:2026-09-11T10:00:00Z}、weekly{used:15.0, remaining:85.0, resets_at:2026-09-14T00:00:00Z}；无 general 条目的 fixture → UnexpectedShape；status_code 非 0 fixture → 带 status_msg 的错误；NO_SECRET_LEAK。
- **禁止**：使用 `api.minimaxi.com`；把 video 条目当 general 用；在 reader 里做 `100 -` 翻转。

### T5 Codex Reader

- **目标**：`providers/codex.py`：`subprocess.run` 调 codexbar-cli（**硬编码显式 `-p codex`**，超时 60s），解析数组取 `[0]`，映射 §1 表；exe 不存在/超时/非零退出 → `CodexSourceUnavailable`。
- **输入**：§1、§5；`tests/fixtures/codex.json`（probe_raw 原文）。
- **文件**：`aegis/providers/codex.py` + 测试。
- **验收**：fixture 解析 → five_hour{used:55.0, resets_at:2026-09-11T07:51:57Z, window_minutes:300}、weekly 同理、extras 含 `codex-gpt-reserve` 与 `reset-credits`；畸形输出（空数组/非 JSON）→ ParseError；EXE 路径不存在 → CodexSourceUnavailable（用注入的假路径测，真机调用留集成验收）；NO_SECRET_LEAK。
- **禁止**：裸 `usage --json` / `-p all`；读 `~/.codex/auth.json`；解析 `pace` 字段。

### T6 缓存 + CLI + 面板 + run.bat

- **目标**：`cache.py`（§8 全部规则）、`run.py`（默认全量抓取渲染面板；`--json` 输出统一模型；`--watch [300]` 按 §9 per-provider due time）、`ui.py`（§10 面板，null→`—`，stale 三件套）、`run.bat`。
- **输入**：T1~T5 产物；§8/§9/§10/§11。
- **文件**：`aegis/cache.py`、`aegis/ui.py`、`run.py`、`run.bat`、`tests/test_cache.py`、`.gitignore`（追加 `cache/`）。
- **验收**：缓存命中→`cached`；抓取失败有缓存→stale 且 `updated_at` 不变（单测用冻结时间断言）；无缓存失败→error 行；`--json` 输出可被 `json.loads` 且结构与 §7.1 一致；`python run.py` 面板四行齐；`run.bat` 双击可跑。
- **禁止**：把 `updated_at` 写成渲染时刻；缓存里出现任何 key；引入调度库/HTTP 框架。

### T7 集成验收 + 安全审计

- **目标**：真机端到端 + 全链路密钥审计。
- **输入**：T1~T6 全部。
- **输出**：① `python -m pytest -q` 全绿；② `python run.py` 真机四 provider 至少三家 `ok`（Codex 依赖 CodexBar 环境，照实记录）；③ `python run.py --json` 的输出中不含 `sk-`、`Bearer `、`ANTHROPIC_AUTH_TOKEN` 等模式，且审计脚本**在内存中**读取各凭据源、断言其值（及尾 8 字符）不出现在输出中；④ 确认 `.gitignore` 覆盖 `probe_raw/` 与 `cache/`。
- **文件**：`tests/test_secret_leak.py`（扩展为全链路）、`tools/audit_output.py`（约 30 行的验收辅助脚本）。
- **验收**：以上 ①~④ 全部通过并留输出摘录（脱敏后）交 Hermes。
- **禁止**：为通过审计而删除/绕过测试；在审计脚本中打印 key。

---

## 13. 测试设计总表

每 provider 六类测试点；`DATA_READ` 类标记 `@pytest.mark.live`（真机、可跳过），其余全部用 `tests/fixtures/`（probe_raw 真实样例拷贝）离线回归，**无网络必须全绿**：

| 类别 | Codex | z.ai | MiniMax | DeepSeek |
| --- | --- | --- | --- | --- |
| DATA_READ（live） | `-p codex` 真跑返回非空数组 | HTTP 200 且 code==200 | HTTP 200 且 status_code==0 | HTTP 200 且 is_available |
| JSON_PARSE | 空数组/非 JSON→ParseError | 畸形 JSON / 缺 `data.limits`→UnexpectedShape | 缺 `model_remains`/无 general→UnexpectedShape | 缺 `balance_infos`→UnexpectedShape |
| NORMALIZATION | used 恒等 + extras 透传 | **percentage=已用**（95→used 95） | **remaining 翻转**（93→used 7） | balance "70.00"→70.00 float，5h/weekly=null |
| RESET_TIME | ISO Z 解析 | epoch ms 两向量（§7.3） | end/weekly_end 两向量 + 跨度断言 | 无（null） |
| ERROR_HANDLING | exe 缺失/超时/非零退出 | 401→AuthError；超时→NetworkError→试备用域；429→退避 | status_code 非 0→带 status_msg | is_available=false→error 态 |
| NO_SECRET_LEAK | 输出/异常无凭据 | 同左（含 cc-switch 读取路径） | 同左 | 同左（含正则提取后残文不含前缀垃圾） |

附加：`test_cache.py`（冻结时间测 TTL/stale/cached 三态）、`test_secret_leak.py`（全链路假 key 注入）。

## 14. 安全约束（硬，摘自 README + 任务书，Pi 全程有效）

1. 不打印任何 key/token/cookie（日志、异常、print、面板、usage.json、测试快照一律不得出现明文或可逆编码）。
2. 不修改 `~/.codex/auth.json`、CC Switch、Claude Code 配置、Hermes 配置、任何 provider 配置；全部只读。
3. 凭据只在内存中使用；`cache/` 只存额度数据不存凭据。
4. 任何新增认证方式必须先报 Hermes 转呈少爷批准。
5. `probe_raw/`（含 `ccs.db`）与 `cache/` 不进 Git。

## 15. 反过度工程清单（v0.1 明确不做）

第三方依赖（requests/PyYAML/rich/HTTP 服务化）· 插件系统/抽象工厂/BaseReader 继承树 · 数据库 · 重试曲线/令牌桶 · venv/打包（PyInstaller）· 托盘/开机启动 · HTML/前端 · `pace` 解析 · MiniMax status 位语义解释。唯一被提议的"新工具"是 T7 的约 30 行审计脚本。若实现中确实想加依赖，必须停下报 Hermes。

## 16. 最大风险（供 Hermes Route Review）

1. **CodexBar 是外部黑盒**：其 CLI 参数/JSON 结构升级会直接打断 Codex 源（D-002 已有退出条件）。缓解：T5 对畸形输出做结构化报错，面板降级为 error 行而不是崩溃。
2. **MiniMax remains 是非公开文档接口**：字段/鉴权可能随平台调整（历史上有 cookie 域名坑）。缓解：`UnexpectedShape` 全字段校验 + base_resp 校验，结构漂移时立刻显式报错而非给出错数据。
3. **凭据位置漂移**：hermes config.yaml 带前缀的 key、cc-switch 的记录名 `'Zhipu GLM en'` 任一变动都会导致 MissingCredential。缓解：credentials.py 每个来源读不到时给出**指路型**错误消息（哪个文件哪个字段），少爷一眼能修。

---

# v0.2 架构附注（2026-09-12 实施后同步）

原文档 §13「线程、进程和退出」规划了 multiprocessing 子进程方案。**实际实施
采用更简单的等价方案（已在 DECISIONS v0.2 D-008 记录理由与证据）**：

1. **并发**：`ThreadPoolExecutor(4)` + 单协调线程（磁盘唯一写者）。
   - 依据：reader 已内建硬超时（Codex subprocess 60s kill；HTTP socket 10s），
     进程边界不增加有效时限。
2. **退出回收**：`aegis/services/proclife.py` 把应用进程加入私有 Windows
   Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）；应用退出时全部后代
   （codexbar-cli.exe 等）由系统回收，只作用于本应用后代。
   - 证据：`tests/test_security_v02.py`（父死后孙进程被回收的集成用例）；
     真机 GUI 关闭 0.28s、无 codexbar 残留。
3. **UI 边界不变**：desktop 不 import providers/credentials/net/cache；
   Envelope→ViewModel 纯函数适配；SQLite 查询在独立 worker 线程。

其余（Envelope 契约、单写者、安全错误映射、SQLite 历史与备份、Token 数据
闸门）按原文档与 UI_NEXT_VERSION_DESIGN.md §12–§13 实施，无偏离。
