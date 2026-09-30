"""整局流程与规则场景测试（脚本玩家，不发 LLM 请求）。"""
from __future__ import annotations

import random

from ..actors.fake import FakeActor
from ..engine.config import GameConfig, PRESETS, RuleFlags
from ..engine.decisions import (
    Ballot, Candidacy, SeerCheck, Shot, SpeakOrder, Speech, Transfer, WitchAction, WolfProposal, Withdraw,
)
from ..engine.engine import Engine
from ..engine.events import (
    PUBLIC, K_BADGE_DESTROYED, K_BADGE_TRANSFER, K_EXILE, K_IDIOT_REVEAL, K_SHOT, K_SPEECH, K_VOTE,
    K_VOTE_RESULT)
from ..engine.presenter import NullPresenter


def engine_for(cfg: GameConfig, actors_spec: dict[int, dict]) -> Engine:
    cfg.human_seat = None
    rng = random.Random(cfg.seed or 1)
    eng = Engine(cfg, {}, presenter=NullPresenter(), seed=cfg.seed or 1)
    eng.setup()
    actors = {}
    for seat in cfg.seats:
        spec = actors_spec.get(seat, {})
        actors[seat] = ScriptActor(seat, rng, spec)
    eng.actors = actors
    return eng


def make_config(preset_index: int, **flag_kw) -> GameConfig:
    preset = PRESETS[preset_index]
    flags = RuleFlags(**{k: v for k, v in flag_kw.items() if k in RuleFlags.__dataclass_fields__})
    for key, value in flag_kw.items():
        if key in RuleFlags.__dataclass_fields__:
            setattr(flags, key, value)
    return GameConfig(
        label=preset.label, n_seats=preset.config.n_seats, counts=dict(preset.config.counts),
        flags=flags, seed=7,
    )


class ScriptActor(FakeActor):
    def __init__(self, seat: int, rng: random.Random, spec: dict) -> None:
        super().__init__(seat, rng)
        self.spec = spec

    def _in_pool(self, value, ask):
        return value if value in list(ask.pool) else None

    def wolf_target(self, ask) -> WolfProposal:
        return WolfProposal(target=self._knife(ask), reason="剧本")

    def witch_action(self, ask) -> WitchAction:
        return WitchAction(
            save=bool(self.spec.get("save", False)) and self.spec.get("poison") is None,
            poison_target=self._in_pool(self.spec.get("poison"), ask), reason="剧本",
        )

    def seer_check(self, ask) -> SeerCheck:
        return SeerCheck(target=self._in_pool(self.spec.get("check"), ask) or ask.pool[0], reason="剧本")

    def candidacy(self, ask) -> Candidacy:
        return Candidacy(run=bool(self.spec.get("run", False)), reason="剧本")

    def withdraw(self, ask) -> Withdraw:
        return Withdraw(withdraw=bool(self.spec.get("withdraw", False)))

    def sheriff_vote(self, ask) -> Ballot:
        return Ballot(target=self._in_pool(self.spec.get("sheriff_vote"), ask))

    def speak_order(self, ask) -> SpeakOrder:
        return SpeakOrder(direction=self.spec.get("direction", 1))

    def speech(self, ask) -> Speech:
        return Speech(text="过。")

    def sheriff_speech(self, ask) -> Speech:
        return Speech(text="把警徽给我。")

    def last_words(self, ask) -> Speech:
        return Speech(text="遗言：过。")

    def vote(self, ask) -> Ballot:
        return Ballot(target=self._in_pool(self.spec.get("vote"), ask))

    def _knife(self, ask):
        if "knife_after" in self.spec:
            value = self.spec["knife"] if ask.day < self.spec.get("knife_from_day", 1) else self.spec["knife_after"]
        else:
            value = self.spec.get("knife")
        return self._in_pool(value, ask)

    def shot(self, ask) -> Shot:
        target = self._in_pool(self.spec.get("shoot"), ask)
        if target is None and not self.spec.get("no_shoot"):
            target = ask.pool[0]
        return Shot(target=target)

    def transfer(self, ask) -> Transfer:
        return Transfer(target=self._in_pool(self.spec.get("transfer"), ask) or ask.pool[0])


