"""警长参选得由人格驱动：不能老是狼人抢警徽，也不能把参选做成阵营偏好。"""
from __future__ import annotations

import random

from ..engine.config import PRESETS, GameConfig
from ..engine.persona import ROLE_CANDID_BONUS, sample_personas
from ..engine.roles import VILLAGER, WOLF, WOLF_KING

TRIALS = 400


def _sample_runs(preset_index: int = 2, trials: int = TRIALS, seed0: int = 1000):
    """返回 (狼人参选次数, 狼人次, 好人人参选次数, 好人次, 全体零参选局数, 狼包揽候选局数, 候选数)"""
    cfg = PRESETS[preset_index].config
    wolf_runs = wolf_total = good_runs = good_total = 0
    zero_runs = 0
    wolf_sweeps = 0
    candidate_counts: list[int] = []
    for n in range(trials):
        rng = random.Random(seed0 + n)
        assignment = cfg.build_deck(rng)
        personas = sample_personas(assignment, rng)
        runners = [s for s, p in personas.items() if p.run_for_sheriff]
        candidate_counts.append(len(runners))
        wolves = [s for s, r in assignment.items() if r in (WOLF, WOLF_KING)]
        goods = [s for s, r in assignment.items() if r not in (WOLF, WOLF_KING)]
        wolf_runs += sum(1 for s in runners if s in wolves)
        wolf_total += len(wolves)
        good_runs += sum(1 for s in runners if s in goods)
        good_total += len(goods)
        if not runners:
            zero_runs += 1
        if runners and all(s in wolves for s in runners):
            wolf_sweeps += 1
    return {
        "wolf_rate": wolf_runs / wolf_total,
        "good_rate": good_runs / good_total,
        "zero_rate": zero_runs / trials,
        "sweep_rate": wolf_sweeps / trials,
        "avg_candidates": sum(candidate_counts) / len(candidate_counts),
        "max_candidates": max(candidate_counts),
    }


def test_wolf_role_bonus_is_zero():
    assert ROLE_CANDID_BONUS[WOLF] == 0.0
    assert ROLE_CANDID_BONUS[WOLF_KING] == 0.0
    assert ROLE_CANDID_BONUS[VILLAGER] == 0.0
    assert all(v >= 0.0 for v in ROLE_CANDID_BONUS.values())
    assert ROLE_CANDID_BONUS["seer"] > ROLE_CANDID_BONUS[WOLF]


def test_candidacy_rate_not_biased_towards_wolves():
    stats = _sample_runs()
    gap = stats["wolf_rate"] - stats["good_rate"]
    assert abs(gap) < 0.08, stats
    assert stats["wolf_rate"] < 0.5, stats


def test_not_every_game_has_no_candidate_and_not_too_many():
    stats = _sample_runs()
    assert stats["zero_rate"] < 0.05, stats
    assert stats["max_candidates"] <= 6, stats
    assert 1.0 <= stats["avg_candidates"] <= 6.0, stats


def test_wolves_do_not_sweep_the_ticket():
    stats = _sample_runs()
    assert stats["sweep_rate"] < 0.05, stats


def test_candidacy_is_reproducible_with_seed():
    first = _sample_runs(trials=20, seed0=4242)
    second = _sample_runs(trials=20, seed0=4242)
    assert first == second


def test_different_seeds_give_different_candidate_sets():
    cfg = PRESETS[2].config
    signatures = set()
    for n in range(25):
        rng = random.Random(900 + n)
        assignment = cfg.build_deck(rng)
        personas = sample_personas(assignment, rng)
        # 键里绝不能放发牌结果：每个种子的牌堆本来必然不同，那样这条断言恒成立 ——
        # 而要检查的恰恰是**候选集**随不随种子变（分组键不能来自被检量本身）。
        signatures.add(tuple(sorted(s for s, p in personas.items() if p.run_for_sheriff)))
    assert len(signatures) > 1, f"候选集完全不随种子变化：{sorted(signatures)}"


