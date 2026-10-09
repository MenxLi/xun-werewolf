"""extension（`/werewolf`）的测试：加载零副作用、引擎加载方式、参数解析、守卫、开局透传。

不依赖 LLM，也不真的开一局（GameSession 用桩替换）。
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path


#: extension 目录 == 引擎包：入口 setup_extension.py 在它根部，engine/ actors/ ui/ 在旁边
ROOT = Path(__file__).resolve().parents[1]              # 仓库根
EXT_DIR = ROOT / "werewolf"
#: 入口顶层不许 import 的东西 —— 搬进包里之后，引擎既能绝对导入进来，也能相对导入进来
ENGINE_ROOTS = {"werewolf", "engine", "actors", "ui"}


def _ext():
    """入口是 `werewolf/setup_extension.py`。

    从仓库根绝对导入它：`__package__` 于是是 `werewolf`，引擎按相对导入解析成
    `werewolf.engine.*` —— 和这里测试用的是同一份引擎，不会出现两份全局状态。
    生产里 xun 用 `xun_ext_werewolf` 包壳加载那条路，归 test_entry_loads_the_way_xun_does 管。
    """
    return importlib.import_module("werewolf.setup_extension")


class FakeCommandRegistry:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def register(self, *commands):
        for command in commands:
            self.commands[command.name] = command
        return self


class FakeToolBox:
    """照 xun 的语义收起：`disable("*")` 不删工具，只是 `list_tools` 不再把它们交给模型。"""

    def __init__(self) -> None:
        self.tools = {"bash": object(), "read_file": object()}
        self.disabled: list[str] = []

    def disable(self, pattern: str) -> None:
        self.disabled.append(pattern)

    def list_tools(self):
        return [] if "*" in self.disabled else list(self.tools)


class FakeAgent:
    def __init__(self, name: str = "main", display=None, identifier: str | None = None) -> None:
        self.name = name
        self.identifier = identifier or f"agent-{name}"
        self.display = display if display is not None else object()
        self.command = FakeCommandRegistry()
        self.toolbox = FakeToolBox()
        self.config = type("Cfg", (), {"model": type("M", (), {"name": "some-model"})()})()
        self.messages: list[str] = []

    def info(self, text: str) -> None:
        self.messages.append(text)

    def error(self, text: str) -> None:
        self.messages.append(text)


class FakeCtx:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent


# --------------------------------------------------------------------------- 加载副作用
def test_setup_extension_only_adds_its_command():
    """加载时只注册 `/werewolf` 这一条 —— 其余命令（含 `/auto-say`）要等它真跑起来。"""
    module = _ext()
    agent = FakeAgent()
    tools_before = sorted(agent.toolbox.tools)
    config_before = (agent.config.model.name, vars(agent.config.model))
    attrs_before = set(vars(agent))

    module.setup_extension(FakeCtx(agent))

    assert set(agent.command.commands) == {module.COMMAND_NAME}, \
        "除 `/werewolf` 之外的命令不许在加载时就出现"
    assert module.AUTO_COMMAND not in agent.command.commands, \
        "`/auto-say` 在一局开始前只会回「没轮到你」，不许提前占补全菜单"
    assert sorted(agent.toolbox.tools) == tools_before, "不许往用户 agent 里塞工具"
    assert agent.config.model.name == config_before[0], "不许改 config"
    assert set(vars(agent)) == attrs_before, "不许给 agent 挂新属性"


def test_the_judge_takes_over_the_command_surface():
    """接管命令面：`/auto-say` 到这一步才注册，agent 自用的工具与命令都不再响应。"""
    from types import SimpleNamespace

    from xun import Command, CommandRegistry, ToolBox

    rt = _ext()
    agent = SimpleNamespace(toolbox=ToolBox().with_defaults(),
                            command=CommandRegistry().with_defaults())
    agent.command.register(Command(name=rt.COMMAND_NAME, handler=lambda a: None, description="x"))
    assert agent.toolbox.list_tools(), "夹具：默认工具箱该有工具"
    assert agent.command.get("clear") is not None, "夹具：默认命令在收起前要用得到"
    assert agent.command.get(rt.AUTO_COMMAND) is None, "夹具：`/auto-say` 归接管这一步注册"

    rt.takeover_command_surface(agent)

    assert agent.toolbox.list_tools() == [], "工具要全部收起，不再交给模型"
    assert set(agent.command.commands) == {rt.COMMAND_NAME, rt.AUTO_COMMAND}
    assert agent.command.get("clear") is None, "默认命令要真的不再响应"
    assert agent.command.get("help") is not None, "help 由 registry 现造，留着是对的"

    rt.takeover_command_surface(agent)          # 一局一命，但幂等更省心：不许把命令弄丢
    assert set(agent.command.commands) == {rt.COMMAND_NAME, rt.AUTO_COMMAND}


def test_extension_entry_contract():
    """xun 要求的契约：入口函数名 + docstring 首行当描述。"""
    module = _ext()
    assert callable(getattr(module, "setup_extension", None))
    doc = (module.__doc__ or "").strip()
    assert doc and doc.splitlines()[0], "模块 docstring 首行会成为 extension 描述"
    assert "狼人杀" in doc


def test_extension_does_not_import_engine_at_module_level():
    """命令执行前不许 import 引擎：加载 extension 必须零副作用。

    入口就住在引擎包里，于是引擎既能 `import werewolf.engine` 进来，也能一句顶层
    `from . import engine` 进来 —— 两条都要拦（相对那条是入口搬进包之后新出现的）。
    """
    tree = ast.parse((EXT_DIR / "setup_extension.py").read_text())
    for node in tree.body:                      # 只看模块顶层
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"顶层相对导入了引擎：.{node.module}"
            names = [node.module or ""]
        else:
            continue
        assert not any(name.split(".")[0] in ENGINE_ROOTS for name in names), \
            f"顶层 import 了引擎：{names}"


# --------------------------------------------------------------------------- 引擎加载方式
def test_load_engine_does_not_touch_sys_path():
    """引擎包就在入口旁边，靠相对导入拿，不许再往 sys.path 里塞东西。"""
    rt = _ext()
    before = list(sys.path)
    rt._load_engine()
    assert sys.path == before, "加载引擎改动了 sys.path（相对导入又被换回路径魔法了？）"


def test_entry_loads_the_way_xun_does():
    """生产路径：xun 用 `xun_ext_<name>` 包壳加载入口，此时引擎只能靠相对导入解析进来。

    这条锁的是「在仓库里跑得好好的、装进 extensions/ 就炸」那一整类问题。
    """
    import importlib.util
    import types

    pkg = "xun_ext_werewolf_probe"
    saved = {k for k in sys.modules if k.startswith(pkg)}
    shell = types.ModuleType(pkg)
    shell.__path__ = [str(EXT_DIR)]                     # 与 xun extension.py 的做法一致
    sys.modules[pkg] = shell
    name = f"{pkg}.setup_extension"
    try:
        spec = importlib.util.spec_from_file_location(name, EXT_DIR / "setup_extension.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)                    # 加载时只该注册命令
        engine = mod._load_engine().Engine.__module__
        assert engine.startswith(pkg + "."), f"引擎没走相对导入，它的身份是：{engine}"
    finally:
        for key in {k for k in sys.modules if k.startswith(pkg)} - saved:
            del sys.modules[key]


def test_broken_install_hint_is_actionable():
    """装错时那句提示得有用：说清缺什么、入口在哪、给能照抄的修法；别再提已废弃的开关。"""
    rt = _ext()
    text = rt._broken_install_hint(ImportError("No module named 'x'"))
    assert "werewolf/engine/engine.py" in text, "要说清缺了什么"
    assert "cp -r" in text, "要给能照抄的修法"
    assert "setup_extension.py" in text, "要告诉用户他这份入口在哪"
    assert "WEREWOLF_HOME" not in text, "已废弃的 WEREWOLF_HOME 不该再出现"


# --------------------------------------------------------------------------- 参数与守卫
def test_parse_arguments_variants():
    """开局设置全在卡片里问 → 命令只剩 stop/status/?，其余一律不认。"""
    rt = _ext()
    assert rt.parse_arguments(None).action == "start"
    assert rt.parse_arguments([]).action == "start"
    assert rt.parse_arguments("").action == "start"
    assert rt.parse_arguments(["stop"]).action == "stop"
    assert rt.parse_arguments(["停"]).action == "stop"
    assert rt.parse_arguments(["status"]).action == "status"
    assert rt.parse_arguments(["局面"]).action == "status"
    assert rt.parse_arguments(["?"]).action == "help"
    # 板子、座位以前是命令参数，现在只能在卡片里选
    for word in ("A", "D", "7", "配", "seat=3"):
        assert rt.parse_arguments([word]).action == "unknown", word
    assert rt.parse_arguments(["A", "seat=3"]).unknown == ["A", "seat=3"]
    assert rt.parse_arguments(["xyz"]).unknown == ["xyz"]


def test_guard_rejects_subagent_and_headless():
    from xun.displays import NullDisplay

    rt = _ext()
    assert "主会话" in (rt.guard_reason(FakeAgent(name="main-child-ab12cd34")) or "")
    assert "无界面" in (rt.guard_reason(FakeAgent(name="main", display=NullDisplay())) or "")
    assert rt.guard_reason(FakeAgent(name="main", display=object())) is None


# --------------------------------------------------------------------------- 命令行为
def test_headless_or_child_never_starts_a_game():
    rt = _ext()
    agent = FakeAgent(name="main-child-0001", display=object())
    rt.start_game(agent, ["A"])
    assert agent.messages and rt.active_record(agent) is None

    from xun.displays import NullDisplay
    agent2 = FakeAgent(name="main", display=NullDisplay())
    rt.start_game(agent2, ["A"])
    assert agent2.messages and rt.active_record(agent2) is None


def test_unknown_argument_shows_usage_instead_of_starting():
    rt = _ext()
    agent = FakeAgent()
    rt.start_game(agent, ["--nope"])
    assert rt.active_record(agent) is None
    assert "没看懂" in " ".join(agent.messages)


def test_status_and_stop_without_a_game():
    rt = _ext()
    for tokens in (["status"], ["stop"]):
        agent = FakeAgent(name=f"probe-{tokens[0]}")
        rt.start_game(agent, tokens)
        assert "没有狼人杀" in " ".join(agent.messages)


class _FakeSession:
    """顶掉入口文件里的 GameSession，只检查 extension 传了什么参数。"""

    last_kwargs: dict = {}
    instances: list["_FakeSession"] = []

    def __init__(self, agent, **kwargs) -> None:
        type(self).last_kwargs = {"agent": agent, **kwargs}
        self.agent = agent
        self.display = getattr(agent, "display", None)
        self.stopped = False
        self.cleaned = False
        self.started = False
        self.alive = False                # 真 GameSession 上是 thread.is_alive()
        self.published: list[str] = []
        published = self.published          # 闭包捕获，避免假 host 的 self 传参问题
        self.host = type("H", (), {"publish_status": staticmethod(lambda: published.append("board"))})()
        type(self).instances.append(self)

    def start_in_background(self):
        self.started = True
        return self

    def stop(self) -> None:
        self.stopped = True

    def cleanup(self) -> None:
        self.cleaned = True


def test_start_game_hands_the_running_agent_to_the_session():
    rt = _ext()
    real = rt.GameSession
    _FakeSession.instances = []
    rt.GameSession = _FakeSession
    try:
        agent = FakeAgent(name="solo", display=object())
        rt.start_game(agent, [])
        kwargs = _FakeSession.last_kwargs
        assert kwargs["agent"] is agent, "法官就是当前会话这个 agent，不能再新建一个"
        assert set(kwargs) == {"agent", "model_name"}, \
            "开局不该有命令参数，也不需要法官名字与显示层入参"
        record = rt.active_record(agent)
        assert record is not None and record.session.started
    finally:
        rt.GameSession = real
        if _FakeSession.instances:
            _FakeSession.instances[-1].stop()
        rt._games.pop(agent.identifier, None)


def test_second_game_is_refused_and_leaves_the_first_one_alone():
    """一局一命：同一条会话第二次 `/werewolf` 只回一句提示，不收旧局、更不建第二个法官。

    第二个 `HostPresenter` 会在同一个 agent 上再挂一份 takeover 钩子，两份钩子都写
    `takeover_result`，后写的空串会把前一份的回复覆盖掉 —— 用户发话就全场没人应答。
    所以这里断言的是"什么都没发生"：不多一个 session、不停旧局。
    """
    rt = _ext()
    real = rt.GameSession
    _FakeSession.instances = []
    rt.GameSession = _FakeSession
    try:
        agent = FakeAgent(name="twice", display=object())
        rt.start_game(agent, [])
        first = _FakeSession.instances[-1]

        first.alive = True                             # 还在打
        rt.start_game(agent, [])
        assert len(_FakeSession.instances) == 1, "第二局不该被建出来"
        assert not first.stopped, "旧局不该被新命令停掉"
        assert "还在进行中" in " ".join(agent.messages)

        first.alive = False                            # 打完了
        rt.start_game(agent, [])
        assert len(_FakeSession.instances) == 1
        assert "新建会话" in " ".join(agent.messages), "重开的出口要写在提示里"

        rt.start_game(agent, ["stop"])
        assert first.stopped
        assert "终止" in " ".join(agent.messages)
    finally:
        rt.GameSession = real
        for instance in _FakeSession.instances:
            instance.stop()
        rt._games.pop(agent.identifier, None)


def test_the_command_surface_changes_the_moment_werewolf_runs():
    """`/werewolf` 一到手就换命令面，而且赶在后台线程起跑**之前**。

    两半都要钉住：
    - 不许太晚：收起以前写在 `play()` 里，于是得等五张开局卡片全点完 —— 中途弃卡（或向导报错）
      就永远不收，而 takeover 钩子是构造即挂、终身不摘的。
    - 不许太早：加载时不许有 `/auto-say`，一局没开始前它只会回一句「没轮到你」。
    """
    rt = _ext()
    from xun import Command

    class _SpySession(_FakeSession):
        """记下后台线程起跑那一刻的命令面 —— 收起必须已经发生。"""
        seen: dict = {}

        def start_in_background(self):
            type(self).seen = {"commands": set(self.agent.command.commands),
                               "disabled": list(self.agent.toolbox.disabled)}
            return super().start_in_background()

    real, instances = rt.GameSession, _FakeSession.instances
    _FakeSession.instances, _SpySession.seen = [], {}
    rt.GameSession = _SpySession
    try:
        agent = FakeAgent(name="surface", display=object())
        rt.setup_extension(FakeCtx(agent))
        agent.command.register(Command(name="tools", handler=lambda a: None, description="x"))
        assert set(agent.command.commands) == {rt.COMMAND_NAME, "tools"}

        rt.start_game(agent, [])                              # 开局卡片一张都还没问
        assert set(agent.command.commands) == {rt.COMMAND_NAME, rt.AUTO_COMMAND}, \
            "`/werewolf` 之后只剩法官这两条：自带命令摘掉，`/auto-say` 注册出来"
        assert "*" in _SpySession.seen["disabled"], "工具箱也在同一步收起"
        assert _SpySession.seen["commands"] == {rt.COMMAND_NAME, rt.AUTO_COMMAND}, \
            "收起得赶在后台线程起跑前：写回 play() 就得等开局卡片全点完"
        assert any("/auto-say" in text for text in agent.messages), "换了命令面要告诉用户一声"
    finally:
        rt.GameSession = real
        _FakeSession.instances = instances
        for instance in _SpySession.instances:
            instance.stop()
        rt._games.pop(agent.identifier, None)


def test_stop_then_status_still_answers():
    rt = _ext()
    real = rt.GameSession
    _FakeSession.instances = []
    rt.GameSession = _FakeSession
    try:
        agent = FakeAgent(name="qa", display=object())
        rt.start_game(agent, [])
        rt.start_game(agent, ["stop"])
        rt.start_game(agent, ["status"])
        joined = " ".join(agent.messages)
        assert _FakeSession.instances[-1].published == ["board"], "status 应该发一张看板卡"
        assert "终止" in joined, "终止后再问局面要说明这一局已终止"
    finally:
        rt.GameSession = real
        for instance in _FakeSession.instances:
            instance.stop()
        rt._games.pop(agent.identifier, None)


# --------------------------------------------------------------- 真运行时（不跑 LLM）
def _fake_session(rt):
    from .test_channels import _make_agent

    return rt.GameSession(_make_agent())


def test_game_session_wizard_reaches_a_playable_config():
    """开局向导是 extension 里唯一会建引擎、发卡片的地方，坏了就整局开不起来。"""
    rt = _ext()
    session = _fake_session(rt)
    asked: list[str] = []

    def fake_ask(question, choices, **kw):
        asked.append(kw.get("title") or question)
        return kw.get("default") or choices[0]

    session.host.ask = fake_ask
    session.host.confirm = lambda *a, **k: True
    try:
        config = session.wizard()
        assert config is not None, "向导没给出配置"
        assert sum(config.counts.values()) <= config.n_seats
        assert config.human_seat in config.seats and config.seed and config.rules_shown_in_setup
        # 开局设置全靠问：没有命令参数能跳过任何一步
        assert [t.split("：")[0] for t in asked] == [f"开局设置 · 第 {n} 步" for n in (1, 2, 3, 4)], asked
        card = session._setup_card(config)
        assert f"你的座位：{config.human_seat}号" in card.text(), card.text()
    finally:
        session.cleanup()
        assert not session.root_dir.exists(), "临时目录要清掉"


def test_every_setup_step_has_a_default():
    """玩家的快速路径 = 一路点默认值：每步默认值必须是第一个选项，且一路点下去真能开局。"""
    rt = _ext()
    session = _fake_session(rt)
    seen: list[str] = []

    def spy(question, choices, **kw):
        title = kw.get("title") or question
        seen.append(title)
        assert kw.get("default") == choices[0], (title, kw.get("default"), choices[0])
        return choices[0]

    session.host.ask = spy
    session.host.confirm = lambda *a, **k: True
    try:
        config = session.wizard()
        assert config is not None, "一路点默认值就该能开局"
        assert len(seen) == 4, seen          # 板子/座位/角色/微调，第 5 步是确认
        assert config.human_role is None, "默认身份随机"
        assert config.human_seat in config.seats
    finally:
        session.cleanup()


def test_declining_the_final_confirm_opens_nothing():
    rt = _ext()
    session = _fake_session(rt)
    session.host.ask = lambda question, choices, **kw: choices[0]
    session.host.confirm = lambda *a, **k: False
    try:
        assert session.wizard() is None, "玩家点取消就不该开局"
    finally:
        session.cleanup()


# --------------------------------------------------------------------------- 接管与临时目录
def test_session_takeover_is_built_in_and_never_released():
    """`HostPresenter(agent)` 构造即接管；`cleanup()` 只收临时目录，不交还会话。

    法官没有自己的工作目录（座位 agent 才有），所以要验的两件事是：钩子在构造时就挂上、
    局后这条会话仍然不进模型，以及座位 agent 的临时目录跟着 `cleanup()` 一起消失。
    """
    from .test_channels import _make_agent

    module = _ext()
    agent = _make_agent()
    session = module.GameSession(agent)
    root = session.root_dir
    assert len(agent.hooks.before_execution.fns) == 1, "构造即接管，只挂一份"
    assert agent.instruct("现在还有谁活着？").execute() != "", "接管中：这句话不该进模型"
    session.cleanup()
    assert agent.instruct("复盘一下 3 号").execute() != "", "一局一命：局后法官仍在"
    assert not root.exists(), "cleanup 之后不该在磁盘上留下这一局的目录"


def test_rule_tweaks_do_not_leak_into_the_shared_presets():
    """第 4 步的微调只属于这一局。板子是模块级共享对象，就地改会把规则留给下一局。

    一局一命只挡住了**同一条会话**开第二局；同一进程里另一个会话的法官读的还是
    同一份 PRESETS，被写过就白捡上一局改过的规则。
    """
    rt = _ext()
    from werewolf.engine.config import RuleFlags

    E = rt._load_engine()
    default_limit = RuleFlags().speech_word_limit
    before = [dict(vars(preset.config.flags)) for preset in E.PRESETS]
    assert default_limit != 800, "这道题的选项和默认值撞了，换一个"

    def run_wizard(tweak: str | None):
        session = _fake_session(rt)
        done = {"tweaked": False}

        def ask(question, choices, **kw):
            title = kw.get("title") or question
            if title.startswith("开局设置 · 第 4 步"):
                if tweak and not done["tweaked"]:
                    done["tweaked"] = True
                    return tweak
                return "用默认规则，继续"
            if "发言字数上限" in question:
                return "800 字"
            return kw.get("default") or choices[0]

        session.host.ask = ask
        session.host.confirm = lambda *a, **k: True
        try:
            return session.wizard()
        finally:
            session.cleanup()

    tuned = run_wizard("改发言字数上限")
    assert tuned.flags.speech_word_limit == 800, tuned.flags
    assert tuned is not E.PRESETS[0].config, "向导把共享的板子直接交出去了"

    plain = run_wizard(None)                       # 第二局：一路点默认值
    assert plain.flags.speech_word_limit == default_limit, \
        f"下一局继承了上一局的微调：{plain.flags.speech_word_limit} 字"
    for preset, snapshot in zip(E.PRESETS, before):
        assert dict(vars(preset.config.flags)) == snapshot, f"板子 {preset.id} 被就地改过"


def test_the_human_seat_gets_a_proxy_for_auto_say():
    """真人那个座位的演员必须带着 `/auto-say` 的替身工厂。

    没挂工厂时：法官答「这一句替你说」，`HumanActor` 却抛错、被引擎兜成默认行动 ——
    玩家写好按下的那句话被吞，场上只看到一句莫名的默认发言。
    """
    rt = _ext()
    from werewolf.engine.config import PRESETS

    session = _fake_session(rt)
    made: list[dict] = []

    class _StubLLM:
        def __init__(self, **kw) -> None:
            made.append(kw)

        def finalize(self) -> None:
            pass

    real = session.E.LLMActor
    session.E.LLMActor = _StubLLM
    try:
        cfg = PRESETS[2].config.copy()
        cfg.seed, cfg.human_seat = 3, 4
        engine = session.E.Engine(cfg, {}, presenter=session.host, seed=cfg.seed)
        engine.setup()
        actors = session.build_actors(engine.state, cfg, "test-model")

        assert set(actors) == set(cfg.seats)
        assert len(made) == len(cfg.seats) - 1, "除真人外每座一个 agent，且不该多建"
        human = actors[cfg.human_seat]
        factory = getattr(human, "_proxy_factory", None)
        assert factory is not None, "真人的替身没挂上：`/auto-say` 会吞掉那句发言"
        proxy = factory()                                  # 第一次 /auto-say 才真建 agent
        assert isinstance(proxy, _StubLLM) and len(made) == len(cfg.seats) - 1 + 1
        assert made[-1]["seat"] == cfg.human_seat, made[-1]
        assert made[-1]["role_id"] == engine.state.players[cfg.human_seat].role_id, \
            "替身要拿真人自己的身份与座位说话"
    finally:
        session.E.LLMActor = real
        session.cleanup()


class _ScriptedSeat:
    """脚本座位演员：让 `GameSession.play()` 能在测试里真跑完一局（不发一次 LLM 请求）。"""

    def __init__(self, seat=None, **kw) -> None:
        import random

        from werewolf.actors.fake import FakeActor

        self.seat, self.is_human = seat, False
        self.calls, self.failures = 0, 0
        self._fake = FakeActor(seat, random.Random(seat or 1))

    def decide(self, ask):
        self.calls += 1
        return self._fake.decide(ask)

    def finalize(self) -> None:
        pass


class _StubCoach:
    """假复盘教练：记住每次调用，好让我们看住「局末不 finalize，cleanup 才收」。"""

    instances: list["_StubCoach"] = []

    def __init__(self, **kw) -> None:
        self.reviews: list[object] = []
        self.questions: list[tuple[str, object]] = []
        self.finalized = False
        type(self).instances.append(self)

    def review(self, state):
        self.reviews.append(state)
        return "## 复盘报告\n\n第 3 夜那把刀决定了胜负。"

    def ask(self, question, state=None):
        self.questions.append((question, state))
        return "那天刀你，是因为警徽在你身上。"

    def finalize(self):
        self.finalized = True


def _play_a_scripted_game(rt) -> tuple:
    """真跑完一局（脚本座位、假教练）。返回 (session, coach, restore) —— 桩必须还回去，
    因为 `session.E` 是全进程那一份引擎命名空间，改它会漏给别的测试。"""
    from werewolf.engine.config import PRESETS

    session = _fake_session(rt)
    _StubCoach.instances = []
    saved = {name: getattr(session.E, name) for name in ("LLMActor", "Reviewer")}
    session.E.LLMActor, session.E.Reviewer = _ScriptedSeat, _StubCoach
    session.host.confirm = lambda *a, **k: True
    session.host.wait_text = lambda **k: "我先听后面的人怎么说。"
    session.agent.info = lambda text: None
    session.coach_tasks: list[bool] = []          # 复盘有没有**经队列**生成（见下面那条断言）
    real_task = session.host.coach_task
    session.host.coach_task = lambda work: (                     # type: ignore[method-assign]
        session.coach_tasks.append(callable(work)), real_task(work))[0]   # noqa: E501
    cfg = PRESETS[2].config.copy()
    cfg.seed, cfg.human_seat = 11, 4

    def restore() -> None:
        for name, value in saved.items():
            setattr(session.E, name, value)

    try:
        session.play(cfg)
    except BaseException:
        restore()
        raise
    return session, _StubCoach.instances[-1], restore


def test_after_the_game_the_session_turns_into_a_review_room():
    """局打完了这条会话变成复盘室：打字进教练，而且教练**还活着**。

    局末就把教练 finalize 掉，「讨论」会退化成「每次重新问一遍模型」—— 前面聊过的全没了。
    所以钉两件事：局末与 `release_players()` 都不许收它，`cleanup()` 才是收尾点。
    """
    rt = _ext()
    session, coach, restore = _play_a_scripted_game(rt)
    try:
        assert coach.reviews, "一局打完了却没生成复盘"
        assert session.coach_tasks == [True], \
            "复盘必须经 `coach_task` 队列生成：就地调就等于允许复盘期间有人并发用教练那个会话"
        assert session.host.game_over, "引擎都返回了，法官还说这局没结束"
        assert not coach.finalized, "复盘教练不许在局末被收掉：局后的讨论靠它的会话记忆"

        session.release_players()                 # 座位 agent 收掉了，教练不该跟着收
        assert not coach.finalized, "release_players 把教练一起收了"

        session.host.on_user_message(["第 2 天你们为什么刀我"])
        assert len(coach.questions) == 1, coach.questions
        question, state = coach.questions[0]
        assert question == "第 2 天你们为什么刀我"
        assert state is session.engine.state, "教练得拿到这一局的局面（复盘没生成也能答）"
    finally:
        restore()
        session.cleanup()
    assert coach.finalized, "cleanup 才是教练的收尾点"


def test_the_coach_workspace_survives_the_end_of_the_game():
    """教练的工作目录得活到讨论结束：它一被 rmtree，会话文件就没了，讨论变失忆。"""
    rt = _ext()
    session = _fake_session(rt)
    try:
        (session.root_dir / "review").mkdir(exist_ok=True)
        (session.root_dir / "review" / "conversation.json").write_text("{}")
        session.release_players()
        assert (session.root_dir / "review").exists(), "release_players 把教练的工作目录删了"
    finally:
        session.cleanup()
    assert not session.root_dir.exists(), "cleanup 还是要整目录收干净"


def test_a_stopped_game_can_still_be_discussed():
    """中途终止（engine 返回 aborted）也一样开讨论口子：判定不能只看 `state.finished`。"""
    rt = _ext()
    from werewolf.engine.config import PRESETS

    session = _fake_session(rt)
    _StubCoach.instances = []
    saved = {name: getattr(session.E, name) for name in ("Engine", "LLMActor", "Reviewer")}

    class _Aborts(saved["Engine"]):
        def run(self):
            return "aborted"                      # 等价于 `/werewolf stop` 之后的引擎返回

    session.E.Engine, session.E.LLMActor, session.E.Reviewer = _Aborts, _ScriptedSeat, _StubCoach
    session.host.confirm = lambda *a, **k: True
    session.agent.info = lambda text: None
    try:
        cfg = PRESETS[2].config.copy()
        cfg.seed, cfg.human_seat = 5, 3
        session.play(cfg)
        assert session.host.game_over, "终止局没开讨论口子"
        session.host.on_user_message(["最后那次投票我没看懂"])
        coach = _StubCoach.instances[-1]           # 教练是懒建的：第一次讨论才建
        assert coach.reviews == [], "终止局不该硬生成一份复盘"
        assert coach.questions, "终止局问不了刚才那几夜"
    finally:
        for name, value in saved.items():
            setattr(session.E, name, value)
        session.cleanup()


def test_a_finished_game_reports_stats_and_reaches_the_review():
    """一局**正常打完**之后那几步也得跑：统计要读引擎里那份演员表，复盘必须真出来。

    这里以前写的是 `actors.values()`（那个局部变量根本不存在），于是每一局都在引擎跑完、
    复盘之前抛 NameError，玩家看到的最后一条是「⚠️ 对局异常终止」，复盘永远出不来。
    `play()` 是整条流程里唯一没有测试覆盖的一段，所以它坏了其余 162 项测试还是全绿。
    """

    rt = _ext()
    from werewolf.engine.config import PRESETS
    from werewolf.ui.host import REVIEWER

    session = _fake_session(rt)

    # 注意这里**不**给 agent 补 toolbox / command：命令面归 `/werewolf` 那一刻接管（见
    # test_the_command_surface_changes_the_moment_werewolf_runs），`play()` 不许再去碰它。
    # 哪天有人把收起写回 play()，这里会直接 AttributeError —— 那是故意的。
    session.agent.info = lambda text: None      # 只挡住那句回执

    class _StubReviewer:
        def __init__(self, **kw):
            pass

        def review(self, state):
            return "复盘正文：第 3 夜那把刀决定了胜负。"

        def finalize(self):
            pass

    real_actor, real_reviewer = session.E.LLMActor, session.E.Reviewer
    session.E.LLMActor, session.E.Reviewer = _ScriptedSeat, _StubReviewer
    session.host.confirm = lambda *a, **k: True
    session.host.wait_text = lambda **k: "我先听后面的人怎么说。"
    try:
        cfg = PRESETS[2].config.copy()
        cfg.seed, cfg.human_seat = 11, 4
        session.play(cfg)                                  # 不许抛：抛了就等于玩家看到的崩局

        events = session.host.display.emitted
        texts = [(getattr(e.payload, "message", None) or getattr(e.payload, "title", "") or "")
                 for e in events]
        assert not any("对局异常终止" in text for text in texts), texts[-3:]
        reports = [e for e in events if e.name == "ModelMessageEvent" and e.agent.name == REVIEWER]
        assert reports, "一局打完了却没把复盘交给玩家"
        assert "复盘正文" in reports[-1].payload.content, reports[-1].payload.content

        stats = [text for text in texts if "AI 决策" in text]
        assert stats, f"一局打完了没有播报统计：{texts[-3:]}"
        first = stats[0].split("决策")[1].split("次")[0]
        calls = int("".join(ch for ch in first if ch.isdigit()) or 0)
        assert calls > 0, f"统计读到的演员表是空的：{stats[0]}"
    finally:
        session.E.LLMActor, session.E.Reviewer = real_actor, real_reviewer
        session.cleanup()


def test_custom_board_defaults_open_a_legal_game():
    """自定义板子那七张卡**一路点第一个选项**必须开得出合法的一局。

    约定是「每步第一个选项就是默认值」，可神职那几张卡的第一个选项是「0」：一路点提交
    拿到的是「8 人以上屠边 + 0 个神职」的板子，第 1 天法官就宣布狼人获胜。
    默认值现在排在第一位，并且按人数算过（一路点提交 = 9 人 3 狼 预女猎各 1）。
    """
    rt = _ext()
    gods = ("seer", "witch", "hunter", "idiot")

    for n_seats in range(5, 13):
        session = _fake_session(rt)
        asked: list = []

        def fake_ask(question, choices, **kw):            # 一路点第一个选项 = 默认值
            asked.append((question, list(choices)))
            if len(asked) > 7:        # 一轮就是七张卡：再问说明默认板子自己被判了不合法，
                raise AssertionError(    # 在来回重问的死循环里转圈（真人还能改答案，桩不能）
                    f"{n_seats} 人：一路点默认过不了校验，向导在原地重问 {notices}")
            return list(choices)[0]

        session.host.ask = fake_ask
        notices: list = []
        session.host.notice = lambda state, text: notices.append(text)
        try:
            config = session.custom_board()
            assert config is not None, f"{n_seats} 人：一路点默认没给出板子"
            config.validate()
            assert not notices, f"{n_seats} 人：一路点默认被判不合法 {notices}"
            assert sum(config.counts.values()) == config.n_seats, config.counts
            dealt_gods = sum(c for r, c in config.counts.items() if r in gods)
            if config.flags.win_condition == "edge":      # 屠边必须真发得出神职和村民
                assert dealt_gods >= 1 and config.counts.get("villager", 0) >= 1, config.counts
            assert asked, "一张卡都没问"
            if n_seats == 9:                              # 一路点默认拿到的就是那张标准局
                assert config.counts.get("wolf") == 3 and dealt_gods == 3, config.counts
        finally:
            session.cleanup()