def _events(eng: Engine, kind: str):
    return [e for e in eng.state.log if e.kind == kind]


def _find_role(state, role: str) -> int:
    for seat, player in state.players.items():
        if player.role_id == role:
            return seat
    raise AssertionError(f"没有 {role}")


# ============================ 全流程 ============================
def test_all_presets_reach_a_result():
    from .harness import play
    for index in range(len(PRESETS)):
        for seed in (1, 2, 3):
            eng = play(index, seed=seed)
            assert eng.state.finished, (index, seed, "未结束")
            assert eng.winner in ("wolf", "good"), (index, seed, eng.winner)
            assert eng.state.day <= 30


def test_fallback_survives_actor_errors():
    from .harness import play
    for seed in (1, 2):
        eng = play(2, seed=seed, error_rate=0.35)
        assert eng.state.finished and eng.winner in ("wolf", "good")
        # 35% 的决策失败率必须真的走到过托管，否则"报错不炸局"是假的
        assert any("托管" in n for n in eng.presenter.notices), (
            seed, "错误率 35% 却没触发过一次托管 —— 注入根本没生效")


def test_each_player_speaks_at_most_once_per_day():
    from .harness import play
    for seed in (1, 2, 3):
        eng = play(2, seed=seed)
        by_phase: dict[tuple, list[int]] = {}
        for event in _events(eng, K_SPEECH):
            if not event.phase.startswith("speech_"):
                continue                      # 警上/PK 发言另算
            by_phase.setdefault((event.day, event.phase), []).append(event.payload["seat"])
        assert by_phase, seed
        for key, seats in by_phase.items():
            assert len(seats) == len(set(seats)), (seed, key, seats)


def test_votes_are_unique_per_day():
    from .harness import play
    for seed in (1, 2, 3):
        eng = play(2, seed=seed)
        by_phase: dict[tuple, list[int]] = {}
        for event in _events(eng, K_VOTE):
            if "voter" not in event.payload:
                continue
            by_phase.setdefault((event.day, event.phase), []).append(event.payload["voter"])
        assert by_phase, seed
        for key, voters in by_phase.items():
            assert len(voters) == len(set(voters)), (seed, key, voters)


# ============================ 规则场景 ============================
def test_hunter_poisoned_cannot_shoot():
    cfg = make_config(2, last_words="none")
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=7)
    probe.setup()
    hunter = _find_role(probe.state, "hunter")
    witch = _find_role(probe.state, "witch")
    victim = next(s for s in cfg.seats if s not in (hunter, witch) and not probe.state.players[s].is_wolf)
    wolves = [s for s in cfg.seats if probe.state.players[s].is_wolf]
    spec = {s: {"knife": victim} for s in wolves}
    spec[witch] = {"poison": hunter}
    eng = engine_for(cfg, spec)
    eng.run()
    assert eng.state.players[hunter].death_cause == "poison"
    shots = [e for e in _events(eng, K_SHOT) if e.audience == PUBLIC and e.payload.get("seat") == hunter]
    assert not shots, [e.text for e in shots]


def test_hunter_knifed_can_shoot():
    cfg = make_config(2, last_words="none")
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=7)
    probe.setup()
    hunter = _find_role(probe.state, "hunter")
    wolves = [s for s in cfg.seats if probe.state.players[s].is_wolf]
    witch = _find_role(probe.state, "witch")
    spec = {s: {"knife": hunter} for s in wolves}
    spec[hunter] = {"shoot": wolves[0]}
    spec[witch] = {}
    eng = engine_for(cfg, spec)
    eng.run()
    shots = [e for e in _events(eng, K_SHOT) if e.audience == PUBLIC and e.payload.get("seat") == hunter]
    assert shots, "猎人被刀应当可以开枪"
    assert any(e.payload.get("target") == wolves[0] for e in shots), (
        "猎人开枪该打中被指定那头狼")
    assert not eng.state.players[wolves[0]].alive, "开枪带走的狼必须死"


