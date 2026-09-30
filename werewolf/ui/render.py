"""把事件流渲染成**纯文本**通道（前端 InfoEvent 是 plain-text，markdown 不生效）。

两类出口（见 `ui/host.py`）：
- `author=None` → 法官的系统播报，走 InfoEvent（前端是左侧竖线的灰色小字），所以这里刻意
  写成紧凑的纯文本行，不用任何 markdown 语法。
- `author="3号"` → 游戏者的话，直接以该座位为作者发 ModelMessageEvent（前端是正常字号的
  聊天气泡，markdown 生效）。复盘报告同理，作者是「复盘教练」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..engine.events import (
    Event, K_BADGE_DESTROYED, K_BADGE_TRANSFER, K_DEATH_ANNOUNCE, K_DEATH_CAUSE,
    K_EXILE, K_IDIOT_REVEAL, K_LAST_WORDS, K_NIGHT_ACTION, K_NIGHT_RESULT, K_ROLE_ASSIGNED, K_SHOT, K_SHERIFF_ELECTED, K_SHERIFF_SPEECH, K_SHERIFF_VOTE, K_SPEECH,
    K_VOTE, K_VOTE_RESULT, K_WIN, K_WOLF_VOTE,
)
from ..engine.roles import role_by_id
from ..engine.state import GameState
from . import html as H

#: 游戏者的话（以座位为作者发成聊天气泡）
SPEECH_KINDS = {K_SPEECH: "", K_SHERIFF_SPEECH: "警上", K_LAST_WORDS: "遗言"}
#: 只有开了上帝视角才播的
GOD_VIEW_KINDS = {K_NIGHT_RESULT, K_WOLF_VOTE}
#: 私密细节（夜里谁动了什么、死因），由 announceable 负责过滤
PRIVATE_KINDS = {K_NIGHT_ACTION, K_DEATH_CAUSE, K_ROLE_ASSIGNED}
#: 连着发的票合并成一行，省屏幕
GROUP_KINDS = {K_VOTE, K_SHERIFF_VOTE}


@dataclass(frozen=True)
class Line:
    """一行播报：`author` 为空就是法官的系统播报，否则是某个游戏者说的话。

    `parts` 非空表示这一行其实是块**看板**（座位/票型/夜结算）：法官会把它并进当前块的
    `html.Card`，浏览器里是对齐着色的卡片；纯文本形态仍然用 `text`（CLI、老版本 xun、
    单测断言走的都是它），所以两边内容一致。
    """

    text: str
    author: str | None = None
    parts: tuple[H.Part, ...] = ()


def plainify(text: str) -> str:
    """兜底：把调用点残留的 markdown 变成纯文本可读形式。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = line.replace("**", "").replace("__", "")
        line = re.sub(r"^\s*\|(.+)\|\s*$", lambda m: " · ".join(c.strip() for c in m.group(1).split("|") if c.strip()), line)
        line = re.sub(r"^\s*[-*]\s+", "· ", line)
        line = re.sub(r"`+", "", line)
        out.append(line)
    return "\n".join(out).strip()


def build_board_lines(cfg) -> list[str]:
    """板子摘要，3~4 行紧凑纯文本。

    法官通道（InfoEvent）在前端不渲染 markdown，用表格会碎成十几行；这里一行的信息量够读，
    也不会挤掉真正的播报。`/werewolf status` 和开局配置共用。
    """
    flags = cfg.flags
    win, lw = flags.win_short, flags.last_words_short
    lines = [
        f"板子：{cfg.n_seats}人 · {cfg.role_names()} · {win}",
        f"警长：{flags.sheriff_text}",
        f"判定：{win}｜{lw}｜女巫{'可' if flags.witch_self_save else '不可'}自救"
        f"｜发言上限 普通 {flags.speech_word_limit} 字 / 警上 {flags.sheriff_word_limit} 字",
        f"观战：你出局后{'开上帝视角（能看到所有身份与夜间行动）' if flags.spectator_god_view else '只看公开信息'}",
    ]
    return lines


def _faction_tone(player) -> str:
    """上帝视角的身份配色（活人看不到身份，所以配色也不该泄密）。"""
    if not player.alive:
        return "dead"
    return "wolf" if player.is_wolf else ("god" if player.is_god else "villager")


