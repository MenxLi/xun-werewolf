"""狼人杀练习局 —— xun extension 入口，同时是「一局」的全部运行时。

装上之后在会话里发 `/werewolf` 就开局：1 个真人（你）+ 其余座位 AI，法官用卡片问你板子、
座位、角色、规则，然后自动天黑。xun 的发现规则是
`{XUN_HOME}/extensions/<name>/setup_extension.py`（`XUN_HOME` 未设时是 `./.xun`），
extension 的名字就是这个目录名，所以**本目录（`werewolf/`）就是那个 extension 目录**：
整目录拷贝或 symlink 成 `{XUN_HOME}/extensions/werewolf/` 即可（`pack.py` 打的包也是这一层）。
引擎子包（`engine/ actors/ ui/`）就在本文件旁边，因此引擎一律**相对导入**（见
`_load_engine()`）——xun 的 package 形态本就为此设计，不需要 `sys.path` 魔法。
代价是不再支持「只拷这一个文件」的装法：flat 形态 `extensions/{name}.py` 不支持相对导入。

两条纪律（有测试锁死，别破）：

1. **加载时只注册命令**：不 import 引擎、不起线程、不改 config、不注册 tool。引擎可能在
   缺依赖的机器上 import 失败，也不能拖慢 xun 启动 —— 这些都推迟到 `/werewolf` 真正执行时
   （见 `_load_engine()`）。**测试锁死了这条**（`test_no_side_effects_on_load`）。
2. **一个会话一局**：再发一次 `/werewolf` 就收尾上一局、换法官接班。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from xun import Command, extension_attr

COMMAND_NAME = "werewolf"
AUTO_COMMAND = "auto-say"
START_DESCRIPTION = "开一局狼人杀（1 真人 + N AI）；开局设置用卡片问你（每步都有默认值），" \
                    "/werewolf stop 终止，/werewolf status 看局面"
USAGE_LONG = """开局狼人杀：在当前会话里开一局，1 个真人（你）+ 其余座位是 AI。

命令就这几个，板子、座位、身份都在法官卡片里选：
  /werewolf            开一局。法官依次问：板子 → 座位 → 角色 → 规则微调 → 确认。
                       每一步的第一个选项就是默认值，一路点下去最快，不需要打任何参数
  /werewolf status     进行中的局面摘要（只给公开信息）
  /werewolf stop       终止进行中的一局（法官还在，可以继续问复盘）
  /werewolf ?          本段用法
  /auto-say            轮到你发言而你不想说：这一句法官替你说（只管这一次，不接管发言权）

开局后**这个会话的 agent 就是法官**（xun 的 takeover_result 让它结构上不可能调用模型）：
选择题点法官卡片，**发言与遗言直接发在这条会话的输入框里**，不用切会话、不用切 agent。
你发给法官的话不会传给任何玩家；法官也不会用模型回答，它只看公开局面。

