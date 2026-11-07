"""서비스 핸들의 중계 (#484) — 진행 · 완료에는 엔진 카운터의 값 묶음을, 실패에는 안내 키를 싣는다."""

from core.models.download_data import DownloadData
from core.models.events import DownloadCounters, ProgressEvent
from core.services.download_service import DownloadHandle


def _data() -> DownloadData:
    data = DownloadData(
        "https://cdn.example/b", "https://chzzk.naver.com/video/1", "out.mp4", 720, "m3u8"
    )
    data.max_threads = 10
    data.completed_threads = 4
    data.merged_segments = 2
    data.reused_threads = 1
    data.reusable_threads = 3
    data.sections_total = 3
    data.sections_done = 1
    data.sections_failed = 1
    data.sections_resumed = 0
    data.cut_progress = 0.5
    data.cut_preparing = True
    data.sections_output_size = 7000
    data.output_size = 9000
    data.total_size = 12345
    return data


# 위 _data()의 값을 그대로 적은 기대값 — 일시정지한 적 없음 · 병합 전
EXPECTED = DownloadCounters(
    total_size=12345,
    max_threads=10,
    completed_threads=4,
    merged_segments=2,
    reused_threads=1,
    reusable_threads=3,
    sections_total=3,
    sections_done=1,
    sections_failed=1,
    sections_resumed=0,
    cut_progress=0.5,
    cut_preparing=True,
    sections_output_size=7000,
    output_size=9000,
    paused_seconds=0.0,
    merging=False,
)


def test_progress_carries_the_counters_of_the_moment_it_was_relayed() -> None:
    """진행 통지는 중계하던 순간의 카운터를 값 묶음으로 실어야 하고, 뒤에 바뀐 카운터는 그 묶음에 없어야 한다.

    공유 데이터(위 값)에서 진행 ProgressEvent(10)을 중계 → 공유 데이터 sections_done 3으로 바꿈
    -> 받은 통지의 값 묶음 == EXPECTED(sections_done 1)
    """
    data = _data()
    seen: list[ProgressEvent] = []
    handle = DownloadHandle(data.content, data, None, on_progress=seen.append)

    handle._relay_progress(ProgressEvent(downloaded_size=10))
    data.sections_done = 3

    [event] = seen
    assert event.downloaded_size == 10
    assert event.counters == EXPECTED


def test_progress_after_merge_start_says_merging() -> None:
    """병합 시작을 중계한 뒤의 진행 통지는 값 묶음에 병합 중이라고 실어야 한다.

    병합 시작 중계 → 진행 ProgressEvent(10) 중계
    -> 값 묶음 merging 참
    """
    data = _data()
    seen: list[ProgressEvent] = []
    handle = DownloadHandle(data.content, data, None, on_progress=seen.append)

    handle._relay_merge_start()
    handle._relay_progress(ProgressEvent(downloaded_size=10))

    assert seen[0].counters.merging is True


def test_failure_carries_the_key_and_the_counters_and_the_exception_callback_keeps_the_exception() -> (
    None
):
    """실패는 키 콜백에 (안내 키, 실패 때의 값 묶음)을, 예외 콜백에 예외 객체를 그대로 넘겨야 한다.

    OSError("디스크")로 실패 중계(예외 콜백 · 키 콜백 둘 다 등록)
    -> 키 콜백 ("Failed to save file", EXPECTED) · 예외 콜백은 같은 예외 객체
    """
    data = _data()
    keys: list[tuple] = []
    errors: list[BaseException] = []
    handle = DownloadHandle(
        data.content,
        data,
        None,
        on_failed=errors.append,
        on_failed_key=lambda key, counters: keys.append((key, counters)),
    )
    error = OSError("디스크")

    handle._relay_failed(error)

    assert keys == [("Failed to save file", EXPECTED)]
    assert errors == [error] and errors[0] is error


def test_finish_carries_the_counters_after_the_finished_callback() -> None:
    """완료는 완료 콜백을 부른 뒤 값 묶음 콜백에 완료하던 때의 값 묶음을 넘겨야 한다.

    완료 중계(완료 콜백 · 값 묶음 콜백 둘 다 등록)
    -> 차례 [완료, (값 묶음, EXPECTED)]
    """
    data = _data()
    calls: list = []
    handle = DownloadHandle(
        data.content,
        data,
        None,
        on_finished=lambda: calls.append("완료"),
        on_finished_counters=lambda counters: calls.append(("값 묶음", counters)),
    )

    handle._relay_finished()

    assert calls == ["완료", ("값 묶음", EXPECTED)]


def test_counters_are_left_out_of_event_equality() -> None:
    """진행 이벤트의 같음(==)은 값 묶음을 보지 않아야 한다 — 엔진이 낸 값이 같으면 같은 통지다.

    ProgressEvent(10, counters=EXPECTED)와 ProgressEvent(10)
    -> 같음
    """
    assert ProgressEvent(downloaded_size=10, counters=EXPECTED) == ProgressEvent(downloaded_size=10)
