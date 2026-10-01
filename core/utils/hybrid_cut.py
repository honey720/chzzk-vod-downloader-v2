"""하이브리드 컷 — 로컬 파일에서 구간 하나를 프레임 단위로 잘라 파일 하나로 만든다 (#309).

키프레임이 아닌 프레임에서 시작하거나 끝나는 구간은 복사만으로 자를 수 없다.
그렇다고 전체를 재인코딩하면 느리고 화질이 떨어진다. 그래서 양 끝만 재인코딩한다.

- 머리: 구간 시작부터 다음 키프레임 직전까지 — 재인코딩(libx264)
- 가운데: 그 키프레임부터 마지막 키프레임 직전까지 — 원본 복사
- 꼬리: 마지막 키프레임부터 구간 끝까지 — 재인코딩
- 오디오: 구간 전체를 한 번에 재인코딩해 영상과 합친다

조각을 이어 붙였을 때 플레이어가 끊김 없이 재생하려면 재인코딩 조각이 원본과
같은 모양이어야 한다. 실제 VOD로 확인한 조건이다.

- 프로파일·레벨·화소 형식·**부호화 크기와 크롭**을 원본 SPS에 맞춘다. 부호화
  크기가 다르면 조각이 바뀌는 곳에서 화면이 깜빡이는 플레이어가 있다
- **B프레임 재정렬 지연**을 원본과 맞춘다. 다르면 이음매에서 DTS가 뒤로 간다
- 가운데 조각의 탐색은 **키프레임 시각에 정확히** 둔다. 조금이라도 앞에 두면
  앞 GOP가 통째로 딸려 오고 편집 목록이 그것을 가린다
- 가운데 조각의 길이는 **DTS**로 준다. 복사에서는 -t가 DTS로 끊긴다
- 탐색은 입력측 -ss, 길이는 출력측 -t로 준다. 입력측 -ss와 절대 시각 -to를
  함께 쓰면 길이가 어긋난다
- 타임스탬프는 원본 그대로 넘긴다(passthrough). 프레임 간격이 고르지 않은 원본이 있다

프레임 시각·키프레임·DTS는 계산하지 않고 색인에서 읽은 값(``CutFrames``)을 쓴다.
입력은 mp4와 fMP4(초기화 세그먼트 + 미디어 세그먼트를 이은 파일)다. MPEG-TS는
아직 다루지 않으며 ``CutError``로 거부한다.

실패 키는 번역하지 않은 i18n 키 원문이며 번역은 앱 계층이 한다
(``MetadataError``와 같은 방식).
"""

import os
import re
import shutil
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Sequence

from core.models.cut import (
    CutFrames,
    CutPiece,
    CutPlan,
    CutResult,
    PieceInfo,
    SourceInfo,
    VideoParams,
)
from core.models.fmp4_index import Fmp4Index, Fmp4Init, Fmp4Segment
from core.models.mp4_index import Mp4Index
from core.utils.ffmpeg import FFmpegError, FFmpegTimeoutError, run_ffmpeg
from core.utils.paths import cut_temp_dir_for

# 실패 키 — 번역하지 않은 i18n 키 원문
CUT_FAILED = "Failed to cut the video"  # ffmpeg가 실패했거나 결과가 어긋났다
CUT_TIMEOUT = "Cutting the video timed out"  # ffmpeg가 제한 시간 안에 끝나지 않았다
CUT_UNSUPPORTED = "This video cannot be cut"  # MPEG-TS · H.264가 아닌 코덱 · 맞출 수 없는 파라미터

# 탐색·길이 경계에 두는 여유(초)의 상한 — 4ms. ffmpeg는 시각을 마이크로초로 받으므로
# 프레임 경계에 딱 맞추면 반올림 방향에 따라 한 프레임이 더 들어오거나 빠진다.
# 실제 여유는 이 값과 프레임 길이의 1/4 중 작은 쪽이다
_MARGIN_SECONDS = 0.004

