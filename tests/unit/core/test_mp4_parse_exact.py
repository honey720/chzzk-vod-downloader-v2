"""moov 해석의 값이 계산 방법과 무관하게 정확한지 (#309).

`core.api.mp4`는 긴 영상에서 해석이 느려 샘플 시각과 샘플 위치의 계산을 바꿨다. 결과
(`Mp4Index`)는 그 전과 같아야 한다. 여기서는 제품의 계산을 쓰지 않고 정의 그대로 —
시각은 분수(Fraction)로, 위치는 샘플마다 더해 가며 — 기대값을 만든다.
"""

import random
from fractions import Fraction
from itertools import accumulate

import pytest

from core.api.mp4 import parse_moov
from tests.unit.core.mp4_builder import TrackSpec, audio_spec, build_mp4, video_spec


def _expected_times(spec: TrackSpec, movie_timescale: int, origin: Fraction) -> list[float]:
    """정의 그대로의 표시 시각 — 빈 편집 + (DTS + ctts − media_time) ÷ timescale − origin."""
    empty_edit, media_time = _edit(spec, movie_timescale)
    decode = [0, *accumulate(spec.deltas)][: len(spec.deltas)]
    composition = spec.composition or [0] * len(spec.deltas)
    return [
        float(empty_edit - origin + Fraction(dts + cts - media_time, spec.timescale))
        for dts, cts in zip(decode, composition)
    ]


def _edit(spec: TrackSpec, movie_timescale: int) -> tuple[Fraction, int]:
    """편집 목록 → (앞의 빈 편집 길이[초], 첫 실제 편집의 media_time)."""
    empty, media_time = Fraction(0), 0
    for duration, time in spec.edits or []:
        if time == -1:
            empty += Fraction(duration, movie_timescale)
        else:
            media_time = time
            break
    return empty, media_time


def _start(spec: TrackSpec, movie_timescale: int) -> Fraction:
    """트랙에서 가장 먼저 표시되는 샘플의 시각(초)."""
    empty_edit, media_time = _edit(spec, movie_timescale)
    decode = [0, *accumulate(spec.deltas)][: len(spec.deltas)]
    composition = spec.composition or [0] * len(spec.deltas)
    shown = [dts + cts - media_time for dts, cts in zip(decode, composition)]
    return empty_edit + Fraction(min(t for t in shown if t >= 0), spec.timescale)


def _random_specs(seed: int) -> tuple[TrackSpec, TrackSpec, int]:
    """무작위 영상 · 오디오 재료 — 나누어떨어지지 않는 timescale, 고르지 않은 샘플 길이와 청크."""
    generator = random.Random(seed)
    movie_timescale = generator.choice([1000, 600, 90000])
    count = generator.randrange(40, 160)
    video_scale = generator.choice([30000, 60000, 90000, 15360, 2997])
    base = generator.choice([1001, 1000, 500, 256, 100])
    deltas = [base + generator.choice([0, 0, 0, 1, -1]) for _ in range(count)]
    reorder = [generator.choice([0, base, 2 * base, 3 * base]) for _ in range(count)]
    video = TrackSpec(
        handler=b"vide",
        timescale=video_scale,
        deltas=deltas,
        sizes=[generator.randrange(1, 5000) for _ in range(count)],
        chunks=_chunks(generator, count),
        composition=reorder,
        sync=sorted(generator.sample(range(1, count + 1), k=max(1, count // 12))),
        edits=[
            (generator.randrange(0, 5000), -1),  # 앞의 빈 편집
            (movie_timescale * 60, generator.choice([0, base, 2 * base])),
        ],
    )
    audio_count = generator.randrange(60, 240)
    audio = TrackSpec(
        handler=b"soun",
        timescale=generator.choice([48000, 44100]),
        deltas=[1024] * audio_count,
        sizes=[generator.randrange(100, 600) for _ in range(audio_count)],
        chunks=_chunks(generator, audio_count),
        edits=[(movie_timescale * 60, generator.choice([0, 1024, 2112]))],
    )
    return video, audio, movie_timescale


def _chunks(generator: random.Random, count: int) -> list[int]:
    """샘플 count개를 크기가 고르지 않은 청크들로 나눈다."""
    chunks = []
    while count:
        size = min(count, generator.randrange(1, 9))
        chunks.append(size)
        count -= size
    return chunks


@pytest.mark.parametrize("seed", range(40))
def test_sample_times_equal_the_exact_rational_definition(seed):
    """샘플의 표시 시각은 분수로 정확히 계산한 값을 float로 바꾼 것과 비트까지 같아야 한다.

    씨앗마다 무작위 영상 · 오디오(나누어떨어지지 않는 timescale · 빈 편집 · 재정렬)를 만들어 해석
    -> 영상 · 오디오의 times가 정의 그대로 계산한 값과 == (근사가 아니다)
    """
    video, audio, movie_timescale = _random_specs(seed)
    built = build_mp4([video, audio], movie_timescale=movie_timescale)
    origin = min(_start(video, movie_timescale), _start(audio, movie_timescale))

    index = parse_moov(built.moov)

    assert list(index.video.times) == _expected_times(video, movie_timescale, origin)
    assert list(index.audio.times) == _expected_times(audio, movie_timescale, origin)


@pytest.mark.parametrize("seed", range(40))
def test_sample_offsets_equal_the_positions_the_builder_wrote(seed):
    """샘플의 파일 안 위치는 조립기가 샘플을 써 넣은 위치와 같아야 한다.

    씨앗마다 무작위 영상 · 오디오(청크마다 샘플 수가 다르다)를 만들어 해석
    -> 영상 · 오디오의 offsets == 조립기가 기록한 샘플 위치, chunk_starts == 청크 크기의 누적
    """
    video, audio, movie_timescale = _random_specs(seed)
    built = build_mp4([video, audio], movie_timescale=movie_timescale)

    index = parse_moov(built.moov)

    for track, spec in ((index.video, video), (index.audio, audio)):
        assert list(track.offsets) == built.sample_offsets[spec.handler]
        assert list(track.chunk_starts) == [0, *accumulate(spec.chunks)][: len(spec.chunks)]


def test_standard_fixture_times_are_unchanged():
    """표준 재료(10fps 영상 12샘플 · 8000Hz 오디오 16샘플)의 표시 시각은 손으로 센 값이어야 한다.

    영상은 디코드 순서 I P B B 세 묶음 -> 표시 시각(초) [0.0, 0.3, 0.1, 0.2, 0.4, 0.7, 0.5, 0.6, …]
    오디오는 0.128초 간격이고 첫 샘플은 편집 목록이 가린다 -> [-0.128, 0.0, 0.128, …]
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert list(index.video.times[:8]) == [0.0, 0.3, 0.1, 0.2, 0.4, 0.7, 0.5, 0.6]
    assert list(index.audio.times[:3]) == [-0.128, 0.0, 0.128]
