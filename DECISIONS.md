# DECISIONS — Aegis Token Monitor

决策日志。每条记录：决定了什么 / 为什么 / 备选方案 / 为什么没选 / 什么情况允许重新考虑。

---

## D-001 目录与版本习惯

- **决定**：项目根 `%USERPROFILE%\Desktop\token监控项目`；v0.1 稳定后冻结，后续以 `v0.2` 新目录演进，不原地覆盖。
- **为什么**：少爷的长期习惯是一版一目录（Aegis-Watchdog / CC-first-project 同款）。
- **备选**：单目录 + git 分支迭代。**没选**：与既有习惯冲突，且唯一稳定版存在被覆盖风险。

## D-002 复用 Win-CodexBar 而不是全部自写

- **决定**：Codex 数据源继续复用 `codexbar-cli.exe usage -p codex --json`（source=oauth）；z.ai 可选复用 `-p zai`。
- **为什么**：Codex 走 ChatGPT OAuth 私有后端，实测 CodexBar 已能稳定拿到 5h / Weekly / Reset / Credits，自写代价高、稳定性差。
- **备选**：全部自写 OAuth client。**没选**：无收益、高风险（涉及 auth.json 与私有接口）。
- **重新考虑条件**：CodexBar 升级后接口变更或 Codex OAuth 失效。

## D-003 MiniMax 必须自写 Reader（已实测解决）

- **决定**：不使用 CodexBar 的 MiniMax provider，改为直连 `GET https://api.minimax.io/v1/api/openplatform/coding_plan/remains`。
- **为什么**：2026-09-11 复现 CodexBar 失败（只给 `used_percent: 0.0`，无 5h/Weekly/Reset）；而官方 remains 接口实测 HTTP 200，直接给出 5h（`current_interval_remaining_percent`）与周窗口（`current_weekly_remaining_percent`）+ 窗口起止时间。
- **备选**：Web 抓取 MiniMax 控制台。**没选**：需要 cookie/登录态，历史不稳定，且违反最小权限。
- **重新考虑条件**：该接口需登录态或字段结构变化。

## D-004 DeepSeek 只做 Balance，不伪造窗口

- **决定**：DeepSeek 仅读 `GET https://api.deepseek.com/user/balance`，统一模型中 five_hour/weekly 置 null。
- **为什么**：官方无 5h/Weekly 概念；伪造同构数据违反"不编数据"原则。
- **备选**：用 CodexBar `-p deepseek`（把余额塞进 `primary.reset_description`）。**没选**：字段语义被复用得不干净，直连接口结构更清晰。

## D-005 凭据只读复用

- **决定**：凭据从既有位置只读读取（pi auth.json / hermes config.yaml / cc-switch.db），不在本项目落盘、不打印、不入 Git。
- **为什么**：符合最小权限与项目安全要求，避免重复维护密钥。
- **重新考虑条件**：上述文件位置改变，或少爷希望改为统一凭据库。

## D-006 z.ai 接口在"窗口刚重置"时会省略 `percentage` / `nextResetTime`

- **背景**：P4 交付后的真机验收中，zai 一行报 `unexpected_shape`、数据全 `—`。Hermes 复现原始响应，发现当 5 小时窗口刚重置、`currentValue=0` 时，接口**整体省略** `percentage` 与 `nextResetTime` 两个键（不是给 0），而 weekly 条目字段齐全。
- **决定**：reader 必须把"缺字段"解释为"窗口已重置/上游未提供"，而不是结构性错误：
  - `percentage` 缺失 → 用 `currentValue / usage * 100` 计算，实在无 `usage` 则取 `0.0`（仍走 `normalize.from_used`，保持唯一翻转入口）；
  - `nextResetTime` 缺失 → 该窗口 `resets_at = null`（**禁止**用本地时间或上一轮缓存值伪造）；
  - `unexpected_shape` 收紧为：无 `data.limits` / 无任何 `CREDIT_LIMIT` / unit 3 与 6 同时缺失；单一 unit 缺失 → 该窗口为 `None`，整体仍 `ok`。
- **为什么**：Provider 数据是真实的，报错是**我们过严**；对上游合法状态报错会让整个 provider 掉线（面板显示 `—`），比"值 0% + 重置时间未知"糟糕得多。
- **备选**：把该 provider 直接标 UNAVAILABLE。**没选**：数据是可用的，只是形态不同。
- **重新考虑条件**：上游开始用 `null` 或 `0` 显式表达同一状态（届时两种形态都要兼容）。

