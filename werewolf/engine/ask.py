"""Ask：引擎递给玩家的一切信息的唯一载体。

引擎在每个决策点构造一个 Ask（只含该座位有资格知道的内容 + 本次新增可见事件），
玩家 actor 只能看到 Ask，拿不到 GameState，因此不存在越权读取身份/他人私密信息的途径。
"""
from __future__ import annotations

from dataclasses import dataclass

from .decisions import (
    Ballot, Candidacy, SeerCheck, Shot, SpeakOrder, Speech, Transfer, WitchAction, WolfProposal, Withdraw,
)
from .events import Event
from .resolve import DIRECTION_TEXT

KIND_WOLF_TARGET = "wolf_target"
KIND_WITCH = "witch"
KIND_SEER = "seer"
KIND_CANDIDACY = "candidacy"
KIND_SHERIFF_SPEECH = "sheriff_speech"
KIND_WITHDRAW = "withdraw"
KIND_SHERIFF_VOTE = "sheriff_vote"
KIND_SPEAK_ORDER = "speak_order"
KIND_SPEECH = "speech"
KIND_VOTE = "vote"
KIND_LAST_WORDS = "last_words"
KIND_SHOT = "shot"
KIND_TRANSFER = "transfer"

#: 一个决策点的全部常识：卡片问什么、答案什么形状、字数上限取哪个 flag。
#: 以前这些抄在四处（`KIND_LABEL`、`LIMIT_FIELD`、`human` 的 13 个方法、
#: `llm_player.SCHEMA_FOR`），改一句文案要跳四个文件。
@dataclass(frozen=True)
class Kind:
    id: str
    label: str            # 卡片标题里的事项名
    schema: type          # 决策模型，同时是 LLM 的结构化输出 schema
    shape: str            # seat（选个座位）/ bool / direction / text（说话）/ witch
    method: str           # FakeActor 那侧的方法名：测试按环节 patch，脚本玩家靠它
    question: str = ""    # 人类卡片上的问题
    none_label: str = ""  # 可以「不选」时那个选项的文案
    yes: str = ""
    no: str = ""
    field: str = "target"  # bool 类把答案写进哪个字段
    limit: str = ""        # 发言上限读 RuleFlags 的哪个字段（空 = 没有上限）
    no_none: bool = False  # 不允许空着（预言家）


KINDS = {k.id: k for k in (
    Kind(KIND_WOLF_TARGET, "狼人行动：选择击杀目标", WolfProposal, "seat", "wolf_target",
         question="今晚你想击杀谁？（你看不到队友投谁）", none_label="空刀（今晚不杀人）"),
    Kind(KIND_WITCH, "女巫行动：用药", WitchAction, "witch", "witch_action"),
    Kind(KIND_SEER, "预言家行动：查验", SeerCheck, "seat", "seer_check",
         question="今晚你想查验谁？", no_none=True),
    Kind(KIND_CANDIDACY, "警长竞选：是否举手", Candidacy, "bool", "candidacy",
         question="你要举手参选警长吗？", yes="参选", no="不参选", field="run"),
    Kind(KIND_SHERIFF_SPEECH, "警上发言", Speech, "text", "sheriff_speech",
         question="轮到你做警上发言（拉票或立警告）。", limit="sheriff_word_limit"),
    Kind(KIND_WITHDRAW, "警长竞选：是否退水", Withdraw, "bool", "withdraw",
         question="你要退水吗？", yes="退水", no="继续竞选", field="withdraw"),
    Kind(KIND_SHERIFF_VOTE, "投票选警徽", Ballot, "seat", "sheriff_vote",
         question="你把警徽投给谁？", none_label="弃票"),
    Kind(KIND_SPEAK_ORDER, "警长决定发言方向", SpeakOrder, "direction", "speak_order",
         question="今天往哪个方向发言？", yes="顺时针（座位号递增）", no="逆时针（座位号递减）"),
    Kind(KIND_SPEECH, "白天发言", Speech, "text", "speech",
         question="轮到你发言。", limit="speech_word_limit"),
    Kind(KIND_VOTE, "投票放逐", Ballot, "seat", "vote",
         question="你投票放逐谁？", none_label="弃票"),
    Kind(KIND_LAST_WORDS, "遗言", Speech, "text", "last_words",
         question="请留下你的遗言。", limit="speech_word_limit"),
    Kind(KIND_SHOT, "开枪", Shot, "seat", "shot",
         question="你要开枪带走谁？", none_label="放弃开枪"),
    Kind(KIND_TRANSFER, "警徽移交", Transfer, "seat", "transfer",
         question="警徽交给谁？", none_label="撕毁警徽"),
)}


