"""moov 해석이 덜 훑으려고 기대는 전제 — 전제가 깨지는 입력에서도 색인이 정의와 같은지 (#417).

해석기는 같은 결과를 덜 훑어 얻으려고 몇 가지 전제에 기댄다(표시 순서의 첫 값이 가장 이르다,
ctts가 없으면 표시 시각이 줄지 않는다, 샘플 번호가 32비트에 든다, 앞 샘플이 늦게 끝날 수 있는 한도).
기대값은 제품의 계산을 쓰지 않고 정의 그대로 만든다. 조각 크기를 아주 작게 바꿔 조각마다 훑다 멈추는
자리가 작은 트랙에서도 여러 번 생기게 한다.
"""

import array
from fractions import Fraction
from itertools import accumulate

import pytest

import core.container.mp4 as mp4_module
import core.container.sample_column as column_module
from core.container.mp4 import parse_moov
from tests.support.mp4_builder import TrackSpec, build_mp4
from tests.core.container.test_mp4_parse_exact import _edit, _expected_times, _random_specs, _start

SMALL_CHUNK = 4  # 조각 하나의 샘플 수 — 트랙마다 조각의 경계가 여러 번 생긴다


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    """해석기와 배열 모듈의 조각 크기를 SMALL_CHUNK로 줄인다."""
    monkeypatch.setattr(column_module, "CHUNK_ITEMS", SMALL_CHUNK)
    monkeypatch.setattr(mp4_module, "CHUNK_ITEMS", SMALL_CHUNK)


def _presented_ticks(spec: TrackSpec, movie_timescale: int) -> list[int]:
    """정의 그대로의 샘플별 표시 시각(틱) — DTS + ctts − media_time."""
    _, media_time = _edit(spec, movie_timescale)
    decode = [0, *accumulate(spec.deltas)][: len(spec.deltas)]
    composition = spec.composition or [0] * len(spec.deltas)
    return [dts + cts - media_time for dts, cts in zip(decode, composition)]


def _expected_frames(spec: TrackSpec, movie_timescale: int, origin: Fraction) -> dict:
    """정의 그대로의 표시 순서 · 표시 시각 · 키프레임 자리 · 길이."""
    ticks = _presented_ticks(spec, movie_timescale)
    times = _expected_times(spec, movie_timescale, origin)
    shown = sorted((n for n in range(len(ticks)) if ticks[n] >= 0), key=lambda n: (ticks[n], n))
    sync = {number - 1 for number in spec.sync} if spec.sync else set(range(len(ticks)))
    return {
        "frame_samples": shown,
        "frame_pts": [times[n] for n in shown],
        "keyframes": [position for position, n in enumerate(shown) if n in sync],
        "duration": max(times[n] + spec.deltas[n] / spec.timescale for n in shown),
    }


def _frames(index) -> dict:
    return {
        "frame_samples": list(index.frame_samples),
        "frame_pts": list(index.frame_pts),
        "keyframes": list(index.keyframes),
        "duration": index.duration,
    }


def _video(**fields) -> TrackSpec:
    """영상 재료 — timescale 1000, 크기 · 청크는 샘플 수에 맞춘다."""
    count = len(fields["deltas"])
    return TrackSpec(
        handler=b"vide", timescale=1000, sizes=[10] * count, chunks=[1] * count, **fields
    )


@pytest.mark.parametrize("seed", range(30))
def test_frames_equal_the_definition_when_parsed_in_small_chunks(seed):
    """조각을 4샘플로 줄여 해석해도 표시 순서 · 표시 시각 · 키프레임 · 길이가 정의와 같아야 한다.

    씨앗마다 무작위 영상 · 오디오(재정렬 · 빈 편집 · 편집 목록이 가리는 샘플)를 만들어 해석
    -> frame_samples · frame_pts · keyframes · duration == 정의 그대로 계산한 값
    """
    video, audio, movie_timescale = _random_specs(seed)
    origin = min(_start(video, movie_timescale), _start(audio, movie_timescale))

    index = parse_moov(build_mp4([video, audio], movie_timescale=movie_timescale).moov)

    assert _frames(index) == _expected_frames(video, movie_timescale, origin)


