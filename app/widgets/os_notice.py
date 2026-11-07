"""다운로드가 끝났을 때의 OS 알림 — 작업 표시줄 깜빡임과 트레이 알림 (#523, 설계 §8-11).

창이 앞에 없을 때만 부른다(부르는 쪽이 정한다). 새 의존성 없이 Qt만 쓴다.

- 작업 표시줄 깜빡임(`QApplication.alert`) — 창이 활성이 될 때까지. 트레이가 없어도 한다.
- 트레이 알림(`QSystemTrayIcon.showMessage`) — 트레이가 있고 알림을 받을 때만. 트레이 아이콘은 알림
  동안만(10초) 띄우고, 알림이나 아이콘을 누르면 창을 앞으로 가져온다.
- 알림 아이콘은 여러 크기(16 · 32 · 48 · 64px)를 담는다 — 16px 한 장이면 Windows가 알림을 띄우지 않았다
  (오너 실기 2026-11-03). 알림 센터에 남지 않는 것은 Qt 트레이 알림의 한계로 받아들인다.
"""

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget

import app.theme as theme
from app.widgets.icons import BUBBLE_FILL_TOKENS, bubble_pixmap

#: 트레이 아이콘을 띄워 두는 시간(ms) — 알림 동안만(설계 §8-11).
TRAY_SHOWN_MS = 10_000

#: 알림 아이콘에 담는 크기(px) — 32px 이상이 있어야 Windows가 알림을 띄운다.
NOTICE_ICON_SIDES = (16, 32, 48, 64)


def notice_icon(state: str) -> QIcon:
    """그 결과 상태의 버블을 여러 크기로 그린 아이콘 — 메뉴바 버블과 같은 상태 색."""
    tokens = theme.current_tokens()
    icon = QIcon()
    for side in NOTICE_ICON_SIDES:
        icon.addPixmap(
            bubble_pixmap(state, tokens[BUBBLE_FILL_TOKENS[state]], tokens["onAccent"], side)
        )
    return icon


def tray_messages_available() -> bool:
    """트레이 알림을 쓸 수 있는 환경인지 — 트레이가 있고 알림을 받는다."""
    return QSystemTrayIcon.isSystemTrayAvailable() and QSystemTrayIcon.supportsMessages()


class DownloadEndNotice(QObject):
    """다운로드 끝 OS 알림 — 창 하나에 하나. 트레이 아이콘은 처음 알릴 때 만든다."""

    def __init__(self, window: QWidget, bring_to_front: Callable[[], None]) -> None:
        """알림을 만든다.

        Args:
            window: 깜빡일 창 · 트레이 아이콘의 부모
            bring_to_front: 알림 · 트레이 아이콘을 눌렀을 때 창을 앞으로 가져오는 함수
        """
        super().__init__(window)
        self._window = window
        self._bringToFront = bring_to_front
        self._tray: QSystemTrayIcon | None = None
        self._hideTimer = QTimer(self)
        self._hideTimer.setSingleShot(True)
        self._hideTimer.setInterval(TRAY_SHOWN_MS)
        self._hideTimer.timeout.connect(self._hideTray)

    def notify(self, title: str, text: str, state: str) -> None:
        """작업 표시줄을 깜빡이고, 쓸 수 있으면 트레이 알림을 띄운다.

        Args:
            title: 알림 제목(앱 이름)
            text: 알림 글(「다운로드를 마쳤습니다 · 완료 N」 등)
            state: 아이콘의 결과 상태(`done` · `some_failed` · `all_failed`)
        """
        QApplication.alert(self._window, 0)
        if not tray_messages_available():
            return
        icon = notice_icon(state)
        if self._tray is None:
            self._tray = QSystemTrayIcon(self._window)
            self._tray.messageClicked.connect(self._onClicked)
            self._tray.activated.connect(self._onClicked)
        self._tray.setIcon(icon)
        self._tray.setToolTip(title)
        self._tray.show()
        self._tray.showMessage(title, text, icon, TRAY_SHOWN_MS)
        self._hideTimer.start()

    def trayShown(self) -> bool:
        """트레이 아이콘이 떠 있는지."""
        return self._tray is not None and self._tray.isVisible()

    def _onClicked(self, *_args: object) -> None:
        """알림이나 트레이 아이콘을 눌렀다 — 창을 앞으로, 트레이 아이콘은 내린다."""
        self._bringToFront()
        self._hideTray()

    def _hideTray(self) -> None:
        """트레이 아이콘을 내린다."""
        self._hideTimer.stop()
        if self._tray is not None:
            self._tray.hide()
