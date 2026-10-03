"""잘린 세그먼트 실패가 카드의 사유로 이어지는지 — 실패 키 매핑과 동봉된 번역 (#321)."""

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QTranslator

import main as main_module
from app.viewmodels.download_viewmodel import DownloadViewModel, _failure_message_key
from core.downloaders.integrity import SEGMENT_TRUNCATED, TruncatedSegmentError

# DownloadViewModel._failure_message가 tr()에 넘기는 원문
SOURCE = (
    "Video data arrived corrupted · try again later\n"
    "Part of the video kept arriving incomplete from the server. Try again later."
)


def test_truncated_segment_error_maps_to_its_own_key():
    """TruncatedSegmentError는 그 예외가 든 키로 매핑돼야 한다.

    TruncatedSegmentError("마지막 moof 뒤에 mdat가 없다")
    -> SEGMENT_TRUNCATED ("Segment was received truncated")
    """
    error = TruncatedSegmentError("마지막 moof 뒤에 mdat가 없다")

    assert SEGMENT_TRUNCATED == "Segment was received truncated"
    assert _failure_message_key(error) == SEGMENT_TRUNCATED


def test_truncated_segment_failure_shows_its_reason_on_the_card(qapp):
    """잘린 세그먼트 실패의 카드 사유는 그 실패의 원문이어야 한다(번역기 없음).

    DownloadViewModel(가짜 content · service)._failure_message(TruncatedSegmentError(...))
    -> SOURCE
    """
    viewmodel = DownloadViewModel(content=MagicMock(), service=MagicMock())

    assert viewmodel._failure_message(TruncatedSegmentError("잘렸다")) == SOURCE


@pytest.mark.parametrize(
    ("language", "headline"),
    [
        ("ko_KR", "영상 일부가 잘려 받아졌습니다 · 잠시 뒤 다시 시도해 주세요"),
        ("en_US", "Video data arrived corrupted · try again later"),
    ],
)
def test_truncated_segment_reason_is_translated_in_the_bundled_catalog(qapp, language, headline):
    """동봉된 번역 카탈로그는 잘린 세그먼트 실패 사유를 그 언어의 두 줄 문구로 돌려줘야 한다.

    translations/<언어>.qm, 컨텍스트 DownloadViewModel, 원문 SOURCE
    -> 첫 줄이 주석의 문구이고 둘째 줄이 비어 있지 않다
    """
    translator = QTranslator()
    assert translator.load(main_module.resource_path(f"translations/{language}.qm"))

    first_line, _, detail = translator.translate("DownloadViewModel", SOURCE).partition("\n")

    assert first_line == headline
    assert detail.strip()
