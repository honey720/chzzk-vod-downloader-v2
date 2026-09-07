"""설정 화면 계약 게이트 — 재작성(#244 ③) 전에 경계를 고정한다.

⚠️ 성공 조건: 설정 화면을 코드로 다시 짠 뒤 **이 파일이 한 줄도 바뀌지 않은 채
통과**한다. 그래서 바뀔 것은 계약에 넣지 않았다 — 레이아웃(QFormLayout 구조·위젯
폭·창 크기), 라벨·버튼 문자열, "창을 만들면 설정 파일이 생긴다"(지금 동작이지만
바람직하지 않아 굳히지 않는다. 모든 테스트가 파일을 미리 만들어 두고 시작한다).

계약 넷:
  (a) 쿠키 두 칸에 값을 넣고 OK 하면 그 값이 설정 파일에 실린다
  (b) OK 는 세 키(cookies · afterDownload · language)를 저장한다
  (c) Cancel 은 아무것도 쓰지 않는다 — 파일 바이트가 그대로다
  (d) 콤보 두 개의 선택지 집합과 각 선택지가 저장하는 값

판정은 **설정 파일의 전후 비교**(json 직접 읽기 — 제품의 load_config·정규화를 거치지
않는다)와 `accept()`/`reject()` 호출로 한다. 위젯 이름은 "값을 입력한다"에만 필요하다.

⚠️ **objectName 넷은 계약의 일부다**: `nidaut` · `nidses` (QLineEdit) ·
`afterDownload` · `language` (QComboBox). 재작성이 이 이름을 유지해야 이 게이트가
그대로 통과한다. QSS·번역 어느 쪽도 이 이름에 묶여 있지 않으므로(조사 실측) 유지
비용은 없다. 그 밖의 위젯·클래스 내부는 보지 않는다.
"""

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

import config.config as config
from app.views.dialog import SettingDialog

AFTER_DOWNLOAD_CHOICES = {"none", "sleep", "shutdown"}
LANGUAGE_CHOICES = {"en_US", "ko_KR"}


def _seed_config() -> Path:
    """설정 파일을 기본값으로 미리 만든다 — 창 생성이 파일을 만드는 동작에 기대지 않는다."""
    config.save_config(config.default_config())
    return Path(config.CONFIG_FILE)


def _read_raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _line_edit(dialog: SettingDialog, name: str) -> QLineEdit:
    w = dialog.findChild(QLineEdit, name)
    assert w is not None, f"계약: QLineEdit objectName {name!r}가 있어야 한다"
    return w


def _combo(dialog: SettingDialog, name: str) -> QComboBox:
    w = dialog.findChild(QComboBox, name)
    assert w is not None, f"계약: QComboBox objectName {name!r}가 있어야 한다"
    return w


def _choices(combo: QComboBox) -> dict[str, int]:
    """콤보의 저장 값(itemData) → 인덱스. 표시 문자열은 보지 않는다(바뀔 대상)."""
    return {combo.itemData(i): i for i in range(combo.count())}


# ================================================================ (a)


def test_cookies_typed_then_ok_land_in_the_config_file(qapp):
    path = _seed_config()
    dlg = SettingDialog()
    _line_edit(dlg, "nidaut").setText("aut-typed")
    _line_edit(dlg, "nidses").setText("ses-typed")
    dlg.accept()
    dlg.deleteLater()
    QApplication.processEvents()

    assert _read_raw(path)["cookies"] == {"NID_AUT": "aut-typed", "NID_SES": "ses-typed"}


# ================================================================ (b)


def test_ok_writes_all_three_keys(qapp):
    """쿠키·다운로드 후 동작·언어 셋 다 — 위젯에 넣은 값 그대로 파일에 실린다."""
    path = _seed_config()
    dlg = SettingDialog()
    _line_edit(dlg, "nidaut").setText("A")
    _line_edit(dlg, "nidses").setText("S")
    after = _combo(dlg, "afterDownload")
    after.setCurrentIndex(_choices(after)["sleep"])
    lang = _combo(dlg, "language")
    lang.setCurrentIndex(_choices(lang)["ko_KR"])
    dlg.accept()
    dlg.deleteLater()
    QApplication.processEvents()

    saved = _read_raw(path)
    assert saved["cookies"] == {"NID_AUT": "A", "NID_SES": "S"}
    assert saved["afterDownload"] == "sleep"
    assert saved["language"] == "ko_KR"


# ================================================================ (c)


def test_cancel_writes_nothing(qapp):
    """모든 칸을 바꾸고 Cancel — 파일 바이트가 그대로다(내용 비교가 아니라 바이트)."""
    path = _seed_config()
    before = path.read_bytes()
    dlg = SettingDialog()
    _line_edit(dlg, "nidaut").setText("changed")
    _line_edit(dlg, "nidses").setText("changed")
    after = _combo(dlg, "afterDownload")
    after.setCurrentIndex(_choices(after)["shutdown"])
    lang = _combo(dlg, "language")
    lang.setCurrentIndex(_choices(lang)["ko_KR"])
    dlg.reject()
    dlg.deleteLater()
    QApplication.processEvents()

    assert path.read_bytes() == before, "Cancel이 설정 파일을 건드렸다"


# ================================================================ (d)


def test_after_download_choices_and_stored_values(qapp):
    _seed_config()
    dlg = SettingDialog()
    assert set(_choices(_combo(dlg, "afterDownload"))) == AFTER_DOWNLOAD_CHOICES
    dlg.deleteLater()
    QApplication.processEvents()


def test_language_choices_and_stored_values(qapp):
    _seed_config()
    dlg = SettingDialog()
    assert set(_choices(_combo(dlg, "language"))) == LANGUAGE_CHOICES
    dlg.deleteLater()
    QApplication.processEvents()


@pytest.mark.parametrize("choice", sorted(AFTER_DOWNLOAD_CHOICES))
def test_each_after_download_choice_is_what_gets_saved(qapp, choice):
    path = _seed_config()
    dlg = SettingDialog()
    combo = _combo(dlg, "afterDownload")
    combo.setCurrentIndex(_choices(combo)[choice])
    dlg.accept()
    dlg.deleteLater()
    QApplication.processEvents()
    assert _read_raw(path)["afterDownload"] == choice


@pytest.mark.parametrize("choice", sorted(LANGUAGE_CHOICES))
def test_each_language_choice_is_what_gets_saved(qapp, choice):
    path = _seed_config()
    dlg = SettingDialog()
    combo = _combo(dlg, "language")
    combo.setCurrentIndex(_choices(combo)[choice])
    dlg.accept()
    dlg.deleteLater()
    QApplication.processEvents()
    assert _read_raw(path)["language"] == choice
