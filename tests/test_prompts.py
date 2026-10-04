"""提示词层：每一种询问都必须能拼出 system 与本轮问题。

这层没有真实 LLM 调用，最容易「改了 Ask 字段 / 加了新环节，提示词里悄悄崩掉」——
所以把每个 kind 都渲染一遍，并检查该说的事都说了（可选座位、字数上限、发言方向）。
"""
from __future__ import annotations

from werewolf.actors.prompts import ASK_TAIL, static_system, turn_prompt
from werewolf.actors.style import BEHAVIOR_RULES, KIND_HINT, style_text
from werewolf.engine.ask import (
    KINDS, KIND_CANDIDACY, KIND_LAST_WORDS, KIND_SEER, KIND_SHERIFF_SPEECH, KIND_SPEAK_ORDER,
    KIND_SPEECH, KIND_VOTE, KIND_WITHDRAW, KIND_WITCH, KIND_WOLF_TARGET,
)
from werewolf.engine.build import AskBuilder
from werewolf.engine.config import PRESETS
from werewolf.engine.engine import Engine
from werewolf.engine.presenter import NullPresenter

from xun.conversation import Conversation

#: 这些环节只会问对应身份，测试时也发给对的人
KIND_SEAT_ROLE = {KIND_WOLF_TARGET: "wolf", KIND_WITCH: "witch", KIND_SEER: "seer"}


def _engine():
    cfg = PRESETS[2].config.copy()
    cfg.human_seat = None
    eng = Engine(cfg, {}, presenter=NullPresenter(), seed=11)
    eng.setup()
    return eng


def _seat_for(eng, kind: str) -> int:
    alive = eng.state.alive_seats()
    role = KIND_SEAT_ROLE.get(kind)
    if role:
        for seat in alive:
            if eng.state.players[seat].role_id == role:
                return seat
    return alive[0]


def test_every_question_kind_renders_both_prompts():
    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    cfg = eng.state.config
    for kind, spec in KINDS.items():
        seat = _seat_for(eng, kind)
        player = eng.state.players[seat]
        ask = builder.build(seat, kind, pool=[s for s in eng.state.alive_seats() if s != seat],
                            note="法官的说明", speeches=[(2, "我先听一圈")])
        turn = turn_prompt(ask)
        system = static_system(ask, cfg, player.role_id, "稳健、按局面行事。", cfg.flags.level)
        assert spec.label in turn, (kind, "法官提问的标题没进本轮提示")
        assert ASK_TAIL[kind].strip(), (kind, "这个环节的问法说明是空的")
        assert ASK_TAIL[kind] in turn, (kind, "没写这个环节要模型输出什么")
        assert kind in KIND_HINT, (kind, "忘了给这个环节写打法提示")
        assert player.role_name in system, (kind, "身份没进 system")
        assert BEHAVIOR_RULES in system, (kind, "行为纪律没进 system")
        # 变动内容一律在本轮那条消息里：system 里只准有待着不动的角色设定
        assert "### 局面提要" in turn and "### 你的私密档案" in turn, kind
        assert "### 局面提要" not in system and "### 你的私密档案" not in system, \
            (kind, "会变的东西混进了 system")
        # 只有末尾那句纪律会提「提示词」，正文里不许出现任何内部词
        body = "\n".join(l for l in turn.splitlines() if not l.startswith("记住：你现在就是"))
        assert "提示词" not in body and "角色卡" not in body, kind


def test_the_prompt_only_offers_seats_and_limits_where_relevant():
    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    alive = eng.state.alive_seats()
    speech = turn_prompt(builder.build(alive[0], KIND_SPEECH, pool=alive, note=""))
    vote = turn_prompt(builder.build(alive[1], KIND_VOTE, pool=[s for s in alive if s != alive[1]]))
    order = turn_prompt(builder.build(alive[2], KIND_SPEAK_ORDER,
                                      note="顺时针从 5号 开始，逆时针从 3号 开始。你压轴归票。"))
    last_words = turn_prompt(builder.build(alive[3], KIND_LAST_WORDS))

    assert f"发言长度上限 {eng.state.config.flags.speech_word_limit} 字" in speech
    # 发言题的座位池是「你能提到谁」，不是选项 —— 写成选项会诱导模型去挑一个 target
    assert "场上还活着的玩家" in speech and "这不是选择题" in speech, speech
    assert "不在其中的选择无效" not in speech, speech
    assert "stance" in speech, "发言题要说明 stance 是给自己的、不公开"
    assert "可选座位" in vote, "投票要把可选座位写清楚"
    assert "本题无需选择座位" in order, order          # 方向题不给座位池
    assert "顺时针" in order and "逆时针" in order
    assert "可选座位" not in last_words


