"""玩家决策的数据结构（同时作为 LLM 的结构化输出 schema）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


# 夜里狼队表决的两条数：卡片文案、答案 schema、引擎结算、发言提示都要说同一个数，
# 所以定义在这里（最叶子的那个模块），而不是各写一遍字面量。
WOLF_VOTE_ROUNDS = 3       # 最多投几轮；投不到唯一最高票就空刀
WOLF_REASON_LIMIT = 14     # 给狼队友那句理由的字数上限，超了直接截，不许写成小作文


class WolfProposal(BaseModel):
    reason: str = Field(default="", description=f"给狼队友看的一句话，不超过 {WOLF_REASON_LIMIT} 字；不想说就留空")
    target: int | None = Field(default=None, description="你想击杀的座位号，None 表示空刀")


class WitchAction(BaseModel):
    save: bool = Field(default=False, description="是否使用解药")
    poison_target: int | None = Field(default=None, description="使用毒药的座位号，None 表示不用毒")
    reason: str = Field(default="", description="你的用药理由（只有你自己知道）")


class SeerCheck(BaseModel):
    target: int = Field(description="你要查验的座位号")
    reason: str = Field(default="", description="查验理由")


class Stance(BaseModel):
    suspected: list[int] = Field(default_factory=list, description="你目前认为最像狼人的座位")
    support: int | None = Field(default=None, description="你倾向站边/信任的座位")
    note: str = Field(default="", description="一句话记录你现在的立场")


class Speech(BaseModel):
    text: str = Field(description="你要在桌上说的话，不要出现座位号前缀")
    stance: Stance = Field(default_factory=Stance, description="你内心的真实立场（不会公开）")


class Candidacy(BaseModel):
    run: bool = Field(description="是否举手参选警长")
    reason: str = Field(default="", description="参选或不参选的理由")


class Withdraw(BaseModel):
    withdraw: bool = Field(default=False, description="是否退水（退出竞选）")


class Ballot(BaseModel):
    target: int | None = Field(default=None, description="你投票放逐的座位号，None 表示弃票")


class Transfer(BaseModel):
    target: int | None = Field(default=None, description="警徽移交给谁，None 表示撕警徽")


class Shot(BaseModel):
    target: int | None = Field(default=None, description="开枪带走的座位号，None 表示不开枪")


class SpeakOrder(BaseModel):
    direction: int = Field(description="发言方向：1=顺时针（座位号递增），-1=逆时针（座位号递减）")
