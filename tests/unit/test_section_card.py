"""구간 다운로드의 카드 표시 — 합친 진행률 · 완료 구간 수 · 컷 단계 문구 · 일부 실패 (#309).

mainWindow와 같은 배선(ContentViewModel ↔ ContentListView ↔ DownloadViewModel)을 최소
하네스로 세우고, 엔진 자리는 서비스 대역이 맡는다. 테스트가 엔진처럼 공유 데이터
(DownloadData)에 구간 상태를 적고 서비스에 등록된 콜백을 불러, 뷰모델 → content → 카드
위젯까지 실제 시그널로 지나가게 한다. 실제 다운로드 · 네트워크는 없다.

핵심 계약:
- 진행 막대는 전송과 컷을 합친 하나다 — 전송이 앞 80%, 컷이 뒤 20%를 구간 수로 나눈다.
  전달 방식(파일 · 세그먼트)이 달라도 같은 진행에는 같은 값이 나온다
- 완료 구간 수는 엔진이 적어 둔 구간 상태에서 읽는다 — 같은 통지가 두 번 와도 늘지 않는다
- 컷 단계의 카드에는 속도 · 남은 시간 대신 단계 문구가 나온다
- 일부 구간만 자르지 못하면 나머지가 끝난 뒤에 카드가 실패가 되고, 실패한 구간 수와 완료
  구간 수가 사유 앞에 나온다. 엔진은 WAITING으로 끝난다
- 완료 구간 수는 구간이 둘 이상일 때만 나온다. 전체 다운로드 카드의 문구는 그대로다
"""

import os
import re

import pytest
from PySide6.QtCore import QObject

import main as main_module
import app.theme as theme
from app.download_logger import DownloadLogger
from app.viewmodels.content_viewmodel import ContentViewModel
from app.viewmodels.data import ContentItem
from app.viewmodels.download_viewmodel import DownloadViewModel
from app.widgets.view import ContentListView
from core.downloaders.base import PostprocessError
from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from core.models.plan import TimeRange
from core.utils.hybrid_cut import CUT_FAILED, CutError
from core.utils.paths import release_output_paths
from tests.unit.card_helpers import hold_style

MB = 1024 * 1024
# 길이가 서로 다른 구간 셋 — 6초 · 11초 · 29초
SELECTIONS = (TimeRange(605.0, 611.0), TimeRange(2520.0, 2531.0), TimeRange(3600.0, 3629.0))


@pytest.fixture(autouse=True)
def _apply_dark_card_qss(qapp):
    """카드를 제품의 스타일로 그린다 — 테스트 하나의 수명 동안만 건다(tests/unit/test_failure_display.py와 같은 규칙)."""
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
        return 72.0

    def wait(self, timeout=None) -> bool:
        return True


class FakeService:
    """DownloadService 대역 — submit 인자(공유 데이터 · 콜백)를 붙잡아 테스트가 엔진처럼 부르게 한다."""

    def __init__(self):
        self.submissions: list[dict] = []

    def submit(self, content, **kwargs):
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
        self.manager.start(item)
        self.viewmodel.start(item)


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
        self.manager.downloadItem()
        self.submission = self.service.submissions[0]
        self.data = self.submission["data"]
        self.data.sections_total = len(self.item.selections)
        self.data.max_threads = segments
        self.qapp.processEvents()

    def transfer(self, fraction: float, speed: float = 3.1) -> None:
        """전송이 fraction만큼 진행됐다고 엔진처럼 알린다 — 파일은 바이트, 세그먼트 기반은 세그먼트 수."""
        total = 100 * MB
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

    def begin_cut(self) -> None:
        """전송이 끝나고 컷 단계에 들어갔다고 알린다."""
        self.transfer(1.0)
        self.submission["on_merge_start"]()

    def cut(self, done: int, failed: int = 0) -> None:
        """구간 하나의 컷이 끝났다고 알린다 — 엔진이 하듯 구간 상태를 적고 속도 0의 진행을 보낸다."""
        self.data.sections_done, self.data.sections_failed = done, failed
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

    길이가 다른 구간 셋. 전송 0 · 30 · 70 · 100% → 컷 단계 → 구간 1 · 2 · 3이 차례로 끝남
    -> 카드의 진행률 == [0, 24, 56, 80, 86, 93, 100] (전송이 80%, 컷이 구간마다 20% ÷ 3),
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


def test_done_count_is_shown_while_transferring_and_when_finished(card):
    """완료 구간 수는 전송 중 · 컷 중 · 완료 카드의 상태 문구 끝에 붙어야 한다.

    구간 셋. 전송 40% → 컷 단계에서 구간 하나 끝남 → 셋 다 끝나고 완료 통지
    -> 전송 중 "… · 0/3", 컷 중 "… · 1/3", 완료 "✓ Completed · 3/3 · 1:12"
    """
    card.start()
    card.transfer(0.4)
    transferring = card.status
    card.begin_cut()
    card.cut(1)
    cutting = card.status
    card.cut(3)
    card.data.model.finish()
    card.submission["on_finished"]()
    card.qapp.processEvents()

    assert transferring.endswith(" · 0/3")
    assert cutting.endswith(" · 1/3")
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


@pytest.mark.parametrize(
    ("style", "expected"),
    [("count", "Sections 3 · 0:46"), ("range", "10:05–10:11 and 2 more")],
)
def test_waiting_card_shows_the_section_summary_in_the_duration_slot(
    card, monkeypatch, style, expected
):
    """구간이 있는 대기 카드는 재생 시간 자리에 구간 요약을 적어야 한다.

    구간 셋(6 · 11 · 29초 = 46초, 첫 구간 10:05 ~ 10:11), 요약 형식은 주석의 두 가지
    -> 재생 시간 자리의 글 == 기대값
    """
    monkeypatch.setattr(type(card.widget), "SECTION_SUMMARY_STYLE", style)

    card.widget.setData(card.item, 0)

    assert card.item.downloadState is DownloadState.WAITING
    assert card.widget.fileSizeLabel.text() == expected


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