def test_seer_and_witch_get_their_private_dossier_back():
    """私密档案每轮重述：模型最容易忘记的就是自己夜里干了什么。"""
    from .harness import play
    eng = play(2, seed=4)
    seer = next(s for s, p in eng.state.players.items() if p.role_id == "seer")
    witch = next(s for s, p in eng.state.players.items() if p.role_id == "witch")
    witch_ask = eng.builder.build(witch, KIND_WITCH, pool=eng.state.alive_seats())
    assert "【用药】" in "\n".join(witch_ask.dossier)
    if eng.state.players[seer].seer_checks:
        ask = eng.builder.build(seer, KIND_SEER, pool=[s for s in eng.state.alive_seats() if s != seer])
        assert "【查验记录】" in "\n".join(ask.dossier)
        assert "【查验记录】" in turn_prompt(ask), "档案要进本轮那条消息，别只留在 Ask 字段里"


def test_every_kind_is_registered_in_one_place():
    """加新环节只许在 `ask.KINDS` 一处登记：schema、形状、卡片问话缺一项，
    人或模型就答不了 —— 以前这三件事抄在三个文件里，测试只能事后对账。"""
    from pydantic import BaseModel

    for kind, spec in KINDS.items():
        assert spec.id == kind, kind
        assert isinstance(spec.schema, type) and issubclass(spec.schema, BaseModel), kind
        assert spec.shape in ("seat", "bool", "direction", "text", "witch"), kind
        assert spec.label and spec.method, kind
        if spec.shape == "seat":
            assert spec.question and "target" in spec.schema.model_fields, kind
        if spec.shape == "bool":
            assert spec.yes and spec.no and spec.field in spec.schema.model_fields, kind
        if spec.shape == "text":
            assert spec.limit, f"{kind} 要说话，就得从 RuleFlags 取一个字数上限字段"
        if spec.shape in ("bool", "direction"):
            assert spec.question, kind


def test_the_speaker_is_told_where_he_stands_in_the_running_order():
    """发言的人得知道自己排在哪儿：第几个、前面谁说过、后面还剩谁（都是公开信息，别让人自己数）。"""
    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    alive = eng.state.alive_seats()
    order = tuple(alive[3:] + alive[:3])                 # 从中间某位开始绕一圈
    shared = dict(pool=alive, order=order)
    first = turn_prompt(builder.build(order[0], KIND_SPEECH, note="", **shared))
    middle = turn_prompt(builder.build(order[2], KIND_SPEECH, note="",
                                       speeches=[(order[0], "我先说"), (order[1], "我也听听")], **shared))
    last = turn_prompt(builder.build(order[-1], KIND_SPEECH, note="", direction=1,
                                     speeches=[(s, "说过了") for s in order[:-1]], **shared))
    vote = turn_prompt(builder.build(order[1], KIND_VOTE, pool=alive))

    assert "第一个开口" in first, first
    assert f"你排第 3/{len(order)}" in middle, middle
    assert f"前面已发言：{order[0]}号、{order[1]}号" in middle, middle
    assert f"你之后还有：{order[3]}号" in middle, middle
    assert f"你排第 {len(order)}/{len(order)}" in last and "归票位" in last, last
    assert "发言位置" not in vote, "非发言环节别塞这一行"


def test_the_election_speech_position_says_nothing_about_direction():
    """警上竞选那一圈没有方向可言（警长还没选出来），别写成「顺时针发言」。"""
    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    alive = eng.state.alive_seats()
    order = tuple(alive[:4])
    ask = builder.build(order[1], KIND_SHERIFF_SPEECH, pool=alive, order=order, direction=0,
                        speeches=[(order[0], "我先跳")])
    line = ask.position_text()
    assert "本轮发言" in line and f"你排第 2/{len(order)}" in line, line
    assert "顺时针" not in line and "逆时针" not in line, line
