"""스트림 선택 실패 사유의 번역이 동봉된 .qm에 들어 있는지 (#318)."""

import pytest
from PySide6.QtCore import QTranslator

import CVDv2 as main_module

# DownloadViewModel._failure_message가 tr()에 넘기는 원문
SOURCE = (
    "Stream not found · pick another resolution\n"
    "The stream for the selected resolution could not be found. Try another resolution."
)


@pytest.mark.parametrize(
    ("language", "headline"),
    [
        ("ko_KR", "선택한 해상도를 받을 수 없습니다 · 다른 해상도를 골라 주세요"),
        ("en_US", "Stream not found · pick another resolution"),
    ],
)
def test_stream_failure_reason_is_translated_in_the_bundled_catalog(qapp, language, headline):
    """동봉된 번역 카탈로그는 스트림 선택 실패 사유를 그 언어의 두 줄 문구로 돌려줘야 한다.

    translations/<언어>.qm, 컨텍스트 DownloadViewModel, 원문 SOURCE
    -> 첫 줄이 주석의 문구이고 둘째 줄이 비어 있지 않다
    """
    translator = QTranslator()
    assert translator.load(main_module.resource_path(f"translations/{language}.qm"))

    first_line, _, detail = translator.translate("DownloadViewModel", SOURCE).partition("\n")

    assert first_line == headline
    assert detail.strip()


@pytest.mark.parametrize(
    ("language", "slow", "connection"),
    [
        (
            "ko_KR",
            "서버 응답이 늦습니다. 잠시 뒤 다시 시도해 주세요.",
            "네트워크 연결을 확인해 주세요.",
        ),
        (
            "en_US",
            "The server is responding slowly. Please try again in a moment.",
            "Please check your network connection.",
        ),
    ],
)
def test_slow_response_has_its_own_wording_in_the_bundled_catalog(qapp, language, slow, connection):
    """동봉된 번역 카탈로그는 영상 정보의 응답 지연을 연결 실패와 다른 문구로 돌려줘야 한다 (#331).

    translations/<언어>.qm, 컨텍스트 FetchJob, 원문 "Server response is slow" ·
    "Network connection error"
    -> 각각 주석의 문구, 두 문구가 다르다
    """
    translator = QTranslator()
    assert translator.load(main_module.resource_path(f"translations/{language}.qm"))

    assert translator.translate("FetchJob", "Server response is slow") == slow
    assert translator.translate("FetchJob", "Network connection error") == connection
