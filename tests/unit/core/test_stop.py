"""오래 도는 일을 그만두게 하는 길 (#309) — 그만둘지 묻기 · 색인 해석의 그만두기 · 해석 관문.

시간으로 순서를 맞추지 않는다. 스레드의 순서는 ``threading.Event``로 정한다.
"""

import threading

import pytest

import core.api.mp4 as mp4_module
import core.models.mp4_index as index_module
import core.models.sample_column as column_module
import core.utils.mp4_ranges as ranges_module
from core.api.mp4 import index_mp4, read_mp4_raw
from core.models.mp4_index import PendingMp4Head
from core.models.plan import TimeRange
from core.utils.mp4_partial import plan_partial
from core.utils.mp4_ranges import sections_download_size, sections_head_size, sections_sizes
from core.utils.stop import StopRequested, check_stop, stop_requested, stoppable
from tests.unit.core.mp4_builder import audio_spec, build_mp4, video_spec


def _raw():
    """실제 mp4 바이트(10fps 12프레임)에서 받은 moov."""
    data = build_mp4([video_spec(), audio_spec()]).data
    return read_mp4_raw(lambda offset, size: data[offset : offset + size])


@pytest.fixture
def tiny_chunks(monkeypatch):
    """조각을 2개씩으로 줄인다 — 12프레임짜리 moov도 여러 조각으로 돈다."""
    monkeypatch.setattr(column_module, "CHUNK_ITEMS", 2)
    monkeypatch.setattr(mp4_module, "CHUNK_ITEMS", 2)


class _After:
    """n번째 물음부터 그만두라고 답한다. 물은 횟수를 센다."""

    def __init__(self, n: int):
        self.n = n
        self.asked = 0

    def __call__(self) -> bool:
        self.asked += 1
        return self.asked >= self.n


# ================================================================ 그만둘지 묻기


def test_check_stop_does_nothing_when_nothing_is_set_on_the_thread():
    """그만둘지 묻는 값을 걸지 않은 스레드에서는 check_stop이 아무것도 하지 않아야 한다.

    걸어 둔 값 없음 -> stop_requested() is False, check_stop()이 예외 없이 돌아온다
    """
    assert stop_requested() is False
    check_stop()


def test_check_stop_raises_when_the_value_set_on_the_thread_says_stop():
    """걸어 둔 값이 참을 돌려주면 check_stop이 StopRequested를 던져야 한다.

    stoppable(늘 참) 안 -> StopRequested, 블록을 나온 뒤에는 던지지 않는다
    """
    with stoppable(lambda: True):
        with pytest.raises(StopRequested):
            check_stop()

    check_stop()


def test_inner_stoppable_wins_and_the_outer_one_comes_back():
    """겹쳐 걸면 안쪽 값이 이기고, 안쪽을 나오면 바깥 값으로 돌아와야 한다.

    바깥 = 늘 참, 안쪽 = 늘 거짓 -> 안쪽에서는 거짓, 안쪽을 나오면 참
    """
    with stoppable(lambda: True):
        with stoppable(lambda: False):
            inner = stop_requested()
        outer = stop_requested()

    assert (inner, outer) == (False, True)


def test_stoppable_given_none_keeps_the_outer_value():
    """None을 걸면 바깥에 걸어 둔 값을 그대로 따라야 한다.

    바깥 = 늘 참, 안쪽 = None -> 안쪽에서도 참
    """
    with stoppable(lambda: True):
        with stoppable(None):
            assert stop_requested() is True


def test_value_set_on_one_thread_does_not_reach_another():
    """한 스레드에 건 값은 다른 스레드의 일을 그만두게 하지 않아야 한다.

    메인에 늘 참을 걸고 다른 스레드에서 stop_requested()를 읽음 -> False
    """
    seen = []
    with stoppable(lambda: True):
        worker = threading.Thread(target=lambda: seen.append(stop_requested()))
        worker.start()
        worker.join()

    assert seen == [False]


# ================================================================ 색인 해석의 그만두기


def test_index_mp4_does_not_start_when_told_to_stop_from_the_beginning():
    """처음부터 그만두라는 답이면 index_mp4는 해석을 시작하지 않고 StopRequested를 던져야 한다.

    should_stop = 늘 참 -> StopRequested, 물은 횟수 == 1
    """
    asked = _After(1)

    with pytest.raises(StopRequested):
        index_mp4(_raw(), asked)

    assert asked.asked == 1


