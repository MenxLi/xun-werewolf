"""看板卡片：xun 的 `HTMLInfoEvent`（带标题栏的卡片）+ 纯文本回退。

为什么要有这个模块：前端的 `InfoEvent` 是灰色小字**纯文本**，看板这种东西（座位状态、票型、
身份牌）排成文本只能靠全角空格硬对齐。`HTMLInfoEvent` 允许我们发 HTML（前端用 DOMPurify
清洗后渲染，内联 `style` 保留），于是能画出对齐的座位 chips、票型表、阵营配色。

关键约束（决定了这里的写法）：

1. **一个 Card = 一次播报 = 一个块**。前端每个事件都是一块带时间戳的卡片，按事件粒度发就
   会刷成屏 —— 所以 Part 只是卡的内部结构，不单独成事件。
2. **HTML 与纯文本从同一份 Part 生成**。`Part.html()` 给浏览器，`Part.text()` 给
   老版本 xun（没有 `HTMLInfoEvent`）、CLI 和单测断言。两者不会漂移，
   也不会出现「网页好看但降级散架」。
3. **只用降级后仍成行的标签**（`div`/`span`/`table`/`tr`/`td`/`br`）。xun 的
   `to_text()` 把这些闭合标签换成换行，所以文本形态天然是表格状的行；不能靠 CSS 排版。
4. **一切动态文本必须 `esc()`**：玩家发言、昵称、复盘文本都可能含 `<`，不能让它们拼进结构。
"""
from __future__ import annotations

import html as _htmlmod
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# ------------------------------------------------------------------ 模式探测
_HTML_INFO = None          # None = 未探测, False = 不支持


def html_supported() -> bool:
    """当前装的 xun 是否有 `HTMLInfoEvent`（较老的版本没有）；探测而不是比版本号。"""
    global _HTML_INFO
    if _HTML_INFO is None:
        try:
            from xun.display_abstract import HTMLInfoEvent          # noqa: F401
            _HTML_INFO = True
        except Exception:
            _HTML_INFO = False
    return _HTML_INFO


def mode() -> str:
    """`"html"` 或 `"text"`。`WEREWOLF_UI=text|html` 可强制（默认自动探测）。"""
    forced = (os.environ.get("WEREWOLF_UI") or "").strip().lower()
    if forced in ("html", "text"):
        return forced
    return "html" if html_supported() else "text"


# ------------------------------------------------------------------ 调色板
#: 低饱和、只承担"区分"职责。阵营色只在上帝视角出现（活人不该从配色里读出身份）。
C = {
    "alive": "#1b5e20",       # 存活：绿字
    "alive_bg": "#e8f2e9",
    "dead": "#8d8d8d",        # 出局：灰字 + 删除线
    "badge": "#8a6d1b",       # 警徽：土金
    "badge_bg": "#fbf3dd",
    "wolf": "#a83232",        # 以下三档仅上帝视角
    "god": "#1f5b8c",
    "villager": "#3f6b3f",
    "line": "#d8d4cc",
    "muted": "#6b6b6b",
    "bar": "#c9c3b8",
    # 身份牌说明条 / 横幅的底色（只给真人自己那张卡与终局卡用）
    "hero_wolf": "#fdeaea",
    "hero_god": "#e9f1f9",
    "hero_villager": "#eef4e8",
    "hero_plain": "#f3f0e9",
}

#: tone → 身份牌底色；字色沿用上面同名的 tone 色，两者成套改。
_HERO_BG = {"wolf": C["hero_wolf"], "god": C["hero_god"],
            "villager": C["hero_villager"], "muted": C["hero_plain"]}

_FONT = "font-size:13px;line-height:1.55;"
_CHIP = ("display:inline-block;padding:1px 7px;margin:1px 3px 1px 0;"
         "border:1px solid #cfcabf;border-radius:10px;white-space:nowrap;")


