"""角色定义：阵营、类别、技能钩子标记，以及给 LLM 看的规则文案。"""
from __future__ import annotations

from dataclasses import dataclass

FACTION_WOLF = "wolf"
FACTION_GOOD = "good"

CAT_WOLF = "wolf"
CAT_GOD = "god"
CAT_VILLAGER = "villager"



@dataclass(frozen=True)
class Role:
    id: str
    name: str
    faction: str
    category: str
    shot: bool = False
    survive_exile: bool = False
    self_prompt: str = ""
    public_prompt: str = ""


WOLF = "wolf"
WOLF_KING = "wolf_king"
SEER = "seer"
WITCH = "witch"
HUNTER = "hunter"
IDIOT = "idiot"
VILLAGER = "villager"

ROLES: dict[str, Role] = {
    WOLF: Role(
        id=WOLF, name="狼人", faction=FACTION_WOLF, category=CAT_WOLF,
        self_prompt=(
            "你是狼人。夜晚与狼队友共同决定击杀一名玩家（狼队友名单会告诉你，"
            "白天必须装作好人，不能自曝身份，除非自曝对狼队明显有利）。"
        ),
        public_prompt="狼人：每晚与同伴共同击杀一人。",
    ),
    WOLF_KING: Role(
        id=WOLF_KING, name="狼王", faction=FACTION_WOLF, category=CAT_WOLF,
        shot=True,
        self_prompt=(
            "你是狼王。夜晚参与狼队击杀；当你被狼人击杀或被投票放逐时，"
            "可以开枪带走一名存活玩家（被女巫毒杀则不能开枪）。"
        ),
        public_prompt="狼王：死亡时可带走一人，被毒不能开枪。",
    ),
    SEER: Role(
        id=SEER, name="预言家", faction=FACTION_GOOD, category=CAT_GOD,
        self_prompt=(
            "你是预言家。每晚查验一名玩家的阵营（好人/狼人）。"
            "白天你需要判断是否跳身份带队：警徽与查验信息只有说出来才有价值，"
            "但说出来就会成为狼队刀口。"
        ),
        public_prompt="预言家：每晚查验一人阵营。",
    ),
    WITCH: Role(
        id=WITCH, name="女巫", faction=FACTION_GOOD, category=CAT_GOD,
        self_prompt=(
            "你是女巫。全场各一瓶解药、一瓶毒药，每晚最多使用一瓶，两瓶都用完后"
            "你不再知道刀口。默认解药不能自救。你只在心里知道用药信息，"
            "是否公开由你自己决定。"
        ),
        public_prompt="女巫：一瓶解药一瓶毒药，每晚最多用一瓶。",
    ),
    HUNTER: Role(
        id=HUNTER, name="猎人", faction=FACTION_GOOD, category=CAT_GOD,
        shot=True,
        self_prompt=(
            "你是猎人。死亡时可以开枪带走一名存活玩家（被女巫毒杀不能开枪）。"
            "被放逐时先留遗言再开枪；被夜杀时开枪在公布死讯之后。"
        ),
        public_prompt="猎人：死亡时可带走一人，被毒不能开枪。",
    ),
    IDIOT: Role(
        id=IDIOT, name="白痴", faction=FACTION_GOOD, category=CAT_GOD,
        survive_exile=True,
        self_prompt=(
            "你是白痴。被投票放逐时翻开身份牌免于死亡，但此后永久失去投票权"
            "（仍可发言），且你的身份当场公开。若你是警长，翻牌后必须把警徽移交他人。"
        ),
        public_prompt="白痴：被放逐时翻牌免死，之后失去投票权。",
    ),
    VILLAGER: Role(
        id=VILLAGER, name="村民", faction=FACTION_GOOD, category=CAT_VILLAGER,
        self_prompt="你是村民，没有技能。靠发言与投票找出狼人。",
        public_prompt="村民：无技能。",
    ),
}


def role_by_id(role_id: str) -> Role:
    try:
        return ROLES[role_id]
    except KeyError:
        raise KeyError(f"未知角色: {role_id!r}") from None


def is_wolf(role_id: str) -> bool:
    return role_by_id(role_id).faction == FACTION_WOLF



