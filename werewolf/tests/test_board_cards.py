"""看板卡片（`HTMLInfoEvent`）：一个时机一块、降级不丢信息、动态文本必须转义。

这里刻意**两种模式都跑**：
- `html` 模式验卡片结构、标题栏、条形/配色是否成形；
- `text` 模式验降级（老版本 xun 与 CLI 看到的）；
- 两者块数必须一致 —— 升级显示层不许把播报拆细。
"""
from __future__ import annotations

import os

from ..engine.events import (
    K_EXILE, K_SHERIFF_VOTE, K_VOTE, K_VOTE_RESULT, Event,
)
from ..ui import html as H, render
from ..ui.host import HostPresenter
from . import harness
from .test_channels import _make_agent, _speech, _state


class _Mode:
    """临时强制显示模式（`html` 需要 xun 支持，测试环境里用的是源码版 xun）。"""

    def __enter__(self) -> str:
        self._old = os.environ.get("WEREWOLF_UI")
        os.environ["WEREWOLF_UI"] = "html"
        return "html"

    def __exit__(self, *exc) -> None:
        if self._old is None:
            os.environ.pop("WEREWOLF_UI", None)
        else:
            os.environ["WEREWOLF_UI"] = self._old


def _script(host: HostPresenter) -> None:
    """同一个脚本喂给两种模式：阶段头 + 日志 + 投票明细 + 结果 + 放逐。"""
    state = _state()
    host.state = state
    # payload 照引擎实际发出的形状（`voter` / `target`）：看板只认这两个键，
    # 以前这里只塞 `seat`，全靠渲染端正则读文案才对得上
    votes = [Event(kind=K_VOTE, audience="public", text=f"{s}号 投票给 {t}号",
                   payload={"voter": s, "target": t}, day=1)
             for s, t in [(1, 3), (2, 3), (4, 5)]]
    host.phase(state, "第 1 天 · 发言", "顺序发言")
    host.on_events(state, [_speech(state, 2, "我先听一圈")])
    host.phase(state, "第 1 天 · 投票", "投票放逐一名玩家")
    host.on_events(state, votes + [
        Event(kind=K_VOTE_RESULT, audience="public", text="3号 以最高票被放逐", payload={}, day=1)])
    host.on_events(state, [
        Event(kind=K_EXILE, audience="public", text="3号 离场", payload={"seat": 3}, day=1)])
    host.cleanup()


def _modes() -> list[str]:
    out = []
    for mode in ("text", "html"):
        os.environ["WEREWOLF_UI"] = mode
        if mode == "html" and not H.html_supported():
            continue
        out.append(mode)
    os.environ.pop("WEREWOLF_UI", None)
    return out


# --------------------------------------------------------------------------- 一致性
def test_block_count_is_the_same_in_both_modes():
    """升级显示层不许把播报拆细：两种模式的块数必须一致。"""
    counts: dict[str, int] = {}
    for mode in _modes():
        os.environ["WEREWOLF_UI"] = mode
        host = HostPresenter(_make_agent())
        try:
            _script(host)
        finally:
            host.cleanup()
        counts[mode] = len(harness.info_blocks(host.display.events))
    assert counts, "没有可跑的模式"
    assert len(set(counts.values())) == 1, counts
    assert counts[next(iter(counts))] >= 2, counts


def test_degradation_keeps_every_key_line():
    """降级（纯文本）不许丢信息：两模式的关键行都在，且文本形态一致。"""
    per_mode: dict[str, str] = {}
    for mode in _modes():
        os.environ["WEREWOLF_UI"] = mode
        host = HostPresenter(_make_agent())
        try:
            _script(host)
        finally:
            host.cleanup()
        per_mode[mode] = "\n".join(harness.info_blocks(host.display.events))
    joined = per_mode["text"]
    for needle in ("存活", "第 1 天 · 发言", "第 1 天 · 投票", "🗳", "1号→3号", "被放逐"):
        assert needle in joined, needle
    if "html" in per_mode:
        # 卡片形态里 `🗳` 这种文本装饰换成了「得票」标签 + 票条，内容本身一条不少
        for needle in ("存活", "第 1 天 · 投票", "得票", "1号→3号", "被放逐"):
            assert needle in per_mode["html"], needle


