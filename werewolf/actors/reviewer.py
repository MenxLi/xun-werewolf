"""局末复盘 + 局后讨论：上帝视角全文交给一个独立 agent，一局一个，建好就留着。

留着是因为讨论靠会话记忆：每问一句就把整局记录重发一遍，模型是在"重新读题"而不是
"接着上次聊"。修剪也和座位那边相反 —— 座位可以剪老发言（前情提要每轮重发），教练不行：
学员追问的正是「第 2 天 3 号说了什么」，所以 `trim_conversation` 钉住开头两条（system +
对局记录），只剪后面的老问答；auto_compact 一并关掉，它会把那份记录连 system 一起摘要掉。
"""
from __future__ import annotations

from pathlib import Path

from xun import Agent, NullDisplay, ToolBox, Workspace
from xun.config import load_config

from ..engine.events import GM, K_WOLF_VOTE, PUBLIC
from ..engine.state import GameState
from .llm_player import current_system, hard_char_budget, rebase_system, trim_conversation

REVIEW_SYSTEM = (
    "你是一位资深狼人杀教练。学员刚打完一局，你手里有这局的**上帝视角**对局记录"
    "（含夜间行动与所有私密信息）。你的活分两段：先生成一份复盘报告，"
    "然后就这一局和学员继续讨论 —— 他会问「我那句该怎么说」「5号为什么投他」这类问题，"
    "你直接答，不必重来一遍报告。"
    "你只依据对局记录发言，记录里没有的就说不知道，绝不编造；"
    "用中文、简洁、口语，输出 markdown。"
)

PROMPT = """你是狼人杀教练。下面是一整局的**上帝视角**记录（含夜间行动与所有私密信息）。

请用中文输出一份 markdown 复盘报告，结构如下：
1. **结果与转折点**：指出具体天数与号位，说明哪一步决定了胜负。
2. **人类玩家复盘**（若存在，见“真人玩家”一行）：分「发言有效性 / 逻辑漏洞 / 抿身份准确度 / 投票与技能时机」四点评，每点都要给“本可以怎么做”的具体替代方案。
3. **其他玩家表现**：一行一个，点名最像狼的人和最会带队的好人。
4. **给你的 3 条改进建议**：可立刻在下一局使用。

要求：不复述整局记录，不要编造记录里没有的信息，总长控制在 700 字内。
如果记录里有「狼队表决」那几行，用它评狼队配合：同一夜投到第 3 轮才空刀、或者每轮票都散开，
都是狼队的实际问题，要点名说；反过来，一轮就定刀口说明狼队抿得准。"""

TRANSCRIPT_HEAD = "=== 对局记录 ===\n"

FOLLOWUP_NOTE = ("学员在复盘之后继续追问这一局。只依据上面的对局记录回答这一个问题："
                 "别重写报告、别复述整局、不超过 200 字，直接给答案；"
                 "记录里没有的信息就明说不知道。")


def wolf_vote_lines(state: GameState) -> list[str]:
    """每晚狼队投了几轮、票型散不散、最后刀了谁。

    单列出来是因为对局记录只保留最后 `max_lines` 条：不单独给一份，前几夜的表决
    （往往正是狼队配合最差的那几夜）会被截掉。
    """
    lines: list[str] = []
    for night in state.nights:
        if not night.wolf_rounds:
            continue
        rounds = [event.text.split("：", 1)[1] for event in state.log
                  if event.kind == K_WOLF_VOTE and event.day == night.day]
        result = "空刀" if night.wolf_target is None else f"刀 {night.wolf_target} 号"
        lines.append(f"第{night.day}夜（投 {night.wolf_rounds} 轮 → {result}）" +
                     ("：" + " / ".join(rounds) if rounds else ""))
    return lines


