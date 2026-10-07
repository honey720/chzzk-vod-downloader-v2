"""moov 해석기가 긴 표를 잘라 도는 것 — 값은 한 번에 돈 것과 같고, 다른 스레드가 멈추지 않는다 (#309).

C 안에서 도는 반복은 끝날 때까지 GIL을 놓지 않는다. 샘플이 수백만 개인 표를 통째로 돌면 그동안
GUI 스레드가 멈춘다. 해석기는 표를 ``CHUNK_ITEMS``개씩 잘라 돈다
(``core/models/sample_column.py``).

세 가지를 잰다.

- 잘라 돈 결과가 한 번에 돈 결과와 같다 — 조각 크기를 아주 작게 바꿔 조각의 경계가 작은
  트랙에서도 수없이 생기게 한다
- 실제로 잘라 돈다 — 한 번에 넘기는 양이 조각 크기를 넘지 않는다
- 해석이 도는 동안 다른 스레드가 돈다 — 긴 합성 moov로 틱의 간격을 잰다
"""

import array
import dataclasses
import random
import struct
import threading
import time
from itertools import accumulate

import pytest

import core.api.mp4 as mp4_module
import core.models.sample_column as column_module
from core.api.mp4 import Mp4Error, parse_moov, summarize_moov
from core.models.sample_column import (
    SampleColumn,
    array_in_chunks,
    count_column,
    fill_in_chunks,
    float_column,
    in_chunks,
)
from tests.unit.core.mp4_builder import TrackSpec, _trak, box, build_mp4, full_box
from tests.unit.core.test_mp4_summary import CASES, _random_tracks

ONE_PASS = 1 << 40  # 어떤 표보다도 큰 조각 — 한 번에 도는 것과 같다


@pytest.fixture
def chunk_items(monkeypatch):
    """조각 크기를 바꾸는 함수 — 해석기와 배열 모듈 양쪽에 건다."""

    def use(items: int) -> None:
        monkeypatch.setattr(column_module, "CHUNK_ITEMS", items)
        monkeypatch.setattr(mp4_module, "CHUNK_ITEMS", items)

    return use


def _columns(index) -> dict:
    """색인의 모든 값 — 배열은 종류와 바이트로 적는다(값이 한 비트만 달라도 다르다)."""
    found = {}

    def walk(prefix: str, value) -> None:
        if isinstance(value, array.array):
            found[prefix] = (value.typecode, value.tobytes())
        elif dataclasses.is_dataclass(value):
            for field in dataclasses.fields(value):
                walk(f"{prefix}.{field.name}", getattr(value, field.name))
        else:
            found[prefix] = value if not isinstance(value, float) else value.hex()

    walk("index", index)
    return found


def _parsed(moov: bytes):
    """해석 결과의 모든 값. 해석이 거부하면 그 실패 키."""
    try:
        return _columns(parse_moov(moov))
    except Mp4Error as error:
        return error.message_key


# ================================================================ 값이 같다


@pytest.mark.parametrize("items", [1, 2, 3, 7, 64])
@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_index_parsed_in_chunks_equals_the_index_parsed_in_one_pass(chunk_items, case, items):
    """표를 잘라 해석한 색인은 한 번에 해석한 색인과 모든 값이 바이트까지 같아야 한다.

    손으로 만든 트랙들(고정 · 가변 프레임률 · B 프레임 · 편집 목록 · 오디오 먼저 시작 등)을
    조각 1 · 2 · 3 · 7 · 64개로 해석
    -> 색인의 모든 배열(종류 · 바이트)과 값 == 조각 없이 해석한 것. 가벼운 해석의 값도 같다
    """
    video, audio = CASES[case]()
    moov = build_mp4([video] + ([audio] if audio else [])).moov
    chunk_items(ONE_PASS)
    whole, summary = _parsed(moov), summarize_moov(moov)

    chunk_items(items)

    assert _parsed(moov) == whole
    assert summarize_moov(moov) == summary


@pytest.mark.parametrize("seed", range(120))
def test_random_tracks_parsed_in_chunks_equal_one_pass(chunk_items, seed):
    """씨앗으로 흔든 트랙도 잘라 해석한 결과가 한 번에 해석한 결과와 같아야 한다 — 거부하는 쪽도 같다.

    씨앗마다의 무작위 트랙(음수 · 큰 ctts, 빈 편집, 샘플마다 다른 길이 포함)을 조각 5개로 해석
    -> 색인의 모든 값 == 조각 없이 해석한 것. 한쪽이 거부하면 다른 쪽도 같은 키로 거부한다
    """
    video, audio = _random_tracks(seed)
    moov = build_mp4([video] + ([audio] if audio else [])).moov
    chunk_items(ONE_PASS)
    whole = _parsed(moov)

    chunk_items(5)

    assert _parsed(moov) == whole