# 오디오 디코드를 구간 시작보다 이만큼 앞에서 시작한다(초). AAC는 앞 프레임과 겹쳐서
# 복원하므로 첫 프레임만 따로 디코드하면 앞부분이 깨끗하지 않다. AAC 한 프레임은
# 44.1kHz에서 23ms다 — 두 프레임이 들어가는 50ms로 잡는다
_AUDIO_PREROLL_SECONDS = 0.05

# 재인코딩 화질 — libx264 CRF. 18은 눈으로 구분하기 어려운 수준이다
_CRF = "18"

# 서브프로세스 제한 시간(초)
_PROBE_TIMEOUT = 60  # 머리만 읽는다
_ENCODE_TIMEOUT = 600  # 머리·꼬리·구간 전체 재인코딩 — 길어야 GOP 하나(수 초 분량)
_COPY_TIMEOUT = 3600  # 가운데 복사·오디오·합치기 — 구간 길이만큼 읽고 쓴다

_TS_PACKET_SIZE = 188  # MPEG-TS 패킷 길이 — 입력이 TS인지 가릴 때 쓴다
_TS_SYNC_BYTE = 0x47

_X264_PROFILES = {66: "baseline", 77: "main", 100: "high"}  # profile_idc → -profile:v 값
_HIDDEN_PACKET = 0x4  # framecrc의 F= 값에서 편집 목록이 가린 패킷을 뜻하는 비트

# aspect_ratio_idc → 화소 가로세로비 (H.264 표 E-1). 표에 없는 번호는 "명시 안 됨"으로 읽는다
_SAR_BY_IDC = {
    1: (1, 1), 2: (12, 11), 3: (10, 11), 4: (16, 11), 5: (40, 33), 6: (24, 11), 7: (20, 11),
    8: (32, 11), 9: (80, 33), 10: (18, 11), 11: (15, 11), 12: (64, 33), 13: (160, 99),
    14: (4, 3), 15: (3, 2), 16: (2, 1),
}  # fmt: skip
_EXTENDED_SAR = 255  # 비율을 sar_width · sar_height로 직접 적는 번호

_SPS_FIELD = re.compile(r"\]\s+\d+\s+(\w+)\s+[01]+ = (-?\d+)")


class CutError(Exception):
    """구간을 잘라 내지 못했다.

    message_key는 번역하지 않은 i18n 키 원문이다(이 모듈의 ``CUT_*`` 상수).
    """

    def __init__(self, message_key: str, detail: str = ""):
        super().__init__(f"{message_key}: {detail}" if detail else message_key)
        self.message_key = message_key


# ================================================================ 프레임 정보


def cut_frames_from_mp4(index: Mp4Index) -> CutFrames:
    """mp4 색인에서 컷에 필요한 프레임 정보를 뽑는다."""
    video = index.video
    audio = index.audio
    shown = [n for n, time in enumerate(audio.times) if time >= 0] if audio else []
    return CutFrames(
        frame_pts=index.frame_pts,
        frame_dts=tuple(video.decode_times[sample] for sample in index.frame_samples),
        keyframes=index.keyframes,
        timescale=video.timescale,
        frame_duration=float(1 / index.fps),
        audio_start=min(audio.times[n] for n in shown) if shown else None,
        audio_end=max(audio.times[n] + audio.durations[n] for n in shown) if shown else None,
    )


def cut_frames_from_fmp4(
    init: Fmp4Init, segments: Sequence[Fmp4Segment], index: Fmp4Index, input_start: float = 0.0
) -> CutFrames:
    """fMP4 색인에서 컷에 필요한 프레임 정보를 뽑는다.

    Args:
        init: 초기화 세그먼트
        segments: ``index``를 만든 미디어 세그먼트(같은 순서). 입력 파일에 든 세그먼트다
        index: ``build_fmp4_index``의 결과
        input_start: 입력 파일이 시작하는 시각(VOD 기준 초). VOD의 일부 세그먼트만
            이어 붙인 입력이면 그 첫 세그먼트의 가장 이른 시각이다
    """
    durations = Counter(d for segment in segments for d in segment.video.durations)
    audio_end = None
    if init.audio is not None and index.audio_pts:
        last = next(s.audio.durations[-1] for s in reversed(segments) if s.audio.durations)
        audio_end = index.audio_pts[-1] + last / init.audio.timescale
    return CutFrames(
        frame_pts=index.frame_pts,
        frame_dts=tuple(index.decode_times[sample] for sample in index.frame_samples),
        keyframes=index.keyframes,
        timescale=init.video.timescale,
        frame_duration=durations.most_common(1)[0][0] / init.video.timescale,
        audio_start=min(index.audio_pts) if index.audio_pts else None,
        audio_end=audio_end,
        input_start=input_start,
    )


