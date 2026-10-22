"""v0.4 视觉常量（依据 docs/UI_POLISH_CC_BRIEF.md 视觉合同；v0.4 唯一来源）。

v0.4.1 修订（2026-09-13 第二轮精修，用户指令覆盖此前条款）：
- 首页不再有"详情 ›"入口；卡片非交互（安静细边框，无手形光标/焦点描边）。
- 额度条高 12 DIP 胶囊形，填充色按已用百分比在锚点间连续插值
  （0% 青绿 → 50% 蓝 → 80% 琥珀 → 100% 珊瑚红）；仅视觉映射，
  不改变低额度告警文案规则。
- DeepSeek 改为简洁钱包卡：钱包剩余 + 已花费并排；无可靠花费口径时
  显示"已花费 —"，首页不再堆配置说明（说明放悬停提示）。
- 底部观察记录区不再显示"Token 统计尚未接入"开发状态。

v0.4 布局语言：安静、清晰的个人用量仪表盘。四行纵向宽卡，卡片取内容自然
高度，多余空间留在内容末尾；**字号与间距不随窗口高度缩放**（v0.3 的
1000 DIP compact 阈值已移除，窗口变矮时自然出现纵向滚动条）。

**填充始终表示已用**，轨道未填充部分表示剩余。0% 保持空填充；无数据不画
填充、不显示伪造百分比。

对比度（相对卡片 #1B1E24，按 WCAG 相对亮度计算）：正文 #EEF1F5 ≈ 14.5:1；
辅助 #A8B0BD ≈ 7.4:1；链接 #9AB8FF ≈ 8.3:1；状态绿/琥珀/红均 ≥ 6:1。
轨道 #2A303A 仅作进度条底 / 装饰分隔，不作交互边界（刷新/历史等实际按钮
边界沿用 control-border，焦点沿用 accent 蓝）。
"""

from __future__ import annotations

# ---- v0.4 语义颜色 ----
CANVAS = "#111317"             # 页面底色
SURFACE = "#1B1E24"            # 服务卡
SURFACE_HOVER = "#22262E"      # 悬停（v0.4.1：卡片不再可交互，仅历史页等控件用）
DIVIDER = "#2B3039"            # 卡片边线 / 装饰分隔（低对比，禁止作交互边界）
TRACK = "#2A303A"              # 进度条轨道（低对比深灰，不发光、不荧光）
CONTROL_BORDER = "#74849A"     # 按钮 / 选择器边界（交互边界专用）
TEXT_PRIMARY = "#EEF1F5"
TEXT_SECONDARY = "#A8B0BD"
ACCENT = "#5B8DEF"             # 键盘焦点（柔和蓝）
LINK = "#9AB8FF"               # 文字链接（历史入口），与焦点蓝分工
ACTION_SURFACE = "#232B39"     # 刷新按钮底色
SUCCESS = "#87CDB1"            # 正常更新（低调绿；始终伴随文字说明）
WARNING = "#E8BC77"            # 部分缺失 / 低额度提醒（琥珀）
ERROR = "#F08C92"              # 更新失败（柔和红；始终伴随文字说明）

# 额度条填充色锚点（已用百分比 → 颜色；锚点间 RGB 连续插值，不到阈值突换。
# v0.4.2「极光渐变」：青绿 → 亮蓝 → 亮紫 → 珊瑚红，任意中间值保持鲜艳
# （旧方案蓝→琥珀的 RGB 插值中段必经灰色，已弃用）。纯视觉映射：
# 不用它替代告警文案，也不把用量包装成成就。）
USAGE_COLOR_ANCHORS = (
    (0.0, "#34D399"),
    (50.0, "#60A5FA"),
    (75.0, "#A78BFA"),
    (100.0, "#F87171"),
)

# 图表分类色（保留，仅历史图表身份用，不是健康色）
CHART_CATEGORY = {
    "codex": "#8AAFFF",
    "zai": "#B8A4D8",
    "minimax": "#79C2CB",
    "deepseek": "#C8B89D",
}

