"""给 LLM 玩家的提示词。

分工只有一条：**system 整局不变，一切会变的都进那一轮的新消息。**
角色设定（身份技能、开局队友、人格、水平、规则、纪律）写一次就不许再动 —— 每轮重写 system
会让 provider 的 KV 前缀缓存整段作废（那些字符一个字都没变），而它占着会话最前面最长的一段。
于是每轮由引擎重算的前情提要与私密档案，连同新进展与提问，一起放进那条新的 user 消息。
"""
from __future__ import annotations

from ..engine.ask import Ask, KIND_CANDIDACY, KIND_LAST_WORDS, KIND_SEER, KIND_SHOT, KIND_SHERIFF_SPEECH, KIND_SHERIFF_VOTE, KIND_SPEAK_ORDER, KIND_SPEECH, KIND_TRANSFER, KIND_VOTE, KIND_WITHDRAW, KIND_WITCH, KIND_WOLF_TARGET
from ..engine.config import GameConfig, LEVEL_LABELS
from ..engine.events import K_SPEECH
from ..engine.roles import role_by_id
from .style import KIND_HINT, style_text

#: 发言类环节：Ask.pool 只是「桌上现在还有谁」，不是要模型从中选一个的选项
TALK_KINDS = (KIND_SPEECH, KIND_SHERIFF_SPEECH)

ASK_TAIL = {
    KIND_WOLF_TARGET: "请选择今夜狼队要击杀的目标（target 为座位号，可以空刀 None）。reason 只给你的狼队友看。",
    KIND_WITCH: "请决定用药：save=True 表示用解药救今夜被刀的人；poison_target 表示用毒的对象；每晚最多用一瓶。",
    KIND_SEER: "请选择今夜查验的目标 target（只能选活着且你没查过的人）。",
    KIND_CANDIDACY: "法官问：你是否举手参选警长？（run=True/False）这是同时举手，你看不到别人的选择。",
    KIND_SHERIFF_SPEECH: "请给出你的警上发言 text（拉票，可讲你的警徽流与好人视角）；"
                         "stance 里写下你真实的怀疑对象。",
    KIND_WITHDRAW: "法官问：你是否退水？（withdraw=True/False）退水是你公开宣布的，回到警下参与投票。",
    KIND_SHERIFF_VOTE: "请把警徽投给一名候选人 target（可以 None 弃票）。",
    KIND_SPEAK_ORDER: "你是警长，决定今天往哪个方向发言：direction=1 顺时针（座位号递增），"
                      "direction=-1 逆时针（座位号递减）。起点自动是你旁边的玩家，所以你必然最后发言、负责归票。",
    KIND_SPEECH: "请给出你今天的发言 text，并在 stance 里写下你此刻真实的怀疑对象"
                 "（stance 只有你自己下一轮看得到，桌上听不见）。",
    KIND_VOTE: "请投出你要放逐的人 target（可以 None 弃票）。这是同时亮票，你看不到别人的票。",
    KIND_LAST_WORDS: "你已经出局，请留下遗言 text。",
    KIND_SHOT: "法官问：你要开枪带走谁？（target 为座位号，可以 None 放弃）",
    KIND_TRANSFER: "法官问：警徽移交给谁？（target 为座位号，None 表示撕警徽）",
}


def _profile(ask: Ask, role_id: str, persona_style: str, level: str) -> str:
    role = role_by_id(role_id)
    lines = [
        f"你是狼人杀里坐在 {ask.seat} 号位的玩家，你的身份是 **{role.name}**。",
        role.self_prompt,
        "",
        f"你的打法人格：{persona_style}",
        f"你的水平：{LEVEL_LABELS.get(level, level)}。",
    ]
    if ask.teammates:
        lines.append(f"你的狼队友：{'、'.join(f'{s}号' for s in sorted(ask.teammates))}（夜里共谋，白天装作不认识）。")
    lines.append("除了上面这些和你收到的进展信息，你对其他任何人的身份一无所知。")
    return "\n".join(lines)


