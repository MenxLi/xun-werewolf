"""对局状态：玩家、事件日志（含冻结快照）、私密档案。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

from .config import GameConfig
from .persona import Persona
from .events import Event, GM, K_DEBUG, PUBLIC, visible
from .roles import CAT_GOD, CAT_VILLAGER, role_by_id


@dataclass
class Player:
    seat: int
    role_id: str
    alive: bool = True
    can_vote: bool = True
    role_revealed: bool = False
    is_human: bool = False
    # 角色内部状态
    heal_used: bool = False
    poison_used: bool = False
    seer_checks: dict[int, str] = field(default_factory=dict)   # seat -> "好人"/"狼人"
    died_on_day: int | None = None
    death_cause: str = ""            # knife / poison / exile / shot / ""
    last_words_done: bool = False

    @property
    def role_name(self) -> str:
        return role_by_id(self.role_id).name

    @property
    def is_wolf(self) -> bool:
        return role_by_id(self.role_id).faction == "wolf"

    @property
    def is_god(self) -> bool:
        return role_by_id(self.role_id).category == CAT_GOD

    @property
    def is_villager(self) -> bool:
        return role_by_id(self.role_id).category == CAT_VILLAGER


@dataclass
class NightRecord:
    day: int
    wolf_target: int | None = None
    wolf_rounds: int = 0        # 刀口投了几轮才定（0 = 这一夜没有狼开口，比如狼全死了）
    saved_by_witch: int | None = None
    poisoned_by_witch: int | None = None
    seer_target: int | None = None
    seer_result: str = ""


@dataclass
class GameState:
    config: GameConfig
    players: dict[int, Player]
    personas: dict[int, Persona] = field(default_factory=dict)
    day: int = 0
    phase: str = "setup"
    sheriff: int | None = None
    sheriff_elected: bool = False      # 竞选是否已经进行过了
    badge_destroyed: bool = False
    nights: list[NightRecord] = field(default_factory=list)
    log: list[Event] = field(default_factory=list)
    _buffer: list[Event] | None = None
    # 冻结块计数：每开一次「同时决策」+1。引擎自己不看它，只有可见性测试拿它当分组键
    # （平票重投两轮共用一个 phase，用 phase 分组的断言会串味，见 `test_visibility`）
    freeze_seq: int = 0
    cursors: dict[int, int] = field(default_factory=dict)
    speaker_order: list[int] = field(default_factory=list)
    finished: bool = False
    winner: str = ""

    # ---- 便捷查询（引擎侧专用，不提供给上下文拼装器）--------------------------
    @property
    def human_seat(self) -> int | None:
        return self.config.human_seat

    @property
    def human_out(self) -> bool:
        return self.human_seat is not None and not self.players[self.human_seat].alive

    def alive_seats(self) -> list[int]:
        return [s for s, p in self.players.items() if p.alive]

    def dead_seats(self) -> list[int]:
        return [s for s, p in self.players.items() if not p.alive]

    def wolves_alive(self) -> list[int]:
        return [s for s, p in self.players.items() if p.alive and p.is_wolf]

    def gods_alive(self) -> list[int]:
        return [s for s, p in self.players.items() if p.alive and p.is_god]

    def villagers_alive(self) -> list[int]:
        return [s for s, p in self.players.items() if p.alive and p.is_villager]

    def voters(self) -> list[int]:
        return [s for s in self.alive_seats() if self.players[s].can_vote]

    # ---- 事件日志 -----------------------------------------------------------
    def emit(self, event: Event) -> Event:
        if self._buffer is not None:
            self._buffer.append(event)
        else:
            self.log.append(event)
        return event

    def emit_public(self, kind: str, text: str, **payload: object) -> Event:
        return self.emit(Event(kind=kind, text=text, audience=PUBLIC, day=self.day, phase=self.phase, payload=payload))

    def emit_god(self, kind: str, text: str, **payload: object) -> Event:
        return self.emit(Event(kind=kind, text=text, audience=GM, day=self.day, phase=self.phase, payload=payload))

    @contextmanager
    def freeze(self):
        """同时决策期间冻结日志：票在收齐前不进公开 log，收齐后一次性公布。"""
        if self._buffer is not None:
            raise RuntimeError("freeze 不允许嵌套")
        self.freeze_seq += 1
        self._buffer = []
        try:
            yield self._buffer
        finally:
            buffer, self._buffer = self._buffer, None
        self.log.extend(buffer)

    def buffered(self) -> list[Event]:
        return list(self._buffer or ())
    def read_new(self, seat: int) -> list[Event]:
        """返回该玩家自上次读取以来的新增可见事件，并推进游标。"""
        start = self.cursors.get(seat, 0)
        out = [e for e in self.log[start:] if visible(e.audience, seat)]
        self.cursors[seat] = len(self.log)
        return out

    # ---- 人类观战可见性 -----------------------------------------------------
    def announceable(self, event: Event) -> bool:
        """这条事件是否应该播报给浏览器里的那一个人类：
        他自己该知道的（含私密）+ 一切公开 + 他出局后的上帝视角。"""
        if event.audience == PUBLIC:
            return True
        if self.human_seat is not None and visible(event.audience, self.human_seat):
            return True
        if event.kind == K_DEBUG:
            return False          # agent 的内心立场只进复盘，不播给观战者
        if event.audience == GM and self.config.flags.spectator_god_view and self.human_out:
            return True
        return False

    # ---- 结算辅助 -----------------------------------------------------------
    def kill(self, seat: int, cause: str) -> Player:
        player = self.players[seat]
        player.alive = False
        player.died_on_day = self.day
        player.death_cause = cause
        return player

    # ---- 警徽：警长死亡后先保留 sheriff，等移交/撕毁决定后再清理 -------------
    def badge_holder(self) -> int | None:
        if self.sheriff is not None and self.players[self.sheriff].alive:
            return self.sheriff
        return None

    def badge_master(self) -> int | None:
        if self.sheriff is not None and not self.players[self.sheriff].alive:
            return self.sheriff
        return None

    def set_badge(self, seat: int | None) -> None:
        self.sheriff = seat

    def destroy_badge(self) -> None:
        self.sheriff = None
        self.badge_destroyed = True

    def win_check(self) -> str:
        """返回 'wolf' / 'good' / ''。"""
        from .resolve import check_winner
        return check_winner(self)

    def vote_weight(self, seat: int) -> float:
        weight = 1.0
        if seat == self.badge_holder():
            weight = self.config.flags.sheriff_vote_weight
        return weight