def test_idiot_exile_reveals_and_removes_vote():
    cfg = make_config(3, last_words="none", sheriff_enabled=False)
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=7)
    probe.setup()
    idiot = _find_role(probe.state, "idiot")
    spec = {s: {"vote": idiot} for s in cfg.seats}
    eng = engine_for(cfg, spec)
    eng.run()
    reveals = _events(eng, K_IDIOT_REVEAL)
    assert reveals, "白痴被放逐应翻牌"
    reveal = reveals[0]
    same_day_exile = [
        e for e in _events(eng, "exile")
        if e.payload.get("seat") == idiot and e.day == reveal.day
    ]
    assert not same_day_exile, "白痴翻牌那次不应同时被放逐出局"
    assert eng.state.players[idiot].can_vote is False
    second_day_votes = [
        e for e in _events(eng, K_VOTE)
        if e.payload.get("voter") == idiot and e.day > reveals[0].day
    ]
    assert not second_day_votes, "翻牌后不应再有投票"


def test_exiled_sheriff_must_settle_the_badge():
    """警长被放逐时也要当场决定警徽。原来只有夜间死亡会问，被票走的警长警徽会无声消失。"""
    cfg = make_config(2)
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=7)
    probe.setup()
    wolves = [s for s in cfg.seats if probe.state.players[s].is_wolf]
    goods = [s for s in cfg.seats if not probe.state.players[s].is_wolf]
    badge, heir = goods[0], goods[1]
    victim = next(s for s in goods if s not in (badge, heir))
    spec: dict[int, dict] = {seat: {"no_shoot": True} for seat in cfg.seats}
    spec[badge].update({"run": True, "transfer": heir, "vote": badge})
    for seat in cfg.seats:
        if seat != badge:
            spec[seat].update({"sheriff_vote": badge, "vote": badge})  # 第一天全场把警长票出去
    for seat in wolves:
        spec[seat].update({"knife": victim, "knife_after": victim})
    eng = engine_for(cfg, spec)
    eng.run()
    # Event 没有 .seat 字段，座位在 payload 里
    assert any(e.payload.get("seat") == badge for e in _events(eng, K_EXILE)), "警长应被放逐"
    settles = [e for e in eng.state.log if e.kind in (K_BADGE_TRANSFER, K_BADGE_DESTROYED)]
    assert settles, "警长被放逐必须当场移交或撕毁警徽"
    sheriff = eng.state.sheriff
    assert sheriff is None or eng.state.players[sheriff].alive, "警徽不该悬在死人手上"


def test_the_day_vote_is_weighed_by_the_badge():
    """白天放逐那轮也得按警徽权重计票 —— 三轮投票并成一条流程后，这事只由一个参数决定。

    票型刻意做成：警长那票押谁，谁就多 0.5 票。撤掉权重就变成 4:4 平票、进 PK、
    重投还是平票，整局谁都放逐不出去。
    """
    cfg = make_config(2)
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=7)
    probe.setup()
    goods = [s for s in cfg.seats if not probe.state.players[s].is_wolf]
    badge, x, y = goods[0], goods[1], goods[2]
    rest = [s for s in cfg.seats if s not in (badge, x, y)]
    spec: dict[int, dict] = {seat: {"no_shoot": True} for seat in cfg.seats}
    spec[badge]["run"] = True
    for seat in cfg.seats:
        if seat != badge:
            spec[seat]["sheriff_vote"] = badge          # 一致把警徽交给 badge
        spec[seat]["vote"] = None                       # 默认弃票，下面再分配
    for seat in [badge] + rest[:3]:
        spec[seat]["vote"] = x                          # 4 人投 x（含警长）
    for seat in rest[3:] + [x]:
        spec[seat]["vote"] = y                          # 4 人投 y
    eng = engine_for(cfg, spec)
    eng.run()
    exiled = [e.payload.get("seat") for e in _events(eng, K_EXILE)]
    assert exiled and exiled[0] == x, (exiled, "警长的 1.5 票没算进白天放逐投票")
    # PK 那轮本来就按权重计票：真出了 4:4，它会替白天的漏算兜底，所以还得钉住「没走到 PK」
    assert not any("PK 后票型" in e.text for e in _events(eng, K_VOTE_RESULT)), \
        "白天的票没按警徽加权，4:4 平票只能进 PK"