# ================================================================ 계획


def plan_cut(frames: CutFrames, first: int, last: int) -> CutPlan:
    """구간 [first, last]를 어떤 조각으로 자를지 정한다. 두 프레임 모두 구간에 든다.

    - 시작이 키프레임이면 머리를 만들지 않는다 — 그 프레임부터 복사할 수 있다
    - 시작과 끝이 같은 GOP 안이면(사이에 복사할 키프레임이 없으면) 구간 전체를
      재인코딩하는 조각 하나(whole)로 한다
    - 꼬리는 항상 만든다. 끝 프레임이 GOP의 마지막이 아니면 복사로는 거기서 끊을 수 없다

    Raises:
        ValueError: 프레임 번호가 범위를 벗어났거나, first보다 앞(또는 같은) 키프레임이
            없어 first를 디코드할 수 없는 경우
    """
    count = len(frames.frame_pts)
    if not 0 <= first <= last < count:
        raise ValueError(f"구간 [{first}, {last}]이 프레임 범위 [0, {count - 1}]를 벗어난다")
    keys = frames.keyframes
    if not keys or keys[0] > first:
        raise ValueError(f"프레임 {first}보다 앞에 키프레임이 없다")

    starts_on_key = (
        keys[bisect_left(keys, first)] == first if bisect_left(keys, first) < len(keys) else False
    )
    after = bisect_right(keys, first)
    next_key = first if starts_on_key else (keys[after] if after < len(keys) else None)
    last_key = keys[bisect_right(keys, last) - 1]  # last와 같거나 앞의 마지막 키프레임

    if next_key is None or next_key > last or (starts_on_key and last_key == first):
        return CutPlan(first, last, (CutPiece("whole", first, last + 1),))
    pieces = []
    if not starts_on_key:
        pieces.append(CutPiece("head", first, next_key))
    if last_key > next_key:
        pieces.append(CutPiece("mid", next_key, last_key))
    pieces.append(CutPiece("tail", last_key, last + 1))
    return CutPlan(first, last, tuple(pieces))


# ================================================================ 실행


def hybrid_cut(
    source_path: str,
    frames: CutFrames,
    first: int,
    last: int,
    output_path: str,
    *,
    inspect: bool = False,
) -> CutResult:
    """입력 파일에서 프레임 [first, last]를 잘라 output_path에 mp4로 쓴다.

    중간 파일은 산출물 옆의 임시 폴더에 두고, 성공하든 실패하든 지운다. 실패하면
    쓰다 만 산출물도 지운다.

    Args:
        source_path: ffmpeg가 읽을 수 있는 로컬 mp4 또는 fMP4 파일
        frames: 그 파일의 프레임 정보 — ``cut_frames_from_mp4`` · ``cut_frames_from_fmp4``
        first: 구간의 첫 프레임 번호(``frames.frame_pts``의 인덱스)
        last: 구간의 끝 프레임 번호(포함)
        output_path: 만들 파일. 이미 있으면 덮어쓴다
        inspect: True면 조각마다 파라미터·패킷 수를 읽어 결과에 싣는다 —
            ``core.utils.cut_check.check_cut``이 쓴다. 조각을 한 번씩 더 읽으므로 느리다

    Raises:
        CutError: 입력을 다룰 수 없는 경우(``CUT_UNSUPPORTED``), ffmpeg가 실패한
            경우(``CUT_FAILED``), 제한 시간을 넘긴 경우(``CUT_TIMEOUT``)
        ValueError: 프레임 번호가 범위를 벗어난 경우
    """
    _reject_transport_stream(source_path)
    plan = plan_cut(frames, first, last)
    source = _probe_source(source_path, frames, plan)
    source_path = os.path.abspath(source_path)
    output_path = os.path.abspath(output_path)

    work = cut_temp_dir_for(output_path)
    shutil.rmtree(work, ignore_errors=True)  # 이전 실행이 죽으면서 남긴 것
    os.makedirs(work)
    try:
        names = []
        inspected = []
        for number, piece in enumerate(plan.pieces):
            name = f"{number}_{piece.kind}.mp4"
            if piece.reencoded:
                _run(
                    _encode_command(source_path, frames, piece, source.video, name),
                    _ENCODE_TIMEOUT,
                    work,
                )
            else:
                _run(_copy_command(source_path, frames, piece, name), _COPY_TIMEOUT, work)
            names.append(name)
            if inspect:
                inspected.append(_inspect_piece(os.path.join(work, name), piece, frames))

        has_audio = frames.audio_start is not None
        if has_audio:
            _run(
                _audio_command(source_path, frames, plan, source.audio_bitrate), _COPY_TIMEOUT, work
            )
        stderr = _run(
            _mux_command(frames, plan, names, has_audio, output_path, work), _COPY_TIMEOUT, work
        )
        if "Non-monotonic DTS" in stderr:
            raise CutError(CUT_FAILED, "조각을 잇는 곳에서 DTS가 뒤로 간다")
    except BaseException:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return CutResult(output_path=output_path, plan=plan, source=source, pieces=tuple(inspected))