## D-007 `updated_at` 的真相边界（UI 显示本地抓取时间必须可区分）

- **背景**：MiniMax remains 与 DeepSeek balance 接口不返回任何"数据时刻"，模型里 `updated_at = null` 是正确表示，但 UI 因此出现两个 `—`，与"Last Updated 一眼可见"的要求冲突。
- **决定**：**JSON 模型的 `updated_at` 保持 `null`（源真相不变）**；UI 层在 `updated_at` 为 null 时显示本地抓取时刻并加 `~` 前缀区分（如 `~15:58`），面板保留脚注说明含义。
- **为什么**：把本地抓取时间写进 `updated_at` 会让"数据时刻"与"抓取时刻"混淆，直接违反"禁止把过期数据伪装成新鲜"的同一条原则。
- **重新考虑条件**：上游新增数据时刻字段（届时优先用源字段）。

---

（后续由 CC 设计、Hermes Route Review、Pi 实现过程中产生的决策追加于下）

---

## v0.2 新增决策（2026-09-12）

## D-008 采集并发用「线程池 + Windows Job Object」，不用 multiprocessing 子进程

- **决定**：MonitorService 用 4 worker 的 ThreadPoolExecutor 跑 reader，配一个
  协调线程独占全部磁盘写入（cache + SQLite 单写者）；进程回收改用
  **应用私有 Windows Job Object（kill-on-close）**——本进程及其全部后代
  （CodexBar 的 codexbar-cli.exe）在应用退出时被系统一并回收。
- **为什么（设计允许的简化，见 UI_NEXT_VERSION_DESIGN §13「更简单且满足相同
  验收目标」条款）**：设计文档选 multiprocessing 的两个动机——①urllib 整轮
  时限；②关闭回收。实测 ① 已由 reader 内部硬超时解决（Codex subprocess
  60s、HTTP socket 10s）；② 的真实风险只剩「退出时 CodexBar 孙进程残留」，
  Job Object 恰好直接解决，且不引入 spawn 入口保护、IPC 脱敏、进程树枚举
  等复杂度。
- **验证证据**：`tests/test_security_v02.py` 集成用例——父进程退出后孙进程
  被 job 回收（OpenProcess 轮询确认死亡）；真机 GUI 实测关闭 0.28s（目标
  ≤2s）且 tasklist 无 codexbar 残留。
- **边界**：Job 只覆盖本应用的后代进程，绝不波及用户独立运行的同名进程；
  ctypes 调用失败时保护层降级（记录日志，不拖垮主功能）。
- **重新考虑条件**：若未来 reader 需要脱离 60s/10s 硬超时（如长任务），
  再引入有界子进程。

## D-009 实际 Token 统计闸门未通过，本版 NOT AVAILABLE YET

- **决定**：不接入 CC Switch `proxy_request_logs` 的 Token 计数；首页/历史
  页明确显示「Token 统计尚未接入」；`usage_events` 等三表不预建。
- **为什么**：闸门 6 条中 2 条不通过——`input_token_semantics` 枚举（0/2）
  含义无权威来源且数据自证相互矛盾（sem=2 的 avg input 远小于 avg
  cache_read，与"包含"假设冲突）；会话导入行用伪 provider_id（`_session`/
  `_codex_session`），按 provider 归属需按 model 名反推（闸门明确禁止）。
  详见 `docs/TOKEN_SOURCE_CAPABILITY.md`。
- **备选**：按 model 名猜 Provider + 假设 semantics=2 为包含。**没选**：
  违反「不伪造数据」硬约束，一旦口径错误会静默污染全部历史。
- **重新考虑条件**：TOKEN_SOURCE_CAPABILITY.md §二 列出的 5 条接入条件全部
  满足（semantics 权威口径、稳定 provider 映射、幂等导入、DeepSeek 覆盖
  确认、跨源去重方案）。

## D-010 缓存损坏条目整体拒绝（v0.2 修复 v0.1 CC 低危发现 2）

- **决定**：服务层读缓存前过 `services/validation.py` 严格验证（有限数值、
  0–100 范围、可解析时间、已知 provider）；任何损坏条目**整体丢弃**，
  不回退 0/0、不美化、不进 UI。