**一局一会话**：打完之后这条会话就固定是法官（复盘随时问），不再改口重开 ——
要再开一局请点前端的「新建会话」，那才是真正把 agent 交回给你自己的方式。
**局打完了可以直接打字讨论**：法官把话转给复盘教练（它手里是那局的上帝视角记录，
含夜间行动），回答以「复盘教练」的气泡出现；`/werewolf status` 仍然秒回看板。
中途 `/werewolf stop` 也一样能问，只是记录只到中止为止。
发出 `/werewolf` 那一刻起，这个 agent 自己的工具箱与默认命令都收起来了（`/tools`、`/clear`
那些不再响应），只剩上面这几条 —— 本局你只用输入框发言，外加 `/auto-say` 让法官替你说一句。
`/auto-say` 也是那一刻才注册：一局没开始之前它无事可干，不该出现在补全菜单里。
"""

def _with_default(default: str, options: Sequence[str]) -> list[str]:
    """把默认值挪到第一个：卡片上「一路点提交」拿到的就是默认板子（与向导别处同一套约定）。"""
    return [default] + [option for option in options if option != default]


def default_hint(default: str) -> str:
    """xun 的 PromptCard 只会把默认值**预选中**（按钮描个边），不写「默认」两个字，
    所以这句话要我们自己写在卡片正文里。"""
    return f"默认「{default}」——不改就直接点「提交」。"


_games: dict[str, "_Record"] = {}
_lock = threading.Lock()

@extension_attr(min_api_version="1.3.0", max_api_version="1.3.0")
def setup_extension(ctx) -> None:
    """加载时唯一做的事：注册 `/werewolf` 这一条命令。

    `/auto-say` **不在这里**注册。一局都没开始之前，「法官替你说一句」根本无事可干，
    把它摆进补全菜单只会误导 —— 用户以为有个开关，其实它现在只会回一句「没轮到你」。
    所以本插件除 `/werewolf` 之外的命令一律等 `/werewolf` 真跑起来才注册，
    同时把 agent 自用的那些摘掉：见 `takeover_command_surface()`。
    """
    ctx.agent.command.register(Command(
        name=COMMAND_NAME,
        handler=start_game,
        description=START_DESCRIPTION,
        description_long=USAGE_LONG,
    ))


# --------------------------------------------------------------------------- 引擎懒加载
_ENGINE: Any = None


def _load_engine() -> Any:
    """第一次用到才 import 引擎，返回一个命名空间（引擎里统一写 `E.XXX`）。

    引擎子包就在本文件旁边，所以只有相对导入这一条路：xun 用 `xun_ext_werewolf` 包壳加载本
    文件时，`__package__` 是那个包壳；在仓库根 `import werewolf.setup_extension`（跑测试）时，
    它是 `werewolf`。身份跟着加载方式走，但**一个进程只走其中一条** —— 混用会出现两份引擎
    （各自独立的全局状态）。所以这里不退回绝对导入，也别往 sys.path 里塞东西。
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    from importlib import import_module
    from types import SimpleNamespace

    pkg = __package__          # 相对导入的锚点：xun 的包壳名，或 `werewolf`（测试从仓库根导入）

    def imp(module: str, attr: str | None = None):
        loaded = import_module(f".{module}", package=pkg)
        return loaded if attr is None else getattr(loaded, attr)

    try:
        config = imp("engine.config")
        roles = imp("engine.roles")
        presenter = imp("engine.presenter")
        host = imp("ui.host")
        _ENGINE = SimpleNamespace(
            Engine=imp("engine.engine", "Engine"), GameAborted=presenter.GameAborted,
            HumanActor=imp("actors.human", "HumanActor"), LLMActor=imp("actors.llm_player", "LLMActor"),
            Reviewer=imp("actors.reviewer", "Reviewer"),
            HostPresenter=host.HostPresenter, REVIEWER=host.REVIEWER,
            render=imp("ui.render"), H=imp("ui.html"), role_by_id=roles.role_by_id,
            GameConfig=config.GameConfig, RuleFlags=config.RuleFlags, LEVEL_LABELS=config.LEVEL_LABELS,
            WIN_EDGE=config.WIN_EDGE, WIN_CITY=config.WIN_CITY, PRESETS=config.PRESETS,
            preset_choices=config.preset_choices, parse_preset_choice=config.parse_preset_choice,
            ROLES=roles.ROLES, SEER=roles.SEER, WITCH=roles.WITCH, HUNTER=roles.HUNTER,
            IDIOT=roles.IDIOT, VILLAGER=roles.VILLAGER, WOLF=roles.WOLF, WOLF_KING=roles.WOLF_KING,
        )
    except Exception as exc:
        raise RuntimeError(_broken_install_hint(exc)) from exc
    return _ENGINE


def _broken_install_hint(exc: BaseException) -> str:
    """装错了要说清四件事：缺什么、入口现在在哪、能照抄的修法，以及 xun 太旧也是候选原因。"""
    return (
        "导入狼人杀引擎失败，这份 extension 不完整：引擎子包（`engine/ actors/ ui/ assets/`）"
        "必须和入口 `setup_extension.py` 在同一个 `werewolf/` 目录里，靠相对导入进来 —— "
        "装好后这里应当看得到 `extensions/werewolf/engine/engine.py`。\n\n"
        f"入口现在在：`{Path(__file__)}`\n"
        f"底层报错：`{type(exc).__name__}: {exc}`\n\n"
        "也可能是 **xun 太旧**：本插件按 xun 最新源码构建（1.3 起会话消息是类型化的消息类）—— "
        "先确认手头的 xun 已经更新到最新，再照下面的修法装。\n\n"
        "修法是**把整个 `werewolf/` 目录**放到 extension 位置（目录名叫 `werewolf`，别只拷入口）：\n"
        "  `cp -r <本仓库>/werewolf <XUN_HOME>/extensions/werewolf`\n"
        "  或 `unzip werewolf-<时间戳>.zip -d <XUN_HOME>/extensions/`"
    )


def _resolve_model_name() -> str:
    """没指定模型时，用 xun 配置里的模型；配置没写就问 provider 要第一个。"""
    import openai
    from xun.config import load_config

    cfg = load_config()
    if cfg.model.name:
        return cfg.model.name
    client = openai.OpenAI(base_url=cfg.provider.openai_base_url, api_key=cfg.provider.openai_api_key)
    models = client.models.list()
    if not models.data:
        raise RuntimeError("Provider 里没有可用模型")
    return models.data[0].id