# --------------------------------------------------------------------------- 卡片结构
def test_phase_block_becomes_a_titled_card():
    with _Mode():
        host = HostPresenter(_make_agent())
        try:
            state = _state()
            host.phase(state, "第 1 天 · 发言", "顺序发言")
            host.on_events(state, [_speech(state, 2, "我是预言家")])
            host.cleanup()
        finally:
            host.cleanup()
        cards = [e for e in host.display.events if e.name == "HTMLInfoEvent"]
        assert cards, "html 模式下法官块应是 HTMLInfoEvent"
        titles = [c.payload.title for c in cards]
        assert any(t and "第 1 天 · 发言" in t for t in titles), titles
        body = cards[0].payload.html
        assert "存活" in cards[0].payload.to_text(), body
        assert "border-radius" in body and "background" in body, body   # 座位 chips 成形
        assert "━━" not in body                                        # 装饰行交给标题栏


def test_role_card_leads_with_the_identity_itself():
    """身份牌是整局唯一一次强调"你是谁"：第一眼必须是大字身份与阵营底色，一眼认得出。"""
    with _Mode():
        state = _state()
        seat = sorted(state.players)[0]
        player = state.players[seat]
        card = render.role_card_obj(state, seat)
        event = card.event()
        assert event.__class__.__name__ == "HTMLInfoEvent"
        assert event.title == f"身份牌 · {seat} 号 · {player.role_name}", event.title
        html = event.html
        assert "<svg" in html and f">{seat}<" in html, html            # 主视觉是卡面图像
        assert player.role_name in html, html                           # 卡上就写着这个身份
        assert "background:#" in html and "border-left-width:5px" in html, html   # 说明条
        assert "font-size:21px" not in html, html   # 卡面画过的话不再用大字说一遍
        assert "｜" not in html, html               # 文本形态的拼接说明不带到网页里
        assert "你是这一局的真人玩家" in html, html  # 网页里留的是横条没有的那一句
        text = card.text()
        assert "╔" in text and "★" in text and player.role_name in text            # 降级形态同样显眼
        assert ("狼人阵营" if player.is_wolf else "好人阵营") in text, text
        assert "板子：" in text and "你的私密信息" in text                          # 内容一条不少


def test_role_card_svg_is_shipped_and_inline_safe():
    """图像资产统一自查：角色一人一张、终局两张横幅，且**内联后不被 DOMPurify 咬掉**。

    图只能整段内联（前端不服务插件目录），于是有两条只有真踩过才知道要守的规矩：
    - **渐变 id 必须全仓库唯一**：都叫 `bg` 时同屏多张卡会串色（实际踩过 —— 女巫卡渲染成
      狼人红底），而身份牌每轮重发、同屏是常态；引用到的 id 也必须存在，否则整块底色消失。
    - **不能用 `<style>`/`<use>`/`xlink:href`/事件属性**：默认清洗配置会把它们去掉，
      图缺胳膊少腿，而 HTML 本身合法，没人会发现。
    """
    import re
    from ..engine.roles import ROLES

    for role_id in sorted(ROLES):
        assert (H.ART_DIR / f"{role_id}.svg").is_file(), f"缺身份卡：{role_id}"   # 以角色表为准
    for winner in H.BANNERS:
        assert (H.BANNER_DIR / f"{winner}.svg").is_file(), f"缺终局横幅：{winner}"

    owners: dict[str, str] = {}
    for path in sorted(H.ASSET_DIR.rglob("*.svg")):
        svg = path.read_text(encoding="utf-8")
        ids = set(re.findall(r'id="([^"]+)"', svg))
        refs = set(re.findall(r'url\(#([^)]+)\)', svg))
        assert refs and refs <= ids, (path, ids, refs)
        for own in ids:
            assert owners.setdefault(own, path.name) == path.name, f"id {own} 被两张图共用"
        for banned in ("<style", "<use", "xlink", "foreignObject", "<script",
                       "onclick", "onload"):
            assert banned not in svg, (path, banned)

    card = H.RoleCard(seat=6, role_id="witch").html()
    assert "{{SEAT}}" not in card and ">6<" in card, card      # 卡上的座位号真填进去了


