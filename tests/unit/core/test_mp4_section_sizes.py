"""색인 없이 센 구간의 크기가 색인으로 센 값과 같은지 (#309).

`core.utils.mp4_section_sizes`는 moov의 표에서 구간에 닿는 샘플만 찾아 받을 바이트의 합과
파일의 머리 길이를 낸다. 계약은 하나다 — **색인으로 센 값(`mp4_ranges.sections_sizes` ·
`sections_head_size`)과 바이트까지 같다.** 여기서는 같은 입력에 두 계산을 돌려 견준다.
입력은 조립기(``mp4_builder``)로 만든 mp4이고, 구간은 씨앗을 정한 난수로 뽑는다.
"""

import random
import struct
import threading
from itertools import accumulate

import pytest

import core.utils.mp4_section_sizes as light
from core.api.mp4 import MP4_UNSUPPORTED, Mp4Error, index_mp4, read_mp4_raw
from core.models.mp4_index import Mp4Raw, parse_gate
from core.models.plan import TimeRange
from core.utils.mp4_ranges import sections_head_size, sections_sizes
from core.utils.mp4_section_sizes import (
    LightTables,
    NeedsFullIndex,
    moov_head_size,
    moov_sections_sizes,
)
from core.utils.stop import StopRequested, stoppable
from tests.unit.core.mp4_builder import (
    TrackSpec,
    _trak,
    audio_spec,
    box,
    build_mp4,
    full_box,
    video_spec,
)
from tests.unit.core.test_mp4_summary import CASES as SUMMARY_CASES

SELECTIONS_PER_SHAPE = 300  # 모양마다 뽑는 구간 수


