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

from PySide6.QtWidgets import QApplication, QLabel, QWidget


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


def hold_style(style):
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
