"""纯函数结算逻辑（可单测，不改动状态）。"""
from __future__ import annotations

from typing import Sequence

from .config import WIN_EDGE


def resolve_night(
    wolf_target: int | None,
    saved: int | None,
    poisoned: int | None,
) -> list[tuple[int, str]]:
    """返回 [(座位, 死因)]，死因 'knife' / 'poison'。本 v1 无守卫，故不存在同守同救。"""
    deaths: list[tuple[int, str]] = []
    if wolf_target is not None and wolf_target != saved:
        deaths.append((wolf_target, "knife"))
    if poisoned is not None and poisoned != wolf_target:
        deaths.append((poisoned, "poison"))
    elif poisoned is not None and poisoned == wolf_target:
        # 刀毒同人：已死，死因记为毒（女巫毒到了被刀的人）
        if saved == wolf_target:
            deaths.append((poisoned, "poison"))
    return deaths


def check_winner(state) -> str:
    """'wolf' / 'good' / ''（未分胜负）。"""
    if not state.wolves_alive():
        return "good"
    alive = state.alive_seats()
    if not alive:
        return "wolf"
    if state.config.flags.win_condition == WIN_EDGE:
        if not state.gods_alive() or not state.villagers_alive():
            return "wolf"
    elif alive == state.wolves_alive():
        return "wolf"
    return ""


def plurality(
    votes: dict[int, int | None],
    weights: dict[int, float] | None = None,
) -> tuple[list[int], dict[int, float]]:
    """加权 plurality。返回 (得票最高者列表, 票型)。列表长度 >1 即平票。"""
    weights = weights or {}
    tally: dict[int, float] = {}
    for voter, target in votes.items():
        if target is None:
            continue
        tally[target] = tally.get(target, 0.0) + weights.get(voter, 1.0)
    if not tally:
        return [], {}
    top = max(tally.values())
    winners = [seat for seat, value in tally.items() if abs(value - top) < 1e-9]
    return sorted(winners), dict(sorted(tally.items()))


def describe_tally(tally: dict[int, float]) -> str:
    if not tally:
        return "无人得票"
    parts = [f"{seat}号 {value:g}票" for seat, value in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))]
    return "、".join(parts)


# ---------------------------------------------------------------- 发言方向
DIRECTION_TEXT = {1: "顺时针", -1: "逆时针"}
"""1 = 座位号递增，-1 = 座位号递减。警长只在这两者之间选，见 `engine._speaker_order`。"""


def seat_after(seat: int, n_seats: int, offset: int = 1) -> int:
    return (seat - 1 + offset) % n_seats + 1


def circle(start: int, direction: int, n_seats: int) -> list[int]:
    """从 start 出发、沿 direction 绕场一整圈的座位序列（含 start 自己）。"""
    return [seat_after(start, n_seats, step * direction) for step in range(n_seats)]


def next_start_seat(previous_start: int, n_seats: int, direction: int = 1) -> int:
    """发言起点轮转：上一天的起点沿当天的发言方向再走一位。"""
    return seat_after(previous_start, n_seats, direction)


def speak_rotation(start: int, direction: int, alive: Sequence[int], n_seats: int) -> list[int]:
    """发言顺序 = 从 start 起沿 direction 绕一圈、跳过死人。

    起点取「警长旁边那位」时，绕一圈必然最后才绕回警长 —— 归票位是这么来的，
    不需要任何额外规则把警长挪到最后。
    """
    living = set(alive)
    return [seat for seat in circle(start, direction, n_seats) if seat in living]


def first_alive_in_direction(seat: int, direction: int, alive: Sequence[int], n_seats: int) -> int | None:
    """从 seat 旁边那位开始沿 direction 走，遇到的第一个活人（绕一圈都没有则 None）。"""
    living = set(alive)
    return next((s for s in circle(seat_after(seat, n_seats, direction), direction, n_seats)
                 if s in living and s != seat), None)