def test_yes_no_questions_carry_no_speech_limit():
    """举手 / 退水这类是非题没有话要说：字数上限行不该出现在题面里（以前会，500 字）。"""
    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    seat = eng.state.alive_seats()[0]
    for kind in (KIND_CANDIDACY, KIND_WITHDRAW):
        ask = builder.build(seat, kind)
        assert ask.word_limit == 0, (kind, ask.word_limit)
        assert "发言长度上限" not in turn_prompt(ask), kind      # 全局规则里的总上限是另一回事
    speech = builder.build(seat, KIND_SPEECH)
    assert speech.word_limit == eng.state.config.flags.speech_word_limit   # 该有还是要有的那些


def test_the_static_system_ignores_everything_that_moves():
    """system 整局一个字节都不许变 —— 它是会话最前面最长的一段。

    每轮重写 system 会把 provider 的 KV 前缀缓存整段打掉（那些字符根本没变），而座位 agent
    一局要被问几十次。所以随天数变的东西（局面、档案、警徽、天数、上限）只准进本轮那条
    新消息；这里把同一个座位在「开局」和「第 7 天」两份 Ask 渲染成同一句话来钉住它。
    """
    from dataclasses import replace

    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    seer = _seat_for(eng, KIND_SEER)
    opening = builder.build(seer, KIND_SEER, pool=[s for s in eng.state.alive_seats() if s != seer])
    later = replace(opening, day=7, phase="vote_7", note="法官的新说明", word_limit=200,
                    recap=("存活：1号、2号；已出局：无", "警徽：3号持有"),
                    dossier=("【查验记录】3号=狼人", "【用药】解药已用"),
                    order=(1, 2, 3), direction=-1)
    rest = (eng.state.config, "seer", "稳健、按局面行事。", eng.state.config.flags.level)

    assert static_system(later, *rest) == static_system(opening, *rest), \
        "会随局面变的内容混进了 system：每轮重写它等于每轮打掉 KV 前缀缓存"
    turn = turn_prompt(later)
    assert "存活：1号、2号" in turn and "【查验记录】3号=狼人" in turn, "提要/档案得进本轮消息"
    assert "法官的新说明" in turn and "发言长度上限 200 字" in turn, turn


def test_a_compacted_history_gets_the_role_back_without_stacking():
    """xun 的 auto_compact 把摘要写进 `messages[0]`，连角色设定一起盖掉。

    接回来的规则要同时躲开两种错：摘要把身份吃掉（玩家忘了自己是谁），以及每轮又接一遍
    （摘要叠摘要，system 越滚越长，缓存也照样没了）。
    """
    from werewolf.actors.llm_player import rebase_system

    role = "你是狼人杀里坐在 5 号位的玩家，你的身份是 预言家。"
    assert rebase_system(role, role) is None, "没人动过 system：一个字都不该重写"
    assert rebase_system(None, role) == role

    rebased = rebase_system("Earlier conversation history...\n第 3 天死了 2 号", role)
    assert rebased.startswith(role) and "第 3 天死了 2 号" in rebased, "设定要在前、摘要要在后"
    assert rebase_system(rebased, role) is None, "已经接回来的不许再叠一层"

    second = rebase_system("新的摘要：第 6 天票了 4 号", role)        # 第二次压缩：整条又被换掉
    assert second.startswith(role) and "第 6 天票了 4 号" in second
    assert "第 3 天" not in second, "旧摘要该跟着旧历史一起走掉"


