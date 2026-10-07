"""moov에서 가볍게 읽은 프레임률 · 길이가 색인의 값과 같은지 (#309).

`summarize_moov`는 샘플마다 값을 펴지 않고 stts · ctts의 구간에서 프레임률과 길이를 구한다.
계약은 하나다 — **전체 해석(`parse_moov`)이 만든 색인의 `fps` · `duration`과 비트까지 같다.**
여기서는 그 둘을 같은 입력에 돌려 견준다. 입력은 조립기(``mp4_builder``)로 만든 mp4이고,
종류마다 손으로 정한 것과 씨앗으로 흔든 것을 쓴다.
"""

import dataclasses
import random
import struct
from fractions import Fraction

import pytest

import core.api.mp4 as mp4_module
from core.api.mp4 import (
    MP4_FRAGMENTED,
    MP4_INVALID,
    MP4_UNSUPPORTED,
    Mp4Error,
    index_mp4,
    parse_moov,
    read_mp4_raw,
    summarize_moov,
    summarize_mp4,
)
from tests.unit.core.mp4_builder import TrackSpec, audio_spec, box, build_mp4, video_spec


def _chunks(count: int, size: int) -> list[int]:
    chunks = [size] * (count // size)
    if count % size:
        chunks.append(count % size)
    return chunks


def _video(count: int, timescale: int, deltas, composition=None, edits=None, sync_every=30):
    return TrackSpec(
        handler=b"vide",
        timescale=timescale,
        deltas=list(deltas),
        sizes=[40] * count,
        chunks=_chunks(count, 15),
        composition=list(composition) if composition is not None else None,
        sync=list(range(1, count + 1, sync_every)),
        edits=edits,
    )


def _audio(count: int, edits=None):
    return TrackSpec(
        handler=b"soun",
        timescale=48000,
        deltas=[1024] * count,
        sizes=[7] * count,
        chunks=_chunks(count, 23),
        edits=edits,
    )


def _same(moov: bytes) -> None:
    """가벼운 해석과 전체 해석의 프레임률 · 길이 · 영상 샘플 수가 같은지 단언한다."""
    index = parse_moov(moov)
    summary = summarize_moov(moov)
    assert summary.fps == index.fps
    assert summary.duration.hex() == index.duration.hex()  # 비트까지
    assert summary.frames == len(index.video.sizes)


IPBB = [1001, 3003, 0, 0]

CASES = {
    # 고정 프레임률 — 구간 하나
    "30fps": lambda: [_video(120, 30000, [1000] * 120), _audio(190)],
    "60fps": lambda: [_video(240, 60000, [1000] * 240), _audio(190)],
    "29.97fps": lambda: [_video(120, 30000, [1001] * 120), _audio(190)],
    # B 프레임 — ctts가 샘플마다 바뀐다. 편집 목록이 재정렬 지연을 지운다
    "b-frames": lambda: [
        _video(120, 30000, [1001] * 120, IPBB * 30, edits=[(4004, 1001)]),
        _audio(190, edits=[(4004, 1024)]),
    ],
    # 마지막 프레임이 디코드 순서로는 끝이 아니다 — 가장 늦게 표시되는 샘플이 끝에서 셋째다
    "last-shown-is-not-last-decoded": lambda: [
        _video(120, 30000, [1001] * 120, IPBB * 30),
        _audio(190),
    ],
    # 가변 프레임률 — 길이가 여러 번 바뀐다
    "variable": lambda: [
        _video(100, 90000, [3000] * 40 + [1500] * 20 + [3003] * 39 + [4500]),
        _audio(150),
    ],
    # 가장 많은 길이가 둘 — 먼저 나온 쪽이 프레임률이다
    "two-modes": lambda: [_video(100, 1000, [40] * 50 + [33] * 50), _audio(150)],
    # 마지막 샘플만 길다 / 짧다 — 길이는 그 샘플이 정한다
    "long-last-sample": lambda: [_video(60, 1000, [33] * 59 + [500]), None],
    "short-last-sample": lambda: [_video(60, 1000, [33] * 59 + [1]), None],
    # 앞의 빈 편집 — 오디오가 먼저 시작한다
    "audio-starts-first": lambda: [
        _video(90, 30000, [1001] * 90, edits=[(700, -1), (3000, 0)]),
        _audio(150),
    ],
    # 편집 목록이 앞 샘플을 가린다(표시 시각이 음수)
    "hidden-head": lambda: [
        _video(90, 30000, [1001] * 90, IPBB * 22 + [1001, 3003], edits=[(3000, 5005)]),
        _audio(150, edits=[(3000, 2048)]),
    ],
    # ctts가 없다 / 오디오가 없다
    "no-ctts-no-audio": lambda: [_video(45, 600, [20] * 45), None],
    # ctts에 음수가 있다
    "negative-ctts": lambda: [
        _video(80, 30000, [1001] * 80, [2002, 4004, -1001, 0] * 20, edits=[(3000, 1001)]),
        _audio(120),
    ],
    # 드물게 한 번 나오는 길이의 샘플이 큰 ctts를 달고 가운데에 있다
    "rare-delta-in-the-middle": lambda: [
        _video(
            90,
            1000,
            [33] * 40 + [100] + [33] * 49,
            [0] * 40 + [5000] + [0] * 49,
        ),
        _audio(120),
    ],
}


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_summary_equals_the_full_parse(case):
    """가볍게 읽은 프레임률 · 길이는 그 mp4를 전체 해석한 색인의 값과 비트까지 같아야 한다.

    위 표의 종류마다(고정 · 가변 프레임률 · B 프레임 · 편집 목록 · 29.97/30/60fps · 오디오 먼저 시작 등)
    -> summarize_moov의 fps · duration · frames == parse_moov가 만든 색인의 것
    """
    video, audio = CASES[case]()

    _same(build_mp4([video] + ([audio] if audio else [])).moov)


def _random_tracks(seed: int):
    generator = random.Random(seed)
    count = generator.randrange(8, 120)
    timescale = generator.choice([1000, 600, 15360, 30000, 60000, 90000])
    base = generator.choice([20, 33, 500, 512, 1000, 1001, 3003])
    kind = generator.randrange(4)
    if kind == 0:  # 고정
        deltas = [base] * count
    elif kind == 1:  # 구간 몇 개
        deltas = []
        while len(deltas) < count:
            deltas += [generator.choice([base, base * 2, base // 2 or 1])] * generator.randrange(
                1, 12
            )
        deltas = deltas[:count]
    else:  # 샘플마다 흔들린다
        deltas = [base + generator.choice([0, 0, 1, -1, base]) for _ in range(count)]
    composition = None
    if generator.random() < 0.7:
        composition = [generator.choice([0, base, 2 * base, 3 * base, -base]) for _ in range(count)]
        if generator.random() < 0.5:  # 큰 값 하나를 아무 데나
            composition[generator.randrange(count)] += base * generator.randrange(4, 40)
    edits = None
    if generator.random() < 0.7:
        edits = []
        if generator.random() < 0.4:
            edits.append((generator.randrange(1, 4000), -1))
        edits.append((1000, generator.choice([0, base, 2 * base, 5 * base])))
    video = TrackSpec(
        handler=b"vide",
        timescale=timescale,
        deltas=deltas,
        sizes=[40] * count,
        chunks=_chunks(count, generator.randrange(1, 9)),
        composition=composition,
        sync=sorted(generator.sample(range(1, count + 1), k=max(1, count // 10))),
        edits=edits,
    )
    audio = None
    if generator.random() < 0.8:
        samples = generator.randrange(4, 160)
        audio_edits = (
            [(1000, generator.choice([0, 1024, 2048]))] if generator.random() < 0.6 else None
        )
        if audio_edits and generator.random() < 0.3:
            audio_edits.insert(0, (generator.randrange(1, 3000), -1))
        audio = _audio(samples, edits=audio_edits)
    return video, audio


@pytest.mark.parametrize("seed", range(300))
def test_summary_equals_the_full_parse_on_random_tracks(seed):
    """씨앗으로 흔든 트랙에서도 가볍게 읽은 값은 전체 해석의 값과 같아야 한다 — 실패하는 쪽도 같다.

    씨앗마다 무작위 timescale · 샘플 길이(고정 · 구간 · 샘플마다) · ctts(음수 · 큰 값 포함) ·
    편집 목록(빈 편집 · media_time) · 오디오(없음 · 편집 목록)
    -> 전체 해석이 색인을 만들면 fps · duration · frames가 같고, 전체 해석이 거부하면 같은 키로 거부한다
    """
    video, audio = _random_tracks(seed)
    moov = build_mp4([video] + ([audio] if audio else [])).moov

    try:
        parse_moov(moov)
    except Mp4Error as full:
        with pytest.raises(Mp4Error) as light:
            summarize_moov(moov)
        assert light.value.message_key == full.message_key
        return
    _same(moov)


def test_summary_does_not_expand_the_sample_tables(monkeypatch):
    """가벼운 해석은 샘플마다 값을 펴는 길(전체 해석)을 타지 않아야 한다.

    B 프레임 · 편집 목록이 있는 30fps 120프레임. 전체 해석 함수를 부르면 실패하게 바꿈
    -> summarize_moov가 값을 돌려준다(전체 해석을 부르지 않았다)
    """
    video, audio = CASES["b-frames"]()
    moov = build_mp4([video, audio]).moov
    expected = parse_moov(moov)

    def forbidden(_moov):
        raise AssertionError("전체 해석을 불렀다")

    monkeypatch.setattr(mp4_module, "parse_moov", forbidden)

    summary = summarize_moov(moov)

    assert (summary.fps, summary.duration) == (expected.fps, expected.duration)


def test_summary_falls_back_to_the_full_parse_when_sample_lengths_keep_changing(monkeypatch):
    """구간이 한계보다 많은(샘플 길이가 계속 바뀌는) 영상은 전체 해석으로 같은 값을 구해야 한다.

    구간의 한계를 3으로 줄임, 길이가 여섯 번 바뀌는 영상
    -> 전체 해석을 한 번 부르고, 값은 색인의 것과 같다
    """
    moov = build_mp4([_video(60, 1000, [33, 34] * 30), _audio(90)]).moov
    expected = parse_moov(moov)
    calls = []
    real = mp4_module.parse_moov
    monkeypatch.setattr(mp4_module, "_SUMMARY_MAX_RUNS", 3)
    monkeypatch.setattr(mp4_module, "parse_moov", lambda data: calls.append(1) or real(data))

    summary = summarize_moov(moov)

    assert len(calls) == 1
    assert (summary.fps, summary.duration.hex()) == (expected.fps, expected.duration.hex())


@pytest.mark.parametrize(
    ("damage", "key"),
    [
        ("mvex", MP4_FRAGMENTED),
        ("no-video", MP4_UNSUPPORTED),
        ("two-edits", MP4_UNSUPPORTED),
        ("garbage", MP4_INVALID),
    ],
)
def test_summary_rejects_what_the_full_parse_rejects(damage, key):
    """가벼운 해석은 조각난 mp4 · 영상 트랙 없음 · 여러 구간 편집 목록 · moov가 아닌 바이트를 전체 해석과 같은 키로 거부해야 한다."""
    if damage == "mvex":
        moov = build_mp4([video_spec(), audio_spec()], mvex=True).moov
    elif damage == "no-video":
        moov = build_mp4([audio_spec()]).moov
    elif damage == "two-edits":
        moov = build_mp4([video_spec(edits=[(600, 100), (600, 900)]), audio_spec()]).moov
    else:
        moov = box(b"free", bytes(32))

    with pytest.raises(Mp4Error) as light:
        summarize_moov(moov)
    with pytest.raises(Mp4Error) as full:
        parse_moov(moov)

    assert light.value.message_key == full.value.message_key == key


def _moov_span(data: bytes) -> tuple[int, int]:
    """파일의 최상위 상자를 차례로 넘겨 moov의 (첫 바이트, 끝 다음 바이트)를 찾는다."""
    position = 0
    while position < len(data):
        size, kind = struct.unpack_from(">I4s", data, position)
        assert size >= 8, "전제: 재료의 상자는 32비트 크기를 쓴다"
        if kind == b"moov":
            return position, position + size
        position += size
    raise AssertionError("재료에 moov가 없다")


def test_raw_moov_gives_the_index_and_bytes_of_the_file():
    """받아 둔 moov 바이트를 나중에 해석한 색인은 파일의 그 moov의 것이어야 하고, 파일 앞부분을 실어야 한다.

    표준 재료의 mp4(moov가 mdat 앞)를 read_mp4_raw로 받은 뒤 index_mp4 / summarize_mp4.
    moov의 자리는 테스트가 파일의 상자를 직접 넘겨 찾는다
    -> raw.moov == 파일의 moov 바이트, moov_range == (첫 바이트, 마지막 바이트)
    -> index_mp4의 색인 == 그 바이트를 parse_moov로 해석하고 자리를 적은 것,
       앞부분 바이트 == 파일의 0부터 moov의 끝까지
    -> summarize_mp4의 값 == 그 색인의 값
    """
    data = build_mp4([video_spec(), audio_spec()]).data
    start, end = _moov_span(data)

    def read(offset: int, size: int) -> bytes:
        return data[offset : offset + size]

    raw = read_mp4_raw(read)
    head = index_mp4(raw)

    assert raw.moov == data[start:end]
    assert raw.moov_range == (start, end - 1)
    assert head.index == dataclasses.replace(
        parse_moov(data[start:end]), moov_range=(start, end - 1)
    )
    assert head.data == data[:end]
    summary = summarize_mp4(raw)
    assert (summary.fps, summary.duration) == (head.index.fps, head.index.duration)
    assert summary.fps == Fraction(10)