- **为什么**：v0.1 `cache._opt_window` 缺百分比默认 0/0，会把损坏数据冒充
  真实数值。
- **同时修复**：v0.1 CC 低危发现 1（`reset_description` 未过 `_scrub`）——
  v0.2 详情页 extras 只保留 id/title/used_percent 白名单字段，
  `reset_description` 自由文本完全不进入桌面 UI。

---

## v0.3 新增决策（2026-09-12，依据 TOKEN_MONITOR_V3_CC_PLAN.md）

## D-011 v3 UI：纵向四行宽卡 + “已用”语义 + DeepSeek 金额卡

- **决定**：
  1. 四宫格改为四个纵向排列的宽卡（Codex / z.ai GLM / MiniMax / DeepSeek），
     默认窗口 1440×1080（夹紧到可用工作区）、最小 960×640，主纵向滚动区；
     前三家卡内双列（五小时 / 本周），内容宽 <800px 时上下排列。
  2. **进度条填充统一表示已用**：主数字“X% 已用”直接取
     `snapshot.used_percent`（归一化层唯一翻转入口的产物），适配层与组件层
     **不做第二次换算**；缺失显示“暂无数据”，不显示 0%、不画空条。
  3. DeepSeek 独立金额卡：累计充值 + 当前余额 + 全宽已用金额条；
     U = T − B、P = U/T 仅在 T>0、0≤B≤T、币种一致、无未拆分赠送余额时启用。
- **充值来源事实**：DeepSeek `GET /user/balance` 只返回当前余额三个分项
  （total / granted / topped_up），**没有累计充值字段**——topped_up_balance
  是“当前余额中的充值分项”，不是充值历史，不能当 T 用。因此提供可选手动
  登记 `config/topup.json`（用户据充值记录填写，默认空 = “待登记”），程序
  只读不写。
- **配色/尺寸**：页面 #101113、卡 #1B1C20、轨道 #2C2D34、填充 #286BFF、
  正文 #F1F2F5、辅助 #A8ADB8；条高 11、卡圆角 14、间距 18/24/30/32。
  低额度提示（剩余 ≤10%）沿用，但以“已用 ≥90%”解释。
- **数据层增量**：`model.Balance` 增加 `granted_balance` / `topped_up_balance`
  可选字段（默认 None、to_dict 仅非 None 输出，向后兼容），deepseek reader /
  cache / validation 同步透传 —— 仅用于口径核对提示，不改 value/currency 语义。
- **验证**：`tests/test_v3_ui.py` 19 项（方案三条已用转换样例、0%/100%/缺失、
  T=100/B=64.80→¥35.20·35.2%、T=B→0%、B=0→100%、缺 T / 零 T / 余额>充值 /
  币种不一致 / 赠送余额均不伪造比例）；`tools/v3_ui_check.py` UI 层断言 +
  截图（测试数据内存注入，不写真实账户数据）。

### D-011 补充（同日反馈）：卡片高度随窗口自由伸缩

- **反馈**：固定卡高（≥210 DIP）在缩小窗口时内容被裁切。
- **修改**：四张服务卡改为 `Expanding` 高度策略（下限：额度卡 ~181 / 金额卡
  ~207，由内容决定）——窗口大时四卡平分可用高度恰好占满（1440×1080 实测
  无滚动条）；窗口缩小时卡片先压缩，压缩到下限后滚动区才出滚动条兜底。
- **验证**：`tools/v3_ui_check.py` 尺寸矩阵（1440×1080 / 1489×712 / 960×640）
  + 真机截图 `docs/screenshots/v3_adapt_*.png`；399 项离线测试回归通过。

### D-011 补充 2（同日反馈）：竖向窗口大小可自由拖动

- **反馈**：窗口左右可拖、上下拖不动。
- **根因**：竖向下限被两层约束顶在 640 DIP —— ① 显式最小尺寸 960×640
  （方案推荐值，按用户需求放宽）；② 详情/历史页内容最小高度（~446）经
  页面栈抬高整窗下限。窗口在最小值附近时上下拖动几乎无余量，感觉"不可调"。
- **修改**：显式最小尺寸放宽为 960×420；详情/历史页各自包进滚动容器，
  内容不再抬高整窗竖向下限。竖向可调范围现为 420 DIP 起、无上限；
  内容显示继续由滚动区自适应兜底。
