"""구간 다운로드의 카드 표시 — 합친 진행률 · 완료 구간 수 · 컷 단계 문구 · 일부 실패 (#309).

mainWindow와 같은 배선(ContentViewModel ↔ ContentListView ↔ DownloadViewModel)을 최소
하네스로 세우고, 엔진 자리는 서비스 대역이 맡는다. 테스트가 엔진처럼 공유 데이터
(DownloadData)에 구간 상태를 적고 서비스에 등록된 콜백을 불러, 뷰모델 → content → 카드
위젯까지 실제 시그널로 지나가게 한다. 실제 다운로드 · 네트워크는 없다.

핵심 계약:
- 진행 막대는 전송과 컷을 합친 하나다 — 한 일만큼 차고(#390), 두 단계가 함께 남은 동안의 몫은
  받던 시간으로 본 전송 예상 시간과 컷 예상 시간(구간 수와 구간 길이에서 어림)으로 나눈다.
  여기서는 시계를 고정해 전송이 컷 예상 시간의 4.02배 걸리게 한다(전송이 막대의 약 80%).
  전달 방식(파일 · 세그먼트)이 달라도 같은 진행에는 같은 값이 나온다
- 받기가 멈춘 동안(정체 · 일시정지) 막대는 서 있고, 재개해도 뛰지 않는다. 받아 둔 것을 다시
  쓰는 재시도의 막대도 0에서 찬다
- 완료 구간 수는 엔진이 적어 둔 구간 상태에서 읽는다 — 같은 통지가 두 번 와도 늘지 않는다
- 컷 단계의 카드에는 속도 · 남은 시간 대신 단계 문구가 나온다
- 일부 구간만 자르지 못하면 나머지가 끝난 뒤에 카드가 실패가 되고, 실패한 구간 수와 완료
  구간 수가 사유 앞에 나온다. 엔진은 WAITING으로 끝난다
- 완료 구간 수는 구간이 둘 이상일 때만, 컷 단계부터 나온다(전송 중에는 숨긴다). 전체
  다운로드 카드의 문구는 그대로다
"""

import os
import re

import pytest
from PySide6.QtCore import QObject

import CVDv2 as main_module
import app.theme as theme
import app.viewmodels.download_viewmodel as download_viewmodel_module
from app.download_logger import DownloadLogger
from app.viewmodels.content_viewmodel import ContentViewModel
from app.viewmodels.data import ContentItem
from app.viewmodels.download_viewmodel import DownloadViewModel
from app.widgets.view import ContentListView
from core.engine.base import PostprocessError
from core.models.download_state import DownloadState
from core.models.download_task import DownloadTaskModel
from core.models.events import ProgressEvent
from core.models.plan import TimeRange
from core.models.section_resume import SectionResume
from core.utils.ffmpeg import RemuxError
from core.sections.hybrid import CUT_FAILED, CutError
from core.utils.paths import release_output_paths
from tests.app.gui.card_helpers import hold_style

MB = 1024 * 1024
# 길이가 서로 다른 구간 셋 — 6초 · 11초 · 29초
SELECTIONS = (TimeRange(605.0, 611.0), TimeRange(2520.0, 2531.0), TimeRange(3600.0, 3629.0))


def cut_estimate(*lengths: float) -> float:
    """그 길이(초)의 구간들을 자르는 데 걸릴 시간의 어림 — 구간마다 1초 + 길이 1초에 0.018초."""
    return len(lengths) * 1.0 + sum(lengths) * 0.018


# 전송에 걸리는 시간(초) — 구간 셋의 컷 예상 시간의 4.02배. 전송이 막대의 80.08%를 차지한다
# (꼭 4배로 두면 80이 부동소수점 오차로 79가 될 수 있다)
TRANSFER_SECONDS = 4.02 * cut_estimate(6, 11, 29)


class _Clock:
    """진행 막대의 시계 대역 — 테스트가 시각과 일시정지해 있던 시간의 합을 정한다."""

    now = 0.0
    paused = 0.0  # 일시정지해 있던 시간의 합(초) — `paused_clock` 픽스처가 모델에 건다


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """진행 막대가 재는 시간을 고정한다 — `_Card.transfer`가 전송의 진행만큼 시계를 돌린다."""
    _Clock.now = 0.0
    _Clock.paused = 0.0
    monkeypatch.setattr(download_viewmodel_module, "_clock", lambda: _Clock.now)
    return _Clock


@pytest.fixture
def paused_clock(monkeypatch):
    """다운로드 모델이 알리는 일시정지 시간의 합을 시계 대역의 값으로 바꾼다."""
    monkeypatch.setattr(DownloadTaskModel, "paused_seconds", property(lambda self: _Clock.paused))
    return _Clock