def test_the_human_seat_gets_no_persona():
    """真人座位不抽人格：他的风格就是他当下的心情。人格只为 AI 存在。"""
    from dataclasses import replace

    from ..engine.engine import Engine
    from ..engine.presenter import NullPresenter

    base = PRESETS[1].config
    cfg = replace(base, counts=dict(base.counts), flags=replace(base.flags), human_seat=3, seed=5)
    eng = Engine(cfg, {}, presenter=NullPresenter(), seed=5)
    eng.setup()
    assert eng.state.personas.get(3) is None, "真人座位不该有人格"
    assert all(s in eng.state.personas for s in cfg.seats if s != 3), "AI 座位都要有人格"


def test_speech_length_habit_is_personality_driven():
    """发言长短来自人格抽样：跨轮稳定（存在人格里）、三档都抽得出来、人人有条口头习惯。

    刻意跑一把种子而不是锁一把：这条要盯的是"这根轴在不在抽样"。锁死单一种子的话，
    档位边界挪一下、或者人格多抽一个数，都可能让那一把牌全撞进同一档而用例莫名变红。
    """
    import random

    from ..engine.persona import sample_personas
    from ..engine.roles import SEER, VILLAGER, WITCH, WOLF

    assignment = {1: WOLF, 2: WOLF, 3: SEER, 4: WITCH, 5: VILLAGER, 6: VILLAGER}
    tiers = {"40~90": 0, "100~180": 0, "200~300": 0}
    quirks: set = set()
    for seed in range(1, 41):
        for persona in sample_personas(assignment, random.Random(seed)).values():
            style = persona.style
            hits = [tier for tier in tiers if tier in style]
            assert len(hits) == 1, ("每条人格都得恰好写一档字数", style, hits)
            tiers[hits[0]] += 1
            assert "口头习惯：" in style, style     # 八个座位光靠"稳健/激进"分不出声音
            quirks.add(style.split("口头习惯：")[1].split("；")[0])
    assert all(count > 0 for count in tiers.values()), f"有一档字数从没被抽出来过：{tiers}"
    assert len(quirks) >= 5, f"40 局抽不出五种口头习惯，等于没有口头习惯：{quirks}"


def test_a_seat_holds_its_line_or_moves_it():
    """「坚持己见 ↔ 墙头草」是一根真在抽样的轴，而且和「信不信人」各管一件事。

    一根轴只管一件事，否则模型会收到自相矛盾的指令：`stubborn` 管**已经说出口的判断改不改**，
    `doubt` 管**信不信别人跳的身份**。以前 doubt 的低端写着"别人讲得顺你就容易跟"，
    那其实就是墙头草本身，两根轴同时在时会互相打架 —— 这里一起钉死。
    """
    import random

    from ..engine.persona import sample_personas
    from ..engine.roles import SEER, VILLAGER, WITCH, WOLF

    HOLD = "自己定的票不轻易改"
    MOVE = "改主意快"
    assignment = {1: WOLF, 2: WOLF, 3: SEER, 4: WITCH, 5: VILLAGER, 6: VILLAGER}
    shapes: set = set()
    doubts: set = set()
    for seed in range(1, 61):
        for persona in sample_personas(assignment, random.Random(seed)).values():
            style = persona.style
            holds, moves = HOLD in style, MOVE in style
            assert not (holds and moves), ("同一个人不许又被要求死扛、又被要求随风倒", style)
            shapes.add((holds, moves))
            assert "容易跟" not in style, ("跟风这话归 stubborn 管，doubt 不许再写", style)
            doubts.add("疑心重" if "疑心重" in style else
                       ("先信三分" if "先信三分" in style else "中段"))
    # 三种人得上桌：死扛的、随风倒的、以及没被特别交代的中间档
    assert shapes == {(True, False), (False, True), (False, False)}, shapes
    assert {"疑心重", "先信三分"} <= doubts, f"疑心这根轴的两端没都抽出来：{doubts}"