def test_dead_sheriff_transfers_badge():
    cfg = make_config(2)
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=7)
    probe.setup()
    wolves = [s for s in cfg.seats if probe.state.players[s].is_wolf]
    goods = [s for s in cfg.seats if not probe.state.players[s].is_wolf]
    badge, target = goods[0], goods[1]
    first_victim = next(s for s in goods if s not in (badge, target))
    spec: dict[int, dict] = {seat: {} for seat in cfg.seats}
    spec[badge] = {"run": True, "transfer": target}
    for seat in goods:
        if seat != badge:
            spec[seat]["sheriff_vote"] = badge
    for seat in wolves:
        spec[seat] = {
            "knife": first_victim, "knife_after": badge, "knife_from_day": 2, "no_shoot": True,
            "vote": None,
        }
    eng = engine_for(cfg, spec)
    eng.run()
    assert eng.state.sheriff is not None or eng.state.badge_destroyed, "警长应先当选"
    transfers = _events(eng, K_BADGE_TRANSFER)
    assert transfers, "警长死亡应触发警徽移交"
    assert transfers[0].payload.get("target") == target, transfers[0].text


def test_no_candidate_means_no_sheriff():
    cfg = make_config(2)
    eng = Engine(cfg, {s: ScriptActor(s, random.Random(s), {"run": False}) for s in cfg.seats},
                 presenter=NullPresenter(), seed=7)
    eng.setup()
    eng.run()
    assert eng.state.sheriff is None
    assert eng.state.sheriff_elected
    assert eng.state.finished

def test_word_limits_are_per_phase_but_never_cut_the_speech():
    """上限按阶段取（警上有自己的上限），但它是行为约束不是消音器：一个字都不截。

    截断会让场上少一条真实存在的证词，而复盘时也追不回来；超限只在上帝视角记一笔。
    """
    from ..engine.ask import KIND_CANDIDACY, KIND_LAST_WORDS, KIND_WITHDRAW, word_limit_for
    from ..engine.config import default_config
    from ..engine.engine import WORD_HARD_CEILING
    from ..engine.events import K_DEBUG, K_SHERIFF_SPEECH

    assert RuleFlags().speech_word_limit == 500
    assert RuleFlags().sheriff_word_limit == 500

    config = default_config()
    config.flags.speech_word_limit = 20
    config.flags.sheriff_word_limit = 60
    engine = engine_for(config, {})                    # 要有 state，超限记录才写得到
    assert word_limit_for(K_SPEECH, config.flags) == 20          # 阶段各按各的上限
    assert word_limit_for(K_SHERIFF_SPEECH, config.flags) == 60
    # 只有"要说话"的环节才有上限：举手 / 退水是被问出「发言长度上限」重灾区
    assert word_limit_for(KIND_CANDIDACY, config.flags) == 0
    assert word_limit_for(KIND_WITHDRAW, config.flags) == 0
    assert word_limit_for(KIND_LAST_WORDS, config.flags) == 20   # 遗言不是双倍额度

    long_speech = "话" * 90
    assert engine._clean_words(long_speech) == long_speech, "发言上限不该截断正文"

    before = len(engine.state.log)
    engine._notes_over_limit(7, long_speech, word_limit_for(K_SHERIFF_SPEECH, config.flags), "警上发言")
    noted = [e for e in engine.state.log[before:] if e.kind == K_DEBUG]
    assert noted and "90 字" in noted[0].text and "60 字" in noted[0].text
    engine._notes_over_limit(7, "话" * 50, word_limit_for(K_SHERIFF_SPEECH, config.flags))
    assert not [e for e in engine.state.log[before + 1:] if e.kind == K_DEBUG], "没超限不该记"

    runaway = engine._clean_words("话" * (WORD_HARD_CEILING + 500))   # 只有失控输出才截
    assert len(runaway) == WORD_HARD_CEILING and runaway.endswith("…")