def test_the_seat_agent_writes_its_system_once_per_game():
    """接线也得钉住：`_decide` 每轮重写 system 是这场改造要拔掉的那颗钉子。

    这里不用真 agent（那要模型 key），但**会话是 xun 真的那个**：xun 1.3 起会话消息是类型化的，
    桩要是自己编一张 dict 消息表，测出来的就只是桩自己的故事（那次升级当场就该红）。
    """
    from dataclasses import replace

    from werewolf.actors.llm_player import LLMActor, current_system

    class _FakeAgent:
        def __init__(self) -> None:
            self.conversation = Conversation()
            self.writes = 0

        def system(self, content: str) -> None:
            self.writes += 1
            self.conversation.set_system_message_content(content)

    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    seer = _seat_for(eng, KIND_SEER)
    ask = builder.build(seer, KIND_SEER, pool=[s for s in eng.state.alive_seats() if s != seer])

    actor = LLMActor.__new__(LLMActor)                     # 绕开 __init__：不建真 Agent、不发请求
    actor.static_system = None
    actor.game_config = eng.state.config
    actor.role_id, actor.persona_style, actor.level = "seer", "稳健、按局面行事。", "balanced"
    actor.agent = _FakeAgent()

    for _round in range(3):
        actor._ensure_system(ask)
    actor._ensure_system(replace(ask, day=6))
    assert actor.agent.writes == 1, f"一局里 system 被写了 {actor.agent.writes} 次"

    # auto_compact 压缩时就是这样把摘要盖在 messages[0] 上的（xun 的 conversation.compact）
    actor.agent.conversation.set_system_message_content("压缩摘要：第 3 天死了 2 号",
                                                        is_compressed=True)
    actor._ensure_system(ask)
    assert actor.agent.writes == 2, "auto_compact 顶掉了设定，要接回来"
    head = current_system(actor.agent.conversation)
    assert head and head.startswith(actor.static_system), "设定要接回开头，摘要留在后面"
    actor._ensure_system(ask)
    assert actor.agent.writes == 2, "接回来一次就够了，别又变成每轮重写"


def test_the_hard_trim_pins_the_head_and_starts_the_tail_on_a_user_message():
    """兜底硬顶剪的是尾巴：开头钉住的几条不许动，剪完要从一条 user 开始。

    会话同样是 xun 真的那个（消息是类型化的）：`head=1` 是座位（只钉 system），
    `head=2` 是复盘教练（system 与那份对局记录都不许剪）。
    """
    from werewolf.actors.llm_player import trim_conversation
    from xun.conversation_message import RawOpenAIMessage, SystemPrompt, UserMessage

    def convo(extra_head: int = 1, trailing_assistant: bool = False) -> Conversation:
        conv = Conversation()
        conv.set_system_message_content("角色设定")
        if extra_head == 2:
            conv.add_user_message("=== 对局记录 ===\n第 1 天……")
        for round_no in range(6):
            conv.add_user_message(f"法官提问 {round_no}")
            conv.messages.append(RawOpenAIMessage(
                raw={"role": "assistant", "content": f"本轮决定 {round_no}"}))
        if trailing_assistant:
            conv.messages.append(RawOpenAIMessage(raw={"role": "assistant", "content": "自言自语"}))
        return conv

    seat = convo()
    trim_conversation(seat, char_budget=0, keep_pairs=2, head=1)
    assert isinstance(seat.messages[0], SystemPrompt), "system 被剪掉了：玩家会忘了自己是谁"
    assert [m.role for m in seat.messages[1:]] == ["user", "assistant", "user", "assistant"]

    coach = convo(extra_head=2)
    trim_conversation(coach, char_budget=0, keep_pairs=2, head=2)
    assert isinstance(coach.messages[1], UserMessage)
    assert coach.messages[1].text.startswith("=== 对局记录 ==="), "剪掉记录等于让教练凭印象下棋"
    assert len(coach.messages) == 6 and coach.messages[-1].role == "assistant"

    loose = convo(trailing_assistant=True)
    trim_conversation(loose, char_budget=0, keep_pairs=2, head=1)
    assert loose.messages[1].role == "user", "剪完以 assistant 开头，有的 provider 直接 400"


def test_the_witch_prompt_offers_only_the_antidote_the_engine_says_she_has():
    """真人卡片与脚本玩家都改读 `Ask.save_pool` 了，模型那一侧也得读到同一份数据。

    否则提示词永远写着「save=True 表示用解药救今夜被刀的人」，药打完的那一夜它照样输出
    save=true —— 引擎吞掉是对的，可它会带着一套编出来的理由去发言和留遗言。
    """
    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    witch = _seat_for(eng, KIND_WITCH)
    pool = [s for s in eng.state.alive_seats() if s != witch]

    can_save = turn_prompt(builder.build(witch, KIND_WITCH, pool=pool, save_pool=(pool[0],)))
    assert "解药还能用" in can_save and f"{pool[0]}号" in can_save, can_save

    spent = turn_prompt(builder.build(witch, KIND_WITCH, pool=pool, save_pool=()))
    assert "用不上" in spent and "save 只能填 false" in spent, spent


