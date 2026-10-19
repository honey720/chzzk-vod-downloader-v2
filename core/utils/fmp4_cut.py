"""fMP4 세그먼트로 받은 구간을 구간 파일로 만든다 — 경계만 다시 인코딩하고 가운데는 그대로 흘린다 (#309).

인코딩 전 다시보기는 fMP4 세그먼트(초기화 세그먼트 + 미디어 세그먼트)로 내려온다. 구간의
세그먼트를 이어 붙인 임시 파일을 만들어 하이브리드 컷에 넘기면 구간 길이만큼을 세 번 쓰고
오디오를 다시 인코딩한다. 여기서는 구간 파일을 한 번만 쓴다.

- **영상**: 구간 경계가 걸린 GOP만 다시 인코딩한다. 경계 세그먼트의 나머지 GOP는 복사해 작은
  조각 파일로 두고, 가운데 세그먼트는 원본 바이트를 그대로 파이프로 넘긴다. 경계가 세그먼트의
  처음 · 끝과 맞으면 그 세그먼트도 통째로 파이프에 들어가고 다시 만드는 것이 없다
- **가운데 세그먼트는 변환 ffmpeg를 거친다**: 원본 바이트를 잇는 ffmpeg(concat의 ``pipe:0``)에
  바로 넣으면 DTS가 뒤로 가고 프레임이 빠진다. 변환 ffmpeg가 영상만 조각난 mp4로 바꿔 넘긴다
- **오디오**: 다시 인코딩하지 않는다. 구간의 세그먼트를 먼저 한 번 흘려 오디오만 복사해 작은
  임시 파일로 뽑고, 잇는 ffmpeg가 그것을 구간 시작 ~ 끝으로 잘라(복사) 함께 넣는다. 구간
  시작에 걸친 덩어리부터 들어가고(앞의 남는 부분은 편집 목록이 가린다), 끝에 걸친 덩어리도
  들어간다 — 오디오가 영상보다 최대 한 덩어리 늦게 끝난다
- **오디오를 영상 조각과 따로 다루는 까닭**: 세그먼트 안에서 오디오와 영상의 시작은 몇 ms씩
  어긋나 있다. concat은 파일마다 가장 이른 스트림의 시각을 기준으로 자리를 잡으므로, 영상
  조각에 오디오가 함께 들면 영상이 그 어긋남만큼 밀린다

같은 구간을 하이브리드 컷으로 자른 것과 영상 패킷 · DTS · PTS · 디코드한 프레임이 같음을 실제
세그먼트로 확인했다. 다른 것은 구간 끝이 세그먼트의 끝과 맞을 때의 마지막 GOP뿐이다 —
하이브리드 컷은 그 GOP를 늘 다시 인코딩하고 여기서는 원본 그대로다.

세그먼트는 통째로 메모리에 올리지 않는다 — 프레임 정보는 moof만 골라 읽고, 본문은 고정 크기
조각으로 옮긴다.

절차(``cut_from_segments``)는 세그먼트의 형식을 모른다. 형식에 따라 다른 것 — 프레임 정보를
읽는 법 · 경계 세그먼트를 ffmpeg가 탐색할 수 있는 입력으로 만드는 법 · 뽑아 둔 오디오의
기준 시각 — 은 ``SegmentSource``로 받는다. TS(암호화 VOD)의 구간도 같은 절차를 쓴다
(``core/utils/ts_cut.py``).
"""

import logging
import os
import shutil
import time
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from itertools import accumulate

from core.api.fmp4 import build_fmp4_index, fmp4_origin
from core.api.hls_fmp4 import read_segment_file
from core.models.cut import CutFrames, CutPiece, CutPlan, CutResult, PieceInfo
from core.models.fmp4_index import Fmp4Head, Fmp4Segment
from core.utils import hybrid_cut as hc
from core.utils.ffmpeg import (
    FFmpegCancelledError,
    FFmpegError,
    FFmpegTimeoutError,
    extract_audio_stream,
    read_in_chunks,
    remux_through,
)
from core.utils.fmp4_sections import Fmp4Section
from core.utils.hybrid_cut import (
    CUT_FAILED,
    CUT_TIMEOUT,
    CUT_UNSUPPORTED,
    CutCancelled,
    CutError,
    cut_frames_from_fmp4,
)