@pytest.mark.parametrize("seed", range(40))
def test_presentation_order_sorted_in_chunks_equals_a_stable_sort(chunk_items, seed):
    """표시 순서를 조각마다 정렬해 이은 것은 전체를 안정 정렬한 것과 같아야 한다.

    무작위 DTS(줄지 않는다 — 길이 0 포함) · PTS − DTS(음수 · 큰 값 · 같은 표시 시각이 많게)로
    만든 500샘플을 조각 16개로 정렬. 씨앗의 넷에 하나는 표시 순서가 거의 뒤집힌 트랙이다
    -> _presentation_order == sorted(range(n), key=presented.__getitem__)
    """
    generator = random.Random(seed)
    count = 500
    deltas = [generator.choice([0, 0, 1, 2, 5]) for _ in range(count)]
    decoded = array.array("q", accumulate(deltas, initial=0))
    decoded.pop()
    if seed % 4 == 0:
        leads = [2 * (decoded[-1] - value) for value in decoded]  # 뒤의 샘플이 먼저 표시된다
    else:
        leads = [generator.choice([-3, 0, 0, 1, 4, 40]) for _ in range(count)]
    presented = array.array("q", (base + lead for base, lead in zip(decoded, leads)))
    chunk_items(16)

    order = mp4_module._presentation_order(presented, decoded, min(leads))

    assert list(order) == sorted(range(count), key=presented.__getitem__)


@pytest.mark.parametrize("count", [0, 1, 7, 8, 9, 16, 23])
def test_filling_in_chunks_keeps_every_value_and_carries_running_totals_across_chunks(
    chunk_items, count
):
    """잘라 채운 배열은 한 번에 채운 배열과 같아야 한다 — 마지막 조각 · 경계를 넘는 누적값까지.

    조각 8개. 0 · 1 · 7 · 8 · 9 · 16 · 23개의 값(조각보다 적음 · 딱 맞음 · 하나 넘음 · 두 배 · 나머지 있음)을
    누적(accumulate)해 채움
    -> array_in_chunks · float_column · count_column == 한 번에 만든 배열, in_chunks를 이은 것 == 원래 배열
    """
    chunk_items(8)
    values = [3 * n + 1 for n in range(count)]
    totals = list(accumulate(values))

    assert array_in_chunks("q", accumulate(values)) == array.array("q", totals)
    assert float_column(map(float, accumulate(values))) == [float(total) for total in totals]
    assert count_column(array.array("q", totals)) == totals
    joined = array.array("q")
    for chunk in in_chunks(array.array("q", totals)):
        assert len(chunk) <= 8
        joined.extend(chunk)
    assert joined == array.array("q", totals)
    filled = fill_in_chunks(SampleColumn("d"), map(float, values))
    assert isinstance(filled, SampleColumn) and filled == [float(value) for value in values]


def test_largest_and_positions_cut_in_chunks_equal_the_plain_ones(chunk_items):
    """잘라 구한 최댓값 · 걸러 낸 자리는 한 번에 구한 것과 같아야 한다.

    조각 4개. 값 11개(최댓값이 마지막 조각에 있다), 샘플 번호 11개 가운데 넷이 wanted에 든다
    -> _largest == max, _positions_in == wanted에 든 샘플의 자리
    """
    chunk_items(4)
    values = [0.5, 3.25, 1.0, 2.0, 9.0, 4.0, 8.5, 1.5, 0.25, 7.0, 9.5]
    samples = array.array("Q", [5, 0, 9, 2, 7, 1, 8, 3, 10, 4, 6])
    wanted = frozenset({0, 3, 6, 9})

    assert mp4_module._largest(iter(values)) == max(values)
    assert list(mp4_module._positions_in(samples, wanted)) == [1, 2, 7, 10]


# ================================================================ 실제로 잘라 돈다


