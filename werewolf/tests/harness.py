"""极简测试框架（不依赖 pytest）：python -m werewolf.tests.run"""
from __future__ import annotations

import importlib
import inspect
from pathlib import Path
import random
import sys
import traceback

from ..actors.fake import FakeActor
from ..engine.config import GameConfig, PRESETS
from ..engine.engine import Engine
from ..engine.presenter import NullPresenter

def block_text(event) -> str:
    """一个播报块的文本：`InfoEvent` 直接给，`HTMLInfoEvent` 走 xun 的 `to_text()` 降级。

    断言写成「块文本含某个子串」就同时对两种模式生效 —— 顺带把降级形态也测了。
    """
    payload = event.payload
    if hasattr(payload, "message"):
        return payload.message
    title = getattr(payload, "title", None) or ""
    return f"{title}\n{payload.to_text()}".strip()


def info_blocks(events) -> list[str]:
    """所有法官播报块（灰字 info 与看板卡都算）。"""
    return [block_text(e) for e in events if e.name in ("InfoEvent", "HTMLInfoEvent")]


def discover_modules() -> list[str]:
    """自动发现本目录下的 test_*.py，新增测试文件不用改这里。

    按目录找、用 `__package__` 拼名字，因此不依赖引擎包叫 `werewolf` 还是
    `xun_ext_werewolf.werewolf`（extension 里是后者）。
    """
    import pkgutil

    return sorted(f"{__package__}.{m.name}" for m in pkgutil.iter_modules([str(Path(__file__).parent)])
                  if m.name.startswith("test_"))


MODULES = discover_modules()


class RecordingActor(FakeActor):
    """记录每次被询问时的 Ask 与当时的日志长度，用于可见性/快照断言。"""

    registry: list = []

    def __init__(self, seat, rng, engine_ref):
        super().__init__(seat, rng)
        self.engine_ref = engine_ref

    def _record(self, ask):
        state = self.engine_ref.state
        RecordingActor.registry.append({
            "seat": self.seat,
            "kind": ask.kind,
            "day": ask.day,
            "phase": ask.phase,
            "log_len": len(state.log),
            # 本轮已收到但尚未公布的选择：整轮收齐前不该出现在任何人的日志里
            "buffer_len": len(state.buffered()),
            "freeze_id": state.freeze_seq,
            "ask": ask,
        })
        return None

    def decide(self, ask):
        self._record(ask)
        return super().decide(ask)


def play(preset_index: int = 2, seed: int = 1, human_seat: int | None = None,
         recording: bool = False, error_rate: float = 0.0, tweak: dict | None = None):
    """跑一整局（纯脚本玩家），返回 engine 与记录。"""
    cfg = PRESETS[preset_index].config
    cfg = GameConfig(
        label=cfg.label, n_seats=cfg.n_seats, counts=dict(cfg.counts),
        human_seat=human_seat, flags=cfg.flags, seed=seed,
    )
    if tweak:
        for key, value in tweak.items():
            setattr(cfg.flags, key, value)
    rng = random.Random(seed * 977 + 13)
    eng = Engine(cfg, {}, presenter=NullPresenter(), seed=seed)
    eng.setup()
    if recording:
        RecordingActor.registry = []
        eng.actors = {s: RecordingActor(s, rng, eng) for s in cfg.seats}
    else:
        eng.actors = {s: FakeActor(s, rng, error_rate=error_rate) for s in cfg.seats}
    eng.run()
    return eng


def run_all() -> int:
    failures = 0
    for module_name in MODULES:
        module = importlib.import_module(module_name)
        tests = [
            (name, fn) for name, fn in sorted(vars(module).items())
            if name.startswith("test_") and inspect.isfunction(fn)
        ]
        print(f"[{module.__name__}]")
        for name, fn in tests:
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
                traceback.print_exc(limit=6)
    total_word = "全部通过" if failures == 0 else f"{failures} 个失败"
    print(f"\n结果：{total_word}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run_all())