logger = logging.getLogger(__name__)

# 구간 계획이 정한 PTS를 받은 세그먼트에서 다시 읽은 PTS와 견줄 때 허용하는 차이(초) — 같은
# 계산을 두 번 한 값이라 같아야 하고, float 오차만 흡수한다
_PTS_TOLERANCE = 1e-6
_COPY_CHUNK_BYTES = 1024 * 1024  # 세그먼트를 읽어 옮기거나 넘길 때 한 번에 읽는 크기 — 1MB
_PAUSE_POLL_SECONDS = 0.05  # 넘기는 도중 일시정지가 풀렸는지 확인하는 간격
_AUDIO_FILE = "audio.m4a"  # 오디오 통과가 만드는 임시 파일
_LIST_FILE = "list.txt"  # 잇는 ffmpeg가 읽는 조각 목록
# 변환 ffmpeg가 파이프로 흘릴 수 있는 조각난 mp4를 쓰게 하는 옵션
_FRAGMENTED = "frag_keyframe+empty_moov+default_base_moof"
_WORK_AUDIO_PER_SECOND = 0.001  # 오디오 통과 — 구간 길이 1초에(읽기만 한다)


def frame_at(frames: CutFrames, pts: float) -> int:
    """PTS가 pts인 프레임의 번호. 받은 세그먼트에 그 프레임이 없으면 CutError다."""
    position = bisect_left(frames.frame_pts, pts - _PTS_TOLERANCE)
    if position < len(frames.frame_pts) and abs(frames.frame_pts[position] - pts) <= _PTS_TOLERANCE:
        return position
    raise CutError(CUT_FAILED, f"받은 세그먼트에 PTS {pts:.6f}초인 프레임이 없다")


@dataclass(frozen=True)
class SegmentSource:
    """구간의 세그먼트에서 읽은 것 — 세그먼트의 형식(fMP4 · TS)에 따라 만드는 법이 다르다."""

    frames: CutFrames  # 구간의 세그먼트 전체의 프레임 정보 — 번호는 넘긴 세그먼트들 기준이다
    frame_counts: Sequence[int]  # 세그먼트마다의 영상 프레임 수
    key_starts: Sequence[bool]  # 세그먼트마다 — 키프레임으로 시작하는가(영상이 없으면 참)
    prefix: bytes  # 세그먼트들 앞에 한 번 넘기는 바이트 — fMP4의 초기화 세그먼트. 없으면 빈 것
    # (만들 파일 이름, 세그먼트의 자리) → (ffmpeg가 탐색할 수 있는 입력 파일, 그 파일의 프레임 정보).
    # 경계 조각을 만들 세그먼트에만 부른다. 파일은 작업 폴더에 만든다
    boundary: Callable[[str, int], tuple[str, CutFrames]]
    # 세그먼트를 흘려 뽑은 오디오 파일의 0초가 frames의 시각으로 언제인가. 오디오가 없으면 None
    audio_origin: float | None
    audio_overhang: float  # 오디오 덩어리 하나의 길이(초) — 구간 끝에 걸친 덩어리가 넘어가는 만큼