def _run(args: list[str], timeout: float, cwd: str) -> str:
    """ffmpeg를 실행하고 stderr를 돌려준다. 실패·시간 초과는 CutError로 바꾼다."""
    try:
        done = run_ffmpeg(["-v", "warning", "-y", *args], timeout=timeout, cwd=cwd)
    except FFmpegTimeoutError as e:
        raise CutError(CUT_TIMEOUT, str(e)) from e
    except FFmpegError as e:
        raise CutError(CUT_FAILED, str(e)) from e
    if done.returncode != 0:
        raise CutError(CUT_FAILED, f"exit {done.returncode}: {done.stderr.strip()[-500:]}")
    return done.stderr


def _seconds(value: float) -> str:
    """ffmpeg에 넘길 시각 — 마이크로초까지."""
    return f"{value:.6f}"


def _margin(frames: CutFrames) -> float:
    return min(_MARGIN_SECONDS, frames.frame_duration / 4)


def _start_of(frames: CutFrames, frame: int) -> float:
    """프레임이 시작하는 시각 — 입력 파일의 시작부터 센 초."""
    return frames.frame_pts[frame] - frames.input_start


def _end_of(frames: CutFrames, end: int) -> float:
    """프레임 번호 end 직전까지의 구간이 끝나는 시각 — 입력 파일의 시작부터 센 초."""
    if end < len(frames.frame_pts):
        return _start_of(frames, end)
    return _start_of(frames, end - 1) + frames.frame_duration


def _encode_command(
    source_path: str, frames: CutFrames, piece: CutPiece, video: VideoParams, name: str
) -> list[str]:
    """재인코딩 조각 — 앞 키프레임부터 디코드해 piece.first 이전을 버리고 piece.end 직전까지."""
    margin = _margin(frames)
    seek = max(_start_of(frames, piece.first) - margin, 0.0)
    length = _end_of(frames, piece.end) - seek - margin
    return [
        *(["-ss", _seconds(seek)] if seek > 0 else []),
        "-i", source_path,
        "-t", _seconds(length),
        "-map", "0:v:0", "-an",
        *_x264_args(video, frames.timescale),
        "-f", "mp4", name,
    ]  # fmt: skip