def _chunks(count: int, size: int) -> list[int]:
    chunks = [size] * (count // size)
    if count % size:
        chunks.append(count % size)
    return chunks


def _video(count: int, *, seed: int, per_chunk: int, b_frames: bool, edits=None, sync=True):
    """60fps 영상 — 샘플 크기는 고르지 않다. b_frames면 P 하나 + B 1~3개의 묶음이 섞인다."""
    rng = random.Random(seed)
    composition = None
    keyframes = [1]
    if b_frames:
        composition = [1000]
        while len(composition) < count:
            group = rng.choice((2, 3, 4))
            if len(composition) - keyframes[-1] + 1 >= 48:
                keyframes.append(len(composition) + 1)
            composition += [group * 1000] + [0] * (group - 1)
        composition = composition[:count]
    else:
        keyframes = list(range(1, count + 1, 48))
    return TrackSpec(
        handler=b"vide",
        timescale=60000,
        deltas=[1000] * count,
        sizes=[rng.randint(400, 3000) for _ in range(count)],
        chunks=_chunks(count, per_chunk),
        composition=composition,
        sync=keyframes if sync else None,
        edits=edits,
    )


def _audio(count: int, *, seed: int, per_chunk: int, edits=None, uniform: bool = False):
    rng = random.Random(seed)
    return TrackSpec(
        handler=b"soun",
        timescale=48000,
        deltas=[1024] * count,
        sizes=[34] * count if uniform else [rng.randint(30, 40) for _ in range(count)],
        chunks=_chunks(count, per_chunk),
        edits=edits,
        uniform_size=uniform,
    )


# 모양 → (트랙 재료를 만드는 함수, build_mp4의 인자)
SHAPES = {
    # 청크 묶음 — 청크 하나에 샘플 여럿
    "grouped-chunks": lambda: (
        [
            _video(1500, seed=1, per_chunk=30, b_frames=True, edits=[(25000, 1000)]),
            _audio(1180, seed=2, per_chunk=23),
        ],
        {},
    ),
    # 청크당 샘플 하나 — 청크 위치 표가 샘플 수만큼이다
    "one-sample-per-chunk": lambda: (
        [
            _video(1500, seed=3, per_chunk=1, b_frames=True, edits=[(25000, 1000)]),
            _audio(1180, seed=4, per_chunk=1),
        ],
        {"co64": True},
    ),
    # 편집 목록이 앞 샘플을 가린다 — 영상 · 오디오 모두
    "edit-list-hides-the-head": lambda: (
        [
            _video(1200, seed=5, per_chunk=17, b_frames=True, edits=[(20000, 9000)]),
            _audio(940, seed=6, per_chunk=11, edits=[(20000, 3072)]),
        ],
        {},
    ),
    # 앞의 빈 편집 — 오디오가 먼저 시작한다
    "empty-edit-before-the-video": lambda: (
        [
            _video(1200, seed=7, per_chunk=30, b_frames=True, edits=[(700, -1), (20000, 1000)]),
            _audio(970, seed=8, per_chunk=23),
        ],
        {},
    ),
    # ctts가 있다 · 편집 목록이 없다 — 첫 프레임이 0보다 늦게 표시된다
    "ctts-without-an-edit-list": lambda: (
        [
            _video(1200, seed=9, per_chunk=30, b_frames=True),
            _audio(940, seed=10, per_chunk=23, uniform=True),
        ],
        {},
    ),
    # ctts가 없다 — 표시 순서가 샘플 순서다
    "no-ctts": lambda: (
        [
            _video(1200, seed=11, per_chunk=30, b_frames=False),
            _audio(940, seed=12, per_chunk=23),
        ],
        {},
    ),
    # 오디오가 없다 · stss가 없다(모든 샘플이 키프레임)
    "no-audio-no-stss": lambda: (
        [_video(900, seed=13, per_chunk=12, b_frames=False, sync=False)],
        {"large_mdat_header": True},
    ),
    # 샘플 길이가 여러 번 바뀐다 — 영상 · 오디오 모두 stts의 구간이 여럿이다
    "several-stts-runs": lambda: (
        [
            TrackSpec(
                handler=b"vide",
                timescale=90000,
                deltas=[3000] * 400 + [1500] * 200 + [3003] * 399 + [4500],
                sizes=[500 + (n * 37) % 900 for n in range(1000)],
                chunks=_chunks(1000, 15),
                sync=list(range(1, 1001, 40)),
            ),
            TrackSpec(
                handler=b"soun",
                timescale=48000,
                deltas=[1024] * 500 + [960] * 300 + [1024] * 400,
                sizes=[30 + n % 9 for n in range(1200)],
                chunks=_chunks(1200, 23),
            ),
        ],
        {},
    ),
    # 표시 시각이 같은 샘플이 이어진다 — 길이 0인 샘플이 하나 걸러 있고 ctts는 모두 0이다
    "equal-presentation-times": lambda: (
        [
            TrackSpec(
                handler=b"vide",
                timescale=30000,
                deltas=[2000, 0] * 300,
                sizes=[300 + (n * 53) % 700 for n in range(600)],
                chunks=_chunks(600, 7),
                composition=[0] * 600,
                sync=list(range(1, 601, 25)),
            ),
            _audio(940, seed=14, per_chunk=23),
        ],
        {},
    ),
    # 키프레임이 가려진 첫 샘플 하나뿐이다 · 청크당 샘플 하나 — 범위가 0번 프레임(표시되는 첫
    # 프레임)의 샘플에서 시작한다. 가려진 샘플의 수를 잘못 세면 다른 청크에서 시작한다
    "only-keyframe-is-hidden": lambda: (
        [
            TrackSpec(
                handler=b"vide",
                timescale=30000,
                deltas=[1001] * 240,
                sizes=[300 + (n * 41) % 500 for n in range(240)],
                chunks=[1] * 240,
                composition=[1001, 3003, 0, 0] * 60,
                sync=[1],
                edits=[(8000, 5005)],
            ),
            _audio(380, seed=15, per_chunk=1, edits=[(8000, 2048)]),
        ],
        {},
    ),
    # 오디오 청크가 파일에서 먼저 놓인다 — 첫 샘플의 위치(머리 길이)가 오디오의 것이다
    "audio-chunk-first": lambda: (
        [
            _audio(940, seed=16, per_chunk=23),
            _video(1200, seed=17, per_chunk=30, b_frames=True, edits=[(20000, 1000)]),
        ],
        {},
    ),
    # 조립기의 표준 재료 — I B B P 재정렬 · 편집 목록 · 고정 크기 오디오
    "builder-standard": lambda: ([video_spec(), audio_spec()], {}),
}
# 가벼운 해석을 재는 테스트의 재료도 쓴다 — 가변 프레임률 · 음수 ctts · 가려진 앞부분 등
for _name, _make in SUMMARY_CASES.items():
    SHAPES[f"summary:{_name}"] = lambda make=_make: ([spec for spec in make() if spec], {})


def _raw_of(tracks, options):
    data = build_mp4(tracks, **options).data
    return read_mp4_raw(lambda offset, size: data[offset : offset + size])


def _head_only(tracks) -> Mp4Raw:
    """ftyp + moov만 만든다 — 샘플 바이트를 쓰지 않아 긴 표도 금방 만든다.

    청크는 moov 뒤에 mdat(64비트 머리)이 이어진다고 보고 트랙마다 한 청크씩 번갈아 놓는다.
    """
    ftyp = box(b"ftyp", b"isom" + bytes(4) + b"isomavc1")

    def moov_for(offsets) -> bytes:
        mvhd = full_box(b"mvhd", struct.pack(">IIII", 0, 0, 1000, 0) + bytes(80))
        return box(
            b"moov", mvhd + b"".join(_trak(s, offsets[t], True) for t, s in enumerate(tracks))
        )

    placeholder = moov_for([[0] * len(spec.chunks) for spec in tracks])
    position = len(ftyp) + len(placeholder) + 16
    chunk_bytes = []
    for spec in tracks:
        sums = [0, *accumulate(spec.sizes)]
        edges = [0, *accumulate(spec.chunks)]
        chunk_bytes.append([sums[edges[c + 1]] - sums[edges[c]] for c in range(len(spec.chunks))])
    offsets = [[0] * len(spec.chunks) for spec in tracks]
    for chunk in range(max(len(spec.chunks) for spec in tracks)):
        for t, spec in enumerate(tracks):
            if chunk < len(spec.chunks):
                offsets[t][chunk] = position
                position += chunk_bytes[t][chunk]
    moov = moov_for(offsets)
    return Mp4Raw(moov=moov, moov_range=(len(ftyp), len(ftyp) + len(moov) - 1), prefix=ftyp)


def _random_selections(index, rng: random.Random, count: int) -> list[TimeRange]:
    """영상 안팎에 걸친 구간 — 프레임 시각 그대로, 프레임 사이, 영상 밖을 섞는다."""
    points = list(index.frame_pts) + list(index.video.decode_times)
    frame = float(1 / index.fps)

    def point() -> float:
        kind = rng.random()
        if kind < 0.35:
            return rng.choice(points)  # 프레임 시각 그대로
        if kind < 0.55:
            return rng.choice(points) + rng.choice((-0.5, 0.5, 0.49, -0.51)) * frame
        return rng.uniform(-0.3, index.duration + 0.3)

    selections = []
    while len(selections) < count:
        start, end = sorted((point(), point()))
        if start < end:
            selections.append(TimeRange(start, end))
    return selections


def _outcome(function):
    """함수의 결과 — 값이거나, 던진 예외의 종류(와 Mp4Error의 실패 키)."""
    try:
        return function()
    except ValueError:
        return "ValueError"
    except Mp4Error as error:
        return ("Mp4Error", error.message_key)


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_light_sizes_equal_the_index_sizes_for_random_selections(shape):
    """색인 없이 센 (받을 바이트의 합, 머리 길이)는 무작위 구간마다 색인으로 센 값과 같아야 한다.

    모양마다 구간 300개(프레임 시각 그대로 · 프레임 사이 · 영상 밖을 섞음), 씨앗 고정
    -> LightTables.sections_sizes([구간]) == mp4_ranges.sections_sizes(색인, [구간])
       (예외로 끝나면 예외의 종류가 같다)
    """
    tracks, options = SHAPES[shape]()
    raw = _raw_of(tracks, options)
    index = index_mp4(raw).index
    tables = LightTables(raw)  # 색인으로 넘기지 않고 표에서 센다 — 넘기면 여기서 예외가 난다
    rng = random.Random(f"sizes:{shape}")

    different = []
    for selection in _random_selections(index, rng, SELECTIONS_PER_SHAPE):
        expected = _outcome(lambda: sections_sizes(index, [selection]))
        actual = _outcome(lambda: tables.sections_sizes([selection]))
        if actual != expected:
            different.append((selection, expected, actual))

    assert different == []


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_light_sizes_equal_the_index_sizes_for_several_selections_at_once(shape):
    """구간 여럿을 함께 세도(겹치는 자리는 한 번만) 색인으로 센 값과 같아야 한다.

    모양마다 구간 1~6개짜리 묶음 60개, 씨앗 고정
    -> LightTables.sections_sizes(묶음) == mp4_ranges.sections_sizes(색인, 묶음)
    """
    tracks, options = SHAPES[shape]()
    raw = _raw_of(tracks, options)
    index = index_mp4(raw).index
    tables = LightTables(raw)
    rng = random.Random(f"groups:{shape}")

    different = []
    for _ in range(60):
        group = _random_selections(index, rng, rng.randint(1, 6))
        expected = _outcome(lambda: sections_sizes(index, group))
        actual = _outcome(lambda: tables.sections_sizes(group))
        if actual != expected:
            different.append((group, expected, actual))

    assert different == []


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_light_head_size_equals_the_index_head_size(shape):
    """색인 없이 구한 머리 길이는 색인으로 구한 값과 같아야 한다.

    모양마다 -> LightTables.head_size() == mp4_ranges.sections_head_size(색인)
    """
    tracks, options = SHAPES[shape]()
    raw = _raw_of(tracks, options)

    assert LightTables(raw).head_size() == sections_head_size(index_mp4(raw).index)


def test_light_sizes_equal_the_index_sizes_when_tables_are_longer_than_one_chunk():
    """표가 자르는 단위(CHUNK_ITEMS)보다 길어도 색인으로 센 값과 같아야 한다.

    영상 70,000샘플(청크당 샘플 하나 · stss에 모든 샘플) · 오디오 40,000샘플, 구간 40개
    -> LightTables.sections_sizes([구간]) == mp4_ranges.sections_sizes(색인, [구간])
    """
    count = 70_000  # CHUNK_ITEMS(32,768)의 두 배를 넘는다
    video = TrackSpec(
        handler=b"vide",
        timescale=60000,
        deltas=[1000] * count,
        sizes=[20 + n % 7 for n in range(count)],
        chunks=[1] * count,
        composition=([1000] + [3000, 0, 0] * count)[:count],
        sync=list(range(1, count + 1)),
        edits=[(count * 1000 // 60, 1000)],
    )
    audio = _audio(40_000, seed=21, per_chunk=1)
    raw = _head_only([video, audio])
    index = index_mp4(raw).index
    tables = LightTables(raw)
    rng = random.Random("long")

    for selection in _random_selections(index, rng, 40):
        assert tables.sections_sizes([selection]) == sections_sizes(index, [selection])


def test_light_sizes_do_not_build_an_index(monkeypatch):
    """표에서 셀 수 있는 모양이면 moov_sections_sizes는 색인을 만들지 않아야 한다.

    청크 묶음 모양, index_mp4 · parse_moov를 부르면 실패하게 바꿔 둠, 구간 1.0~5.0초
    -> 색인으로 센 값과 같은 값이 나온다(색인 해석은 한 번도 불리지 않는다)
    """
    tracks, options = SHAPES["grouped-chunks"]()
    raw = _raw_of(tracks, options)
    expected = sections_sizes(index_mp4(raw).index, [TimeRange(1.0, 5.0)])

    def forbidden(*args, **kwargs):
        raise AssertionError("색인을 만들었다")

    monkeypatch.setattr(light.mp4, "index_mp4", forbidden)
    monkeypatch.setattr(light.mp4, "parse_moov", forbidden)

    assert moov_sections_sizes(raw, [TimeRange(1.0, 5.0)]) == expected
    assert moov_head_size(raw) == expected[1]


def _with_audio_composition():
    """오디오에 ctts가 있다 — 표에서 세지 않는 모양."""
    audio = _audio(300, seed=31, per_chunk=23)
    audio.composition = [0] * 300
    return [_video(400, seed=30, per_chunk=30, b_frames=True), audio]


def test_shape_that_cannot_be_read_from_tables_is_reported():
    """표에서 셀 수 없는 모양이면 LightTables는 NeedsFullIndex를 던져야 한다.

    오디오 트랙에 ctts가 있는 mp4 -> NeedsFullIndex
    """
    raw = _raw_of(_with_audio_composition(), {})

    with pytest.raises(NeedsFullIndex):
        LightTables(raw)


def test_shape_that_cannot_be_read_from_tables_is_counted_with_an_index():
    """표에서 셀 수 없는 모양이면 moov_sections_sizes는 색인을 만들어 같은 값을 내야 한다.

    오디오 트랙에 ctts가 있는 mp4, 구간 1.0~3.0초
    -> moov_sections_sizes == mp4_ranges.sections_sizes(색인, …), moov_head_size도 같다
    """
    raw = _raw_of(_with_audio_composition(), {})
    index = index_mp4(raw).index
    selections = [TimeRange(1.0, 3.0)]

    assert moov_sections_sizes(raw, selections) == sections_sizes(index, selections)
    assert moov_head_size(raw) == sections_head_size(index)


def test_moov_behind_the_samples_is_refused_like_the_index():
    """moov가 샘플보다 뒤에 있으면 색인으로 셀 때와 같은 실패 키의 Mp4Error여야 한다.

    ftyp · mdat · moov 순서의 mp4, 구간 0.2~0.5초 -> Mp4Error(MP4_UNSUPPORTED) — 크기 · 머리 모두
    """
    data = build_mp4([video_spec(), audio_spec()], moov_first=False).data
    raw = read_mp4_raw(lambda offset, size: data[offset : offset + size])

    with pytest.raises(Mp4Error) as sizes:
        moov_sections_sizes(raw, [TimeRange(0.2, 0.5)])
    with pytest.raises(Mp4Error) as head:
        moov_head_size(raw)

    assert sizes.value.message_key == head.value.message_key == MP4_UNSUPPORTED


def test_damaged_chunk_table_is_judged_by_the_index():
    """표가 어긋난 moov면 moov_sections_sizes는 색인 해석과 같은 예외를 내야 한다.

    stsc의 청크당 샘플 수를 하나 늘린(샘플 수가 stsz와 달라진) mp4
    -> index_mp4가 던지는 것과 같은 실패 키의 Mp4Error
    """
    data = bytearray(build_mp4([video_spec(), audio_spec()]).data)
    at = data.index(b"stsc") + 4 + 8  # 버전 · 플래그(4) · 항목 수(4) 뒤가 첫 항목이다
    first_chunk, per_chunk = struct.unpack_from(">II", data, at)
    struct.pack_into(">II", data, at, first_chunk, per_chunk + 1)
    raw = read_mp4_raw(lambda offset, size: bytes(data[offset : offset + size]))
    with pytest.raises(Mp4Error) as expected:
        index_mp4(raw)

    with pytest.raises(Mp4Error) as actual:
        moov_sections_sizes(raw, [TimeRange(0.2, 0.5)])

    assert actual.value.message_key == expected.value.message_key


def test_counting_stops_when_told_to():
    """그만두라는 답이 걸려 있으면 moov_sections_sizes는 StopRequested로 끝나야 한다.

    청크 묶음 모양, stoppable(항상 참) 안에서 부름 -> StopRequested
    """
    tracks, options = SHAPES["grouped-chunks"]()
    raw = _raw_of(tracks, options)

    with stoppable(lambda: True), pytest.raises(StopRequested):
        moov_sections_sizes(raw, [TimeRange(1.0, 5.0)])


def test_counting_with_an_index_waits_for_its_turn_to_parse():
    """색인을 만들어 세야 하는 모양이면, 다른 해석이 도는 동안에는 기다렸다가 세어야 한다.

    오디오 트랙에 ctts가 있는 mp4. 해석의 차례를 테스트가 쥔 채 다른 스레드에서 세게 하고,
    0.3초 뒤 차례를 놓음
    -> 차례를 쥔 동안: 세는 스레드가 끝나지 않았다, 놓은 뒤: 색인으로 센 값과 같은 값이 나온다
    """
    raw = _raw_of(_with_audio_composition(), {})
    selections = [TimeRange(1.0, 3.0)]
    expected = sections_sizes(index_mp4(raw).index, selections)
    got = []
    worker = threading.Thread(target=lambda: got.append(moov_sections_sizes(raw, selections)))

    with parse_gate():
        worker.start()
        worker.join(0.3)  # 기다리는 구간을 넓힌다 — 차례를 얻기 전에는 돌아오지 않는다
        waiting = worker.is_alive()
    worker.join(10)

    assert waiting
    assert got == [expected]


def test_no_room_for_the_mdat_header_is_refused_like_the_index():
    """moov와 첫 샘플 사이에 mdat 머리가 들어갈 자리가 없으면 색인으로 셀 때와 같은 실패 키의 Mp4Error여야 한다.

    moov의 끝을 4바이트 뒤로 적은 moov(첫 샘플까지 4바이트 — 머리는 8바이트다), 구간 0.2~0.5초
    -> 색인으로 셀 때와 같은 실패 키(MP4_UNSUPPORTED) — 크기 · 머리 모두
    """
    data = build_mp4([video_spec(), audio_spec()]).data
    raw = read_mp4_raw(lambda offset, size: data[offset : offset + size])
    tight = Mp4Raw(
        moov=raw.moov, moov_range=(raw.moov_range[0], raw.moov_range[1] + 4), prefix=raw.prefix
    )
    with pytest.raises(Mp4Error) as expected:
        sections_sizes(index_mp4(tight).index, [TimeRange(0.2, 0.5)])

    with pytest.raises(Mp4Error) as sizes:
        moov_sections_sizes(tight, [TimeRange(0.2, 0.5)])
    with pytest.raises(Mp4Error) as head:
        moov_head_size(tight)

    assert expected.value.message_key == MP4_UNSUPPORTED
    assert sizes.value.message_key == head.value.message_key == MP4_UNSUPPORTED