def test_the_witch_prompt_says_what_the_poison_can_still_do():
    """毒药那一问也一样：能用就说能从谁里面选，用完了就明说只能填 null。

    卡片靠候选池摆按钮，模型靠这句话；两边都从引擎给的那份数据取，才不会一个还在问、
    另一个知道没药可下。
    """
    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    witch = _seat_for(eng, KIND_WITCH)

    usable = turn_prompt(builder.build(witch, KIND_WITCH,
                                       pool=[s for s in eng.state.alive_seats() if s != witch]))
    assert "毒药还能用" in usable, usable
    assert "毒不了你自己" in usable, usable

    spent = turn_prompt(builder.build(witch, KIND_WITCH, pool=(), save_pool=()))
    assert "毒药已经用完" in spent and "poison_target 只能填 null" in spent, spent


def test_the_table_speaking_discipline_reaches_the_model():
    """「桌上怎么说话」这一段必须真的进 system，而且和发言环节的提示对得上。

    玩家读不下去的发言基本都是同几种病：长句、先铺垫后结论、书面语、正确的废话。
    这些是静态纪律（整局一份，进 system 最前面），不是每轮重复的唠叨。
    """
    text = style_text()
    assert "一句话只放一个信息点" in text, text
    for banned in ("首先", "综合来看", "值得注意的是", "让我们"):
        assert banned in text, (banned, "AI 味黑名单里少了这条")
    assert "3号有毛病" in text, "没有『坏写法 → 好写法』的对照例，模型只知道规矩不知道样子"

    # 关键一条：这段必须真的被 static_system 拼进去。写过一次“新纪律加了常量却没人调用”，
    # 全套测试照样绿，模型却一个字都没看到 —— 断言写在 style.py 里测不到这件事。
    eng = _engine()
    builder = AskBuilder(eng.state, eng.stances)
    seat = _seat_for(eng, KIND_SPEECH)
    system = static_system(builder.build(seat, KIND_SPEECH), eng.state.config,
                           "villager", "稳健。", eng.state.config.flags.level)
    assert "一句话只放一个信息点" in system and "金水" in system, "说人话纪律没进 system"
    assert "## 行为纪律" in system, system[:400]
    # 发言/竞选/遗言三处提示都要「先结论、说完就停」，别让人等三段才听到重点
    assert "开口第一句就是结论" in KIND_HINT[KIND_SPEECH], KIND_HINT[KIND_SPEECH]
    assert "说完就停" in KIND_HINT[KIND_SPEECH] or "说完就停" in text
    assert "别夸自己" in KIND_HINT[KIND_SHERIFF_SPEECH], KIND_HINT[KIND_SHERIFF_SPEECH]
    assert "别把前面的发言重讲一遍" in KIND_HINT[KIND_LAST_WORDS], KIND_HINT[KIND_LAST_WORDS]


def test_the_table_slang_is_offered_but_not_forced():
    """黑话表进 system（用了话就短），但人格里同时有「满口术语」和「说大白话」两档。

    全员满口金水查杀，真人玩家反而读不动；全员不许术语，发言又会退化成绕圈子。
    """
    text = style_text()
    for term in ("金水", "查杀", "警徽流", "归票", "票型", "退水"):
        assert term in text, (term, "黑话表少了这个")
    import random

    from werewolf.engine.persona import QUIRKS, sample_personas
    from werewolf.engine.roles import SEER, VILLAGER, WITCH, WOLF

    assignment = {1: WOLF, 2: WOLF, 3: SEER, 4: WITCH, 5: VILLAGER, 6: VILLAGER}
    jargon_users: set = set()
    plain_speakers: set = set()
    quirks: set = set()
    for seed in range(1, 25):
        for style in (p.style for p in sample_personas(assignment, random.Random(seed)).values()):
            jargon_users.add("满口金水查杀" in style)
            plain_speakers.add("不堆术语" in style)
            quirks.add(style.split("口头习惯：")[1].split("；")[0])
    assert jargon_users == {True, False}, "黑话密度这一档没在抽样，说明它被写死了"
    assert plain_speakers == {True, False}, "反过来也一样：不能所有人都被要求说大白话"
    assert len(quirks) >= 3, f"24 局抽不出三种口头习惯，等于没有口头习惯：{quirks}"
    assert quirks <= set(QUIRKS), quirks - set(QUIRKS)
