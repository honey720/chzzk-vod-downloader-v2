"""SettingDialog.openLogsFolder 3-OS 동작 검증 (#181).

os.startfile은 Windows 전용이라 macOS·Linux에서 클릭 시 무조건
AttributeError였다. QDesktopServices.openUrl로 바꿔 3-OS 공통 동작을
보장한다.
"""

from unittest.mock import patch

import pytest
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

import config.dialog as dialog_module
import theme
from tests.unit.card_helpers import hold_style
from config.dialog import SettingDialog


def test_open_logs_folder_uses_desktop_services(qapp):
    dialog = SettingDialog()
    with patch("config.dialog.QDesktopServices.openUrl", return_value=True) as m:
        dialog.openLogsFolder()

    assert m.call_count == 1
    (url,), _ = m.call_args
    assert url.isLocalFile()
    assert url.toLocalFile().endswith("logs")


def test_open_logs_folder_warns_on_failure(qapp):
    dialog = SettingDialog()
    with patch("config.dialog.QDesktopServices.openUrl", return_value=False):
        with patch("config.dialog.QMessageBox.warning") as warn:
            dialog.openLogsFolder()

    assert warn.called


def _popup_window():
    """지금 떠 있는 콤보 팝업(컨테이너)의 `QWindow`.

    `view.window().windowHandle()`로 잡으면 PySide에서 그 래퍼가 먼저 죽어
    "Internal C++ object already deleted"가 난다(실측) — `allWindows()`에서
    보이는 Popup 타입 창을 찾는 쪽이 안전하다.
    """
    return next(
        w
        for w in QGuiApplication.allWindows()
        if w.type() == Qt.WindowType.Popup and w.isVisible()
    )


def _hover_row(view, row):
    """팝업 뷰의 `row` 위로 마우스를 지나가게 한다 — **합성 이벤트로만**.

    `QTest.mouseMove(QWidget)`는 버튼이 눌려 있지 않으면 `QCursor::setPos()`로
    **실제 커서를 옮기고** OS가 이벤트를 만들어 주길 기다린다(Qt 6.11
    `qtestmouse.h`, "Qt 7에서 바꿀 것" 주석이 달린 호환 경로). offscreen은
    커서 이동을 합성 이벤트로 바꿔 줘서 통과했지만, Windows 실기에서는 팝업에
    닿지 않아 이 클래스가 실기에서만 실패했다. 더 나쁜 것은 테스트가 오너의
    마우스를 뺏는다는 점이다 — `QScreen.grabWindow`가 오너의 다른 창을 찍던
    것과 같은 계열이라 실제 커서는 건드리지 않는다. 팝업 `QWindow`에 보내는
    `QTest.mouseMove(QWindow)`는 `QWindowSystemInterface`를 타는 합성 이벤트라
    어느 플랫폼에서든 같은 경로로 뷰포트에 도달한다.

    첫 이동을 뷰포트 좌상단으로 두는 이유: 이전 테스트가 남긴 좌표와 이번
    대상이 같으면 이동량 0으로 `entered()` 신호가 안 나는 함정(실측)을 피해
    반드시 실이동을 만든다.

    ⚠️ 남는 조건: **실제 커서가 팝업이 뜨는 자리에 놓여 있으면** Fusion의
    마우스 추적이 그 실제 이동을 받아 강조를 오염시킬 수 있다(미표시
    다이얼로그의 팝업은 화면 좌상단에 뜬다). CI는 커서가 고정이라 해당 없고,
    로컬 실기에서 그 자리에 커서를 두고 돌리면 이 클래스가 흔들릴 수 있다 —
    커서를 치워 두는 코드는 위와 같은 이유로 넣지 않는다.
    """
    window = _popup_window()
    viewport = view.viewport()
    for point in (viewport.rect().topLeft(), view.visualRect(view.model().index(row, 0)).center()):
        QTest.mouseMove(window, window.mapFromGlobal(viewport.mapToGlobal(point)))


