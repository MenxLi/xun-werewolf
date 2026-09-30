"""局末复盘：神视角全文交给一个独立 agent，生成中文 markdown 教练报告。"""
from __future__ import annotations

from pathlib import Path

from xun import Agent, NullDisplay, ToolBox, Workspace
from xun.config import load_config

from ..engine.events import GM, K_WOLF_VOTE, PUBLIC
from ..engine.state import GameState

REVIEW_SYSTEM = (
    "你是一位资深狼人杀教练，任务是复盘一局对局。"
    "你只依据学员提供的对局记录发言，不会编造记录里没有的信息；"
    "你用中文、简洁、直接给结论，输出 markdown。"
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


def wolf_vote_lines(state: GameState) -> list[str]:
    """每晚狼队投了几轮、票型散不散、最后刀了谁。

    单列出来是因为下面的对局记录只保留最后 `max_lines` 条：不单独给一份，
    前几夜的表决（往往正是狼队配合最差的那几夜）会被截掉。
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
    def __init__(self, model_name: str, *, workdir: str | Path) -> None:
        cfg = load_config().clone()
        cfg.auto_confirm = False
        cfg.enable_extensions = False
        cfg.model.name = model_name   # 只覆盖模型名；温度等其余参数照用户 xun 配置
        self.agent = Agent(
            name="复盘教练", display=NullDisplay(), config=cfg, toolbox=ToolBox(),
            workspace=Workspace(workdir=Path(workdir)),
        ).initialize()

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
        self.agent.system(REVIEW_SYSTEM)
        self.agent.instruct(PROMPT + "\n\n=== 对局记录 ===\n" + self.transcript(state), _emit_event=False)
        result = self.agent.execute(max_iterations=1)
        if result.is_err():
            raise RuntimeError(f"复盘失败：{result.value}")
        return "## 复盘报告\n\n" + str(result.unwrap())

    def finalize(self) -> None:
        try:
            self.agent.finalize()
        except Exception:
            pass