def test_the_context_window_is_wide_enough_to_recall_the_game():
    """长期记忆 = 前情提要（条数 × 每条长度）+ 每个玩家的滚动窗口。

    这两个数值很容易在改提示词时被顺手调小，而一旦调小，玩家就开始「忘了」前面说过
    什么 —— 局内几乎发现不了，所以钉在这里。
    """
    import inspect

    from ..actors.llm_player import LLMActor
    from ..engine.build import RECAP_EVENTS, _clip
    from .harness import play

    assert RECAP_EVENTS >= 20, "前情提要该带多少条事件"
    assert inspect.signature(_clip).parameters["limit"].default >= 90, "一条发言该留多少字"
    assert inspect.signature(LLMActor.__init__).parameters["keep_pairs"].default >= 14, "滚动窗口"

    widest = 0
    for seed in (1, 2, 3, 5, 12):
        eng = play(2, seed=seed)
        ask = eng.builder.build(eng.state.alive_seats()[0], K_VOTE)
        spoken = [ln for ln in ask.recap if "「" in ln]      # 提要里的发言条目
        widest = max(widest, len(spoken))
    assert widest >= 15, f"一局跑到最后，前情提要里只剩 {widest} 条发言"
def test_the_dead_are_told_only_whether_they_may_shoot():
    """死因**不**私下告诉死者：告诉他「你是被毒死的」，他一句遗言就把女巫的用药卖给了全场。

    能开枪的角色只该知道「这次能不能开枪」。真死因留在上帝视角的夜结算里 —— 那是 GM 受众，
    `visible()` 决定它永远进不了任何玩家的 Ask（连死者自己的记忆也进不去）。
    """
    from ..engine.events import K_DEATH_CAUSE, GM

    def private_notes(eng: Engine, seat: int) -> list[str]:
        return [e.text for e in _events(eng, K_DEATH_CAUSE)
                if e.audience not in (PUBLIC, GM) and seat in e.audience]

    for mode in ("knife", "poison"):
        cfg = make_config(2, last_words="all")
        probe = Engine(cfg, {}, presenter=NullPresenter(), seed=7)
        probe.setup()
        hunter = _find_role(probe.state, "hunter")
        witch = _find_role(probe.state, "witch")
        wolves = [s for s in cfg.seats if probe.state.players[s].is_wolf]
        if mode == "knife":                                  # 狼队刀猎人
            spec = {seat: {"knife": hunter} for seat in wolves}
            spec[witch] = {}
        else:                                                # 女巫毒猎人（狼刀别人）
            victim = next(s for s in cfg.seats
                          if s not in (hunter, witch) and not probe.state.players[s].is_wolf)
            spec = {seat: {"knife": victim} for seat in wolves}
            spec[witch] = {"poison": hunter}
        eng = engine_for(cfg, spec)
        eng.run()

        assert eng.state.players[hunter].death_cause == mode, mode
        notes = private_notes(eng, hunter)
        assert notes, (mode, "猎人该收到「能不能开枪」的私下告知")
        for text in notes:                                   # 只说能不能开枪，不说为什么
            assert not any(word in text for word in ("毒", "狼", "刀", "放逐")), (mode, text)
        assert any(("可以开枪" in text) == (mode == "knife") for text in notes), (mode, notes)


