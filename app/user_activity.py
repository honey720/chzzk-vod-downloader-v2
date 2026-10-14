"""사용자가 지금 앱을 조작 중인지 — 마지막 입력의 시각으로 답한다 (#309).

긴 영상의 색인 해석은 조각 사이에서 쉬어야 그동안 화면이 버벅이지 않는데, 항상 쉬면 해석이
3~4배 걸린다. 그래서 **조작하는 동안에만** 쉰다(``core.utils.interaction``). 이 모듈은 그
"조작하는 동안"을 정한다: 앱 창에 입력이 들어온 뒤 ``INTERACTION_SECONDS`` 동안이다.

입력으로 치는 것은 마우스 버튼 · 휠 · 키 · 창 크기 바꾸기 · 창 옮기기다. **버튼을 누르지 않은
마우스 이동은 치지 않는다** — 창 위에서 마우스가 지나가기만 해도 해석이 쉬어, 다운로드 준비가
두 배 넘게 길어졌다(실기: 약 4초 → 12초).

입력은 앱 전체의 이벤트 필터 하나로 본다 — 창이 여럿이어도(메인 창 · 대화상자 · 팝업) 한 곳에서
알아챈다. 필터는 이벤트를 막지 않는다(늘 거짓을 돌려준다).

``user_interacting``은 어느 스레드에서 불려도 된다 — 숫자 하나를 읽을 뿐이다.
"""

import time

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt

# 마지막 입력 뒤 이 시간(초) 동안을 조작 중으로 본다. 창을 끄는 동안의 입력은 수 ms~수십 ms
# 간격으로 이어지므로 그 사이가 끊기지 않고, 손을 떼면 이 시간 뒤에 해석이 제 속도로 돌아간다
INTERACTION_SECONDS = 0.2

# 어느 객체에 오든 입력으로 치는 이벤트 — 마우스 버튼 · 휠 · 키. 창의 테두리 · 제목 줄을 누르는
# 것(NonClientArea…)도 친다 — 테두리를 잡는 순간부터 조작 중이 되어, 끌기의 첫 걸음부터 해석이
# 쉰다(테두리를 잡고 끄는 동안에는 창 크기 변경만 온다)
_INPUT_EVENTS = frozenset(
    {
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
        QEvent.Type.NonClientAreaMouseButtonPress,
        QEvent.Type.NonClientAreaMouseButtonRelease,
        QEvent.Type.NonClientAreaMouseButtonDblClick,
        QEvent.Type.Wheel,
        QEvent.Type.KeyPress,
        QEvent.Type.KeyRelease,
    }
)
# 창(QWindow)에 올 때만 입력으로 치는 이벤트 — 창 크기 바꾸기 · 창 옮기기. 창 안의 위젯도 배치가
# 바뀔 때마다 같은 이벤트를 받는데(카드가 늘 때 등), 그것은 조작이 아니다
_WINDOW_EVENTS = frozenset({QEvent.Type.Resize, QEvent.Type.Move})
# 버튼을 누른 채일 때만 입력으로 치는 이벤트 — 마우스 이동. 누른 채 움직이는 것은 끌기다
# (스크롤 막대 · 글자 고르기). 누르지 않고 지나가는 것은 조작이 아니다
_MOVE_EVENTS = frozenset({QEvent.Type.MouseMove, QEvent.Type.NonClientAreaMouseMove})

_last_input = float("-inf")  # 마지막 입력의 시각(``_now``) — 아직 없으면 -inf
_watch: "_InputWatch | None" = None  # 걸어 둔 필터 — 한 번만 건다


def _now() -> float:
    """흐르기만 하는 시계(초) — 테스트의 monkeypatch 지점."""
    return time.monotonic()


class _InputWatch(QObject):
    """앱 전체의 이벤트를 지켜보며 입력이 온 시각을 적는다 — 이벤트는 그대로 흘려보낸다."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 — Qt의 이름
        global _last_input
        kind = event.type()
        if (
            kind in _INPUT_EVENTS
            or (kind in _WINDOW_EVENTS and watched.isWindowType())
            or (kind in _MOVE_EVENTS and event.buttons() != Qt.MouseButton.NoButton)
        ):
            _last_input = _now()
        return False


def watch_user_input(app: QCoreApplication) -> None:
    """앱 전체의 입력을 지켜보기 시작한다 — GUI가 시작할 때 한 번 부른다. 다시 불러도 한 번만 건다.

    헤드리스 실행은 부르지 않는다 — 화면이 없다(``user_interacting``은 늘 거짓이다).
    """
    global _watch
    if _watch is None:
        _watch = _InputWatch(app)  # 앱이 부모다 — 앱과 함께 사라진다
        app.installEventFilter(_watch)


def stop_watching_user_input() -> None:
    """걸어 둔 필터를 걷고 마지막 입력을 잊는다 — 테스트가 앱을 처음 상태로 되돌릴 때 쓴다."""
    global _watch, _last_input
    if _watch is not None:
        application = QCoreApplication.instance()
        if application is not None:
            application.removeEventFilter(_watch)
        _watch.deleteLater()
        _watch = None
    _last_input = float("-inf")


def user_interacting() -> bool:
    """지금 사용자가 앱을 조작 중인지 — 마지막 입력이 ``INTERACTION_SECONDS`` 안이면 참이다."""
    return _now() - _last_input < INTERACTION_SECONDS