def test_frame_list_leaves_out_frames_the_edit_list_hides():
    """편집 목록이 앞 프레임을 가리면 표시 순서에서 그 프레임들이 빠져야 한다.

    영상 8프레임(길이 100틱, ctts 없음), elst media_time = 200 -> 샘플 0 · 1의 표시 시각이 음수
    -> frame_samples == (2 … 7), frame_pts[0] == 0.0
    """
    video = _video(deltas=[100] * 8, edits=[(800, 200)])

    index = parse_moov(build_mp4([video]).moov)

    assert list(index.frame_samples) == [2, 3, 4, 5, 6, 7]
    assert index.frame_pts[0] == 0.0


def test_video_start_is_the_earliest_frame_when_the_first_decoded_frame_shows_later():
    """디코드 순서의 첫 프레임이 늦게 표시되면 영상의 시작은 가장 이른 표시 시각이어야 한다.

    영상 4프레임(길이 100틱), ctts [300, 0, 0, 0] -> 표시 시각(틱) [300, 100, 200, 300]
    -> frame_samples == (1, 2, 0, 3), frame_pts == (0.0, 0.1, 0.2, 0.2)
    """
    video = _video(deltas=[100] * 4, composition=[300, 0, 0, 0])

    index = parse_moov(build_mp4([video]).moov)

    assert list(index.frame_samples) == [1, 2, 0, 3]
    assert list(index.frame_pts) == [0.0, 0.1, 0.2, 0.2]


def test_audio_start_with_ctts_is_its_earliest_sample_not_a_bisected_one():
    """ctts가 있어 표시 시각이 줄었다 느는 오디오면 오디오의 시작은 가장 이른 표시 시각이어야 한다.

    영상은 빈 편집 1초 뒤에 시작한다. 오디오 6샘플(8000Hz, 길이 1024틱), ctts [3000, 0, 0, 0, 0, 0]
    -> 표시 시각(틱) [3000, 1024, 2048, 3072, 4096, 5120], 가장 이른 것은 1024
    -> frame_pts[0] == 1 − 1024/8000초
    """
    video = _video(deltas=[100] * 4, edits=[(1000, -1), (400, 0)])
    audio = TrackSpec(
        handler=b"soun",
        timescale=8000,
        deltas=[1024] * 6,
        sizes=[7] * 6,
        chunks=[2, 2, 2],
        composition=[3000, 0, 0, 0, 0, 0],
    )

    index = parse_moov(build_mp4([video, audio]).moov)

    assert index.frame_pts[0] == float(Fraction(1) - Fraction(1024, 8000))


def test_audio_start_with_ctts_whose_earliest_sample_is_past_the_first_chunk_equals_one_pass(
    monkeypatch,
):
    """ctts 오디오의 가장 이른 표시 샘플이 둘째 조각 이후에 있으면 오디오의 시작은 한 번에 훑은 것과 같아야 한다.

    영상은 빈 편집 1초 뒤에 시작한다. 오디오 6샘플(8000Hz, 길이 1024틱), ctts [9000, 9000, 9000, 9000, 0, 0],
    조각 4샘플
    -> 표시 시각(틱) [9000, 10024, 11048, 12072, 4096, 5120], 가장 이른 것은 둘째 조각의 4096
    -> frame_pts[0] == 1 − 4096/8000초 == 조각 없이 해석한 frame_pts[0]
    """
    video = _video(deltas=[100] * 4, edits=[(1000, -1), (400, 0)])
    audio = TrackSpec(
        handler=b"soun",
        timescale=8000,
        deltas=[1024] * 6,
        sizes=[7] * 6,
        chunks=[2, 2, 2],
        composition=[9000, 9000, 9000, 9000, 0, 0],  # 첫 조각(샘플 0~3)은 모두 둘째 조각보다 늦다
    )
    moov = build_mp4([video, audio]).moov

    chunked = parse_moov(moov).frame_pts[0]
    monkeypatch.setattr(column_module, "CHUNK_ITEMS", 1 << 15)
    monkeypatch.setattr(mp4_module, "CHUNK_ITEMS", 1 << 15)
    one_pass = parse_moov(moov).frame_pts[0]

    assert chunked == float(Fraction(1) - Fraction(4096, 8000))
    assert chunked == one_pass


