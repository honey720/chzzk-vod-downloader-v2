"""카드 GUI 게이트 공용 헬퍼 (#245 [F] — "거짓 초록" 구조적 차단).

두 헬퍼는 각각 한 가지 우연 통과 경로를 막는다:

- `resize_to()` — 카드는 top-level일 때 최소폭 아래로 **조용히 클램프**된다
  (offscreen 폰트에서 5-pill 대기 카드 560px 요청 → 실제 686px). 폭이 안 바뀌면
  "단조 감소" 같은 단언이 아무것도 재지 않은 채 통과한다. 요청 폭과 실제 폭이
  다르면 즉시 실패시킨다.
- `shown()` — 숨은 라벨은 마지막(낡은) 표시 문자열·기하를 그대로 돌려줘 "잘리지
  않았다/잘렸다" 단언이 우연히 통과한다(#245 박제 테스트 실사례, E-2 감사에서
  같은 부류 25개 확인). `isVisible()`을 먼저 단언한 뒤에만 표시 문자열을 준다.

개별 단언을 고치는 대신 이 헬퍼를 거치게 해서 다음 사람이 같은 것을 다시
쓰지 못하게 한다. 폭은 절대 px가 아니라 카드 자체 기하로 유도한 값을 넣는다.
"""

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF
from PySide6.QtGui import QEnterEvent, QHelpEvent
from PySide6.QtWidgets import QApplication, QLabel, QStyle, QToolTip, QWidget