def test_the_spotlight_rides_on_the_mic_axis():
    """风头（原 vanity）并进抢麦欲：一根轴两头各有一种爱法，不再单独抽一根。

    单独一根 vanity 时，模型会同时收到「抢麦定节奏」和「很在意风头」两句同一类的话。
    合并后：抢麦的高端是前台抢风头，低调那端是后台攒话语权 —— 两头都得在，且互斥。
    """
    import random

    from ..engine.persona import sample_personas
    from ..engine.roles import SEER, VILLAGER, WITCH, WOLF

    HI, LO = "很在意风头", "很在意自己在这个局里的位置"
    assignment = {1: WOLF, 2: WOLF, 3: SEER, 4: WITCH, 5: VILLAGER, 6: VILLAGER}
    counts = {HI: 0, LO: 0}
    for seed in range(1, 41):
        for persona in sample_personas(assignment, random.Random(seed)).values():
            style = persona.style
            assert "风头和你在这个局里的位置" not in style, ("旧的独立 vanity 句子回潮了", style)
            front, back = HI in style, LO in style
            assert not (front and back), ("前台抢风头与后台攒话语权不可能同时是这个人", style)
            if front:
                assert "第一个开口" in style, ("风头这句必须挂在抢麦那一档上", style)
            counts[HI] += front
            counts[LO] += back
    assert counts[HI] and counts[LO], f"合并后的两头没都抽出来过：{counts}"


def test_the_persona_draw_borrows_one_number_from_the_game_rng():
    """人格抽样只准从牌桌的 rng 里取走一个数。

    这是给"以后加轴"上的保险：轴数一变，共用主 rng 的取数位置就整体平移，
    于是发牌、刀口、票型跟着全变，一堆和人格无关的用例集体变红（每改一次人格就得重定种子）。
    """
    import random

    from ..engine import persona
    from ..engine.roles import SEER, VILLAGER, WOLF

    class Counting(random.Random):
        calls = 0

        def getrandbits(self, width):
            Counting.calls += 1
            return random.Random.getrandbits(self, width)

    persona.sample_personas({1: WOLF, 2: SEER, 3: VILLAGER}, Counting(9))
    assert Counting.calls == 1, f"主 rng 被多取了 {Counting.calls} 个数，加轴会平移整局"


def test_an_extra_axis_would_change_nobody_but_the_seat_himself():
    """再加一根说话轴，只准改到那一个座位自己的描述：别人是否参选、主 rng 都不许动。

    现在并没有第十根轴，这里临时插一根进去 —— 验的是"以后加轴"这件事不连锁。
    """
    import random

    from ..engine import persona
    from ..engine.roles import VILLAGER, WOLF

    assignment = {seat: (WOLF if seat <= 2 else VILLAGER) for seat in range(1, 7)}
    before = persona.sample_personas(assignment, random.Random(20))
    assert persona.sample_personas(assignment, random.Random(20)) == before, "抽样本身不可复现"

    original = persona.AXES
    persona.AXES = original + ("posture",)
    try:
        after = persona.sample_personas(assignment, random.Random(20))
    finally:
        persona.AXES = original

    assert {s: p.run_for_sheriff for s, p in after.items()} == \
        {s: p.run_for_sheriff for s, p in before.items()}, "加一根说话轴把谁参选给写掉了"
    assert any(after[s].style != before[s].style for s in before), "新轴没进描述，这条测试在空转"
    for persona_obj in after.values():
        assert "口头习惯：" in persona_obj.style, persona_obj.style


def test_personas_store_only_what_something_reads():
    """Persona 上不许存没人读的数。

    以前存着 aggression / vanity / logic_first，全仓库除了抽样处自己没别人读 ——
    看着像模型参数，其实只是写描述时顺手取的局部变量（存着还让人以为行为受它们管）。
    """
    import dataclasses

    from ..engine.persona import Persona

    assert {f.name for f in dataclasses.fields(Persona)} == {
        "seat", "mic_desire", "run_for_sheriff", "style"}


