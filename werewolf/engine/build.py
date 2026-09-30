"""AskBuilder：可见性信息系统的唯一出口。

它把事件日志切成两部分交给玩家：
- `delta`：自该玩家上次决策以来新增的、他有资格看到的**逐字事件**；
- `recap`：更早事件的**结构化摘要**（死亡名单、已公开身份、警徽、票型、发言要点一行）。
它只提供该座位可见的内容，并且没有任何读取他人身份或他人私密信息的 API。
"""
from __future__ import annotations

from typing import Sequence

from .ask import Ask, word_limit_for
from .events import K_EXILE, K_IDIOT_REVEAL, K_LAST_WORDS, K_SHERIFF_ELECTED, K_SHERIFF_SPEECH, K_SPEECH, K_VOTE_RESULT
from .state import GameState
from .persona import Persona


#: 前情提要最多带多少条事件（票型、放逐、警徽流和发言都算）。条数 × 每条长度
#: ≈ 长期记忆窗口的大小，宁多勿少：这层压缩是有损的，但比让每个玩家每轮重读
#: 整条记录便宜得多。
RECAP_EVENTS = 24

#: 前情提要里一条发言的展示长度。宁可长一点：这条摘要是为了「记住之前说过什么」，
#: 掐成半句话反而让人得回去翻记录。
def _clip(text: str, limit: int = 90) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AskBuilder:
    def __init__(self, state: GameState, stances: dict[int, object] | None = None) -> None:
        self.state = state
        self.stances: dict[int, object] = stances if stances is not None else {}

    # ---- 摘要 -------------------------------------------------------------
    def recap(self, seat: int, upto: int) -> list[str]:
        state = self.state
        lines: list[str] = []
        alive = "、".join(f"{s}号" for s in state.alive_seats())
        dead = [s for s in state.dead_seats() if s != seat]
        dead_text = "、".join(f"{s}号(第{state.players[s].died_on_day}天)" for s in dead) or "无"
        lines.append(f"存活：{alive}；已出局：{dead_text}")

        badge = state.badge_holder()
        if state.sheriff_elected:
            if badge:
                lines.append(f"警徽：{badge}号持有")
            elif state.badge_destroyed:
                lines.append("警徽：已被撕毁，本局不再有警长")
            else:
                lines.append("警徽：竞选已结束但未产生警长")
            if state.sheriff is not None and not state.players[state.sheriff].alive and not state.badge_destroyed:
                lines.append(f"（{state.sheriff}号是已出局的原警长，警徽待移交）")

        revealed = [p for p in state.players.values() if p.role_revealed]
        if revealed:
            lines.append("已公开身份：" + "、".join(f"{p.seat}号={p.role_name}" for p in sorted(revealed, key=lambda p: p.seat)))

        speeches: list[str] = []
        for event in state.log[:upto]:
            if not event.seen_by(seat):
                continue
            if event.kind == K_EXILE:
                lines.append(f"第{event.day}天放逐：{event.payload.get('seat')}号")
            elif event.kind == K_VOTE_RESULT:
                lines.append(f"第{event.day}天票型：{event.text}")
            elif event.kind in (K_IDIOT_REVEAL, K_SHERIFF_ELECTED):
                lines.append(f"第{event.day}天：{event.text}")
            elif event.kind in (K_SPEECH, K_LAST_WORDS, K_SHERIFF_SPEECH):
                speeches.append(f"第{event.day}天 {event.payload.get('seat')}号：「{_clip(event.payload.get('words', event.text))}」")
        lines.extend(speeches[-RECAP_EVENTS:])
        return lines

    def dossier(self, seat: int) -> list[str]:
        """私密档案：每轮原样重述，防止模型忘记自己的关键私有信息。"""
        state = self.state
        player = state.players[seat]
        lines: list[str] = []
        if player.seer_checks:
            done = "、".join(f"{s}号={r}" for s, r in sorted(player.seer_checks.items()))
            lines.append(f"【查验记录】{done}")
        if player.role_id == "witch":
            lines.append(
                "【用药】解药" + ("已用" if player.heal_used else "未用")
                + "，毒药" + ("已用" if player.poison_used else "未用")
            )
        if player.is_wolf:
            pack = [s for s in state.players if state.players[s].is_wolf and s != seat]
            dead_pack = [s for s in pack if not state.players[s].alive]
            lines.append("【狼队友】" + ("、".join(f"{s}号" for s in sorted(pack)) or "你是唯一的狼"))
            if dead_pack:
                lines.append("【已出局的狼队友】" + "、".join(f"{s}号" for s in dead_pack) + "（不再参与狼队讨论）")
            nights = [n for n in state.nights if n.wolf_rounds]
            if nights:
                lines.append("【狼队表决】" + "、".join(
                    f"第{n.day}夜 " + ("空刀" if n.wolf_target is None else f"{n.wolf_target}号")
                    + (f"（{n.wolf_rounds}轮）" if n.wolf_rounds > 1 else "")
                    for n in nights))
        stance = self.stances.get(seat)
        if stance is not None:
            suspected = "、".join(f"{s}号" for s in getattr(stance, "suspected", ()) or ()) or "暂无"
            support = getattr(stance, "support", None)
            note = getattr(stance, "note", "") or ""
            lines.append(
                f"【你上一轮的立场】 suspected={suspected}；"
                + (f"你倾向信任 {support}号；" if support else "你还没有明确站边；")
                + (note if note else "")
            )
        return lines

    # ---- 组装 --------------------------------------------------------------
    def build(
        self,
        seat: int,
        kind: str,
        *,
        pool: Sequence[int] = (),
        note: str = "",
        must_choose: bool = True,
        speeches: Sequence[tuple[int, str]] = (),
        order: Sequence[int] = (),
        direction: int = 0,
        persona: Persona | None = None,
        save_pool: Sequence[int] = (),
    ) -> Ask:
        state = self.state
        player = state.players[seat]
        # 提要必须止于**他上次读到哪**。`len(log) - len(delta)` 那份反推是错的：delta 只含他
        # 看得见的增量，夹在中间的私密事件不占长度，于是窗口起点偏后 —— 刚逐字发给他的发言
        # 又被摘要一遍，还把真正更早的提要挤出 RECAP_EVENTS（实测 8 局 572 次问话重复 583 行）。
        start = state.cursors.get(seat, 0)
        delta = tuple(state.read_new(seat))
        return Ask(
            kind=kind,
            day=state.day,
            phase=state.phase,
            seat=seat,
            identity=player.role_name,
            teammates=tuple(
                s for s, p in state.players.items() if p.is_wolf and s != seat
            ) if player.is_wolf else (),
            pool=tuple(int(s) for s in pool),
            save_pool=tuple(int(s) for s in save_pool),
            must_choose=must_choose,
            note=note,
            word_limit=word_limit_for(kind, state.config.flags),
            delta=delta,
            dossier=tuple(self.dossier(seat)),
            recap=tuple(self.recap(seat, start)),
            speeches=tuple(speeches),
            persona=persona or state.personas.get(seat),
            order=tuple(int(s) for s in order),
            direction=direction,
        )
