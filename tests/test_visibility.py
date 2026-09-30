"""可见性与快照不变量：玩家上下文只含他该看到的；同时决策的人共享同一份快照。"""
from __future__ import annotations

from werewolf.engine.config import PRESETS
from werewolf.engine.decisions import WolfProposal
from werewolf.engine.engine import Engine
from werewolf.engine.events import GM, K_SPEECH, K_WOLF_VOTE, PUBLIC
from werewolf.engine.presenter import NullPresenter
from .harness import RecordingActor, play

SIMULTANEOUS = ("candidacy", "vote", "sheriff_vote", "withdraw")

_cache: dict = {}


def _game(seed: int = 3):
    key = ("records", seed)
    if key not in _cache:
        eng = play(2, seed=seed, recording=True)
        _cache[key] = (eng, list(RecordingActor.registry))
    return _cache[key]


def test_simultaneous_decisions_share_one_snapshot():
    """同时决策：同一轮的人看到同一份公开历史，已收的票在整轮收齐前不得公布。"""
    multi_round = 0
    # 5/11 会走到平票重投，覆盖两轮的路径。哪几个种子走到平票会随人格抽样的随机数消耗而整体
    # 平移；空转时这条测试自己会红，按提示换种子，别把断言放松。
    for seed in (1, 2, 3, 4, 5, 11):
        _, records = _game(seed)
        groups: dict[tuple, list] = {}
        for rec in records:
            if rec["kind"] in SIMULTANEOUS:
                # 轮次取自引擎的 freeze_seq：平票重投两轮共用同一个 phase，
                # 绝不能用日志长度反推轮次——真泄漏时每人会被当成独立一轮，断言全部空转。
                key = (rec["day"], rec["phase"], rec["kind"], rec["freeze_id"])
                groups.setdefault(key, []).append(rec)
        assert groups, (seed, "没采集到任何同时决策阶段")
        rounds: dict[tuple, dict[int, list]] = {}
        for (day, phase, kind, fid), recs in groups.items():
            rounds.setdefault((day, phase, kind), {})[fid] = recs
        for key, by_freeze in rounds.items():
            multi_round += len(by_freeze) - 1
            for fid, round_ in sorted(by_freeze.items()):
                seats = [rec["seat"] for rec in round_]
                assert len(set(seats)) == len(seats), (seed, key, fid, "同一轮里有人被问了两次")
                assert len({rec["log_len"] for rec in round_}) == 1, (seed, key, fid, seats)
                assert round_[0]["buffer_len"] == 0, (seed, key, fid, "上一轮未公布的选票漏进了这一轮")
    assert multi_round, "这批种子没采到平票重投，两轮路径失去覆盖"


def test_delta_never_exposes_others_private_events():
    for seed in (1, 2, 3):
        _, records = _game(seed)
        for rec in records:
            for event in rec["ask"].delta:
                if event.audience == GM:
                    raise AssertionError(f"上帝视角事件进入了玩家上下文：{event.text}")
                if event.audience == PUBLIC:
                    continue
                assert rec["seat"] in event.audience, (
                    seed, rec["seat"], rec["kind"], event.text, sorted(event.audience)
                )


def test_delta_never_repeats_an_event():
    """游标必须单调推进：同一事件不能重复投喂同一个玩家。"""
    for seed in (1, 2):
        _, records = _game(seed)
        seen: dict[int, set] = {}
        for rec in records:
            bucket = seen.setdefault(rec["seat"], set())
            for event in rec["ask"].delta:
                marker = (event.day, event.kind, event.text, id(event))
                assert marker not in bucket, (seed, rec["seat"], event.text)
                bucket.add(marker)


def test_the_wolf_vote_is_only_visible_to_living_wolves():
    """狼队表决的收件人 = **那一夜还活着的狼**：好人永远收不到，死掉的狼从下一夜起也收不到。

    以前写成「每个狼（含死人）都该看到」，只是恰好那一局狼全活到了最后才没红。
    `died_on_day >= event.day` 就是“那一夜他还在桌上”：当夜倒牌的狼当晚还投过票，
    从下一夜起就不该再收到队友的票型和理由。
    """
    eng, _ = _game(1)
    chats = [e for e in eng.state.log if e.kind == K_WOLF_VOTE]
    assert chats, "这一局没有狼队表决，测试会空转"
    for event in chats:
        expected = {s for s, p in eng.state.players.items()
                    if p.is_wolf and (p.died_on_day is None or p.died_on_day >= event.day)}
        assert event.audience != PUBLIC, event.text
        assert set(event.audience) == expected, (event.day, sorted(event.audience), sorted(expected))


def test_later_speakers_see_earlier_speeches():
    for seed in (1, 2):
        eng, records = _game(seed)
        speeches = [e for e in eng.state.log if e.kind == K_SPEECH]
        by_phase: dict[str, list] = {}
        for event in speeches:
            by_phase.setdefault(event.phase, []).append(event)
        for phase, events in by_phase.items():
            for index, earlier in enumerate(events):
                for later in events[index + 1:]:
                    later_rec = next(
                        (r for r in records if r["kind"] == "speech" and r["phase"] == phase
                         and r["seat"] == later.payload["seat"]), None
                    )
                    assert later_rec is not None, (seed, phase, later.text)
                    texts = [e.text for e in later_rec["ask"].delta]
                    assert earlier.text in texts, (seed, phase, earlier.text, later.text)