def _capture_current_index_before_resync_runs(window, view):
    """복원 필터의 `Show` 처리가 실행되기 *전* 시점의 `view.currentIndex()`를 기록한다.

    Qt는 이벤트 필터를 나중에 설치한 것부터 먼저 부른다(실측 확인) — 이
    프로브를 `_wire_popup_highlight_resync()`가 이미 설치한 복원 필터
    *뒤에* 설치하면, 이 프로브는 복원 필터의 `Show` 처리보다 먼저 불린다.
    그 순간 값이 이미 올바르면 그 이전(Hide 시점)에 미리 고쳐놨다는 뜻이고,
    아직 오염된 값이면 Show가 되고 나서야 고치는 것이므로 화면에 한 프레임
    잘못된 값이 찍힌다(오너가 실기에서 본 깜빡임과 정확히 같은 지점).
    """
    seen = []

    class _Probe(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Show:
                seen.append(view.currentIndex().row())
            return False

    probe = _Probe(window)
    window.installEventFilter(probe)
    return seen, probe


class TestComboBoxPopupHighlightResync:
    """#241 후속 — 오너 실기 확인: 드롭다운을 열고 다른 항목 위로 마우스만
    지나간(클릭 없이) 뒤 닫았다 다시 열면, 마지막으로 호버한 항목에 강조가
    고정돼 있어 실제 선택값을 읽을 수 없었다.

    Fusion은 `SH_ComboBox_ListMouseTracking` 힌트 때문에 팝업 안에서
    마우스가 지나간 항목으로 `QItemSelectionModel`의 selection 자체를
    옮긴다(`#240` 감사 후속으로 native Windows 스타일과 대조해 확인 —
    거긴 이 힌트가 꺼져 있어 호버해도 selection이 안 움직인다, Qt 보편
    동작이 아니라 Fusion 전용) — `showPopup()`을 다시 불러도 이 selection을
    실제 선택값으로 되돌리지 않는다. `config.dialog._wire_popup_highlight_resync()`가
    이걸 되돌린다.
    """

    @pytest.fixture(autouse=True)
    def _production_style(self, qapp):
        """이 클래스는 **Fusion 전용 동작**(호버가 selection을 옮김)을 단언하므로
        스타일을 명시한다 — SPEC §2.0 "스타일 의존 동작을 대조할 때는 명시적으로
        `setStyle()`". 그동안은 offscreen QPA의 기본 스타일이 Fusion이라 명시 없이
        통과했고, Windows 실기(기본 windows11, 마우스 추적 힌트 꺼짐)에서
        드러났다. `scope="function"` + `hold_style`은 #243 우회 조건이다.

        전역 QSS도 명시한다(없음). 이 클래스는 다이얼로그를 띄우지 않은 채 팝업만
        여는데, 앞 파일이 남긴 전역 QSS(`::item` padding)가 걸려 있으면 미표시
        다이얼로그의 팝업 높이가 3행을 못 담아(실기 실측: viewport 72px < 26px×3)
        `scrollTo(currentIndex)`가 row 0을 위로 밀어 숨기고, 그 행을 겨냥한
        호버·클릭이 허공에 떨어진다 — 전체 스위트를 실기로 돌릴 때만 4건이
        실패했던 원인. 제품(다이얼로그 표시 상태)에서는 78px로 3행이 다 보인다
        (실측). 전역 상태를 남기는 픽스처 자체는 감사 3단계 대상이다."""
        qapp.setStyle(hold_style(theme.build_style()))
        qapp.setStyleSheet("")

    def _combo(self, dialog):
        combo = dialog.afterDownload
        combo.setCurrentIndex(2)  # "shutdown" — 임의의 눈에 띄는 값
        return combo

    @staticmethod
    def _open_popup(qtbot, combo):
        """`showPopup()` 뒤 팝업 뷰가 실제로 보일 때까지 기다린다.

        Windows 실기 QPA는 `SH_ComboBox_Popup`이 꺼진 드롭다운을 롤 효과
        (`Qt::UI_AnimateCombo`, `QRollEffect` 150ms)로 띄우므로 `showPopup()`
        직후에는 뷰가 아직 안 보인다 — 그 상태에서 호버·클릭을 넣거나 `Show`
        이벤트를 기다리면 아무 일도 안 일어나 이 클래스 5건이 실기에서만
        실패했다(offscreen은 효과가 없어 즉시 보이므로 CI에서는 늘 통과했고,
        `#245` 감사가 offscreen 기준이라 못 잡은 부류). 고정 대기가 아니라
        `waitUntil`이라 offscreen에서는 첫 검사에서 바로 빠져나간다.
        """
        combo.showPopup()
        qtbot.waitUntil(combo.view().isVisible)

    def test_popup_opens_with_highlight_on_the_real_current_value(self, qapp, qtbot):
        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        combo = self._combo(dialog)

        self._open_popup(qtbot, combo)
        view = combo.view()

        assert view.currentIndex().row() == 2
        assert view.selectionModel().isSelected(view.model().index(2, 0))
        combo.hidePopup()

    def test_stray_hover_does_not_survive_a_reopen(self, qapp, qtbot):
        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        combo = self._combo(dialog)

        self._open_popup(qtbot, combo)
        view = combo.view()
        _hover_row(view, 0)  # "none" 위로 마우스만 지나간다, 클릭은 안 함
        assert view.currentIndex().row() == 0  # Qt가 강조를 실제로 옮긴 것부터 확인

        combo.hidePopup()
        self._open_popup(qtbot, combo)

        assert combo.currentIndex() == 2  # 콤보 자신의 선택값은 애초에 안 바뀌었다
        assert view.currentIndex().row() == 2  # 재오픈 시 강조가 실제 값으로 돌아와야 한다
        assert view.selectionModel().isSelected(view.model().index(2, 0))
        assert not view.selectionModel().isSelected(view.model().index(0, 0))
        combo.hidePopup()

    def test_repeated_reopen_without_hover_stays_on_the_real_value(self, qapp, qtbot):
        """호버가 전혀 없었으면 애초에 흐트러질 게 없다 — 되돌리는 로직이
        아무 부작용 없이 반복 재오픈에서도 항상 같은 값을 보여주는지 확인."""
        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        combo = self._combo(dialog)
        view = combo.view()

        for _ in range(3):
            self._open_popup(qtbot, combo)
            assert view.currentIndex().row() == 2
            combo.hidePopup()

    def test_fault_injection_without_resync_the_bug_reproduces(self, qapp, qtbot, monkeypatch):
        """새 테스트가 실제로 고장을 잡는지 확인 — resync 배선을 빼면 위
        시나리오가 다시 실패해야 한다(고장 주입, #241 검증 관례와 동일)."""
        monkeypatch.setattr(dialog_module, "_wire_popup_highlight_resync", lambda combo: None)

        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        combo = self._combo(dialog)
        view = combo.view()

        self._open_popup(qtbot, combo)
        _hover_row(view, 0)
        combo.hidePopup()
        self._open_popup(qtbot, combo)

        assert view.currentIndex().row() == 0  # 배선을 빼면 #241 증상 그대로 재현된다
        combo.hidePopup()

    def test_click_selecting_a_new_value_survives_the_hide_time_restore(self, qapp, qtbot):
        """클릭으로 항목을 골라 닫는 경우의 함정 확인 — `Hide` 이벤트가 오는
        시점엔 `combo.currentIndex()`가 아직 클릭 전 값이다(실측 확인). 그
        순간 바로 되돌리면 방금 고른 값을 도로 뭉갠다 — `QTimer.singleShot(0, ...)`
        로 다음 턴까지 미뤄야 안전하다는 것을 여기서 고정한다."""
        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        combo = self._combo(dialog)  # currentIndex=2("shutdown")
        view = combo.view()

        self._open_popup(qtbot, combo)
        rect0 = view.visualRect(view.model().index(0, 0))  # "none" 클릭
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect0.center())
        QTest.qWait(20)  # Hide 시점에 예약된 singleShot(0)이 돌 시간을 준다

        assert combo.currentIndex() == 0  # 클릭한 값으로 실제 선택이 바뀌었다

        self._open_popup(qtbot, combo)
        assert view.currentIndex().row() == 0  # 방금 고른 값이 강조돼야 한다(0으로 뭉개지면 안 됨)
        assert view.selectionModel().isSelected(view.model().index(0, 0))
        combo.hidePopup()

    def test_highlight_is_already_correct_before_show_time_resync_runs(self, qapp, qtbot):
        """복원이 "일어난 시점"이 아니라 "열렸을 때 이미 올바른가"를 본다 —
        전자만 확인하면 이번 깜빡임 결함을 못 잡는다(오너 지시).

        `_capture_current_index_before_resync_runs()`로 복원 필터의 `Show`
        처리보다 먼저 실행되는 프로브를 심어, "Show 콜백이 돌기 전" 순간의
        값을 그대로 잡는다. `Hide` 시점에 미리 고쳐놨다면(이번 수정) 이
        시점에도 이미 올바르다 — 깜빡일 시간 자체가 없다."""
        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        combo = self._combo(dialog)  # currentIndex=2
        view = combo.view()
        window = view.window()

        self._open_popup(qtbot, combo)
        _hover_row(view, 0)  # 오염시킨다
        combo.hidePopup()
        QTest.qWait(20)  # Hide 시점 singleShot(0) 복원이 돌 시간을 준다

        seen, _probe = _capture_current_index_before_resync_runs(window, view)

        self._open_popup(qtbot, combo)
        assert seen == [2]  # Show 콜백이 돌기 *전*부터 이미 올바른 값이어야 한다
        combo.hidePopup()

    def test_fault_injection_show_only_resync_still_flickers(self, qapp, qtbot, monkeypatch):
        """고장 주입 — Hide 시점 복원을 지우고 Show 시점만 남기면(이전 구현),
        위 무깜빡임 테스트가 다시 실패해야 한다. 안 실패하면 그 테스트가
        이번 결함을 안 보고 있는 것이다(오너 지시)."""

        def show_only_event_filter(self, watched, event):
            if event.type() == QEvent.Type.Show:
                self._resync()
            return False

        monkeypatch.setattr(
            dialog_module._ComboBoxPopupHighlightResync, "eventFilter", show_only_event_filter
        )

        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        combo = self._combo(dialog)
        view = combo.view()
        window = view.window()

        self._open_popup(qtbot, combo)
        _hover_row(view, 0)
        combo.hidePopup()
        QTest.qWait(20)  # Hide 쪽이 있었다면 여기서 이미 고쳤을 시간 — 지금은 없음

        seen, _probe = _capture_current_index_before_resync_runs(window, view)

        self._open_popup(qtbot, combo)
        assert seen == [0]  # Show 콜백 전엔 아직 오염된 값 그대로 — 깜빡임 재현
        combo.hidePopup()


