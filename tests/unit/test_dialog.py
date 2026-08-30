"""SettingDialog.openLogsFolder 3-OS 동작 검증 (#181).

os.startfile은 Windows 전용이라 macOS·Linux에서 클릭 시 무조건
AttributeError였다. QDesktopServices.openUrl로 바꿔 3-OS 공통 동작을
보장한다.
"""

from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtTest import QTest

import config.dialog as dialog_module
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


def _hover_row(view, row):
    # 이전 테스트가 남긴 전역 커서 위치가 우연히 이번 대상 좌표와 같으면
    # QTest.mouseMove가 이동량 0으로 보고 entered() 신호 자체를 안 낸다
    # (offscreen 플랫폼에서 팝업이 항상 같은 화면 위치에 뜨기 때문에 실제로
    # 재현되는 실측 함정) — 매번 다른 지점을 거쳐 반드시 실이동을 만든다.
    viewport = view.viewport()
    QTest.mouseMove(viewport, viewport.rect().topLeft())
    rect = view.visualRect(view.model().index(row, 0))
    QTest.mouseMove(viewport, rect.center())


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

    def _combo(self, dialog):
        combo = dialog.afterDownload
        combo.setCurrentIndex(2)  # "shutdown" — 임의의 눈에 띄는 값
        return combo

    def test_popup_opens_with_highlight_on_the_real_current_value(self, qapp, qtbot):
        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        combo = self._combo(dialog)

        combo.showPopup()
        view = combo.view()

        assert view.currentIndex().row() == 2
        assert view.selectionModel().isSelected(view.model().index(2, 0))
        combo.hidePopup()

    def test_stray_hover_does_not_survive_a_reopen(self, qapp, qtbot):
        dialog = SettingDialog()
        qtbot.addWidget(dialog)
        combo = self._combo(dialog)

        combo.showPopup()
        view = combo.view()
        _hover_row(view, 0)  # "none" 위로 마우스만 지나간다, 클릭은 안 함
        assert view.currentIndex().row() == 0  # Qt가 강조를 실제로 옮긴 것부터 확인

        combo.hidePopup()
        combo.showPopup()

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
            combo.showPopup()
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

        combo.showPopup()
        _hover_row(view, 0)
        combo.hidePopup()
        combo.showPopup()

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

        combo.showPopup()
        rect0 = view.visualRect(view.model().index(0, 0))  # "none" 클릭
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect0.center())
        QTest.qWait(20)  # Hide 시점에 예약된 singleShot(0)이 돌 시간을 준다

        assert combo.currentIndex() == 0  # 클릭한 값으로 실제 선택이 바뀌었다

        combo.showPopup()
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

        combo.showPopup()
        _hover_row(view, 0)  # 오염시킨다
        combo.hidePopup()
        QTest.qWait(20)  # Hide 시점 singleShot(0) 복원이 돌 시간을 준다

        seen, _probe = _capture_current_index_before_resync_runs(window, view)

        combo.showPopup()
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

        combo.showPopup()
        _hover_row(view, 0)
        combo.hidePopup()
        QTest.qWait(20)  # Hide 쪽이 있었다면 여기서 이미 고쳤을 시간 — 지금은 없음

        seen, _probe = _capture_current_index_before_resync_runs(window, view)

        combo.showPopup()
        assert seen == [0]  # Show 콜백 전엔 아직 오염된 값 그대로 — 깜빡임 재현
        combo.hidePopup()
