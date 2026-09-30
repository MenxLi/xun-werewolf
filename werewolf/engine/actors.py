"""玩家 actor 接口。引擎只通过 dispatch(actor, ask) 与玩家交互。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .ask import Ask, KINDS


class ActorError(RuntimeError):
    pass


@runtime_checkable
class Actor(Protocol):
    """决策协议只有一个动词：问什么、答案什么形状，全写在 `ask.KINDS` 里。"""

    seat: int
    is_human: bool

    def decide(self, ask: Ask): ...

    def finalize(self) -> None: ...


def dispatch(actor: Actor, ask: Ask):
    if ask.kind not in KINDS:
        raise ActorError(f"未知决策类型 {ask.kind!r}")
    return actor.decide(ask)
