"""SettingDialog.openLogsFolder 3-OS 동작 검증 (#181).

os.startfile은 Windows 전용이라 macOS·Linux에서 클릭 시 무조건
AttributeError였다. QDesktopServices.openUrl로 바꿔 3-OS 공통 동작을
보장한다.
"""

from unittest.mock import patch

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


class TestComboBoxPopupHighlightResync:
    """#241 후속 — 오너 실기 확인: 드롭다운을 열고 다른 항목 위로 마우스만
    지나간(클릭 없이) 뒤 닫았다 다시 열면, 마지막으로 호버한 항목에 강조가
    고정돼 있어 실제 선택값을 읽을 수 없었다.

    Qt는 팝업 안에서 마우스가 지나간 항목으로 `QItemSelectionModel`의
    selection 자체를 옮긴다(스타일시트 유무와 무관한 Qt 자체 동작, 실측
    확인) — `showPopup()`을 다시 불러도 이 selection을 실제 선택값으로
    되돌리지 않는다. `config.dialog._resync_popup_highlight_on_show()`가
    팝업이 뜰 때마다(`Show` 이벤트) 이걸 되돌린다.
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
        monkeypatch.setattr(dialog_module, "_resync_popup_highlight_on_show", lambda combo: None)

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