def test_index_mp4_stops_between_chunks(tiny_chunks):
    """해석하는 도중에 그만두라는 답이 오면 index_mp4는 끝까지 가지 않고 멈춰야 한다.

    조각 2개씩. 끝까지 돌 때 묻는 횟수 N을 먼저 세고, 그 절반째 물음부터 그만두라고 답함
    -> StopRequested, 물은 횟수 == N // 2 (그 뒤로는 묻지도 않는다)
    """
    whole = _After(10**9)
    index_mp4(_raw(), whole)
    assert whole.asked > 10, "전제: 조각 사이에서 여러 번 묻는다"
    half = _After(whole.asked // 2)

    with pytest.raises(StopRequested):
        index_mp4(_raw(), half)

    assert half.asked == whole.asked // 2


def test_index_mp4_gives_the_same_index_whether_or_not_it_is_asked(tiny_chunks):
    """그만둘지 묻는 값을 줘도, 그만두라는 답이 없으면 색인은 주지 않았을 때와 같아야 한다.

    should_stop = 늘 거짓 -> 색인 == should_stop 없이 만든 색인
    """
    assert index_mp4(_raw(), lambda: False).index == index_mp4(_raw()).index


def test_fetching_the_moov_stops_between_chunks(monkeypatch):
    """moov를 받는 도중에 그만두라는 답이 오면 남은 본문을 받지 않고 응답을 닫아야 한다.

    조각 셋을 주는 응답, 둘째 조각 앞에서부터 그만두라고 답함
    -> StopRequested, 내준 조각 2개(셋째는 청하지 않았다), 응답의 닫기 1번
    """

    class _Response:
        status_code = 206
        headers = {"Content-Range": "bytes 0-29/1000"}

        def __init__(self):
            self.given = 0
            self.closed = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed += 1

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            for _ in range(3):
                self.given += 1
                yield b"x" * 10

    response = _Response()

    class _Session:
        def get(self, url, **kwargs):
            return response

    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: _Session())
    read = mp4_module._range_reader("https://example.invalid/v.mp4")

    with stoppable(_After(2)):
        with pytest.raises(StopRequested):
            read(0, 30)

    assert (response.given, response.closed) == (2, 1)


# ================================================================ 받아 둔 moov의 해석


def test_stopped_get_keeps_the_bytes_and_the_next_get_builds_the_index():
    """그만둔 청은 기억되지 않아야 한다 — 바이트가 남아 있고, 다음 청이 색인을 만든다.

    첫 청 = 늘 그만두라고 답함, 둘째 청 = 묻는 값 없음
    -> 첫 청 StopRequested, 그 뒤 raw is not None, 둘째 청이 색인을 돌려준다
    """
    raw = _raw()
    pending = PendingMp4Head(raw, lambda held: index_mp4(held), permanent=(mp4_module.Mp4Error,))

    with pytest.raises(StopRequested):
        pending.get(lambda: True)
    kept = pending.raw
    head = pending.get()

    assert kept is raw
    assert head.index == index_mp4(raw).index


class _Blocking:
    """해석 대역 — 들어온 것을 알리고, 문을 열 때까지 기다린다. 드나든 순서를 적는다."""

    def __init__(self, name: str, order: list):
        self.name = name
        self.order = order
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, raw):
        self.order.append(f"{self.name} 들어옴")
        self.entered.set()
        assert self.release.wait(10), "해석 대역의 문이 열리지 않았다"
        self.order.append(f"{self.name} 나감")
        return self.name


def test_only_one_index_is_built_at_a_time_across_different_moovs():
    """서로 다른 moov의 해석도 한 번에 하나만 돌아야 한다 — 둘째는 첫째가 끝난 뒤에 시작한다.

    A의 해석이 도는 중에 B를 청함. B가 관문에서 기다리는 것을 확인한 뒤(B의 물음이 불린다) A를 놓음
    -> 순서 == [A 들어옴, A 나감, B 들어옴, B 나감]
    """
    order: list[str] = []
    first, second = _Blocking("A", order), _Blocking("B", order)
    pending_a, pending_b = PendingMp4Head(object(), first), PendingMp4Head(object(), second)
    waiting = threading.Event()

    def asked() -> bool:
        waiting.set()  # 잠금을 기다리는 동안에만 불린다(해석 대역은 묻지 않는다)
        return False

    thread_a = threading.Thread(target=pending_a.get)
    thread_b = threading.Thread(target=lambda: pending_b.get(asked))
    thread_a.start()
    assert first.entered.wait(5)
    thread_b.start()
    assert waiting.wait(5), "전제: B가 관문에서 기다린다"
    assert not second.entered.is_set()
    second.release.set()
    first.release.set()
    thread_a.join(5)
    thread_b.join(5)

    assert order == ["A 들어옴", "A 나감", "B 들어옴", "B 나감"]


