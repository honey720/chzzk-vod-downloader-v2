"""DownloadPlan 계약 검증 — prepare()의 계획 반환과 베이스의 소비 (#83).

검증 범위:
- DownloadPlan 모델: 불변(frozen), 기본값(빈 selections = 전체 다운로드), part_count
- 두 다운로더의 prepare()가 DownloadPlan을 반환하고, 계획 필드가 현행
  실행에 필요한 정보(items·total_size·requires_postprocess)를 담는다
- 구간을 해석하지 못하는 다운로더가 낸 계획에 selections가 있으면 베이스 run()이 명시적
  미지원 예외를 낸다. file(mp4) · m3u8(HLS fMP4) · hls_aes(암호화 HLS TS)는 구간을 받는다 —
  그 경로는 tests/unit/core/test_file_sections.py · test_m3u8_sections.py ·
  test_hls_aes_sections.py가 본다 (#309)

selections가 빈 값일 때 현행과 동일 동작인 것은 기존 실행 테스트
(test_file_downloader_run / test_m3u8_downloader_run)와 규칙 박제 테스트가
무수정으로 통과하는 것으로 확인한다.
"""

import dataclasses

import pytest

import core.downloaders.file_downloader as fd_module
import core.downloaders.m3u8_downloader as m3u8_module
from core.downloaders.base import BaseDownloader
from core.downloaders.file_downloader import FileDownloader
from core.downloaders.hls_aes_downloader import HlsAesDownloader
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.downloaders.ranges import split_ranges
from core.models.plan import DownloadPlan, TimeRange
from core.models.download_data import DownloadData

MB = 1024 * 1024


class RecordingLogger:
    """호출된 로그 메서드 이름만 기록하는 DownloadLogger 호환 페이크."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)

        return record


# ================================================================ 모델 계약


def test_plan_is_frozen_and_defaults_to_full_download():
    """계획은 불변이고, selections 기본값(빈 튜플)은 전체 다운로드를 뜻한다."""
    plan = DownloadPlan(items=((0, 9), (10, 19)), total_size=20)

    assert plan.selections == ()
    assert plan.requires_postprocess is False
    assert plan.part_count == 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.total_size = 999


def test_time_range_holds_seconds():
    """TimeRange는 초 단위 (start, end) 구간을 담는다 — 다중 구간은 튜플로 나열한다."""
    plan = DownloadPlan(items=(), selections=(TimeRange(0.0, 10.0), TimeRange(30.0, 45.5)))

    assert plan.selections[1].end == pytest.approx(45.5)
    assert plan.part_count == 0


# ================================================================ file prepare


class HeadSession:
    """HEAD로 content-length만 답하는 가짜 세션 (_get_total_size 최소 인터페이스)."""

    def __init__(self, size: int):
        self.headers = {"content-length": str(size)}

    def head(self, url, **kwargs):
        return self

    def raise_for_status(self):
        pass


def _make_file_data() -> DownloadData:
    return DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path="unused.mp4",
        resolution=144,  # 파트 1MB
        content_type="video",
    )


def test_file_prepare_returns_plan_with_ranges_and_total_size(monkeypatch):
    """file 계획: items는 바이트 범위 튜플, 총 크기를 미리 알아 total_size를 채운다."""
    total = 3 * MB + 512
    data = _make_file_data()
    engine = FileDownloader(data, RecordingLogger())
    monkeypatch.setattr(fd_module, "get_thread_session", lambda: HeadSession(total))

    plan = engine.prepare(data.content)

    assert isinstance(plan, DownloadPlan)
    assert plan.items == tuple(split_ranges(total, MB))
    assert plan.total_size == total
    assert plan.part_count == 4
    assert plan.requires_postprocess is False  # file은 후처리(병합) 없음
    assert plan.selections == ()  # 전체 다운로드가 기본 케이스


# ================================================================ m3u8 prepare


class PlaylistSession:
    """플레이리스트 텍스트만 답하는 가짜 세션 (prepare 최소 인터페이스)."""

    def __init__(self, text: str):
        self._text = text

    def get(self, url, **kwargs):
        class _Response:
            text = self._text

            def raise_for_status(self):
                pass

        return _Response()


def _make_m3u8_data() -> DownloadData:
    return DownloadData(
        base_url="https://example.invalid/hls/video.m3u8",
        vod_url="https://chzzk.naver.com/video/1",
        output_path="unused.mp4",
        resolution=1080,
        content_type="m3u8",
    )


def test_m3u8_prepare_returns_plan_with_segments(monkeypatch):
    """m3u8 계획: items는 (index, 세그먼트), 총 크기 미상(None), 병합 후처리 필요."""
    playlist = "\n".join(
        ["#EXTM3U", '#EXT-X-MAP:URI="init.m4s"']
        + [line for i in range(3) for line in ("#EXTINF:2.000,", f"seg_{i}.m4v")]
        + ["#EXT-X-ENDLIST"]
    )
    data = _make_m3u8_data()
    engine = M3U8Downloader(data, RecordingLogger())
    monkeypatch.setattr(m3u8_module, "get_thread_session", lambda: PlaylistSession(playlist))

    plan = engine.prepare(data.content)

    assert isinstance(plan, DownloadPlan)
    assert plan.items == ((0, "seg_0.m4v"), (1, "seg_1.m4v"), (2, "seg_2.m4v"))
    assert plan.total_size is None  # 총 바이트 크기는 미리 알 수 없다
    assert plan.part_count == 3
    assert plan.requires_postprocess is True  # 병합 필요
    assert plan.selections == ()


# ================================================================ selections 거부


def _selection_plan() -> DownloadPlan:
    return DownloadPlan(items=((0, MB - 1),), total_size=MB, selections=(TimeRange(0.0, 10.0),))


def test_file_m3u8_and_hls_aes_accept_selections_and_the_default_does_not():
    """구간을 받는 다운로더는 file · m3u8 · hls_aes이고, 기본값은 받지 않아야 한다 (#309).

    BaseDownloader · FileDownloader · M3U8Downloader · HlsAesDownloader의 supports_selections
    -> 기본 False, file · m3u8 · hls_aes True
    """
    assert BaseDownloader.supports_selections is False
    assert FileDownloader.supports_selections is True
    assert M3U8Downloader.supports_selections is True
    assert HlsAesDownloader.supports_selections is True


def test_base_run_rejects_selections_for_a_downloader_that_does_not_support_them(
    tmp_path, monkeypatch
):
    """구간을 받지 않는 다운로더가 낸 계획에 selections가 있으면 run()은 미지원 예외를 실패로 통지해야 한다.

    M3U8Downloader의 supports_selections를 False로 바꾸고 prepare가 selections 있는 계획을 내도록 함
    -> 실패 1건(NotImplementedError), 산출물 없음
    """
    failures: list[BaseException] = []
    data = _make_m3u8_data()
    data.output_path = str(tmp_path / "out.mp4")
    engine = M3U8Downloader(data, RecordingLogger(), on_failed=failures.append)
    monkeypatch.setattr(engine, "supports_selections", False)
    monkeypatch.setattr(engine, "prepare", lambda content: _selection_plan())

    data.model.start()
    engine.run()

    assert len(failures) == 1
    assert isinstance(failures[0], NotImplementedError)
    assert not (tmp_path / "out.mp4").exists()
