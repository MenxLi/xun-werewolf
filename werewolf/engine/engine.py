"""游戏引擎：阶段机与结算。所有规则判定都在这里，LLM 只负责扮演玩家。"""
from __future__ import annotations

import random
import threading
from concurrent.futures import Future
from typing import Callable, Sequence

from .actors import Actor, dispatch
from .ask import (
    Ask, KIND_CANDIDACY, KIND_LAST_WORDS, KIND_SEER, KIND_SHOT, KIND_SHERIFF_SPEECH,
    KIND_SHERIFF_VOTE, KIND_SPEAK_ORDER, KIND_SPEECH, KIND_TRANSFER, KIND_VOTE, KIND_WITHDRAW,
    KIND_WITCH, KIND_WOLF_TARGET, word_limit_for,
)
from .build import AskBuilder
from .config import (
    GameConfig, LAST_WORDS_ALL, LAST_WORDS_FIRST_NIGHT,
    WIN_EDGE, WIN_LABEL,
)
from .decisions import (
    WOLF_REASON_LIMIT, WOLF_VOTE_ROUNDS,
    Ballot, SeerCheck, Shot, SpeakOrder, Speech, Transfer, WitchAction, WolfProposal,
    Candidacy, Withdraw,
)
from .events import (
    K_BADGE_DESTROYED, K_BADGE_TRANSFER, K_DEATH_ANNOUNCE,
    K_DEATH_CAUSE, K_DEBUG, K_EXILE, K_GAME_START, K_IDIOT_REVEAL, K_LAST_WORDS, K_NIGHT_ACTION,
    K_NIGHT_RESULT, K_ROLE_ASSIGNED, K_SHOT, K_SHERIFF_SPEECH, K_SPEAK_ORDER, K_SPEECH,
    K_CANDIDACY, K_CANDIDATE_LIST, K_SHERIFF_ELECTED, K_SHERIFF_VOTE, K_WITHDRAW,
    K_VOTE_RESULT, K_WIN, K_WOLF_VOTE, Event, private, group,
)
from .persona import sample_personas
from .presenter import GameAborted, NullPresenter
from .resolve import (
    DIRECTION_TEXT, describe_tally, first_alive_in_direction, next_start_seat, plurality,
    resolve_night, speak_rotation,
)
from .roles import IDIOT, SEER, WITCH, role_by_id
from .state import GameState, Player, NightRecord

MAX_DAYS = 30

#: 一次「同时决策」最多并发发问几个座位：再高只是让 provider 自己排队，不如在这儿排
GROUP_WORKERS = 6

#: 一次提问最多等多久（只计 AI 座位）。xun 建 OpenAI client 不传 timeout，所以一个请求
#: 真能永不返回；那时按默认行动托管这个座位，让局面继续走 —— 宁可少一个人行动，
#: 也不能让一个挂住的 HTTP 把整局钉死。真人不计时：他看卡片、打字，几分钟都正常。
ASK_TIMEOUT_SEC = 300.0

#: 发言的**防御性**硬顶：只有模型失控（复读机、刷屏）才会撞上。规则里的发言上限
#: 不再截断正文（那是行为约束，不是消音器），但这种量级的输出会把每个人的上下文
#: 冲掉，所以照截，并在上帝视角留一句。
WORD_HARD_CEILING = 4000

CAUSE_SHORT = {"knife": "刀口", "poison": "毒杀", "exile": "放逐"}

#: 死因**不**私下告诉死者。告诉了他，他一句遗言（「我是被毒死的」）就把女巫今晚的用药卖给了
#: 全场，狼队尤其受益 —— 这条泄漏 `Ask` 那三层拦不住，因为遗言本来就是公开的。
#: 能开枪的角色只需要知道「这次能不能开枪」；真死因只留在上帝视角的夜结算里。
SHOT_ALLOWED = "法官私下告知你：你可以开枪带走一名存活玩家，开不开、带谁，你自己定。"
SHOT_FORBIDDEN = ("法官私下告知你：你这次死亡不满足开枪条件。"
                  "原因别声张，也别提法官私下跟你说过什么。")


def _seat(seat: int | None, empty: str = "无") -> str:
    """座位号的可读写法：None 不是 'None号'。"""
    return f"{seat}号" if seat else empty


