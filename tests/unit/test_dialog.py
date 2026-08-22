"""SettingDialog.openLogsFolder 3-OS 동작 검증 (#181).

os.startfile은 Windows 전용이라 macOS·Linux에서 클릭 시 무조건
AttributeError였다. QDesktopServices.openUrl로 바꿔 3-OS 공통 동작을
보장한다.
"""

from unittest.mock import patch

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