def test_reasoning_discipline_is_in_the_prompt():
    """提示词里要有可判定的推理纪律，不是空喊"讲逻辑"。"""
    from ..actors.style import BEHAVIOR_RULES

    assert "讲逻辑" in BEHAVIOR_RULES
    for needle in ("事实", "推测", "错在哪一步", "必须给出理由"):
        assert needle in BEHAVIOR_RULES, needle
    assert "别为了凑字数写满它" in BEHAVIOR_RULES


def _knife(plan: list[list[int]]):
    """按给定的每轮票跑一次狼队表决，返回 (记录, 每只狼看到的题面, 播给狼队的事件, 狼座, 可选目标)。

    `plan[i][r]` = 第 i 只狼第 r 轮投谁：**池子里的第几个**（0 起）；`-1` = 空刀；`-2` = 提队友。
    用代号不写死座位，是因为换seed狼座就变了 —— 写死会把队友投出去还以为在谈刀口。
    """
    from ..engine.decisions import WolfProposal
    from ..engine.engine import Engine
    from ..engine.events import K_WOLF_VOTE
    from ..engine.presenter import NullPresenter
    from ..engine.state import NightRecord

    cfg = GameConfig(n_seats=9, counts={"wolf": 3, "seer": 1, "witch": 1, "villager": 4})
    engine = Engine(cfg, {}, presenter=NullPresenter(), seed=1)
    engine.setup()
    pack = sorted(s for s, pl in engine.state.players.items() if pl.is_wolf)
    pool = sorted(s for s, pl in engine.state.players.items() if not pl.is_wolf)
    assert len(pack) == len(plan), "排练的狼数得对得上板子"

    class _Voter:
        is_human = False

        def __init__(self, seat: int, codes: list[int], index: int) -> None:
            self.seat, self.codes, self.index = seat, codes, index
            self.notes: list[str] = []
            self.rounds: list[tuple[int, str]] = []   # 每次被问时：档案里已写了几个夜、狼队表决那条长啥样
            self.calls = 0

        def decide(self, ask):
            self.notes.append(ask.note)
            verdict = [line for line in ask.dossier if line.startswith("【狼队表决】")]
            self.rounds.append((len([n for n in engine.state.nights if n.wolf_rounds]),
                                verdict[0] if verdict else ""))
            code = self.codes[min(self.calls, len(self.codes) - 1)]
            self.calls += 1
            if code == -1:
                target = None
            elif code == -2:
                target = pack[(self.index + 1) % len(pack)]      # 投给队友，引擎必须作废
            else:
                target = pool[code]
            return WolfProposal(target=target, reason="就他最像")

        def finalize(self) -> None:
            pass

    voters = [_Voter(seat, codes, i) for i, (seat, codes) in enumerate(zip(pack, plan))]
    engine.actors = {v.seat: v for v in voters}
    engine.state.day += 1
    record = NightRecord(day=engine.state.day)
    engine.state.nights.append(record)
    engine._wolf_vote(tuple(pack), record)
    texts = [e.text for e in engine.state.log if e.kind == K_WOLF_VOTE]
    return record, [v.notes for v in voters], texts, [v.rounds for v in voters], tuple(pack), tuple(pool)


def test_the_wolf_knife_is_decided_by_the_votes():
    """狼队刀口跟着票走：凑到唯一最高票就定；提队友的票作废；全队空刀就空刀。"""
    record, _notes, texts, _dossier, _pack, pool = _knife([[0, 0], [0, 0], [1, 1]])
    assert (record.wolf_target, record.wolf_rounds) == (pool[0], 1), "两票对一票，第 1 轮就该定"
    assert f"最终决定击杀：{pool[0]}号" in texts[0], texts

    record, _notes, texts, _dossier, _pack, _pool = _knife([[-1], [-1], [-1]])
    assert (record.wolf_target, record.wolf_rounds) == (None, 1), "全队空刀，没必要再烧两轮"
    assert "空刀" in texts[0], texts

    record, _notes, _texts, _dossier, pack, _pool = _knife([[-2], [-2], [-2]])
    assert record.wolf_target is None, "互相提队友的票必须作废（题面池子里没有队友）"
    assert record.wolf_rounds == 1, "票全作废等于全队没开口，别再烧两轮"


