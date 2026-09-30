"""法官的规则式回答：不叫模型，只看当前局面，回一条确定性的 info。

法官手里没有 LLM（见 `host.py`），所以用户问什么都不会触发模型；这也意味着法官
被话术套出私密信息在结构上不可能发生 —— 它能读到的只有 `board_summary(..., god_view)`
那一层，而 god_view 只在人类玩家已出局且开局选了「开上帝视角」时才为真。
"""
from __future__ import annotations

from typing import Sequence

from ..engine.state import GameState
from . import render

RULE_WORDS = ("规则", "板子", "怎么玩", "怎么赢", "胜负", "屠边", "屠城", "遗言", "开枪",
              "自救", "解药", "毒药", "查验", "警徽流", "平票", "几票",
              "白痴", "女巫", "猎人", "狼王", "守卫", "归票", "发言顺序", "rule")
#: 明确在问“现在什么情况”——这类问题优先于规则解释
STRONG_STATUS_WORDS = ("活着", "存活", "谁在", "死了", "出局", "票型", "几人", "局面", "状态", "警徽")

STATUS_WORDS = ("局面", "状态", "活着", "存活", "还有谁", "谁在", "死了", "出局", "票型",
                "票", "警徽", "警长", "现在", "情况", "几人", "board", "status")
HELP_WORDS = ("帮助", "怎么用", "怎么玩这个", "提示", "该我", "我该", "做什么", "干嘛",
              "点哪里", "卡片", "按钮", "help", "指挥")
IDENTITY_WORDS = ("身份", "谁狼", "谁是狼", "几号狼", "狼是谁", "复盘", "结果", "谁赢", "报告")

USAGES = ("我能回答这几类（都只用公开信息）：\n"
          "· 局面：还有谁活着 / 谁死了 / 票型 / 警徽在谁手里\n"
          "· 规则：这个板子怎么玩、胜负判定、警徽与遗言规则\n"
          "· 操作：现在该点什么、卡片怎么用\n"
          "要查身份只有两种时候：你自己知道的事，或者这局打完了。")


def answer(state: GameState | None, god_view: bool, texts: Sequence[str]) -> str:
    """把用户对法官说的话归到四类之一，给出回答。"""
    text = " ".join(t.strip() for t in texts if t and t.strip())
    if not text:
        return USAGES
    low = text.lower()

    hits = lambda words: any(w in low or w in text for w in words)

    if hits(HELP_WORDS):
        return _help(state)
    if hits(IDENTITY_WORDS):
        return _identity(state, god_view)
    if hits(RULE_WORDS) and not hits(STRONG_STATUS_WORDS):
        return _rules(state)
    if hits(STATUS_WORDS) or not state:
        return _status(state, god_view)
    if state and state.finished:
        return _identity(state, god_view)
    return USAGES


def _help(state: GameState | None) -> str:
    lines = ["这个会话现在就是法官。三种输入方式，各管各的：",
             "· 选人选动作（投票、夜里行动、举手、警徽、女巫用药）：聊天区会弹出**带按钮**的卡片，"
             "点按钮选中再点「提交」才算数；这一步在输入框里打字不算回答。",
             "· 轮到你说话（发言、警上发言、遗言）：也播一张卡，但那张卡没有按钮也没有输入框，"
             "只告诉你轮到谁、排第几；话直接发在这条会话的输入框里，法官原样交给桌上播报。",
             "· 问法官（局面、规则、复盘）：同一个输入框，但只在**没轮到你的时候**算提问 —— "
             "轮到你发言时，输入框里的第一条会被当成那句发言。想随时查局面用 /werewolf status，"
             "它任何时刻都可用，也不会被当成发言。回答只看公开信息，身份一律不给。"]
    if state:
        lines.append(f"现在是第 {state.day} 天，阶段 {state.phase}；没轮到你时看别人发言就好。")
    return "\n".join(lines)


def _status(state: GameState | None, god_view: bool) -> str:
    return render.board_summary(state, god_view=god_view)


def _rules(state: GameState | None) -> str:
    if state is None:
        return "还没开局，等法官问你要哪个板子。"
    flags = state.config.flags
    lines = [f"板子：{state.config.role_names()}"]
    lines += ["· " + line for line in flags.summary_lines()]
    return "\n".join(lines)


def _identity(state: GameState | None, god_view: bool) -> str:
    if state is None:
        return "还没开局，没有身份可说。"
    if state.finished:
        return "局已终，全部公开：\n" + render.board_summary(state, god_view=True)
    if god_view:
        return "上帝视角（你已出局）：\n" + render.board_summary(state, god_view=True)
    return ("活着的局里我不能说身份 —— 我也只知道公开信息："
            "谁跳了什么、票怎么飞的、谁翻过牌。\n要这个就说「局面」。")
