"""구간 → 받을 바이트 범위 (#178, #309).

mp4 색인(``Mp4Index``)과 구간(``TimeRange``)으로, 그 구간을 잘라 내는 데 필요한
샘플이 파일의 어느 바이트에 있는지 계산한다. 받기 전에 구간의 크기를 알 수 있고,
받을 때는 이 범위만 요청하면 된다.

범위는 컷(``core.utils.hybrid_cut``)이 실제로 읽는 샘플을 모두 담는다 (#309).

- 시작: 구간의 첫 프레임이 아니라 **첫 프레임보다 조금 앞의 시각을 덮는 키프레임**
  부터다. 컷은 오디오를 구간 시작보다 앞에서 디코드하기 시작하고, 재인코딩 조각도
  시작 시각보다 조금 앞으로 탐색한다 — ffmpeg는 그 시각의 앞 키프레임으로 간다.
  첫 프레임이 키프레임이거나 키프레임 바로 뒤면 그 앞 GOP가 통째로 범위에 든다
- 영상은 그 키프레임부터 끝 프레임을 디코드하는 데 필요한 샘플까지, 오디오는 그
  키프레임의 DTS부터 끝 프레임이 끝나는 시각까지와 겹치는 샘플이다
- 양 끝을 **청크 단위**로 넓힌다. mp4는 청크의 위치만 적으므로(stco/co64), 청크의
  일부만 받으면 받은 범위만 이어 붙인 파일에서 그 청크의 위치를 적을 수 없다

범위는 **연속 범위 하나**로 돌려준다. 영상과 오디오 청크는 파일 안에서 약 0.5초
단위로 번갈아 놓여 있어, 필요한 샘플의 첫 바이트부터 마지막 바이트까지가 거의
빈틈없이 이어진다. 트랙별로 나누거나 샘플만 골라 받으면 요청이 2~3개로 늘 뿐
줄어드는 바이트가 거의 없다.
"""

from bisect import bisect_right
from collections.abc import Iterable

from core.models.mp4_index import Mp4Index, Mp4Track, SelectionBytes
from core.models.plan import TimeRange
from core.utils.hybrid_cut import SOURCE_LEAD_SECONDS
from core.utils.mp4_partial import plan_partial
from core.utils.timecode import snap_to_frame


def sections_download_size(index: Mp4Index, selections: Iterable[TimeRange]) -> int:
    """그 구간들을 받는 데 드는 바이트의 합 — 구간 다운로드가 받는 양이다 (#309).

    구간마다의 범위(``selection_byte_ranges``)를 합쳐 센다 — 겹치는 자리는 한 번만 받는다.
    file 다운로더가 같은 구간을 모두 받을 때의 전체 크기(``DownloadPlan.total_size``)와 같다.

    Raises:
        ValueError: 구간이 영상 밖인 경우(``selection_byte_ranges``)
        Mp4Error: moov가 샘플보다 뒤에 있는 등 부분 파일을 만들 수 없는 경우
    """
    spans = [
        span for selection in selections for span in selection_byte_ranges(index, selection).ranges
    ]
    return plan_partial(index, spans).download_size


def sections_head_size(index: Mp4Index) -> int:
    """구간 다운로드가 만드는 파일의 머리 길이(바이트) — ftyp · moov 등 첫 샘플 앞의 것이다 (#309).

    머리는 구간을 정할 때 이미 받아 두어 다시 받지 않는다(``sections_download_size``에 들지
    않는다). 받은 파일의 크기는 두 값의 합이다 — 구간이 영상 전체를 덮으면 원본의 크기와 같다.

    Raises:
        Mp4Error: moov가 샘플보다 뒤에 있는 등 부분 파일을 만들 수 없는 경우
    """
    return plan_partial(index, ()).head_size


# 오디오가 시작하는 시각을 이만큼 앞으로 잡는다(초) — 키프레임의 DTS가 오디오 샘플의
# 경계에 놓였을 때 반올림 방향에 따라 앞 샘플부터 읽힐 수 있다
_AUDIO_SEEK_SLACK = 0.001