@pytest.fixture(autouse=True)
def _apply_dark_card_qss(qapp):
    """카드를 제품의 스타일로 그린다 — 테스트 하나의 수명 동안만 건다(tests/app/gui/test_failure_display.py와 같은 규칙)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """위젯의 썸네일·파일 크기 조회 스레드가 실네트워크에 나가지 않게 차단한다."""

    class _FailingSession:
        def head(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

        def get(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


@pytest.fixture(autouse=True)
def quiet_download_logger(monkeypatch):
    """DownloadLogger의 파일 생성을 막는다."""
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)


class FakeHandle:
    """DownloadHandle 대역 — 뷰모델이 쓰는 인터페이스만 제공한다."""

    def __init__(self, data):
        self.data = data

    def elapsed_seconds(self) -> float:
        """걸린 시간으로 늘 72초를 돌려준다 — 완료 카드의 "1:12"가 이 값이다."""
        return 72.0

    def wait(self, timeout=None) -> bool:
        """기다릴 것이 없으므로 바로 끝났다고 답한다."""
        return True


class FakeService:
    """DownloadService 대역 — submit 인자(공유 데이터 · 콜백)를 붙잡아 테스트가 엔진처럼 부르게 한다."""

    def __init__(self):
        self.submissions: list[dict] = []

    def submit(self, content, **kwargs):
        """아무것도 돌리지 않고 받은 인자를 적어 둔 뒤 대역 핸들을 돌려준다."""
        self.submissions.append({"content": content, **kwargs})
        return FakeHandle(kwargs["data"])


class WindowHarness(QObject):
    """mainWindow의 다운로드 배선만 재현한 최소 하네스 — 바운드 메서드로 연결한다."""

    def __init__(self, manager: ContentViewModel, viewmodel: DownloadViewModel):
        super().__init__()
        self.manager = manager
        self.viewmodel = viewmodel
        manager.downloadRequested.connect(self.startDownload)

    def startDownload(self, item: ContentItem) -> None:
        """다운로드 요청을 받아 목록과 다운로드 뷰모델을 차례로 시작시킨다."""
        self.manager.start(item)
        self.viewmodel.start(item)

    def retryCard(self, item: ContentItem) -> None:
        """mainWindow.onCardRetry와 같은 일을 한다 — 끝낸 구간을 확인하고 카드를 대기로 되돌린다."""
        self.viewmodel.prepareRetry(item)
        item.stateMessage = ""
        item.downloadState = DownloadState.WAITING
        item.download_progress = 0
        self.manager.model.notifyChanged(item)


class _Card:
    """카드 하나와 그 다운로드의 엔진 자리 — 테스트가 엔진처럼 구간 상태를 적고 콜백을 부른다."""

    def __init__(self, qapp, tmp_path, content_type: str, selections=SELECTIONS):
        self.qapp = qapp
        self.view = ContentListView()
        self.manager = ContentViewModel()
        self.view.bind(self.manager)
        self.service = FakeService()
        self.viewmodel = DownloadViewModel(self.manager, service=self.service)
        self.harness = WindowHarness(self.manager, self.viewmodel)
        self.view.retryRequested.connect(self.harness.retryCard)
        self.item = ContentItem(
            "https://chzzk.naver.com/video/1",
            {"title": "구간 시험", "category": "게임", "channelName": "채널", "duration": 7200},
            [[1080, "http://example.invalid/1080"]],
            1080,
            "http://example.invalid/1080",
            str(tmp_path),
            content_type,
            None,
        )
        self.item.total_size = "595.34 MB"
        self.item.selections = tuple(selections)
        self.transfer_seconds = TRANSFER_SECONDS  # 전송이 끝나기까지 시계가 가는 양
        self.fraction = 0.0  # 마지막으로 알린 전송의 진행
        self._started_at = _Clock.now  # 다운로드를 시작한 때의 시계
        self.manager.model.addItem(self.item)
        qapp.processEvents()

    @property
    def widget(self):
        return self.view.widgetFor(self.item)

    @property
    def status(self) -> str:
        """카드 3행의 상태 문구(원문)."""
        return self.widget.statusLabel.text()

    def start(self, segments: int = 10) -> None:
        """다운로드를 시작하고, 엔진의 prepare가 한 것처럼 구간 수와 전송 단위 수를 적는다."""
        self._started_at = _Clock.now
        self.manager.downloadItem()
        self.submission = self.service.submissions[-1]
        self.data = self.submission["data"]
        self.data.sections_total = len(self.item.selections)
        self.data.max_threads = segments
        self.qapp.processEvents()

    def transfer(self, fraction: float, speed: float = 3.1) -> None:
        """전송이 fraction만큼 진행됐다고 엔진처럼 알린다 — 파일은 바이트, 세그먼트 기반은 세그먼트 수."""
        total = 100 * MB
        self.fraction = fraction
        _Clock.now = self._started_at + self.transfer_seconds * fraction
        self.data.completed_threads = round(self.data.max_threads * fraction)
        self.submission["on_progress"](
            ProgressEvent(
                downloaded_size=round(total * fraction),
                total_size=None if self.item.is_segment_based else total,
                speed=speed,
                active_threads=4,
            )
        )
        self.qapp.processEvents()

    def wait(self, seconds: float, notifications: int = 0) -> None:
        """받는 것 없이 seconds가 지나게 한다 — 그동안 진행 통지를 notifications번 보낸다(정체).

        뒤의 `transfer`가 그만큼 늦은 시각에 오도록 시작 시각도 옮긴다.
        """
        for _ in range(notifications):
            _Clock.now += seconds / notifications
            self.submission["on_progress"](
                ProgressEvent(
                    downloaded_size=round(100 * MB * self.fraction),
                    total_size=None if self.item.is_segment_based else 100 * MB,
                    speed=0.0,
                    active_threads=4,
                )
            )
            self.qapp.processEvents()
        if not notifications:
            _Clock.now += seconds
        self._started_at += seconds

    def begin_cut(self) -> None:
        """전송이 끝나고 컷 단계에 들어갔다고 알린다."""
        self.transfer(1.0)
        self.submission["on_merge_start"]()

    def cut(self, done: int, failed: int = 0) -> None:
        """구간 하나의 컷이 끝났다고 알린다 — 엔진이 하듯 구간 상태를 적고 속도 0의 진행을 보낸다."""
        self.data.sections_done, self.data.sections_failed = done, failed
        self.notify_cut()

    def cut_inside(self, progress: float) -> None:
        """구간을 자르는 도중의 진행을 엔진처럼 알린다 — 컷 진행(0~1)을 적고 통지를 보낸다."""
        self.data.cut_progress = progress
        self.notify_cut()

    def notify_cut(self) -> None:
        """컷 단계의 진행 통지를 한 번 보낸다(구간 상태는 그대로)."""
        self.submission["on_progress"](
            ProgressEvent(
                downloaded_size=100 * MB,
                total_size=None if self.item.is_segment_based else 100 * MB,
                speed=0.0,
                active_threads=0,
            )
        )
        self.qapp.processEvents()

    def retry(self) -> None:
        """실패 카드의 ↻를 누른다 — 위젯 시그널 → 뷰 릴레이 → 하네스."""
        self.widget.retryButton.click()
        self.qapp.processEvents()

    def restart(self, segments: int = 10) -> None:
        """대기로 되돌린 카드의 다운로드를 다시 시작한다 — 새 제출이 생겼음을 먼저 확인한다."""
        before = len(self.service.submissions)
        self.start(segments)
        assert len(self.service.submissions) == before + 1

    def close(self) -> None:
        release_output_paths(self.submission["content"].selection_paths)
        self.view.deleteLater()
        self.qapp.processEvents()


@pytest.fixture
def card(qapp, tmp_path, request):
    """실배선된 카드 하나 — 전달 방식은 테스트의 매개변수("video" · "m3u8" · "hls_aes")."""
    made = _Card(qapp, tmp_path, getattr(request, "param", "hls_aes"))
    yield made
    if hasattr(made, "submission"):
        made.close()


# ================================================================ 합친 진행률


@pytest.mark.parametrize("card", ["video", "m3u8", "hls_aes"], indirect=True)
def test_combined_progress_rises_through_transfer_and_cut_to_100(card):
    """구간 셋의 진행률은 전송과 컷을 합쳐 단조 증가하고, 전달 방식이 달라도 같은 값이며, 마지막에 100이어야 한다.

    길이가 다른 구간 셋. 전송 0 · 30 · 70 · 100% → 컷 단계 → 구간 1 · 2 · 3이 차례로 끝남.
    전송에 컷 예상 시간의 4.02배가 걸림(전송이 막대의 80.08%), 엔진이 컷 진행을 적지 않음
    -> 카드의 진행률 == [0, 24, 56, 80, 86, 93, 100] (컷은 구간이 끝날 때마다 나머지의 1/3),
       진행 막대의 값도 같다
    """
    card.start()
    seen = []
    for fraction in (0.0, 0.3, 0.7):
        card.transfer(fraction)
        seen.append(card.item.download_progress)
    card.begin_cut()
    card.notify_cut()
    seen.append(card.item.download_progress)
    for done in (1, 2, 3):
        card.cut(done)
        seen.append(card.item.download_progress)
        assert card.widget._progressValue() == card.item.download_progress

    assert seen == [0, 24, 56, 80, 86, 93, 100]
    assert seen == sorted(seen)


# ================================================================ 완료 구간 수


def test_done_count_comes_from_the_engine_state_not_from_notifications(card):
    """같은 진행 통지가 여러 번 와도 완료 구간 수는 엔진이 적어 둔 값 그대로여야 한다.

    구간 셋, 컷 단계에서 구간 둘이 끝난 뒤 같은 통지를 세 번 더 보냄
    -> 상태 문구에 "2/3"이 있고 "3/3" · "4/3" · "5/3"은 없다, 아이템의 완료 구간 수 == 2
    """
    card.start()
    card.begin_cut()
    card.cut(1)
    card.cut(2)
    for _ in range(3):
        card.notify_cut()

    assert card.item.sections_done == 2
    assert "2/3" in card.status
    assert not any(wrong in card.status for wrong in ("3/3", "4/3", "5/3"))


def test_done_count_is_hidden_while_transferring_and_shown_from_the_cut_stage(card):
    """완료 구간 수는 전송 중에는 숨기고, 컷 단계와 완료 카드의 상태 문구에 붙어야 한다.

    구간 셋. 전송 40% → 그 상태에서 일시정지 → 재개 → 컷 단계에서 구간 하나 끝남 → 셋 다 끝나고 완료 통지
    -> 전송 중 == "32% · 3.1 MB/s · 0:19 left"(수 없음), 전송 중 일시정지 == "32% · Paused"(수 없음),
       컷 중 == "86% · Cutting · 1/3", 완료 == "✓ Completed · 3/3 · 1:12"
    """
    card.start()
    card.transfer(0.4)
    transferring = card.status
    card.viewmodel.pause()
    card.qapp.processEvents()
    paused = card.status
    card.viewmodel.resume()
    card.qapp.processEvents()
    card.begin_cut()
    card.cut(1)
    cutting = card.status
    card.cut(3)
    card.data.model.finish()
    card.submission["on_finished"]()
    card.qapp.processEvents()

    assert transferring == "32% · 3.1 MB/s · 0:19 left"
    assert paused == "32% · Paused"
    assert cutting == "86% · Cutting · 1/3"
    assert card.item.downloadState is DownloadState.FINISHED
    assert card.status == "✓ Completed · 3/3 · 1:12"


# ================================================================ 컷 단계


@pytest.mark.parametrize("card", ["video", "m3u8", "hls_aes"], indirect=True)
def test_cut_stage_shows_the_stage_text_instead_of_speed_and_remaining_time(card):
    """컷 단계의 카드에는 속도 · 남은 시간이 아니라 단계 문구가 나와야 한다.

    구간 셋. 전송이 끝나고 컷 단계에서 구간 하나가 끝남(엔진은 속도 0의 진행을 보낸다)
    -> 상태 문구 == "86% · Cutting · 1/3", "MB/s" · "N/A" · "left" 없음
    """
    card.start()
    card.begin_cut()
    card.cut(1)

    assert card.item.downloadState is DownloadState.RUNNING
    assert card.status == "86% · Cutting · 1/3"
    assert not any(text in card.status for text in ("MB/s", "N/A", "left"))


def test_cut_stage_says_preparing_while_the_engine_reads_the_segments(card):
    """컷 단계에서 엔진이 프레임 정보를 읽는 동안에는 카드가 자르기 준비 중이라고 적어야 한다.

    구간 셋. 컷 단계에서 엔진이 준비 중(cut_preparing)이라 적고 컷 진행 0.3을 알림 → 준비 중을
    내리고 같은 진행을 다시 알림
    -> 준비 중 == "<진행률>% · Preparing to cut · 0/3", 내린 뒤 == "<같은 진행률>% · Cutting · 0/3",
       아이템의 값이 엔진의 값을 따라간다
    """
    card.start()
    card.begin_cut()
    card.data.cut_preparing = True
    card.cut_inside(0.3)
    preparing = (card.item.cut_preparing, card.status)
    card.data.cut_preparing = False
    card.notify_cut()

    percent = card.item.download_progress
    assert preparing == (True, f"{percent}% · Preparing to cut · 0/3")
    assert (card.item.cut_preparing, card.status) == (False, f"{percent}% · Cutting · 0/3")


# ================================================================ 일부 실패


def test_partial_failure_fails_the_card_only_after_the_remaining_sections_finish(card):
    """구간 하나를 자르지 못해도 카드는 나머지 구간이 끝난 뒤에 실패가 되고, 엔진은 WAITING으로 끝나야 한다.

    구간 셋. 컷 단계에서 구간 1 완료 → 구간 2 실패 → 구간 3 완료 → 엔진이 실패를 알림(PostprocessError)
    -> 구간 2가 실패한 직후와 구간 3이 끝난 직후의 카드는 RUNNING이고 진행률이 93 · 100,
       실패 통지 뒤 카드는 FAILED, 상태 문구가 "✕ 1 failed · 2/3 · "로 시작하고 사유의 첫 줄이 이어진다,
       공유 모델의 상태 == WAITING
    """
    card.start()
    card.begin_cut()
    card.cut(1)
    card.cut(1, failed=1)
    after_failure = (card.item.downloadState, card.item.download_progress)
    card.cut(2, failed=1)
    after_last = (card.item.downloadState, card.item.download_progress, card.status)

    error = PostprocessError("후처리(cut) 실패: 구간 1개")
    error.__cause__ = CutError(CUT_FAILED, "시험")
    card.submission["on_failed"](error)
    card.qapp.processEvents()

    assert after_failure == (DownloadState.RUNNING, 93)
    assert after_last == (DownloadState.RUNNING, 100, "100% · Cutting · 2/3")
    assert card.item.downloadState is DownloadState.FAILED
    assert (card.item.sections_done, card.item.sections_failed) == (2, 1)
    assert card.status.startswith("✕ 1 failed · 2/3 · ")
    assert "\n" not in card.status
    assert card.data.model.state is DownloadState.WAITING


# ================================================================ 구간 요약


def test_waiting_card_shows_the_section_summary_in_the_duration_slot(card):
    """구간이 있는 대기 카드는 재생 시간 자리에 구간 수와 길이의 합을 적어야 한다.

    구간 셋(6 · 11 · 29초 = 46초)
    -> 재생 시간 자리의 글 == "Sections 3 · 0:46"
    """
    assert card.item.downloadState is DownloadState.WAITING
    assert card.widget.fileSizeLabel.text() == "Sections 3 · 0:46"


# ================================================================ 구간이 없거나 하나인 카드


@pytest.mark.parametrize(
    ("content_type", "waiting", "running", "postprocess"),
    [
        ("hls_aes", "02:00:00", "40% · 3.1 MB/s · 0:19 left", "50% · Post-processing"),
        ("video", None, "40% · 3.1 MB/s · 0:19 left", None),
    ],
)
def test_whole_download_card_text_is_unchanged(
    qapp, tmp_path, content_type, waiting, running, postprocess
):
    """구간이 없는 카드의 대기 · 전송 · 후처리 · 완료 문구는 구간 표시가 없던 때와 같아야 한다.

    구간 없는 카드(세그먼트 기반 · 파일). 전송 40% → (세그먼트 기반만) 병합 5/10 → 완료
    -> 대기의 재생 시간 자리(세그먼트 기반은 재생 시간, 파일은 아이템의 크기 글) · 전송 문구 ·
       후처리 문구 · 완료 문구가 주석의 글 그대로다
    """
    card = _Card(qapp, tmp_path, content_type, selections=())
    try:
        waiting_text = card.widget.fileSizeLabel.text()
        if waiting is None:
            waiting = f"{card.item.total_size}"  # 파일 카드는 아이템의 크기 글을 그대로 적는다
        card.manager.downloadItem()
        card.submission = card.service.submissions[0]
        card.data = card.submission["data"]
        card.data.max_threads = 10
        card.transfer(0.4)
        running_text = card.status
        postprocess_text = None
        if postprocess is not None:
            card.submission["on_merge_start"]()
            card.data.merged_segments = 5
            card.notify_cut()
            postprocess_text = card.status
        card.data.model.finish()
        card.submission["on_finished"]()
        qapp.processEvents()

        assert card.submission["content"].selections == ()
        assert (waiting_text, running_text, postprocess_text) == (waiting, running, postprocess)
        assert card.status == "✓ Completed · 1:12"
    finally:
        card.close()


def test_single_section_card_shows_no_done_count(qapp, tmp_path):
    """구간이 하나인 카드의 상태 문구에는 완료 구간 수가 붙지 않아야 한다.

    구간 하나. 전송 40% → 컷 단계에서 그 구간이 끝남 → 완료
    -> 전송 · 컷 · 완료 문구 어디에도 "수/수" 모양이 없다, 컷 문구 == "100% · Cutting",
       완료 문구 == "✓ Completed · 1:12"
    """
    card = _Card(qapp, tmp_path, "hls_aes", selections=SELECTIONS[:1])
    try:
        card.start()
        card.transfer(0.4)
        texts = [card.status]
        card.begin_cut()
        card.cut(1)
        texts.append(card.status)
        card.data.model.finish()
        card.submission["on_finished"]()
        qapp.processEvents()
        texts.append(card.status)

        assert not any(re.search(r"\d+/\d+", text) for text in texts)
        assert texts[1:] == ["100% · Cutting", "✓ Completed · 1:12"]
    finally:
        card.close()


# ================================================================ 엔진에 넘기기


def test_viewmodel_hands_the_sections_and_their_file_names_to_the_engine(card):
    """뷰모델은 아이템의 구간과, 구간마다 배정한 파일 경로를 제출하는 Content에 실어야 한다.

    구간 셋인 아이템(제목 "구간 시험", 1080p)을 시작
    -> 제출된 content의 selections == 구간 셋, selection_paths의 파일 이름 == "구간 시험 1080p_1.mp4" ~ "_3.mp4"
    """
    card.start()

    content = card.submission["content"]
    assert content.selections == SELECTIONS
    assert [os.path.basename(path) for path in content.selection_paths] == [
        f"구간 시험 1080p_{number}.mp4" for number in (1, 2, 3)
    ]


# ================================================================ 실패한 구간만 다시 처리


def _fail_second_section(card) -> tuple[str, ...]:
    """엔진처럼 구간 1 · 3의 파일을 만들고, 둘째 컷만 실패한 채 끝났다고 알린다. 구간 파일 경로를 돌려준다."""
    card.start()
    card.begin_cut()
    card.cut(1)
    card.cut(1, failed=1)
    card.cut(2, failed=1)
    paths = card.submission["content"].selection_paths
    for number in (0, 2):
        with open(paths[number], "wb") as f:
            f.write(b"made")
    card.data.section_resume = SectionResume(
        selections=SELECTIONS, paths=paths, done=frozenset({0, 2})
    )
    error = PostprocessError("후처리(cut) 실패: 구간 1개")
    error.__cause__ = CutError(CUT_FAILED, "시험")
    card.submission["on_failed"](error)
    card.qapp.processEvents()
    release_output_paths(paths)  # 엔진은 실행이 끝날 때 이름의 예약을 푼다
    return paths


def test_retry_keeps_the_file_names_and_hands_over_what_the_failed_run_left(card):
    """일부 구간이 실패한 카드를 재시도해 시작하면 그때의 구간 파일 경로와 남긴 것을 그대로 넘겨야 한다.

    구간 셋, 둘째만 실패(`_1` · `_3` 파일이 디스크에 있다) → ↻ → 다운로드 시작
    -> 새로 제출된 content의 selection_paths == 첫 실행의 경로(" (1)" 없음),
       section_resume의 끝낸 구간 == {0, 2}, 아이템의 완료 구간 수 == 2 · 실패 수 == 0
    """
    paths = _fail_second_section(card)

    card.retry()
    card.restart()

    content = card.submission["content"]
    assert len(card.service.submissions) == 2
    assert content.selection_paths == paths
    assert content.section_resume.done == frozenset({0, 2})
    assert (card.item.sections_done, card.item.sections_failed) == (2, 0)


def test_retry_bar_starts_from_zero_and_the_count_continues_from_the_finished_sections(card):
    """재시도한 카드의 진행 막대는 0부터 다시 차고, 완료 구간 수는 끝낸 구간 수에서 이어져야 한다.

    구간 셋, 둘째만 실패 → ↻ → 시작. 엔진처럼 구간 상태를 (전체 3, 완료 2, 이어받음 2)로 적고
    전송 0% · 50% → 컷 단계 → 둘째 구간 끝남 → 완료 통지. 전송에 둘째 구간(11초)의 컷 예상
    시간의 4.02배가 걸림
    -> 진행률 == [0, 40, 80, 100], 전송 중 문구에 구간 수 없음, 컷 단계 == "80% · Cutting · 2/3",
       둘째가 끝난 뒤 == "100% · Cutting · 3/3", 완료 == "✓ Completed · 3/3 · 1:12", 남긴 것이 지워진다
    """
    _fail_second_section(card)
    card.retry()
    card.transfer_seconds = 4.02 * cut_estimate(11)  # 이번에 자를 것은 둘째 구간뿐이다
    card.restart()
    card.data.sections_done = card.data.sections_resumed = 2
    seen = []

    card.transfer(0.0)
    seen.append((card.item.download_progress, card.status))
    card.transfer(0.5)
    seen.append((card.item.download_progress, card.status))
    card.begin_cut()
    card.notify_cut()
    seen.append((card.item.download_progress, card.status))
    card.cut(3)
    seen.append((card.item.download_progress, card.status))
    card.data.model.finish()
    card.submission["on_finished"]()
    card.qapp.processEvents()

    assert [progress for progress, _text in seen] == [0, 40, 80, 100]
    assert not re.search(r"\d+/\d+", seen[0][1] + seen[1][1])
    assert [text for _progress, text in seen[2:]] == ["80% · Cutting · 2/3", "100% · Cutting · 3/3"]
    assert card.status == "✓ Completed · 3/3 · 1:12"
    assert card.item.section_retry is None


def test_finished_files_are_checked_at_the_retry_press_and_again_at_the_start(card):
    """끝낸 구간의 파일은 재시도를 누를 때와 다운로드를 시작할 때 모두 확인해, 없는 구간을 다시 만들 것으로 돌려야 한다.

    구간 셋, 둘째만 실패. `_3` 파일을 지움 → ↻ → `_1` 파일을 지움 → 다운로드 시작
    -> ↻ 직후 카드의 기록의 끝낸 구간 == {0}, 넘겨진 section_resume의 끝낸 구간 == 빈 것
    """
    paths = _fail_second_section(card)
    os.remove(paths[2])

    card.retry()
    after_press = card.item.section_retry[1].done
    os.remove(paths[0])
    card.restart()

    assert after_press == frozenset({0})
    assert card.submission["content"].section_resume.done == frozenset()


def test_global_download_does_not_start_a_card_left_failed(card):
    """재시도를 누르지 않은 실패 카드는 전역 다운로드가 다시 돌리지 않아야 한다.

    구간 셋, 둘째만 실패. ↻ 없이 다운로드를 다시 요청
    -> 제출은 첫 실행의 1건뿐, 카드는 FAILED 그대로
    """
    _fail_second_section(card)

    card.manager.downloadItem()
    card.qapp.processEvents()

    assert len(card.service.submissions) == 1
    assert card.item.downloadState is DownloadState.FAILED


def test_retry_after_the_stream_changed_starts_over_with_new_file_names(card):
    """실패한 뒤 받을 스트림이 바뀌었으면 이어받지 않고 새 이름으로 처음부터 받아야 한다.

    구간 셋, 둘째만 실패(`_1` · `_3` 파일이 있다). 아이템의 base_url을 바꿈 → ↻ → 시작
    -> 제출된 content의 section_resume is None, 파일 이름 == "_1 (1)" · "_2" · "_3 (1)",
       아이템의 완료 구간 수 == 0
    """
    _fail_second_section(card)
    card.item.base_url = "http://example.invalid/720"

    card.retry()
    card.restart()

    content = card.submission["content"]
    assert content.section_resume is None
    assert [os.path.basename(path) for path in content.selection_paths] == [
        "구간 시험 1080p_1 (1).mp4",
        "구간 시험 1080p_2.mp4",
        "구간 시험 1080p_3 (1).mp4",
    ]
    assert card.item.sections_done == 0


@pytest.mark.parametrize(
    ("cause", "headline"),
    [
        (CutError(CUT_FAILED, "시험"), "Could not cut the section · press retry"),
        (RemuxError("exit 183"), "Segments are corrupted · download the video again"),
    ],
    ids=["cut", "remux"],
)
def test_cut_failure_card_shows_its_own_reason(card, cause, headline):
    """컷이 실패한 카드는 컷 실패의 사유를, 그 밖의 후처리 실패는 지금의 사유를 보여야 한다.

    구간 셋, 둘째 실패. 실패 통지의 원인이 CutError / RemuxError
    -> 상태 문구 == "✕ 1 failed · 2/3 · " + 그 원인의 사유 첫 줄
    """
    card.start()
    card.begin_cut()
    card.cut(2, failed=1)
    error = PostprocessError("후처리 실패")
    error.__cause__ = cause

    card.submission["on_failed"](error)
    card.qapp.processEvents()

    assert card.status == f"✕ 1 failed · 2/3 · {headline}"


@pytest.mark.parametrize("changed", ["folder", "title"])
def test_retry_after_the_folder_or_title_changed_starts_over_in_the_place_now_chosen(
    card, tmp_path, changed
):
    """실패한 뒤 저장 폴더나 제목을 바꿨으면 이어받지 않고, 지금 고른 폴더 · 제목으로 처음부터 받아야 한다.

    구간 셋, 둘째만 실패(옛 폴더에 `_1` · `_3` 파일이 있다).
    folder: 저장 폴더를 새 폴더로 바꿈 / title: 제목을 "새 제목"으로 바꿈 → ↻ → 시작
    -> 제출된 content의 section_resume is None, 구간 파일 셋의 폴더 == 아이템의 지금 저장 폴더,
       파일 이름 == 지금 제목의 `_1` · `_2` · `_3`(" (1)" 없음), 아이템의 완료 구간 수 == 0
    """
    _fail_second_section(card)
    if changed == "folder":
        moved = tmp_path / "moved"
        moved.mkdir()
        card.item.download_path = str(moved)
    else:
        card.item.title = "새 제목"

    card.retry()
    card.restart()

    content = card.submission["content"]
    title = "새 제목" if changed == "title" else "구간 시험"
    assert content.section_resume is None
    assert {os.path.dirname(path) for path in content.selection_paths} == {card.item.download_path}
    assert [os.path.basename(path) for path in content.selection_paths] == [
        f"{title} 1080p_{number}.mp4" for number in (1, 2, 3)
    ]
    assert card.item.sections_done == 0


# ================================================================ 구간 안의 진행 · 전송과 컷의 몫


@pytest.mark.parametrize("card", ["video", "m3u8", "hls_aes"], indirect=True)
def test_bar_rises_inside_a_section_while_it_is_being_cut(card):
    """구간 하나를 자르는 동안 엔진이 컷 진행을 올리면 끝난 구간 수가 그대로여도 막대가 올라야 한다.

    구간 셋, 전송이 막대의 80.08%. 컷 단계에서 끝난 구간 0개인 채로 컷 진행 0.1 · 0.2 · 0.3 · 0.4
    -> 진행률 == [82, 84, 86, 88], 문구의 완료 구간 수는 내내 0/3
    """
    card.start()
    card.begin_cut()
    seen = []

    for progress in (0.1, 0.2, 0.3, 0.4):
        card.cut_inside(progress)
        seen.append(card.item.download_progress)
        assert card.status.endswith("Cutting · 0/3")

    assert seen == [82, 84, 86, 88]


def test_bar_never_goes_back_when_a_lower_cut_progress_arrives(card):
    """앞서 보인 값보다 낮은 컷 진행이 와도 막대는 내려가지 않아야 한다.

    구간 셋. 컷 진행 0.5 → 0.2 → 0.6
    -> 진행률 == [90, 90, 92]
    """
    card.start()
    card.begin_cut()
    seen = []

    for progress in (0.5, 0.2, 0.6):
        card.cut_inside(progress)
        seen.append(card.item.download_progress)

    assert seen == [90, 90, 92]


def test_bar_reaches_100_only_when_the_last_section_is_done(card):
    """막대는 마지막 구간의 일이 끝났을 때만 100이어야 한다 — 컷 진행이 1에 닿아도 그 전에는 99다.

    구간 셋. 구간 둘이 끝난 채 컷 진행 0.999 · 1.0 → 셋째 구간 끝남
    -> 진행률 == [99, 99, 100]
    """
    card.start()
    card.begin_cut()
    card.cut(2)
    seen = []

    for progress in (0.999, 1.0):
        card.cut_inside(progress)
        seen.append(card.item.download_progress)
    card.cut(3)
    seen.append(card.item.download_progress)

    assert seen == [99, 99, 100]


@pytest.mark.parametrize(
    ("times_the_cut_estimate", "at_cut_start"),
    [(0.25, 20), (1.0, 50), (3.0, 75), (9.0, 90)],
)
def test_share_of_the_transfer_follows_how_long_the_transfer_took(
    card, times_the_cut_estimate, at_cut_start
):
    """전송이 막대에서 차지하는 몫은 전송에 걸린 시간 ÷ (그 시간 + 컷 예상 시간)이어야 한다.

    구간 셋(6 · 11 · 29초 — 컷 예상 3 × 1초 + 46 × 0.018초 = 3.828초).
    전송에 그 0.25 · 1 · 3 · 9배가 걸림
    -> 컷 단계에 들어선 순간의 진행률 == 20 · 50 · 75 · 90
    """
    card.transfer_seconds = times_the_cut_estimate * cut_estimate(6, 11, 29) * 1.0001  # 내림 여유
    card.start()

    card.begin_cut()
    card.notify_cut()

    assert card.item.download_progress == at_cut_start


def test_bar_during_transfer_is_the_time_spent_over_the_expected_total(card):
    """전송 중의 막대는 지난 시간 ÷ (지금 속도로 본 전송 예상 시간 + 컷 예상 시간)이어야 한다.

    구간 셋(컷 예상 3.828초). 7.656초가 지났을 때 전송이 50%(전송 예상 15.312초)
    -> 진행률 == int(100 × 7.656 ÷ (15.312 + 3.828)) == 40
    """
    card.transfer_seconds = 4 * cut_estimate(6, 11, 29) * 1.0001  # 내림 여유
    card.start()

    card.transfer(0.5)

    assert card.item.download_progress == 40


# ================================================================ 받기가 멈출 때 · 재시도 (#390)


@pytest.mark.parametrize("card", ["video", "m3u8", "hls_aes"], indirect=True)
def test_bar_stands_still_while_nothing_is_received_and_then_goes_on_as_if_it_had_not_stopped(card):
    """받는 것 없이 시간이 지나는 동안 막대는 서 있고, 다시 받기 시작하면 멈추지 않았을 때와 같은 값이어야 한다 (#390).

    구간 셋, 전송이 막대의 80.08%. 전송 30% → 진행이 그대로인 통지 30번이 30초에 걸쳐 옴 → 전송 70%
    -> 진행률: 30%에서 24, 정체 동안 내내 24, 70%에서 56 (멈추지 않았을 때의 값과 같다)
    """
    card.start()
    card.transfer(0.3)
    before = card.item.download_progress
    during = []

    for _ in range(3):
        card.wait(10, notifications=10)
        during.append(card.item.download_progress)
    card.transfer(0.7)

    assert before == 24
    assert during == [24, 24, 24]
    assert card.item.download_progress == 56


@pytest.mark.parametrize("card", ["m3u8", "hls_aes"], indirect=True)
def test_notifications_that_only_received_bytes_count_as_receiving_time(card):
    """세그먼트가 끝나지 않고 바이트만 늘어난 통지 사이도 받던 시간이어야 한다 (#390).

    구간 셋, 전송이 막대의 80.08%. 전송 10%마다 그 사이 한가운데에 받은 바이트만 늘어난 통지
    (완료 세그먼트 수는 그대로)가 옴 → 전송 30% · 70%
    -> 진행률: 30%에서 24, 70%에서 56 (세그먼트가 통지마다 끝날 때와 같다)
    """
    card.start()
    seen = {}

    for step in range(1, 8):
        fraction = step / 10
        _Clock.now = card._started_at + card.transfer_seconds * (fraction - 0.05)
        card.submission["on_progress"](
            ProgressEvent(
                downloaded_size=round(100 * MB * (fraction - 0.05)),
                total_size=None,
                speed=3.1,
                active_threads=4,
            )
        )
        card.qapp.processEvents()
        card.transfer(fraction)
        seen[step] = card.item.download_progress

    assert (seen[3], seen[7]) == (24, 56)


def test_bar_does_not_jump_after_a_pause(card, paused_clock):
    """60초 일시정지한 뒤 재개해도 막대가 뛰지 않고, 멈추지 않았을 때와 같은 값이어야 한다 (#390).

    구간 셋, 전송이 막대의 80.08%. 전송 30% → 60초 일시정지(그동안 통지 없음) → 재개 뒤 첫 통지에서
    전송 40% → 전송 70%
    -> 진행률 == [24, 32, 56] (멈추지 않았을 때와 같다)
    """
    card.start()
    card.transfer(0.3)
    seen = [card.item.download_progress]

    paused_clock.paused += 60
    card.wait(60)
    for fraction in (0.4, 0.7):
        card.transfer(fraction)
        seen.append(card.item.download_progress)

    assert seen == [24, 32, 56]


@pytest.mark.parametrize("card", ["m3u8", "hls_aes"], indirect=True)
def test_retry_that_reuses_stored_segments_fills_the_bar_from_zero_to_100(card):
    """받아 둔 세그먼트를 다시 쓰는 재시도도 막대가 0에서 시작해 100에서 끝나야 한다 (#390).

    세그먼트 10개 가운데 6개는 받지 않고 다시 씀(엔진이 준비 끝에 reusable_threads = 6을 적음).
    다시 쓴 6개가 한꺼번에 끝난 통지 → 받은 것 2개 · 4개 → 컷 단계 → 구간 셋이 차례로 끝남.
    받기가 컷 예상 시간의 4.02배
    -> 진행률 == [0, 40, 80, 80, 86, 93, 100] (컷 단계에 들어선 통지는 받기가 끝난 80 그대로)
    """
    card.start(segments=10)
    card.data.reusable_threads = 6
    seen = []

    card.data.completed_threads = card.data.reused_threads = 6
    card.wait(0.0, notifications=1)
    seen.append(card.item.download_progress)
    for received in (2, 4):
        _Clock.now = card._started_at + card.transfer_seconds * received / 4
        card.data.completed_threads = 6 + received
        card.wait(0.0, notifications=1)
        seen.append(card.item.download_progress)
    card.submission["on_merge_start"]()
    card.notify_cut()
    seen.append(card.item.download_progress)
    for done in (1, 2, 3):
        card.cut(done)
        seen.append(card.item.download_progress)

    assert seen == [0, 40, 80, 80, 86, 93, 100]


@pytest.mark.parametrize("card", ["video"], indirect=True)
def test_retry_that_receives_nothing_fills_the_bar_with_the_cut_alone(card):
    """받을 것이 없는 재시도(남긴 원본을 그대로 쓴다)는 막대가 0에서 시작해 컷만으로 100까지 차야 한다 (#390).

    구간 셋, 엔진이 받을 바이트 0을 알림 → 컷 단계 → 컷 진행 0.5 → 구간 셋이 끝남
    -> 진행률 == [0, 0, 50, 100]
    """
    card.start(segments=0)
    seen = []

    card.submission["on_progress"](
        ProgressEvent(downloaded_size=0, total_size=0, speed=0.0, active_threads=0)
    )
    card.qapp.processEvents()
    seen.append(card.item.download_progress)
    card.submission["on_merge_start"]()
    card.notify_cut()
    seen.append(card.item.download_progress)
    card.cut_inside(0.5)
    seen.append(card.item.download_progress)
    card.cut(3)
    seen.append(card.item.download_progress)

    assert seen == [0, 0, 50, 100]


@pytest.mark.parametrize("cut_takes", [3.0, 1 / 3], ids=["three-times", "a-third"])
def test_cut_longer_or_shorter_than_its_estimate_neither_lowers_nor_jumps_the_bar(card, cut_takes):
    """컷이 예상보다 3배 길거나 1/3이어도 막대는 컷의 한 일만큼만 올라 줄거나 뛰지 않아야 한다 (#390).

    구간 셋, 전송이 막대의 80.08%. 컷 단계에서 컷 진행 0.25 · 0.5 · 0.75가 예상 시간의
    cut_takes배에 걸쳐 고르게 옴 → 구간 셋이 끝남
    -> 진행률 == [80, 85, 90, 95, 100] (예상과 같을 때와 같다)
    """
    card.start()
    card.begin_cut()
    card.notify_cut()
    seen = [card.item.download_progress]

    for progress in (0.25, 0.5, 0.75):
        _Clock.now += cut_estimate(6, 11, 29) * cut_takes / 4
        card.cut_inside(progress)
        seen.append(card.item.download_progress)
    card.cut(3)
    seen.append(card.item.download_progress)

    assert seen == [80, 85, 90, 95, 100]
