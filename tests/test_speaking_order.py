"""发言安排：警长只定方向，起点自动是他旁边那位，于是他必然压轴归票。

两层断言：
- 纯函数层：绕圈、找旁边那位活人、起点轮转（含反向环绕）；
- 引擎层：从**播报出来的事件**读每天真实的发言顺序，断言警长永远最后、且播报的
  起点+方向与真实顺序自洽（不读引擎内部字段，改实现就会变红）。
"""
from __future__ import annotations

import random

from werewolf.actors.fake import FakeActor
from werewolf.engine.config import PRESETS
from werewolf.engine.engine import Engine
from werewolf.engine.events import K_SPEAK_ORDER, K_SPEECH
from werewolf.engine.presenter import NullPresenter
from werewolf.engine.resolve import first_alive_in_direction, next_start_seat, speak_rotation
from .test_flow import engine_for, make_config
from .harness import play


# ------------------------------------------------------------------ 纯函数
def test_speak_rotation_walks_the_table_in_the_chosen_direction():
    all_alive = list(range(1, 10))
    assert speak_rotation(5, 1, all_alive, 9) == [5, 6, 7, 8, 9, 1, 2, 3, 4]
    assert speak_rotation(5, -1, all_alive, 9) == [5, 4, 3, 2, 1, 9, 8, 7, 6]


def test_speak_rotation_skips_the_dead():
    assert speak_rotation(5, 1, [1, 2, 3, 4, 5, 7, 9], 9) == [5, 7, 9, 1, 2, 3, 4]
    assert speak_rotation(5, -1, [1, 2, 3, 4, 5, 7, 9], 9) == [5, 4, 3, 2, 1, 9, 7]


def test_first_alive_in_direction_starts_next_to_the_seat_and_never_returns_it():
    assert first_alive_in_direction(6, 1, [2, 3, 6, 7], 9) == 7
    assert first_alive_in_direction(6, 1, [4, 6, 9], 9) == 9        # 旁边那位死了就接着往下找
    assert first_alive_in_direction(6, -1, [4, 5, 6], 9) == 5
    assert first_alive_in_direction(6, 1, [6], 9) is None           # 只剩自己


def test_next_start_seat_follows_the_direction():
    assert next_start_seat(5, 9, 1) == 6
    assert next_start_seat(5, 9, -1) == 4
    assert next_start_seat(1, 9, -1) == 9                            # 反向也要环绕
    assert next_start_seat(9, 9) == 1                                # 不传方向仍是老行为


def test_the_sheriff_is_the_last_one_the_rotation_reaches():
    """归票位不是一条额外规则，而是「从旁边那位开始绕一圈」的必然结果。"""
    rng = random.Random(11)
    checked = 0
    for _ in range(300):
        n = rng.randint(5, 12)
        sheriff = rng.randint(1, n)
        alive = [s for s in range(1, n + 1) if s == sheriff or rng.random() < 0.6]
        if len(alive) < 3:
            continue
        for direction in (1, -1):
            start = first_alive_in_direction(sheriff, direction, alive, n)
            assert start is not None and start != sheriff
            order = speak_rotation(start, direction, alive, n)
            assert sorted(order) == sorted(alive)                    # 不多不少、不重复
            assert order[-1] == sheriff, (n, sheriff, direction, order)
            checked += 1
    assert checked > 100, checked


# ------------------------------------------------------------------ 引擎层
def _game_with_living_sheriff(direction: int, seed: int = 7):
    """一个「警长一直活着、每天没人被票出去」的剧本，好连着看好几天的发言安排。

    狼每晚只刀同一个人（刀完就空刀），白天全场弃票，于是局面稳定地一天天往下走。
    """
    cfg = make_config(2)                       # 9 人标准
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=seed)
    probe.setup()
    wolves = [s for s in cfg.seats if probe.state.players[s].is_wolf]
    goods = [s for s in cfg.seats if not probe.state.players[s].is_wolf]
    badge, victim = goods[0], goods[1]
    spec: dict[int, dict] = {seat: {"no_shoot": True, "vote": None} for seat in cfg.seats}
    spec[badge].update({"run": True, "direction": direction, "transfer": None})
    for seat in cfg.seats:
        if seat != badge:
            spec[seat]["sheriff_vote"] = badge
    for seat in wolves:
        spec[seat].update({"knife": victim, "knife_after": victim})
    eng = engine_for(cfg, spec)
    eng.run()
    return eng, badge


def _daily_orders(eng: Engine) -> dict[int, list[int]]:
    """从公开事件读出每天的发言顺序（day -> 座位序）。"""
    orders: dict[int, list[int]] = {}
    for event in eng.state.log:
        if event.kind == K_SPEECH and event.phase.startswith("speech_"):
            orders.setdefault(event.day, []).append(event.payload["seat"])
    return orders


