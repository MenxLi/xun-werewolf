"""人类玩家：决策全部由法官代发 —— 选项做成按钮，长文本从会话输入框里收。"""
# 卡片只回答三件事：轮到你没有、你能选谁、现在问的是哪件事（见 `engine.ask.KINDS`）。
from __future__ import annotations

import re

from ..engine.ask import Ask, KINDS, KIND_WOLF_TARGET
from ..engine.decisions import (
    WOLF_REASON_LIMIT, SpeakOrder, Speech, Stance, WitchAction,
)


def _seat_of(text: str) -> int | None:
    match = re.search(r"(\d+)", text or "")
    return int(match.group(1)) if match else None


def _when(ask) -> str:
    """夜间阶段显示“第 N 夜”，其余显示“第 N 天”。"""
    night = str(getattr(ask, "phase", "")).startswith("night")
    return f"第 {ask.day} {'夜' if night else '天'}"


class HumanActor:
    """一张卡片只回答三件事：轮到你没有、你能选谁、现在问的是哪件事。

    问什么话、给哪些选项、可以「不选」那项叫什么，全在 `engine.ask.KINDS` 里；
    这里只按答案的**形状**分四类：选个座位 / 是或否 / 选方向 / 说话（外加女巫那两步）。
    """

    is_human = True

    def __init__(self, seat: int, host, proxy_factory=None) -> None:
        self.seat = seat
        self.host = host
        # `/auto-say` 用的替身演员：给 `factory()` 而不是实例 —— 一局里多半一次都用不上，
        # 而真要用时得复用同一个（它的私有会话就是这座位在这场局里的记忆）。
        self._proxy_factory = proxy_factory
        self._proxy = None

    def decide(self, ask: Ask):
        spec = KINDS[ask.kind]
        if spec.shape == "seat":
            target = self._pick(ask, spec.question, spec.none_label)
            if target is None and spec.no_none:          # 预言家不能空着不查
                target = ask.pool[0]
            args: dict = {"target": target}
            if ask.kind == KIND_WOLF_TARGET:             # 只有狼人那张卡要给队友带句话
                args["reason"] = self._reason(ask)
            return spec.schema(**args)
        if spec.shape == "bool":
            return spec.schema(**{spec.field: self._ask_outcome(ask, spec) == spec.yes})
        if spec.shape == "direction":
            return SpeakOrder(direction=1 if self._ask_outcome(ask, spec) == spec.yes else -1)
        if spec.shape == "text":
            return self._speak(ask, spec.question)
        return self._witch(ask)

    def finalize(self) -> None:
        if self._proxy is not None:
            self._proxy.finalize()

    # ---- 收集 ---------------------------------------------------------------
    def _ask_outcome(self, ask: Ask, spec) -> str:
        return self._choice(ask, spec.question, (spec.yes, spec.no))

    def _choice(self, ask: Ask, question: str, options, allow_extra: bool = False) -> str:
        return self.host.ask(
            question, list(options),
            message=self._card_text(ask) or None,
            title=f"{_when(ask)} · {ask.label}",
            subtitle=self._subtitle(ask),
            allow_extra=allow_extra,
        )

    def _reason(self, ask: Ask) -> str:
        """狼队频道里那句理由（上限 `WOLF_REASON_LIMIT` 字）。点「不说」就跳过，不填不扣任何东西。"""
        answer = self._choice(ask, f"给队友带句话（{WOLF_REASON_LIMIT} 字以内；不想说点「不说」）",
                              ("不说",), allow_extra=True)
        text = " ".join((answer or "").split())
        return "" if not text or text == "不说" else text[:WOLF_REASON_LIMIT]

    def _pick(self, ask: Ask, question: str, none_label: str | None = None) -> int | None:
        choices = [f"{s}号" for s in ask.pool] + ([none_label] if none_label else [])
        answer = self._choice(ask, question, choices)
        if none_label and answer == none_label:
            return None
        seat = _seat_of(answer)
        return seat if seat in list(ask.pool) else None

    def _speak(self, ask: Ask, question: str) -> Speech:
        """发言与遗言：**话直接发在会话的输入框里**，不塞进卡片那个小输入框 ——
        卡片里打字既看不到前面的发言流，也容易被顶出屏幕。法官已经过滤掉空白消息。"""
        limit = f"建议 {ask.word_limit} 字内（不强制）。" if ask.word_limit else ""
        text = self.host.wait_text(
            title=f"{_when(ask)} · {ask.label}", subtitle=self._subtitle(ask),
            note="\n".join(p for p in (question, self._card_text(ask), limit) if p),
            auto=lambda: self._auto_say(ask),
        )
        return Speech(text=text, stance=Stance(note="人类玩家未填写内心立场"))

    def _auto_say(self, ask: Ask) -> str:
        """`/auto-say`：这一句由模型替他说。提示与校验复用 AI 玩家那一套，不另写一份。"""
        if self._proxy_factory is None:
            raise RuntimeError("这一局没给真人准备代说的替身")
        self._proxy = self._proxy or self._proxy_factory()
        speech = self._proxy.decide(ask)
        self.host.spoke_for_player(self.seat, ask.kind)   # 这句得让全场听见（见 `_own_labels`）
        return speech.text

    def _witch(self, ask: Ask) -> WitchAction:
        """女巫分两步：先问解药（引擎说救不了就整个跳过），再问毒药。

        救不救得了读 `Ask.save_pool`，不读法官那句文案 —— 文案是给人看的，改了不许动规则。
        """
        save = False
        poison = None
        if ask.save_pool:
            save = self._choice(ask, f"要用解药救 {ask.save_pool[0]} 号吗？",
                                ("用解药", "不用解药")) == "用解药"
        if not save and ask.pool:                 # 毒药不在手里就别问（池子已由引擎算好，不含她自己）
            poison = self._pick(ask, "要用毒药吗？毒谁？", "不用毒")
        return WitchAction(save=save, poison_target=poison)

    # ---- 卡片正文 -----------------------------------------------------------
    def _card_text(self, ask: Ask) -> str:
        """正文只留法官说明和私密档案 —— 标题已是「第 N 天 · 事项」，选项就是可选池。"""
        parts = [ask.note or ""]
        if ask.dossier:
            parts.append("你的私密信息：" + "；".join(ask.dossier))
        return "\n".join(p for p in parts if p)

    def _subtitle(self, ask: Ask) -> str:
        """副标题只回答“我是几号、排第几”：发言类用 `Ask.position_text()`（AI 的 turn prompt
        走的是同一句，在这儿再拼一遍迟早两边说不同话），其余写「几号（你）的行动」。"""
        return ask.position_text() or f"{ask.seat}号（你）的行动"