def _copy_command(source_path: str, frames: CutFrames, piece: CutPiece, name: str) -> list[str]:
    """복사 조각 — piece.first(키프레임)부터 piece.end(키프레임) 직전 패킷까지."""
    seek = _start_of(frames, piece.first)  # 여유 없이 키프레임 시각 그대로
    # 복사는 DTS로 끊긴다. 끝 키프레임 패킷과, 디코드 순서로 그 직전 패킷의 DTS 사이에 둔다
    decode_order = sorted(frames.frame_dts)
    end_dts = frames.frame_dts[piece.end]
    previous_dts = decode_order[bisect_left(decode_order, end_dts) - 1]
    stop = (end_dts + previous_dts) / 2 - frames.input_start
    return [
        *(["-ss", _seconds(seek)] if seek > 0 else []),
        "-i", source_path,
        "-t", _seconds(stop - max(seek, 0.0)),
        "-map", "0:v:0", "-an", "-c:v", "copy",
        "-video_track_timescale", str(frames.timescale),
        "-f", "mp4", name,
    ]  # fmt: skip


def _audio_command(
    source_path: str, frames: CutFrames, plan: CutPlan, bitrate: int | None
) -> list[str]:
    """오디오 — 구간 전체를 한 번에 재인코딩한다. 디코드는 구간 시작보다 조금 앞에서 시작한다."""
    start = _start_of(frames, plan.first)
    length = _end_of(frames, plan.last + 1) - start
    preroll = min(_AUDIO_PREROLL_SECONDS, max(start, 0.0))
    seek = start - preroll
    return [
        *(["-ss", _seconds(seek)] if seek > 0 else []),
        "-i", source_path,
        *(["-ss", _seconds(preroll)] if preroll > 0 else []),  # 앞 여유는 디코드만 하고 버린다
        "-t", _seconds(length),
        "-map", "0:a:0", "-vn", "-c:a", "aac",
        *(["-b:a", f"{bitrate}k"] if bitrate else []),
        "-f", "mp4", "audio.m4a",
    ]  # fmt: skip


