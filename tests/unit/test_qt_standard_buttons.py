"""Qt 표준 버튼(OK/Cancel/Yes/No) 번역 게이트 (#240 2단계, v2.9.7).

설정 창의 `QDialogButtonBox`와 메시지 상자(`QMessageBox` 정적 호출 13곳)의 버튼 문구는
우리 `.ts`가 아니라 **Qt 자체 카탈로그**(`translations/qtbase_ko.qm`)가 번역한다.
`main.set_language`가 앱 언어에 맞춰 그 카탈로그를 설치하고, 그 **뒤에** 우리 번역기를
설치한다(Qt는 나중에 설치한 번역기를 먼저 검색한다 — 아래 대조군 테스트가 그 규칙을 직접 잰다).

언어는 테스트가 config 값으로 **고정**한다 — 설정에 `language`가 있으면 시스템 로케일을
보지 않는다(`main.set_language`). 표준 버튼 문구는 로케일이 아니라 설치된 번역기에만
달려 있다.

고장 주입(확인됨):
- 카탈로그 설치 제거 → 한국어 게이트 실패
- 설치 순서 뒤집기(우리 번역 → Qt 카탈로그) → 순서 게이트 실패
- `en_US`인데 한국어 카탈로그 로드 → 영어 게이트 실패
"""

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, Qt, QTranslator
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

import config.config as config
import main
from app.views.dialog import SettingDialog

KOREAN = {"ok": "확인", "cancel": "취소", "yes": "예(&Y)", "no": "아니요(&N)"}
ENGLISH = {"ok": "OK", "cancel": "Cancel", "yes": "&Yes", "no": "&No"}