def test_a_stalemate_goes_the_full_three_rounds_and_nobody_dies():
    """投不到唯一最高票就复投；投满三轮还没定 —— 空刀。

    旧实现是「平票取编号最小」：300 局实测 45% 的夜靠它兜底，结果 1 号被刀 182 次、
    12 号 75 次，狼队没谈拢的代价全压在低号位玩家身上。所以这里要把两个目标对调再跑一遍：
    **两边都必须是空刀**，谁也不许被规则指定去死。
    """
    for plan in ([[0, 0, 0], [1, 1, 1], [-1, -1, -1]],
                 [[1, 1, 1], [0, 0, 0], [-1, -1, -1]]):
        record, _notes, texts, _dossier, _pack, _pool = _knife(plan)
        assert record.wolf_target is None, f"一对一平票不该由规则指定谁死：{plan} {texts}"
        assert record.wolf_rounds == 3, f"应该投满三轮才放弃：{plan} → {record.wolf_rounds} 轮"
        assert "空刀" in texts[-1], texts[-1]


def test_the_night_under_vote_is_not_written_into_the_dossier():
    """还没投出结论的这一夜，不许出现在狼的私密档案里。

    档案按 `wolf_rounds` 挑「表决过的夜」，早先在每轮开头就写这个数，于是复投时狼自己的
    行动卡片上就写着「【狼队表决】第1夜 空刀（2轮）」—— 票还没收齐，法官先替这夜定了案。
    投出结论之后它当然该进档案（复盘、下一夜都要用），所以这里两头都断言。
    """
    record, _notes, texts, dossiers, _pack, _pool = _knife([[0, 0, 0], [1, 1, 1], [-1, -1, 0]])
    assert record.wolf_rounds >= 2, f"这条要跑过复投才有意义：{texts}"
    assert all(seat_rounds for seat_rounds in dossiers), "夹具没被问到就等于这条用例是空转的"
    for seat_rounds in dossiers:
        for asked_at, (settled_nights, verdict_line) in enumerate(seat_rounds):
            assert f"第{record.day}夜" not in verdict_line, \
                f"第{asked_at + 1}次被问时，档案里已经有了正在表决的这一夜：{verdict_line}"
    assert record.wolf_rounds, "投出结论后必须写进档案（下一夜与复盘要看）"


def test_the_pack_sees_the_tally_between_rounds():
    """复投前，上一轮的票型必须送到每只狼手上 —— 这就是线下那套“比数字”。"""
    record, notes, texts, _dossier, _pack, pool = _knife([[0, 0, 0], [1, 1, 1], [-1, -1, 0]])
    assert record.wolf_rounds >= 2, texts
    assert all("票型" not in seen[0] for seen in notes), "第 1 轮没有上一轮，不该有票型"
    assert all("票型" in seen[1] for seen in notes), [seen[1] for seen in notes]
    assert f"{pool[0]}号 1票" in notes[0][1], notes[0][1]
    # 端到端闭环：引擎拼出的那句题面，必须能被狼手里那个解析器读回同一批领先目标
    from ..actors.fake import _tally_leads
    assert sorted(_tally_leads(notes[0][1])) == sorted([pool[0], pool[1]]), notes[0][1]
    assert "最终决定" not in notes[0][1], "题面里不该替狼队写结论"
    for text in texts:
        assert "狼队表决" in text, text