def esc(text: object) -> str:
    """所有动态文本过一遍：`<`/`&`/引号变实体，前端 DOMPurify 也就不用替我们背锅。"""
    return _htmlmod.escape(str(text if text is not None else ""), quote=False)


# ------------------------------------------------------------------ Parts
class Part:
    """卡片的一段。两种渲染必须来自同一份数据。"""

    def text(self) -> str:
        raise NotImplementedError

    def html(self) -> str:
        raise NotImplementedError


def _tone_style(tone: str) -> str:
    """tone → 内联样式。tone 是个小词表：alive / dead / badge / wolf / god / villager / muted。

    阵营色（wolf/god/villager）只出现在上帝视角 —— 活人不该从配色里读出身份。
    """
    if tone == "alive":
        return f"background:{C['alive_bg']};color:{C['alive']};border-color:#bcd6bf;"
    if tone == "dead":
        return f"color:{C['dead']};text-decoration:line-through;"
    if tone == "badge":
        return (f"background:{C['badge_bg']};color:{C['badge']};border-color:#e0cfa0;"
                f"font-weight:600;")
    return f"color:{C.get(tone, C['muted'])};"


@dataclass
class Chips(Part):
    """一行 chips：`prefix` + 若干标签 + `suffix`。局面看板、身份行、已出局、狼队友都用它。

    纯文本形态两种，都是今天的措辞：
      - `brackets=True`：`存活 8 人 [1·2·3]｜场上狼 2 匹｜警徽 3号`（座位看板，标签用 `·` 连）
      - 否则：`身份 1号狼人(活) 2号女巫(死)`（用 `sep` 连）
    """

    prefix: str
    items: Sequence[tuple[str, str]] = ()         # (标签, tone)
    suffix: str = ""
    sep: str = " "
    brackets: bool = False

    def text(self) -> str:
        labels = [t for t, _ in self.items] or ["无"]
        if self.brackets:
            return f"{self.prefix}[{'·'.join(labels)}]{self.suffix}"
        return f"{self.prefix}{self.sep.join(labels)}{self.suffix}"

    def html(self) -> str:
        muted = f'<span style="color:{C["muted"]};">'      # 说明文字用灰字，不抢内容的戏
        tail = f"{muted}{esc(self.suffix)}</span>" if self.suffix else ""
        chips = "".join(_chip(text, tone) for text, tone in self.items)
        return (f'<div style="{_FONT}">{muted}{esc(self.prefix)}</span>'
                f'{chips or "无"}{tail}</div>')


def _chip(label: str, tone: str) -> str:
    """纯数字标签补成「N号」（看板里光一个数字和「5号(第1天)」不协调）。"""
    if label.isdigit():
        label = f"{label}号"
    mark = "◆" if tone == "badge" else ""
    return f'<span style="{_CHIP}{_tone_style(tone)}">{mark}{esc(label)}</span>'


# ------------------------------------------------------------------ 图像资产
#: 图像随插件一起发布在 `werewolf/assets/` 下，按模块相对路径读取。
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
ART_DIR = ASSET_DIR / "roles"          # 七个角色一人一张身份卡
BANNER_DIR = ASSET_DIR / "banners"     # 终局胜负横幅
#: 有胜负横幅的结局。`draw` 与中途终止**没有**横幅 —— 给一场平局或者没打完的局
#: 发「胜利」横幅是撒谎，宁可不出图。
BANNERS = ("wolf", "good")
_ART: dict[str, str] = {}


def art(folder: Path, name: str) -> str:
    """读一张随包发布的 SVG（每个进程只读一次，之后走缓存）。

    为什么是**内联 SVG**而不是 `<img src="...">`：xun 的 web 服务不服务插件目录
    （没有静态资源路由），插件里的文件前端根本取不到；PNG 想显示只剩 base64 data URI
    一条路，一张几十 KB 还只有一个分辨率。内联 SVG 一张 1~3 KB、任意屏幕都清晰，
    前端那道 DOMPurify 默认清洗对它原样通过（实测 3.4.16：清洗前后逐字节相同）——
    所以资产刻意不用 `<style>`/`<use>`/`filter`/`xlink:href`/事件属性，有测试逐条守着。

    缺文件是打包事故，直接抛：悄悄发一张没有图的卡，玩家会以为自己看到的是完整卡面。
    """
    key = f"{folder.name}/{name}"        # roles 与 banners 里有同名文件（wolf.svg）
    text = _ART.get(key)
    if text is None:
        text = _ART[key] = (folder / f"{name}.svg").read_text(encoding="utf-8")
    return text