def _long_moov(frames: int) -> bytes:
    """긴 영상의 moov — 60fps · IPBB 재정렬 · 1초마다 키프레임 · 샘플마다 다른 크기, 오디오 없음.

    mdat는 만들지 않는다(해석은 moov만 본다). 청크 위치는 샘플이 이어 놓인 것처럼 적는다.
    """
    per_chunk = 30
    chunks = [per_chunk] * (frames // per_chunk) + ([frames % per_chunk] or [])
    chunks = [size for size in chunks if size]
    sizes = [20_000 + (n * 7919) % 9000 for n in range(frames)]
    spec = TrackSpec(
        handler=b"vide",
        timescale=60_000,
        deltas=[1000] * frames,
        sizes=sizes,
        chunks=chunks,
        composition=[(1000, 3000, 0, 0)[n % 4] for n in range(frames)],
        sync=list(range(1, frames + 1, 60)),
        edits=[(frames * 1000 // 60, 1000)],
    )
    offsets = list(accumulate((sum(sizes[n : n + per_chunk]) for n in range(0, frames, per_chunk))))
    chunk_offsets = [40, *(40 + offset for offset in offsets[:-1])]
    mvhd = full_box(b"mvhd", struct.pack(">IIII", 0, 0, 1000, 0) + bytes(80))
    return box(b"moov", mvhd + _trak(spec, chunk_offsets, True))


def test_parser_never_hands_more_than_a_chunk_to_one_pass(monkeypatch, chunk_items):
    """해석기는 샘플 수만큼 긴 반복을 조각 크기보다 많이 한 번에 넘기지 않아야 한다.

    조각 1,000개. 영상 20,000샘플의 moov를 해석하며 배열을 채우는 쪽이 한 번에 청하는 양
    (islice)과 정렬 · 세기에 한 번에 넘기는 양을 적음
    -> 채우기: 청한 양이 모두 1,000 이하이고 20번 넘게 청한다
    -> 정렬 · 세기: 한 번에 넘긴 양이 모두 1,000 + 재정렬 여유(64) 이하이고 각각 20번 넘게 돈다
    """
    frames, items = 20_000, 1_000
    chunk_items(items)
    asked: list[int] = []
    sorted_sizes: list[int] = []
    counted_sizes: list[int] = []
    real_islice, real_counter = column_module.islice, mp4_module.Counter

    def watching_islice(source, stop):
        asked.append(stop)
        return real_islice(source, stop)

    def watching_sorted(values, **kwargs):
        sorted_sizes.append(len(values))
        return sorted(values, **kwargs)

    class WatchingCounter(real_counter):
        def update(self, values=None, **kwargs):
            if values is not None:  # 빈 Counter를 만들 때도 update(None)이 불린다
                counted_sizes.append(len(values))
            super().update(values, **kwargs)

    monkeypatch.setattr(column_module, "islice", watching_islice)
    monkeypatch.setattr(mp4_module, "sorted", watching_sorted, raising=False)
    monkeypatch.setattr(mp4_module, "Counter", WatchingCounter)

    index = parse_moov(_long_moov(frames))

    assert len(index.frame_pts) == frames, "전제: 모든 프레임이 색인에 있다"
    assert max(asked) <= items and len(asked) > frames // items
    assert max(sorted_sizes) <= items + 64 and len(sorted_sizes) >= frames // (items + 64)
    assert max(counted_sizes) <= items and len(counted_sizes) >= frames // items


# ================================================================ 다른 스레드가 멈추지 않는다

# 틱 스레드가 쉬는 간격(초) — GUI의 타이머 틱과 같은 크기다
_TICK_SECONDS = 0.005
# 잘라 돈 해석의 틱 최대 간격이 한 번에 돈 해석의 것의 이 비율 아래여야 한다. 이 머신에서 잰
# 값은 약 0.15(40ms ÷ 300ms)다 — 절대 시간을 박지 않는 것은 러너마다 속도가 몇 배씩 다르기
# 때문이고, 0.5는 다른 프로세스가 끼어들어 틱이 한두 번 늦어져도 넘지 않는 여유다
_GAP_RATIO = 0.5
_ATTEMPTS = 3  # 러너가 잠깐 멈춘 한 번으로 실패하지 않게 — 한 번이라도 넘지 않으면 통과다


def _worst_tick_gap(work) -> float:
    """work가 다른 스레드에서 도는 동안, 이 스레드가 짧게 쉬고 깨어나기를 되풀이한 간격의 최댓값."""
    done = threading.Event()
    worker = threading.Thread(target=lambda: (work(), done.set()))
    worst = 0.0
    worker.start()
    last = time.perf_counter()
    while not done.is_set():
        time.sleep(_TICK_SECONDS)  # GIL을 놓고 쉰다 — 깨어나려면 GIL을 다시 얻어야 한다
        now = time.perf_counter()
        worst = max(worst, now - last)
        last = now
    worker.join()
    return worst


def test_another_thread_keeps_running_while_a_long_moov_is_parsed(chunk_items):
    """긴 moov를 해석하는 동안 다른 스레드가 오래 멈추지 않아야 한다 — 한 번에 돌 때보다 훨씬 짧게.

    영상 600,000샘플의 moov. 다른 스레드에서 해석하는 동안 이 스레드가 5ms씩 쉬며 깨어난 간격을
    잼. 조각 없이(한 번에) 해석할 때와 제품의 조각 크기로 해석할 때를 견줌(세 번까지 다시 잰다)
    -> 잘라 돈 해석의 최대 간격 < 한 번에 돈 해석의 최대 간격 × 0.5
    """
    moov = _long_moov(600_000)
    product_items = column_module.CHUNK_ITEMS
    seen = []
    for _attempt in range(_ATTEMPTS):
        chunk_items(ONE_PASS)
        whole = _worst_tick_gap(lambda: parse_moov(moov))
        chunk_items(product_items)
        chunked = _worst_tick_gap(lambda: parse_moov(moov))
        seen.append((round(chunked * 1000), round(whole * 1000)))
        if chunked < whole * _GAP_RATIO:
            return
    pytest.fail(
        f"잘라 돈 해석이 다른 스레드를 오래 멈췄다 — (잘라 돈 것, 한 번에 돈 것) ms: {seen}"
    )
