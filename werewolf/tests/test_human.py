"""人类玩家交互路径测试：用桩 host 走完 HumanActor 的每一种决策。"""
from __future__ import annotations

import random
from typing import Sequence

from ..actors.fake import FakeActor
from ..actors.human import HumanActor
from ..engine.config import PRESETS
from ..engine.engine import Engine
from ..engine.presenter import NullPresenter


class StubHost:
    """按 prompt 关键字挑选项，模拟真人点按钮。"""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, list[str], str]] = []
        self.freewrite: list[tuple[str, bool]] = []
        self.reason_reply = "不说"
        self.titles: list[str] = []
        self.messages: list[str] = []
        self.subtitles: list[str] = []
        self.say_for_me = False              # 模拟 `/auto-say`
        self.authored: list[tuple[int, str]] = []

    def say(self, markdown: str) -> None:
        pass

    def info(self, text: str) -> None:
        pass

    def notice(self, state, text: str) -> None:
        pass

    def spoke_for_player(self, seat: int, kind: str) -> None:
        self.authored.append((seat, kind))

    def stop_requested(self) -> bool:
        return False

    def confirm(self, prompt: str, message: str = "", default: bool = True, **kw) -> bool:
        return True

    def wait_text(self, title: str, subtitle: str = "", note: str = "", auto=None) -> str:
        """真人在**会话输入框**里发的那句话（发言、遗言走这里，不走卡片）。

        `say_for_me=True` 模拟 `/auto-say`：这一句由法官（替身演员）产。
        """
        text = (auto() if self.say_for_me and auto is not None
                else "我判断 2 号最像狼，今天先出他，别被话术带走。")
        self.prompts.append((title, [], text))
        self.titles.append(title)
        self.messages.append(note)
        self.subtitles.append(subtitle)
        return text

    def ask(self, prompt: str, choices: Sequence[str], *, message: str = "", title: str = "",
            subtitle: str = "", default: str | None = None, allow_extra: bool = False) -> str:
        """卡片问答。`allow_extra` 记下这张卡允不允许自己填 —— 狼人那句“带话”必须为真。"""
        choices = list(choices)
        self.freewrite.append((prompt, allow_extra))
        if "带句话" in prompt:
            answer = self.reason_reply
        elif any(k in prompt for k in ("开枪",)):
            answer = next((c for c in choices if "放弃" not in c), choices[0])
        elif "警徽移交" in prompt or "警徽交给谁" in prompt:
            answer = next((c for c in choices if "撕" not in c), choices[0])
        elif "毒药" in prompt and "不用毒" in choices:
            answer = "不用毒"
        elif "解药" in prompt:
            answer = "不用解药"
        elif "投票" in prompt or "警徽投给" in prompt:
            answer = next((c for c in choices if c != "弃票"), choices[0])
        elif "空刀" in choices:
            answer = next((c for c in choices if c != "空刀（今晚不杀人）"), choices[0])
        else:
            answer = choices[0]
        self.prompts.append((prompt, choices, answer))
        self.titles.append(title)
        self.messages.append(message)
        self.subtitles.append(subtitle)
        return answer


def _play_with_human(preset_index: int, seed: int):
    preset = PRESETS[preset_index]
    config = preset.config.copy()
    config.seed = seed
    config.human_seat = 1
    engine = Engine(config, {}, presenter=NullPresenter(), seed=seed)
    engine.setup()
    state = engine.state
    assert state is not None
    rng = random.Random(seed * 17 + 3)
    host = StubHost()
    actors: dict[int, object] = {}
    for seat, player in state.players.items():
        actors[seat] = HumanActor(seat, host) if seat == 1 else FakeActor(seat, rng)
    engine.actors = actors
    engine.run()
    kinds = {title for title, _c, _a in host.prompts}
    return engine, host, kinds, state


def test_human_can_complete_a_whole_game():
    for seed in (1, 2, 3, 4):
        engine, host, _kinds, state = _play_with_human(2, seed)
        assert state.finished, (seed, "有真人参与时未走完")
        assert state.winner in ("wolf", "good"), (seed, state.winner)
        assert host.prompts, seed


