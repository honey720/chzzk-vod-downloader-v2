"""실패 예외 → 안내 키 매핑 (#134, #127의 조회 경로 방식) — 순수 함수 단위 검증.

구 tests/unit/test_qt_bridge.py(#75)에서 남긴 부분이다. 브리지 배선을 재던
나머지 11건은 B1(#259) 흡수로 대상 클래스가 사라졌고, 같은 계약을 클래스
이름 없이 재는 tests/app/gui/test_download_viewmodel_contract.py(B0)와 전부
겹쳐 지웠다. 여기 4건은 매핑 함수를 직접 재며, 통지 경로를 거친 표시 문구는
계약 게이트의 `test_failure_reason_headline_by_exception`이 본다.
"""

import pytest
import requests

from app.viewmodels.download_viewmodel import _failure_message_key
from core.chzzk.playback_tracks import STREAM_NOT_FOUND, StreamSelectionError
from core.engine.base import (
    DISK_SPACE_SHORT,
    DISK_SPACE_SHORT_WHOLE,
    DiskSpaceError,
    FileSizeLimitError,
    PostprocessError,
)
from core.sources.hls_aes_downloader import DecryptionError
from core.utils.ffmpeg import FFmpegNotFoundError, RemuxError
from core.sections.hybrid import CUT_FAILED, CUT_TIMEOUT, CUT_UNSUPPORTED, CutError


class _FakeHttpResponse:
    """HTTPError에 실을 상태 코드만 가진 응답 흉내."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> requests.HTTPError:
    return requests.HTTPError(f"HTTP {status}", response=_FakeHttpResponse(status))


def _postprocess_with(cause: BaseException) -> PostprocessError:
    """그 원인을 체인한 후처리 실패."""
    error = PostprocessError("후처리(cut) 실패")
    error.__cause__ = cause
    return error


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

    def test_disk_space_error_maps_to_its_own_key_not_the_save_failure(self):
        """DiskSpaceError는 OSError의 하위지만 저장 실패가 아니라 제 키로 매핑돼야 한다 (#309).

        DiskSpaceError(필요 10, 여유 5) -> "Not enough disk space"
        """
        assert _failure_message_key(DiskSpaceError(10, 5)) == DISK_SPACE_SHORT
        assert DISK_SPACE_SHORT == "Not enough disk space"

    def test_file_size_limit_error_maps_to_its_own_key(self):
        """FileSizeLimitError는 OSError의 하위지만 저장 실패가 아니라 제 키로 매핑돼야 한다 (#309).

        FileSizeLimitError(5GiB, "FAT32") -> "File too large for a FAT32 drive"
        """
        error = FileSizeLimitError(5 * 1024**3, "FAT32")

        assert _failure_message_key(error) == "File too large for a FAT32 drive"

    @pytest.mark.parametrize(
        "make",
        [
            lambda: _postprocess_with(CutError(CUT_FAILED, "exit 1")),
            lambda: _postprocess_with(RemuxError("exit 1: File too large")),
            lambda: OSError(28, "No space left on device"),
        ],
        ids=["cut-failure", "remux-failure", "oserror"],
    )
    def test_space_key_written_by_the_engine_wins_over_the_kind_of_the_error(self, make):
        """엔진이 space_key를 적어 둔 실패는 예외의 종류와 무관하게 그 키로 매핑돼야 한다 (#309).

        컷 실패(PostprocessError ← CutError) · 재포장 실패(PostprocessError ← RemuxError) · OSError에
        space_key = "File too large for a FAT32 drive"
        -> 셋 다 그 키("Section cut failed" · "Postprocessing failed - invalid segments" ·
           "Failed to save file"이 아니다)
        """
        error = make()
        error.space_key = "File too large for a FAT32 drive"

        assert _failure_message_key(error) == "File too large for a FAT32 drive"

    @pytest.mark.parametrize(
        "make",
        [
            lambda: _postprocess_with(CutError(CUT_FAILED, "exit 1: File too large")),
            lambda: _postprocess_with(RemuxError("exit 1: File too large")),
        ],
        ids=["cut-failure", "remux-failure"],
    )
    def test_save_failure_key_written_by_the_engine_wins_over_cut_and_corruption(self, make):
        """엔진이 space_key에 저장 실패 키를 적은 후처리 실패는 그 키로 매핑돼야 한다 (#309).

        컷 실패(PostprocessError ← CutError) · 재포장 실패(PostprocessError ← RemuxError)에
        space_key = "Failed to save file"
        -> 둘 다 "Failed to save file"("Section cut failed" ·
           "Postprocessing failed - invalid segments"가 아니다)
        """
        error = make()
        error.space_key = "Failed to save file"

        assert _failure_message_key(error) == "Failed to save file"

    def test_disk_space_error_of_a_download_without_sections_maps_to_its_own_key(self):
        """구간 없는 다운로드의 DiskSpaceError는 구간 파일을 말하지 않는 키로 매핑돼야 한다 (#309).

        DiskSpaceError(필요 10, 여유 5, 구간 없는 다운로드의 키)
        -> "Not enough disk space for the download"
        """
        error = DiskSpaceError(10, 5, DISK_SPACE_SHORT_WHOLE)

        assert _failure_message_key(error) == DISK_SPACE_SHORT_WHOLE
        assert DISK_SPACE_SHORT_WHOLE == "Not enough disk space for the download"

    def test_stream_selection_error_maps_to_its_own_key(self):
        """StreamSelectionError는 그 예외가 든 키로 매핑돼야 한다 (#318).

        StreamSelectionError("720 해상도 스트림을 찾을 수 없습니다.")
        -> STREAM_NOT_FOUND
        """
        error = StreamSelectionError("720 해상도 스트림을 찾을 수 없습니다.")

        assert _failure_message_key(error) == STREAM_NOT_FOUND

    def test_unknown_exception_has_no_key(self):
        assert _failure_message_key(RuntimeError("anything")) is None

    def test_cut_failure_has_its_own_key(self):
        """PostprocessError의 원인이 CutError면 컷 실패의 키여야 한다 (#309).

        원인이 CutError(CUT_FAILED) · CutError(CUT_TIMEOUT) · CutError(CUT_UNSUPPORTED)
        -> 셋 모두 "Section cut failed"
        """
        keys = []
        for cut_key in (CUT_FAILED, CUT_TIMEOUT, CUT_UNSUPPORTED):
            error = PostprocessError("후처리(cut) 실패")
            error.__cause__ = CutError(cut_key, "시험")
            keys.append(_failure_message_key(error))

        assert keys == ["Section cut failed"] * 3

    def test_cut_failure_caused_by_missing_ffmpeg_keeps_the_ffmpeg_key(self):
        """컷이 ffmpeg 실행 파일을 찾지 못해 실패했으면 ffmpeg를 찾지 못했다는 키여야 한다 (#309).

        PostprocessError ← CutError ← FFmpegNotFoundError
        -> "Postprocessing failed - ffmpeg not found"
        """
        cut = CutError(CUT_FAILED, "ffmpeg 없음")
        cut.__cause__ = FFmpegNotFoundError("imageio-ffmpeg 패키지 미설치")
        error = PostprocessError("후처리(cut) 실패")
        error.__cause__ = cut

        assert _failure_message_key(error) == "Postprocessing failed - ffmpeg not found"
