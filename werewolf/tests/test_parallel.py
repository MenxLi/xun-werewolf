"""「同时决策」必须真的并发发问，而不是排着队逐个问。

一局里最耗时间的就是排队等模型：9 人局一次投票要串起八次模型往返。这里让脚本玩家在
每个同时决策的回合做一次握手 —— **现场得同时有第二个人也在想**，否则超时。
哪天有人把 `_ask_group` 改回 for 循环，这些握手就会全部超时，测试立刻变红。
"""
from __future__ import annotations

import random
import threading
import time

from ..actors.fake import FakeActor
from ..engine.config import SEER, VILLAGER, WIN_CITY, WOLF, GameConfig, RuleFlags
from ..engine.decisions import Ballot, SeerCheck, WolfProposal, WitchAction
from ..engine.engine import GROUP_WORKERS, Engine
from ..engine.events import K_EXILE
from ..engine.presenter import NullPresenter

HANDSHAKE_TIMEOUT = 5.0


class ConcurrencyHub:
    """按「环节 + 阶段」发握手点：每个同时决策的回合都要证明至少两人同时在想。"""

    def __init__(self, want: int = 2, timeout: float = HANDSHAKE_TIMEOUT) -> None:
        self.want, self.timeout = want, timeout
        self.lock = threading.Lock()
        self._points: dict[tuple[str, str], "_Meeting"] = {}
        self.failures: list[str] = []

    def prove(self, kind: str, phase: str) -> None:
        with self.lock:
            meeting = self._points.setdefault((kind, phase), _Meeting(self.want, self.timeout))
        if not meeting.attend():
            with self.lock:
                self.failures.append(f"{kind}@{phase}")

    @property
    def groups(self) -> list[tuple[str, str]]:
        with self.lock:
            return sorted(self._points)

    @property
    def peaks(self) -> dict[tuple[str, str], int]:
        with self.lock:
            return {key: point.peak for key, point in self._points.items()}


class _Meeting:
    """一次同时决策的现场：凑够 `want` 人后，后来者直接放行（不必全员到齐）。"""

    def __init__(self, want: int, timeout: float) -> None:
        self.want, self.timeout = want, timeout
        self.condition = threading.Condition()
        self.here = 0
        self.peak = 0
        self.gathered = False

    def attend(self) -> bool:
        deadline = time.monotonic() + self.timeout
        with self.condition:
            self.here += 1
            self.peak = max(self.peak, self.here)
            self.gathered = self.gathered or self.here >= self.want
            self.condition.notify_all()
            while not self.gathered:
                left = deadline - time.monotonic()
                if left <= 0:
                    self.here -= 1
                    return False
                self.condition.wait(left)
            self.here -= 1
            return True


class _Scripted(FakeActor):
    """夜里空刀、白天把票集中到指定那匹狼身上：局面一天一天干净地往下走。"""

    def __init__(self, seat: int, rng: random.Random, hub: ConcurrencyHub, wolves: list[int]) -> None:
        super().__init__(seat, rng)
        self.hub = hub
        self.wolves = wolves

    def _prove(self, ask) -> None:
        self.hub.prove(ask.kind, ask.phase)

    def wolf_target(self, ask) -> WolfProposal:
        if ask.day == 1:                      # 第一夜两匹狼都在场，正好验一次并发
            self._prove(ask)
        return WolfProposal(target=None, reason="空刀，白天再说")

    def witch_action(self, ask) -> WitchAction:
        return WitchAction()

    def seer_check(self, ask) -> SeerCheck:
        return SeerCheck(target=self._pick(ask) or ask.pool[0], reason="先抿一个")

    def vote(self, ask) -> Ballot:
        self._prove(ask)
        wanted = self.wolves[ask.day - 1] if ask.day <= len(self.wolves) else None
        return Ballot(target=wanted if wanted in list(ask.pool) else None)


def _config() -> GameConfig:
    return GameConfig(
        label="并行测试局", n_seats=6, counts={WOLF: 2, SEER: 1, VILLAGER: 3},
        flags=RuleFlags(win_condition=WIN_CITY, sheriff_enabled=False, last_words="none"),
        seed=5,
    )


def _play(actor_factory) -> tuple[Engine, list[int]]:
    cfg = _config()
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=cfg.seed)
    probe.setup()
    wolves = sorted(s for s in cfg.seats if probe.state.players[s].is_wolf)
    eng = Engine(cfg, {}, presenter=NullPresenter(), seed=cfg.seed)
    eng.setup()
    eng.actors = {s: actor_factory(s, wolves) for s in cfg.seats}
    eng.run()
    return eng, wolves


