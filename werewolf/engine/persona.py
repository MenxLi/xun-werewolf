"""隐藏人格抽样与参选倾向：人格只改说话风格和参选意愿，不改阵营信息。

设计要点：
- 人格在每个座位上独立抽样，**与阵营严格独立**（`_style` 拿不到 role_id）；
- 参选概率 = sigmoid(抢麦欲偏移 + role_bonus + 本局竞选氛围)，其中**狼人阵营的 role_bonus
  恒为 0.0**（有单测锁死），因此狼不会系统性更容易抢警徽；
- 抽到的 `run_for_sheriff` 只作为"人格的一部分"影响 agent，不把概率数字告诉模型；
- `style` 是给模型看的一句话人格描述，整局不变（它进 system，不是每轮消息），所以里面
  写的必须是**说话习惯**这种能一直照着做的东西，而且要有具体的口头习惯 —— 一局八个人
  如果只有"稳健/激进/讲逻辑"三种形容词，八段发言听下来就是同一个人在换座位。

抽样流是刻意切开的：传进来的 rng 只被取走**一个数**（本局基数），之后竞选氛围、每个座位
的说话轴、每个座位的参选判定，各自从基数派生出的子流里取。于是以后加一根、减一根轴，只会
改到那个座位自己的说话风格 —— 不会把发牌、刀口、票型一起平移掉，也不会让别人突然变了参选
态度。（以前这些全共用一条流，动一次文案档位数就得重定一批测试的种子。）
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass

from .roles import HUNTER, IDIOT, SEER, VILLAGER, WITCH, WOLF, WOLF_KING

MAX_CANDIDATES = 6

# 只有好人有正偏置；狼队为 0.0（硬编码，禁止调高）。
ROLE_CANDID_BONUS: dict[str, float] = {
    WOLF: 0.0,
    WOLF_KING: 0.0,
    VILLAGER: 0.0,
    SEER: 0.35,
    WITCH: 0.10,
    HUNTER: 0.10,
    IDIOT: 0.05,
}

#: 口头习惯：每人抽一条，跨轮稳定。发言的"声音"主要靠这个区分，不靠形容词。
QUIRKS: tuple[str, ...] = (
    "爱用反问逼人表态（“你就说一句，你到底在怕什么？”）",
    "爱把话压成带数字的短句（“两个理由。一，……”）",
    "爱直接点名喊人（“9号，你别绕。”）",
    "爱甩完结论就收（“就这样，过。”）",
    "爱自嘲一句再讲正事（“我牌面难看，但这票我不会给错”）",
    "爱讲听感（“我耳朵就是觉得你别扭”）",
    "爱用位置和概率说话（“这个位置藏狼最划算”）",
    "爱把话留一半（“我心里有人，先不点名”）",
    "爱主动认错重盘（“昨天是我看错了，今天重盘一遍”）",
    "爱替人下判断（“别装了，你就是那匹”）",
)

#: 每座抽的说话轴。加一根就往里加一个名字（只有 `mic_desire` 进参选概率）。
#: 顺序无所谓：每个名字各走自己的子流位置，加在哪个位置都不影响别人的取值。
AXES: tuple[str, ...] = ("mic", "aggression", "logic", "talk", "nerve",
                         "warmth", "doubt", "jargon", "stubborn")

@dataclass(frozen=True)
class Persona:
    """只存**有人读**的数：抢麦欲（警上人数超限时的筛选）、要不要参选、说给人看的那句风格。

    以前还存着 aggression / vanity / logic_first 三个数，全仓库除了抽样处自己没人读过 ——
    它们的唯一作用是变成 style 里的一句话，那就该留在局部变量里，别装成模型。
    """
    seat: int
    mic_desire: float
    run_for_sheriff: bool
    style: str


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def candid_probability(mic_desire: float, role_id: str, mood: float = 0.0) -> float:
    bonus = ROLE_CANDID_BONUS.get(role_id, 0.0)
    return sigmoid(2.4 * (mic_desire - 0.5) + bonus + mood)


def _child(base: int, *keys: object) -> random.Random:
    """从本局基数派生一条子流。用 blake2b 而不是 `hash()`：后者带进程盐，换进程就不一样了。"""
    digest = hashlib.blake2b(":".join(str(k) for k in (base, *keys)).encode(), digest_size=8)
    return random.Random(int.from_bytes(digest.digest(), "big"))


def _talk_clause(talk: float) -> str:
    """话长度习惯：跨轮稳定，座位之间不同 —— 让一局里有人一两句收工、有人摆链条。

    字数上限只是天花板（`config.flags.speech_word_limit`），这里给的是**软目标**，
    整体刻意压得比上限低很多：桌上是听人说话，不是读人写作。
    """
    if talk <= 0.33:
        return "你说话简短，一两句收工，把判断压成一句结论（发言 40~90 字就够）"
    if talk <= 0.72:
        return "你发言中等长度，给结论加一两条依据就收（100~180 字）"
    return "你话多，习惯把票型、位置学、听感摆出来，但每句仍然只说一件事（200~300 字）"


def _style(v: dict[str, float], quirk: str) -> str:
    """把说话轴拼成一句人话。

    只挑**会在发言里被听出来**的写，而且一根轴只管一件事，免得模型收到自相矛盾的指令：
    `nerve` 管说的时候敢不敢断言，`stubborn` 管已经说出口的判断改不改，
    `doubt` 管信不信别人跳的身份，`warmth` 管替不替人说话。
    每条都按真桌打法写：嘴硬的不许把猜的说成事实，不改票的见到硬信息也得认。
    """
    clauses: list[str] = [_talk_clause(v["talk"]), f"口头习惯：{quirk}"]
    if v["mic"] >= 0.72:
        clauses.append("抢麦欲很强，很在意风头，喜欢第一个开口、用警徽定节奏")
    elif v["mic"] >= 0.45:
        clauses.append("发言欲望中等，看局面决定是否抢先手")
    else:
        clauses.append("偏低调，让别人先说、自己后手收网，但很在意自己在这个局里的位置")
    if v["aggression"] >= 0.7:
        clauses.append("打法激进，敢直接顶人和硬抿")
    elif v["aggression"] <= 0.35:
        clauses.append("打法保守，不愿第一个树敌")
    else:
        clauses.append("打法稳健，该争的时候才争")
    if v["logic"] >= 0.65:
        clauses.append("讲逻辑链，靠票型和发言结构说服别人")
    elif v["logic"] <= 0.35:
        clauses.append("更靠直觉和情绪感染，喜欢抓别人语气里的破绽")
    if v["nerve"] >= 0.65:
        clauses.append("把话说死，哪怕只有一半把握也敢咬定（但没依据的东西你得说成猜的）")
    elif v["nerve"] <= 0.35:
        clauses.append("说话留余地，没把握就说“可能是我多心”")
    if v["warmth"] >= 0.65:
        clauses.append("心软，容易替人说话、把人往回捞")
    elif v["warmth"] <= 0.35:
        clauses.append("冷面，谁的面子都不给，只盘位置")
    if v["doubt"] >= 0.65:
        clauses.append("疑心重，谁跳身份都想先验一遍再信")
    elif v["doubt"] <= 0.35:
        clauses.append("好说话，谁跳身份都先信三分，回头再补验")
    if v["stubborn"] >= 0.65:
        clauses.append("自己定的票不轻易改，别人喊你改名你先把理由讲完；"
                       "要改，得是比昨天更硬的理由——查验、毒、枪这种硬信息摆出来你就得认，不许装看不见")
    elif v["stubborn"] <= 0.35:
        clauses.append("改主意快，场上风向一变就敢改票，也不觉得改票丢人；"
                       "但每次改口都得说一句是什么让你改的，别说改就改")
    clauses.append("满口金水查杀警徽流这些说法" if v["jargon"] >= 0.6
                   else "尽量用大白话说，不堆术语")
    return "；".join(clauses) + "。"


def sample_personas(assignment: dict[int, str], rng: random.Random,
                    skip: int | None = None) -> dict[int, Persona]:
    """给 AI 座位抽人格。`skip` 是真人座位：不抽 —— 他自己的心情就是他的风格。"""
    base = rng.getrandbits(64)          # 主 rng 只被取走这一个数
    mood = _child(base, "mood").gauss(0.0, 0.45)      # 本局的竞选氛围：有的局人人抢麦
    personas: dict[int, Persona] = {}
    for seat, role_id in sorted(assignment.items()):
        if seat == skip:
            continue
        axes = _child(base, seat, "axes")
        values = {name: axes.random() for name in AXES}
        quirk = axes.choice(QUIRKS)
        # 参选判定单开一条子流：加一根说话轴不该把谁是否举手写掉
        run = _child(base, seat, "candid").random() < candid_probability(
            values["mic"], role_id, mood)
        personas[seat] = Persona(seat=seat, mic_desire=values["mic"],
                                 run_for_sheriff=run, style=_style(values, quirk))

    # 警上人数过多时，只保留抢麦欲最高的若干人举手（其余视为未举手）
    running = [p.seat for p in personas.values() if p.run_for_sheriff]
    if len(running) > MAX_CANDIDATES:
        keep = set(sorted(running, key=lambda s: -personas[s].mic_desire)[:MAX_CANDIDATES])
        for seat in running:
            if seat not in keep:
                personas[seat] = Persona(**{**personas[seat].__dict__, "run_for_sheriff": False})
    return personas