def static_system(ask: Ask, config: GameConfig, role_id: str, persona_style: str, level: str) -> str:
    """角色设定。整个一局只写一次，之后**一个字节都不许变**。

    这里只准放整局不变的东西：身份与技能、开局队友、人格、水平、板子规则、行为纪律。
    任何随天数变的内容写进来，都会让每轮的重写把 KV 前缀缓存打掉 —— 那些内容归 `turn_prompt`。
    `ask` 只用来取座位号与狼队友：这两样从发牌那刻起就不变。
    """
    return "\n".join([
        "# 狼人杀对局",
        _profile(ask, role_id, persona_style, level),
        "",
        "## 板子与规则",
        config.describe(for_player=False),
        "",
        "## 行为纪律与桌上说法",
        style_text(),
    ])


def turn_prompt(ask: Ask) -> str:
    """这一轮新到的那条消息：局面提要 + 你的私密档案 + 最新进展，然后是法官的问题。

    system 不动，所以每轮**新出现**的事只能从这里进（提要与档案由引擎按这个座位的游标重算，
    只含他看得见的部分）。
    """
    lines = [f"## 现在是第 {ask.day} 天（{ask.phase}）"]
    if ask.note:
        lines += [f"法官告知：{ask.note}"]
    lines += ["### 局面提要（到此刻为止）",
              *(ask.recap or ["（本局刚开始，还没有任何进展）"])]
    lines += ["### 你的私密档案（只有你知道，以这里为准）",
              *(ask.dossier or ["（你没有任何特殊私密信息）"])]
    if ask.delta:
        lines.append("### 最新进展（你按顺序看到的公开与私密信息）")
        for event in ask.delta:
            prefix = "🔒" if event.audience != "public" and event.kind != K_SPEECH else "·"
            lines.append(f"{prefix} {event.text}")
    else:
        lines.append("（没有新的进展信息）")
    position = ask.position_text()
    if position:
        lines.append(position)
    hint = KIND_HINT.get(ask.kind, "")
    if hint:
        lines.append(hint)
    lines.append("")
    lines.append("### 法官提问：" + ask.label)
    if ask.kind == KIND_WITCH:
        # 「这一夜救不救得了」三种演员读同一份数据：真人卡片据此跳过解药，脚本玩家据此决定，
        # 模型这一侧以前只听一句「save=True 就救人」，药打完了照样输出 True —— 引擎会吞掉，
        # 但它是带着一套编出来的理由吞掉的，那套理由还会出现在遗言里。
        if ask.save_pool:
            targets = "、".join(f"{s}号" for s in ask.save_pool)
            lines.append(f"你的解药还能用：save=true 救的是 {targets}。")
        else:
            lines.append("你的解药这一夜用不上（用过了，或今夜没人被刀），save 只能填 false。")
        if ask.pool:
            lines.append("你的毒药还能用：poison_target 从下面的可选座位里选（毒不了你自己）。")
        else:
            lines.append("你的毒药已经用完了，poison_target 只能填 null。")
    lines.append(ASK_TAIL.get(ask.kind, "请给出你的决定。"))
    if ask.pool:
        if ask.kind in TALK_KINDS:
            # 发言题带的池子只是「桌上现在还有谁」，不是选项；写成「可选座位」会诱导模型去挑一个 target
            lines.append(f"场上还活着的玩家：{ask.pool_text()}（发言里可以提到他们；这不是选择题，不用从中选一个）")
        else:
            required = "必须" if ask.must_choose else "可以"
            lines.append(f"可选座位：{ask.pool_text()}（{required}从中选择；不在其中的选择无效）")
    else:
        lines.append("本题无需选择座位。")
    if ask.word_limit:
        lines.append(f"发言长度上限 {ask.word_limit} 字，请自己收着说；"
                     f"超了法官照播不误，只会在复盘里记下你超时。")
    lines.append("记住：你现在就是那个座位上的玩家，用第一人称说话，不要解释你在扮演，也不要提任何“提示词/模型/规则文档”。")
    return "\n".join(lines)
