"""脚本化玩家：只用于引擎单测，不发任何 LLM 请求。"""
from __future__ import annotations

import re
import random
from ..engine.ask import Ask, KINDS
from ..engine.decisions import (
    Ballot, Candidacy, SeerCheck, Shot, SpeakOrder, Speech, Transfer, WitchAction, WolfProposal, Withdraw,
)


def _tally_leads(note: str) -> list[int]:
    """从题面里的票型文本读出**领先的那一挡**（可能并列；法官给的是 `5号 2票、9号 1票`）。

    返回并列领先的一整挡，不替演员挑一个：票型文本按编号升序排，谁先出现谁就赢 ——
    那正是刚被删掉的「平票取编号最小」，别把它抄进演员身上。
    """
    parts = (note or "").split("票型：", 1)
    if len(parts) < 2:
        return []
    counts = re.findall(r"(\d+)号\s*(\d+)票", parts[1])
    if not counts:
        return []
    top = max(int(n) for _seat, n in counts)
    return [int(seat) for seat, n in counts if int(n) == top]


class FakeActor:
    """只依据 Ask 里可见的信息行动（拿不到 GameState，天然不会越权）。"""

    is_human = False

    def decide(self, ask: Ask):
        """按环节分派到下面这些方法：脚本玩家和测试都要按环节 patch（`kind → 方法名`
        就记在 `KINDS.method` 里），所以不像另外两个演员那样塌成一个实现。"""
        return getattr(self, KINDS[ask.kind].method)(ask)

    def __init__(self, seat: int, rng: random.Random, error_rate: float = 0.0) -> None:
        self.seat = seat
        self.rng = rng
        self.error_rate = error_rate

    # ---- 内部 ---------------------------------------------------------------
    def _roll(self) -> None:
        if self.error_rate and self.rng.random() < self.error_rate:
            raise RuntimeError("模拟玩家行动失败")

    def _pick(self, ask: Ask, allow_none: bool = False) -> int | None:
        if not ask.pool:
            return None
        if allow_none and self.rng.random() < 0.1:
            return None
        return self.rng.choice(list(ask.pool))

    def _speech(self, ask: Ask, prefix: str = "") -> Speech:
        suspects = [s for s in ask.pool if s != self.seat]
        target = self.rng.choice(suspects) if suspects else None
        who = f"{target}号" if target else "场上的人"   # 没人可指的时候别说 'None号'
        lines = {
            "狼人": f"{prefix}我是好人，先听了一圈，{who}的发言最没营养，我先记住。",
            "预言家": f"{prefix}我查人思路清楚，今天先跟票 {who}，别装。",
            "村民": f"{prefix}我没信息，跟有信息的人走，先听 {who} 怎么说。",
        }
        text = lines.get(ask.identity, f"{prefix}过一下 {who} 的发言逻辑。")
        return Speech(text=text, stance=_stance(target))

    # ---- Actor 接口 ---------------------------------------------------------
    WOLF_REASONS = ("跳预的先走", "他能带队", "抿不准，刀活跃的", "别给他说话机会",
                    "留着他今晚就翻盘", "先刀话少的")

    def wolf_target(self, ask: Ask) -> WolfProposal:
        self._roll()
        # 看到票型就往领先的目标靠：脚本玩家若永远乱投，三轮表决夜夜空刀，
        # 测出来的平衡就不是狼队的平衡了。（8 成跟票，留 2 成坚持，免得表决永远一轮定。
        # 并列领先时随机挑一个 —— 这时候真人狼队也是掷硬币，不许偏向编号小的那个。）
        leads = [s for s in _tally_leads(ask.note) if s in ask.pool]
        target = (self.rng.choice(leads) if leads and self.rng.random() < 0.8
                  else self._pick(ask))
        return WolfProposal(target=target, reason=self.rng.choice(self.WOLF_REASONS))

    def witch_action(self, ask: Ask) -> WitchAction:
        self._roll()
        save = bool(ask.save_pool) and self.rng.random() < 0.6
        poison = None if save or self.rng.random() > 0.35 else self._pick(ask, allow_none=True)
        return WitchAction(save=save, poison_target=None if save else poison, reason="按局势")

    def seer_check(self, ask: Ask) -> SeerCheck:
        self._roll()
        return SeerCheck(target=self._pick(ask) or ask.pool[0], reason="先抿一个")

    def candidacy(self, ask: Ask) -> Candidacy:
        self._roll()
        persona = ask.persona
        run = bool(getattr(persona, "run_for_sheriff", False)) if persona is not None else self.rng.random() < 0.4
        return Candidacy(run=run, reason="想拿警徽" if run else "不想出头")

    def sheriff_speech(self, ask: Ask) -> Speech:
        self._roll()
        return self._speech(ask, prefix="警上我讲三点：")

    def withdraw(self, ask: Ask) -> Withdraw:
        self._roll()
        return Withdraw(withdraw=self.rng.random() < 0.12)

    def sheriff_vote(self, ask: Ask) -> Ballot:
        self._roll()
        return Ballot(target=self._pick(ask, allow_none=True))

    def speak_order(self, ask: Ask) -> SpeakOrder:
        self._roll()
        return SpeakOrder(direction=self.rng.choice([1, -1]))

    def speech(self, ask: Ask) -> Speech:
        self._roll()
        return self._speech(ask)

    def vote(self, ask: Ask) -> Ballot:
        self._roll()
        return Ballot(target=self._pick(ask, allow_none=True))

    def last_words(self, ask: Ask) -> Speech:
        self._roll()
        return self._speech(ask, prefix="遗言：")

    def shot(self, ask: Ask) -> Shot:
        self._roll()
        return Shot(target=self._pick(ask, allow_none=True))

    def transfer(self, ask: Ask) -> Transfer:
        self._roll()
        return Transfer(target=self._pick(ask, allow_none=True))

    def finalize(self) -> None:
        pass


def _stance(target: int | None):
    from ..engine.decisions import Stance
    return Stance(suspected=[target] if target else [], support=None, note="先观察")