def test_the_wolf_pack_and_the_vote_are_asked_at_the_same_time():
    hub = ConcurrencyHub()
    eng, _wolves = _play(lambda seat, wolves: _Scripted(seat, random.Random(seat * 7 + 1), hub, wolves))
    assert eng.state.finished and eng.winner == "good", (eng.winner, eng.state.day)
    assert hub.failures == [], f"这些环节被串行发问了：{hub.failures}"
    assert {kind for kind, _phase in hub.groups} == {"wolf_target", "vote"}, hub.groups
    assert all(peak >= 2 for peak in hub.peaks.values()), hub.peaks


def test_group_asking_does_not_change_who_gets_exiled():
    """并发只该改「什么时候问」，不许改「谁说了什么」：票型与放逐对象照旧。"""
    hub = ConcurrencyHub()
    eng, wolves = _play(lambda seat, pack: _Scripted(seat, random.Random(seat * 7 + 1), hub, pack))
    exiled = [e.payload.get("seat") for e in eng.state.log if e.kind == K_EXILE]
    assert exiled == wolves, exiled               # 一天一匹狼，两天清空
    assert eng.presenter.notices == [], eng.presenter.notices


def test_a_meltdown_in_the_group_only_hands_over_that_one_seat():
    """有人炸了只托管他自己：别人的票照收，局面照走。"""
    hub = ConcurrencyHub()

    class _Meltdown(_Scripted):
        def vote(self, ask) -> Ballot:
            self._prove(ask)
            if ask.day == 1 and self.seat == self.wolves[0]:
                raise RuntimeError("模拟玩家发疯")
            return super().vote(ask)

    eng, wolves = _play(lambda seat, pack: _Meltdown(seat, random.Random(seat * 7 + 1), hub, pack))
    assert eng.state.finished and eng.winner == "good", (eng.winner, eng.state.day)
    assert any("托管" in note for note in eng.presenter.notices), eng.presenter.notices
    assert all("炸" not in note for note in eng.presenter.notices)
    exiled = [e.payload.get("seat") for e in eng.state.log if e.kind == K_EXILE]
    assert exiled == wolves, exiled


def test_the_group_pool_is_bounded():
    """并发上限存在且大于 1：不然要么白改，要么一次把 provider 打死。"""
    assert 1 < GROUP_WORKERS <= 8, GROUP_WORKERS


class _WindowPresenter(NullPresenter):
    """数活动窗口的开与关：每个 AI 提问都该被一对 `thinking`/`thought` 括住。"""

    def __init__(self) -> None:
        super().__init__(keep=True)
        self.opened: list[int] = []
        self.closed: list[int] = []

    def thinking(self, state, seat: int, kind: str) -> None:
        self.opened.append(seat)

    def thought(self, state, seat: int) -> None:
        self.closed.append(seat)


def test_every_ai_ask_is_wrapped_in_an_activity_window():
    """每次问 AI 都要开一个活动窗口再关掉 —— **包括同时决策的环节**。

    圆点是玩家判断「这局还在算」的唯一信号。以前只有逐个提问的路径会打招呼，
    于是投票、举手、退水、狼刀这些并发环节前端整段静默，看着像整局死了；
    而只剩 1 人投票（退化成逐个问）反而出圆点 —— 断断续续更像卡死。
    """
    presenter = _WindowPresenter()
    cfg = GameConfig(
        label="窗口局", n_seats=7, counts={WOLF: 2, SEER: 1, VILLAGER: 4},
        flags=RuleFlags(win_condition=WIN_CITY), seed=4,     # 开警长：举手/警上/退水都要问
    )
    eng = Engine(cfg, {}, presenter=presenter, seed=cfg.seed)
    eng.setup()
    asks: list[tuple[int, str]] = []

    class _Tally(FakeActor):
        def decide(self, ask):
            asks.append((ask.seat, ask.kind))
            return super().decide(ask)

    rng = random.Random(11)
    eng.actors = {seat: _Tally(seat, rng) for seat in cfg.seats}
    eng.run()

    assert eng.state.finished, "这局没走完，断言无从谈起"
    assert len(asks) > 20, asks                               # 确实问了很多次
    assert {kind for _seat, kind in asks} >= {"vote", "candidacy", "wolf_target"}, asks
    assert len(presenter.opened) == len(asks), \
        f"问了 {len(asks)} 次只打了 {len(presenter.opened)} 次招呼：有环节整段静默"
    assert sorted(presenter.closed) == sorted(seat for seat, _kind in asks), \
        "有活动窗口开了没人关：圆点会一直亮着说谎"