def test_audio_hidden_entirely_by_the_edit_list_leaves_the_start_to_video():
    """편집 목록이 오디오의 모든 샘플을 가리면 시각의 0은 영상의 첫 프레임이어야 한다.

    영상 4프레임(빈 편집 1초), 오디오 4샘플(8000Hz, 길이 1024틱, ctts 없음), 오디오 elst media_time = 8192
    -> frame_pts[0] == 0.0, 오디오 times는 모두 음수
    """
    video = _video(deltas=[100] * 4, edits=[(1000, -1), (400, 0)])
    audio = TrackSpec(
        handler=b"soun",
        timescale=8000,
        deltas=[1024] * 4,
        sizes=[7] * 4,
        chunks=[2, 2],
        edits=[(1000, 8192)],
    )

    index = parse_moov(build_mp4([video, audio]).moov)

    assert index.frame_pts[0] == 0.0
    assert all(time < 0 for time in index.audio.times)


def test_audio_decode_times_follow_stts_runs_of_different_lengths():
    """stts 구간마다 길이가 다른 오디오면 DTS와 샘플 길이가 구간을 샘플마다 편 정의와 같아야 한다.

    오디오 10샘플(48000Hz), 길이(틱) [1024 × 3, 960 × 2, 2048, 1024 × 4], elst media_time = 1024
    -> decode_times == (DTS − 1024) ÷ 48000, durations == 길이 ÷ 48000
    """
    deltas = [1024] * 3 + [960] * 2 + [2048] + [1024] * 4
    video = _video(deltas=[100] * 4)
    audio = TrackSpec(
        handler=b"soun",
        timescale=48000,
        deltas=deltas,
        sizes=[7] * 10,
        chunks=[3, 3, 4],
        edits=[(1000, 1024)],
    )
    decode = [0, *accumulate(deltas)][:10]
    origin = min(Fraction(0), Fraction(decode[1] - 1024, 48000))  # 영상 0초 · 오디오의 첫 표시 샘플

    index = parse_moov(build_mp4([video, audio]).moov)

    assert list(index.audio.decode_times) == [
        float(Fraction(dts - 1024, 48000) - origin) for dts in decode
    ]
    assert list(index.audio.durations) == [delta / 48000 for delta in deltas]


def test_duration_reaches_a_frame_shown_first_that_ends_last():
    """먼저 표시되는 프레임이 가장 늦게 끝나면 길이는 그 프레임이 끝나는 시각이어야 한다.

    영상 41프레임 — 샘플 0은 길이 5000틱 · ctts 4500, 나머지 40개는 길이 100틱 · ctts 0
    -> 샘플 0이 표시 순서의 맨 앞(4500틱)이고 9500틱에 끝난다. 나머지는 9000틱까지 끝난다
    -> duration == (9500 − 4500) ÷ 1000 == 5.0
    """
    video = _video(deltas=[5000] + [100] * 40, composition=[4500] + [0] * 40)

    index = parse_moov(build_mp4([video]).moov)

    assert index.frame_samples[0] == 0
    assert index.duration == 5.0


def test_sample_limits_fit_the_sample_number_array():
    """트랙의 샘플 상한은 샘플 번호를 담는 배열 종류의 최댓값보다 작아야 한다.

    샘플 번호는 0 … 상한 − 1이다
    -> max(_MAX_SAMPLES) − 1 <= 2 ** (8 × itemsize) − 1
    """
    width = array.array(mp4_module._SAMPLE_NUMBERS).itemsize * 8

    assert max(mp4_module._MAX_SAMPLES.values()) - 1 <= 2**width - 1
