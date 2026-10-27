"""대조용 하이브리드 컷 — 입력 파일 하나에서 구간을 잘라 파일 하나로 만드는 옛 방식 (#309).

구간 파일을 직접 쓰기 전에 제품이 쓰던 절차다(``core/utils/hybrid_cut.py``에 있었다).

- 머리 · 꼬리: 재인코딩
- 가운데: 원본 복사 조각
- 오디오: 구간 전체를 한 번에 재인코딩
- 조각을 잇고 오디오를 합친다

제품은 이 절차를 더 쓰지 않는다 — mp4 구간은 받으면서 구간 파일을 채우고, 세그먼트 구간은
세그먼트에서 바로 구간 파일을 쓴다. 새 방식이 만든 구간 파일의 영상 패킷 · DTS · PTS를 견주는
대조로 테스트에 남긴다. 함수의 몸은 옮겨 온 그대로다.

재인코딩 명령 · 복사 명령 · 입력의 SPS 읽기 · ffmpeg 실행 · 진행 합치기는 제품의 것
(``core.utils.hybrid_cut``)을 그대로 부른다 — 여기 있는 것은 조각을 나누어 만들고 잇는 절차와
오디오를 다시 인코딩하는 부분이다.
"""

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass

from core.models.cut import CutFrames, CutPlan, CutResult, SourceInfo
from core.utils import hybrid_cut as hc
from core.utils.hybrid_cut import CUT_FAILED, CutError, plan_cut
from core.utils.paths import cut_temp_dir_for

# 오디오 디코드를 구간 시작보다 이만큼 앞에서 시작한다(초) — 제품의 받을 범위 계산이 쓰는 값과 같다
_AUDIO_PREROLL_SECONDS = hc._AUDIO_PREROLL_SECONDS

# 오디오 비트레이트를 맞추는 단위(kb/s). 컨테이너에 적힌 값은 인코더의 설정값이 아니라
# 잰 평균일 수 있다(표본에 191,999bit/s가 있었다). AAC가 흔히 쓰는 값은 이 단위의 배수다
_AUDIO_BITRATE_STEP = 16

# 원본의 오디오 비트레이트를 알 수 없을 때 쓰는 값(kb/s)
_DEFAULT_AUDIO_BITRATE = 192

# 컨테이너에 적힌 값으로 받아들이는 범위(kb/s). 벗어나면 손상된 값으로 보고 위 기본값을 쓴다
_AUDIO_BITRATE_RANGE = (8, 512)

_WORK_COPY_PER_SECOND = 0.002  # 복사 조각 — 구간 길이 1초에
_WORK_AUDIO_PER_SECOND = 0.015  # 오디오 재인코딩 — 구간 길이 1초에


@dataclass(frozen=True)
class ReferenceSource(SourceInfo):
    """입력에서 읽은 정보에 오디오를 다시 인코딩한 비트레이트를 더한 것."""

    audio_bitrate: int | None = (
        None  # 오디오를 다시 인코딩하는 비트레이트(kb/s). 오디오가 없으면 None
    )