def cut_fmp4_section(
    work_dir: str,
    head: Fmp4Head,
    section: Fmp4Section,
    segment_paths: Sequence[str],
    output_path: str,
    *,
    on_fed: Callable[[int], None] | None = None,
    inspect: bool = False,
    on_stage: Callable[[str, float], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    on_read: Callable[[float], None] | None = None,
) -> tuple[CutResult, CutFrames]:
    """받아 둔 fMP4 세그먼트에서 구간 하나를 output_path에 mp4로 쓴다.

    Args:
        work_dir: 조각 파일 · 오디오 임시 파일을 둘 폴더. 비우고 쓰고, 끝나면 지운다
        head: 초기화 세그먼트와 그 해석 결과
        section: 구간 — 받을 세그먼트의 범위와 첫 · 끝 프레임의 시각
        segment_paths: 구간의 세그먼트 파일 — ``section.first_segment``부터 이어지는 순서대로
        output_path: 만들 구간 파일. 실패하면 남기지 않는다
        on_fed: 영상을 잇는 통과에서 세그먼트 하나를 다 넘길 때마다 그 자리(segment_paths의
            번호)로 부른다 — 부르는 쪽이 더 쓰이지 않는 세그먼트를 지운다. 오디오 통과와 경계
            조각으로만 읽힌 세그먼트에는 부르지 않는다
        inspect · on_progress · should_stop · should_pause:
            ``core.utils.hybrid_cut.encode_boundary_pieces``와 같다. on_progress의 1은 구간
            파일을 다 쓴 뒤에만 나온다
        on_stage: 단계는 ``read``(프레임 읽기) · ``audio``(오디오 통과) · 조각마다의
            ``<번호>_<종류>`` · ``join``(구간 파일 쓰기)이다
        on_read: 프레임 정보를 읽는 동안(``read`` 단계) 읽은 세그먼트의 비율(0~1)로 부른다.
            on_progress는 그 단계가 끝난 뒤부터 오른다 — 부르는 쪽이 둘을 한 진행으로 합친다

    Returns:
        (컷 결과, 구간의 세그먼트 전체에서 읽은 프레임 정보)

    Raises:
        CutError: 받은 세그먼트로 구간을 만들 수 없거나(``CUT_UNSUPPORTED`` — 이어지는
            세그먼트가 키프레임으로 시작하지 않는 등), ffmpeg가 실패한 경우(``CUT_FAILED``)
        CutCancelled: should_stop이 참을 돌려줘 그만둔 경우
        Mp4Error: 세그먼트의 moof를 해석하지 못한 경우(``core.api.mp4``)
    """
    paths = list(segment_paths)
    init = head.init

    def load() -> SegmentSource:
        # 프레임 정보는 moof만 골라 읽는다 — 본문은 읽지 않는다. 세그먼트마다 멈춤 · 일시정지를
        # 따르고 읽은 비율을 알린다(세그먼트가 수천 개면 이 단계만 몇 초다)
        parsed = []
        for count, path in enumerate(paths, start=1):
            _hold()
            parsed.append(read_segment_file(path, init))
            if on_read is not None:
                on_read(count / len(paths))
        index = build_fmp4_index(init, parsed, section.origin)
        input_start = float(fmp4_origin(init, parsed[0]) - section.origin)
        frames = cut_frames_from_fmp4(init, parsed, index, input_start)
        has_audio = init.audio is not None and frames.audio_start is not None
        # 구간 끝에 걸친 오디오 덩어리는 통째로 들어간다 — 그 하나의 길이
        ticks = (
            next((s.audio.durations[-1] for s in reversed(parsed) if s.audio.durations), 0)
            if has_audio
            else 0
        )
        return SegmentSource(
            frames=frames,
            frame_counts=tuple(len(segment.video.sizes) for segment in parsed),
            key_starts=tuple(
                not segment.video.sizes
                or bool(segment.video.sync_samples and segment.video.sync_samples[0] == 0)
                for segment in parsed
            ),
            prefix=head.init_data,
            boundary=lambda name, position: (
                _joined(work_dir, name, head, paths[position]),
                _segment_frames(head, parsed[position], section),
            ),
            # 뽑아 둔 오디오는 입력의 시작부터 센다
            audio_origin=frames.input_start if has_audio else None,
            audio_overhang=ticks / init.audio.timescale if has_audio else 0.0,
        )

    return cut_from_segments(
        work_dir,
        load,
        section.first_pts,
        section.last_pts,
        paths,
        output_path,
        on_fed=on_fed,
        inspect=inspect,
        on_stage=on_stage,
        on_progress=on_progress,
        should_stop=should_stop,
        should_pause=should_pause,
    )


def cut_from_segments(
    work_dir: str,
    load: Callable[[], SegmentSource],
    first_pts: float,
    last_pts: float,
    segment_paths: Sequence[str],
    output_path: str,
    *,
    on_fed: Callable[[int], None] | None = None,
    inspect: bool = False,
    on_stage: Callable[[str, float], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> tuple[CutResult, CutFrames]:
    """받아 둔 세그먼트에서 구간 하나를 output_path에 mp4로 쓴다 — 형식과 무관한 절차다.

    Args:
        work_dir: 조각 파일 · 오디오 임시 파일을 둘 폴더. 비우고 쓰고, 끝나면 지운다
        load: 세그먼트에서 프레임 정보 등을 읽어 돌려주는 함수 — ``read`` 단계로 잰다.
            작업 폴더가 만들어진 뒤에 한 번 부른다
        first_pts: 구간 첫 프레임의 PTS — ``load``가 준 프레임 정보의 시각
        last_pts: 구간 끝 프레임의 PTS
        segment_paths: 구간의 세그먼트 파일 — 이어지는 순서대로
        output_path: 만들 구간 파일. 실패하면 남기지 않는다
        on_fed · inspect · on_stage · on_progress · should_stop · should_pause:
            ``cut_fmp4_section``과 같다

    Returns:
        (컷 결과, ``load``가 준 프레임 정보)

    Raises:
        CutError · CutCancelled: ``cut_fmp4_section``과 같다. ``load``가 던진 예외는 그대로 나간다
    """
    token = hc._stop_check.set(should_stop)
    pause_token = hc._pause_check.set(should_pause)
    output_path = os.path.abspath(output_path)
    shutil.rmtree(work_dir, ignore_errors=True)  # 이전 실행이 죽으면서 남긴 것
    os.makedirs(work_dir)
    try:
        return _cut(
            work_dir, load, first_pts, last_pts, list(segment_paths), output_path, on_fed,
            inspect, on_stage, on_progress,
        )  # fmt: skip
    except BaseException:
        # 지우지 못해도 원래 예외를 올린다 (#389) — 여기서 난 OSError가 CutError를 대신하면 구간
        # 하나의 실패가 다운로드 전체의 실패가 된다
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError as e:
            logger.warning("실패한 구간 파일을 지우지 못했다: %s", e)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        hc._pause_check.reset(pause_token)
        hc._stop_check.reset(token)


def _hold() -> None:
    """넘기는 도중 멈춤 · 일시정지 요청을 따른다 — 일시정지 중에는 넘기지 않고 기다린다."""
    stop, pause = hc._stop_check.get(), hc._pause_check.get()
    if stop is not None and stop():
        raise CutCancelled("멈추라는 요청으로 컷을 그만뒀다")
    while pause is not None and pause():
        time.sleep(_PAUSE_POLL_SECONDS)
        if stop is not None and stop():
            raise CutCancelled("멈추라는 요청으로 컷을 그만뒀다")


def _feed(
    init_data: bytes, paths: Sequence[str], on_each: Callable[[int], None] | None = None
) -> Iterator[bytes]:
    """앞에 넘길 바이트(초기화 세그먼트)와 그 세그먼트들의 바이트를 고정 크기 조각으로 낸다."""
    if init_data:
        yield init_data
    for number, path in enumerate(paths):
        for chunk in read_in_chunks(path, _COPY_CHUNK_BYTES):
            _hold()
            yield chunk
        # 여기 왔으면 이 세그먼트의 마지막 조각까지 넘어갔다
        if on_each is not None:
            on_each(number)


def _segment_frames(head: Fmp4Head, segment: Fmp4Segment, section: Fmp4Section) -> CutFrames:
    """세그먼트 하나만 든 입력 파일의 프레임 정보 — 경계 조각을 만들 때 쓴다."""
    index = build_fmp4_index(head.init, [segment], section.origin)
    start = float(fmp4_origin(head.init, segment) - section.origin)
    return cut_frames_from_fmp4(head.init, [segment], index, start)


def _joined(work_dir: str, name: str, head: Fmp4Head, segment_path: str) -> str:
    """초기화 세그먼트 + 세그먼트 하나를 이은 작은 fMP4 — ffmpeg가 탐색할 수 있는 입력이다."""
    path = os.path.join(work_dir, name)
    with open(path, "wb") as out, open(segment_path, "rb") as source:
        out.write(head.init_data)
        shutil.copyfileobj(source, out, _COPY_CHUNK_BYTES)
    return path


def _cut(
    work_dir: str,
    load: Callable[[], SegmentSource],
    first_pts: float,
    last_pts: float,
    paths: list[str],
    output_path: str,
    on_fed: Callable[[int], None] | None,
    inspect: bool,
    on_stage: Callable[[str, float], None] | None,
    on_progress: Callable[[float], None] | None,
) -> tuple[CutResult, CutFrames]:
    started = time.perf_counter()
    try:
        given = load()
    finally:
        if on_stage is not None:
            on_stage("read", time.perf_counter() - started)
    frames = given.frames
    first, last = frame_at(frames, first_pts), frame_at(frames, last_pts)
    offsets = [0, *accumulate(given.frame_counts)]
    if offsets[-1] != len(frames.frame_pts):
        raise CutError(CUT_UNSUPPORTED, "세그먼트의 영상 샘플 수와 표시되는 프레임 수가 다르다")
    for number, starts_with_key in enumerate(given.key_starts):
        if not starts_with_key:
            raise CutError(CUT_UNSUPPORTED, f"세그먼트 {number}가 키프레임으로 시작하지 않는다")

    # 프레임이 든 세그먼트 — 영상 샘플이 없는 세그먼트는 건너뛴다(같은 자리가 겹치면 뒤의 것)
    head_segment = bisect_right(offsets, first) - 1
    tail_segment = bisect_right(offsets, last) - 1
    length = hc._end_of(frames, last + 1) - hc._start_of(frames, first)
    has_audio = given.audio_origin is not None
    # 뽑아 둔 오디오 파일에서 구간이 시작하는 자리(초)
    start = frames.frame_pts[first] - given.audio_origin if has_audio else 0.0

    # ---- 조각의 계획: (종류, 파일 이름 또는 None(파이프), 전체에서의 첫 프레임, 끝 프레임 다음,
    #      그 조각을 만드는 명령의 재료)
    base_head, base_tail = offsets[head_segment], offsets[tail_segment]
    local_first, local_last = first - base_head, last - base_tail
    pieces: list[tuple[str, str | None, int, int, tuple]] = []
    piped: list[int] = []
    one_segment = head_segment == tail_segment
    ends_with_segment = local_last == given.frame_counts[tail_segment] - 1
    # 경계 세그먼트의 입력 — 머리는 늘 만든다(원본의 영상 파라미터를 여기서 읽는다). 꼬리는
    # 꼬리 조각을 만들 때만 만든다
    inputs = {"head": given.boundary("head_source.mp4", head_segment)}
    head_frames = inputs["head"][1]
    if not one_segment and not ends_with_segment:
        inputs["tail"] = given.boundary("tail_source.mp4", tail_segment)
    tail_frames = inputs.get("tail", inputs["head"])[1]
    if one_segment and not ends_with_segment:
        # 세그먼트 하나 안에서 끝난다 — 그 세그먼트만 입력으로 한 하이브리드 컷의 조각 그대로
        for number, piece in enumerate(hc.plan_cut(head_frames, local_first, local_last).pieces):
            pieces.append(
                (piece.kind, f"{number}_{piece.kind}.mp4", base_head + piece.first,
                 base_head + piece.end, ("head", piece))
            )  # fmt: skip
    else:
        piped = list(range(head_segment + 1, tail_segment))
        if local_first == 0:
            piped.insert(0, head_segment)  # 구간이 이 세그먼트의 처음부터다 — 통째로 흘린다
        else:
            key = next((k for k in head_frames.keyframes if k > local_first), None)
            count = len(head_frames.frame_pts)
            encoded = CutPiece("head", local_first, key if key is not None else count)
            pieces.append(("head", "0_head.mp4", first, base_head + encoded.end, ("head", encoded)))
            if key is not None:
                rest_end = offsets[head_segment + 1]
                pieces.append(("mid", "1_rest.mp4", base_head + key, rest_end, ("rest", key)))
        after: list[tuple[str, str | None, int, int, tuple]] = []
        if not one_segment and ends_with_segment:
            piped.append(tail_segment)  # 구간이 이 세그먼트의 끝까지다 — 통째로 흘린다
        elif not one_segment:
            key = max(k for k in tail_frames.keyframes if k <= local_last)
            if key > 0:
                lead = CutPiece("mid", 0, key)
                after.append(("mid", "3_lead.mp4", base_tail, base_tail + key, ("tail", lead)))
            encoded = CutPiece("tail", key, local_last + 1)
            after.append(("tail", "4_tail.mp4", base_tail + key, last + 1, ("tail", encoded)))
        if piped:
            pieces.append(("mid", None, offsets[piped[0]], offsets[piped[-1] + 1], ()))
        pieces.extend(after)

    work: dict[str, tuple[float, float]] = {}
    if has_audio:
        # 둘째 값은 그 단계 안의 진행의 분모 — 오디오 통과는 넘긴 세그먼트 수로 센다
        work["audio"] = (hc._WORK_CALL + length * _WORK_AUDIO_PER_SECOND, float(len(paths)))
    for kind, name, _first, _end, _made in pieces:
        if name is not None:
            stage = os.path.splitext(name)[0]
            work[stage] = (hc._WORK_ENCODE_PIECE if kind != "mid" else hc._WORK_CALL, 0.0)
    work["join"] = (hc._WORK_CALL + length * hc._WORK_MUX_PER_SECOND, length)
    progress = hc._CutProgress(on_progress, work)

    # ---- 1. 오디오 통과 — 구간의 세그먼트를 한 번 흘려 오디오만 복사로 뽑는다. 지우지 않는다
    if has_audio:
        with hc._stage(on_stage, "audio", progress):
            passed = progress.sink("audio")  # 세그먼트를 하나 넘길 때마다 그 수를 알린다
            try:
                extract_audio_stream(
                    _feed(
                        given.prefix,
                        paths,
                        (lambda number: passed(number + 1)) if passed is not None else None,
                    ),
                    os.path.join(work_dir, _AUDIO_FILE),
                    timeout=hc._COPY_TIMEOUT,
                    should_stop=hc._stop_check.get(),
                    should_pause=hc._pause_check.get(),
                )
            except FFmpegCancelledError as e:
                raise CutCancelled(str(e)) from e
            except FFmpegTimeoutError as e:
                raise CutError(CUT_TIMEOUT, str(e)) from e
            except FFmpegError as e:
                raise CutError(CUT_FAILED, str(e)) from e

    # ---- 2. 경계 조각 (영상만)
    source = hc._probe_source(inputs["head"][0], head_frames, CutPlan(local_first, local_first, ()))
    inspected: list[PieceInfo] = []
    for kind, name, piece_first, piece_end, made in pieces:
        if name is None:
            continue
        # 머리 세그먼트의 나머지 GOP(rest)는 머리와 같은 입력에서 나온다
        path, piece_frames = inputs["tail" if made[0] == "tail" else "head"]
        with hc._stage(on_stage, os.path.splitext(name)[0], progress):
            if made[0] == "rest":
                command = hc._copy_rest_command(path, piece_frames, made[1], name)
                timeout = hc._COPY_TIMEOUT
            elif made[1].reencoded:
                command = hc._encode_command(path, piece_frames, made[1], source.video, name)
                timeout = hc._ENCODE_TIMEOUT
            else:
                command = hc._copy_command(path, piece_frames, made[1], name)
                timeout = hc._COPY_TIMEOUT
            hc._run(command, timeout, work_dir)
        if inspect:
            inspected.append(
                hc._inspect_piece(
                    os.path.join(work_dir, name), CutPiece(kind, piece_first, piece_end), frames
                )
            )

    # ---- 3. 잇기 — 조각 · 파이프 · 조각을 잇고, 뽑아 둔 오디오를 구간 시작 ~ 끝으로 잘라 함께 넣는다
    if len(pieces) == 1:
        # 조각이 하나면 concat을 거치지 않는다 — concat은 키프레임마다 SPS · PPS를 패킷 안에
        # 끼워 넣는다. 통째로 흘리는 구간의 패킷이 원본 그대로 남는다
        video_input = ["-i", pieces[0][1] or "pipe:0"]
    else:
        lines = ["ffconcat version 1.0"]
        for number, (_kind, name, piece_first, piece_end, _made) in enumerate(pieces):
            lines.append("file pipe:0" if name is None else f"file {name}")
            if number + 1 < len(pieces):
                span = frames.frame_pts[piece_end] - frames.frame_pts[piece_first]
                lines.append(f"duration {hc._seconds(span)}")
        with open(os.path.join(work_dir, _LIST_FILE), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        video_input = [
            "-protocol_whitelist", "file,pipe,fd", "-f", "concat", "-safe", "0", "-i", _LIST_FILE,
        ]  # fmt: skip
    scale = ["-video_track_timescale", str(frames.timescale)]
    audio_input = (
        ["-ss", hc._seconds(start), "-t", hc._seconds(length), "-i", _AUDIO_FILE]
        if has_audio
        else []
    )
    join = [
        *video_input,
        *audio_input,
        "-map", "0:v:0", *(["-map", "1:a:0"] if has_audio else []),
        "-c", "copy", *scale, "-f", "mp4", output_path,
    ]  # fmt: skip
    sink = progress.sink("join")

    def fed(number: int) -> None:
        position = piped[number]
        if sink is not None:
            sink(max(frames.frame_pts[offsets[position + 1] - 1] - frames.frame_pts[first], 0.0))
        if on_fed is not None:
            on_fed(position)

    with hc._stage(on_stage, "join", progress, last=False):
        if piped:
            convert = [
                "-i", "pipe:0", "-map", "0:v:0", "-c", "copy", *scale,
                "-movflags", _FRAGMENTED, "-f", "mp4", "pipe:1",
            ]  # fmt: skip
            try:
                warnings = remux_through(
                    _feed(given.prefix, [paths[n] for n in piped], fed),
                    convert,
                    join,
                    output_path,
                    cwd=work_dir,
                    # 세그먼트를 다 넘긴 뒤의 기다림도 다른 단계와 같은 제한 시간 · 멈춤 · 일시정지를 따른다
                    timeout=hc._COPY_TIMEOUT,
                    should_stop=hc._stop_check.get(),
                    should_pause=hc._pause_check.get(),
                )
            except FFmpegCancelledError as e:
                raise CutCancelled(str(e)) from e
            except FFmpegTimeoutError as e:
                raise CutError(CUT_TIMEOUT, str(e)) from e
            except FFmpegError as e:
                raise CutError(CUT_FAILED, str(e)) from e
        else:
            warnings = hc._run(join, hc._COPY_TIMEOUT, work_dir)
    if "Non-monotonic DTS" in warnings:
        raise CutError(CUT_FAILED, "조각을 잇는 곳에서 DTS가 뒤로 간다")
    progress.finish("join")
    plan = CutPlan(first, last, tuple(CutPiece(kind, a, b) for kind, _name, a, b, _made in pieces))
    overhang = given.audio_overhang if has_audio else 0.0
    result = CutResult(
        output_path=output_path,
        plan=plan,
        source=source,
        pieces=tuple(inspected),
        audio_overhang=overhang,
    )
    return result, frames