def _seed_config(language: str) -> dict:
    """격리된 config 파일에 언어를 박아 둔다 — 시스템 로케일이 답에 끼어들지 않게."""
    cfg = config.default_config()
    cfg["language"] = language
    path = Path(config.CONFIG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg


@pytest.fixture
def translators(qapp, monkeypatch):
    """`main.set_language`를 실제 QApplication에 걸고, 끝나면 설치한 번역기를 전부 뗀다.

    전역 상태(번역기)를 되돌리지 않으면 뒤 테스트가 한국어 버튼을 보게 된다.
    """
    monkeypatch.setattr(main, "app", qapp, raising=False)
    installed: list = []

    def install(language: str) -> list:
        cfg = _seed_config(language)
        result = main.set_language(cfg, QTranslator())
        installed.extend(result)
        return result

    yield install
    for translator in installed:
        qapp.removeTranslator(translator)
    main._qt_translators.clear()
    QApplication.processEvents()


def _box_texts(box: QDialogButtonBox) -> dict:
    return {
        "ok": box.button(QDialogButtonBox.StandardButton.Ok).text(),
        "cancel": box.button(QDialogButtonBox.StandardButton.Cancel).text(),
    }


def _message_box_texts() -> dict:
    """정적 QMessageBox.warning/information/critical(OK)과 warning(Yes|No)이 만드는 것과 같은 상자."""
    ok_box = QMessageBox(QMessageBox.Icon.Warning, "t", "m", QMessageBox.StandardButton.Ok)
    yn_box = QMessageBox(QMessageBox.Icon.Warning, "t", "m",
                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    texts = {
        "ok": ok_box.button(QMessageBox.StandardButton.Ok).text(),
        "yes": yn_box.button(QMessageBox.StandardButton.Yes).text(),
        "no": yn_box.button(QMessageBox.StandardButton.No).text(),
    }
    ok_box.deleteLater()
    yn_box.deleteLater()
    return texts


class TestKorean:
    def test_setting_dialog_ok_cancel_are_korean(self, qapp, translators):
        translators("ko_KR")
        dialog = SettingDialog()
        try:
            assert _box_texts(dialog.dialogButtonBox) == {"ok": KOREAN["ok"], "cancel": KOREAN["cancel"]}
        finally:
            dialog.deleteLater()

    def test_message_box_standard_buttons_are_korean(self, qapp, translators):
        translators("ko_KR")
        assert _message_box_texts() == {"ok": KOREAN["ok"], "yes": KOREAN["yes"], "no": KOREAN["no"]}

    def test_every_message_box_call_site_uses_standard_buttons_only(self):
        """정적 QMessageBox 호출 13곳이 전부 표준 버튼이라 카탈로그 하나로 덮인다 — 문구를
        직접 준 버튼(addButton/setButtonText)이 생기면 이 가정이 깨진다."""
        import re

        root = Path(main.__file__).resolve().parent
        calls, custom = 0, []
        for path in sorted((root / "app").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            calls += len(re.findall(r"QMessageBox\.(warning|information|critical|question)\(", text))
            if re.search(r"setButtonText\(|\.addButton\(\s*[\"']", text):
                custom.append(path.relative_to(root).as_posix())
        assert calls == 13, f"정적 QMessageBox 호출이 {calls}곳 — 13곳 전제가 바뀌었다(게이트 갱신)"
        assert custom == [], f"문구를 직접 준 버튼이 있다 — 카탈로그가 덮지 못한다: {custom}"


class TestEnglish:
    def test_english_setting_keeps_english_buttons(self, qapp, translators):
        """영어 설정에서는 카탈로그를 얹지 않는다 — 한국어 카탈로그가 섞이면 문구가 갈린다."""
        result = translators("en_US")
        assert len(result) == 1, "영어에는 Qt 카탈로그가 없어야 한다(원문)"
        dialog = SettingDialog()
        try:
            assert _box_texts(dialog.dialogButtonBox) == {"ok": ENGLISH["ok"], "cancel": ENGLISH["cancel"]}
        finally:
            dialog.deleteLater()
        assert _message_box_texts() == {"ok": ENGLISH["ok"], "yes": ENGLISH["yes"], "no": ENGLISH["no"]}


class TestInstallOrder:
    def test_later_installed_translator_wins(self, qapp):
        """Qt 규칙의 대조군 — 같은 키를 가진 번역기 둘 중 나중에 설치한 쪽이 이긴다."""

        class _Stub(QTranslator):
            def __init__(self, tag):
                super().__init__()
                self.tag = tag

            def isEmpty(self):
                return False

            def translate(self, context, source, disambiguation=None, n=-1):
                return f"{self.tag}:{source}" if (context, source) == ("Ctx", "key") else ""

        first, second = _Stub("first"), _Stub("second")
        qapp.installTranslator(first)
        qapp.installTranslator(second)
        try:
            assert QCoreApplication.translate("Ctx", "key") == "second:key"
        finally:
            qapp.removeTranslator(first)
            qapp.removeTranslator(second)

    def test_app_catalog_is_installed_after_the_qt_catalog(self, qapp, translators):
        """우리 번역이 Qt 카탈로그보다 우선하려면 우리 것이 **마지막**에 설치돼야 한다."""
        result = translators("ko_KR")
        assert len(result) == 2, "한국어에는 Qt 카탈로그 + 우리 번역, 둘이 설치돼야 한다"
        qt_catalog, app_catalog = result
        assert qt_catalog is main._qt_translators[-1], "첫 번째가 Qt 카탈로그가 아니다"
        assert app_catalog is not qt_catalog
        # 우리 번역이 살아 있다 — 설정 창 제목 원문이 한국어로 나온다
        assert QCoreApplication.translate("SettingDialog", "Settings") != "Settings"


class TestMissingCatalogDoesNotCrash:
    def test_missing_catalog_leaves_english_and_installs_only_ours(self, qapp, translators, monkeypatch):
        """카탈로그 파일이 없으면 영어로 남는 것이 정상 — 앱은 그대로 뜬다."""
        real = main.qt_catalog_path
        monkeypatch.setattr(main, "qt_catalog_path", lambda language: real(language) + ".missing")
        result = translators("ko_KR")
        assert len(result) == 1
        assert _message_box_texts()["ok"] == ENGLISH["ok"]


class TestPreserved:
    """보존 목록 — 카탈로그가 바꾸는 것은 문구뿐이어야 한다."""

    def _dialog(self, qapp, translators, language):
        translators(language)
        dialog = SettingDialog()
        dialog.show()
        QApplication.processEvents()
        return dialog

    @pytest.mark.parametrize("language", ("ko_KR", "en_US"))
    def test_button_order_default_and_escape(self, qapp, qtbot, translators, language):
        dialog = self._dialog(qapp, translators, language)
        qtbot.addWidget(dialog)
        box = dialog.dialogButtonBox
        ok, cancel = box.button(QDialogButtonBox.StandardButton.Ok), box.button(QDialogButtonBox.StandardButton.Cancel)
        assert ok.x() < cancel.x(), "버튼 순서(OK 왼쪽, Cancel 오른쪽)가 바뀌었다"
        assert ok.isDefault() and not cancel.isDefault(), "기본 버튼(Enter)이 OK가 아니다"
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        QApplication.processEvents()
        assert dialog.result() == QDialog.DialogCode.Rejected and not dialog.isVisible(), "Esc가 취소하지 않는다"

    @pytest.mark.parametrize("language", ("ko_KR", "en_US"))
    def test_tab_order_ends_with_ok_then_cancel(self, qapp, qtbot, translators, language):
        dialog = self._dialog(qapp, translators, language)
        qtbot.addWidget(dialog)
        box = dialog.dialogButtonBox
        ok, cancel = box.button(QDialogButtonBox.StandardButton.Ok), box.button(QDialogButtonBox.StandardButton.Cancel)
        assert ok.nextInFocusChain() is cancel, "탭 순서가 OK → Cancel이 아니다"

    def test_minimum_size_is_reported_for_both_languages(self, qapp, translators, monkeypatch):
        """최소 크기는 버튼 문구 폭에 따라 달라질 수 있다 — 세 조건을 같이 잰다(회귀 감시용).

        en_US / ko_KR / ko_KR(카탈로그 없음). 마지막 둘의 차가 **Qt 카탈로그가 바꾼 몫**이다
        (en_US와 ko_KR의 차는 우리 라벨 번역의 몫이라 섞어 읽으면 안 된다).
        """
        sizes = {}
        real = main.qt_catalog_path
        for tag, language, catalog in (("en_US", "en_US", True), ("ko_KR", "ko_KR", True), ("ko_KR-no-catalog", "ko_KR", False)):
            monkeypatch.setattr(main, "qt_catalog_path", real if catalog else (lambda lang: real(lang) + ".missing"))
            result = translators(language)
            dialog = SettingDialog()
            sizes[tag] = (dialog.minimumSizeHint().width(), dialog.minimumSizeHint().height())
            dialog.deleteLater()
            for translator in result:  # 다음 조건을 재기 전에 이번 번역기를 전부 뗀다
                qapp.removeTranslator(translator)
        assert all(w > 0 and h > 0 for w, h in sizes.values()), sizes
        print("minimumSizeHint(w,h):", sizes)