- **验证**：resize 1080/712/500/420 全部精确生效；Esc 导航正常；
  399 项离线测试通过。

### D-011 补充 3（同日反馈）：窗口高度不足时切紧凑排版，让四家尽量同屏可见

- **反馈**：缩小窗口后只能看到两张卡，其他要滚动才出现。
- **根因**：常规排版字号（主数字 27pt / 卡片内边距 24 / 行距 12 / 卡间距 18 / 条高 11）
  对应的卡高下限约 190 DIP；四张 + 间距 + 足迹 + 头部 ≈ 990 DIP，1400×712 的窗口
  只能看到前两家。
- **修改**：
  1. 新增 `theme.COMPACT` 常量集（字号/间距/边距/条高/卡下限的紧凑值）。
  2. 每个卡片 / 列 / 进度条 / 足迹条加 `set_compact(bool)`，切换小排版。
  3. `MainWindow.resizeEvent` 监听窗口高度：低于 1000 DIP 切紧凑；
     调页面外边距 / 卡间距 / 进度条高度 / 字号，传递到四张卡 + 金额卡 + 足迹条。
- **验证**（各尺寸实测，scroll=滚动条最大范围）：
  - 1440×1080：紧凑关，卡高 209，scroll 0（四家全可见）。
  - **1466×712（用户尺寸）：紧凑开，卡高 136，scroll 0** —— 四张卡 + 足迹全部同屏。
  - 1489×632：紧凑开，卡高 120/136，scroll 32。
  - 960×640：紧凑开，卡高 120/136，scroll 24。
  - 960×500：scroll 164（极端矮，兜底滚动）。
  - 399 项离线测试通过。

## D-012 v0.4 UI 精修：自然高度卡 + 移除 compact 阈值 + 统一字体与汇总口径（2026-09-13）

依据：`docs/UI_POLISH_CC_BRIEF.md`（设计与验收要求，CODE_CHANGED 输入）。
本决策记录 v0.4 相对 v0.3 的全部 UI 变更；数据采集、权限、缓存、历史语义
与已用百分比判定不变。

### 背景问题（v0.3 代码证据）

1. 卡片以 `addWidget(card, 1)` 加入纵向布局且允许纵向扩展 → 多余高度被
   分进卡片，形成大片空白。
2. `resizeEvent` 以窗口高度 <1000 DIP 切换整套 compact 样式 → 上下拖动
   跨阈值时字号、间距、条高整体突变。
3. 数字统一 Consolas；MoneyCardRow 的"待登记"文字也走数字字体路径，回退
   不协调。
4. DeepSeek 未登记充值总额时保留空进度轨道，易被当成 0% 已用。
5. 顶部汇总只传三家额度服务 → 出现"3 家已更新"与四张更新卡不一致。

### 修改

1. **布局**：卡片 `stretch 0` + `Preferred` 纵向策略，取内容自然高度
   （额度卡实测 138 DIP，符合 128–144 目标）；`addStretch(1)` 沉底。
   页面内容最大宽 1120 DIP，滚动区 `AlignHCenter` 居中。删除 resizeEvent
   与 `theme.COMPACT` 全部逻辑；窗口变矮自然滚动，字号/间距/条高全程稳定。
2. **窗口**：初始 1080×700（默认高度取"首页内容自然高度 + 头部 + 余量"，
   保证完整出现），最小 720×460；尺寸保存于 `data/window.json`（gitignore），
   恢复时夹紧到当前可用工作区。
3. **额度区**：改为"周期标签（左）+ 已用值（右）→ 6 DIP 细条 → 重置时间"；
   堆叠断点 620 DIP 按文本容纳宽度确定（720 最小宽下实测两列完整，
   不堆叠）；缺失时"暂无数据"走正文字体且不显示进度条。
4. **字体**：全局 Segoe UI + Microsoft YaHei UI 回退；数字用 `QFont.Tag
   ("tnum")` 等宽数字，删除 Consolas 路径。
5. **色板**：背景 #111317、卡 #1B1E24、填充蓝 #5B8DEF、轨道 #2A303A、
   链接 #9AB8FF、错误红 #F08C92（错误文字从琥珀改为柔和红，仍伴随文字）。