class Reviewer:
    # workdir 必填，理由同 `llm_player`：默认的 mkdtemp 不在 root_dir 下，cleanup 收不掉
    #: 讨论会话的滚动窗口；开头那两条不在窗口里（见 `_head`）
    keep_pairs = 10

    def __init__(self, model_name: str, *, workdir: str | Path) -> None:
        cfg = load_config().clone()
        cfg.auto_confirm = False
        cfg.enable_extensions = False
        cfg.model.name = model_name   # 只覆盖模型名；温度等其余参数照用户 xun 配置
        cfg.auto_compact.enabled = False          # 理由见模块 docstring
        self.char_budget = hard_char_budget(cfg)
        self.agent = Agent(
            name="复盘教练", display=NullDisplay(), config=cfg, toolbox=ToolBox(),
            workspace=Workspace(workdir=Path(workdir)),
        ).initialize()
        self.static_system = ""      # 整局只写一次的 system（被盖掉才接回来）
        self.loaded = False          # 对局记录喂进去了没有（复盘或第一次追问时喂，只喂一次）
        self._head = 1               # 开头钉住的条数：喂过记录之后是 system + 记录 = 2

    def _ensure_system(self) -> None:
        if not self.static_system:
            self.static_system = REVIEW_SYSTEM
            self.agent.system(REVIEW_SYSTEM)
            return
        rebuilt = rebase_system(current_system(self.agent.conversation), self.static_system)
        if rebuilt is not None:
            self.agent.system(rebuilt)

    def _answer(self, text: str) -> str:
        """问一句答一句；失败就抛，由法官决定怎么告诉用户（不许把话吞掉）。"""
        self._ensure_system()
        self.agent.instruct(text, _emit_event=False)
        trim_conversation(self.agent.conversation, self.char_budget, self.keep_pairs, self._head)
        result = self.agent.execute(max_iterations=1)
        if result.is_err():
            raise RuntimeError(f"复盘教练没答上来：{result.value}")
        return str(result.unwrap()).strip()

    def _record(self, state: GameState) -> str:
        """对局记录，只喂一次；返回要拼在问题前面的那段（第二次起是空串）。"""
        if self.loaded:
            return ""
        self.loaded = True
        self._head = 2                 # system + 这条带记录的消息，都不许剪
        return "\n" + TRANSCRIPT_HEAD + self.transcript(state) + "\n\n"

    @staticmethod
    def transcript(state: GameState, max_lines: int = 320) -> str:
        lines = [
            f"板子：{state.config.describe().splitlines()[0]}",
            *[f"规则：{line}" for line in state.config.flags.summary_lines()],
            "身份：" + "、".join(
                f"{p.seat}号={p.role_name}{'（真人玩家）' if p.is_human else ''}"
                for p in sorted(state.players.values(), key=lambda p: p.seat)
            ),
            f"真人玩家：{'无' if state.human_seat is None else str(state.human_seat) + '号'}",
            *[f"狼队表决：{line}" for line in wolf_vote_lines(state)],
            "",
        ]
        events = state.log[-max_lines:] if len(state.log) > max_lines else state.log
        for event in events:
            if event.audience == PUBLIC:
                tag = "公开"
            elif event.audience == GM:
                tag = "上帝"
            else:
                tag = "私密->" + "、".join(str(s) for s in sorted(event.audience))
            lines.append(f"[第{event.day}天][{tag}] {event.text}")
        return "\n".join(lines)

    def review(self, state: GameState) -> str:
        """整局复盘报告。一局一次；之后的追问都走 `ask`（记录由 `_record` 认账，不重发）。"""
        report = self._answer(PROMPT + "\n" + self._record(state))
        return "## 复盘报告\n\n" + report

    def ask(self, question: str, state: GameState) -> str:
        """局后讨论：接着会话记忆回答一句。

        复盘没生成（或生成失败）也得能讨论，所以记录没喂过就顺手在这儿喂 —— 拼在同一条
        user 消息里，不额外多一次模型调用。
        """
        lead = self._record(state)
        return self._answer(lead + FOLLOWUP_NOTE + "\n\n学员的问题：\n" + question.strip())

    def finalize(self) -> None:
        try:
            self.agent.finalize()
        except Exception:
            pass