def test_empty_knife_night_tells_the_witch_the_truth():
    """狼空刀那一夜，不许对女巫说「你已用完两瓶药」。

    两瓶药都在的人被告知「你已看过刀口」，她就会把这条没有的信息当事实带到发言与遗言里
    （「法官没提刀口，说明是空刀」）—— 上面那三层可见性拦不住，因为这是引擎亲口说的假话。
    顺手钉住第二件事：救不救得了是引擎给的 `save_pool`，不是演员从文案里嗅出来的。
    """
    cfg = make_config(2)                                  # 9 人标准局：预女猎
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=cfg.seed)
    probe.setup()
    witch = _find_role(probe.state, "witch")
    wolves = [s for s, player in probe.state.players.items() if player.is_wolf]
    victim = next(s for s in probe.state.alive_seats()
                  if s != witch and not probe.state.players[s].is_wolf)

    asked: list = []

    class Spy(ScriptActor):
        def witch_action(self, ask):
            asked.append(ask)
            return super().witch_action(ask)

    def run_with(spec: dict):
        asked.clear()
        eng = engine_for(cfg, spec)
        eng.actors[witch] = Spy(witch, random.Random(3), {})
        eng.run()
        assert asked, "一夜都没问到女巫"
        return asked[0]

    night = run_with({seat: {} for seat in wolves})        # 狼队全体空刀
    assert "空刀" in night.note, night.note
    assert "用完两瓶药" not in night.note, night.note
    assert night.save_pool == (), night.save_pool          # 没人被刀，解药无从使用

    night = run_with({seat: {"knife": victim} for seat in wolves})
    assert night.note == f"今夜被刀的是 {victim}号。", night.note
    assert night.save_pool == (victim,), night.save_pool


def test_the_witch_is_never_offered_herself_or_a_poison_she_has_not_got():
    """女巫能毒谁、还能不能毒，是引擎算给她的一份数据 —— 不是一张点了算数、回头又作废的按钮。

    以前 `pool` 直接给全体存活座位：① 里面含她自己（卡片上就有「毒 6号」这个按钮，而 6号就是她），
    ② 第二瓶药用完之后每一夜还在问她毒谁。两种情况下她选谁都事后被丢掉（结算写「毒药 未用」），
    可她已经做过一次选择、AI 还为此编好了一段用药理由，然后带着它去发言和留遗言。
    """
    cfg = make_config(2)                                  # 9 人标准局：预女猎
    probe = Engine(cfg, {}, presenter=NullPresenter(), seed=cfg.seed)
    probe.setup()
    witch = _find_role(probe.state, "witch")
    asked: list = []

    class Spy(ScriptActor):
        def witch_action(self, ask):
            asked.append(ask)
            return WitchAction(save=False, poison_target=self.spec.get("poison"), reason="剧本")

    def run_night(spec: dict, *, heal_used: bool = False, poison_used: bool = False):
        asked.clear()
        eng = engine_for(cfg, {})
        eng.state.players[witch].heal_used = heal_used     # 今晚她手里还剩哪瓶药
        eng.state.players[witch].poison_used = poison_used
        eng.actors[witch] = Spy(witch, random.Random(5), spec)
        eng._night()
        return eng, (asked[0] if asked else None)

    eng, ask = run_night({})
    assert ask is not None, "第一夜就该问女巫"
    assert witch not in ask.pool, f"毒药池里出现了她自己的 {witch}号：{ask.pool}"
    assert set(ask.pool) == set(eng.state.players) - {witch}, (ask.pool, sorted(eng.state.players))

    _eng, ask = run_night({}, heal_used=True, poison_used=True)
    assert ask is None, "两瓶药都用完了还在问她用药：不管她答什么都会掉进「毒药 未用」"

    eng, ask = run_night({"poison": witch})               # 演员硬要毒自己（模型真会这么输出）
    assert ask is not None
    assert eng.state.nights[-1].poisoned_by_witch is None, "自毒被引擎当成了真行动"