def hybrid_cut(
    source_path: str,
    frames: CutFrames,
    first: int,
    last: int,
    output_path: str,
    *,
    inspect: bool = False,
    on_stage: Callable[[str, float], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
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
        on_stage: 단계 하나가 끝날 때마다(실패로 끝나도) ``(단계 이름, 걸린 초)``로 부른다.
            단계는 순서대로 ``probe``(입력의 SPS 읽기) · 조각마다 ``<번호>_<종류>``(재인코딩
            또는 복사) · ``audio`` · ``mux``다. 걸린 시간에는 명령을 만드는 계산도 든다
        on_progress: 이 컷의 진행을 0~1로 알린다 — 단계가 끝날 때와, 단계가 도는 동안
            ffmpeg가 진행을 알릴 때마다. 값은 줄지 않고, 1은 산출물을 다 쓴 뒤에만 나온다.
            실패한 컷은 1을 알리지 않는다
        should_stop: 주면 컷이 도는 동안 짧은 간격으로 부른다. 참을 돌려주면 도는 ffmpeg를
            바로 끝내고 중간 파일 · 쓰다 만 산출물을 지운 뒤 ``CutCancelled``를 낸다 —
            어느 단계에서든 닿는다. 다른 스레드에서 불릴 수 있다
        should_pause: 주면 같은 간격으로 부른다. 참인 동안 도는 ffmpeg를 멈춰 두고 다음 단계의
            ffmpeg를 띄우지 않는다. 거짓이 되면 멈춘 자리에서 이어 간다 — 산출물은 일시정지
            없이 자른 것과 같다. 다른 스레드에서 불릴 수 있다

    Raises:
        CutError: 입력을 다룰 수 없는 경우(``CUT_UNSUPPORTED``), ffmpeg가 실패한
            경우(``CUT_FAILED``), 제한 시간을 넘긴 경우(``CUT_TIMEOUT``)
        CutCancelled: should_stop이 참을 돌려줘 그만둔 경우
        ValueError: 프레임 번호가 범위를 벗어난 경우
    """
    token = hc._stop_check.set(should_stop)
    pause_token = hc._pause_check.set(should_pause)
    try:
        return _hybrid_cut(
            source_path, frames, first, last, output_path, inspect, on_stage, on_progress
        )
    finally:
        hc._pause_check.reset(pause_token)
        hc._stop_check.reset(token)


def _hybrid_cut(
    source_path: str,
    frames: CutFrames,
    first: int,
    last: int,
    output_path: str,
    inspect: bool,
    on_stage: Callable[[str, float], None] | None,
    on_progress: Callable[[float], None] | None,
) -> CutResult:
    hc._reject_transport_stream(source_path)
    plan = plan_cut(frames, first, last)
    has_audio = frames.audio_start is not None
    progress = hc._CutProgress(on_progress, _stage_work(frames, plan, has_audio))
    with hc._stage(on_stage, "probe", progress):
        probed = hc._probe_source(source_path, frames, plan)
        source = ReferenceSource(video=probed.video, audio_bitrate=_audio_bitrate(frames))
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
            with hc._stage(on_stage, f"{number}_{piece.kind}", progress):
                if piece.reencoded:
                    hc._run(
                        hc._encode_command(source_path, frames, piece, source.video, name),
                        hc._ENCODE_TIMEOUT,
                        work,
                    )
                else:
                    hc._run(
                        hc._copy_command(source_path, frames, piece, name), hc._COPY_TIMEOUT, work
                    )
            names.append(name)
            if inspect:
                inspected.append(hc._inspect_piece(os.path.join(work, name), piece, frames))

        if has_audio:
            with hc._stage(on_stage, "audio", progress):
                hc._run(
                    _audio_command(source_path, frames, plan, source.audio_bitrate),
                    hc._COPY_TIMEOUT,
                    work,
                )
        with hc._stage(on_stage, "mux", progress, last=False):
            stderr = hc._run(
                _mux_command(frames, plan, names, has_audio, output_path, work),
                hc._COPY_TIMEOUT,
                work,
            )
        if "Non-monotonic DTS" in stderr:
            raise CutError(CUT_FAILED, "조각을 잇는 곳에서 DTS가 뒤로 간다")
        progress.finish("mux")
    except BaseException:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return CutResult(output_path=output_path, plan=plan, source=source, pieces=tuple(inspected))


def _stage_work(
    frames: CutFrames, plan: CutPlan, has_audio: bool
) -> dict[str, tuple[float, float]]:
    """단계 이름 → (그 단계의 일의 양, 그 단계가 쓰는 출력의 길이[초]). 도는 순서대로 담는다.

    일의 양은 구간 길이에서 어림한 시간이다(``_WORK_*``). 출력의 길이는 ffmpeg가 알리는
    출력 시각을 그 단계 안의 진행으로 바꾸는 분모다 — 0이면 단계가 끝날 때만 오른다.
    """
    length = hc._end_of(frames, plan.last + 1) - hc._start_of(frames, plan.first)
    work: dict[str, tuple[float, float]] = {"probe": (hc._WORK_CALL, 0.0)}
    for number, piece in enumerate(plan.pieces):
        span = hc._end_of(frames, piece.end) - hc._start_of(frames, piece.first)
        if piece.reencoded:
            work[f"{number}_{piece.kind}"] = (hc._WORK_ENCODE_PIECE, span)
        else:
            work[f"{number}_{piece.kind}"] = (hc._WORK_CALL + span * _WORK_COPY_PER_SECOND, span)
    if has_audio:
        work["audio"] = (hc._WORK_CALL + length * _WORK_AUDIO_PER_SECOND, length)
    work["mux"] = (hc._WORK_CALL + length * hc._WORK_MUX_PER_SECOND, length)
    return work


def _audio_command(
    source_path: str, frames: CutFrames, plan: CutPlan, bitrate: int | None
) -> list[str]:
    """오디오 — 구간 전체를 한 번에 재인코딩한다. 디코드는 구간 시작보다 조금 앞에서 시작한다."""
    start = hc._start_of(frames, plan.first)
    length = hc._end_of(frames, plan.last + 1) - start
    preroll = min(_AUDIO_PREROLL_SECONDS, max(start, 0.0))
    seek = start - preroll
    return [
        *(["-ss", hc._seconds(seek)] if seek > 0 else []),
        "-i", source_path,
        *(["-ss", hc._seconds(preroll)] if preroll > 0 else []),  # 앞 여유는 디코드만 하고 버린다
        "-t", hc._seconds(length),
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
                lines.append(f"duration {hc._seconds(span)}")
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


def _audio_bitrate(frames: CutFrames) -> int | None:
    """오디오를 다시 인코딩할 비트레이트(kb/s)를 정한다. 오디오가 없으면 None이다.

    원본 스트림 전체에 대해 하나로 정해지는 값(``CutFrames.audio_bitrate``)만 쓴다. 입력
    파일에서 ffmpeg가 보여 주는 값은 쓰지 않는다 — 받은 패킷으로 추정한 값이라 원본의
    일부만 든 입력에서는 원본과 다르고, 다른 정도가 ffmpeg 버전마다 다르다(7.0.2는
    중간에서 시작하는 fMP4에서 128을 51 · 65로 보여 줬다).

    값을 알 수 없거나 받아들이는 범위를 벗어나면 ``_DEFAULT_AUDIO_BITRATE``를 쓴다.
    """
    if frames.audio_start is None:
        return None
    low, high = _AUDIO_BITRATE_RANGE
    if frames.audio_bitrate is None or not low <= frames.audio_bitrate <= high:
        return _DEFAULT_AUDIO_BITRATE
    return _nominal_bitrate(frames.audio_bitrate)


def _nominal_bitrate(declared: int) -> int:
    """오디오 비트레이트(kb/s)를 가장 가까운 _AUDIO_BITRATE_STEP의 배수로 맞춘다."""
    return max(round(declared / _AUDIO_BITRATE_STEP), 1) * _AUDIO_BITRATE_STEP