def selection_byte_ranges(index: Mp4Index, selection: TimeRange) -> SelectionBytes:
    """구간을 잘라 내는 데 필요한 파일 바이트 범위를 구한다.

    구간의 시작·끝 시각은 ``snap_to_frame``으로 실제 프레임에 맞춘다(시작은 앞,
    끝은 뒤 규칙). 끝 프레임은 범위에 포함된다.

    Returns:
        ``ranges``에 범위 하나를 담은 ``SelectionBytes``. ``total_size``는 그 범위의
        바이트 수다. ``keyframe``은 범위가 시작하는 키프레임이다 — 첫 프레임의 바로 앞
        키프레임보다 하나 더 앞일 수 있다.
    """
    first_frame = snap_to_frame(selection.start, index.frame_pts, index.fps, "start")
    last_frame = max(first_frame, snap_to_frame(selection.end, index.frame_pts, index.fps, "end"))
    keyframe = _keyframe_covering(index, index.frame_pts[first_frame] - SOURCE_LEAD_SECONDS)

    video = index.video
    first_sample = index.frame_samples[keyframe]
    # B프레임은 표시 순서와 디코드 순서가 다르다 — 끝 프레임보다 먼저 표시되면서
    # 나중에 디코드되는 샘플이 있을 수 있어, 표시 범위 안의 가장 뒤 샘플까지 잡는다
    last_sample = max(index.frame_samples[keyframe : last_frame + 1])
    spans = [_chunk_span(video, first_sample, last_sample)]

    if index.audio is not None:
        audio = index.audio
        # ffmpeg는 영상의 키프레임을 찾은 뒤 다른 트랙을 그 키프레임의 DTS로 맞춘다 —
        # 오디오는 PTS가 아니라 DTS를 덮는 샘플부터 읽힌다(B프레임이 있으면 DTS가 앞선다)
        time_from = video.decode_times[first_sample] - _AUDIO_SEEK_SLACK
        time_to = index.frame_pts[last_frame] + video.durations[index.frame_samples[last_frame]]
        needed = [
            sample
            for sample, start in enumerate(audio.times)
            if start < time_to and start + audio.durations[sample] > time_from
        ]
        if needed:
            spans.append(_chunk_span(audio, needed[0], needed[-1]))

    begin = min(span[0] for span in spans)
    end = max(span[1] for span in spans)
    return SelectionBytes(
        ranges=((begin, end - 1),),
        total_size=end - begin,
        keyframe=keyframe,
        first_frame=first_frame,
        last_frame=last_frame,
    )


def _keyframe_covering(index: Mp4Index, seconds: float) -> int:
    """PTS가 seconds와 같거나 앞인 가장 가까운 키프레임의 프레임 번호. 없으면 첫 키프레임(없으면 0)."""
    times = [index.frame_pts[frame] for frame in index.keyframes]
    position = bisect_right(times, seconds)
    if position:
        return index.keyframes[position - 1]
    return index.keyframes[0] if index.keyframes else 0


def _chunk_span(track: Mp4Track, first_sample: int, last_sample: int) -> tuple[int, int]:
    """샘플 first~last가 든 청크들의 (첫 바이트, 마지막 바이트 + 1).

    청크 안의 샘플은 파일에 이어 붙어 놓인다 — 청크의 첫 샘플의 위치가 청크의 시작이고
    마지막 샘플의 끝이 청크의 끝이다.
    """
    starts = track.chunk_starts
    first_chunk = bisect_right(starts, first_sample) - 1
    last_chunk = bisect_right(starts, last_sample) - 1
    begin_sample = starts[first_chunk]
    end_sample = starts[last_chunk + 1] if last_chunk + 1 < len(starts) else len(track.offsets)
    return track.offsets[begin_sample], track.offsets[end_sample - 1] + track.sizes[end_sample - 1]