def test_sheriff_ballot_outside_the_pool_is_abstain():
    """警徽票只能投候选人：池外的票只算**弃票**，不改写得票人。

    这条兜的是「大模型乱填一个座位号」。偶数座位故意把警徽投给自己（池子里没有自己），
    奇数座位老实投给池子里的第一个候选人 —— 前者必须落成弃票，后者不许被记成自投。
    把 `_ballot_round` 的池内校验放宽，这条立刻变红。
    """
    import random
    from dataclasses import replace

    from ..actors.fake import FakeActor
    from ..engine.decisions import Ballot, Candidacy, Withdraw
    from ..engine.engine import Engine
    from ..engine.events import K_SHERIFF_VOTE
    from ..engine.presenter import NullPresenter

    base = PRESETS[2].config
    cfg = replace(base, seed=4, flags=replace(base.flags, sheriff_enabled=True,
                                            vote_reveal=True))
    engine = Engine(cfg, {}, presenter=NullPresenter(), seed=4)
    engine.setup()

    class _HalfSelfish(FakeActor):
        """候选人固定成 1/2/3 号，其余人都要投票 —— 两种票都跑得到。"""

        def candidacy(self, ask):
            return Candidacy(run=self.seat <= 3)

        def withdraw(self, ask):
            return Withdraw(withdraw=False)

        def sheriff_vote(self, ask):
            if self.seat % 2 == 0:
                return Ballot(target=self.seat)      # 池外：池子里没有自己
            return Ballot(target=ask.pool[0])        # 池内第一个候选人

    engine.actors = {s: _HalfSelfish(s, random.Random(s)) for s in cfg.seats}
    engine.run()

    revealed = [e for e in engine.state.log if e.kind == K_SHERIFF_VOTE and "voter" in e.payload]
    assert revealed, "亮票规则开着，该播出投票明细"
    selfish = [e for e in revealed if e.payload["voter"] % 2 == 0]
    honest = [e for e in revealed if e.payload["voter"] % 2 == 1]
    assert selfish and honest, "两种票都该被问到过"
    assert all(e.payload["target"] is None for e in selfish), \
        [f'{e.payload["voter"]}号→{e.payload["target"]}' for e in selfish]
    assert all("弃票" in e.text for e in selfish), [e.text for e in selfish]
    assert all(e.payload["target"] not in (None, e.payload["voter"]) for e in honest), \
        [f'{e.payload["voter"]}号→{e.payload["target"]}' for e in honest]


def test_a_seats_voice_does_not_depend_on_its_camp():
    """人格描述（含口头习惯、黑话密度、嘴硬/留余地）必须和阵营**严格无关**。

    这条不靠统计，靠结构：把同一批座位的身份对调，用同一个种子重抽，每个人的风格描述
    必须一字不差。只要 `_style` 或抽样偷看了 role_id，对调之后就会露出来。
    否则「狼发言更飘」这种牌面信息会被提示词白送出去（AGENTS.md：人格不能变成阵营偏差）。
    """
    import inspect

    from ..engine.persona import candid_probability, sample_personas
    from ..engine.roles import HUNTER, SEER, VILLAGER, WITCH, WOLF, WOLF_KING

    seats = [1, 2, 3, 4, 5, 6, 7]
    roles_a = {1: WOLF, 2: WOLF, 3: WOLF, 4: SEER, 5: WITCH, 6: HUNTER, 7: VILLAGER}
    roles_b = {1: VILLAGER, 2: SEER, 3: WITCH, 4: WOLF, 5: WOLF_KING, 6: WOLF, 7: HUNTER}
    for seed in (3, 17, 42):
        a = sample_personas(roles_a, random.Random(seed))
        b = sample_personas(roles_b, random.Random(seed))
        for seat in seats:
            assert a[seat].style == b[seat].style, (seed, seat, a[seat].style, b[seat].style)

    # 新加的几条说话轴也不许偷偷进参选概率：那个函数只准看抢麦欲、身份和竞选氛围
    assert list(inspect.signature(candid_probability).parameters) == ["mic_desire", "role_id", "mood"]
