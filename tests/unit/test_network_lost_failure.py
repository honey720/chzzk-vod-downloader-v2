"""회선 끊김 실패가 카드의 사유로 이어지는지 — 실패 키 매핑과 동봉된 번역 (#329)."""

from unittest.mock import MagicMock

import pytest
import requests
from PySide6.QtCore import QTranslator

import CVDv2 as main_module
from app.viewmodels.download_viewmodel import DownloadViewModel, _failure_message_key
from core.downloaders.base import NETWORK_LOST, NetworkLostError

# DownloadViewModel._failure_message가 tr()에 넘기는 원문
SOURCE = (
    "Network connection lost · check it and try again\n"
    "Nothing arrived for a while because the network connection was lost. "
    "Check your connection and try again."
)


def test_network_lost_error_maps_to_its_own_key():
    """NetworkLostError는 일반 연결 오류 키가 아니라 그 예외가 든 키로 매핑돼야 한다.

    NetworkLostError(10.0), 원인 ConnectionError
    -> NETWORK_LOST ("Network connection lost")
    """
    error = NetworkLostError(10.0)
    error.__cause__ = requests.ConnectionError("끊김")

    assert NETWORK_LOST == "Network connection lost"
    assert _failure_message_key(error) == NETWORK_LOST


def test_network_lost_failure_shows_its_reason_on_the_card(qapp):
    """회선 끊김 실패의 카드 사유는 그 실패의 원문이어야 한다(번역기 없음).

    DownloadViewModel(가짜 content · service)._failure_message(NetworkLostError(10.0))
    -> SOURCE
    """
    viewmodel = DownloadViewModel(content=MagicMock(), service=MagicMock())

    assert viewmodel._failure_message(NetworkLostError(10.0)) == SOURCE


@pytest.mark.parametrize(
    ("language", "headline"),
    [
        ("ko_KR", "네트워크 연결이 끊겼습니다 · 연결을 확인하고 다시 시도해 주세요"),
        ("en_US", "Network connection lost · check it and try again"),
    ],
)
def test_network_lost_reason_is_translated_in_the_bundled_catalog(qapp, language, headline):
    """동봉된 번역 카탈로그는 회선 끊김 실패 사유를 그 언어의 두 줄 문구로 돌려줘야 한다.

    translations/<언어>.qm, 컨텍스트 DownloadViewModel, 원문 SOURCE
    -> 첫 줄이 주석의 문구이고 둘째 줄이 비어 있지 않다
    """
    translator = QTranslator()
    assert translator.load(main_module.resource_path(f"translations/{language}.qm"))

    first_line, _, detail = translator.translate("DownloadViewModel", SOURCE).partition("\n")

    assert first_line == headline
    assert detail.strip()
