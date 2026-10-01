"""구간 → 받을 바이트 범위 (#178).

mp4 색인(``Mp4Index``)과 구간(``TimeRange``)으로, 그 구간을 잘라 내는 데 필요한
샘플이 파일의 어느 바이트에 있는지 계산한다. 받기 전에 구간의 크기를 알 수 있고,
받을 때는 이 범위만 요청하면 된다.

영상은 구간의 첫 프레임을 디코드하려면 그 앞의 키프레임부터 필요하다. 오디오는
영상과 같은 시각 범위(키프레임부터 끝 프레임이 끝날 때까지)의 샘플을 받는다.

범위는 **연속 범위 하나**로 돌려준다. 영상과 오디오 청크는 파일 안에서 약 0.5초
단위로 번갈아 놓여 있어, 필요한 샘플의 첫 바이트부터 마지막 바이트까지가 거의
빈틈없이 이어진다. 실제 파일에서 재면 필요 없는 바이트는 양 끝의 오디오 청크
일부뿐이고 범위의 0.2% 이하다. 트랙별로 나누거나 샘플만 골라 받으면 요청이 2~3개로
늘 뿐 줄어드는 바이트가 거의 없다.
"""

from bisect import bisect_right

from core.models.mp4_index import Mp4Index, SelectionBytes
from core.models.plan import TimeRange
from core.utils.timecode import snap_to_frame


def selection_byte_ranges(index: Mp4Index, selection: TimeRange) -> SelectionBytes:
    """구간을 잘라 내는 데 필요한 파일 바이트 범위를 구한다.

    구간의 시작·끝 시각은 ``snap_to_frame``으로 실제 프레임에 맞춘다(시작은 앞,
    끝은 뒤 규칙). 끝 프레임은 범위에 포함된다.

    영상은 첫 프레임 앞(또는 같은) 키프레임부터, 끝 프레임까지 표시되는 모든
    프레임을 디코드하는 데 필요한 샘플까지다 — 디코드 순서로 이어진 한 덩어리다.
    오디오는 그 키프레임의 시각부터 끝 프레임이 끝나는 시각까지와 겹치는 샘플이다.

    Returns:
        ``ranges``에 범위 하나를 담은 ``SelectionBytes``. ``total_size``는 그 범위의
        바이트 수다.
    """
    first_frame = snap_to_frame(selection.start, index.frame_pts, index.fps, "start")
    last_frame = max(first_frame, snap_to_frame(selection.end, index.frame_pts, index.fps, "end"))
    keyframe = _keyframe_at_or_before(index, first_frame)

    video = index.video
    first_sample = index.frame_samples[keyframe]
    # B프레임은 표시 순서와 디코드 순서가 다르다 — 끝 프레임보다 먼저 표시되면서
    # 나중에 디코드되는 샘플이 있을 수 있어, 표시 범위 안의 가장 뒤 샘플까지 잡는다
    last_sample = max(index.frame_samples[keyframe : last_frame + 1])
    spans = [
        (video.offsets[sample], video.offsets[sample] + video.sizes[sample])
        for sample in range(first_sample, last_sample + 1)
    ]

    if index.audio is not None:
        audio = index.audio
        time_from = index.frame_pts[keyframe]
        time_to = index.frame_pts[last_frame] + video.durations[index.frame_samples[last_frame]]
        spans += [
            (audio.offsets[sample], audio.offsets[sample] + audio.sizes[sample])
            for sample, start in enumerate(audio.times)
            if start < time_to and start + audio.durations[sample] > time_from
        ]

    begin = min(span[0] for span in spans)
    end = max(span[1] for span in spans)
    return SelectionBytes(
        ranges=((begin, end - 1),),
        total_size=end - begin,
        keyframe=keyframe,
        first_frame=first_frame,
        last_frame=last_frame,
    )


def _keyframe_at_or_before(index: Mp4Index, frame: int) -> int:
    """frame과 같거나 앞에 있는 가장 가까운 키프레임의 프레임 번호. 없으면 0."""
    position = bisect_right(index.keyframes, frame)
    return index.keyframes[position - 1] if position else 0
