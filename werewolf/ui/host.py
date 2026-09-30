"""法官（HostPresenter）：整局零 LLM 调用，只做"事件 → 前端通道"的分发。

法官**不新建 agent**：它把当前会话的 agent 接管过来 —— 注册一个 `before_execution` 钩子，
把 `takeover_result` 填上，xun 的 execution loop 就在进模型之前直接返回（xun/hooks.py）。
于是「法官不使用模型」不是纪律而是结构：它手里没有模型，只有公开局面，用户怎么话术都
套不出私密信息；用户在输入框里打的字全部由 `on_user_message` 接走（发言进游戏，其余走
`ui/answers.py` 的规则式回答）。接管是**终身**的：一局一命，局打完了法官仍在，
把 agent 交还给用户的正规出口是前端「新建会话」，不是一条命令。

三条通道（前端渲染规则决定了为什么这么分）：

1. **法官一律发 InfoEvent**（前端是左侧竖线的灰色小字，无 token 徽标、不占"发言"气泡）。
   前端的 plain-text 不渲染 markdown，所以法官文本在 `ui/render.py` 里就是紧凑纯文本。
2. **游戏者的话直接 emit ModelMessageEvent**，作者是那个座位（`display.on_event` 手工发）——
   前端渲染成正常字号的聊天气泡、markdown 生效，读起来就是一群人吵架，而不是一片灰色系统日志。
3. **玩家的发言走输入框，不走卡片**：`wait_text()` 播一张"轮到你"的卡，然后等用户在这条
   会话的输入框里发的那句话（发言是长文本，卡在输入框里打字比在卡片的小输入框里舒服）。
"""
from __future__ import annotations

import functools
import re
import contextlib
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from xun.display_abstract import (
    AgentInfo, DisplayEvent, InfoEvent, ModelMessageEvent, ModelWorkingEvent, WarningEvent,
)
from xun.types import CancelledError

from ..engine.config import WIN_LABEL
from ..engine.events import Event
from ..engine.presenter import GameAborted
from ..engine.state import GameState
from . import answers, render
from . import html as H

REVIEWER = "复盘教练"


@dataclass
class _Wait:
    """某个座位「正在想」的状态：预定的气泡作者 + `model_call_id`。

    为什么预定 call_id：前端按 `model_call_id` 归并气泡，**思考窗口和它最终说出的话必须
    共用一个 id**，否则一个人的一句话会被拆成两段。放一个对象里，也免得「作者 → 三个字典」
    需要同步（以前漏掉一个字典就会出现幽灵状态）。
    """

    info: AgentInfo
    call_id: str


#: 补刀 cancel_event 的间隔与上限（见 `_poke_until_cards_close`）
POKE_SEC = 0.2
POKE_DEADLINE_SEC = 5.0


def _synchronized(fn):
    """把「攒块 + 发事件」串到一把锁里 —— 见 `HostPresenter._lock` 上的说明。

    只给碰 `_pending` / `_waiting` 的方法用。**等待用户输入的 `ask()` / `wait_text()`
    不加**：那一等可能是几分钟，不能让一条播报堵住整局。
    """
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapped


def _coalesce(items: Sequence[Any]) -> list[H.Part]:
    """攒下的（文本行 / 看板 Part）→ Parts：连续的文本行并成一个 Log，顺序不变。"""
    parts: list[H.Part] = []
    buffer: list[str] = []
    for item in items:
        if isinstance(item, H.Part):
            if buffer:
                parts.append(H.Log(buffer[:]))
                buffer.clear()
            parts.append(item)
        elif str(item).strip():
            buffer.append(str(item))
    if buffer:
        parts.append(H.Log(buffer))
    return parts


