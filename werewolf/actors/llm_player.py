"""LLM 玩家：xun Agent（NullDisplay，绝不把内心戏显示到前端）+ 结构化输出 + 会话修剪。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from xun import Agent, NullDisplay, ToolBox, Workspace
from xun.config import load_config

from ..engine.ask import Ask, KINDS
from ..engine.config import GameConfig
from .prompts import static_system, turn_prompt


def rebase_system(current: str | None, static: str) -> str | None:
    """system 被谁动过就把角色设定接回来；没人动过就返回 None（一个字都不重写）。

    唯一会动它的是 xun 的 auto_compact：它把摘要写进 `messages[0]`，连角色设定一起盖掉。
    这里把设定放回开头、摘要接在后面 —— 平时那条 system 完全不动，provider 的 KV 前缀缓存
    一路命中；压缩之后角色设定也不会丢。再压缩一次会把整条 system 换成新摘要，那时再接一次。
    """
    text = (current or "").strip()
    wanted = static.strip()
    if text == wanted or text.startswith(wanted):
        return None             # 没被动过；或就是我们上次写的那份（含已接回的摘要），别再叠一遍
    return static if not text else f"{static}\n\n## 前情摘要（更早的会话已被压缩）\n{text}"


def hard_char_budget(cfg: Any) -> int:
    """兜底硬顶的字符数：设在 auto_compact 阈值之上，正常一局永远碰不到。

    只有 auto_compact 被关掉、或模型窗口很小那种情况，`trim_conversation` 才替它剪。
    """
    return max(cfg.auto_compact.token_threshold * 2, 4000)


def trim_conversation(conv: Any, char_budget: int, keep_pairs: int, head: int = 1) -> None:
    """超过硬顶才剪，且只剪尾巴：留下开头 `head` 条 + 最近 `keep_pairs` 对问答。

    座位传 1（只钉 system，前情提要每轮重发，老发言可剪）；复盘教练传 2（钉住 system +
    那份对局记录 —— 学员追问的正是记录里的细节，剪掉等于让教练凭印象下棋）。
    """
    if conv.estimated_message_length() <= char_budget:
        return
    messages = conv.messages
    keep = head if messages and messages[0].get("role") == "system" else 0
    if len(messages) <= keep + 2 * keep_pairs:
        return
    tail = messages[-(2 * keep_pairs):]
    # 剪完得从一条 user 开始：留着以 assistant 开头的会话，有的 provider 直接 400
    tail = tail[next((i for i, m in enumerate(tail) if m.get("role") == "user"), 0):]
    conv.messages[:] = messages[:keep] + tail


class LLMActor:
    """一个座位一个 agent，私有会话；只能看到 Ask 里给它的内容。"""

    def __init__(
        self,
        seat: int,
        role_id: str,
        game_config: GameConfig,
        persona_style: str,
        model_name: str,
        temperature: float = 0.9,
        keep_pairs: int = 14,
        # 必填：座位的会话文件必须落在这一局的 `root_dir` 里，跟着 cleanup() 一起收。
        # 以前它默认 `tempfile.mkdtemp()`，而清理只管 root_dir —— 走到默认分支就是永久泄漏，
        # 而两个调用点本来就都显式传了。
        *, workdir: str | Path,
    ) -> None:
        self.seat = seat
        self.is_human = False
        self.role_id = role_id
        self.game_config = game_config
        self.persona_style = persona_style
        self.level = game_config.flags.level
        self.keep_pairs = keep_pairs
        self.calls = 0
        self.failures = 0
        self.static_system: str | None = None      # 整局只写一次的 system（None = 还没轮到它）

        cfg = load_config().clone()
        cfg.auto_confirm = False
        cfg.enable_extensions = False
        cfg.model.name = model_name
        cfg.model.temperature = temperature   # 只覆盖温度（那是「AI 水平」那一档）；
                                              # 其余模型参数照用户 xun 配置，包括思考强度
        self.char_budget = hard_char_budget(cfg)
        self.agent = Agent(
            name=f"{seat}号",
            display=NullDisplay(),
            config=cfg,
            toolbox=ToolBox(),
            workspace=Workspace(workdir=Path(workdir)),
        ).initialize()

    # ---- 内部 ---------------------------------------------------------------
    def _ensure_system(self, ask: Ask) -> None:
        """第一次决定时写 system，之后不再重写 —— 每轮重写等于每轮打掉整个前缀缓存。

        设定只能由第一次那份 `Ask` 生成（座位号与狼队友从发牌起就没变过），所以这里必须
        幂等：同一座位问一百次，`agent.system()` 也只该被调一次，除非有人动了 system。
        """
        if self.static_system is None:
            self.static_system = static_system(ask, self.game_config, self.role_id,
                                               self.persona_style, self.level)
            self.agent.system(self.static_system)
            return
        messages = self.agent.conversation.messages
        head = messages[0] if messages and messages[0].get("role") == "system" else {}
        rebuilt = rebase_system(str(head.get("content") or "") or None, self.static_system)
        if rebuilt is not None:
            self.agent.system(rebuilt)

    def _trim(self) -> None:
        trim_conversation(self.agent.conversation, self.char_budget, self.keep_pairs)

    def _validate(self, ask: Ask, decision: Any) -> Any:
        pool = list(ask.pool)
        # 选座位的字段：普通环节是 target，女巫的毒药是 poison_target（解药走 save_pool，不在这里）。
        # 少了 poison_target 这一条，模型毒了个不合法的人也会被引擎静默吞掉，一次重问的机会都没有。
        for attr in ("target", "poison_target"):
            value = getattr(decision, attr, None)
            if pool and value is not None and value not in pool:
                raise ValueError(f"{attr}={value} 不在合法座位 {pool} 内")
        target = getattr(decision, "target", None)
        if ask.must_choose and KINDS[ask.kind].no_none and target is None:
            raise ValueError("必须给出选择")
        return decision

    def _decide(self, ask: Ask) -> Any:
        schema = KINDS[ask.kind].schema
        self._ensure_system(ask)
        self.agent.instruct(turn_prompt(ask), _emit_event=False)
        self._trim()
        last_error: Exception | None = None
        for attempt in range(2):
            result = self.agent.execute(schema=schema, max_iterations=1)
            if result.is_err():
                last_error = RuntimeError(str(result.value))
                self.agent.instruct("上一条输出无法解析，请重新按 schema 输出合法 JSON。", _emit_event=False)
                continue
            decision = result.unwrap()
            try:
                return self._validate(ask, decision)
            except ValueError as exc:
                last_error = exc
                self.agent.instruct(f"你的选择不合法：{exc}。请只从法官给出的可选座位里重新选择。", _emit_event=False)
        self.failures += 1
        raise RuntimeError(f"{self.seat}号 决策失败：{last_error}")

    # ---- Actor 接口 ---------------------------------------------------------
    def decide(self, ask: Ask) -> Any:
        """决策协议只有一个动词：问什么、答案什么形状，全在 `engine.ask.KINDS`。"""
        self.calls += 1
        return self._decide(ask)
    def finalize(self) -> None:
        try:
            self.agent.finalize()
        except Exception:
            pass