def status_parts(state: GameState, god_view: bool) -> list[H.Part]:
    """局面摘要 → 看板 Parts。**文本形态与今天的 `status_lines` 逐行一致**。

    法官通道里每多一行就多一倍视觉重量，所以能并的并掉。
    """
    alive = state.alive_seats()
    holder = state.badge_holder()
    badge = f"｜警徽 {holder}号" if holder else ""
    # 「场上狼几匹」**只有上帝视角才给**。存活名单本身是公开的，但狼还剩几匹等于把场上的
    # 身份分布直接念出来：好人据此知道狼只剩一匹就不必再试探，狼也知道自己已是最后一匹。
    # 公开形态只报公开事实：存活几人、警徽在谁手上。
    tones = [(str(s), "badge" if s == holder else "alive") for s in alive]
    parts: list[H.Part] = [H.Chips(
        prefix=f"存活 {len(alive)} 人 ", items=tones, brackets=True,
        suffix=(f"｜场上狼 {len(state.wolves_alive())} 匹{badge}" if god_view else badge))]

    dead = sorted([p for p in state.players.values() if not p.alive], key=lambda p: p.seat)
    if dead:
        parts.append(H.Chips(prefix="已出局 ", sep="、",
                             items=[(f"{p.seat}号(第{p.died_on_day or '?'}天)", "dead")
                                    for p in dead]))
    flipped = sorted([p for p in state.players.values() if p.role_revealed], key=lambda p: p.seat)
    if flipped:
        parts.append(H.Chips(prefix="已翻牌 ", sep="、",
                             items=[(f"{p.seat}号={p.role_name}", "muted") for p in flipped]))
    if god_view:
        parts.append(H.Chips(
            prefix="身份 ",
            items=[(f"{p.seat}号{p.role_name}{'(活)' if p.alive else '(死)'}", _faction_tone(p))
                   for p in sorted(state.players.values(), key=lambda p: p.seat)]))
    return parts


def status_lines(state: GameState, god_view: bool) -> list[str]:
    return [part.text() for part in status_parts(state, god_view)]


def phase_title(title: str, subtitle: str = "") -> str:
    """阶段块的标题（前端标题栏用；文本形态由 `html.Card` 加 `━━` 装饰）。"""
    tail = f"　（{plainify(subtitle)}）" if subtitle else ""
    return f"{plainify(title)}{tail}"


def board_summary(state: GameState | None, god_view: bool = False) -> str:
    """公开局面摘要。法官问答、`/werewolf status` 共用这一份（永远不含未公开信息）。"""
    if state is None:
        return "游戏尚未开始。"
    lines = build_board_lines(state.config)
    lines.append(f"局面：第 {state.day} 天 · 阶段 {state.phase}")
    lines += status_lines(state, god_view)
    recent = [e for e in state.log if e.audience == "public" and e.kind in SPEECH_KINDS][-6:]
    if recent:
        lines.append("最近发言：")
        lines += ["· " + e.text for e in recent]
    return "\n".join(lines)


def _vote_pair(event: Event) -> tuple[str, int | None]:
    """一条投票明细 → (排好版的「3号→6号」, 目标座位)。座位取自事件 payload。

    以前这里和 `vote_line` 都用正则从法官的**措辞**里刨座位：把「投票给」改写成别的说法，
    看板上的票就悄悄记到「弃票」头上 —— 而票型是场上最重要的一串数字。
    """
    target = event.payload.get("target")
    if target is None:
        return event.text, None                     # 弃票：法官怎么写就怎么念（PK 那圈带前缀）
    voter = event.payload.get("voter", "?")
    tag = "（警徽）" if event.kind == K_SHERIFF_VOTE else ""
    return f"{voter}号→{target}号{tag}", int(target)


def speech_author(seat: int | str, kind: str) -> str:
    """发言气泡的作者名。法官提前挂"正在工作"标记时也必须用它，两边才不会错位。"""
    label = SPEECH_KINDS.get(kind, "")
    return f"{seat}号" + (f"（{label}）" if label else "")


def _speech(event: Event) -> Line:
    seat = event.payload.get("seat", "?")
    body = event.text.split("：", 1)[-1] if "：" in event.text else event.text
    return Line(text=body, author=speech_author(seat, event.kind))


def _one(event: Event) -> Line:
    if event.kind in SPEECH_KINDS:
        return _speech(event)
    if event.kind in GOD_VIEW_KINDS:
        text = f"[上帝视角] {event.text}"
        parts = night_parts(text) if event.kind == K_NIGHT_RESULT else ()
        return Line(text=text, parts=parts)
    if event.kind in (K_DEATH_ANNOUNCE, K_EXILE, K_SHOT, K_IDIOT_REVEAL, K_WIN,
                      K_VOTE_RESULT, K_SHERIFF_ELECTED, K_BADGE_TRANSFER, K_BADGE_DESTROYED):
        return Line(text="▶ " + event.text)
    if event.kind in PRIVATE_KINDS:
        return Line(text="· " + event.text)
    return Line(text=plainify(event.text))


def vote_line(votes: Sequence[tuple[str, int | None]]) -> Line:
    """投票明细 → 一行文本 + 票型看板（得票条形，按**人数**，不是加权票数）。

    每一项是 `(排好版的明细, 目标座位)`；计数只看目标座位，不看文案。
    """
    pairs = [label for label, _target in votes]
    counts: dict[str, int] = {}
    for _label, target in votes:
        key = f"{target}号" if target is not None else "弃票"
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return Line(
        text="🗳 " + "　".join(pairs),
        parts=(H.Tally(pairs=list(pairs), counts=ordered, total=len(pairs)),))