def test_human_gets_role_specific_night_prompt():
    seen: set[str] = set()
    for seed in range(1, 20):
        engine, _host, kinds, state = _play_with_human(2, seed)
        role = state.players[1].role_id
        if role == "wolf" or role == "wolf_king":
            seen.add("wolf")
        if role == "seer":
            seen.add("seer")
        if role in ("villager", "idiot"):
            seen.add("plain")
        if len(seen) == 3:
            break
    assert seen == {"wolf", "seer", "plain"}, seen


def test_human_asked_for_speech_and_vote_when_alive():
    for seed in (1, 2, 3, 4, 5):
        engine, host, kinds, state = _play_with_human(1, seed)
        if state.players[1].died_on_day in (None, 0) or (state.players[1].died_on_day or 0) > 1:
            titles = " ".join(title for title, _c, _a in host.prompts)
            assert "发言" in titles or "投票" in titles, (seed, titles)
            return
    raise AssertionError("没找到人类玩家活过第一天的样本")


def test_human_prompts_always_offer_legal_seats_only():
    for seed in (1, 2, 3):
        engine, host, _kinds, state = _play_with_human(2, seed)
        for prompt, choices, answer in host.prompts:
            seats = [int(c.replace("号", "")) for c in choices if c.endswith("号")]
            for seat in seats:
                assert seat in state.players, (seed, seat, prompt)


def test_every_actor_implements_the_full_protocol():
    """人类 / AI 玩家都要能回答引擎可能问到的每种决策，否则阶段会卡住。"""
    from ..actors.fake import FakeActor
    from ..actors.llm_player import LLMActor

    from ..engine.ask import KINDS

    for cls in (HumanActor, FakeActor, LLMActor):
        assert hasattr(cls, "decide") and hasattr(cls, "finalize"), cls.__name__
    # 脚本玩家按环节写实现（测试要能单独 patch 某一环节），所以它的每个 method 都得在
    for kind, spec in KINDS.items():
        assert hasattr(FakeActor, spec.method), f"FakeActor 缺 {spec.method}（{kind}）"


def test_prompt_titles_say_night_or_day():
    """夜间阶段的卡片标题写“第 N 夜”，白天写“第 N 天”，别把玩家绕晕。"""
    from ..engine.ask import Ask, KIND_SEER, KIND_SPEECH

    host = StubHost()
    HumanActor(1, host).decide(Ask(kind=KIND_SEER, day=1, phase="night_1", seat=1, pool=(2, 3)))
    assert host.titles == ["第 1 夜 · 预言家行动：查验"], host.titles

    host2 = StubHost()
    HumanActor(1, host2).decide(Ask(kind=KIND_SPEECH, day=2, phase="speech_2", seat=1,
                                    pool=(2, 3), word_limit=100))
    assert host2.titles == ["第 2 天 · 白天发言"], host2.titles


def test_speech_is_collected_from_the_input_box():
    """发言不弹"填空卡片"：法官只播一张"轮到你"的提示，话从会话输入框里收。

    卡片那个小输入框写长文很难受（会被顶起、看不到前面的发言流），所以长文本一律走
    `wait_text`。这里盯两件事：真的走了 wait_text、提示里说清去哪儿说话和字数建议。
    """
    from ..engine.ask import Ask, KIND_SPEECH

    host = StubHost()
    speech = HumanActor(1, host).decide(Ask(kind=KIND_SPEECH, day=1, phase="speech_1",
                                            seat=1, pool=(2,), word_limit=100))
    assert speech.text.startswith("我判断 2 号"), "输入框里那句话要原样上桌"
    assert host.prompts and host.prompts[-1][1] == [], "发言不该再弹选项卡片"
    joined = " ".join(host.messages)
    assert "轮到你发言" in joined, joined
    assert "100 字" in joined, joined
    # "去哪儿说话"是 host 的卡片页脚管的，见 test_channels 的端到端那条


