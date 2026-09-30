"""通道测试：法官只发 info，游戏者的话以座位为作者 emit，用户消息被接管且不进模型。

法官住在**会话自己**的 agent 里（走 xun 的 `takeover_result`），所以这里被测的是一个 agent 桩。
"""
from __future__ import annotations

import contextlib
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from werewolf.actors.fake import FakeActor
from werewolf.engine.config import PRESETS
from werewolf.engine.engine import Engine
from werewolf.engine.events import (
    K_DEATH_ANNOUNCE, K_EXILE, K_LAST_WORDS, K_SHERIFF_SPEECH, K_SPEECH, K_VOTE,
    K_VOTE_RESULT, K_WIN, Event,
)
from werewolf.engine.presenter import NullPresenter
from werewolf.engine.state import GameState
from werewolf.ui import render
from . import harness
from werewolf.ui.host import REVIEWER, HostPresenter
from xun.display_abstract import AgentInfo, DisplayAbstract, DisplayEvent


def _make_display():
    from xun.display_abstract import DisplayAbstract

    class _Rec(DisplayAbstract):
        def __init__(self) -> None:
            self.events: list[DisplayEvent] = []
            self.requests: list = []

        def on_event(self, event) -> None:
            self.events.append(event)

        def get_choice(self, request):
            self.requests.append(request)
            choices = list(request.choices or [])
            return (request.default or (choices[0] if choices else "")) or ""

    return _Rec()


class _StubHooks:
    """只实现法官用到的那半个 hook 注册表：add + invoke。"""

    def __init__(self) -> None:
        self.fns: list = []

    def add(self, fn) -> None:
        self.fns.append(fn)

    def invoke(self, args) -> None:
        for fn in self.fns:
            fn(args)


