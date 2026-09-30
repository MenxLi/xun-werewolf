"""纯函数结算测试。"""
from __future__ import annotations

from werewolf.engine.config import GameConfig, RuleFlags, WIN_CITY, WIN_EDGE
from werewolf.engine.resolve import check_winner, next_start_seat, plurality, resolve_night, seat_after
from werewolf.engine.state import GameState, Player


def _state(spec, flags=None, alive=None, sheriff=None):
    """spec: {座位: 角色}。"""
    flags = flags or RuleFlags()
    counts: dict[str, int] = {}
    for role in spec.values():
        counts[role] = counts.get(role, 0) + 1
    cfg = GameConfig(n_seats=len(spec), counts=counts, flags=flags)
    players = {seat: Player(seat=seat, role_id=role) for seat, role in spec.items()}
    st = GameState(config=cfg, players=players)
    keep = set(alive if alive is not None else list(spec))
    for seat, player in players.items():
        if seat not in keep:
            player.alive = False
            player.died_on_day = 1
    if sheriff is not None:
        st.sheriff = sheriff
    return st


def test_night_knife_only():
    assert resolve_night(3, None, None) == [(3, "knife")]


def test_night_saved():
    assert resolve_night(3, 3, None) == []


def test_night_poison_plus_knife():
    assert resolve_night(3, None, 5) == [(3, "knife"), (5, "poison")]


def test_night_poison_same_as_knife_no_double_death():
    deaths = resolve_night(3, None, 3)
    assert deaths == [(3, "knife")], deaths


def test_night_save_but_poisoned():
    assert resolve_night(3, 3, 3) == [(3, "poison")]


def test_plurality_simple():
    winners, tally = plurality({1: 3, 2: 3, 3: 4})
    assert winners == [3], winners
    assert tally == {3: 2.0, 4: 1.0}, tally


def test_plurality_tie():
    winners, tally = plurality({1: 3, 2: 4})
    assert winners == [3, 4] and tally == {3: 1.0, 4: 1.0}, (winners, tally)


def test_plurality_sherrif_weight():
    winners, tally = plurality({1: 5, 2: 4, 3: 4}, weights={1: 1.5})
    assert winners == [4] and tally == {4: 2.0, 5: 1.5}, (winners, tally)
    winners2, tally2 = plurality({1: 5, 2: 5, 3: 4}, weights={1: 1.5})
    assert winners2 == [5] and tally2[5] == 2.5, (winners2, tally2)


def test_plurality_all_abstain():
    winners, tally = plurality({1: None, 2: None})
    assert winners == [] and tally == {}


def test_win_edge_gods_dead():
    st = _state({1: "wolf", 2: "wolf", 3: "seer", 4: "villager", 5: "villager", 6: "villager"},
                RuleFlags(win_condition=WIN_EDGE), alive=[1, 2, 4, 5, 6])
    assert check_winner(st) == "wolf"


def test_win_edge_villagers_dead():
    st = _state({1: "wolf", 2: "wolf", 3: "seer", 4: "villager", 5: "villager", 6: "witch"},
                RuleFlags(win_condition=WIN_EDGE), alive=[1, 2, 3, 6])
    assert check_winner(st) == "wolf"


def test_win_good_when_wolves_dead():
    st = _state({1: "wolf", 2: "wolf", 3: "seer", 4: "villager", 5: "villager"},
                RuleFlags(win_condition=WIN_EDGE), alive=[3, 4, 5])
    assert check_winner(st) == "good"


def test_win_city_only_all_good_dead():
    st = _state({1: "wolf", 2: "wolf", 3: "seer", 4: "villager", 5: "villager"},
                RuleFlags(win_condition=WIN_CITY), alive=[1, 2, 3])
    assert check_winner(st) == ""
    st.players[3].alive = False
    assert check_winner(st) == "wolf"


def test_win_not_over_yet():
    st = _state({1: "wolf", 2: "wolf", 3: "seer", 4: "villager", 5: "villager", 6: "hunter"},
                RuleFlags(win_condition=WIN_EDGE), alive=[1, 2, 3, 4, 5])
    assert check_winner(st) == ""


def test_speaker_start_rotation_wraps():
    assert next_start_seat(9, 9) == 1
    assert next_start_seat(3, 9) == 4


def test_seat_after_wraps():
    assert seat_after(9, 9) == 1
    assert seat_after(9, 9, 3) == 3


def test_vote_weight_only_for_alive_sheriff():
    st = _state({1: "wolf", 2: "wolf", 3: "seer", 4: "villager", 5: "villager", 6: "villager"},
                RuleFlags(), alive=[1, 2, 3, 4, 5, 6], sheriff=3)
    assert st.vote_weight(3) == 1.5 and st.vote_weight(4) == 1.0
    st.kill(3, "knife")
    assert st.vote_weight(3) == 1.0 and st.badge_master() == 3 and st.badge_holder() is None