def _press_enter_the_way_macos_delivers_it(window):
    """macOS QPA의 Enter 배달 순서를 어느 플랫폼에서든 재현한다.

    실제 macOS에서는 팝업(NSPanel)이 key window가 될 수 없고 키보드 grab도
    없어서(QTBUG-106597) 키 이벤트가 **다이얼로그 창**으로 온다 — 순서는
    `ShortcutOverride`(→ 활성 팝업의 뷰로 전달) 다음 같은 키의 `KeyPress`
    (→ 그 시점의 활성 팝업 또는 다이얼로그 포커스 위젯). Windows QPA는 팝업이
    키보드를 grab해서 둘 다 팝업 창으로 가므로 실기로는 이 순서가 안 나온다
    — 그래서 창을 직접 지정해 보낸다(`QTest.keyClick(QWindow)`는
    `QWindowSystemInterface`를 타므로 QPA가 창을 고른 뒤의 경로와 같다).

    `ShortcutOverride`를 명시적으로 먼저 보내는 이유: macOS의
    `QGuiApplicationPrivate::processKeyEvent`는 shortcut 단계를 QPA(QNSView)에
    맡기고 건너뛰므로, macOS CI에서 `keyPress(QWindow)`만 보내면 컨테이너의
    Enter 처리(`ShortcutOverride`에서 팝업을 닫고 항목 선택)가 아예 안 돈다.
    다른 플랫폼에서는 `keyPress`가 override를 한 번 더 내지만, 그때는 팝업이
    이미 닫혀 콤보에 닿고 accept되지 않으므로 결과가 같다(offscreen 실측).
    """
    QTest.keyEvent(QTest.KeyAction.Shortcut, window, Qt.Key.Key_Return)
    QTest.keyPress(window, Qt.Key.Key_Return)
    QTest.keyRelease(window, Qt.Key.Key_Return)