def test_last_words_also_use_the_input_box():
    from ..engine.ask import Ask, KIND_LAST_WORDS

    host = StubHost()
    words = HumanActor(4, host).decide(Ask(kind=KIND_LAST_WORDS, day=1,
                                                phase="last_words_1", seat=4, pool=()))
    assert "我判断" in words.text
    assert host.titles and "遗言" in host.titles[-1], host.titles


def test_speech_card_shows_the_speaking_order():
    """发言卡片要写清「你排第几」，而且这句话必须就是引擎给 AI 的那一句 —— 同一个出口。

    以前真人在这里看到的是 actor 自己拼的第二套说法（"已发言…，你是第 N 个"），和 AI
    拿到的 `Ask.position_text()` 迟早会说不一致的顺序。现在卡片副标题 == position_text。
    """
    from ..engine.ask import Ask, KIND_SPEECH

    opening = Ask(kind=KIND_SPEECH, day=1, phase="speech_1", seat=3, pool=(2, 4),
                  word_limit=100, order=(3, 2, 4), direction=1)
    host = StubHost()
    HumanActor(3, host).decide(opening)
    assert host.subtitles[-1] == opening.position_text(), host.subtitles
    assert "你排第 1/3" in host.subtitles[-1], host.subtitles
    assert "第一个开口" in host.subtitles[-1], host.subtitles

    later = Ask(kind=KIND_SPEECH, day=1, phase="speech_1", seat=3, pool=(2, 4),
                word_limit=100, order=(2, 3, 4), direction=1, speeches=((2, "a"),))
    host2 = StubHost()
    HumanActor(3, host2).decide(later)
    assert host2.subtitles[-1] == later.position_text(), host2.subtitles
    assert "你排第 2/3" in host2.subtitles[-1], host2.subtitles
    assert "前面已发言：2号" in host2.subtitles[-1], host2.subtitles
    assert "你之后还有：4号" in host2.subtitles[-1], host2.subtitles
    # 同一句位置提示只出现一次：卡片正文里不该再重复一遍
    assert "发言位置" not in " ".join(host2.messages), host2.messages


def test_non_speech_cards_say_which_seat_is_asked():
    """不涉及发言顺序的卡片（投票、夜里行动）副标题写「几号（你）的行动」。"""
    from ..engine.ask import Ask, KIND_VOTE

    host = StubHost()
    HumanActor(5, host).decide(Ask(kind=KIND_VOTE, day=1, phase="vote_1", seat=5, pool=(1, 2)))
    assert host.subtitles[-1] == "5号（你）的行动", host.subtitles


def test_auto_say_lets_the_proxy_speak_this_one_line():
    """`/auto-say`：这一句由替身演员产，并告诉法官「这句要让全场听见」。"""
    from types import SimpleNamespace

    from ..engine.ask import Ask, KIND_SPEECH

    class _Proxy:
        def __init__(self) -> None:
            self.asks: list = []

        def decide(self, ask):
            self.asks.append(ask)
            return SimpleNamespace(text="法官替他说的一句：先听后面的玩家怎么说。")

        def finalize(self) -> None:
            self.finalized = True

    def factory() -> _Proxy:
        proxy = _Proxy()
        made.append(proxy)
        return proxy

    made: list[_Proxy] = []
    host = StubHost()
    host.say_for_me = True
    actor = HumanActor(1, host, proxy_factory=factory)
    speech = actor.decide(Ask(kind=KIND_SPEECH, day=1, phase="speech_1", seat=1,
                              pool=(2,), word_limit=100))

    assert speech.text.startswith("法官替他说的一句"), speech.text
    assert len(made) == 1 and len(made[0].asks) == 1, "替身只该被叫一次，拿到的就是这一份 Ask"
    assert host.authored == [(1, KIND_SPEECH)], "得告诉法官这一句要播出去"
    actor.finalize()
    assert getattr(made[0], "finalized", False), "替身的私有会话要跟着收掉"