def test_an_edge_board_needs_gods_and_villagers_that_are_actually_dealt():
    """屠边校验数的是**发得出去的座位**，不是 counts 里出现过哪些键。

    数量 0 的角色键照样在字典里（自定义板子就是这么拼出来的），按键判断等于
    「0 个神职也算有神职」：板子合法、开局合法，第 1 天法官宣布
    「神职已全部出局（屠边）」—— 狼队一句话没说就赢了。
    """
    gods = ("seer", "witch", "hunter", "idiot")
    empty_gods = make_config(3)                           # 12 人竞技板，把神职全清零
    wolves = sum(c for r, c in empty_gods.counts.items()
                 if r not in gods and r != "villager")
    # 神职的键**留在字典里、数量为 0**（自定义板子就是这么拼出来的）：按键判断才会看成「有神职」
    empty_gods.counts = {**{role: 0 for role in gods}, "wolf": wolves,
                         "wolf_king": 0, "villager": empty_gods.n_seats - wolves}
    empty_gods.flags.win_condition = "edge"
    try:
        empty_gods.validate()
    except ValueError as exc:
        assert "神职" in str(exc), exc
    else:
        raise AssertionError("0 个神职的屠边板子照样通过了校验")

    empty_folks = make_config(2)                          # 9 人标准板，把村民全清零给神职
    empty_folks.counts = {"wolf": 3, "seer": 2, "witch": 2, "hunter": 1, "idiot": 1, "villager": 0}
    empty_folks.flags.win_condition = "edge"
    try:
        empty_folks.validate()
    except ValueError as exc:
        assert "村民" in str(exc), exc
    else:
        raise AssertionError("0 个村民的屠边板子照样通过了校验")


def test_a_tied_tally_does_not_push_the_wolf_to_the_lowest_seat():
    """票型并列时，脚本狼跟谁必须是随机的。

    法官给的票型按编号升序排（`5号 1票、9号 1票`），要是演员只认文本里第一个座位，
    刚删掉的「平票取编号最小」就等于换了个地方活着。
    """
    from ..actors.fake import _tally_leads
    from ..engine.ask import Ask, KIND_WOLF_TARGET

    note = "上一轮的票型：5号 1票、9号 1票。这一轮可以坚持，也可以改。"
    assert sorted(_tally_leads(note)) == [5, 9], _tally_leads(note)
    assert _tally_leads("这一轮你看不到队友投了谁。") == [], "没给票型时不该读出东西"

    ask = Ask(kind=KIND_WOLF_TARGET, day=1, phase="night_1", seat=3, teammates=(3, 6, 8),
              pool=(5, 9, 11), must_choose=False, note=note)
    hits: dict[int | None, int] = {}
    for seed in range(400):
        got = FakeActor(3, random.Random(seed)).wolf_target(ask).target
        hits[got] = hits.get(got, 0) + 1
    assert hits[5] and hits[9], f"并列的两个目标都该被跟到：{hits}"
    assert hits[5] < hits[9] * 2, f"跟票明显偏向小编号：{hits}"


def test_the_tally_text_the_judge_prints_is_the_text_the_wolves_read():
    """法官票型文本与脚本狼的解析是一根线，措辞改了必须同步。

    脚本狼只看得见题面，拿不到结构化票型（这是演员层的边界，不能为了省事给它开洞）。
    于是「引擎怎么拼票型」和「演员怎么读票型」之间有一根看不见的线：哪天有人把
    「5号 2票」改成「5号 2 票」或者把「票型：」换成别的词，脚本狼就悄悄不再跟票 ——
    测试全绿，只是空刀率莫名升高。这条测试就是那根线的接头。
    """
    from ..actors.fake import _tally_leads
    from ..engine.engine import Engine
    from ..engine.resolve import describe_tally, plurality

    for picks in ({6: 5, 8: 5, 9: 7}, {6: 1, 8: 2}, {3: 9, 4: 9, 5: 9}, {6: 11, 8: 11}):
        leaders, counts = plurality(picks, {s: 1.0 for s in picks})
        # `_wolf_note` 只用到模块常量，不碰实例状态，所以可以直接借类来问它
        note = Engine._wolf_note(None, describe_tally(counts))
        assert sorted(_tally_leads(note)) == sorted(leaders), (picks, note, _tally_leads(note))