6. **DeepSeek 金额卡**：当前余额为主数字（28 DIP）；累计充值移为右侧辅助
   小字；未登记显示"累计充值未登记 · 可在 config/topup.json 登记后显示
   已用比例"；无有效比例时隐藏进度条（不留空轨道），不留白凑等高。
7. **汇总口径**：`_render_cards` 统一为四家构建 ProviderVM，汇总与四卡一致
   （修复"3 家已更新"）。
8. **卡头**：LIVE 且"采集刚刚"时不再与"已更新"重复显示，完整时间保留在
   tooltip。底部记录区改名"额度观察记录"，自然高度（33 DIP）。

### 验证摘要（详见 docs/UI_ACCEPTANCE_RESULTS.md v0.4 节）

- 离线 401 passed（v0.3 全量继承；`test_run_cli` 1 例 GBK 控制台解码失败
  为 v0.3 既有环境问题，与本次修改无关）。
- `tools/v4_ui_check.py` 全部通过：正常/失败/缺失/待登记/口径不一致渲染、
  高窗口空余沉底（卡高 138 不变）、矮窗口滚动 + 字号不变、窄窗口无截断、
  堆叠断点、四家汇总口径。
- 真实启动冒烟（`tools/real_launch_smoke.py`）：四家真实采集成功，滚动 0。
- QT_SCALE_FACTOR 1.25/1.5 模拟通过；OS 级 125%/150% 缩放未在真机切换验证。

## D-013 v0.4.1 第二轮精修：去详情入口、额度条变色、钱包卡、清理占位（2026-09-13）

依据：用户第二轮指令（覆盖 DESIGN.md v0.4 相应条款）。只做 UI 与金额展示
必需的口径处理；采集系统未动。

1. **详情入口删除**：首页所有"详情 ›"移除；卡片不再 emit clicked、无手形
   光标、focusPolicy=NoFocus、paintEvent 去掉悬停高亮与焦点描边，保留安静
   细边框。DetailPage 代码保留但无入口；刷新/历史按钮焦点不变。
2. **额度条**：高 12 DIP 胶囊（轨道/填充同圆角）；新增 `_usage_color()` 按
   已用百分比在锚点 (0,#75C9B2)/(50,#729FF0)/(80,#DFB777)/(100,#E38D91) 间
   线性插值。`set_used(warning)` 参数保留兼容但不再切换颜色；"额度较低"
   文字提示与 v0.2 告警阈值不变。0% 空填充、小比例不放大、100% 不越界。
3. **DeepSeek 钱包卡**：只保留 服务名+简短状态+并排"钱包剩余/已花费"。
   adapter.MoneyVM 改为 {state, balance_text, spent_text, spent_hint,
   balance_time_text}；compute_money 口径闸门不变。已花费仅 MO_OK 时按
   累计充值−余额计算（上游无消费累计字段），否则"—"+悬停口径说明
   （theme.SPENT_HINT）。删除首页充值说明/配置路径/额度条。
4. **底部清理**：删除"Token 统计尚未接入"；不新增装饰。
5. **验证**：402 passed；`tools/v4_ui_check.py` 扩展为 v0.4.1 断言（颜色
   锚点插值、条像素采样 0/1/50/80/100%、钱包口径、无详情入口、缩放）；
   真实启动冒烟通过；QT_SCALE_FACTOR 1.25/1.5 模拟通过。

## D-014 v0.4.2 额度条换色：极光渐变（2026-09-13，用户指令）

- **问题**：v0.4.1 的填充色锚点 蓝(#729FF0)→琥珀(#DFB777) 在 RGB 线性插值下，
  约 60–80% 区间数学上必然经过去饱和灰色（用户截图 66% 处灰白，否决）。
- **方案**：用户在三个候选中选定「极光渐变」——锚点改为
  0% #34D399（翡翠绿）/ 50% #60A5FA（亮蓝）/ 75% #A78BFA（亮紫）/ 100% #F87171
  （珊瑚红）。相邻锚点色相相邻过渡，RGB 插值全程饱和、无灰色段；
  插值算法（widgets._usage_color）与告警语义不变。
- **验证**：自检新增"无灰色段"断言（全色带每 5% 采样，饱和度下限）；
  条像素采样 80% = #B786DF（鲜紫，不再发灰）；402 passed；
  真实启动截图 v42_home_real_aurora.png（86% 呈鲜艳玫红）；
  QT_SCALE_FACTOR 1.25/1.5 模拟通过。