def test_witch_card_asks_about_antidote_only_when_saving_is_possible():
    """真人女巫这一步问什么，由引擎给的 `save_pool` 决定，不由法官那句文案决定。

    以前是拿 note 里有没有「不再知道刀口」判断的：改一句法官措辞就等于改规则，
    而狼空刀那一夜那句文案本来就是假的（她两瓶药都在，却被跳过了问解药）。
    """
    from ..engine.ask import Ask, KIND_WITCH

    stuck = Ask(kind=KIND_WITCH, day=1, phase="night_1", seat=4, pool=(1, 2, 3, 5),
                note="今夜被刀的是 3号。", save_pool=())    # 解药已用：救不了
    host = StubHost()
    action = HumanActor(4, host).decide(stuck)
    prompts = [prompt for prompt, _c, _a in host.prompts]
    assert not any("解药" in p for p in prompts), prompts   # 救不了就别问
    assert any("毒药" in p for p in prompts), prompts
    assert action.save is False, action

    saveable = Ask(kind=KIND_WITCH, day=1, phase="night_1", seat=4, pool=(1, 2, 3, 5),
                   note="今夜是空刀，没人被刀。", save_pool=(3,))   # 有解药：先问解药
    host2 = StubHost()
    HumanActor(4, host2).decide(saveable)
    assert any("解药" in p and "3" in p for p, _c, _a in host2.prompts), host2.prompts


def test_witch_card_never_offers_her_own_seat_and_skips_a_spent_poison():
    """真人女巫的毒药卡片：选项里不许出现她自己；毒药不在手里就整张卡都别弹。

    引擎以前把全体存活座位当作毒药候选：卡片上会出现「毒 6号」（6号就是她自己），
    第二瓶药用完之后每一夜还在问「毒谁」—— 点了任何一个都会被静默丢掉。
    现在候选池由引擎算好（不含她自己，药用完就是空池），卡片只照着池子摆按钮。
    """
    from ..engine.ask import Ask, KIND_WITCH

    host = StubHost()
    HumanActor(4, host).decide(Ask(kind=KIND_WITCH, day=1, phase="night_1", seat=4,
                                   pool=(1, 2, 3, 5), note="今夜被刀的是 2号。", save_pool=()))
    poison = [(prompt, choices) for prompt, choices, _a in host.prompts if "毒药" in prompt]
    assert poison, "解药用完了，毒药这一问还在"
    assert "4号" not in poison[0][1], f"卡片把「毒自己」做成了按钮：{poison[0][1]}"

    host2 = StubHost()
    spent = Ask(kind=KIND_WITCH, day=2, phase="night_2", seat=4, pool=(), save_pool=(),
                note="你已用完两瓶药，不再知道刀口。")
    action = HumanActor(4, host2).decide(spent)
    assert not any("毒药" in prompt for prompt, _c, _a in host2.prompts), \
        "没有毒药可下还问她毒谁：她点什么都算说了句假话"
    assert action.poison_target is None, action


def test_a_human_wolf_can_send_the_pack_a_short_line():
    """真人狼人也能给队友带一句话；填了就必须送达，点「不说」才是空。

    以前只有大模型狼有 `reason`，真人那张卡只有座位按钮 —— 真人狼等于天生不能沟通。
    """
    from ..engine.ask import Ask, KIND_WOLF_TARGET
    from ..actors.human import HumanActor

    def run(reply: str):
        host = StubHost()
        host.reason_reply = reply
        ask = Ask(kind=KIND_WOLF_TARGET, day=1, phase="night_1", seat=1, identity="狼人",
                  teammates=(4, 7), pool=(2, 3, 5, 8), must_choose=False,
                  note="今晚你想击杀谁？（你看不到队友投谁）")
        return HumanActor(1, host).decide(ask), host

    got, host = run("先刀跳预的那个")
    assert got.target == 2, got
    assert got.reason == "先刀跳预的那个", got
    assert any(flag for prompt, flag in host.freewrite if "带句话" in prompt), \
        "理由那张卡必须允许真人自己填，不然只有一个「不说」可点"

    got, _host = run("不说")
    assert got.reason == "", "点了「不说」就该留空，不该把选项文本当理由"

    got, _host = run("一" * 40)
    assert len(got.reason) == 14, f"理由要截到 14 字，实际 {len(got.reason)} 字"