def _mux_command(
    frames: CutFrames, plan: CutPlan, names: list[str], has_audio: bool, output_path: str, work: str
) -> list[str]:
    """조각을 잇고 오디오를 합친다. 조각의 길이는 원본 시각의 차이로 명시한다."""
    if len(names) == 1:
        video_input = ["-i", names[0]]
    else:
        lines = ["ffconcat version 1.0"]
        for number, (name, piece) in enumerate(zip(names, plan.pieces)):
            lines.append(f"file {name}")
            if number + 1 < len(names):
                span = frames.frame_pts[piece.end] - frames.frame_pts[piece.first]
                lines.append(f"duration {_seconds(span)}")
        with open(os.path.join(work, "list.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        video_input = ["-f", "concat", "-safe", "0", "-i", "list.txt"]
    return [
        *video_input,
        *(["-i", "audio.m4a"] if has_audio else []),
        "-map", "0:v:0",
        *(["-map", "1:a:0"] if has_audio else []),
        "-c", "copy",
        "-video_track_timescale", str(frames.timescale),
        "-f", "mp4", output_path,
    ]  # fmt: skip


def _x264_args(video: VideoParams, timescale: int) -> list[str]:
    """원본 SPS에 맞춘 libx264 인코딩 옵션."""
    display_width = video.coded_width - video.crop[2]
    display_height = video.coded_height - video.crop[3]
    params = ["b-pyramid=" + ("normal" if video.reorder_delay == 2 else "none")]
    filters: list[str] = []
    if (video.coded_width, video.coded_height) != (_ceil16(display_width), _ceil16(display_height)):
        # x264는 화면 크기를 16의 배수로만 올림한다. 원본이 그보다 크게 부호화했으면
        # 화면을 원본의 부호화 크기까지 늘리고(가장자리 화소를 이어 붙인다) 늘린 만큼 크롭을 적는다
        right, bottom = video.crop[2], video.crop[3]
        borders = ":".join(
            f"{side}={size}" for side, size in (("right", right), ("bottom", bottom)) if size
        )
        filters = [
            "-vf",
            f"pad={video.coded_width}:{video.coded_height}:0:0,fillborders={borders}:mode=smear",
        ]
        params.append(f"crop-rect=0,0,{right},{bottom}")
    return [
        *filters,
        "-c:v", "libx264",
        "-profile:v", _X264_PROFILES[video.profile_idc],
        "-level:v", f"{video.level_idc / 10:.1f}",
        "-pix_fmt", "yuv420p",
        "-preset", "medium", "-crf", _CRF,
        "-bf", "0" if video.reorder_delay == 0 else "3",
        "-x264-params", ":".join(params),
        "-fps_mode", "passthrough",
        "-enc_time_base", f"1:{timescale}",
        "-video_track_timescale", str(timescale),
    ]  # fmt: skip


def _ceil16(value: int) -> int:
    return -(-value // 16) * 16


# ================================================================ 읽기


def _reject_transport_stream(path: str) -> None:
    """입력이 MPEG-TS면 거부한다 — 첫 두 패킷의 동기 바이트로 가린다."""
    try:
        with open(path, "rb") as f:
            head = f.read(_TS_PACKET_SIZE + 1)
    except OSError as e:
        raise CutError(CUT_FAILED, f"입력 파일을 열 수 없다: {e}") from e
    if len(head) > _TS_PACKET_SIZE and head[0] == head[_TS_PACKET_SIZE] == _TS_SYNC_BYTE:
        raise CutError(CUT_UNSUPPORTED, "MPEG-TS 입력은 아직 다루지 않는다")


def _probe_source(path: str, frames: CutFrames, plan: CutPlan) -> SourceInfo:
    """입력의 SPS와 오디오 비트레이트를 읽고, 재인코딩으로 맞출 수 있는 모양인지 확인한다."""
    key = frames.keyframes[bisect_right(frames.keyframes, plan.first) - 1]
    delay = round((frames.frame_pts[key] - frames.frame_dts[key]) / frames.frame_duration)
    video, audio_bitrate = _read_params(path, delay)
    problems = []
    if video.profile_idc not in _X264_PROFILES:
        problems.append(f"프로파일 {video.profile_idc}")
    if (video.chroma_format_idc, video.bit_depth) != (1, 8):
        problems.append(f"화소 형식 {video.chroma_format_idc}/{video.bit_depth}비트")
    if video.crop[0] or video.crop[1]:
        problems.append(f"왼쪽·위 크롭 {video.crop[:2]}")
    if not 0 <= delay <= 2:
        problems.append(f"재정렬 지연 {delay}프레임")
    if problems:
        raise CutError(CUT_UNSUPPORTED, "맞출 수 없는 영상: " + ", ".join(problems))
    return SourceInfo(video=video, audio_bitrate=audio_bitrate)


def _read_params(path: str, reorder_delay: int) -> tuple[VideoParams, int | None]:
    """ffmpeg의 trace_headers 출력에서 첫 SPS를 읽는다. 동봉 ffmpeg에는 ffprobe가 없다."""
    try:
        done = run_ffmpeg(
            ["-v", "info", "-i", path, "-map", "0:v:0", "-c", "copy", "-frames:v", "1",
             "-bsf:v", "trace_headers", "-f", "null", "-"],
            timeout=_PROBE_TIMEOUT,
        )  # fmt: skip
    except FFmpegTimeoutError as e:
        raise CutError(CUT_TIMEOUT, str(e)) from e
    except FFmpegError as e:
        raise CutError(CUT_FAILED, str(e)) from e
    text = done.stderr
    codec = re.search(r"Stream #0:\d+.*?: Video: (\w+)", text)
    if done.returncode != 0 or codec is None:
        raise CutError(CUT_FAILED, f"입력을 읽지 못했다: {text.strip()[-300:]}")
    if codec.group(1) != "h264":
        raise CutError(CUT_UNSUPPORTED, f"코덱 {codec.group(1)}")

    fields: dict[str, int] = {}
    in_sps = False
    for line in text.splitlines():
        if "Sequence Parameter Set" in line:
            if fields:
                break
            in_sps = True
        elif "Picture Parameter Set" in line:
            if fields:
                break
        elif in_sps:
            match = _SPS_FIELD.search(line)
            if match:
                fields.setdefault(match.group(1), int(match.group(2)))
    if "pic_width_in_mbs_minus1" not in fields:
        raise CutError(CUT_UNSUPPORTED, "SPS를 읽지 못했다")
    if not fields.get("frame_mbs_only_flag", 1):
        raise CutError(CUT_UNSUPPORTED, "인터레이스 영상")

    ratio = fields.get("aspect_ratio_idc", 0)
    sar = (
        (fields.get("sar_width", 0), fields.get("sar_height", 0))
        if ratio == _EXTENDED_SAR
        else _SAR_BY_IDC.get(ratio, (0, 0))
    )
    video = VideoParams(
        profile_idc=fields["profile_idc"],
        level_idc=fields["level_idc"],
        chroma_format_idc=fields.get("chroma_format_idc", 1),
        bit_depth=8 + fields.get("bit_depth_luma_minus8", 0),
        coded_width=(fields["pic_width_in_mbs_minus1"] + 1) * 16,
        coded_height=(fields["pic_height_in_map_units_minus1"] + 1) * 16,
        # 4:2:0 프레임 부호화에서 크롭 단위는 2픽셀이다
        crop=tuple(
            2 * fields.get(f"frame_crop_{side}_offset", 0)
            for side in ("left", "top", "right", "bottom")
        ),
        sar=sar,
        reorder_delay=reorder_delay,
    )
    bitrate = re.search(r"Audio: .*?, (\d+) kb/s", text)
    return video, int(bitrate.group(1)) if bitrate else None


def read_packets(
    path: str, stream: str = "v:0", extra: Sequence[str] = ()
) -> tuple[int, list[tuple[int, int, int, int]]]:
    """파일의 패킷을 디코드 없이 읽는다 — (timebase 분모, [(dts, pts, 길이, 플래그)]).

    플래그는 framecrc의 F= 값이다(1 = 키프레임, 4 = 편집 목록이 가린 패킷). 값이 없는
    줄은 키프레임이다.

    Raises:
        CutError: ffmpeg가 실패했거나 패킷을 하나도 읽지 못한 경우(``CUT_FAILED``)
    """
    try:
        done = run_ffmpeg(
            ["-v", "error", *extra, "-i", path, "-map", f"0:{stream}", "-c", "copy", "-f", "framecrc", "-"],
            timeout=_COPY_TIMEOUT,
        )  # fmt: skip
    except FFmpegTimeoutError as e:
        raise CutError(CUT_TIMEOUT, str(e)) from e
    except FFmpegError as e:
        raise CutError(CUT_FAILED, str(e)) from e
    denominator = 0
    packets = []
    for line in done.stdout.splitlines():
        if line.startswith("#tb"):
            denominator = int(line.split("/")[1])
        elif not line.startswith("#"):
            cells = [cell.strip() for cell in line.split(",")]
            flags = next((int(cell[2:], 16) for cell in cells[6:] if cell.startswith("F=")), 1)
            packets.append((int(cells[1]), int(cells[2]), int(cells[3]), flags))
    if done.returncode != 0 or not denominator or not packets:
        raise CutError(CUT_FAILED, f"패킷을 읽지 못했다: {done.stderr.strip()[-300:]}")
    return denominator, packets


def _inspect_piece(path: str, piece: CutPiece, frames: CutFrames) -> PieceInfo:
    """조각 파일의 파라미터와 패킷 수를 읽는다."""
    timescale, packets = read_packets(path)
    shown = [p for p in packets if not p[3] & _HIDDEN_PACKET]
    first_key = next((p for p in shown if p[3] & 1), shown[0] if shown else packets[0])
    frame_ticks = frames.frame_duration * timescale
    video, _bitrate = _read_params(path, round((first_key[1] - first_key[0]) / frame_ticks))
    return PieceInfo(
        piece=piece,
        video=video,
        timescale=timescale,
        packets=len(packets),
        hidden_packets=len(packets) - len(shown),
    )


def expected_end(frames: CutFrames, plan: CutPlan) -> float:
    """구간이 끝나는 시각(VOD 기준 초) — 끝 프레임 다음 프레임의 PTS, 마지막 프레임이면 한 프레임 뒤."""
    return _end_of(frames, plan.last + 1) + frames.input_start


def frame_ticks(frames: CutFrames, frame: int, base: int, timescale: int) -> int:
    """프레임 frame의 PTS를 프레임 base 기준의 틱으로 바꾼다."""
    return round((frames.frame_pts[frame] - frames.frame_pts[base]) * timescale)