class _StubConversation:
    """会话消息表，只实现接管需要的 `pop_from_last_user_message`。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def pop_from_last_user_message(self, inclusive: bool = True) -> list[dict]:
        for index in range(len(self.messages) - 1, -1, -1):
            if self.messages[index].get("role") == "user":
                start = index if inclusive else index + 1
                popped = self.messages[start:]
                del self.messages[start:]
                return popped
        return []


class _StubAgent:
    """够用的 agent 桩：法官只碰 display / get_choice / hooks / conversation / cancel_event。

    `get_choice` 和 `execute` 都照 xun 的真实行为写：auto-confirm 的判断在显示层里、
    `before_execution` 早于显示层、`takeover_result` 非 None 就跳过整个 loop 不调模型。
    桩一旦不像真的，测出来的就只是桩自己的故事 —— 尤其 auto_confirm 那条。
    """

    def __init__(self, display=None, *, auto_confirm: bool = False) -> None:
        self.display = display if display is not None else _make_display()
        self.name = "会话"
        self.identifier = "agent-main"
        self.agent_info = AgentInfo(name=self.name, identifier=self.identifier,
                                    description="", workdir=Path.cwd())
        self.workspace = SimpleNamespace(workdir=Path.cwd())
        self.hooks = SimpleNamespace(before_execution=_StubHooks())
        self.conversation = _StubConversation()
        self.cancel_event = threading.Event()
        # 执行态（xun 的 `cancellable_execution`）：法官包不包这段等待，前端就照着显示
        # 「运行中」/「空闲」。桩照真实语义写：可重入、只有最外层退出才真的算闲下来。
        self.running = False
        self.run_depth = 0
        self.auto_confirm = auto_confirm
        self.auto_answers: list[str] = []

    @contextlib.contextmanager
    def cancellable_execution(self):
        self.run_depth += 1
        self.running = True
        try:
            yield
        finally:
            self.run_depth -= 1
            self.running = self.run_depth > 0

    def display_event(self, event) -> None:
        self.display.on_event(DisplayEvent(name=type(event).__name__,
                                           agent=self.agent_info, payload=event))

    def get_choice(self, prompt, choices, message=None, title=None, subtitle=None,
                   default=None, allow_extra=False, _skip_auto_confirm=False):
        if self.auto_confirm and not _skip_auto_confirm:
            pick = default or (list(choices)[0] if choices else "")
            self.auto_answers.append(prompt)
            return SimpleNamespace(choice=pick, source="auto")
        request = DisplayAbstract.ChoiceRequest(
            agent_info=self.agent_info, prompt=prompt, choices=list(choices), message=message,
            title=title, subtitle=subtitle, default=default, allow_extra=allow_extra)
        return SimpleNamespace(choice=self.display.get_choice(request), source="user")

    def instruct(self, text: str) -> "_StubAgent":
        self.conversation.messages.append({"role": "user", "content": text})
        return self

    def execute(self) -> str:
        params = SimpleNamespace(agent=self, schema=None, max_iterations=0, takeover_result=None)
        self.hooks.before_execution.invoke(params)
        return params.takeover_result or ""


def _make_agent(display=None, **kwargs) -> _StubAgent:
    """法官现在接管**当前** agent，所以测试要造的是一个 agent，不再只是一个 display。"""
    return _StubAgent(display, **kwargs)


def _judge() -> HostPresenter:
    """一个握着一局的法官。构造即接管，所以这里只剩"给它一份局面"。"""
    host = HostPresenter(_make_agent())
    host.state = _state()
    return host


class _SpyCoach:
    """假复盘教练：记住被问过什么；`block` 用来模拟「模型答得慢」。"""

    def __init__(self, answer: str = "那天刀你，是因为警徽在你身上。",
                 block=None) -> None:
        self.questions: list[str] = []
        self.answer, self.block = answer, block
        self.entered = threading.Event()      # 「我已经在教练里了」的信号

    def __call__(self, question: str) -> str:
        self.questions.append(question)
        self.entered.set()
        if self.block is not None:
            self.block.wait(5)                # 模拟模型答得慢
        return self.answer


def _bubbles(host: HostPresenter, author: str = REVIEWER) -> list[str]:
    return [e.payload.content for e in host.display.events
            if e.name == "ModelMessageEvent" and e.agent.name == author]


def _infos(host: HostPresenter) -> str:
    return " ".join(e.payload.message for e in host.display.events if e.name == "InfoEvent")


def test_the_coach_is_untouched_while_the_game_is_alive():
    """局没打完，输入框里的话仍走规则式回答 —— 教练一个字节都不许碰（AGENTS.md 的红线）。"""
    host, coach = _judge(), _SpyCoach()
    host.discussible(coach)
    try:
        host.agent.instruct("谁是狼").execute()
        assert coach.questions == [], "局还在打就把话交给模型：红线破了"
        assert _bubbles(host) == [], "局内不许有教练气泡冒出来"
        assert "公开信息" in _infos(host) or "不能" in _infos(host)
    finally:
        host.cleanup()


def test_after_close_game_the_input_box_talks_to_the_coach():
    """`close_game()` 之后打字就是讨论：话进教练，回答以「复盘教练」的气泡播（吃 markdown）。"""
    host, coach = _judge(), _SpyCoach()
    host.discussible(coach)
    try:
        host.close_game()
        host.agent.instruct("第 2 天你们为什么刀我").execute()
        assert coach.questions == ["第 2 天你们为什么刀我"], coach.questions
        bubbles = _bubbles(host)
        assert bubbles and coach.answer in bubbles[-1], "教练的回答得是气泡，不是灰色 info"
        assert coach.answer not in _infos(host), "同一句话不该又当系统日志播一遍"
    finally:
        host.cleanup()


def test_a_broken_coach_still_answers_instead_of_swallowing_the_question():
    """模型挂了不能把用户的话吞了：退回规则式回答，并说清这次为什么答得笨。"""
    def angry(question: str) -> str:
        raise RuntimeError("没有可用的模型")

    host = _judge()
    host.discussible(angry)
    try:
        host.close_game()
        reply = host.agent.instruct("复盘一下这局").execute()
        bubbles = _bubbles(host)
        assert bubbles and "没答上来" in bubbles[-1], bubbles
        assert reply and reply.strip(), "兜底也得给出一段话"
    finally:
        host.cleanup()


def test_two_questions_are_queued_not_asked_at_once():
    """连发两句：第二句不许并发冲进教练（会把它的会话写乱），但也不能被丢掉 —— 排队。"""
    release = threading.Event()
    coach = _SpyCoach(block=release)
    host = _judge()
    host.discussible(coach)
    try:
        host.close_game()
        out: list[str] = []
        first = threading.Thread(target=lambda: out.append(host.on_user_message(["第一个问题"])))
        first.start()
        assert coach.entered.wait(5), "第一句根本没进到教练里"
        second = host.on_user_message(["第二个问题"])
        assert coach.questions == ["第一个问题"], f"第二句并发冲进教练了：{coach.questions}"
        assert "排上了" in second, second          # 收了，但没说「现在就问」
        release.set()
        first.join(10)
        assert coach.questions == ["第一个问题", "第二个问题"], coach.questions
    finally:
        release.set()
        host.cleanup()


def test_typing_while_the_review_is_generating_queues_instead_of_racing():
    """复盘那几十秒里打字：不许第二个线程同时用教练那个会话 —— 排队，复盘完就答它。

    复盘是全场最久的一步，而它和讨论用的是**同一个**教练会话；两个线程一起 instruct +
    execute 会把那份记录写乱（同 `Engine._spawn` 的「同一个座位不许同时问两次」）。
    """
    order: list[str] = []
    started, release = threading.Event(), threading.Event()

    def slow_review() -> str:
        order.append("复盘开始")
        started.set()
        release.wait(5)
        order.append("复盘结束")
        return "## 复盘报告"

    host = _judge()
    host.discussible(lambda question: (order.append(f"答：{question}"), f"答：{question}")[1])
    try:
        host.close_game()
        generating = threading.Thread(target=lambda: host.coach_task(slow_review))
        generating.start()
        assert started.wait(5), "复盘根本没开始"
        note = host.on_user_message(["第 2 天你们为什么刀我"])
        assert "排上了" in note, note
        assert order == ["复盘开始"], f"复盘还没答完就有第二个线程在问教练了：{order}"
        release.set()
        generating.join(10)
        assert order == ["复盘开始", "复盘结束", "答：第 2 天你们为什么刀我"], order
    finally:
        release.set()
        host.cleanup()


def test_a_broken_review_tells_the_player_instead_of_dying_quietly():
    """复盘生成炸了要说清楚，而且**别把讨论一起炸掉**：记录还在，学员照样能问。"""
    def angry_review() -> str:
        raise RuntimeError("模型没配 key")

    host = _judge()
    asked: list[str] = []
    host.discussible(lambda question: (asked.append(question), "那天刀你是因为警徽在你身上。")[1])
    try:
        host.close_game()
        host.coach_task(angry_review)
        assert any("没生成出复盘" in text for text in _bubbles(host)), _bubbles(host)
        host.on_user_message(["那这局狼队是怎么配合的"])
        assert asked == ["那这局狼队是怎么配合的"], "复盘失败把讨论一起关了"
    finally:
        host.cleanup()


def test_ask_forwards_default_into_xuns_choice_request():
    """默认值是玩家最快的开局路径，必须真的落到 xun 的 ChoiceRequest 上。

    前端 PromptCard.vue 用 `selected = prompt.default` 预选中那个按钮（提交按钮随即可点），
    所以只在 `HostPresenter.ask` 的 kwarg 上打转是自欺欺人——这里一直传到显示层再断一次。
    """
    host = HostPresenter(_make_agent())
    answer = host.ask("你想坐几号位？", ["随机", "1号", "2号"], default="随机",
                      title="开局设置 · 第 2 步：你的座位", message="默认随机")
    request = host.display.requests[-1]
    assert request.default == "随机", "默认值没传进 xun 的 ChoiceRequest"
    assert list(request.choices) == ["随机", "1号", "2号"]
    assert (request.prompt, request.title) == ("你想坐几号位？", "开局设置 · 第 2 步：你的座位")
    assert request.message.startswith("默认随机"), request.message
    assert "点下面的按钮" in request.message, request.message   # 选择题要说清这一步点按钮
    assert request.allow_extra is False, "卡片不再靠自由输入框收长文本"
    assert answer == "随机", "假显示按 default 返回，取值也要对得上"


def test_confirm_marks_确认_as_default():
    host = HostPresenter(_make_agent())
    assert host.confirm("可以开局吗？") is True
    assert host.display.requests[-1].default == "确认"


def _state(seed: int = 3, preset: int = 1) -> GameState:
    config = PRESETS[preset].config.copy()
    config.seed = seed
    engine = Engine(config, {}, presenter=NullPresenter(), seed=seed)
    engine.setup()
    return engine.state  # type: ignore[return-value]


def _speech(state: GameState, seat: int, text: str, kind: str = K_SPEECH) -> Event:
    return Event(kind=kind, audience="public", text=f"{seat}号（发言）：{text}",
                 payload={"seat": seat}, day=state.day)


# --------------------------------------------------------------------------- 渲染层
def test_speeches_are_attributed_to_seats():
    state = _state()
    lines = render.render_batch(state, [
        _speech(state, 3, "我验了 5 号，查杀"),
        _speech(state, 5, "少来，我是预言家", K_SHERIFF_SPEECH),
        _speech(state, 2, "走了，信 1 号", K_LAST_WORDS),
    ])
    assert [(line.author, line.text) for line in lines] == [
        ("3号", "我验了 5 号，查杀"),
        ("5号（警上）", "少来，我是预言家"),
        ("2号（遗言）", "走了，信 1 号"),
    ]


def test_system_lines_have_no_author_and_no_markdown():
    state = _state()
    vote = Event(kind=K_VOTE, audience="public", text="1号 投票 3号", payload={"seat": 1}, day=1)
    result = Event(kind=K_VOTE_RESULT, audience="public", text="3号 被放逐（3 票）", payload={}, day=1)
    win = Event(kind=K_WIN, audience="public", text="狼人阵营胜利", payload={}, day=2)
    lines = render.render_batch(state, [vote, result, win])
    assert all(line.author is None for line in lines), lines
    joined = "\n".join(line.text for line in lines)
    assert "🗳" in joined and "▶" in joined
    for forbidden in ("**", "##", "|---", "```"):
        assert forbidden not in joined, f"纯文本通道里不该出现 markdown：{forbidden}"


def test_plainify_tames_markdown():
    text = render.plainify("### 标题\n**粗**体\n- 项目\n| a | b |\n`code`")
    assert "###" not in text and "**" not in text and "`" not in text
    assert "· 项目" in text and "a · b" in text


def test_status_lines_are_compact_and_gated():
    state = _state()
    plain = render.status_lines(state, god_view=False)
    assert "\n".join(plain).count("存活") == 1
    assert all("|" not in line for line in plain)
    assert not any(line.startswith("身份") for line in plain), "普通视角不能出现身份总表"
    god = render.status_lines(state, god_view=True)
    assert any(line.startswith("身份") for line in god)


def test_board_summary_never_leaks_roles():
    """板子构成（"狼人×2"）是公开信息，不能出现的是**座位与身份的绑定**。"""
    state = _state()
    summary = render.board_summary(state, god_view=False)
    for player in state.players.values():
        if player.role_revealed:
            continue
        for pattern in (f"{player.seat}号={player.role_name}", f"{player.seat}号（{player.role_name}",
                        f"{player.seat} 号 · {player.role_name}", f"{player.seat}号{player.role_name}"):
            assert pattern not in summary, pattern
    god = render.board_summary(state, god_view=True)
    assert any(f"{p.seat}号{p.role_name}" in god for p in state.players.values()), "上帝视角应给出绑定"


# --------------------------------------------------------------------------- 通道层
def test_judge_never_emits_chat_bubbles():
    host = HostPresenter(_make_agent())
    state = _state()
    try:
        host.state = state
        host.say("### 最终配置\n- 板子 A")
        host.info("本局共 3 天")
        host.notice(state, "复盘生成失败")
        host.phase(state, "第 1 天 · 发言")
        host.on_events(state, [_speech(state, 3, "查杀 5 号"),
                               Event(kind=K_WIN, audience="public", text="好人胜利", payload={}, day=1)])
        bubbles = [e for e in host.display.events if e.name == "ModelMessageEvent"]
        assert all(e.agent.name != "法官" for e in bubbles), "法官不该发聊天气泡"
        infos = harness.info_blocks(host.display.events)
        assert any("最终配置" in m for m in infos), infos
        assert any("第 1 天 · 发言" in m for m in infos)
        assert any("复盘生成失败" in m for m in
                   [e.payload.message for e in host.display.events if e.name == "WarningEvent"])
    finally:
        host.cleanup()


def test_player_speech_bubbles_are_authored_by_the_seat():
    host = HostPresenter(_make_agent())
    state = _state()
    try:
        host.state = state
        host.on_events(state, [
            _speech(state, 3, "我是预言家，验了 5 号"),
            _speech(state, 5, "他假的", K_SHERIFF_SPEECH),
            _speech(state, 2, "过了", K_LAST_WORDS),
        ])
        messages = [(e.agent.name, e.payload.content)
                    for e in host.display.events if e.name == "ModelMessageEvent"]
        assert messages == [("3号", "我是预言家，验了 5 号"),
                            ("5号（警上）", "他假的"),
                            ("2号（遗言）", "过了")], messages
        assert all(e.payload.total_tokens == 0
                   for e in host.display.events if e.name == "ModelMessageEvent")
    finally:
        host.cleanup()


def test_review_report_is_a_markdown_message_by_the_coach():
    host = HostPresenter(_make_agent())
    try:
        host.review("## 战局走向\n\n- 第 1 天出了 2 号")
        last = [e for e in host.display.events if e.name == "ModelMessageEvent"][-1]
        assert last.agent.name == REVIEWER
        assert last.payload.content.startswith("## ")
    finally:
        host.cleanup()


def test_judge_is_the_running_agent_itself():
    """法官不再新建 agent：整个会话被接管，所以既没有第二个 workspace，也没有第二个 toolbox。"""
    agent = _make_agent()
    host = HostPresenter(agent)
    try:
        assert host.agent is agent
        assert host.self_info.name == "会话"
        assert len(agent.hooks.before_execution.fns) == 1, \
            "构造即接管：钩子必须挂上（而且只挂一份，两份会互相覆盖 takeover_result）"
    finally:
        host.cleanup()


def test_takeover_is_forever():
    """一局一命：局打完了法官**继续**接管，这条会话不再进模型。

    没有 hand_back、也没有退出命令 —— 想把这个 agent 拿去干别的，出口是前端「新建会话」。
    这条测试盯的就是"我们真的不打算交还"：漏了这点就会有人往回加 `judge_mode` 开关，
    而开关一旦存在，就得再养一条 leave 命令和一套收尾状态机。
    """
    host = _judge()
    agent = host.agent
    assert agent.instruct("现在还有谁活着？").execute() != ""
    host.cleanup()
    assert agent.instruct("复盘一下 3 号").execute() != "", "局后法官仍在：这条会话不再调模型"
    assert not hasattr(host, "hand_back"), "别再提供交还会话的出口"


def test_speech_is_taken_from_the_input_box_end_to_end():
    """播一张「轮到你」的卡 → 玩家在输入框说话 → 这句话交给正在等的环节。

    这是 `wait_text` 唯一的端到端覆盖，盯住三件会各自坏掉的事：
    卡片要说清话发在哪儿（不然玩家还是去点卡片）；发言要真的送达等待者；
    以及那句话不该沉进会话历史（接管时已经被摘走了）。
    """
    import time

    host = _judge()
    agent = host.agent
    got: list[str] = []
    thread = threading.Thread(target=lambda: got.append(host.wait_text("第 1 天 · 发言", "你排第 2/5")))
    thread.start()
    try:
        for _ in range(200):                      # 等它真的挂起，别用 sleep 赌运气
            if host._awaiting:
                break
            time.sleep(0.01)
        assert host._awaiting == 1, "wait_text 没有进入等待"
        blocks = harness.info_blocks(host.display.events)
        assert any("输入框" in b for b in blocks), blocks
        assert any("你排第 2/5" in b for b in blocks), blocks

        agent.instruct("我觉得 2 号最像狼，今天先出他。").execute()
        thread.join(timeout=5)
        assert got == ["我觉得 2 号最像狼，今天先出他。"], got
        assert agent.conversation.messages == [], "发言不该留在会话历史里"
    finally:
        host.stop()
        thread.join(timeout=5)
        host.cleanup()


def test_judge_cards_are_not_auto_confirmed():
    """会话开着自动确认时，法官的卡片也不能被自动答掉 —— 真人的决策必须玩家亲手点。

    xun 的 auto-confirm 判断在显示层里，`Agent.get_choice` 只认 `_skip_auto_confirm`，
    所以这条是靠那个下划线参数保住的，别"顺手清理"成普通入参。
    """
    agent = _make_agent(auto_confirm=True)
    host = HostPresenter(agent)
    host.ask("首刀目标？", ["1号", "2号"], default="1号")
    assert agent.auto_answers == [], "法官卡片被 auto-confirm 答掉了"
    # 反向对照：少了那个参数，这个桩确实会自动答 —— 说明上一条断言不是空的
    agent.get_choice("模型自己的问题", ["a", "b"], default="a")
    assert agent.auto_answers == ["模型自己的问题"]


def test_takeover_consumes_the_message_it_answers():
    """回答过的那句话要从会话里取走 —— 这就是"这批已消费"的游标。

    不取走的话，用户在等发言时连发两句，下一次读又会读到同一句；反正这条会话已经
    不会再调模型，留着也没人读，取走比记游标便宜。
    """
    host = _judge()
    agent = host.agent
    agent.instruct("现在还有谁活着？")
    assert len(agent.conversation.messages) == 1
    reply = agent.execute()
    assert "存活" in reply
    assert agent.conversation.messages == [], "法官的问题不该留在会话历史里"


def test_user_message_to_judge_is_answered_without_the_model():
    """接管之后用户发的话由法官规则式回答：一次模型都不调，答案走 info 块。

    这条以前靠 `_JudgeAgent`（一个没有工具的小 agent），现在走 xun 的 takeover ——
    断言的是"用户的话不会掉进模型"这个不变量，跟法官住在哪个 agent 里无关。
    """
    host = _judge()
    host.agent.instruct("现在还有谁活着？").execute()
    infos = [e.payload.message for e in host.display.events if e.name == "InfoEvent"]
    assert any("存活" in m for m in infos), infos
    host.cleanup()


def test_judge_refuses_identities_while_the_game_is_alive():
    host = _judge()
    state = host.state
    try:
        host.agent.instruct("谁是狼").execute()
        reply = " ".join(e.payload.message for e in host.display.events if e.name == "InfoEvent")
        assert "不能" in reply or "公开信息" in reply, reply
        for player in state.players.values():
            assert player.role_name not in reply

        host.agent.instruct("现在规则是什么").execute()
        reply = " ".join(e.payload.message for e in host.display.events if e.name == "InfoEvent")
        assert "板子" in reply or "胜负" in reply, reply

        host.agent.instruct("随便说点什么奇怪的").execute()
        reply = " ".join(e.payload.message for e in host.display.events if e.name == "InfoEvent")
        from werewolf.ui.answers import USAGES
        assert USAGES in reply, "兜底该把「我只能答这几类」说清楚"

        # 用法说明不能教人把发言写进卡片：那会绕过 wait_text，引擎永远等不到这句话
        host.agent.instruct("怎么用").execute()
        reply = " ".join(e.payload.message for e in host.display.events if e.name == "InfoEvent")
        assert "输入框" in reply, reply
        assert "或输入其他答复" not in reply, reply
    finally:
        host.cleanup()


def test_whole_fake_game_over_the_real_channels():
    """整局跑一遍：法官零气泡，所有气泡都来自座位。"""
    config = PRESETS[1].config.copy()
    config.seed = 11
    engine = Engine(config, {}, presenter=NullPresenter(), seed=11)
    engine.setup()
    state = engine.state
    host = HostPresenter(_make_agent())
    try:
        host.state = state
        presenter = host
        rng = random.Random(7)
        engine.presenter = presenter
        engine.actors = {seat: FakeActor(seat, rng) for seat in state.players}
        engine.run()
        bubbles = [(e.agent.name, e.payload.content)
                   for e in host.display.events if e.name == "ModelMessageEvent"]
        assert bubbles, "发言应该以气泡形式出现"
        assert all(name[0].isdigit() or name.startswith("复盘") or "号" in name for name, _ in bubbles), bubbles
        assert not any(name == "法官" for name, _ in bubbles)
        # 活动窗口要成对开合：漏关会让圆点对着一局已经结束的牌局一直装忙。
        # 这里同时盯住另一头 —— 整局一次都没亮过，说明汇聚点上的打招呼又丢了。
        assert not host._waiting, f"跑完一局仍有没关的活动窗口：{list(host._waiting)}"
        assert any(e.name == "ModelWorkingEvent" for e in host.display.events), "整局都没亮活动指示"
    finally:
        host.cleanup()


def test_status_block_refreshes_when_the_day_changes():
    """局面摘要：同一阶段内不刷屏，换天时强制重发一次。"""
    host = HostPresenter(_make_agent())
    try:
        state = _state()
        host.state = state
        host.phase(state, "第 1 天 · 发言", "顺序发言")
        host.phase(state, "第 1 天 · 投票", "投票放逐一名玩家")
        host.phase(state, "第 2 天 · 夜幕", "神职行动")
        host.cleanup()        # 播报是攒到阶段收尾才落地，这里强制结算
        msgs = harness.info_blocks(host.display.events)
        assert sum("存活" in m for m in msgs) == 2, msgs
        # 同一阶段内不重复局面摘要；阶段头也只出现一次（文本形态是 ━━ 行，卡片形态是标题栏）
        assert sum("第 1 天 · 发言" in m for m in msgs) == 1, msgs
    finally:
        host.cleanup()


def test_board_lines_are_plain_text_lines():
    from werewolf.engine.config import PRESETS

    rows = render.build_board_lines(PRESETS[0].config)
    assert rows[0].startswith("板子：")
    assert all("|" not in row and not row.startswith("#") for row in rows)
    assert any(row.startswith("判定：") for row in rows)


def test_system_lines_are_batched_into_one_block():
    """连续的系统行并成一条 info；只有玩家发言才断开成气泡。"""
    host = HostPresenter(_make_agent())
    try:
        state = _state()
        host.state = state
        host.on_events(state, [
            Event(kind=K_DEATH_ANNOUNCE, audience="public", text="昨夜倒牌的是 1号", payload={}, day=1),
            Event(kind=K_VOTE_RESULT, audience="public", text="第 1 天票型：3号 2票", payload={}, day=1),
            _speech(state, 3, "我是预言家，查杀 5 号"),
            Event(kind=K_EXILE, audience="public", text="5号 以最高票被放逐出局", payload={"seat": 5}, day=1),
        ])
        host.cleanup()
        msgs = harness.info_blocks(host.display.events)
        assert len(msgs) == 2, msgs
        assert "昨夜倒牌" in msgs[0] and "票型" in msgs[0], msgs[0]
        assert "放逐" in msgs[1], msgs[1]
    finally:
        host.cleanup()


def test_thinking_uses_the_same_author_label_as_the_speech_bubble():
    """法官说"谁在想"的时候，名字必须和稍后出现的发言气泡一致（含警上/遗言后缀）。"""
    host = HostPresenter(_make_agent())
    try:
        state = _state()
        host.state = state
        host.thinking(state, 3, K_SHERIFF_SPEECH)
        host.say_as(render.speech_author(3, K_SHERIFF_SPEECH), "警上讲两句。")
        bubble = [e for e in host.display.events if e.name == "ModelMessageEvent"][-1]
        assert bubble.agent.name == "3号（警上）", bubble.agent.name
        # 正常速度说完：不该有任何"还在想"的播报
        assert not [e for e in host.display.events if e.name == "WarningEvent"]
    finally:
        host.cleanup()


def test_working_indicator_rides_the_bound_agent():
    """活动指示必须由**已绑定的法官**发出 —— 挂在座位那种手工作者身上前端不画（实测过）。

    同时守住气泡连续性：思考窗口与随后那句发言共用一个 `model_call_id`，
    否则前端会把一个人的一句话拆成两段。
    """
    host = HostPresenter(_make_agent())
    try:
        state = _state()
        host.state = state
        host.thinking(state, 3, K_SHERIFF_SPEECH)
        working = [e for e in host.display.events if e.name == "ModelWorkingEvent"]
        assert len(working) == 1, working
        assert working[0].agent.identifier == host.self_info.identifier, working[0].agent
        assert not working[0].agent.identifier.startswith("ww-"), working[0].agent.identifier

        host.thinking(state, 3, K_SHERIFF_SPEECH)            # 同一座位重复打招呼
        assert len([e for e in host.display.events
                    if e.name == "ModelWorkingEvent"]) == 1, "不许叠窗口"

        author = render.speech_author(3, K_SHERIFF_SPEECH)
        host.say_as(author, "警上讲两句。")
        bubble = [e for e in host.display.events if e.name == "ModelMessageEvent"][-1]
        assert bubble.payload.model_call_id == working[0].payload.model_call_id, \
            "发言必须接在同一个思考窗口上"
        assert not host._waiting, "话说完了就该收掉窗口"
    finally:
        host.cleanup()


def test_two_threads_never_split_a_block():
    """`/werewolf status` 在命令线程发看板，游戏线程同时在播报：不许出现半截块。

    两个线程都往 `HostPresenter._pending`（普通 list）里攒：没有锁就会互相咬掉半截、
    或把别人的内容接到自己的标题下面，前端就是一片错乱的灰块。
    """
    host = HostPresenter(_make_agent())
    state = _state()
    host.state = state
    errors: list[BaseException] = []

    def game_thread() -> None:
        try:
            for day in range(1, 21):
                host.phase(state, f"第 {day} 天 · 发言", "顺序发言")
                host.on_events(state, [_speech(state, 2, "我讲两点"), _speech(state, 4, "我附议")])
                host.say_as("5号", "同意 2号。")
        except BaseException as exc:            # noqa: BLE001  收起来最后一起断言
            errors.append(exc)

    def command_thread() -> None:
        try:
            for _ in range(40):
                host.publish_status()
        except BaseException as exc:            # noqa: BLE001
            errors.append(exc)

    try:
        threads = [threading.Thread(target=fn) for fn in (game_thread, command_thread)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(thread.is_alive() for thread in threads), "有线程没跑完（锁成死锁了？）"
    finally:
        host.cleanup()

    assert not errors, [repr(e) for e in errors]
    blocks = harness.info_blocks(host.display.events)
    assert blocks and all(block.strip() for block in blocks), blocks      # 没有半截空块
    assert sum("存活" in block for block in blocks) >= 2, blocks          # 看板真发出去了


def test_human_own_speech_is_not_replayed_as_a_seat_bubble():
    """真人打的字在会话流里已经有他自己的气泡，不许再播一遍成「5号」的气泡。"""
    host = HostPresenter(_make_agent())
    try:
        state = _state()
        host.state = state
        human = sorted(state.players)[0]        # `_state()` 只排座位，真人座位自己指定
        state.config.human_seat = human         # config 是副本（`PRESETS[...].config.copy()`）
        ai = next(seat for seat in state.players if seat != human)
        host.on_events(state, [
            _speech(state, human, "我是好人，我要守护大家！"),
            _speech(state, ai, "我是预言家，昨夜验了 3 号。"),
        ])
        authors = [e.agent.name for e in host.display.events if e.name == "ModelMessageEvent"]
        assert f"{ai}号" in authors, authors                       # AI 座位的气泡照旧
        assert not [a for a in authors if a.startswith(f"{human}号")], authors
    finally:
        host.cleanup()


def test_each_prompt_card_says_how_to_answer_it():
    """两类卡各自把「怎么回答」写在脸上 —— 猜错方向的代价是玩家的输入被吞。

    选择题是带按钮的卡（在输入框里打字不算回答）；发言那张卡**没有按钮也没有输入框**，
    话要发在会话输入框里。后者原先写「不用点卡片」，读起来像该弹的卡片没弹出来。
    """
    host = HostPresenter(_make_agent())
    try:
        host.ask("要刀谁？", ["1号", "不用"], message="夜里")
        choice = host.display.requests[-1].message
        assert "点下面的按钮" in choice, choice

        with host._input:                       # 先放好那句话，wait_text 取到就返回
            host._inbox.append("我怀疑 1 号。")
        host.wait_text(title="第 1 天 · 发言", subtitle="你排第 1/2", note="该你说话了")
        card = [e for e in host.display.events if e.name == "HTMLInfoEvent"][-1]
        text = card.payload.to_text()
        assert "输入框" in text, text
        assert "不用点卡片" not in text, text            # 不许引用根本没弹出来的控件
        assert "/werewolf status" in text, text          # 不被吞的出口要写在卡上
    finally:
        host.cleanup()


def test_public_board_never_assigns_identities_or_counts_wolves():
    """公开出口逐个审计：不许出现「N号=身份」的身份分配，也不许报狼的数量。

    规则文本里合法地出现角色名（「猎人/狼王：被女巫毒杀不能开枪」），所以这里钉的是
    **身份分配的形状**（座位号紧跟角色名）与狼数，而不是角色名本身。
    玩家自己贴的标签是另一回事：`_visible` 里 `not is_alive` 那条兜住死人身份。
    """
    import re

    from werewolf.engine.roles import ROLES
    from werewolf.ui import answers

    state = _state()
    exits = {
        "看板卡": "\n".join(render.status_lines(state, False)),
        "局面摘要": render.board_summary(state, False),
        "法官问答·局面": answers.answer(state, False, ["现在什么情况"]),
        "法官问答·人数": answers.answer(state, False, ["几人活着"]),
    }
    assigned = re.compile(r"\d+号(?:%s)" % "|".join(ROLES))
    for name, text in exits.items():
        assert text, (name, "空出口等于没审计")
        assert not assigned.search(text), (name, text)
        assert "场上狼" not in text and "匹" not in text, (name, text)

def test_working_windows_open_and_close_in_pairs():
    """一次思考只打一行 running，收尾必须配对。

    文本形态靠这条 `ModelWorkingEvent` 打「🟢 某某 running」，所以一个窗口只能有一条；
    网页形态不再靠补发事件顶圆点（那套 `_rearm` 随旧前端一起作废），它看的是执行态。
    """
    agent = _make_agent()
    host = HostPresenter(agent)

    def lit() -> int:
        return len([e for e in host.display.events if e.name == "ModelWorkingEvent"])

    try:
        state = _state()
        host.state = state
        host.thinking(state, 3, K_SPEECH)
        assert lit() == 1, "开始算要亮一次"
        host.info("播报一条")
        host.thinking(state, 3, K_SPEECH)
        assert lit() == 1, "同一窗口不叠加、也不补发顶尾部 —— 否则终端被 running 刷满"
        assert agent.running is True
        host.thought(state, 3)
        assert lit() == 1, "收尾不该再补一条 running"
        assert agent.running is False, "算完了执行态必须松开"
    finally:
        host.cleanup()


def test_reasking_a_seat_closes_its_stale_window():
    """同一座位换环节时，上一环节漏关的窗口要收尾。

    否则新标签覆盖 `_opened[seat]` 以后旧标签再没人关，那个僵尸窗口会一直撑着
    执行态 —— 「运行中」就从此对着一局早算完的牌局永远亮着。
    先自校验夹具：真有两个环节的作者标签不同，否则这条用例是空转的。
    """
    from werewolf.engine import events as ev

    host = HostPresenter(_make_agent())
    try:
        state = _state()
        host.state = state
        kinds = [getattr(ev, name) for name in dir(ev) if name.startswith("K_")]
        labels = {kind: render.speech_author(3, kind) for kind in kinds}
        assert len(set(labels.values())) > 1, f"夹具失效，标签全一样：{set(labels.values())}"
        host.thinking(state, 3, K_SPEECH)                       # 模拟漏播气泡：这窗口没人关
        host.thinking(state, 3, next(k for k, v in labels.items() if v != labels[K_SPEECH]))
        assert len(host._waiting) == 1, list(host._waiting)     # 旧的那个必须被收尾
    finally:
        host.cleanup()


def test_auto_speech_answers_only_the_speech_it_was_asked_for():
    """`/auto-say`：只在正等一句话时生效、用完即失效；他自己打了一句就以他的为准。"""
    import threading
    import time

    host = HostPresenter(_make_agent())
    try:
        host.state = _state()
        assert not host.request_auto_speech(), "没在等发言时不该生效，更不该把请求攒着等下次"
        assert not host._auto, "无效的请求不许留下状态"

        produced: list[str] = []
        out: list[str] = []

        def make_line() -> str:
            produced.append("call")
            return "法官替他说的一句"

        def speak() -> None:
            out.append(host.wait_text("第 1 天 · 发言", auto=make_line))

        def await_input() -> None:
            for _ in range(500):
                if host._awaiting:
                    return
                time.sleep(0.01)
            raise AssertionError("没等到发言环节")

        thread = threading.Thread(target=speak)
        thread.start()
        await_input()
        assert host.request_auto_speech(), "正等着一句话时要能生效"
        thread.join(20)
        assert out == ["法官替他说的一句"], out
        assert len(produced) == 1 and not host._auto, "用完必须失效"

        thread2 = threading.Thread(target=speak)
        thread2.start()
        await_input()
        assert host.request_auto_speech()
        host.on_user_message(["等等，我自己来说"])
        thread2.join(20)
        assert out[1] == "等等，我自己来说", out
        assert len(produced) == 1, "他自己把那句话说了，就不该再叫模型"
    finally:
        host.cleanup()


def test_the_line_the_judge_spoke_is_heard_by_the_table():
    """法官替真人说的那一句必须播成气泡。

    「不重播真人自己的话」那条过滤（`_own_labels`）针对的是他自己输入框里那句；代说的
    那句要是也被一起吞掉，场上就等于假装他没说话。
    """
    from werewolf.engine.events import K_SPEECH, PUBLIC, Event

    host = HostPresenter(_make_agent())
    try:
        state = _state()
        seat = state.config.human_seat = 3        # 夹具默认全 AI 局，这里让 3 号当真人
        host.state = state
        # 自校验：气泡作者标签要真的落进"不重播"那一批，否则下面两个断言都是空转
        assert render.speech_author(seat, K_SPEECH) in host._own_labels(state)
        words = "我先听后面的玩家怎么说"
        event = Event(kind=K_SPEECH, audience=PUBLIC, text=f"{seat}号（发言）：{words}",
                      payload={"seat": seat, "words": words}, day=state.day)

        def bubbles() -> list[str]:
            return [e.agent.name for e in host.display.events if e.name == "ModelMessageEvent"]

        host.on_events(state, [event])
        assert bubbles() == [], "他自己打的那句话本来就在流里，不该重播一遍"
        host.spoke_for_player(seat, K_SPEECH)
        host.on_events(state, [event])
        assert bubbles(), "法官替他说的这一句必须上桌"
    finally:
        host.cleanup()


# ---------------------------------------------------------------- 执行态（网页的「运行中」）
# 前端判断这个 agent 忙不忙，只看 xun 在执行态进出时发的 AgentRunningStart/EndEvent。
# 游戏跑在后台线程、从不进执行态，所以「AI 正在算」那几秒必须由法官主动包一段范围，
# 否则界面写着「空闲」、什么都不转，看着就像卡死。反过来，等真人的时候必须松开：
# 那段时间前端把「发送」换成了「停止」，扣着范围就是扣着用户打字的能力。
def _busy_judge() -> tuple[HostPresenter, _StubAgent]:
    agent = _make_agent()
    host = HostPresenter(agent)
    host.state = _state()
    return host, agent


def test_the_judge_enters_the_run_state_while_a_seat_is_computing():
    host, agent = _busy_judge()
    assert agent.running is False, "没开局、没人在算，不该抢一个「运行中」"

    host.thinking(host.state, 3, K_SPEECH)
    assert agent.running is True, "AI 座位正在算，前端必须看得见它在忙"

    host.thinking(host.state, 7, K_SPEECH)     # 并发环节：第二个座位也在算
    assert agent.run_depth == 1, "同一批等待只能包一层，退出时才会干净"

    host.thought(host.state, 3)
    assert agent.running is True, "7号 还在算，不能因为一个座位落地就装闲"

    host.thought(host.state, 7)
    assert agent.running is False, "都落地了还亮着「运行中」就是撒谎"
    assert agent.run_depth == 0


def test_a_pending_card_leaves_the_run_state_so_the_user_can_answer():
    """卡片挂着等真人时不能占着执行态 —— 网页那段时间只给「停止」按钮。"""
    host, agent = _busy_judge()
    host.thinking(host.state, 3, K_SPEECH)     # 还有个 AI 在算

    seen = {}

    def fake_choice(prompt, choices, **kwargs):
        seen["running"] = agent.running
        return SimpleNamespace(choice=list(choices)[0], text="")

    host.agent.get_choice = fake_choice
    host.ask(prompt="今晚刀谁", choices=("1号", "2号"))
    assert seen == {"running": False}, "卡片等真人时必须松开执行态"
    assert agent.running is True, "卡片收了、3号 还在算 —— 「运行中」要接回去"

    host.thought(host.state, 3)
    assert agent.running is False, "卡片收了、也算完了"


def test_waiting_for_a_spoken_line_frees_the_composer_and_reclaims_it_after():
    host, agent = _busy_judge()
    host.thinking(host.state, 3, K_SPEECH)     # 等真人的同时，另一只狼还在算

    got = {}

    def speak():
        got["text"] = host.wait_text("轮到你发言")

    thread = threading.Thread(target=speak, daemon=True)
    thread.start()
    for _ in range(200):
        if host._awaiting:
            break
        time.sleep(0.01)
    got["waiting"] = agent.running           # 采样：这段时间用户要能打字

    host.on_user_message(["我怀疑 5 号。"])
    thread.join(timeout=5)
    assert got["text"] == "我怀疑 5 号。"
    assert got["waiting"] is False, "等输入框的时候不能占着执行态，否则那几秒打不了字"
    assert agent.running is True, "输入落地，还在算的那只狼要把「运行中」接回去"

    host.thought(host.state, 3)
    assert agent.running is False


def test_cleanup_never_leaves_a_run_scope_behind():
    host, agent = _busy_judge()
    host.thinking(host.state, 3, K_SPEECH)
    host.thinking(host.state, 5, K_SPEECH)
    host.cleanup()
    assert agent.running is False, "局 end 了还挂着执行态 = 永远亮着的「运行中」"
    assert agent.run_depth == 0, "范围没配对退出，前端会一直显示忙"
