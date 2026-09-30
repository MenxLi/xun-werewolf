"""对局配置：规则开关、板子（角色分布）、预设与合法性校验。"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

from .roles import (
    CAT_GOD, CAT_VILLAGER, ROLES, VILLAGER, WOLF, WOLF_KING,
    SEER, WITCH, HUNTER, IDIOT, is_wolf, role_by_id,
)

MIN_SEATS, MAX_SEATS = 5, 14

WIN_EDGE = "edge"      # 屠边：村民全死 或 神职全死 -> 狼胜
WIN_CITY = "city"      # 屠城：好人全死 -> 狼胜

#: 阵营的中文名。终局播报与终局卡标题以前各写一遍，现在只这一份
WIN_LABEL = {"wolf": "狼人阵营", "good": "好人阵营", "draw": "平局"}

LAST_WORDS_FIRST_NIGHT = "first_night_only"
LAST_WORDS_ALL = "all"
LAST_WORDS_NONE = "none"

LEVEL_ROOKIE = "rookie"
LEVEL_BALANCED = "balanced"
LEVEL_ELITE = "elite"

LEVEL_LABELS = {LEVEL_ROOKIE: "新手", LEVEL_BALANCED: "均衡", LEVEL_ELITE: "高手"}


@dataclass
class RuleFlags:
    win_condition: str = WIN_EDGE
    last_words: str = LAST_WORDS_FIRST_NIGHT
    witch_self_save: bool = False
    reveal_dead_role: bool = False
    witch_sees_knife_only_with_potion: bool = True
    sheriff_enabled: bool = True
    sheriff_vote_weight: float = 1.5
    spectator_god_view: bool = True
    speech_word_limit: int = 500
    #: 警上发言上限。以前是 150，但引擎结算时没按 kind 取上限，等于没生效（已修）
    sheriff_word_limit: int = 500
    vote_reveal: bool = True
    level: str = LEVEL_BALANCED

    # ---- 文案（法官通道要短、提示词要全，两处都从这里取，避免两份映射各自漂移）
    @property
    def win_short(self) -> str:
        return "屠边" if self.win_condition == WIN_EDGE else "屠城"

    @property
    def win_text(self) -> str:
        return self.win_short + ("（村民全死或神职全死即狼胜）" if self.win_condition == WIN_EDGE
                                 else "（好人全死即狼胜）")

    @property
    def last_words_short(self) -> str:
        return {LAST_WORDS_FIRST_NIGHT: "首夜死者有遗言",
                LAST_WORDS_ALL: "死者都有遗言"}.get(self.last_words, "一律无遗言")

    @property
    def last_words_text(self) -> str:
        return {
            LAST_WORDS_FIRST_NIGHT: "首夜死者有遗言，之后夜死无遗言，被放逐者有遗言",
            LAST_WORDS_ALL: "所有死者都有遗言",
            LAST_WORDS_NONE: "一律无遗言",
        }[self.last_words]

    @property
    def sheriff_text(self) -> str:
        """警长那一条的唯一写法。

        板子摘要（`render.build_board_lines`）和逐条确认（`summary_lines`）以前各写一遍，
        已经漂移到"摘要里少了死前可移交警徽、警徽票数一种 `:g` 一种 `str()`"。
        """
        if not self.sheriff_enabled:
            return "无"
        return (f"有（警徽 {self.sheriff_vote_weight:g} 票，决定每天发言方向且压轴归票，"
                f"死前可移交警徽）")

    def summary_lines(self, for_player: bool = True) -> list[str]:
        """规则摘要。`for_player=False` 是**给 AI 玩家**的：最后那条观战视角是问真人的显示选项，
        对 AI 座位说了只会让它以为自己有上帝视角。"""
        lines = [
            f"胜负判定：{self.win_text}",
            f"遗言规则：{self.last_words_text}",
            f"女巫：解药{'可' if self.witch_self_save else '不可'}自救"
            + ("；两瓶药用完后不再知道刀口" if self.witch_sees_knife_only_with_potion else ""),
            "猎人/狼王：被女巫毒杀不能开枪",
            f"警长：{self.sheriff_text}",
            f"发言字数上限：普通 {self.speech_word_limit} 字，警上 {self.sheriff_word_limit} 字",
            f"公布死讯时{'公开身份' if self.reveal_dead_role else '不公开身份'}；{'亮票' if self.vote_reveal else '不亮票（只公布票型）'}",
            f"AI 玩家水平：{LEVEL_LABELS.get(self.level, self.level)}",
        ]
        if for_player:
            lines.append(
                f"你出局后的观战视角："
                f"{'上帝视角（可看到所有身份与夜间行动）' if self.spectator_god_view else '只看公开信息'}")
        return lines


@dataclass
class GameConfig:
    """一局的全部配置。座位号从 1 开始。"""
    label: str = "自定义"
    n_seats: int = 9
    counts: dict[str, int] = field(default_factory=lambda: {WOLF: 3, SEER: 1, WITCH: 1, HUNTER: 1, VILLAGER: 3})
    human_seat: int | None = None
    human_role: str | None = None
    flags: RuleFlags = field(default_factory=RuleFlags)
    seed: int | None = None
    #: 开局向导里已经把规则逐条问过并展示过（引擎就不再重复播报 9 条规则）
    rules_shown_in_setup: bool = False

    # ---- 基本量 -----------------------------------------------------------
    @property
    def seats(self) -> list[int]:
        return list(range(1, self.n_seats + 1))

    @property
    def n_wolves(self) -> int:
        return sum(c for r, c in self.counts.items() if is_wolf(r))

    @property
    def n_good(self) -> int:
        return self.n_seats - self.n_wolves

    def validate(self) -> None:
        bad = [r for r in self.counts if r not in ROLES]
        if bad:
            raise ValueError(f"未知角色: {bad}")
        if any(c < 0 for c in self.counts.values()):
            raise ValueError("角色数量不能为负")
        total = sum(self.counts.values())
        if total != self.n_seats:
            raise ValueError(f"角色总数 {total} 与人数 {self.n_seats} 不一致")
        if not (MIN_SEATS <= self.n_seats <= MAX_SEATS):
            raise ValueError(f"人数需在 {MIN_SEATS}-{MAX_SEATS} 之间")
        if self.n_wolves < 1:
            raise ValueError("至少要有一匹狼")
        if self.n_wolves > self.n_good - 1:
            raise ValueError(f"狼过多（{self.n_wolves} 狼 / {self.n_good} 好人），好人无法获胜")
        if self.flags.win_condition == WIN_EDGE:
            # 数的是**发得出去的座位**，不是 counts 里的键：自定义板子把数量 0 的神职键留在字典里，
            # 按键判断等于「0 个神职也算有神职」——于是屠边局第 1 天就宣布「神职已全部出局」，
            # 狼队一句话没说就赢了（AGENTS.md：能选谁/能不能赢是数据，不能靠数错的东西判定）。
            gods = sum(c for r, c in self.counts.items() if role_by_id(r).category == CAT_GOD)
            folks = sum(c for r, c in self.counts.items() if role_by_id(r).category == CAT_VILLAGER)
            if gods < 1:
                raise ValueError("屠边规则需要至少一个神职")
            if folks < 1:
                raise ValueError("屠边规则需要至少一个村民")
        if self.human_seat is not None and not (1 <= self.human_seat <= self.n_seats):
            raise ValueError(f"你的座位 {self.human_seat} 不在 1-{self.n_seats} 之间")
        if self.human_role is not None and self.counts.get(self.human_role, 0) < 1:
            raise ValueError("本板子没有你选择的角色")

    def role_names(self) -> str:
        parts = []
        for rid, count in sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0])):
            if count:
                parts.append(f"{count} {role_by_id(rid).name}")
        return " / ".join(parts)

    def describe(self, for_player: bool = True) -> str:
        head = f"**{self.label}：{self.n_seats} 人（{self.role_names()}）**"
        bullets = "\n".join(f"- {line}" for line in self.flags.summary_lines(for_player=for_player))
        return head + "\n\n" + bullets

    # ---- 发牌 -------------------------------------------------------------
    def build_deck(self, rng: random.Random) -> dict[int, str]:
        """返回 seat -> role_id。若指定 human_role，则保证 human_seat 拿到该角色。"""
        self.validate()
        deck: list[str] = []
        for rid, count in self.counts.items():
            deck.extend([rid] * count)
        rng.shuffle(deck)
        seats = list(self.seats)
        assignment = dict(zip(seats, deck))
        if self.human_seat is not None and self.human_role:
            swap_seat = next(s for s, r in assignment.items() if r == self.human_role)
            assignment[swap_seat], assignment[self.human_seat] = (
                assignment[self.human_seat], self.human_role,
            )
        return assignment

    def copy(self) -> "GameConfig":
        """深拷一份（rules/flags 都是新的）。

        必须走 `replace`：逐字段列举的那版漏过 `rules_shown_in_setup`，copy 之后静默变 False，
        于是引擎又把 9 条规则重播一遍。`replace` 会跟着字段表自动带上以后新增的开关。
        """
        return replace(self, counts=dict(self.counts), flags=RuleFlags(**vars(self.flags)))


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    blurb: str
    config: GameConfig


def _flags(**kw) -> RuleFlags:
    base = RuleFlags()
    for key, value in kw.items():
        if key not in RuleFlags.__dataclass_fields__:
            raise KeyError(f"未知规则开关: {key}")
        setattr(base, key, value)
    return base


PRESETS: list[Preset] = [
    Preset(
        id="A", label="6人·入门", blurb="最快一局，练验人与带队",
        config=GameConfig(
            label="6人·入门", n_seats=6,
            counts={WOLF: 2, SEER: 1, VILLAGER: 3},
            flags=_flags(win_condition=WIN_CITY),
        ),
    ),
    Preset(
        id="B", label="8人·新手", blurb="加入女巫，练用药时机",
        config=GameConfig(
            label="8人·新手", n_seats=8,
            counts={WOLF: 3, SEER: 1, WITCH: 1, VILLAGER: 3},
            flags=_flags(win_condition=WIN_CITY),
        ),
    ),
    Preset(
        id="C", label="9人·标准", blurb="经典预女猎，练推人与开枪",
        config=GameConfig(
            label="9人·标准", n_seats=9,
            counts={WOLF: 3, SEER: 1, WITCH: 1, HUNTER: 1, VILLAGER: 3},
            flags=_flags(win_condition=WIN_EDGE),
        ),
    ),
    Preset(
        id="D", label="12人·竞技", blurb="长局，含狼王与白痴，练发言结构与站边",
        config=GameConfig(
            label="12人·竞技", n_seats=12,
            counts={WOLF: 3, WOLF_KING: 1, SEER: 1, WITCH: 1, HUNTER: 1, IDIOT: 1, VILLAGER: 4},
            flags=_flags(win_condition=WIN_EDGE),
        ),
    ),
]


def preset_by_id(pid: str) -> Preset:
    for preset in PRESETS:
        if preset.id == pid:
            return preset
    raise KeyError(f"没有板子 {pid!r}")


def preset_choices() -> list[str]:
    return [f"{p.id} · {p.label}（{p.blurb}）" for p in PRESETS] + ["自定义板子"]


def parse_preset_choice(text: str) -> Preset | None:
    """把法官按钮文本解析回 Preset；返回 None 表示自定义。"""
    text = text.strip()
    if "自定义" in text:
        return None
    head = text.split("·")[0].strip().upper()
    if head in {"A", "B", "C", "D"}:
        return preset_by_id(head)
    for preset in PRESETS:
        if preset.label in text:
            return preset
    return None


def default_config() -> GameConfig:
    cfg = preset_by_id("C").config.copy()
    cfg.validate()
    return cfg