@dataclass
class RoleCard(Part):
    """真人自己那张身份牌：卡面图像 + 一行技能说明。

    图只存在于 HTML 形态，所以两形态各自把自己那套话说完整（谁也不比谁少）：
    - HTML：SVG 横条已经画了「几号 / 什么身份 / 哪一阵营 / 技能」，下面那条阵营色说明条
      只说 `hint`（横条上没有的那一句）—— 把横条里的话再抄一遍就是同一句话讲两遍；
    - 纯文本（老版本 xun / CLI / 单测断言）：没有图像，`╔═╗` 大字框 + `★` 必须
      把座位、身份、阵营、说明一条不落地说完。
    """

    seat: int
    role_id: str = ""
    role_name: str = ""
    faction: str = ""                     # "狼人阵营" / "好人阵营"
    tone: str = "muted"                   # wolf / god / villager
    note: str = ""                        # 一句话技能说明（文本形态用）
    hint: str = ""                        # HTML 形态的说明条；卡面画过的话这里不重复

    def text(self) -> str:
        rule = "═" * 42
        lines = ["╔" + rule,
                 f"║ ★ {self.seat} 号 · {self.role_name}"
                 + (f"（{self.faction}）" if self.faction else "")]
        if self.note:
            lines.append("║ " + self.note)
        lines.append("╚" + rule)
        return "\n".join(lines)

    def html(self) -> str:
        # 卡面上的座位号是占位符，这里填进去
        card = art(ART_DIR, self.role_id).replace("{{SEAT}}", str(self.seat))
        fg = C.get(self.tone, C["muted"])
        bg = _HERO_BG.get(self.tone, _HERO_BG["muted"])
        note = (f'<div style="margin:6px 0 0;padding:7px 11px;background:{bg};'
                f'border:1px solid {fg};border-left-width:5px;border-radius:6px;'
                f'font-size:13px;color:{fg};">{esc(self.hint or self.note)}</div>') \
                if (self.hint or self.note) else ""
        return f'<div style="margin:2px 0;">{card}{note}</div>'


@dataclass
class Banner(Part):
    """终局胜负横幅：一局只出现一次，值得占一块图。纯文本形态只占一行。"""

    LABEL = {"wolf": "狼人阵营胜利", "good": "好人阵营胜利"}

    winner: str = ""                      # 只接受 `BANNERS` 里的值

    def text(self) -> str:
        return f"★ {self.LABEL[self.winner]} ★"

    def html(self) -> str:
        return f'<div style="margin:2px 0;">{art(BANNER_DIR, self.winner)}</div>'


@dataclass
class KV(Part):
    """键值行。文本形态 `头：k1 v1｜k2 v2`（与引擎既有播报格式一致）。"""

    rows: Sequence[tuple[str, str]]
    head: str = ""
    emphasize: str = ""                     # 这个 key 的值加粗（如「死亡」）

    def text(self) -> str:
        body = "｜".join(f"{k} {v}" for k, v in self.rows)
        return f"{self.head}{body}" if self.head else body

    def html(self) -> str:
        cells = []
        for k, v in self.rows:
            weight = "font-weight:600;" if k == self.emphasize else ""
            cells.append(
                f'<td style="padding:1px 10px 1px 0;vertical-align:top;'
                f'color:{C["muted"]};white-space:nowrap;">{esc(k)}</td>'
                f'<td style="padding:1px 14px 1px 0;{weight}">{esc(v)}</td>')
        rows_html = "".join("<tr>" + "".join(cells[i:i + 4]) + "</tr>"
                            for i in range(0, len(cells), 4))
        head = f'<div style="{_FONT}font-weight:600;">{esc(self.head)}</div>' if self.head else ""
        return f'{head}<table style="{_FONT}border-collapse:collapse;"><tbody>{rows_html}</tbody></table>'