def _text_of(message: Any) -> str:
    """一条 chat message 里的纯文本（content 可能是字符串，也可能是带 image_url 的分块）。"""
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def take_texts(agent: Any) -> list[str]:
    """把刚进来的用户消息从对话里**取走**交给法官，返回原文。

    取走是为了"这批已经消费掉了"：用户在等发言时连发两句，下次再读还会读到同一句，
    不记游标就分不清哪句是新话。摘走天然记了游标（inclusive 连后面的一起）。前端的历史是
    事件流、不受影响，用户照样看得见自己说过什么；会话已经不调模型，留着也没人读。
    """
    popped = agent.conversation.pop_from_last_user_message(inclusive=True) or []
    return [text for text in (_text_of(message) for message in popped) if text.strip()]


class HostPresenter:
    """把 GameState 的变化翻译成前端能读的东西。`agent` 是被接管的那个 agent（会话主 agent）。"""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.display = agent.display
        self.state: GameState | None = None
        self.stop_event = threading.Event()
        # 接管是终身的：xun 的 HookRegistry 没有 remove，而我们根本不打算交还 ——
        # 开局那一刻起，这个 agent 的每一次执行都由法官回答（`execute` 永不进模型）。
        # 想把这个 agent 还回正常对话，唯一的出口是前端的「新建会话」。
        # 所以这里只在构造时挂一次钩子；一局一命也保证了同一个 agent 上不会挂第二份
        # （两份钩子都会写 `takeover_result`，后写的那份会把前一份的回复覆盖成空）。
        agent.hooks.before_execution.add(self._takeover)
        # 两个线程会同时往这里攒：游戏线程（phase / on_events / _flush）与命令线程
        # （`/werewolf status` → publish_status → card → _flush）。攒块与发事件都在锁里，
        # 否则两边各攒一半，前端就会看到半截块、重复块或错接到别的阶段。
        self._lock = threading.RLock()
        # 用户在输入框发的话从这里交给正在等它的人（发言）。**不能复用 _lock**：
        # 等发言可以等几分钟，播报会被一起锁死。
        self._input = threading.Condition()
        self._inbox: list[str] = []
        self._awaiting = 0                    # 有几个环节正在等用户输入
        self._auto = False                    # `/auto-say`：这一次发言交给法官（模型）替说
        self._authored: str | None = None     # 法官代说那句的气泡作者，一次性的放行标记
        self._cards = 0                       # 有几张卡片正挂在前端（stop 时要唤醒它们）
        # ("", 0, 0) = 还没发过局面摘要；换天 / 换昼夜时强制重发一次
        self._status_signature = ("", 0, 0)
        # 攒着一次播报，flush 时合成**一个块**。元素是纯文本行或 `html.Part`（看板），
        # 连续文本行在 flush 时并成一个 Log part，顺序不变。
        self._pending: list[Any] = []
        self._pending_title = ""               # 非空 = 本块是阶段头（文本形态是 ━━ 行）
        self._phase_title = ""                 # 当前阶段名：给被气泡切断的后续块当标题
        self._waiting: dict[str, _Wait] = {}   # 作者 → 还在等它发言/在算的状态
        self._opened: dict[int, str] = {}      # seat → 打招呼用的作者标签（`thought` 据此关窗口）
        # 包着「有 AI 座位在算」那段等待的执行态范围；None = 法官现在看起来是闲的
        self._run_stack: contextlib.ExitStack | None = None
        self._day_seen = 0
        self._chunks = 0
        self.self_info = AgentInfo.from_agent(agent)   # 法官自己的名字/工作目录（造气泡要用）

    # ---------------------------------------------------------------- 接管
    def _takeover(self, args: Any) -> None:
        """`before_execution` 钩子：这一次执行由法官回答，绝不进模型。

        `schema` 不为 None 时 xun 会把 takeover_result 按 schema 校验（seat 决策都带 schema，
        但它们是自己 new 出来的 agent，压根没注册这个钩子）——这里照样接管并回一句哑话，
        宁可不回答也不让模型掺进这一局。
        """
        texts = [] if args.schema is not None else take_texts(args.agent)
        args.takeover_result = self.on_user_message(texts)

    @_synchronized
    def on_user_message(self, texts: Sequence[str]) -> str:
        """用户在输入框发的话：**轮到真人发言时第一条一律算那句发言**，其余按向法官提问回答。

        为什么不猜意图（比如「以「法官」开头就不当发言」）：猜错的两种代价都不可接受 ——
        把真人写好的一句话吞掉，或者把他随口的问题当成发言播给全场。所以规则只有一条、可预期：
        **输入框就是当前那件事的答案**（该发言时是发言，没轮到你时是提问）；想随时查局面用
        `/werewolf status`（`execute_command` 不走 execution loop，永远不会被当成发言吞掉）。

        返回值就是这次执行的"回答"（xun 直接拿它当 execute() 的返回值，不渲染）；给用户
        看的字一律走 `_info` —— 官方前端并不显示 execute() 的返回值。
        """
        rest = [text for text in texts if text and text.strip()]
        if not rest:
            return ""
        with self._input:
            if self._awaiting > 0:
                self._inbox.append(rest.pop(0))
                self._input.notify_all()
        if not rest:
            return ""                         # 纯发言：交给引擎播报，法官不回话
        reply = answers.answer(self.state, self.god_view, rest)
        self._info(reply)
        return reply

    # ---------------------------------------------------------------- 通道原语
    @_synchronized
    def _info(self, text: str) -> None:
        if text and text.strip():
            self.agent.display_event(InfoEvent(message=text.strip("\n")))

    def _sync_running(self) -> None:
        """把「有 AI 座位在算、且没在等真人」这段等待包进 agent 的执行态。

        xun 在 `cancellable_execution` 最外层进出时发 `AgentRunningStartEvent` /
        `AgentRunningEndEvent`，前端据此点亮「运行中」（侧栏徽章、活动条转圈、底部光谱线）。
        不包的话，游戏线程再怎么发事件，前端都认定这个 agent 闲着 —— 等 AI 输出的那几秒
        界面看着就像卡死（`ModelWorkingEvent` 救不了：活动条的 working 只认 runningAgents）。

        **等真人的时候必须松开**：`_cards`/`_awaiting` 不为零那段时间用户要能打字提问，
        而前端处于运行态时把「发送」换成「停止」（App.vue 直接拒发），卡片倒是照样能点
        （答复走 WS 的 `_pending.respond`，不经过执行循环）。
        """
        want = bool(self._waiting) and not self._cards and self._awaiting <= 0
        with self._lock:
            if want == (self._run_stack is not None):
                return
            if want:
                stack = contextlib.ExitStack()
                stack.enter_context(self.agent.cancellable_execution())
                self._run_stack = stack
            else:
                stack, self._run_stack = self._run_stack, None
                try:
                    stack.close()
                except CancelledError:
                    # 用户按了「停止」：CM 退出时发现被取消会抛这个，停局另有其人
                    # （`stop_event` + 唤醒挂着的卡片），这里不接这个话
                    pass

    def info(self, text: str) -> None:
        """系统提示（一行小字）。"""
        self._info(render.plainify(text))

    @_synchronized
    def notice(self, state: GameState | None, text: str) -> None:
        if state is not None:
            self.state = state
        self.agent.display_event(WarningEvent(message=render.plainify(text)))

    def say(self, text: str) -> None:
        """法官播报。法官不"发言"：所有内容都走 info（旧调用点的 markdown 会被 plainify）。"""
        self._flush()
        self._info(render.plainify(text))

    @_synchronized
    def thinking(self, state: GameState, seat: int, kind: str) -> None:
        """引擎打招呼：某座位要说活了（只有 AI 会走到这儿，人类是输入框发言）。

        两个效果：文本形态打一行「running」，网页形态把这段等待包进执行态（`_sync_running`），
        前端于是显示「运行中」。不占屏幕、不等超时。
        """
        author = render.speech_author(seat, kind)
        # 同座位换环节时，上一环节漏关的窗口必须收尾：新标签一覆盖 `_opened[seat]`，
        # 旧标签再没人关，那个僵尸窗口会一直撑着执行态 —— 「运行中」从此永远亮着说谎。
        # engine 串行提问、并发组内座位唯一，所以走到这里的旧窗口必然是上一轮的残留。
        stale = self._opened.get(seat)
        if stale is not None and stale != author:
            self._waiting.pop(stale, None)   # 标签没变就别拆了重建：那是同一个窗口
        self._opened[seat] = author        # 记住标签，`thought` 据此关窗口
        self.thinking_author(author)

    @_synchronized
    def thought(self, state: GameState, seat: int) -> None:
        """某座位的提问收尾：关掉它的活动窗口。

        对所有提问一视同仁：发言那一句在 `on_events` 里播成玩家气泡时已经被 `say_as` 关过，
        这里再关一次是幂等的（见 `Engine._collected`）。作者标签取 `thinking` 当时记下的
        那个，不在这里重算 —— 重算就得猜展示标签。
        """
        author = self._opened.pop(seat, None)
        if author:
            self._waiting.pop(author, None)
        self._sync_running()

    @_synchronized
    def thinking_author(self, author: str) -> None:
        """某座位开始思考：登记它的等待窗口，并把这件事告诉显示层。

        这条 `ModelWorkingEvent` 管的是**文本形态**（终端会打一行「🟢 某某 running」）；
        作者用手工造的 `ww-*` 标签无所谓。网页形态的活动指示不看它 —— 那由执行态决定，
        见 `_sync_running`。

        同一个作者重复打招呼不叠加，只发一个窗口。
        """
        if author in self._waiting:
            return
        call_id = f"{author}-{uuid.uuid4().hex[:8]}"
        self._waiting[author] = _Wait(
            info=AgentInfo(name=author, identifier=f"ww-{author}",
                           workdir=self.self_info.workdir),
            call_id=call_id)
        self.agent.display_event(ModelWorkingEvent(model_call_id=call_id))
        self._sync_running()

    @_synchronized
    def say_as(self, author: str, text: str, identifier: str | None = None) -> None:
        """以某个作者的名义发一条聊天消息（玩家发言、复盘教练的报告）。

        顺带取消该作者可能还在跑的"还在想"心跳：话说出来了，就不必再提醒。
        """
        if not text or not text.strip():
            return
        pending = self._waiting.pop(author, None)
        if pending is not None:
            agent_info, call_id = pending.info, pending.call_id   # 接上那个思考窗口
        else:
            self._chunks += 1
            agent_info = AgentInfo(name=author, identifier=identifier or f"ww-{author}",
                                   workdir=self.self_info.workdir)
            call_id = f"{author}-{self._chunks}-{uuid.uuid4().hex[:6]}"
        # 故意绕开 agent.display_event：事件作者不是法官，而是那个座位/教练。
        # WebDisplay.on_event 只做「存历史 + 广播」，不要求作者 agent 已 bind。
        self.display.on_event(DisplayEvent(
            name="ModelMessageEvent",
            agent=agent_info,
            payload=ModelMessageEvent(
                model_call_id=call_id,
                content=text.strip(),
                total_tokens=0,
            ),
        ))
        self._sync_running()   # 这一个座位落地了，其余可能还在算（`on_events` 也经这儿）

    # ---------------------------------------------------------------- 状态与播报
    @property
    def god_view(self) -> bool:
        state = self.state
        return bool(state and state.human_out and state.config.flags.spectator_god_view)

    @_synchronized
    def phase(self, state: GameState, title: str, subtitle: str = "") -> None:
        """阶段开始：一条 info（阶段标题 + 状态），状态没变就只发标题。"""
        self.state = state
        self._flush()                          # 上一节的尾巴先落地
        day, hour = self._phase_of(title)
        if day == 0:
            day = self._day_seen            # 标题里没写天数的阶段（警长竞选等）沿用上一天
        self._day_seen = day
        rows = render.status_lines(state, self.god_view)
        signature = (repr(tuple(rows)), day, hour)
        self._phase_title = self._pending_title = render.phase_title(title, subtitle)
        if signature != self._status_signature:
            self._status_signature = signature
            self._pending.extend(render.status_parts(state, self.god_view))

    @staticmethod
    def _phase_of(title: str) -> tuple[int, int]:
        """从阶段标题取 (天数, 昼夜)，用来判断局面摘要要不要重发。"""
        day = 0
        m = re.search(r"(\d+)\s*天", title or "")
        if m:
            day = int(m.group(1))
        hour = 1 if any(k in (title or "") for k in ("夜", "黑夜")) else 0
        return day, hour

    def _own_labels(self, state: GameState) -> frozenset[str]:
        """真人自己那几种发言的气泡作者名（发言 / 警上 / 遗言）。

        他说的话在会话流里本来就有自己的气泡（右侧那条「你 致 …」），法官再把同一段文字
        播成「5号（警上）」的气泡就是把一句话说两遍 —— 阅读顺序也没变，中间还夹着法官卡片。

        发言托管不会因此丢掉信息：那种场合法官另外会播「已按默认行动托管」那一行，
        而托管文案本身是固定的套话，不是他写的东西。
        `/auto-say` 让法官替他说的那一句**不在此列**：那句不是他自己打的字，`spoke_for_player`
        会临时把它放行，否则场上等于假装他没说话。
        """
        seat = state.config.human_seat
        if seat is None:
            return frozenset()
        return frozenset(render.speech_author(seat, kind) for kind in render.SPEECH_KINDS)

    @_synchronized
    def on_events(self, state: GameState, events: Sequence[Event]) -> None:
        """一次事件批次 → 尽量少的块。

        前端的每个 InfoEvent 都是一个带表头时间戳的灰块，按事件粒度透传就会刷成屏；
        所以连续的系统行攒成一条 info，只有玩家发言（有作者的）才单独成气泡。
        """
        self.state = state
        own = self._own_labels(state)
        if self._authored in own:            # 法官代说的那一句放行（见 `spoke_for_player`）
            own = own - {self._authored}
        for line in render.render_batch(state, events):
            if line.author:
                self._flush()                  # 气泡前必须落地，保证阅读顺序
                if line.author not in own:     # 真人自己的话不重播（见 `_own_labels`）
                    self.say_as(line.author, line.text)
            elif line.parts:
                self._pending.extend(line.parts)   # 票型/夜结算看板并进本块
            else:
                self._pending.append(line.text)
        self._authored = None                  # 放行标记一次一用

    @_synchronized
    def _flush(self) -> None:
        """把攒下的行与看板合成**一个块**发出去（一个时机一个块）。

        纯文本模式下等价于今天的一条 info；有 `HTMLInfoEvent` 时是一张带标题栏的看板卡。
        """
        if not self._pending:
            self._pending_title = ""
            return
        card = H.Card(title=self._pending_title, soft_title=self._phase_title,
                      rule=bool(self._pending_title), parts=_coalesce(self._pending))
        self._pending.clear()
        self._pending_title = ""
        self.agent.display_event(card.event())

    @_synchronized
    def card(self, card: H.Card) -> None:
        """直接发一张卡（身份牌、配置卡这种独立时机）。"""
        self._flush()
        self.agent.display_event(card.event())

    @_synchronized
    def finished(self, state: GameState) -> None:
        self.state = state
        self._flush()
        winner = WIN_LABEL.get(state.winner, state.winner or "结束")
        self._phase_title = f"本局结束 · {winner}"
        # 终局一律给全量身份：这一局已经结束，没有泄密问题
        parts = render.status_parts(state, True)
        if state.winner in H.BANNERS:      # 平局 / 中途终止不发「胜利」横幅
            parts = [H.Banner(winner=state.winner)] + parts
        self.card(H.Card(title=self._phase_title, parts=parts))

    def status_card(self) -> H.Card:
        """`/werewolf status`：配置 + 局面 一张看板卡（永远不含未公开信息）。"""
        state = self.state
        if state is None:
            return H.Card(title="局面", parts=[H.Log(["这一局还没开始。"])])
        return H.Card(title=f"局面 · 第 {state.day} 天 · {state.phase}",
                      parts=render.setup_parts(state.config, state.config.human_seat,
                                               sorted(state.players))
                      + render.status_parts(state, self.god_view))

    def publish_status(self) -> None:
        """把局面卡直接发到会话里（`/werewolf status` 用它）。"""
        self.card(self.status_card())

    def review(self, report: str) -> None:
        """复盘报告：一条结论 + 一份 markdown（作者是「复盘教练」，这样才吃 markdown 渲染）。"""
        self.say_as(REVIEWER, report)

    # ---------------------------------------------------------------- 生命周期
    def stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        with self._input:
            self._auto = False
            self._input.notify_all()             # 等输入的环节立刻醒
        self._sync_running()                     # 先松开执行态，别让用户对着一个假「运行中」
        if self._cards:                          # 挂着的卡片只能靠 cancel_event 唤醒
            threading.Thread(target=self._poke_until_cards_close, daemon=True).start()

    def _poke_until_cards_close(self) -> None:
        """把 `cancel_event` 反复置位，直到挂着的卡片都收了。

        `get_choice` 的取消只能看 `agent.cancel_event`（xun web_display.py:219），而
        `/werewolf stop` 本身跑在这个 agent 的命令 scope 里 —— scope 退出时 xun 会 clear 掉
        这个 event（running_state.py 的 finally），一次 poke 会被抹掉。所以补刀到卡片收完。
        """
        deadline = time.monotonic() + POKE_DEADLINE_SEC
        while self._cards and time.monotonic() < deadline:
            try:
                self.agent.cancel_event.event.set()
            except Exception:                    # 桩 agent 没有 cancel_event 就算了
                return
            time.sleep(POKE_SEC)

    @_synchronized
    def cleanup(self) -> None:
        """收尾这一局的播报。

        只管播报，不动 agent：它不归我们 finalize（本来就是这个会话自己的 agent），
        也没有"交还"这一步 —— 局打完了法官照旧回答复盘与局面，直到用户另开一个会话。
        """
        self._waiting.clear()
        self._opened.clear()
        self._auto = False
        self._sync_running()                   # 局 end 了还挂着执行态 = 永远亮着的谎
        self._flush()                          # 中途终止也别把播报吞了

    @_synchronized
    def spoke_for_player(self, seat: int, kind: str) -> None:
        """记下「这一句是法官替 `seat` 说的」：全场必须听得见它。"""
        self._authored = render.speech_author(seat, kind)

    # ---------------------------------------------------------------- 收集人类输入
    def ask(self, prompt: str, choices: Sequence[str], *, message: str = "", title: str = "",
            subtitle: str = "", default: str | None = None,
            allow_extra: bool = False) -> str:
        """点卡片：只用于**选择题**（投票、夜里行动、举手、警徽……）。长文本一律走输入框。"""
        if self.stop_requested():
            raise GameAborted("会话已关闭")
        self._flush()                          # 播报先显示出来，再弹卡片
        # 选择题和发言是两条不同的输入通路，各自说明白，别让人猜：这一步在输入框里打字
        # 不算回答，会被当成向法官提问（见 `on_user_message`）。开了 `allow_extra` 的卡片上
        # 多一个输入框（终端形态是最后一项 Other），那是**卡片自己的**输入框，跟会话输入框不是一回事。
        how = ("↳ 点下面的按钮选一个，再点「提交」。要自己写一句，用这张卡片上的输入框"
               "（终端里选最后一项 Other）—— 会话输入框里打字不算回答。" if allow_extra else
               "↳ 点下面的按钮选一个，再点「提交」。这一步别在输入框里打字。")
        message = f"{message}\n{how}" if message else how
        self._cards += 1
        self._sync_running()
        try:
            outcome = self.agent.get_choice(
                prompt=prompt,
                choices=list(choices),
                message=message or None,
                title=title or None,
                subtitle=subtitle or None,
                default=default,
                allow_extra=allow_extra,      # 只有「带句话」这种短输入开；选项卡一律不开
                # 法官的卡片必须真人点：auto_confirm 是给模型工作流的便利，开着它会让这局
                # 在自己点卡片的情况下悄悄跑完（xun display_abstract.py:317）
                _skip_auto_confirm=True,
            )
        except CancelledError as exc:
            raise GameAborted("用户取消了这局") from exc
        finally:
            self._cards -= 1
            self._sync_running()
        if self.stop_requested():
            raise GameAborted("会话已关闭")
        return outcome.choice

    def wait_text(self, title: str, subtitle: str = "", note: str = "",
                auto: Callable[[], str] | None = None) -> str:
        """等用户在**会话输入框**里发一句话（发言、遗言用这个）。

        先播一张卡说明"轮到你、你排第几、要说什么"，然后等输入。等的时候不锁播报，
        别的玩家该发言照样往下走；`stop_event` 一置就中断。
        """
        parts: list[H.Part] = []
        if subtitle:
            parts.append(H.Log([subtitle]))
        if note:
            parts.append(H.Log([note]))
        parts.append(H.Log([
            "↳ 该你说话了：在这条会话下面的输入框里打出这句话、发出去，全场就听见了。",
            "这一步没有按钮卡，也不用点什么「提交」。",
            "想先查局面用 /werewolf status，它任何时刻都能用，包括正在等你发言时。",
            "轮到你又不想说：/auto-say，这一句法官替你说（只管这一次，不影响后面）。",
        ], small=True))
        self.card(H.Card(title=title, parts=parts))
        with self._input:
            self._awaiting += 1
            self._sync_running()
            try:
                while not self._inbox and not self._auto:
                    if self.stop_requested():
                        raise GameAborted("会话已关闭")
                    # 不轮询：`stop()` 就在同一把 Condition 里 notify_all，而它是全仓唯一
                    # 写 stop_event 的地方 —— 0.5s 空转只是第二重保护
                    self._input.wait()
                if self._inbox:            # 他自己打了一句：以他说的为准，代说请求作废
                    self._auto = False
                    return self._inbox.pop(0)
                self._auto = False
            finally:
                self._awaiting -= 1
                self._sync_running()
        # 出锁再叫模型：握着 Condition 的锁跑一次模型请求，会把 `stop()` 和别的播报一起别住
        if auto is None:                   # 只有会代说的环节（发言/警上/遗言）才传这个回调
            raise GameAborted("这一步没法代说，请直接用输入框回答")
        self.info("↳ 这一句法官替你说（`/auto-say`，只管这一次）。")
        return auto()


    def request_auto_speech(self) -> bool:
        """`/auto-say` 的落点：把**这一次**发言交给法官（模型）。只管一次，不留状态。

        必须正等着一句话时才生效（`_awaiting` 就是那个计数）。没轮到你的时候这条命令什么都不做
        —— 把请求攒着"等下次轮到你再说"，等于替玩家决定了他自己没说出口的话，那是更糟的失败。
        """
        with self._input:
            if not self._awaiting:
                return False
            self._auto = True
            self._input.notify_all()
            return True

    def confirm(self, prompt: str, message: str = "", default: bool = True,
                title: str = "法官确认") -> bool:
        answer = self.ask(prompt, ["确认", "取消"], message=message, title=title,
                          default="确认" if default else "取消")
        return answer == "确认"

