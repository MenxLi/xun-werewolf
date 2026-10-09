"""座位演员的 LLM 通路：对着一个假 provider，真跑一遍 xun 的 execution loop。

只假到 `chat.completions.create` 那一层：`Agent` 是真的，`system()` / `instruct()` /
`execute(schema=…)` / `finalize()` 走的都是 xun 真那条路。为什么非要真跑：1.4 把执行态
改名 `run_scope`、把 `_skip_auto_confirm` 的下划线去掉、写 system 不再顺手复位
`is_compressed` —— 这类事在自己编的 agent 桩上永远测不到（桩不会自己改名字），只有真跑
一次 loop 才当场炸出来。真模型当然也不碰。
"""
from __future__ import annotations

import shutil
import tempfile
from types import SimpleNamespace

from werewolf.actors.llm_player import LLMActor
from werewolf.engine.ask import KIND_SEER
from werewolf.engine.build import AskBuilder
from werewolf.engine.config import PRESETS
from werewolf.engine.engine import Engine
from werewolf.engine.presenter import NullPresenter
from werewolf.engine.roles import SEER


def _setup():
    """一局假局面 + 一个预言家座位演员（模型换成假的，先不给回答）。

    回答要等各测试拿到 `pool`（合法座位）之后再塞进 `provider.replies`：
    「非法座位」那两条得先知道合法的是哪几个，才写得出「只差一格」的坏答案。
    """
    cfg = PRESETS[2].config.copy()
    cfg.human_seat = None
    eng = Engine(cfg, {}, presenter=NullPresenter(), seed=11)
    eng.setup()
    seat = next(s for s in eng.state.alive_seats() if eng.state.players[s].role_id == SEER)
    pool = [s for s in eng.state.alive_seats() if s != seat]
    ask = AskBuilder(eng.state, eng.stances).build(seat, KIND_SEER, pool=pool)

    workdir = tempfile.mkdtemp(prefix="ww-llm-")
    actor = LLMActor(seat=seat, role_id=SEER, game_config=eng.state.config,
                     persona_style="稳健、按局面行事。", model_name="fake-model",
                     temperature=0.7, workdir=workdir)
    provider = _FakeProvider([])
    actor.agent._openai_client = SimpleNamespace(          # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=provider))
    return ask, pool, actor, provider, workdir


class _FakeProvider:
    """假 provider：`create(stream=True, …)` 回一段流式 chunk，最后一章只带 usage。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        text = self.replies.pop(0) if self.replies else "{}"
        return [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content=text, tool_calls=None))], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(
                completion_tokens=11, prompt_tokens=2200, total_tokens=2211,
                prompt_tokens_details=None)),
        ]


def test_a_seat_decision_reaches_the_real_xun_loop():
    """一次查验：合法 JSON → 校验通过的决策，且模型确实收到角色设定。"""
    ask, pool, actor, provider, workdir = _setup()
    provider.replies = [f'{{"target": {pool[0]}, "reason": "先验最像狼的"}}']
    try:
        decision = actor.decide(ask)
        assert decision.target == pool[0], decision
        assert actor.calls == 1 and actor.failures == 0

        request = provider.requests[0]
        head = request["messages"][0]
        # 角色设定必须是会话最前面那条 system（KV 前缀缓存就靠它整局不动）
        assert head["role"] == "system" and head["content"].startswith(actor.static_system)
        assert head["content"].count(actor.static_system) == 1, "设定不许叠第二层"
        schema = request["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["target"], "结构化输出要把决策 schema 交给 provider"
        assert request["temperature"] == 0.7, "AI 水平那一档得真的落到模型参数上"
        assert actor.agent.conversation.messages[0].is_compressed is False
    finally:
        actor.finalize()
        shutil.rmtree(workdir, ignore_errors=True)


def test_an_illegal_seat_is_asked_again_instead_of_being_swallowed():
    """模型选了不存在的座位：要重问一次，而不是被引擎静默吞成一个默认行动。"""
    ask, pool, actor, provider, workdir = _setup()
    provider.replies = ['{"target": 99}', f'{{"target": {pool[1]}}}']
    try:
        decision = actor.decide(ask)
        assert decision.target == pool[1], decision
        assert len(provider.requests) == 2, "非法座位必须换来一次重问"
        assert actor.failures == 0, "第二次就对了，不该记失败"
        second = provider.requests[1]["messages"][-1]["content"]
        assert "不在合法座位" in second, second
    finally:
        actor.finalize()
        shutil.rmtree(workdir, ignore_errors=True)


def test_two_bad_answers_become_a_failure_for_the_engine():
    """两次都不合法才抛：引擎那侧据此走默认行动，并记一次失败。"""
    ask, pool, actor, provider, workdir = _setup()
    provider.replies = ['{"target": 99}', '{"target": 98}']
    try:
        try:
            actor.decide(ask)
            raise AssertionError("两次都非法，应该抛出去")
        except RuntimeError as exc:
            assert "决策失败" in str(exc), exc
        assert len(provider.requests) == 2, "只重问一次，不许无限追问"
        assert actor.failures == 1
    finally:
        actor.finalize()
        shutil.rmtree(workdir, ignore_errors=True)