def test_role_card_html_inlines_the_art():
    """发身份牌时图像要**内联**显示：`<img src="assets/...">` 前端取不到，等于没图。"""
    with _Mode():
        state = _state()
        seat = sorted(state.players)[0]
        card = render.role_card_obj(state, seat)
        html = card.event().html
        assert "<svg" in html, html
        assert f">{seat}<" in html, html                         # 卡上座位号 = 真人座位
        assert "<img" not in html and "src=" not in html, html     # 不是外链，前端拿不到
        assert "<svg" not in card.text()                          # 纯文本形态不掺图像标记


def test_vote_tally_shows_bars_and_counts():
    with _Mode():
        line = render.vote_line([("1号→3号", 3), ("2号→3号", 3), ("4号 弃票", None)])
        part = line.parts[0]
        assert isinstance(part, H.Tally)
        assert line.text == "🗳 1号→3号　2号→3号　4号 弃票"
        html = part.html()
        assert "3号" in html and "弃票" in html
        assert "2 票" in html and "1 票" in html, html
        assert "得票" in html and "width:" in html                     # 票条


def test_night_settlement_is_a_key_value_table():
    text = "[上帝视角] 第 1 夜结算：刀口 5号｜解药 未用｜毒药 未用｜查验 3号=狼人｜死亡 5号(刀)"
    parts = render.night_parts(text)
    assert len(parts) == 1 and isinstance(parts[0], H.KV)
    assert parts[0].text() == text                                     # 文本形态原样还原
    html = parts[0].html()
    assert "<table" in html and "刀口" in html and "5号(刀)" in html


def test_tally_is_printed_once_in_card_mode():
    """票型事件（带 `tally` payload）在卡片里只留 `Tally` 部件那一份。

    部件已经画了"每人一票 + 得票条"，再把原文贴进同一个块就是同一块里两遍；
    而"本轮无人被放逐"这类结果不带 tally payload，是真新增 —— 由 degradation 那条测试守住。
    """
    if "html" not in _modes():
        return
    from ..ui import render

    with _Mode():
        lines = render.render_batch(_state(), [
            Event(kind=K_VOTE, audience="public", text="1号 投票给 3号",
                  payload={"voter": 1, "target": 3}, day=1),
            Event(kind=K_VOTE, audience="public", text="2号 投票给 3号",
                  payload={"voter": 2, "target": 3}, day=1),
            Event(kind=K_VOTE_RESULT, audience="public", text="第 1 天票型：3号 2票",
                  payload={"tally": {3: 2}}, day=1)])
    merged = [ln for ln in lines if "票型" in ln.text]
    assert len(merged) == 1, lines
    again = [part.text() for part in merged[0].parts if "票型" in part.text()]
    assert not again, f"同一个块里票型出现了两遍：{again}"


def test_the_tally_counts_the_ballot_not_the_judges_wording():
    """票型看板的计数来自事件 payload，不来自法官那句措辞。

    以前明细和计数都用正则从文案里刨座位：把「投票给」换成别的说法，票就悄悄进了
    「弃票」那一栏 —— 而票型是场上最重要的一串数字。这里故意用正则认不出的措辞。
    """
    with _Mode():
        events = [Event(kind=K_VOTE, audience="public", text=f"{s}号 提名 {t}号",
                        payload={"voter": s, "target": t}, day=1)
                  for s, t in [(1, 3), (2, 3)]]
        line = render.render_batch(_state(), events)[0]
    part = line.parts[0]
    assert isinstance(part, H.Tally)
    assert ("3号", 2) in list(part.counts), list(part.counts)
    assert not any("弃票" in label for label, _n in part.counts), list(part.counts)

    with _Mode():                                        # 警徽票标出来，但仍然计在目标名下
        badge_line = render.render_batch(_state(), [
            Event(kind=K_SHERIFF_VOTE, audience="public", text="5号 把警徽投给 2号",
                  payload={"voter": 5, "target": 2}, day=1)])[0]
    assert "5号→2号（警徽）" in badge_line.text, badge_line.text
    assert ("2号", 1) in list(badge_line.parts[0].counts), list(badge_line.parts[0].counts)