@dataclass
class Log(Part):
    """若干行原文（本节日志、规则要点）。文本用 \\n 连接。"""

    lines: Sequence[str]
    small: bool = False

    def text(self) -> str:
        return "\n".join(self.lines)

    def html(self) -> str:
        style = _FONT
        if self.small:
            style = "font-size:12px;line-height:1.5;color:%s;" % C["muted"]
        # 行内的 \n 也要断开：这个 part 的契约是"排版不靠 CSS"（见模块 docstring），
        # 而浏览器会把行内换行塌成空格 —— 于是「轮到你 + 私密信息 + 字数建议」三截话挤成一坨
        body = "<br>".join(esc(seg) for line in self.lines for seg in line.split("\n"))
        return f'<div style="{style}">{body}</div>'


@dataclass
class Tally(Part):
    """票型：每人一票 + 得票条形。文本保持今天的 `🗳 3号→6号　4号→7号`。"""

    pairs: Sequence[str]                    # 已排版的「3号→6号」
    counts: Sequence[tuple[str, int]] = ()  # 得票排序（含弃票）
    total: int = 0

    def text(self) -> str:
        return "🗳 " + "　".join(self.pairs)

    def html(self) -> str:
        voted = ("<div style=\"" + _FONT + "\">"
                 + "<br>".join(esc(p) for p in self.pairs) + "</div>")
        bars = []
        for label, n in self.counts:
            pct = 0 if not self.total else round(n * 100 / self.total)
            bars.append(
                f'<div style="{_FONT}margin-top:2px;">'
                f'<span style="display:inline-block;min-width:52px;">{esc(label)}</span>'
                f'<span style="display:inline-block;height:8px;width:{max(pct, 4)}px;'
                f'background:{C["bar"]};border-radius:3px;vertical-align:middle;"></span>'
                f'<span style="color:{C["muted"]};"> {n} 票</span></div>')
        head = (f'<div style="{_FONT}color:{C["muted"]};margin-bottom:3px;">得票</div>'
                if bars else "")
        return head + voted + "".join(bars)


# ------------------------------------------------------------------ Card
@dataclass
class Card:
    """一次播报 = 一张卡。`title` 走前端标题栏，正文是 Parts。

    - `rule=True`：纯文本形态给标题加 `━━ … ━━` 装饰（阶段块，沿用今天的样式）。
    - `soft_title`：**只在 HTML 里当标题用**的回退标题。一个阶段被发言气泡切成好几块时，
      后续块在纯文本里不需要重复阶段名（灰字省着用），但卡片没有标题栏会看不出属于哪一节。
    """

    title: str = ""
    parts: list[Part] = field(default_factory=list)
    rule: bool = False
    soft_title: str = ""

    # ---- 纯文本（老版本 xun / CLI / 断言）
    def text(self) -> str:
        body = [p.text() for p in self.parts if p.text().strip()]
        if not self.title:
            return "\n".join(body)
        head = f"━━ {self.title} ━━" if self.rule else f"【{self.title}】"
        return "\n".join([head, *body])

    # ---- HTML（前端）
    def html(self) -> str:
        return "".join(f'<div style="margin:2px 0;">{p.html()}</div>' for p in self.parts)

    def event(self):
        """发事件的 payload：新版 xun 用 HTMLInfoEvent，否则退回 InfoEvent。"""
        if mode() == "html":
            from xun.display_abstract import HTMLInfoEvent
            return HTMLInfoEvent(html=self.html(), title=(self.title or self.soft_title) or None)
        from xun.display_abstract import InfoEvent
        return InfoEvent(message=self.text())