def resize_to(widget: QWidget, width: int) -> None:
    """카드를 `width`로 놓고 보인 뒤, **실제 폭이 요청과 같은지** 단언한다.

    높이는 자연값(sizeHint)을 쓴다 — 임의 고정 높이를 주면 세로 여분이 행 사이로
    분배돼 실제 목록과 다른 조건에서 재게 된다.
    """
    widget.resize(width, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()
    if widget.width() != width:
        # 최상위 창의 최소폭은 레이아웃이 **직전 표시 모드에서 캐시**한 값이다
        # (경로가 텍스트→아이콘으로 바뀌면 몇 px 내려간다). 첫 resize는 옛 최소폭에
        # 걸리고 모드가 바뀐 뒤에야 새 값이 적용된다 — 실제 창 드래그도 두 단계로
        # 진행되므로 한 번 더 시도한다. 그래도 다르면 진짜 클램프다.
        widget.resize(width, widget.sizeHint().height())
        QApplication.processEvents()
    assert widget.width() == width, (
        f"요청 폭 {width}px인데 실제 {widget.width()}px — 최소폭({widget.minimumSizeHint().width()}px)에 "
        "클램프됐다. 이 폭에서는 게이트가 아무것도 재지 않는다 — 폭을 카드 기하로 유도할 것"
    )


ROOMY_WIDTH = 2400  # 폭을 유도하기 전에 놓는 폭(px) — 대체 글꼴에서도 3줄이 안 접힌다


def row3_need(card: QWidget) -> int:
    """지금 3줄 모양이 바라는 폭 — 보이는 위젯의 글 폭(+4) · 아이콘 폭 · 사이 간격을 독립 합산한다.

    늘어나는 빈칸과 숨은 위젯은 간격을 받지 않는다(QBoxLayout).
    """
    layout = card.resolutionLayout
    widths = []
    for index in range(layout.count()):
        entry = layout.itemAt(index)
        widget = entry.widget()
        if widget is None or entry.isEmpty():
            continue
        if isinstance(widget, QLabel) and widget.text():
            widths.append(widget.fontMetrics().horizontalAdvance(widget.text()) + 4)
        else:
            widths.append(widget.width())
    return sum(widths) + layout.spacing() * (len(widths) - 1)


def unfold(card: QWidget) -> int:
    """카드를 3줄이 하나도 접히지 않는 가장 좁은 폭에 놓고 그 폭을 돌려준다(#555).

    넉넉한 폭에서 3줄 모양의 폭을 독립 합산하고(`row3_need`) 3줄 밖의 폭(썸네일 · 여백 · 테두리)을
    더한다. 글꼴마다 이 폭이 달라 절대 px로 박으면 글자가 넓은 글꼴에서 카드가 설계대로 접힌다.
    넉넉한 폭에서 이미 접혔으면 합산 폭이 접힌 모양 기준이 되어 뒤 단언이 실패한다 — 거짓 통과는 없다.
    """
    resize_to(card, ROOMY_WIDTH)
    chrome = card.width() - card.statusRow.width()
    width = chrome + row3_need(card)
    resize_to(card, width)
    return width


def shown(widget: QWidget) -> str:
    """`widget`이 **보이는지 단언한 뒤** 실제 표시 문자열을 돌려준다.

    QLabel 계열은 `QLabel.text()`(ElidingLabel의 `text()`는 원문을 돌려주므로
    우회), 버튼 등은 자신의 `text()`. 숨은 위젯의 낡은 값을 읽는 경로를 없앤다.
    """
    assert widget.isVisible(), (
        f"{widget.objectName() or type(widget).__name__}이(가) 보이지 않는다 — "
        "숨은 위젯의 표시 문자열·기하는 낡은 값이라 단언 대상이 아니다"
    )
    return QLabel.text(widget) if isinstance(widget, QLabel) else widget.text()


#: hold_style()이 보관하는 스타일 객체들 — 프로세스가 끝날 때까지 절대 비우지 않는다.
_STYLE_REFS: list = []


def hold_style(style) -> QStyle:
    """`qapp.setStyle()`에 넘길 스타일 객체의 파이썬 참조를 **앱 수명 동안** 붙든다 (#243 우회).

    `setStyle()`로 소유권이 Qt C++ 쪽으로 넘어간 스타일의 파이썬 래퍼가 먼저
    죽으면, 다음 `setStyle()`이 이전 스타일을 지우는 시점에 이중 해제로
    SIGSEGV가 난다(macOS offscreen CI에서 2/2 결정적 재현 — `_apply_dark_card_qss`
    setup의 `setStyle`에서 exit 139). 파이썬 참조를 여기 리스트에 살려두면
    지우는 쪽이 Qt 하나뿐이라 이중 해제가 성립하지 않는다.

    ⚠️ 폭탄이 심기는 곳은 "래퍼가 죽는 모든 setStyle 지점"이고 터지는 곳은
    "그다음 setStyle"이다 — 그래서 지명 픽스처 하나가 아니라 **앱 수준
    `setStyle(theme.build_style())` 지점 전부**가 이 헬퍼를 거쳐야 한다.
    근본 원인(#243)의 수정이 아니라 테스트 쪽 우회다. 제품 코드는 불변.
    """
    _STYLE_REFS.append(style)
    return style


def snapshot_top_levels() -> set:
    """지금 살아 있는 최상위 위젯 집합 — `drop_new_top_levels`의 기준점."""
    return {id(w) for w in QApplication.topLevelWidgets()}


def drop_new_top_levels(before: set) -> None:
    """테스트가 만든 최상위 창을 닫고 **실제로 파괴**한다 — 숨은 채 남기지 않는다.

    close()만 하면 창은 숨은 채 살아 있고, 다음 테스트의 `qapp.setStyle()`이 그 창들까지
    다시 폴리시한다 — 파이썬 참조가 끊긴 자식 위젯을 건드리면 xvfb(리눅스 CI)에서
    SIGSEGV로 죽었다(PR #248 CI, test_failure_display의 setStyle에서 터짐). 창을 만든
    테스트가 끝날 때 deleteLater + 이벤트 처리 + gc로 확실히 없앤다.
    """
    import gc

    for widget in QApplication.topLevelWidgets():
        if id(widget) not in before:
            widget.close()
            widget.deleteLater()
    for _ in range(3):
        QApplication.processEvents()
    gc.collect()
    QApplication.processEvents()


_SENDING_POINTER_EVENT = [
    0
]  # 테스트가 들어감 · 나감을 보내는 중인 깊이 — 0이면 창 시스템이 보낸 것


def enter(widget: QWidget) -> None:
    """위젯에 들어감 이벤트를 보낸다(포인터는 옮기지 않는다 — safety 규칙)."""
    center = QPointF(widget.rect().center())
    _send_pointer_event(widget, QEnterEvent(center, center, widget.mapToGlobal(center)))
    QApplication.processEvents()


def leave(widget: QWidget) -> None:
    """위젯에 나감 이벤트를 보낸다(포인터는 옮기지 않는다 — safety 규칙)."""
    _send_pointer_event(widget, QEvent(QEvent.Type.Leave))
    QApplication.processEvents()


def _send_pointer_event(widget: QWidget, event: QEvent) -> None:
    """테스트가 보낸 것으로 표시하고 들어감 · 나감을 보낸다 — `hover_from_tests_only`가 이것만 통과시킨다."""
    _SENDING_POINTER_EVENT[0] += 1
    try:
        QApplication.sendEvent(widget, event)
    finally:
        _SENDING_POINTER_EVENT[0] -= 1


def hover_from_tests_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드 호버가 실제 포인터 자리를 따르지 않게 한다 — 호버는 테스트가 정한다 (#587).

    실제 화면에서는 테스트 창이 실제 포인터 밑에 뜨면 창 시스템이 들어감을 보내고, 그 이벤트는 창이
    뜬 뒤 몇 턴 늦게 올 수 있다 — 테스트가 `setHovered(False)`로 호버를 끈 뒤 이벤트를 처리하면 다시
    켜져, 호버 없는 모양을 재던 단언이 호버 바탕 · 카드 버튼을 쟀다. Qt가 창 시스템의 들어감을 위젯에
    넘길 때도 `spontaneous()`가 거짓이라 이벤트로는 가를 수 없어, 테스트가 보낸 것(`enter` · `leave`)만
    카드의 들어감 · 나감 처리에 닿게 한다. 목록의 호버 다시 맞추기는 포인터 밑에 카드가 없다고 본다
    (#575의 `test_theme_switch.py`와 같은 방식). 실제 포인터는 옮기지 않는다.

    호버를 재는 파일의 autouse 픽스처에서 부른다. `QTest`로 창에 합성 마우스 이벤트를 보내 누름 ·
    호버를 재는 테스트에는 쓰지 않는다 — 그 이벤트가 만드는 들어감도 막힌다.
    """
    from app.widgets import view as view_module
    from app.widgets.widget import ContentItemWidget

    monkeypatch.setattr(view_module, "widget_under_pointer", lambda: None)
    original_enter = ContentItemWidget.enterEvent
    original_leave = ContentItemWidget.leaveEvent

    def enter_from_tests(card: ContentItemWidget, event: QEnterEvent) -> None:
        if _SENDING_POINTER_EVENT[0]:
            original_enter(card, event)

    def leave_from_tests(card: ContentItemWidget, event: QEvent) -> None:
        if _SENDING_POINTER_EVENT[0]:
            original_leave(card, event)

    monkeypatch.setattr(ContentItemWidget, "enterEvent", enter_from_tests)
    monkeypatch.setattr(ContentItemWidget, "leaveEvent", leave_from_tests)


TOOLTIP_SENTINEL = "⁣tooltip-sentinel"  # Qt 툴팁에 미리 넣는 글 — 위젯 툴팁과 겹치지 않는다


def qt_tooltip_shown(widget: QWidget) -> bool:
    """위젯에 Qt 툴팁 이벤트를 보내 Qt 기본 툴팁(`QToolTip`)이 그 위젯의 글로 떴는지 돌려준다.

    말풍선 필터(`TipFilter`)는 툴팁 글이 있으면 툴팁 이벤트를 삼킨다 — 그러면 `QToolTip.showText`가
    불리지 않는다. 실제 화면(Windows)은 툴팁을 잠깐 뒤에 보이므로 보임이 아니라 글로 잰다: 먼저
    `TOOLTIP_SENTINEL`을 띄워 두고, 이벤트 뒤 Qt 툴팁의 글이 위젯 툴팁으로 바뀌었는지 본다.
    """
    QToolTip.showText(widget.mapToGlobal(QPoint(0, 0)), TOOLTIP_SENTINEL)
    point = QPoint(2, 2)
    event = QHelpEvent(QEvent.Type.ToolTip, point, widget.mapToGlobal(point))
    QApplication.sendEvent(widget, event)
    QApplication.processEvents()
    shown = bool(widget.toolTip()) and QToolTip.text() == widget.toolTip()
    QToolTip.hideText()
    return shown


def bubble_text_for(widget: QWidget) -> str | None:
    """지금 말풍선이 `widget`에 떠 있으면 그 글, 아니면 None."""
    from app.widgets import tip_bubble

    bubble = tip_bubble.current_bubble()
    if bubble is None or not bubble.isVisible() or bubble.target is not widget:
        return None
    return bubble.text