def _announced(eng: Engine, day: int):
    events = [e for e in eng.state.log if e.kind == K_SPEAK_ORDER and e.day == day]
    assert events, (day, "没播报今天从谁开始")
    return events[-1]


def test_the_sheriff_always_speaks_last_over_a_whole_game():
    for direction in (1, -1):
        eng, badge = _game_with_living_sheriff(direction)
        orders = _daily_orders(eng)
        assert len(orders) >= 2, (orders, "剧本要让警长活着说上好几天")
        for day, order in orders.items():
            if len(order) >= 3:
                assert order[-1] == badge, (direction, day, order)


def test_the_announced_start_and_direction_match_the_real_order():
    for direction in (1, -1):
        eng, badge = _game_with_living_sheriff(direction)
        n = eng.config.n_seats
        for day, order in _daily_orders(eng).items():
            if len(order) < 3:
                continue
            told = _announced(eng, day)
            assert told.payload.get("direction") == direction, told.text
            assert told.payload.get("seat") == badge, told.text
            start = told.payload["start"]
            assert order == speak_rotation(start, direction, order, n), told.text
            assert start == first_alive_in_direction(badge, direction, order, n), told.text


def test_the_two_directions_really_produce_different_orders():
    clockwise, _ = _game_with_living_sheriff(1)
    counter, _ = _game_with_living_sheriff(-1)
    day = max(d for d, order in _daily_orders(clockwise).items() if len(order) >= 4)
    assert _daily_orders(clockwise)[day] != _daily_orders(counter)[day]


def test_without_a_sheriff_the_default_start_and_clockwise_are_announced():
    cfg = make_config(2, sheriff_enabled=False)
    eng = Engine(cfg, {}, presenter=NullPresenter(), seed=7)
    eng.setup()
    eng.actors = {s: FakeActor(s, random.Random(s)) for s in cfg.seats}
    eng.run()
    announced = [e for e in eng.state.log if e.kind == K_SPEAK_ORDER]
    assert announced, "没有警长也要播报今天从谁开始"
    for event in announced:
        assert event.payload.get("direction") == 1, event.text
        assert event.payload.get("seat") is None, event.text      # 没人决定，只是默认值
    for day, order in _daily_orders(eng).items():
        if len(order) < 3:
            continue
        assert order == speak_rotation(_announced(eng, day).payload["start"], 1, order, cfg.n_seats)


def test_every_preset_keeps_the_order_self_consistent():
    """整局跑完，每天的发言安排都必须自洽（防「警长插到中间」这类回归）。"""
    for index in range(len(PRESETS)):
        for seed in (1, 2, 3):
            eng = play(index, seed=seed)
            for day, order in _daily_orders(eng).items():
                if len(order) < 3:
                    continue
                told = _announced(eng, day)
                order_of_the_day = speak_rotation(
                    told.payload["start"], told.payload["direction"], order, eng.config.n_seats)
                assert order == order_of_the_day, (index, seed, day, order, told.text)
                # payload 里的 seat = 当天决定方向的那个警长；他必须压轴
                decider = told.payload.get("seat")
                if decider is not None:
                    assert order[-1] == decider, (index, seed, day, order, told.text)


def test_every_speaker_is_told_the_same_order_the_judge_announced():
    """发言者拿到的位置信息必须与法官当众播报的那条一致，且「已发言」真是排在他前面的人。"""
    from .harness import RecordingActor, play

    for seed in (1, 2, 3, 5):
        RecordingActor.registry.clear()
        eng = play(2, seed=seed, recording=True)
        n = eng.config.n_seats
        asked = [r for r in RecordingActor.registry if r["kind"] in ("speech", "sheriff_speech")]
        assert asked, seed
        checked = 0
        for rec in asked:
            ask = rec["ask"]
            # 警上那一圈可以只有一个人竞选（他独自讲完就直接投票），白天整圈至少两人
            assert ask.order, (seed, rec["phase"], ask.order)
            assert ask.seat in ask.order, (seed, rec["phase"], ask.seat, ask.order)
            for seat, _words in ask.speeches:
                assert ask.order.index(seat) < ask.order.index(ask.seat), \
                    (seed, ask.day, seat, ask.seat, ask.order)
            if rec["phase"] != f"speech_{ask.day}":
                continue                      # 警上/PK 那圈各自有各自的顺序，下面只核对白天整圈
            assert len(ask.order) >= 2, (seed, ask.day, ask.order)
            told = _announced(eng, ask.day)
            assert ask.direction == told.payload["direction"], (seed, ask.day, ask.position_text())
            assert list(ask.order) == speak_rotation(told.payload["start"], ask.direction,
                                                     list(ask.order), n), (seed, ask.day, told.text)
            checked += 1
        assert checked >= 5, (seed, checked)