class Engine:
    def __init__(
        self,
        config: GameConfig,
        actors: dict[int, Actor],
        presenter=None,
        seed: int | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.actors = actors
        self.presenter = presenter or NullPresenter()
        self.seed = seed if seed is not None else random.randrange(1, 2**31 - 1)
        self.rng = random.Random(self.seed)
        self.stances: dict[int, object] = {}
        self.state: GameState | None = None
        self.builder: AskBuilder | None = None
        self._announced = 0
        self._inflight: dict[int, Future] = {}        # 座位 -> 那次还没回来的提问
        self._slots = threading.BoundedSemaphore(GROUP_WORKERS)
        self._aborted = False
        self._start_seat: int | None = None   # 今天发言起点；次日沿当天方向再走一位
        self._direction = 1                   # 今天发言方向，见 resolve.DIRECTION_TEXT
        self.winner = ""

    # ================= 基础设施 =================
    def _check_stop(self) -> None:
        if self.presenter.stop_requested():
            self._aborted = True                      # 排在并发上限后面的问题不必再问了
            raise GameAborted("会话已关闭")

    def _announce(self) -> None:
        assert self.state is not None
        new = self.state.log[self._announced:]
        if new:
            self._announced = len(self.state.log)
            self.presenter.on_events(self.state, new)

    def _phase(self, title: str, subtitle: str = "") -> None:
        assert self.state is not None
        self._announce()
        self.presenter.phase(self.state, title, subtitle)

    def _note(self, text: str) -> None:
        assert self.state is not None
        self.state.emit_god(K_DEBUG, text)
        self.presenter.notice(self.state, text)

    def _thinking(self, seat: int, event_kind: str) -> None:
        """AI 玩家要开口了，先给前端一个活动指示（人类跳过：他那边是卡片，不是思考）。"""
        if getattr(self.actors.get(seat), "is_human", False):
            return
        self.presenter.thinking(self.state, seat, event_kind)

    def _prepare(self, seat: int, kind: str, *, pool: Sequence[int] = (), note: str = "",
                 must_choose: bool = True, speeches: Sequence[tuple[int, str]] = (),
                 order: Sequence[int] = (), direction: int = 0,
                 save_pool: Sequence[int] = ()) -> Ask:
        """把问题拼好但**先不发出去**：同时决策要先让每人拿到同一份快照，再一起发问。"""
        assert self.builder is not None
        ask = self.builder.build(seat, kind, pool=pool, note=note, must_choose=must_choose,
                                 speeches=speeches, order=order, direction=direction,
                                 save_pool=save_pool)
        return ask

    def _call(self, ask: Ask) -> object:
        """只调玩家、只回结果：工作线程里绝不碰状态与播报（写日志、托管必须回主线程）。"""
        return dispatch(self.actors[ask.seat], ask)

    def _settle(self, ask: Ask, value: object, error: Exception | None,
                fallback: Callable[[], object]) -> object:
        """玩家超时 / 格式错误 / 选了非法座位 → 法官按默认行动托管，绝不卡死整局。"""
        if error is None:
            return value
        self._note(f"{ask.seat}号 在「{ask.label}」环节异常（{type(error).__name__}: {error}），已按默认行动托管")
        return fallback()

    def _spawn(self, ask: Ask) -> Future:
        """把一次提问丢进守护线程，顺便打招呼（前端那个活动圆点从这里开始）。

        为什么不是 `ThreadPoolExecutor`：它的线程不是守护线程，一个永不返回的模型请求
        会连进程退出一起钉住。守护线程丢了就丢了，引擎这边超时托管照走。

        上一次提问还没回来就不再发第二次：那个座位的会话正被上一次调用用着，同时问两次
        会把它的对话写乱 —— 这里直接给一个已失败的 future，交给 `_settle` 托管。
        """
        self._thinking(ask.seat, K_SHERIFF_SPEECH if ask.kind == KIND_SHERIFF_SPEECH else K_SPEECH)
        future: Future = Future()
        previous = self._inflight.get(ask.seat)
        if previous is not None and not previous.done():
            future.set_exception(TimeoutError("上一次提问还挂在天上"))
            return future

        def worker() -> None:
            with self._slots:                         # 同时最多 GROUP_WORKERS 个座位在算
                if self._aborted:                     # 局已终止：还没问出去的就别问了
                    future.set_exception(GameAborted("本局已终止"))
                    return
                try:
                    future.set_result(self._call(ask))
                except BaseException as error:        # 含 GameAborted：交回游戏线程判定
                    future.set_exception(error)

        threading.Thread(target=worker, name=f"ww-ask-{ask.seat}", daemon=True).start()
        self._inflight[ask.seat] = future
        return future

    def _collected(self, ask: Ask, fallback: Callable[[], object], future: Future) -> object:
        """收一个答案：报错或超时都按默认行动托管，窗口照关（圆点不能一直亮着说谎）。

        活动指示在 `_spawn` 里开、在这个汇聚点上关，对所有提问一视同仁：夜里神职与刀、
        开枪、移交警徽、同时决策的每一票全都要经过这儿。先前只有发言循环会播，于是「天黑闭眼」之后
        前端整段静默，看着像这局死了。发言那一句在结算播报时已经播成玩家气泡（`on_events`
        里的 `say_as` 会顺手关掉这个作者的窗口），这儿再关一次是幂等的；没播成气泡的路径
        （托管、超限、非发言）由这儿兜住，否则圆点一直亮着说谎。
        人类座位由 `_thinking` 自己跳过（他那边是卡片或输入框，不是模型在算）。
        """
        wait = None if getattr(self.actors.get(ask.seat), "is_human", False) else ASK_TIMEOUT_SEC
        try:
            value: object = future.result(wait)
            error: Exception | None = None
        except GameAborted:
            self._aborted = True                      # 还没问出去的问题不必再问了
            raise
        except Exception as exc:                      # 含 TimeoutError（等待超时）
            value, error = None, exc
        try:
            return self._settle(ask, value, error, fallback)
        finally:
            self.presenter.thought(self.state, ask.seat)

    def _ask_prepared(self, ask: Ask, fallback: Callable[[], object]) -> object:
        return self._collected(ask, fallback, self._spawn(ask))

    def _ask(self, seat: int, kind: str, *, fallback: Callable[[], object],
             **question: object) -> object:
        """顺序问一个座位：夜里神职、发言、开枪、移交警徽这些有先后的环节。"""
        return self._ask_prepared(self._prepare(seat, kind, **question), fallback)

    def _ask_group(self, items: Sequence[tuple[Ask, Callable[[], object]]]) -> dict[int, object]:
        """同时决策：一次把问题发给所有人，收齐了才结算（返回 seat -> 决策）。

        原来是排着队逐个问 —— 12 人局一轮投票要串起十一次模型往返，一局光等 AI 就是几十分钟。
        拼问题留在主线程按座位序做（快照因此仍然一致），并发只跑 `_call`。

        先发全收：`_spawn` 逐个发出时每个座位各得一个活动圆点，所以整段并发期间前端
        都在亮；收的时候按座位序，最后一个回来才结算。
        停止请求：真人那一侧的卡片会立刻抛 `GameAborted`（`HostPresenter.stop()` 设 cancel_event），
        这里记下 `_aborted`，排在并发上限后面还没问出去的问题直接作废。
        """
        if len(items) < 2:
            return {ask.seat: self._ask_prepared(ask, fb) for ask, fb in items}
        pending = [(ask, fb, self._spawn(ask)) for ask, fb in items]
        return {ask.seat: self._collected(ask, fb, future) for ask, fb, future in pending}

    # ================= 开局 =================
    def setup(self) -> GameState:
        cfg = self.config
        assignment = cfg.build_deck(self.rng)
        players = {
            seat: Player(seat=seat, role_id=role, is_human=(seat == cfg.human_seat))
            for seat, role in assignment.items()
        }
        personas = sample_personas(assignment, self.rng, skip=cfg.human_seat)
        state = GameState(config=cfg, players=players, 
                          phase="setup", personas=personas)
        self.state = state
        self.builder = AskBuilder(state, self.stances)

        # 一块搞定（前端 pre-wrap）。走开局向导的局已经在确认步骤里展示过规则，这里就不重复
        opening = f"本局开局：{cfg.n_seats} 人局（{cfg.role_names()}），座位号 1 到 {cfg.n_seats}。"
        if not cfg.rules_shown_in_setup:
            opening += "\n本局规则：\n" + "\n".join("· " + line for line in cfg.flags.summary_lines())
        state.emit_public(K_GAME_START, opening)
        for seat, player in sorted(players.items()):
            pack = [s for s, p in players.items() if p.is_wolf and s != seat] if player.is_wolf else []
            text = f"你的座位是 {seat} 号，身份是 {player.role_name}。"
            if pack:
                text += " 你的狼队友是：" + "、".join(f"{s}号" for s in sorted(pack)) + "。狼队夜晚共同决定刀口，白天装作好人。"
            state.emit(private(K_ROLE_ASSIGNED, text, seat, day=0, phase="setup"))
        state.emit_god(
            K_GAME_START,
            "本局身份（上帝视角）：" + "、".join(f"{p.seat}号={p.role_name}" for p in sorted(players.values(), key=lambda p: p.seat)),
        )
        state.cursors = {seat: 0 for seat in players}   # 从 0 开始：所有人都要读到自己身份
        state.personas = personas
        return state

    # ================= 主循环 =================
    def run(self) -> str:
        state = self.state or self.setup()
        try:
            self._phase("开局", self.config.describe().split("\n")[0])
            self._announce()
            while not state.finished:
                self._check_stop()
                if state.day >= MAX_DAYS:
                    self._finish("draw", "超过最大天数，判定平局")
                    break
                self._night()
                if state.finished:
                    break
                self._day()
            self._announce()
            self.presenter.finished(state)
        except GameAborted:
            self.winner = "aborted"
        return self.winner

    # ================= 夜晚 =================
    def _night(self) -> None:
        state = self.state
        assert state is not None and self.builder is not None
        state.day += 1
        state.phase = f"night_{state.day}"
        record = NightRecord(day=state.day)
        state.nights.append(record)
        self._phase(f"第 {state.day} 夜", "天黑请闭眼")

        # --- 狼人（只有存活狼能投票；夜里不许说话，只能表决）---
        wolves = state.wolves_alive()
        if wolves:
            self._wolf_vote(wolves, record)

        # --- 女巫 ---
        witch = next((p for p in state.players.values() if p.alive and p.role_id == WITCH), None)
        if witch is not None:
            out_of_potions = witch.heal_used and witch.poison_used
            hide = out_of_potions and self.config.flags.witch_sees_knife_only_with_potion
            knife = record.wolf_target
            # 三种夜里是三句话：看不见刀口（药打完了）、狼空刀、有人被刀。以前前两种合成一句
            # 「你已用完两瓶药」，于是空刀夜的女巫被告知自己已经看过刀口 —— 她会据此推断
            # 「法官没提说明没人被刀」，把一条本来不该有的信息当成事实说给全场。
            note = (
                "你已用完两瓶药，不再知道刀口。" if hide
                else "今夜是空刀，没人被刀。" if knife is None
                else f"今夜被刀的是 {knife}号。"
            )
            # 「这一夜救不救得了」是规则，写进 Ask 让三种演员共用。以前真人演员是拿上面那句
            # 文案嗅探的：改一句法官措辞就等于悄悄改了规则。
            save_pool = () if (hide or knife is None or witch.heal_used
                               or (knife == witch.seat and not self.config.flags.witch_self_save)
                               ) else (knife,)
            # 毒药的目标池**不含她自己**（引擎从来不允许自毒），而且药用完就是空池。
            # 以前这里给的是全体存活座位：真人卡片上会出现「毒 6号」这种按钮（6号就是她自己），
            # 第二瓶药用完之后每一夜还在问她毒谁 —— 她点谁都被静默丢掉（结算写「毒药 未用」），
            # 可她已经在卡片上做过一次选择、AI 还为此编好了一段用药理由，然后拿去发言和留遗言。
            poison_pool = () if witch.poison_used else [s for s in state.alive_seats() if s != witch.seat]
            if not save_pool and not poison_pool:
                # 两瓶药都不在手里了：这一夜没有任何可下的药，就别问（问了只会逼她说一句假话），
                # 但按规则该让她知道的刀口照样私下告诉她。
                state.emit(private(K_NIGHT_ACTION, note + "（两瓶药都用完了，这一夜不用再问你。）",
                                   witch.seat, day=state.day, phase=state.phase))
            else:
                result = self._ask(
                    witch.seat, KIND_WITCH, pool=list(poison_pool), note=note, must_choose=False,
                    save_pool=save_pool, fallback=lambda: WitchAction(),
                )
                assert isinstance(result, WitchAction)
                if result.save and save_pool:
                    record.saved_by_witch = knife
                    witch.heal_used = True
                if result.poison_target in poison_pool and record.saved_by_witch is None:
                    record.poisoned_by_witch = result.poison_target
                    witch.poison_used = True
                state.emit(private(K_NIGHT_ACTION, (
                    f"你的用药决定：解药{'用在了 ' + str(record.saved_by_witch) + '号' if record.saved_by_witch else '未用'}；"
                    f"毒药{'用在了 ' + str(record.poisoned_by_witch) + '号' if record.poisoned_by_witch else '未用'}。"
                ), witch.seat, day=state.day, phase=state.phase))

        # --- 预言家 ---
        seer = next((p for p in state.players.values() if p.alive and p.role_id == SEER), None)
        if seer is not None:
            pool = [s for s in state.alive_seats() if s != seer.seat and s not in seer.seer_checks]
            if pool:
                result = self._ask(
                    seer.seat, KIND_SEER, pool=pool, note="查验一名玩家的阵营（只能是还活着且你没查过的人）。",
                    fallback=lambda: SeerCheck(target=pool[0]),
                )
                assert isinstance(result, SeerCheck)
                target = result.target if result.target in pool else pool[0]
                verdict = "狼人" if state.players[target].is_wolf else "好人"
                seer.seer_checks[target] = verdict
                record.seer_target, record.seer_result = target, verdict
                state.emit(private(K_NIGHT_ACTION, f"你查验了 {target}号，结果是「{verdict}」。", seer.seat, day=state.day, phase=state.phase))

        # --- 结算 ---
        deaths = resolve_night(record.wolf_target, record.saved_by_witch, record.poisoned_by_witch)
        state.emit_god(
            K_NIGHT_RESULT,
            f"第 {state.day} 夜结算："
            f"刀口 {_seat(record.wolf_target)}｜解药 {_seat(record.saved_by_witch, '未用')}｜"
            f"毒药 {_seat(record.poisoned_by_witch, '未用')}｜"
            f"查验 {(_seat(record.seer_target) + '=' + str(record.seer_result)) if record.seer_target else '未查验'}｜"
            f"死亡 {'、'.join(f'{s}号({CAUSE_SHORT.get(c, c)})' for s, c in deaths) or '无人'}",
        )
        for seat, cause in deaths:
            state.kill(seat, cause)

    def _wolf_vote(self, wolves: Sequence[int], record: NightRecord) -> None:
        """狼队出刀 = 盲投表决，最多 `WOLF_VOTE_ROUNDS` 轮。

        线下面杀的夜里狼队不许出声，靠比数字统一刀口，这里做的是同一件事：每轮**同时盲投**
        （谁都不知道别人这一轮投谁），轮与轮之间只把**票型**发给存活狼，没有自由讨论。
        投到唯一最高票就执行；`WOLF_VOTE_ROUNDS` 轮都投不到就空刀 —— 真实局里这叫「分刀」，代价本来就是
        这一夜没人死。
        （以前是「平票取编号最小」：300 局 12 人实测 45% 的夜靠它兜底改判，结果 1 号被刀
        182 次、12 号只有 75 次 —— 狼队自己没谈拢，代价全压在低号位玩家身上。）
        """
        state = self.state
        assert state is not None
        tally = ""
        for round_no in range(1, WOLF_VOTE_ROUNDS + 1):
            with state.freeze():
                asks = [self._prepare(
                    seat, KIND_WOLF_TARGET,
                    pool=[s for s in state.alive_seats() if s not in wolves],
                    note=self._wolf_note(tally),
                    # 空刀是合法答案（schema 里 target 可为 None）。少了这个 False，题面会被
                    # prompts 渲染成「必须从中选择」，AI 就被自己的题面劝退了空刀。
                    must_choose=False,
                ) for seat in wolves]
                answers = self._ask_group([(ask, lambda: WolfProposal()) for ask in asks])
                votes: dict[int, int | None] = {}
                sayings: dict[int, str] = {}
                for ask in asks:
                    result = answers[ask.seat]
                    assert isinstance(result, WolfProposal)
                    target = result.target
                    # 按**题面给的池子**校验，不是按活着的人：池子已经排除了狼，模型真提刀
                    # 队友时这里必须丢掉，否则多数决可能把狼队友定成刀口（女巫还会被通知杀他）。
                    if target is not None and target not in ask.pool:
                        target = None
                    votes[ask.seat] = target
                    reason = " ".join((result.reason or "").split())
                    if reason:
                        sayings[ask.seat] = reason[:WOLF_REASON_LIMIT]

            picks = {seat: target for seat, target in votes.items() if target is not None}
            detail = "、".join(
                f"{seat}号→{'空刀' if votes[seat] is None else str(votes[seat]) + '号'}"
                + (f"（{sayings[seat]}）" if seat in sayings else "")
                for seat in sorted(votes))
            # `plurality` 顺手把票型也算好了（第二个返回值），文本用全仓同一个 `describe_tally`
            leaders, counts = plurality(picks, {s: 1.0 for s in picks})
            if not picks:                                   # 全队都想空刀，没必要再烧一轮
                outcome = "全队要空刀"
            elif len(leaders) == 1:
                record.wolf_target = leaders[0]
                outcome = f"最终决定击杀：{record.wolf_target}号"
            elif round_no >= WOLF_VOTE_ROUNDS:
                outcome = f"投满 {WOLF_VOTE_ROUNDS} 轮仍没定，本夜空刀"
            else:
                outcome = "刀口未定，再投一轮"
            # 轮与轮之间狼队能看到的只有票型 + 各自投了谁（这条文本），没有自由讨论
            state.emit(group(K_WOLF_VOTE, f"狼队表决（第{round_no}轮）：{detail}。{outcome}。",
                             wolves, day=state.day, phase=state.phase))
            if record.wolf_target is not None or not picks or round_no >= WOLF_VOTE_ROUNDS:
                # `wolf_rounds` 只在**投出结论**这一刻写。档案（`build.py`）拿它挑「表决过的夜」，
                # 每轮开头就写会把正在表决的这一夜也算进去：狼在自己的行动卡片上读到
                # 「【狼队表决】第1夜 空刀（2轮）」—— 票还没投完，法官就替这夜写了结局。
                record.wolf_rounds = round_no
                return
            tally = describe_tally(counts)

    def _wolf_note(self, tally: str) -> str:
        note = ("刀口由狼队投票决定：这一轮你**看不到队友投了谁**。"
                f"最多投 {WOLF_VOTE_ROUNDS} 轮，谁能拿到唯一最高票就刀谁；"
                f"{WOLF_VOTE_ROUNDS} 轮都投不到唯一最高票就空刀。主动空刀也是合法选项。")
        if tally:
            note += f"上一轮的票型：{tally}。这一轮可以坚持，也可以改 —— 改就改票，不用解释给别人听。"
        return note

    # ================= 天亮 =================
    def _dawn(self) -> None:
        state = self.state
        assert state is not None
        state.phase = f"dawn_{state.day}"
        dead = [p.seat for p in sorted(state.players.values(), key=lambda p: p.seat)
                if not p.alive and p.died_on_day == state.day]
        self._phase(f"第 {state.day} 天 · 天亮", "公布死讯")
        if not dead:
            state.emit_public(K_DEATH_ANNOUNCE, "昨夜是平安夜，无人死亡。")
        else:
            names = "、".join(f"{s}号" for s in dead)
            text = f"昨夜倒牌的是：{names}。"
            if self.config.flags.reveal_dead_role:
                text += "（公开身份：" + "、".join(f"{s}号={state.players[s].role_name}" for s in dead) + "）"
                for s in dead:
                    state.players[s].role_revealed = True
            state.emit_public(K_DEATH_ANNOUNCE, text)
            for s in dead:
                player = state.players[s]
                if role_by_id(player.role_id).shot:
                    # 只说「能不能开枪」，不说死因（见 SHOT_ALLOWED）：真死因只在上帝视角的
                    # 夜结算里，进不了任何玩家的 Ask，也就没人在遗言里把它说出去。
                    can_shoot = player.death_cause != "poison"
                    state.emit(private(K_DEATH_CAUSE, SHOT_ALLOWED if can_shoot else SHOT_FORBIDDEN, s))
        self._badge_handover()
        self._last_words([s for s in dead if self._want_last_words("night")])
        self._shots(dead)
        self._check_win("night")

    def _want_last_words(self, when: str) -> bool:
        flags = self.config.flags
        if flags.last_words == "none":
            return False
        if when == "exile":
            return True
        if flags.last_words == LAST_WORDS_ALL:
            return True
        return flags.last_words == LAST_WORDS_FIRST_NIGHT and (self.state.day if self.state else 0) <= 1

    def _last_words(self, seats: Sequence[int]) -> None:
        state = self.state
        assert state is not None
        for seat in seats:
            player = state.players[seat]
            if player.last_words_done:
                continue
            player.last_words_done = True
            result = self._ask(
                seat, KIND_LAST_WORDS,
                note="留下你的遗言（可以公开身份、指出狼坑，也可以留话题）。",
                must_choose=False, fallback=lambda: Speech(text="（没有留下遗言）"),
            )
            words = self._clean_words(getattr(result, "text", ""))
            self._notes_over_limit(seat, words, word_limit_for(KIND_LAST_WORDS, self.config.flags), "遗言")
            state.emit(public_last_words(seat, words, day=state.day))

    def _shots(self, dead_seats: Sequence[int]) -> None:
        state = self.state
        assert state is not None
        for seat in dead_seats:
            player = state.players[seat]
            if not role_by_id(player.role_id).shot:
                continue                      # 没技能的角色不询问（Role.shot 是唯一真相）
            if player.death_cause == "poison":
                continue                      # 被毒不能开枪：不询问，也不外泄原因以外的信息
            if player.death_cause == "shot":
                continue                      # 被枪带走的不能反枪（v1 简化，规则里已声明）
            pool = [s for s in state.alive_seats()]
            if not pool:
                continue
            result = self._ask(
                seat, KIND_SHOT, pool=pool, note=f"你是 {player.role_name}，可以开枪带走一名存活玩家，也可以放弃。",
                must_choose=False, fallback=lambda: Shot(),
            )
            assert isinstance(result, Shot)
            target = result.target if result.target in pool else None
            if target is None:
                state.emit_god(K_SHOT, f"{seat}号（{player.role_name}）选择不开枪。")
                continue
            state.emit_public(K_SHOT, f"{seat}号（{player.role_name}）发动技能，带走了 {target}号。", seat=seat, target=target)
            state.kill(target, "shot")
            if self._want_last_words("shot"):
                self._last_words([target])

    def _badge_handover(self) -> None:
        """警长死亡后的警徽移交/撕毁。"""
        state = self.state
        assert state is not None
        master = state.badge_master()
        if master is None:
            return
        pool = [s for s in state.alive_seats()]
        result = self._ask(
            master, KIND_TRANSFER, pool=pool,
            note="你已出局，可以选择把警徽移交给一名存活玩家，或者撕毁警徽（撕毁后本局不再有警长）。",
            must_choose=False, fallback=lambda: Transfer(),
        )
        assert isinstance(result, Transfer)
        target = result.target if result.target in pool else None
        if target is None:
            state.destroy_badge()
            state.emit_public(K_BADGE_DESTROYED, f"原警长 {master}号 撕毁了警徽，本局不再有警长。", seat=master)
        else:
            state.set_badge(target)
            state.emit_public(K_BADGE_TRANSFER, f"原警长 {master}号 把警徽移交给了 {target}号。", seat=master, target=target)

    # ================= 白天 =================
    def _day(self) -> None:
        state = self.state
        assert state is not None
        self._dawn()
        if state.finished:
            return
        state.phase = f"day_{state.day}"
        if self.config.flags.sheriff_enabled and not state.sheriff_elected:
            sheriff_election(self)
            if state.finished:
                return
        start, direction = self._speaker_order()
        order = speak_rotation(start, direction, state.alive_seats(), self.config.n_seats)
        state.speaker_order = order
        self._phase(f"第 {state.day} 天 · 发言",
                    f"{DIRECTION_TEXT[direction]}发言 ｜ " + " → ".join(f"{s}号" for s in order))
        self._speeches(order)
        if state.finished:
            return
        self._vote_phase()
        self._start_seat = next_start_seat(start, self.config.n_seats, direction)

    def _ordered_by_today(self, seats: Sequence[int]) -> list[int]:
        """按当天的发言顺序排（PK 上台沿用当天顺序，别打乱「越靠后越接近归票」的节奏）。"""
        order = self.state.speaker_order if self.state else ()
        rank = {seat: index for index, seat in enumerate(order)}
        return sorted(seats, key=lambda s: rank.get(s, len(rank)))

    @property
    def _default_start(self) -> int:
        state = self.state
        assert state is not None
        if self._start_seat is None:
            # 起点只随机一种。`first_speaker` 曾有三个取值，但向导、预设、测试从不写它，
            # 另两个分支从来没被执行过 —— 删了。要做「固定从 1 号开始」这种规则，
            # 先得让向导问得出这一项，而不是留一条没人拧得到的旋钮。
            self._start_seat = self.rng.choice(state.alive_seats())
        return self._start_seat

    def _speaker_order(self) -> tuple[int, int]:
        """发言安排：警长只决定**方向**（顺时针 / 逆时针），起点自动是他旁边那位。

        绕一圈必然最后才绕回警长，所以他天然压轴归票 —— 和现实局一致，也让「拿到警徽」
        这件事有真实收益，而不是获得一个「想让自己第几个说话」的怪能力。
        没有警长时沿用开局定下的随机起点 + 顺时针。
        """
        state = self.state
        assert state is not None
        holder = state.badge_holder()
        if holder is None:
            start, self._direction = self._default_start, 1
            state.emit_public(K_SPEAK_ORDER, f"今天从 {start}号 开始，{DIRECTION_TEXT[1]}发言。",
                              start=start, direction=1)
            return start, self._direction

        def starts(direction: int) -> int | None:
            return first_alive_in_direction(holder, direction, state.alive_seats(),
                                            self.config.n_seats)

        options = "，".join(
            f"{DIRECTION_TEXT[d]}从 {starts(d)}号 开始" for d in (1, -1) if starts(d) is not None)
        result = self._ask(
            holder, KIND_SPEAK_ORDER,
            note=f"决定今天往哪个方向发言：{options}。两种都是你最后发言、负责归票。",
            fallback=lambda: SpeakOrder(direction=1),
        )
        assert isinstance(result, SpeakOrder)
        direction = -1 if result.direction == -1 else 1
        start = starts(direction) or starts(1) or self._default_start
        self._start_seat, self._direction = start, direction
        state.emit_public(
            K_SPEAK_ORDER,
            f"警长 {holder}号 决定今天{DIRECTION_TEXT[direction]}发言，从 {start}号 开始"
            f"（{holder}号 压轴归票）。",
            start=start, seat=holder, direction=direction)
        return start, direction

    def _clean_words(self, text: str) -> str:
        """清掉换行；**不按发言上限截断**。

        发言上限（`config.flags.speech_word_limit`）是给人和模型的行为约束：超了自己
        收着说。法官照原样播给全场，只在上帝视角记一笔（见 `_notes_over_limit`）——
        把发言砍成半句话，场上就少了一条真实存在的证词，复盘时也追不回来。
        只有 `WORD_HARD_CEILING` 那种失控长度才真截。
        """
        text = str(text).strip().replace("\n", " ")
        if len(text) > WORD_HARD_CEILING:
            text = text[: WORD_HARD_CEILING - 1] + "…"
        return text or "（沉默）"

    def _notes_over_limit(self, seat: int, words: str, limit: int, label: str = "发言") -> None:
        """超长发言不截断，但你复盘时能看到谁没守住上限。"""
        assert self.state is not None
        if limit and len(words) > limit:
            self.state.emit_god(K_DEBUG, f"{seat}号 {label} {len(words)} 字，超出上限 {limit} 字（法官没截）")

    def _speeches(self, order: Sequence[int], kind: str = KIND_SPEECH, note: str = "", phase: str = "") -> list[tuple[int, str]]:
        state = self.state
        assert state is not None
        spoken: list[tuple[int, str]] = []
        pool = state.alive_seats()
        previous_phase = state.phase
        state.phase = phase or ('sheriff_speech' if kind == KIND_SHERIFF_SPEECH else f'speech_{state.day}')
        for seat in order:
            if seat not in state.alive_seats():
                continue
            event_kind = K_SHERIFF_SPEECH if kind == KIND_SHERIFF_SPEECH else K_SPEECH
            result = self._ask(
                seat, kind, pool=pool, note=note, speeches=spoken, must_choose=False,
                # 把自己在发言链里的位置一起交给玩家：顺序与方向是公开信息，别让人自己数座位
                order=tuple(order), direction=0 if kind == KIND_SHERIFF_SPEECH else self._direction,
                fallback=lambda: Speech(text="我过，先听后面的玩家怎么说。"),
            )
            words = self._clean_words(getattr(result, "text", ""))
            self._notes_over_limit(seat, words, word_limit_for(kind, self.config.flags),
                                   # 比的是 event_kind：`kind` 是 ask 的词表，与事件词表只是恰好同值
                                   "警上发言" if event_kind == K_SHERIFF_SPEECH else "发言")
            stance = getattr(result, "stance", None)
            if stance is not None:
                self.stances[seat] = stance
                state.emit_god(K_DEBUG, f"{seat}号 内心立场：{stance}")
            state.emit(public_speech(seat, words, event_kind, day=state.day, phase=state.phase))
            spoken.append((seat, words))
            self._announce()
        state.phase = previous_phase
        return spoken

    # ================= 投票 =================
    def _vote_phase(self) -> None:
        state = self.state
        assert state is not None
        self._phase(f"第 {state.day} 天 · 投票", "投票放逐一名玩家")
        state.phase = f"vote_{state.day}"
        # 白天放逐、警徽、PK 重投是同一条流程：冻结 → 同时问 → 收齐 → 按规则亮票 → 加权计票。
        # 以前白天那份单独写了一遍（同一套校验与亮票逻辑抄两次，改一处忘另一处）。
        winners, tally = _ballot_round(
            self, state.voters(), KIND_VOTE, list(state.alive_seats()),
            note="投票放逐一名玩家（可以弃票）。注意：所有人同时亮票，你看不到别人的票。",
            hit="{}号 投票给 {}号", abstain="{}号 弃票", weighted=True)
        state.emit_public(K_VOTE_RESULT, f"第 {state.day} 天票型：{describe_tally(tally)}", tally=dict(tally))
        self._announce()
        if not winners:
            state.emit_public(K_VOTE_RESULT, "本轮无人被放逐。")
            self._check_win("vote")
            return
        if len(winners) > 1:
            winners, tally = pk_round(self, winners)
            state.emit_public(K_VOTE_RESULT, f"PK 后票型：{describe_tally(tally)}", tally=dict(tally))
            self._announce()
            if not winners:
                state.emit_public(K_VOTE_RESULT, "PK 仍然平票，本轮无人被放逐。")
                self._check_win("vote")
                return
        if len(winners) > 1:
            state.emit_public(K_VOTE_RESULT, "仍然平票，本轮无人被放逐。")
            self._check_win("vote")
            return
        self._exile(winners[0])

    def _exile(self, seat: int) -> None:
        state = self.state
        assert state is not None
        player = state.players[seat]
        role = player.role_id
        if role == IDIOT and not player.role_revealed:
            player.role_revealed = True
            player.can_vote = False
            state.emit_public(K_IDIOT_REVEAL, f"{seat}号 翻出身份牌：白痴！他免于被放逐，但从此失去投票权。", seat=seat)
            if state.sheriff == seat:
                pool = [s for s in state.alive_seats() if s != seat]
                result = self._ask(
                    seat, KIND_TRANSFER, pool=pool,
                    note="你翻牌后必须立刻把警徽移交出去（只能给存活的他人）。",
                    must_choose=False, fallback=lambda: Transfer(),
                )
                target = getattr(result, "target", None)
                if target in pool:
                    state.set_badge(target)
                    state.emit_public(K_BADGE_TRANSFER, f"{seat}号 把警徽移交给了 {target}号。", seat=seat, target=target)
                else:
                    state.destroy_badge()
                    state.emit_public(K_BADGE_DESTROYED, f"{seat}号 撕毁了警徽。", seat=seat)
            self._announce()
            self._check_win("vote")
            return
        state.emit_public(K_EXILE, f"{seat}号 以最高票被放逐出局。", seat=seat)
        state.kill(seat, "exile")
        self._announce()
        if self._want_last_words("exile"):
            self._last_words([seat])
        self._badge_handover()  # 警长被放逐同样要当场决定移交还是撕毁（原来只有夜间死亡才问）
        self._shots([seat])
        self._check_win("vote")

    # ================= 结束 =================
    def _check_win(self, when: str) -> str:
        state = self.state
        assert state is not None
        winner = state.win_check()
        if winner:
            self._finish(winner, when)
        return winner

    def _finish(self, winner: str, when: str = "") -> None:
        state = self.state
        assert state is not None
        if state.finished:
            return
        state.finished = True
        state.winner = winner
        self.winner = winner
        label = WIN_LABEL.get(winner, winner)
        if winner == "wolf":
            if not state.villagers_alive() and state.config.flags.win_condition == WIN_EDGE:
                reason = "村民已全部出局（屠边）"
            elif not state.gods_alive() and state.config.flags.win_condition == WIN_EDGE:
                reason = "神职已全部出局（屠边）"
            else:
                reason = "好人已全部出局"
        elif winner == "good":
            reason = "狼人已全部出局"
        else:
            reason = when
        state.phase = "over"
        state.emit_public(K_WIN, f"游戏结束：{label}！（{reason}）", winner=winner)
        # 身份表不在这里再播一遍：紧接着的终局卡就是全量身份（`render.status_parts(state, True)`，
        # 纯文本形态也一样带）。以前这两处连着发，玩家看到的是一份信息的两个副本。
        state.emit_god(K_WIN, f"本局结束于第 {state.day} 天，胜方 {winner}，原因：{reason}", winner=winner)
        self._announce()


def public_speech(seat: int, words: str, kind: str, day: int, phase: str = "") -> Event:
    return Event(kind=kind, text=f"{seat}号：{words}", audience="public", day=day, phase=phase,
            payload={"seat": seat, "words": words})


def public_last_words(seat: int, words: str, day: int) -> Event:
    return Event(
        kind=K_LAST_WORDS,
        text=f"{seat}号（遗言）：{words}",
        audience="public", day=day, phase="last_words",
        payload={"seat": seat, "words": words},
    )


# ============================================================================ 警长竞选与 PK
# 这两个「同时决定」环节的节拍和别处一样：按座位序拼好问题（`_prepare`）→
# 一次并发发问（`_ask_group`）→ 收齐后按座位序结算。拼与结算都在主线程，
# 所以冻结快照与播报顺序都和谁先想出来无关。
def _rotation_from(engine: "Engine", seats: list[int]) -> list[int]:
    """随机起点、按座位序绕一圈（警上发言顺序：此刻还没有警长，谈不上方向）。"""
    if not seats:
        return []
    start = engine.rng.choice(seats)
    n = engine.config.n_seats
    return sorted(seats, key=lambda s: (s - start) % n)


def _ballot_round(engine: "Engine", voters: list[int], kind: str, pool: list[int], *,
                  note: str, hit: str, abstain: str, weighted: bool = False
                  ) -> tuple[list[int], dict[int, float]]:
    """一轮同时亮票的投票：发问 → 校验目标在池内（不能投自己）→ 按规则亮票 → 计票。

    白天放逐、警徽、PK 重投**都走这儿**，三者不同的只有文案和「警长是否多倍票」。
    返回 (得票最高者列表, 票型)，列表长度 >1 即平票。
    """
    state = engine.state
    assert state is not None
    with state.freeze():
        asks = [(engine._prepare(seat, kind, pool=[s for s in pool if s != seat],
                                must_choose=False, note=note), lambda: Ballot())
                for seat in voters]
        answers = engine._ask_group(asks)
        votes: dict[int, int | None] = {}
        for seat in voters:
            options = [s for s in pool if s != seat]
            target = getattr(answers[seat], "target", None)
            target = target if target in options else None
            votes[seat] = target
            if state.config.flags.vote_reveal:
                state.emit_public(kind, hit.format(seat, target) if target else abstain.format(seat),
                                  voter=seat, target=target)
    return plurality(votes, {s: (state.vote_weight(s) if weighted else 1.0) for s in votes})


def sheriff_election(engine: "Engine") -> None:
    state = engine.state
    assert state is not None
    alive = state.alive_seats()
    state.phase = f"sheriff_{state.day}"
    engine._phase("警长竞选 · 同时举手", "你看不到别人有没有举手")

    # 1) 同时举手：看不到别人的选择，所以没有跟风可言
    with state.freeze():
        asks = []
        for seat in alive:
            persona = state.personas.get(seat)
            leans = bool(persona.run_for_sheriff if persona else False)
            asks.append((engine._prepare(
                seat, KIND_CANDIDACY, must_choose=False,
                note="法官询问：是否举手参选警长？看不到其他人的选择。"),
                lambda leans=leans: Candidacy(run=leans)))     # 人格倾向只是默认值，模型可反着选
        answers = engine._ask_group(asks)
    decisions = {seat: bool(getattr(answers[seat], "run", False)) for seat in alive}
    for seat in alive:
        state.emit(private(K_CANDIDACY, f"你{'举手' if decisions[seat] else '没有举手'}参选警长。",
                           seat, day=state.day, phase=state.phase))

    candidates = [s for s in alive if decisions[s]]
    if not candidates:
        state.emit_public(K_CANDIDATE_LIST, "没有人竞选警长，本局没有警长。")
        state.sheriff_elected = True
        return
    state.emit_public(
        K_CANDIDATE_LIST,
        "警上候选人：" + "、".join(f"{s}号" for s in candidates) + "；其余玩家在警下。",
        candidates=tuple(candidates),
    )
    engine._announce()

    # 2) 警上发言（随机起点，按座位序；发言必须串行，后面的人要听到前面的）
    order = _rotation_from(engine, candidates)
    state.emit_public(K_CANDIDATE_LIST, "警上发言顺序：" + " → ".join(f"{s}号" for s in order))
    engine._speeches(order, KIND_SHERIFF_SPEECH, note="警上发言：说服警下把警徽给你，可以说说你心中的警徽流。")

    # 3) 退水（同样同时决定，否则「看别人退了跟着退」会污染判断）
    with state.freeze():
        asks = [(engine._prepare(seat, KIND_WITHDRAW, must_choose=False,
                                 note="是否退水（退出竞选，回到警下参与投票）？"),
                 lambda: Withdraw()) for seat in _rotation_from(engine, candidates)]
        answers = engine._ask_group(asks)
        withdrawn = [ask.seat for ask, _fb in asks if bool(getattr(answers[ask.seat], "withdraw", False))]
        for seat in withdrawn:
            state.emit_public(K_WITHDRAW, f"{seat}号 退水，回到警下。", seat=seat)
    candidates = [s for s in candidates if s not in withdrawn]
    if candidates:
        state.emit_public(K_CANDIDATE_LIST, "退水后警上候选人：" + "、".join(f"{s}号" for s in candidates))
    if not candidates:
        state.emit_public(K_CANDIDATE_LIST, "所有候选人退水，本局没有警长。")
        state.sheriff_elected = True
        engine._announce()
        return
    if len(candidates) == 1:
        state.set_badge(candidates[0])
        state.emit_public(K_SHERIFF_ELECTED, f"{candidates[0]}号 无对手，直接当选警长（警徽 {state.config.flags.sheriff_vote_weight:g} 票）。", seat=candidates[0])
        state.sheriff_elected = True
        engine._announce()
        return

    # 4) 投票选警徽（候选人不投票）；平票再投一轮
    for _round in range(2):
        winners, tally = _ballot_round(
            engine, [s for s in state.voters() if s not in candidates],
            KIND_SHERIFF_VOTE, list(candidates),
            note="把警徽投给一名候选人（可弃票）。所有票同时公布。",
            hit="{}号 把警徽投给 {}号", abstain="{}号 弃票")
        state.emit_public(
            K_VOTE_RESULT,          # 票型汇总是结果，不是投票明细，别被并进同一行
            f"警徽投票结果：{describe_tally(tally)}" + ("（平票）" if len(winners) > 1 else ""),
            tally=dict(tally),
        )
        engine._announce()
        if len(winners) == 1:
            state.set_badge(winners[0])
            state.emit_public(
                K_SHERIFF_ELECTED,
                f"{winners[0]}号 当选警长（警徽 {state.config.flags.sheriff_vote_weight:g} 票）。",
                seat=winners[0],
            )
            break
        if _round == 0 and len(winners) > 1:
            state.emit_public(K_VOTE_RESULT, "警徽投票平票，平票候选人再做一段发言后重投。")
            engine._speeches(sorted(winners), KIND_SHERIFF_SPEECH,
                            note="警徽平票重投前，再做一段拉票发言。", phase=f"sheriff_pk_{state.day}")
            candidates = sorted(winners)
            continue
        state.emit_public(K_SHERIFF_VOTE, "警徽投票再次平票，本局没有警长。")
    state.sheriff_elected = True
    engine._announce()


def pk_round(engine: "Engine", tied: list[int]) -> tuple[list[int], dict[int, float]]:
    """平票 PK：平票者发言 + 重投（只能投平票者）。上台顺序沿用当天的发言方向与顺序。"""
    state = engine.state
    assert state is not None
    engine._phase("平票 PK", " → ".join(f"{s}号" for s in tied) + " 上台发言后重投")
    state.emit_public(K_VOTE_RESULT, "本轮平票：" + "、".join(f"{s}号" for s in tied) + " 进入 PK。")
    engine._announce()
    engine._speeches(engine._ordered_by_today(tied), KIND_SPEECH,
                     note="你在 PK 台上：说服大家不要把票投给你。", phase=f"pk_{state.day}")
    state.phase = f"pk_vote_{state.day}"
    return _ballot_round(
        engine, state.voters(), KIND_VOTE, list(tied),
        note="PK 重投：只能从平票玩家中选择（可弃票）。所有人同时亮票。",
        hit="{}号 PK 票投给 {}号", abstain="{}号 PK 弃票", weighted=True)