# --------------------------------------------------------------------------- 参数
@dataclass
class Parsed:
    action: str = "start"                  # start | stop | status | help
    unknown: list[str] = field(default_factory=list)


#: 一局一命，所以没有「退出 / 重开」这类命令；开局设置也全在卡片里问，只剩两个辅助动作
_COMMANDS = {"stop": "stop", "停": "stop",
             "status": "status", "局面": "status",
             "?": "help", "help": "help"}


def parse_arguments(arguments: Sequence[str] | str | None) -> Parsed:
    """只认一个命令词：stop / status / ?；空参数 = 开一局（走开局设置）。命令词本身不进这里。"""
    raw = arguments.split() if isinstance(arguments, str) else list(arguments or [])
    tokens = [t for t in raw if t.strip()]
    parsed = Parsed()
    if not tokens:
        return parsed                        # 光秃秃的 /werewolf = 开一局，法官逐步问
    if len(tokens) > 1:
        parsed.unknown, parsed.action = tokens, "unknown"
        return parsed
    token = tokens[0].strip().lower()
    if token in _COMMANDS:
        parsed.action = _COMMANDS[token]
    else:
        parsed.action, parsed.unknown = "unknown", [tokens[0]]
    return parsed


#: 遗言规则：按钮文本 ↔ 引擎取值
LAST_WORDS_CHOICES = {"首夜死者有遗言，放逐者有遗言": "first_night_only",
                     "所有死者都有遗言": "all", "一律无遗言": "none"}

# --------------------------------------------------------------------------- 守卫与注册表
@dataclass
class _Record:
    """一个会话一局，且**永不删除**：局打完了还得靠它读 state 回答复盘。

    一局一命 ⇒ 这张表的上限就是「开过局的会话数」，不去淘汰，也不提供把 agent 交还的命令
    （交还的正规出口是前端「新建会话」）。
    """
    session: "GameSession"
    stopped: bool = False


def guard_reason(agent: Any) -> str | None:
    """不能开局时给出原因（子 agent / 没有界面）。"""
    name = getattr(agent, "name", "") or ""
    if "-child-" in name:
        return ("狼人杀要开在**主会话**里：子 agent 是干活的临时会话，"
                "它一结束你的局就没了。请在左边那个会话里再发一次。")
    display = getattr(agent, "display", None)
    if display is None:
        return "这个 agent 没有绑定显示界面，开不了局。"
    try:
        from xun.displays import NullDisplay

        if isinstance(display, NullDisplay):
            return "当前是无界面（NullDisplay）环境，法官卡片没人点，开不了局。"
    except Exception:                        # pragma: no cover
        pass
    return None


def active_record(agent: Any) -> _Record | None:
    with _lock:
        return _games.get(getattr(agent, "identifier", str(id(agent))))