def night_parts(text: str) -> tuple[H.Part, ...]:
    """`[上帝视角] 第 1 夜结算：刀口 5号｜解药 未用｜…` → KV 看板。

    文本形态原样还原（`k v` 用｜连），所以降级/CLI 与今天完全一致。
    """
    head, sep, body = (text or "").partition("：")
    if not sep or "｜" not in body:
        return ()
    rows = []
    for segment in body.split("｜"):
        key, blank, value = segment.partition(" ")
        rows.append((key.strip(), value.strip() if blank else ""))
    return (H.KV(rows=rows, head=head + "：", emphasize="死亡"),)


def setup_parts(cfg, human_seat: int | None = None, seats: Sequence[int] = ()) -> list[H.Part]:
    """板子/判定/发言上限（开局与 `/werewolf status` 共用）。"""
    parts: list[H.Part] = [H.Log(build_board_lines(cfg), small=True)]
    if seats:
        parts.append(H.Chips(
            prefix="座位 ",
            items=[(f"{s}号" + ("（你）" if s == human_seat else ""),
                    "badge" if s == human_seat else "muted") for s in seats]))
    return parts


def role_card_obj(state: GameState, seat: int) -> H.Card:
    """身份牌：整局唯一一次强调"你是谁"，所以第一眼就是大字身份牌，板子规则往后排。

    真人得在一片灰色法官播报里**不用读就知道**自己是几号、什么身份、哪一阵营 ——
    认错身份的一局就白打了。文本形态（老版本 xun / CLI）同样带 `╔═╗` 框与 `★`，内容一条不少。
    """
    player = state.players[seat]
    role = role_by_id(player.role_id)
    tone = "wolf" if player.is_wolf else ("god" if player.is_god else "villager")
    parts: list[H.Part] = [
        H.RoleCard(seat=seat, role_id=player.role_id, role_name=player.role_name,
                   tone=tone, faction="狼人阵营" if player.is_wolf else "好人阵营",
                   note=f"{role.public_prompt.rstrip('。')}｜你是这一局的真人玩家",
                   hint="你是这一局的真人玩家。",
        ),
        H.Log([f"板子：{state.config.n_seats} 人 · {state.config.role_names()}"]),
        H.Log(["· " + line for line in state.config.flags.summary_lines()], small=True),
    ]
    if player.is_wolf:
        pack = sorted(s for s, p in state.players.items() if p.is_wolf and s != seat)
        parts.append(H.Chips(
            prefix="狼队友 ",
            items=[(f"{s}号", "wolf") for s in pack] or [("就你一个", "muted")]))
        parts.append(H.Log(["白天装作好人，夜里和队友一起定刀口。"], small=True))
    else:
        parts.append(H.Log(["你是好人阵营，不知道谁是狼人。"], small=True))
    parts.append(H.Log(["你的私密信息只有你自己知道；说什么、说多少，按你自己的判断来。"], small=True))
    return H.Card(title=f"身份牌 · {seat} 号 · {player.role_name}", parts=parts)


def render_batch(state: GameState, events: Sequence[Event]) -> list[Line]:
    """一批事件 → 若干行播报（连着的投票合并，避免刷屏）。"""
    lines: list[Line] = []
    pending_votes: list[str] = []

    def flush_votes(result_text: str | None = None, repeats_tally: bool = False) -> None:
        if not pending_votes:
            return
        pairs, pending_votes[:] = pending_votes[:], []
        if result_text is None:
            lines.append(vote_line(pairs))
            return
        merged = vote_line(pairs)
        # 票型事件（引擎给它挂了 `tally` payload）的内容，`Tally` 部件已经画成"每人一票 + 得票条"，
        # HTML 形态不再贴一份文本副本；而"本轮无人被放逐"这类结果不带 tally payload，是真新增，必须留。
        parts = merged.parts if repeats_tally else merged.parts + (H.Log([f"▶ {result_text}"]),)
        lines.append(Line(text=merged.text + "\n▶ " + result_text, parts=parts))

    for event in events:
        if not state.announceable(event):
            continue
        if event.kind in GROUP_KINDS:
            pending_votes.append(_vote_pair(event))
            continue
        if event.kind in (K_VOTE_RESULT, K_SHERIFF_ELECTED) and pending_votes:
            # 票型 + 结果同一块；带 tally payload 的就是票型本身，HTML 形态只留部件那一份
            flush_votes(plainify(event.text), repeats_tally=bool(event.payload.get("tally")))
            continue
        flush_votes()                               # 没有明细可并时，结果自己成行
        lines.append(_one(event))
    flush_votes()
    return lines