def test_recap_window_is_the_seats_own_cursor():
    """同一次上下文里，逐字给他的那句话不得再被摘要一遍。

    窗口边界必须是"他上次读到哪"（游标）。用 `len(log) - len(delta)` 反推会被私密事件带偏：
    私密事件不占 delta 长度，于是起点偏后，把 delta 里已经逐字给过的发言也划进摘要区间。
    这里刻意排成「公开 → 只有上帝看得见 → 公开」，反推法必然踩雷（实测 8 局重复 941 次）。
    """
    from werewolf.engine.build import AskBuilder
    from werewolf.engine.events import K_DEBUG, K_SPEECH, PUBLIC, Event

    cfg = PRESETS[1].config.copy()
    cfg.seed = 5
    eng = Engine(cfg, {}, presenter=NullPresenter(), seed=5)
    eng.setup()
    state = eng.state
    builder = AskBuilder(state)

    def speech(seat: int, words: str) -> None:
        state.emit(Event(kind=K_SPEECH, audience=PUBLIC, text=f"{seat}号（发言）：{words}",
                         payload={"seat": seat, "words": words}, day=state.day))

    speech(2, "第一条：这句只该出现一次")
    builder.build(1, "vote", pool=(2,))            # 把 1 号的游标推到这里
    speech(3, "第二条：和第三条同一次给")
    state.emit_god(K_DEBUG, "只有上帝看得见的一条")   # 不占 delta 长度 —— 就是它带偏反推
    speech(4, "第三条：本次逐字给他")

    ask = builder.build(1, "vote", pool=(2,))
    assert len(ask.delta) == 2, "构造没排对，探针白设"
    joined = " ".join(ask.recap)
    assert "第二条：和第三条同一次给" not in joined, (
        "本次逐字给过的发言又被摘要了一遍（提要窗口用了 len(log)-len(delta) 反推）")


def test_recap_only_uses_events_older_than_delta():
    for seed in (1, 3):
        _, records = _game(seed)
        for rec in records:
            if not rec["ask"].delta:
                continue
            first_delta_text = rec["ask"].delta[0].text
            assert first_delta_text not in rec["ask"].recap, (
                seed, rec["kind"], first_delta_text, rec["ask"].recap[:3]
            )


def test_private_dossier_matches_only_own_role():
    """私密档案只包含自己的信息：预言家的查验记录不会出现在别人档案里。"""
    for seed in (1, 2, 3):
        eng, records = _game(seed)
        seer = next((s for s, p in eng.state.players.items() if p.role_id == "seer"), None)
        if seer is None:
            continue
        for rec in records:
            dossier = rec["ask"].dossier
            if rec["seat"] != seer:
                # 「查验了」那句是死的（全仓没有这个措辞）；真挡箭牌是档案里的【查验记录】标题
                assert not any("查验" in line for line in dossier), (seed, rec["seat"], dossier)
            else:
                assert all("【查验记录】" in line for line in dossier if "查验" in line), (seed, dossier)


def test_the_wolf_team_sees_each_other_s_votes_and_a_short_line():
    """票型和那句短理由只进狼队频道；理由被压到 14 字，别让它长成小作文。"""
    import random

    from werewolf.actors.fake import FakeActor

    cfg = PRESETS[2].config.copy()
    cfg.human_seat = None
    eng = Engine(cfg, {}, presenter=NullPresenter(), seed=13)
    eng.setup()
    wolves = [s for s in cfg.seats if eng.state.players[s].is_wolf]

    class _Chatty(FakeActor):
        def wolf_target(self, ask):
            return WolfProposal(target=sorted(ask.pool)[0], reason="一" * 40 + "，别刀我")

    eng.actors = {s: _Chatty(s, random.Random(s * 3 + 1)) for s in cfg.seats}
    eng._night()

    votes = [e for e in eng.state.log if e.kind == K_WOLF_VOTE]
    assert votes, "夜里没播狼队表决"
    seen = {s: "".join(e.text for e in eng.state.read_new(s)) for s in cfg.seats}
    assert all("狼队表决" in seen[s] for s in wolves), "狼队友看不到彼此的表决"
    assert any("→" in e.text for e in votes), votes[0].text
    assert any("一" * 14 in e.text for e in votes), ("理由没截到 14 字", votes[0].text)
    assert not any("一" * 15 in e.text for e in votes), ("理由没截干净", votes[0].text)
    for seat in cfg.seats:
        if seat not in wolves:
            assert "狼队表决" not in seen[seat], f"{seat}号 看到了狼队的表决"


def test_the_wolf_tally_reaches_wolves_and_the_reviewer_only():
    """表决记录的出口只有三处：狼的档案、狼的频道、复盘。好人侧任何入口都不许出现。

    【狼队表决】那行档案跟着 `is_wolf` 走，看着像不可能漏；漏的地方在**摘要**：
    前情提要是逐座拼的，一旦把夜里的票型顺手塞进 recap，等于全场每晚免费看狼队内部表决。
    """
    from werewolf.actors.reviewer import Reviewer

    for seed in (1, 2):
        eng, records = _game(seed)
        wolves = {s for s, p in eng.state.players.items() if p.is_wolf}
        seen_wolf_line = False
        for rec in records:
            ask = rec["ask"]
            joined = " ".join(ask.dossier) + " " + " ".join(ask.recap)
            if rec["seat"] in wolves:
                seen_wolf_line = seen_wolf_line or any("【狼队表决】" in line for line in ask.dossier)
            else:
                assert "狼队表决" not in joined, (seed, rec["seat"], rec["kind"], joined[:200])
        assert seen_wolf_line, f"{seed} 号局的狼档案里没有表决记录，测试空转"

    text = Reviewer.transcript(eng.state)
    assert "狼队表决" in text, text[:400]
    assert any("轮 →" in line for line in text.splitlines()), "复盘里要能看到那一夜投了几轮"
