"""事件与可见性模型。

引擎只维护一条 append-only 事件日志；每条事件自带"观众集合"，
拼装 agent 上下文时只做 `audience` 判断，因此不存在越权读取身份/他人私密信息的 API。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Sequence

PUBLIC: Literal["public"] = "public"
GM: Literal["gm"] = "gm"

Audience = frozenset[int] | Literal["public", "gm"]


def seats(*seats_: int) -> frozenset[int]:
    return frozenset(seats_)


def visible(audience: Audience, seat: int) -> bool:
    if audience == PUBLIC:
        return True
    if audience == GM:
        return False
    return seat in audience


@dataclass(frozen=True)
class Event:
    """一条事实。`text` 是给玩家看的一行中文描述（可见性范围内）。"""
    kind: str
    text: str
    audience: Audience = PUBLIC
    day: int = 0
    phase: str = ""
    payload: Mapping = field(default_factory=dict)

    def seen_by(self, seat: int) -> bool:
        return visible(self.audience, seat)


def private(kind: str, text: str, seat: int, *, day: int = 0, phase: str = "", **payload: object) -> Event:
    return Event(kind=kind, text=text, audience=seats(seat), day=day, phase=phase, payload=payload)


def group(kind: str, text: str, audience: Sequence[int], *, day: int = 0, phase: str = "", **payload: object) -> Event:
    return Event(kind=kind, text=text, audience=seats(*audience), day=day, phase=phase, payload=payload)


# 事件 kind 常量（测试与渲染依赖这些值）
K_GAME_START = "game_start"
K_ROLE_ASSIGNED = "role_assigned"
K_NIGHT_ACTION = "night_action"
K_NIGHT_RESULT = "night_result"
K_DEATH_ANNOUNCE = "death_announce"
K_DEATH_CAUSE = "death_cause"
K_LAST_WORDS = "last_words"
K_SPEECH = "speech"
K_VOTE = "vote"
K_VOTE_RESULT = "vote_result"
K_EXILE = "exile"
K_SHOT = "shot"
K_IDIOT_REVEAL = "idiot_reveal"
K_CANDIDACY = "candidacy"
K_CANDIDATE_LIST = "candidate_list"
K_SHERIFF_SPEECH = "sheriff_speech"
K_WITHDRAW = "withdraw"
K_SHERIFF_VOTE = "sheriff_vote"
K_SHERIFF_ELECTED = "sheriff_elected"
K_BADGE_TRANSFER = "badge_transfer"
K_BADGE_DESTROYED = "badge_destroyed"
K_SPEAK_ORDER = "speak_order"
K_WIN = "win"
K_WOLF_VOTE = "wolf_vote"      # 狼队夜里的盲投表决（票型 + 一句话理由），只有存活狼看得到
K_DEBUG = "debug"         # 内部记录（人格与选择有出入等），GM 可见