# ---- 尺寸（逻辑像素 / DIP；字号与间距不随窗口高度缩放） ----
WINDOW_DEFAULT = (1080, 700)   # 初始窗口，受可用工作区夹紧并留安全余量（app.py）
WINDOW_MIN = (720, 460)        # 最小窗口；再小则纵向滚动兜底
CONTENT_MAX_WIDTH = 1120       # 页面内容最大宽度，超宽窗口居中，不无限拉长进度条
MARGIN_OUTER = 20              # 页面左右留白 16–24
GAP = 12                       # 卡片间距 12
CARD_PADDING = 18              # 卡片内边距 16–20
HEADER_HEIGHT = 56             # 顶栏 52–56：标题 20 DIP，刷新与汇总正文尺寸
RADIUS_CARD = 12
RADIUS_BUTTON = 8
RADIUS_CONTROL = 6
BORDER_WIDTH = 1
BAR_HEIGHT = 12                # 进度条高 12（胶囊圆角 = 高度一半）
FOCUS_WIDTH = 2
COLUMN_STACK_THRESHOLD = 620   # 卡内容宽不足此值 → 两额度区上下排列（保证重置时间不截断）
CARD_INNER_GAP = 8             # 卡头与额度区 / 额度区与备注之间的行距
COLUMN_GAP = 24                # 两额度区水平间距
COLUMN_ROW_SPACING = 6         # 额度区内 标签行→条→重置 的行距

# ---- 字体（统一 Segoe UI + Microsoft YaHei UI 中文回退；无终端风格） ----
# 数字沿用同一无衬线字体，启用等宽数字（tnum）保证跳动时不抖动。
# px → pt = px × 0.75 @96dpi；pt 与 DIP 在 Windows 缩放下由 Qt 统一换算。
FONT_FAMILY = "Segoe UI"
FONT_FAMILY_FALLBACK = "Microsoft YaHei UI"

POINT_PAGE_TITLE = 15.0        # 页面标题 20 DIP（缩小品牌存在感）
POINT_CARD_TITLE = 12.75       # 服务名 17 DIP（16–18）
POINT_SECTION = 10.5           # 额度区标题 / 累计充值标签 14 DIP
POINT_MAIN_NUMBER = 19.5       # 核心已用百分比 26 DIP（24–28）
POINT_BALANCE = 21.0           # 余额主数字 28 DIP
POINT_BODY = 10.5              # 正文 14 DIP
POINT_SECONDARY = 9.75         # 辅助信息 13 DIP（12–13）

# ---- 阈值 ----
LOW_REMAINING_THRESHOLD = 10.0   # 剩余 ≤10%（已用 ≥90%）→ 沿用 v0.2 提示逻辑，以“已用”解释

# ---- 状态文案（沿用 v0.2 合同 + “上次数据”标注） ----
STATUS_TEXT = {
    "LIVE": "已更新",
    "CACHED": "已保存",
    "CACHED_EXPIRED": "待更新",
    "PARTIAL": "部分数据",
    "UNAVAILABLE": "暂不可用",
    "UNSUPPORTED": "不提供",
    "ERROR": "更新失败",
    "LOADING": "读取中",
    "REFRESHING": "刷新中",
}
STALE_DATA_TAG = "上次数据"
NO_DATA_TEXT = "暂无数据"

# 已花费口径说明（钱包卡"已花费 —"的悬停提示；不再在首页正文长期展示）
SPENT_HINT = {
    "REGISTER_PENDING": "暂无可信花费来源：累计充值未登记。可在 config/topup.json 登记 "
                        "total_topup，登记后按“累计充值 − 当前余额”计算花费（个人手动口径）",
    "NO_TOPUP": "暂无充值记录，无可计算的花费",
    "BALANCE_UNAVAILABLE": "余额暂不可用，暂不计算花费",
    "MISMATCH": "余额与登记充值口径不一致，暂不计算花费（详情见余额说明）",
}
