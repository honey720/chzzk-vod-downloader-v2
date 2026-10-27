"""카드의 폴더 열기가 띄우는 경고 창의 글이 화면 언어로 나오는지 (#431).

OS가 열기를 거부하게 한 뒤 카드의 폴더 열기 버튼을 누르고, 경고 창(``QMessageBox.warning``)에 넘긴
제목 · 본문을 적는다. 동봉된 번역 카탈로그(ko_KR · en_US)를 앱에 건다.
"""

import pytest
from PySide6.QtCore import QTranslator
from PySide6.QtWidgets import QApplication

import app.widgets.widget as widget_module
import CVDv2 as main_module
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.unit.card_helpers import drop_new_top_levels, snapshot_top_levels


@pytest.fixture(autouse=True)
def _drop_windows(qapp):
    """이 파일의 테스트가 띄운 창을 테스트 끝에 파괴한다(card_helpers.drop_new_top_levels)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """카드가 크기를 묻는 요청을 내보내지 않게 한다."""

    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


@pytest.fixture
def refused(monkeypatch):
    """OS가 폴더 · 파일 열기를 거부하게 하고, 경고 창에 넘긴 (제목, 본문)을 적는다."""
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        widget_module.QProcess, "startDetached", staticmethod(lambda program, arguments: False)
    )
    monkeypatch.setattr(widget_module.QDesktopServices, "openUrl", staticmethod(lambda url: False))
    monkeypatch.setattr(
        widget_module.QMessageBox,
        "warning",
        staticmethod(lambda parent, title, text: warnings.append((title, text))),
    )
    return warnings


@pytest.fixture
def language(qapp, request):
    """동봉된 번역 카탈로그 하나를 앱에 건다 — 테스트가 끝나면 뗀다."""
    translator = QTranslator()
    assert translator.load(main_module.resource_path(f"translations/{request.param}.qm"))
    QApplication.installTranslator(translator)
    yield request.param
    QApplication.removeTranslator(translator)


def _card(tmp_path, state: DownloadState, output_path: str) -> ContentItemWidget:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 60},
        [[1080, "https://v.invalid/1080.mp4"]],
        1080,
        "https://v.invalid/1080.mp4",
        str(tmp_path),
        "video",
        None,
    )
    item.downloadState = state
    item.output_path = output_path
    card = ContentItemWidget(item, 0)
    card.setData(item, 0)
    card.show()
    return card


_FOLDER = {
    "ko_KR": ("경고", "'{0}'을(를) 열 수 없습니다."),
    "en_US": ("Warning", "Could not open '{0}'."),
}
_FILE = {
    "ko_KR": ("경고", "'{0}'을(를) 찾을 수 없습니다."),
    "en_US": ("Warning", "Could not find '{0}'."),
}


@pytest.mark.parametrize("language", ["ko_KR", "en_US"], indirect=True)
def test_folder_open_warning_for_a_folder_is_in_the_screen_language(
    qapp, tmp_path, refused, language
):
    """대기 카드의 폴더 열기를 OS가 거부하면 경고 창의 제목 · 본문이 화면 언어여야 한다.

    번역 카탈로그 ko_KR(또는 en_US)을 걸고, 저장 폴더를 여는 대기 카드에서 폴더 열기 버튼을 누름
    -> 경고 창 하나 — ko: ("경고", "'<폴더>'을(를) 열 수 없습니다.") · en: ("Warning", "Could not open '<폴더>'.")
    """
    card = _card(tmp_path, DownloadState.WAITING, "")

    card.openDirectoryButton.click()

    title, body = _FOLDER[language]
    assert refused == [(title, body.format(str(tmp_path)))]


@pytest.mark.parametrize("language", ["ko_KR", "en_US"], indirect=True)
def test_folder_open_warning_for_a_finished_file_is_in_the_screen_language(
    qapp, tmp_path, refused, language
):
    """완료 카드의 폴더 열기(파일을 고른 채 폴더 열기)를 OS가 거부하면 경고 창의 글이 화면 언어여야 한다.

    번역 카탈로그 ko_KR(또는 en_US)을 걸고, 산출물 파일이 있는 완료 카드에서 폴더 열기 버튼을 누름
    -> 경고 창 하나 — ko: ("경고", "'<파일>'을(를) 찾을 수 없습니다.") · en: ("Warning", "Could not find '<파일>'.")
    """
    output = tmp_path / "제목 1080p.mp4"
    output.write_bytes(b"x")
    card = _card(tmp_path, DownloadState.FINISHED, str(output))

    card.openDirectoryButton.click()

    title, body = _FILE[language]
    assert refused == [(title, body.format(str(output)))]