def test_a_hung_seat_times_out_and_the_game_carries_on():
    """一次提问挂住（模型请求永不返回）不许钉死整局：超时的座位按默认行动托管。

    xun 建 client 不传 HTTP timeout，所以这种请求真的存在；插件先前写好了「超时→托管」
    这句话却没写计时器，一个座位就把整局钉死（连停局都要等它）。
    """
    from ..engine import engine as engine_module

    saved = engine_module.ASK_TIMEOUT_SEC
    engine_module.ASK_TIMEOUT_SEC = 0.3
    try:
        hub = ConcurrencyHub()

        class _Stalled(_Scripted):
            def vote(self, ask) -> Ballot:
                self._prove(ask)
                if ask.day == 1 and self.seat == self.wolves[0]:
                    time.sleep(8.0)      # 永不返回的请求：它自己醒不醒都不影响这局走完（8 秒只为为变异验证留余量）
                return super().vote(ask)

        eng, _wolves = _play(lambda seat, pack: _Stalled(seat, random.Random(seat * 7 + 1), hub, pack))
        assert eng.state.finished, "挂住的座位把整局钉死了"
        hung = [n for n in eng.presenter.notices if "TimeoutError" in n]
        assert hung, f"没看到超时托管的告知：{eng.presenter.notices}"
    finally:
        engine_module.ASK_TIMEOUT_SEC = saved


def test_a_seat_with_a_question_still_in_flight_is_never_asked_twice():
    """上一次提问还挂着的座位，绝不问第二次 —— 那个座位的会话正被上一次调用用着。

    超时只是引擎不再等它，工作线程还在跑；这时下一轮再问同一座位，就会同时往同一个
    会话里写两次。这里让一个好人座位挂住 1.2 秒（超时压到 0.3 秒），于是下一轮投票
    必然在他还没回来时又来找他：
    - `attempts` = 发问出口撞上一次还没回来的次数（证明这测试真的测到了那一刻）；
    - `overlap`  = 演员侧真的同时被叫进去两次（守护没拦住就得红）。
    """
    from ..engine import engine as engine_module

    saved = engine_module.ASK_TIMEOUT_SEC
    engine_module.ASK_TIMEOUT_SEC = 0.3
    try:
        hub = ConcurrencyHub()
        lock = threading.Lock()
        inside: set[int] = set()
        overlap: list[str] = []
        attempts: list[str] = []

        cfg = _config()
        probe = Engine(cfg, {}, presenter=NullPresenter(), seed=cfg.seed)
        probe.setup()
        wolves = sorted(s for s in cfg.seats if probe.state.players[s].is_wolf)
        stall = next(s for s in cfg.seats if s not in wolves)   # 好人，两天投票都会找它

        class _Slow(_Scripted):
            def decide(self, ask):
                with lock:
                    if self.seat in inside:
                        overlap.append(f"{self.seat}号@{ask.kind}")
                    inside.add(self.seat)
                try:
                    return super().decide(ask)
                finally:
                    with lock:
                        inside.discard(self.seat)

            def vote(self, ask):
                self._prove(ask)
                if self.seat == stall:
                    time.sleep(1.2)                  # 超时之后线程仍挂在天上
                return super().vote(ask)

        eng = Engine(cfg, {}, presenter=NullPresenter(), seed=cfg.seed)
        eng.setup()
        eng.actors = {s: _Slow(s, random.Random(s * 7 + 1), hub, wolves) for s in cfg.seats}
        real_spawn = eng._spawn

        def spy_spawn(ask):                            # 发问出口：撞上一次没回来的就记一笔
            with lock:
                if ask.seat in inside:
                    attempts.append(f"{ask.seat}号@{ask.kind}")
            return real_spawn(ask)

        eng._spawn = spy_spawn
        eng.run()

        assert eng.state.finished, "挂住的座位把整局钉死了"
        assert attempts, "两次都没撞上「上一次还没回来」，这测试什么都没测到"
        assert overlap == [], f"同一座位被同时问了两次：{overlap}"
    finally:
        engine_module.ASK_TIMEOUT_SEC = saved


def test_a_terminated_game_voids_the_questions_not_yet_dispatched():
    """停局之后，排在并发上限后面还没问出去的问题必须作废，不许再往座位上抛。

    用户点「停」时往往有一串问题还在排队：逐个抛出去没人收答案，还会让停掉的局
    继续对着会话发问。
    """
    from ..engine.presenter import GameAborted

    cfg = _config()

    class _Stopped(NullPresenter):
        def stop_requested(self) -> bool:
            return True

    eng = Engine(cfg, {}, presenter=_Stopped(), seed=cfg.seed)
    eng.setup()
    try:
        eng._check_stop()                            # 走真实路径：这里把 _aborted 立起来
    except GameAborted:
        pass
    else:
        raise AssertionError("已停的局没被 _check_stop 拦住")

    asked: list[int] = []
    eng._call = lambda ask: asked.append(ask.seat) or Ballot(target=None)

    class _Ask:
        seat, kind, day, phase = cfg.seats[0], "vote", 1, "vote_1"

    error = eng._spawn(_Ask()).exception(2.0)
    assert isinstance(error, GameAborted), error
    assert asked == [], f"停局之后还往座位上抛问题：{asked}"
