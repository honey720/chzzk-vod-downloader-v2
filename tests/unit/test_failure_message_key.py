"""실패 예외 → 안내 키 매핑 (#134, #127의 조회 경로 방식) — 순수 함수 단위 검증.

구 tests/unit/test_qt_bridge.py(#75)에서 남긴 부분이다. 브리지 배선을 재던
나머지 11건은 B1(#259) 흡수로 대상 클래스가 사라졌고, 같은 계약을 클래스
이름 없이 재는 tests/unit/test_download_viewmodel_contract.py(B0)와 전부
겹쳐 지웠다. 여기 4건은 매핑 함수를 직접 재며, 통지 경로를 거친 표시 문구는
계약 게이트의 `test_failure_reason_headline_by_exception`이 본다.
"""

import requests

from app.viewmodels.download_viewmodel import _failure_message_key
from core.downloaders.base import PostprocessError
from core.downloaders.hls_aes_downloader import DecryptionError
from core.utils.ffmpeg import FFmpegNotFoundError, RemuxError


class _FakeHttpResponse:
    """HTTPError에 실을 상태 코드만 가진 응답 흉내."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> requests.HTTPError:
    return requests.HTTPError(f"HTTP {status}", response=_FakeHttpResponse(status))


class TestFailureMessageKey:
    """실패 예외 → 안내 키 매핑 (#134, #127의 조회 경로 방식)."""

    def test_postprocess_maps_by_cause_type(self):
        """PostprocessError는 원인(FFmpegError 하위 타입)에 따라 다른 키로 갈린다 (#180).

        FFmpegNotFoundError(실행 파일 자체를 못 찾음)와 RemuxError(ffmpeg는
        돌았으나 입력이 무효함)는 유저가 할 수 있는 조치가 다르다 — 하나로
        뭉뚱그린 "ffmpeg 설치 상태를 확인해 주세요"가 #180 초기 진단을
        엉뚱한 방향(Gatekeeper·서명)으로 세 단계나 끌고 간 원인이었다.
        """
        not_found = PostprocessError("ffmpeg 실행 파일을 찾지 못했다")
        not_found.__cause__ = FFmpegNotFoundError("imageio-ffmpeg 패키지 미설치")
        assert _failure_message_key(not_found) == "Postprocessing failed - ffmpeg not found"

        invalid_input = PostprocessError("ffmpeg stderr...")
        invalid_input.__cause__ = RemuxError("exit 183: Invalid data found")
        assert _failure_message_key(invalid_input) == "Postprocessing failed - invalid segments"

        # 원인 체인이 없는 경우(예외적)도 "설치 안내"로 오도하지 않는다 —
        # 안전한 기본값은 손상 쪽이다(설치 안내는 확신 있을 때만 보여준다)
        no_cause = PostprocessError("cause 없음")
        assert _failure_message_key(no_cause) == "Postprocessing failed - invalid segments"

        assert _failure_message_key(DecryptionError("키·IV 불일치")) == "Decryption failed"

    def test_http_statuses_map_like_metadata_path(self):
        assert _failure_message_key(_http_error(403)) == "Viewing permission required"
        assert _failure_message_key(_http_error(401)) == "Viewing permission required"
        assert _failure_message_key(_http_error(404)) == "Video not found"
        assert _failure_message_key(_http_error(500)) == "Network connection error"

    def test_transport_and_os_errors(self):
        assert _failure_message_key(requests.ConnectionError("boom")) == (
            "Network connection error"
        )
        assert _failure_message_key(requests.Timeout("slow")) == "Network connection error"
        assert _failure_message_key(OSError(28, "No space left", "C:\\full\\path.mp4")) == (
            "Failed to save file"
        )

    def test_unknown_exception_has_no_key(self):
        assert _failure_message_key(RuntimeError("anything")) is None