# --------------------------------------------------------------------------- 一局
class GameSession:
    """一局：开局配置向导 → 建引擎与玩家 → 后台线程跑 → 收尾复盘。

    引擎相关的东西全部经 `E = _load_engine()` 取（见模块 docstring 的纪律 1）。
    """

    TEMPERATURE_BY_LEVEL = {"rookie": 1.0, "balanced": 0.9, "elite": 0.8}

    def __init__(self, agent: Any, model_name: str | None = None) -> None:
        import tempfile

        self.E = _load_engine()
        self.agent = agent
        self.model_name = model_name
        # 只放座位 agent 的工作目录与复盘产物。法官不再需要自己的工作目录 ——
        # 它就是 `agent` 本身，不新建 agent、不新建 workspace。
        self.root_dir = Path(tempfile.mkdtemp(prefix="werewolf-"))
        self.host = self.E.HostPresenter(agent)   # 构造即接管：用户发的话不进模型
        self.engine: Any = None
        self.thread: threading.Thread | None = None
        self.reviewer: Any = None               # 一局一个，建好就留着：局后的讨论要用它的会话
        self.finalized = False
        self.players_released = False

    # ---- 生命周期 ---------------------------------------------------------
    @property
    def alive(self) -> bool:
        """这一局还在不在跑（只用来决定 `/werewolf` 的拒绝文案怎么说）。"""
        return self.thread is not None and self.thread.is_alive()

    def start_in_background(self) -> "GameSession":
        self.thread = threading.Thread(target=self.run, name="werewolf-game", daemon=True)
        self.thread.start()
        return self

    def run(self) -> None:
        import traceback

        E = self.E
        host = self.host
        try:
            config = self.wizard()
            if config is not None:
                self.play(config)
        except E.GameAborted:
            host.notice(self.engine.state if self.engine else None, "已收到停止请求，本局终止。")
        except Exception as exc:                 # 任何异常都不该让会话静默死掉
            self.host.say(f"⚠️ **对局异常终止**：{type(exc).__name__}: {exc}\n\n"
                          f"```\n{traceback.format_exc(limit=6)}\n```")
        finally:
            self.release_players()

    def stop(self) -> None:
        self.host.stop()

    def release_players(self) -> None:
        """一局结束就回收座位 agent 与它们的工作目录；法官**故意留着**，玩家可以继续问复盘。

        原来只有下次 `/werewolf` 收尾旧局时才清理：座位 agent 会一直挂在会话里当幽灵参与者，
        /tmp/werewolf-XXXX 也没人删。

        复盘教练是**故意不收**的：局后的讨论要靠它的会话记忆，它的工作目录（`review/`）
        也得活着。收尾在 `cleanup()`。
        """
        import shutil

        if self.players_released:
            return
        self.players_released = True
        if self.engine is not None:
            for actor in self.engine.actors.values():
                try:
                    actor.finalize()
                except Exception:
                    pass
        for seat in (self.engine.state.players if self.engine else {}):
            shutil.rmtree(self.root_dir / f"seat-{seat}", ignore_errors=True)

    def cleanup(self) -> None:
        import shutil

        if self.finalized:
            return
        self.finalized = True
        self.release_players()
        if self.reviewer is not None:
            self.reviewer.finalize()          # 教练留到这时候才收：局后的讨论要用它
            self.reviewer = None
        self.host.stop()
        self.host.cleanup()
        shutil.rmtree(self.root_dir, ignore_errors=True)

    # ---- 开局配置向导 -----------------------------------------------------
    def _card(self, title: str, lines: list[str]) -> Any:
        E = self.E
        return E.H.Card(title=title, parts=[E.H.Log(lines, small=True)])

    def _setup_card(self, config: Any) -> Any:
        E = self.E
        role = "随机" if not config.human_role else E.role_by_id(config.human_role).name
        parts = E.render.setup_parts(config, config.human_seat, range(1, len(config.seats) + 1))
        parts.append(E.H.Log(E.render.plainify(config.describe()).splitlines(), small=True))
        parts.append(E.H.Log([f"你的座位：{config.human_seat}号 ｜ 身份：{role}"]))
        return E.H.Card(title="最终配置 · 确认后才开局", parts=parts)

    def preset_lines(self) -> list[str]:
        E = self.E
        return [
            f"{p.id} {p.config.n_seats}人 "
            f"{p.config.flags.win_short}：{p.config.role_names()} —— {p.blurb}"
            for p in E.PRESETS
        ]

    def _seeded(self, config: Any) -> Any:
        """发一个随机种子。GameConfig 是冻结的（引擎拿到配置后不会被半路改），所以只能 replace。"""
        import random

        return replace(config, seed=random.randrange(1, 2 ** 31 - 1))

    def _confirm(self, config: Any) -> Any:
        """最后一步：展示最终配置 → 确认 → 交给引擎。"""
        host = self.host
        host.card(self._setup_card(config))
        if not host.confirm("以上配置可以开局吗？", "点“确认”立即天黑。",
                            title="开局设置 · 第 5 步：确认开局"):
            host.say("好的，本局取消。想再开一局就再发一次 `/werewolf`。")
            return None
        return replace(config, rules_shown_in_setup=True)   # 规则已展示，引擎不必再播一遍

    def wizard(self) -> Any:
        """五步向导：板子 → 座位 → 角色 → 规则微调 → 确认。每步第一个选项就是默认值。"""
        E, host = self.E, self.host
        host.card(self._card("欢迎来到狼人杀练习局", [
            "这一局只有你一个真人，其余座位都是 AI；我会依次问你板子、座位、角色、规则。",
            "每一步都预选了一个默认值（卡片上会写明），不改动就点「提交」，一路点下去最快开局。",
            "轮到你发言、留遗言：想说的话**直接发在这条会话的输入框里**（就是你现在打字的地方）。",
            "其余选择（投票、夜里用刀用毒、举手、警徽）点法官卡片的按钮。",
            "平时问规则、问局面也算发言之外的事：法官只看公开信息回答，不用模型、不会泄密。",
            "想提前终止用 /werewolf stop，看局面用 /werewolf status。",
            "一局一会话：这局打完后这条会话就固定是法官，复盘随时问；要再开一局请新建会话。",
        ]))
        host.card(self._card("可选板子", self.preset_lines()))

        choices = E.preset_choices()
        chosen = E.parse_preset_choice(host.ask(
            "选一个板子（板子说明见上面的清单）", choices,
            title="开局设置 · 第 1 步：选板子", default=choices[0],
            message=default_hint(choices[0])))
        if chosen is None:                      # 没对上预设 → 自定义板子（要问 4 件事）
            config = self.custom_board()
            if config is None:
                return None
        else:
            # 板子是模块级共享对象。不 copy 就进第 4 步，就地微调会写进 PRESETS，
            # 于是同进程里的下一局（新会话、新法官）白捡上一局改过的规则。
            config = chosen.config.copy()

        config = replace(self._seeded(config), human_seat=self._ask_seat(config))
        role_answer = host.ask(
            "你想拿什么身份？",
            ["随机"] + sorted({E.role_by_id(rid).name for rid, n in config.counts.items() if n > 0}),
            title="开局设置 · 第 3 步：你的角色", default="随机",
            message="选身份最公平的是随机；指定身份会锁定你的角色（其余座位重新分配）。"
                    + default_hint("随机"))
        if role_answer != "随机":
            config = replace(config, human_role=next(
                rid for rid, role in E.ROLES.items() if role.name == role_answer))

        return self._confirm(self.tweaks(config))

    def _ask_seat(self, config: Any) -> int:
        """第 2 步：座位，默认随机。"""
        import random

        answer = self.host.ask("你想坐几号位？", ["随机"] + [f"{s}号" for s in config.seats],
                               title="开局设置 · 第 2 步：你的座位", default="随机",
                               message=default_hint("随机"))
        seat = random.choice(config.seats) if answer == "随机" else int(answer.replace("号", ""))
        self.host.say(f"座位：1-{len(config.seats)}，你坐 {seat} 号，其余座位都是 AI 玩家。")
        return seat

    def custom_board(self) -> Any:
        """自选板子：人数 → 狼数 → 狼王 → 神职 → 校验（不合法就重来）。

        每一步的**第一个选项就是默认值**（和向导别处一条约定，卡片上写明「默认…」）：一路点提交
        拿到的是 9 人 / 3 狼 / 预女猎各 1 / 其余村民。以前神职那几张卡的第一个选项是「0」，
        一路点到底拿到「屠边 + 0 个神职」的板子，第 1 天法官就直接宣布狼人获胜（神职已全部出局）。
        """
        E, host = self.E, self.host
        while True:
            seat_options = _with_default("9", [str(n) for n in range(5, 13)])
            seats_answer = host.ask("这局几个人？", seat_options + ["取消自定义"],
                                    title="自定义板子 · 人数", default=seat_options[0],
                                    message="5 到 12 人。" + default_hint(seat_options[0]))
            if "取消" in seats_answer:
                return None
            n_seats = int(seats_answer)
            wolf_options = _with_default(str(max(1, n_seats // 3)),
                                         [str(n) for n in range(1, max(2, n_seats // 2))])
            n_wolves = int(host.ask(
                f"{n_seats} 人局里有几匹狼？", wolf_options,
                title="自定义板子 · 狼数", default=wolf_options[0],
                message=f"好人要有 {n_seats // 2} 人以上才打得动。" + default_hint(wolf_options[0])))
            king = 1 if host.ask("狼人阵营要不要狼王？", ["不要狼王", "1 匹狼王"],
                                 title="自定义板子 · 狼王", default="不要狼王",
                                 message="狼王死亡时可以开枪带人（被毒不能开枪）。"
                                         + default_hint("不要狼王")).startswith("1") else 0
            counts: dict[str, int] = {E.WOLF_KING: min(king, n_wolves)} if king else {}
            counts[E.WOLF] = n_wolves - counts.get(E.WOLF_KING, 0)
            for rid in (E.SEER, E.WITCH, E.HUNTER, E.IDIOT):
                role = E.role_by_id(rid)
                room = max(0, n_seats - sum(counts.values()) - 1)      # 至少留 1 个村民，屠边才判得动
                options = _with_default("1" if (room and rid != E.IDIOT) else "0",
                                        [str(n) for n in range(0, min(3, room) + 1)])
                counts[rid] = int(host.ask(f"{role.name} 要几个？", options,
                                           title="自定义板子 · 神职", default=options[0],
                                           message=role.public_prompt + default_hint(options[0])))
            gods = sum(counts.get(r, 0) for r in (E.SEER, E.WITCH, E.HUNTER, E.IDIOT))
            config = E.GameConfig(
                label="自定义板子", n_seats=n_seats,
                counts={**counts, E.VILLAGER: max(n_seats - n_wolves - gods, 0)},
                flags=E.RuleFlags(win_condition=E.WIN_EDGE if n_seats >= 8 else E.WIN_CITY))
            try:
                config.validate()
            except ValueError as exc:
                host.notice(None, f"这个板子不合法：{exc}。我们重来一次。")
                continue
            host.card(self._card("你的自定义板子", E.render.plainify(config.describe()).splitlines()))
            return config

    def tweaks(self, config: Any) -> Any:
        """第 4 步：规则微调。一次改一项，改完回到这张卡；默认值就是当前值，选「用默认规则」就收。"""
        E, host = self.E, self.host
        while True:
            choice = host.ask(
                "规则要不要微调？",
                ["用默认规则，继续", "改胜负判定", "改遗言规则", "改女巫能不能自救",
                 "改发言字数上限", "改 AI 水平", "改警徽票权", "改警长竞选开不开",
                 "改出局后的观战视角"],
                title="开局设置 · 第 4 步：规则微调", default="用默认规则，继续",
                message=default_hint("用默认规则，继续") + "\n"
                        + "默认：" + "；".join(config.flags.summary_lines()[:3]) + "……")
            if choice.startswith("用默认规则"):
                return config
            flags = config.flags
            if choice.startswith("改胜负判定"):
                flags.win_condition = (E.WIN_EDGE if host.ask("胜负判定用哪种？", ["屠边", "屠城"],
                                                              title="规则微调",
                                                              default=flags.win_short) == "屠边"
                                       else E.WIN_CITY)
            elif choice.startswith("改遗言"):
                value = host.ask("遗言规则？", list(LAST_WORDS_CHOICES), title="规则微调",
                                 default=next(k for k, v in LAST_WORDS_CHOICES.items()
                                              if v == flags.last_words))
                flags.last_words = LAST_WORDS_CHOICES[value]
            elif choice.startswith("改女巫"):
                flags.witch_self_save = host.ask(
                    "女巫解药能不能自救？", ["不能自救", "可以自救"], title="规则微调",
                    default="可以自救" if flags.witch_self_save else "不能自救").startswith("可以")
            elif choice.startswith("改发言"):
                value = host.ask("普通发言字数上限？", ["100 字", "200 字", "500 字", "800 字"],
                                 title="规则微调", subtitle="警上发言上限同步跟随",
                                 default=f"{flags.speech_word_limit} 字")
                flags.speech_word_limit = flags.sheriff_word_limit = int(value.split(" ")[0])
            elif choice.startswith("改 AI"):
                value = host.ask("AI 玩家的水平？", ["新手", "均衡", "高手"], title="规则微调",
                                 default=E.LEVEL_LABELS[flags.level],
                                 message="影响他们的发言质量与犯错概率。")
                flags.level = next(k for k, v in E.LEVEL_LABELS.items() if v == value)
            elif choice.startswith("改警徽"):
                flags.sheriff_vote_weight = float(
                    host.ask("警徽票权？", ["1 票", "1.5 票", "2 票"], title="规则微调",
                             subtitle="1 票等于警长没有加成",
                             default=f"{flags.sheriff_vote_weight:g} 票").split(" ")[0])
            elif choice.startswith("改警长"):
                current = "要警长竞选" if flags.sheriff_enabled else "不要警长竞选"
                flags.sheriff_enabled = host.ask(
                    "这局要不要警长竞选？", ["要警长竞选", "不要警长竞选"], title="规则微调",
                    default=current,
                    message="警长决定每天往哪个方向发言，并且天然压轴归票。"
                            + default_hint(current)).startswith("要")
            elif choice.startswith("改出局"):
                flags.spectator_god_view = host.ask(
                    "你出局之后要不要开上帝视角？", ["开上帝视角", "只看公开信息"], title="规则微调",
                    default="开上帝视角" if flags.spectator_god_view else "只看公开信息",
                    message="上帝视角能看到所有身份和夜间行动，复盘更快；只看公开信息更接近实盘。",
                ).startswith("开")

    # ---- 跑局 -------------------------------------------------------------
    def build_actors(self, state: Any, config: Any, model: str) -> dict[int, Any]:
        """每个 AI 座位一个 agent；真人座位是卡片演员，另外给他挂一个**代说**的替身。

        替身是懒建的（第一次 `/auto-say` 才真去建 agent），但工厂必须开局就挂上：
        没挂的时候法官答「这一句替你说」，`HumanActor` 只能抛错、被引擎兜成默认行动 ——
        那句发言就这么被吞了（而且场上只会看到一句默认行动，谁也不知道为什么）。
        """
        actors: dict[int, Any] = {}
        for seat, player in state.players.items():
            persona = state.personas.get(seat)
            if seat == config.human_seat:
                actors[seat] = self.E.HumanActor(
                    seat, self.host,
                    proxy_factory=lambda s=seat, p=player, per=persona: self._seat_agent(
                        s, p.role_id, config, model, per))   # 座位必须绑成默认值：闭包捕获循环变量会拿到最后一座
            else:
                actors[seat] = self._seat_agent(seat, player.role_id, config, model, persona)
        return actors

    def _seat_agent(self, seat: int, role_id: str, config: Any, model: str, persona: Any) -> Any:
        """一个座位 agent。真人的替身也走这条路：发言提示词与合法性校验只有一份。"""
        return self.E.LLMActor(
            seat=seat, role_id=role_id, game_config=config,
            persona_style=persona.style if persona else "稳健、按局面行事。",
            model_name=model,
            temperature=self.TEMPERATURE_BY_LEVEL.get(config.flags.level, 0.9),
            workdir=self.root_dir / f"seat-{seat}")

    def _coach(self) -> Any:
        """复盘教练：建一次就留着（讨论靠它的会话记忆，重建等于擦掉前面聊过的）。"""
        if self.reviewer is None:
            self.reviewer = self.E.Reviewer(model_name=self.model_name or _resolve_model_name(),
                                            workdir=self.root_dir / "review")
        return self.reviewer

    def discuss_with_coach(self, question: str) -> str:
        """局后用户说的话都到这儿（法官只做转发）。"""
        return self._coach().ask(question, self.engine.state)

    def play(self, config: Any) -> None:
        E, host = self.E, self.host
        model = self.model_name or _resolve_model_name()
        self.model_name = model                  # 教练是懒建的，到时候要用同一个模型
        # 教练在 `close_game()` 之前就挂好：复盘还在生时用户就能打字，那句话会排进队列
        host.coach = self.discuss_with_coach
        engine = E.Engine(config, {}, presenter=host, seed=config.seed)
        engine.setup()
        self.engine = engine
        state = engine.state
        engine.actors = self.build_actors(state, config, model)

        host.card(E.render.role_card_obj(state, config.human_seat))
        # 身份牌一局只发一次，这里再复述一遍：认错身份的一局就白打了
        me = state.players[config.human_seat]
        if not host.confirm(f"记住这张身份牌：{config.human_seat} 号 · {me.role_name}。准备好了就天黑。",
                            "确认后进入第 1 夜。", title="准备开局"):
            return

        host.info("开局。这条会话的 agent 已是法官：工具箱与自带命令都收着，"
                  "查局面 `/werewolf status`，这一句不想说 `/auto-say`。")
        if engine.run() == "aborted":
            # 中途终止的局也想问刚才那几夜，所以照样开讨论口子（复盘生成失败也照样能问）
            host.close_game()
            host.info("这局中止了。想问刚才那段就直接打字 —— 复盘教练手里有到中止为止的"
                      "整局记录（含夜间行动）。")
            return
        host.close_game()                              # 从现在起，输入框里的话进复盘教练
        calls = sum(getattr(a, "calls", 0) for a in engine.actors.values())
        failures = sum(getattr(a, "failures", 0) for a in engine.actors.values())
        host.info(f"本局共 {state.day} 天，AI 决策 {calls} 次（其中 {failures} 次需要重试）。")
        # 复盘走讨论那条队列（生成失败由队列兜，之后照样能问）：见 `HostPresenter.coach_task`
        host.coach_task(lambda: self._coach().review(state))
        host.info("接下来这条会话就是复盘室：直接打字问教练（它看得到整局记录，"
                  "含夜间行动），想重看局面用 `/werewolf status`。")


# --------------------------------------------------------------------------- 命令入口
def start_game(agent: Any, arguments: Sequence[str] | None = None) -> None:
    """`/werewolf` 的 handler。命令立即返回，一局跑在后台线程里。"""
    parsed = parse_arguments(arguments)

    if parsed.action == "help":
        agent.info(USAGE_LONG)
        return
    if parsed.unknown:
        agent.info("没看懂这些参数：" + "、".join(parsed.unknown) + "\n\n" + USAGE_LONG)
        return
    if parsed.action == "status":
        _status(agent)
        return
    if parsed.action == "stop":
        _stop(agent)
        return

    reason = guard_reason(agent)
    if reason:
        agent.info(reason)
        return
    try:
        _load_engine()                                 # 懒加载引擎；装错了这里给出可操作指引
    except Exception as exc:
        agent.info(str(exc))
        return

    agent_key = getattr(agent, "identifier", str(id(agent)))
    with _lock:
        previous = _games.get(agent_key)
    if previous is not None:
        # 一局一命：法官的接管摘不掉（xun 的 HookRegistry 没有 remove，我们也决定不交还），
        # 所以同一条会话上不许出现第二个法官 —— 第二个 HostPresenter 会在同一个 agent 上再挂
        # 一份钩子，两份钩子都写 takeover_result，用户发的话就没人答了。
        if previous.session.alive:
            agent.info("这局还在进行中。要终止用 `/werewolf stop`，看局面用 `/werewolf status`。")
        else:
            agent.info("这个会话已经打过一局了，法官不改口重开 —— 复盘和局面随时问它。\n\n"
                       "要再开一局：点前端的「新建会话」，那才是把 agent 交回给你自己的方式。")
        return

    session = GameSession(agent, model_name=None)
    with _lock:
        _games[agent_key] = _Record(session=session)
    # 命令面在这一刻换人：注册 `/auto-say`、摘掉 agent 自用的命令、收起工具箱。
    # 放在登记之后 —— 不然 `/auto-say` 已经能点，它的 handler 却还查不到这一局，
    # 只会回「当前会话没有狼人杀」。也不放在 `play()` 里：那要等五张开局卡片全点完，
    # 中途弃卡就永远不收，而接管可是终身的。
    takeover_command_surface(agent)
    session.start_in_background()

    # 只回执一句：怎么用交给法官的欢迎卡（这里以前抄了一份，命令改了就成了过期文案）
    agent.info("狼人杀开始了：这个会话从现在起由法官接管 —— 开局设置一张张卡片问你，"
               "发言直接发在输入框里。这个 agent 自用的命令与工具箱已收起，"
               "新注册出来的 `/auto-say` 是让法官替你说一句。"
               "一局一会话：打完了它还在，复盘随时问。")


def takeover_command_surface(agent: Any) -> None:
    """`/werewolf` 一到手就把这条会话的命令面换成法官那两条：注册 `/auto-say`、
    摘掉其余一切命令、收起工具箱。这条会话已被法官接管，agent 自用的那些一律用不上。

    工具：`ToolBox.disable("*")`（`toolbox.py:135`），`list_tools` 随即不再把它们交给模型，
      下一次请求自然生效。xun 没有"注销工具"的 API，也不需要还原 —— 接管是终身的（一局一命），
      要拿回自己的 agent 请新建会话。
    命令：`CommandRegistry` 只有公开的 `commands` 字典（`command.py:101`），没有注销/隐藏 API，
      所以把不属于本插件的直接摘掉。`/help` 由 registry 现造（`command.py:121-131`），摘掉的那些
      它也不再列出来；`/api/commands/<agent_id>` 读的就是这个活的字典，摘完立刻少几条（实测过）。
      但**前端不会被通知「命令表变了」**：xun 没有这类播报事件，网页只在切 agent/会话时拉一次，
      外加输入框里是一个光秃秃的 `/命令` 时才防抖重拉。所以补全菜单可能还挂着几条已经死掉的命令，
      那是前端的陈旧快照而不是这里没摘掉 —— 点上去只会得到 `Unknown command`。
    """
    agent.toolbox.disable("*")
    if AUTO_COMMAND not in agent.command.commands:
        agent.command.register(Command(
            name=AUTO_COMMAND,
            handler=auto_say,
            description="这一句法官替你说（只管一次，用完即失效；轮到你发言时用）",
        ))
    keep = {COMMAND_NAME, AUTO_COMMAND}
    for name in [n for n in agent.command.commands if n not in keep]:
        agent.command.commands.pop(name)


def _stop(agent: Any) -> None:
    record = active_record(agent)
    if record is None:
        agent.info("当前会话没有狼人杀。用 `/werewolf` 开一局。")
        return
    record.stopped = True
    try:
        record.session.stop()
    except Exception as exc:                           # pragma: no cover
        agent.info(f"终止时出了点问题：`{type(exc).__name__}: {exc}`")
        return
    agent.info("这一局已终止。法官还留在这条会话里，想问刚才那局的复盘随时问它。")


def auto_say(agent: Any, arguments: Sequence[str] | None = None) -> None:
    """`/auto-say` 的 handler：轮到你说话又不想说时，这一句交给法官（模型）替你说。

    只管这一次：不接管发言权，也不影响你的下一句。得正等着一句话时才有用。
    """
    if arguments:
        agent.info("`/auto-say` 不带参数。")
        return
    record = active_record(agent)
    if record is None:
        agent.info("当前会话没有狼人杀。用 `/werewolf` 开一局。")
        return
    if record.session.host.request_auto_speech():
        agent.info("好，这一句法官替你说；下一句还是你自己说。")
    else:
        agent.info("现在没轮到你说话。`/auto-say` 只管一次：等你发言（含警上发言、遗言）的时候用。")


def _status(agent: Any) -> None:
    record = active_record(agent)
    if record is None:
        agent.info("当前会话没有狼人杀。用 `/werewolf` 开一局。")
        return
    host = record.session.host
    try:
        host.publish_status()                          # 一张看板卡（不含未公开信息）
        if record.stopped:
            agent.info("这一局已经终止了，上面是终局局面。")
    except Exception as exc:                           # pragma: no cover
        agent.info(f"读不到局面：`{type(exc).__name__}: {exc}`（这一局已经终止了）"
                   if record.stopped else f"读不到局面：`{type(exc).__name__}: {exc}`")
