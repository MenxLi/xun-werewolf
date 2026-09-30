"""引擎向外界播报的唯一出口。

`NullPresenter` 的方法表就是全部出口：测试/CLI 用这份空实现，
浏览器侧的 `HostPresenter` 同名同参。引擎只经由这几个方法说话，别的一律不许直接碰界面。
"""
from __future__ import annotations

class GameAborted(RuntimeError):
    """会话被关闭 / 用户取消，引擎应立即退出。"""


class NullPresenter:
    """空播报器（测试/CLI 用）；方法表即引擎的全部出口。"""

    def __init__(self, keep: bool = False) -> None:
        self.events: list = []
        self.phases: list[str] = []
        self.notices: list[str] = []
        self.keep = keep

    def phase(self, state, title: str, subtitle: str = "") -> None:
        self.phases.append(title)

    def on_events(self, state, events) -> None:
        if self.keep:
            self.events.extend(events)

    def thinking(self, state, seat: int, kind: str) -> None:
        """某座位即将开始思考（前端活动提示；默认什么都不做）。"""

    def thought(self, state, seat: int) -> None:
        """某座位的决策回来了：关掉它的活动窗口（`thinking` 的反操作）。"""

    def notice(self, state, text: str) -> None:
        self.notices.append(text)

    def finished(self, state) -> None:
        pass

    def stop_requested(self) -> bool:
        return False