def test_dynamic_text_is_escaped_not_injected():
    with _Mode():
        state = _state()
        host = HostPresenter(_make_agent())
        try:
            host.state = state
            host.phase(state, "第 1 天 · 发言")
            host.on_events(state, [Event(
                # 引擎以后新增的 kind 一律兜底成纯文本 —— 顺手把这条也验了
                kind="kind_from_the_future", audience="public",
                text="<img src=x onerror=alert(1)><script>bad()</script>", payload={}, day=1)])
            host.cleanup()
        finally:
            host.cleanup()
        html = "".join(e.payload.html for e in host.display.events if e.name == "HTMLInfoEvent")
        assert "<script>" not in html and "<img " not in html, html
        assert "&lt;script&gt;" in html and "alert(1)" in html         # 内容仍在，只是不再是标签


def test_status_command_publishes_a_public_card():
    with _Mode():
        host = HostPresenter(_make_agent())
        state = _state()
        host.state = state
        host.publish_status()
        cards = [e for e in host.display.events if e.name == "HTMLInfoEvent"]
        assert len(cards) == 1, cards
        text = cards[0].payload.to_text()
        assert "板子：" in text and "存活" in text
        # 上帝视角关着：不许出现任何未翻牌玩家的身份
        for player in state.players.values():
            if not player.role_revealed:
                assert f"{player.seat}号{player.role_name}" not in text, player.seat


def test_finished_board_reveals_everything():
    with _Mode():
        host = HostPresenter(_make_agent())
        state = _state()
        state.winner = "wolf"
        host.finished(state)
        cards = [e for e in host.display.events if e.name == "HTMLInfoEvent"]
        assert len(cards) == 1, cards
        assert "本局结束" in (cards[0].payload.title or "")
        assert "ban_wolf" in cards[0].payload.html, cards[0].payload.html   # 终局有胜负横幅
        text = cards[0].payload.to_text()
        for player in state.players.values():               # 终局全部公开
            assert f"{player.seat}号{player.role_name}" in text, player.seat


def test_winner_banner_has_its_own_text_form():
    """横幅的文本形态只占一行 —— 不许因为没图就把胜负信息丢了。"""
    for winner, label in (("wolf", "狼人阵营胜利"), ("good", "好人阵营胜利")):
        assert H.Banner(winner=winner).text() == f"★ {label} ★"
        assert f"ban_{winner}" in H.Banner(winner=winner).html()


def test_no_victory_banner_without_a_winner():
    """平局、中途终止不许发「胜利」横幅：那是对着一场没打完的局撒谎。"""
    assert H.BANNERS == ("wolf", "good")
    with _Mode():
        host = HostPresenter(_make_agent())
        state = _state()
        state.winner = "draw"
        host.finished(state)
        cards = [e for e in host.display.events if e.name == "HTMLInfoEvent"]
        assert len(cards) == 1, cards
        assert "<svg" not in cards[0].payload.html, cards[0].payload.html
        assert "胜利" not in cards[0].payload.to_text(), cards[0].payload.to_text()


def test_public_board_never_reveals_the_wolf_count():
    """公开看板不许报「场上狼几匹」，也不许出现任何身份字样。

    存活名单是公开信息，但狼还剩几匹就是身份分布 —— 好人据此不必再试探，狼也知道自己
    是最后一匹。`/werewolf status` 与法官问答都走同一个 `status_lines(god_view=False)`，
    所以这一条同时守住了那两处出口。
    """
    from ..engine.roles import ROLES
    with _Mode():
        state = _state()
        state.winner = None
        public = "\n".join(render.status_lines(state, False))
        god = "\n".join(render.status_lines(state, True))
        assert "狼" not in public, public
        assert "场上狼" in god, god
        for role_id, role in ROLES.items():
            assert role.name not in public, (role_id, public)