def kind_label(kind: str) -> str:
    """环节的中文名。未登记的 kind 原样返回（心跳文案那类会传进别的字样）。"""
    spec = KINDS.get(kind)
    return spec.label if spec else kind


def word_limit_for(kind: str, flags: object) -> int:
    """这个环节的发言上限（0 = 没有上限）。

    只有「要说话」的环节才有上限：举手 / 退水这类是非题被塞上限，模型就收到一句
    无意义的「发言长度上限 500 字」。提示词、真人卡片、引擎记超限都从这一处取。
    """
    field = KINDS[kind].limit
    return int(getattr(flags, field, 0) or 0) if field else 0


@dataclass(frozen=True)
class Ask:
    kind: str
    day: int
    phase: str
    seat: int
    identity: str = ""                      # 自己的身份（只有这份 Ask 里有）
    teammates: tuple[int, ...] = ()         # 狼队友
    pool: tuple[int, ...] = ()              # 可选目标
    save_pool: tuple[int, ...] = ()         # 女巫：解药这一夜能救的人（空 = 救不了，别问）
    must_choose: bool = True
    note: str = ""                          # 法官提示，如 "今夜倒牌的是 5 号"
    word_limit: int = 0
    delta: tuple[Event, ...] = ()           # 自上次决策以来的新增可见事件
    dossier: tuple[str, ...] = ()           # 私密档案（查验/用药/狼队历史/立场）
    recap: tuple[str, ...] = ()             # 公开前情提要（已死亡、已公开身份、警徽、票型）
    speeches: tuple[tuple[int, str], ...] = ()   # 本轮已发生的发言（同阶段）
    persona: object = None
    order: tuple[int, ...] = ()            # 本轮发言顺序（只有发言类询问会给）
    direction: int = 0                     # 本轮发言方向，0 = 不适用（比如警上竞选）

    @property
    def label(self) -> str:
        return kind_label(self.kind)

    def pool_text(self) -> str:
        if not self.pool:
            return "无"
        return "、".join(f"{s}号" for s in self.pool)

    def position_text(self) -> str:
        """本轮这条发言链上我在哪儿：方向、排第几、前面谁已经交代了、后面还剩谁。

        方向和顺序都是法官当众播报过的公开信息，这一行只是替玩家省掉「在脑子里数座位」，
        并让他清楚自己是在前面开荒、还是听着别人说完才上台、或者直接压轴归票。
        """
        order = [int(s) for s in self.order]
        if len(order) < 2 or self.seat not in order:
            return ""
        index = order.index(self.seat)
        spoken = {int(s) for s, _words in self.speeches}
        ahead = "、".join(f"{s}号" for s in order[:index] if s in spoken)
        behind = "、".join(f"{s}号" for s in order[index + 1:])
        way = DIRECTION_TEXT.get(self.direction)
        head = f"{way}发言" if way else "本轮发言"
        last = "；你是最后一个发言（归票位）" if index == len(order) - 1 else ""
        return (f"发言位置：{head}，你排第 {index + 1}/{len(order)}"
                + (f"；前面已发言：{ahead}" if ahead else "；你是本轮第一个开口的")
                + (f"；你之后还有：{behind}" if behind else "")
                + last + "。")