def test_a_request_waiting_for_another_index_gives_up_when_told_to_stop():
    """다른 해석이 끝나기를 기다리던 청은 그만두라는 답이 오면 해석을 시작하지 않고 그만둬야 한다.

    A의 해석이 도는 중에 B를 청하고, B가 기다리는 동안 B에 그만두라고 답함
    -> B는 StopRequested, B의 해석 대역은 한 번도 불리지 않았다, 그동안 A는 아직 안에 있다
    """
    order: list[str] = []
    first, second = _Blocking("A", order), _Blocking("B", order)
    pending_a, pending_b = PendingMp4Head(object(), first), PendingMp4Head(object(), second)
    stop = threading.Event()
    result: list = []

    def get_b() -> None:
        try:
            result.append(pending_b.get(stop.is_set))
        except StopRequested:
            result.append("그만둠")

    thread_a = threading.Thread(target=pending_a.get)
    thread_b = threading.Thread(target=get_b)
    thread_a.start()
    assert first.entered.wait(5)
    thread_b.start()
    stop.set()
    thread_b.join(5)
    still_inside = not first.release.is_set() and thread_a.is_alive()
    first.release.set()
    thread_a.join(5)

    assert result == ["그만둠"]
    assert still_inside
    assert order == ["A 들어옴", "A 나감"]


def test_the_gate_is_free_again_after_a_stopped_build():
    """그만둔 해석도 관문을 놓아야 한다 — 다음 해석이 기다리지 않고 돈다.

    첫 moov의 해석을 그만두게 한 뒤 다른 moov를 청함 -> 둘째 청이 색인을 돌려준다, 관문은 비어 있다
    """
    raw = _raw()
    stopped = PendingMp4Head(raw, lambda held: index_mp4(held))
    with pytest.raises(StopRequested):
        stopped.get(_After(2))

    head = PendingMp4Head(raw, lambda held: index_mp4(held)).get()

    assert head.index == index_mp4(raw).index
    assert index_module._PARSE_GATE.acquire(blocking=False)
    index_module._PARSE_GATE.release()


# ================================================================ 크기 세기 (plan_partial · sections_sizes)


def test_plan_partial_scans_the_sample_offsets_in_chunks(tiny_chunks):
    """plan_partial은 샘플 위치 열을 조각마다 훑어야 한다 — 값은 한 번에 훑은 것과 같고, 조각 사이에서 그만둘 수 있다.

    조각 2개씩 -> head_size == 조각을 키웠을 때의 값
    그만두라는 답을 걸고 부름 -> StopRequested (한 번에 훑으면 묻는 틈이 없다)
    """
    index = index_mp4(_raw()).index

    chunked = plan_partial(index, ()).head_size
    with stoppable(lambda: True):
        with pytest.raises(StopRequested):
            plan_partial(index, ())

    assert chunked == min(min(index.video.offsets), min(index.audio.offsets))


def test_sections_sizes_equals_the_two_separate_sizes_with_one_layout(monkeypatch):
    """sections_sizes는 받을 양 · 머리 길이를 따로 구한 것과 같은 값을, 배치 한 번으로 돌려줘야 한다.

    구간 0.2~0.5초 · 0.4~1.0초(겹친다)
    -> (sections_download_size, sections_head_size)와 같다, plan_partial은 1번 불린다
    """
    index = index_mp4(_raw()).index
    selections = [TimeRange(0.2, 0.5), TimeRange(0.4, 1.0)]
    expected = (sections_download_size(index, selections), sections_head_size(index))
    calls = []
    real = ranges_module.plan_partial

    def counting(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(ranges_module, "plan_partial", counting)

    assert sections_sizes(index, selections) == expected
    assert len(calls) == 1