class TestComboBoxPopupEnterStaysInsideThePopup:
    """#240 macOS 실기 회귀 — 드롭다운을 열고 Enter로 항목을 고르면 설정 창까지
    닫혔다(v2.9.6은 항목만 선택). 원인 경로와 수정은 `theme._DropDownComboBoxStyle`
    docstring 참고 — 지연 닫기 힌트를 QComboBox에 한해 macOS에서만 켠다.

    게이트는 Enter 뒤에 (1) 콤보가 강조돼 있던 항목으로 바뀌고 (2) 다이얼로그가
    그대로 열려 있는 것. 메커니즘을 재므로 `deferred_popup_hide`를 명시해 넘긴다
    (플랫폼 기본값 분기는 `test_theme.py`가 따로 잰다).
    """

    def _show(self, qapp, qtbot):
        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)
        dialog.activateWindow()
        qtbot.waitUntil(lambda: qapp.activeWindow() is dialog)
        combo = dialog.afterDownload
        combo.setCurrentIndex(2)  # "shutdown"
        combo.setFocus()
        return dialog, combo

    def _open_popup_and_move_highlight(self, qtbot, combo, row):
        combo.showPopup()
        view = combo.view()
        # Windows 실기 QPA는 `SH_ComboBox_Popup`이 꺼진 드롭다운을 롤 효과
        # (`Qt::UI_AnimateCombo`, QRollEffect 150ms)로 띄우므로 뜰 때까지 기다린다
        # — offscreen은 효과가 없어 즉시 보인다.
        qtbot.waitUntil(view.isVisible)
        view.setCurrentIndex(view.model().index(row, 0))  # 키보드 탐색으로 강조를 옮긴 상태
        return view

    def test_enter_picks_the_highlighted_item_and_leaves_the_dialog_open(self, qapp, qtbot):
        qapp.setStyle(hold_style(theme.build_style(deferred_popup_hide=True)))
        dialog, combo = self._show(qapp, qtbot)
        finished = []
        dialog.finished.connect(finished.append)
        view = self._open_popup_and_move_highlight(qtbot, combo, 0)

        _press_enter_the_way_macos_delivers_it(dialog.windowHandle())

        qtbot.waitUntil(lambda: not view.isVisible())  # 지연 닫기라 ~80ms 뒤에 닫힌다
        assert combo.currentIndex() == 0  # Enter가 강조돼 있던 "none"을 골랐다
        assert dialog.isVisible()
        assert finished == []  # 설정 창은 그대로 열려 있어야 한다

    def test_fault_injection_without_deferred_hide_the_dialog_closes(self, qapp, qtbot):
        """지연 닫기를 끄면(수정 전 동작) 위 게이트가 실제로 실패하는지 — 이
        테스트가 회귀를 보고 있다는 증거이자, macOS 증상의 플랫폼 무관
        재현이다(고장 주입 관례)."""
        qapp.setStyle(hold_style(theme.build_style(deferred_popup_hide=False)))
        dialog, combo = self._show(qapp, qtbot)
        finished = []
        dialog.finished.connect(finished.append)
        view = self._open_popup_and_move_highlight(qtbot, combo, 0)

        _press_enter_the_way_macos_delivers_it(dialog.windowHandle())

        qtbot.waitUntil(lambda: not view.isVisible())
        assert combo.currentIndex() == 0  # 항목 선택 자체는 된다 — v2.9.6과 같은 부분
        assert finished == [int(QDialog.DialogCode.Accepted)]  # 새어 나간 Enter가 OK를 눌렀다
        assert not dialog.isVisible()
