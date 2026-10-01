"""잘라 낸 파일의 정합 판정 (#309).

하이브리드 컷은 재인코딩한 조각과 복사한 조각을 이어 붙인다. 이어 붙인 결과가
원본의 그 구간과 맞는지를 다섯 항목으로 판정한다. 테스트와 실제 영상 확인이 같은
함수를 쓴다 — 판정 기준이 두 곳에서 따로 구현되어 어긋나지 않게 한다.

| 항목 | 기준 |
|---|---|
| params | 재인코딩 조각의 프로파일·레벨·화소 형식·부호화 크기·크롭·SAR·timescale·재정렬 지연이 원본과 같다 |
| decode | 결과 파일을 끝까지 디코드해 오류 출력이 0줄이다 |
| seams | 패킷의 PTS가 원본 프레임의 PTS와 같다 — 누락·중복 없음, 가려진 패킷 없음, DTS 단조 증가 |
| start_end | 첫·끝 PTS가 요청과 1프레임 이내이고, 편집 목록을 무시하고 읽어도 프레임이 같다 |
| av_sync | 구간 시작·끝의 (오디오 − 영상) 차이가 원본의 같은 위치 값과 1프레임 이내로 같다 |

A/V는 컨테이너에 적힌 시각으로 잰다. 디코드한 샘플 수로 재면 AAC 마지막 프레임의
채움(최대 1024샘플)이 섞인다.
"""

from core.models.cut import CutCheck, CutFrames, CutResult
from core.utils.ffmpeg import FFmpegError, run_ffmpeg
from core.utils.hybrid_cut import CUT_FAILED, CutError, expected_end, frame_ticks, read_packets

# PTS를 틱으로 견줄 때 허용하는 차이(틱) — 초 단위 float를 틱으로 되돌릴 때의 반올림
_TICK_TOLERANCE = 1

_HIDDEN_PACKET = 0x4  # 편집 목록이 가린 패킷 (framecrc의 F= 값)
_DECODE_TIMEOUT = 3600  # 결과 파일을 끝까지 디코드하는 제한 시간(초)


def check_cut(frames: CutFrames, result: CutResult) -> CutCheck:
    """잘라 낸 파일이 원본의 그 구간과 맞는지 다섯 항목으로 판정한다.

    Args:
        frames: 컷에 쓴 원본의 프레임 정보
        result: ``hybrid_cut(..., inspect=True)``의 결과 — 조각 정보가 들어 있어야 한다

    Raises:
        ValueError: result에 조각 정보가 없는 경우(inspect 없이 자른 결과)
        CutError: 결과 파일을 읽지 못한 경우
    """
    if not result.pieces:
        raise ValueError("조각 정보가 없다 — hybrid_cut(inspect=True)로 자른 결과가 필요하다")
    plan = result.plan
    notes: dict[str, str] = {}

    # ── params
    source = result.source.video
    different = []
    for info in result.pieces:
        expected_packets = info.piece.end - info.piece.first
        if info.packets != expected_packets or info.hidden_packets:
            different.append(
                f"{info.piece.kind}: 패킷 {info.packets}(기대 {expected_packets}) · 가려진 패킷 {info.hidden_packets}"
            )
        if info.piece.reencoded and (info.video != source or info.timescale != frames.timescale):
            fields = [
                name
                for name in source.__dataclass_fields__
                if getattr(info.video, name) != getattr(source, name)
            ]
            if info.timescale != frames.timescale:
                fields.append("timescale")
            different.append(f"{info.piece.kind}: 원본과 다른 값 {fields}")
    notes["params"] = "; ".join(different) or "재인코딩 조각이 원본과 같다"

    # ── decode
    try:
        done = run_ffmpeg(
            ["-v", "error", "-i", result.output_path, "-f", "null", "-"], timeout=_DECODE_TIMEOUT
        )
    except FFmpegError as e:
        raise CutError(CUT_FAILED, str(e)) from e
    errors = [line for line in done.stderr.splitlines() if line.strip()]
    decode_ok = done.returncode == 0 and not errors
    notes["decode"] = f"exit {done.returncode}, 오류 출력 {len(errors)}줄" + (
        f" — {errors[0][:120]}" if errors else ""
    )

    # ── seams
    timescale, packets = read_packets(result.output_path)
    count = plan.last - plan.first + 1
    want = [
        frame_ticks(frames, frame, plan.first, timescale)
        for frame in range(plan.first, plan.last + 1)
    ]
    got = sorted(packet[1] for packet in packets)
    hidden = sum(1 for packet in packets if packet[3] & _HIDDEN_PACKET)
    off = sum(1 for a, b in zip(got, want) if abs(a - b) > _TICK_TOLERANCE)
    monotonic = all(b[0] > a[0] for a, b in zip(packets, packets[1:]))
    seams_ok = len(packets) == count and not hidden and not off and monotonic
    notes["seams"] = (
        f"패킷 {len(packets)}(기대 {count}) · 가려진 패킷 {hidden} · PTS 불일치 {off} · DTS 단조 증가 {monotonic}"
    )

    # ── start_end
    frame = frames.frame_duration * timescale  # 한 프레임의 틱 수
    start_off = abs(got[0] - want[0]) if got else float("inf")
    end_off = abs(got[-1] - want[-1]) if got else float("inf")
    _, raw_packets = read_packets(result.output_path, extra=("-ignore_editlist", "1"))
    raw = sorted(packet[1] for packet in raw_packets)
    shift = raw[0] - got[0] if raw and got else 0
    same_frames = len(raw) == len(got) and all(
        abs(a - shift - b) <= _TICK_TOLERANCE for a, b in zip(raw, got)
    )
    start_end_ok = start_off <= frame and end_off <= frame and same_frames
    notes["start_end"] = (
        f"시작 오차 {start_off}틱 · 끝 오차 {end_off}틱 (1프레임 {frame:g}틱)"
        f" · 편집 목록 무시: 패킷 {len(raw)}, 전부 {shift}틱만큼만 밀림 {same_frames}"
    )

    # ── av_sync
    start = frames.frame_pts[plan.first]
    end = expected_end(frames, plan)
    if frames.audio_start is None:
        av_ok = True
        notes["av_sync"] = "오디오 없음"
    else:
        audio_scale, audio_packets = read_packets(result.output_path, stream="a:0")
        shown = [p for p in audio_packets if not p[3] & _HIDDEN_PACKET and p[1] + p[2] > 0]
        audio_first = max(shown[0][1], 0) / audio_scale
        audio_last = (shown[-1][1] + shown[-1][2]) / audio_scale
        video_first = got[0] / timescale
        video_last = (got[-1] + frame) / timescale
        # 원본의 같은 위치: 오디오가 구간 시작·끝을 덮으면 0, 덮지 못하면 모자란 만큼
        want_start = max(frames.audio_start, start) - start
        want_end = min(frames.audio_end, end) - end
        start_diff = (audio_first - video_first) - want_start
        end_diff = (audio_last - video_last) - want_end
        av_ok = abs(start_diff) <= frames.frame_duration and abs(end_diff) <= frames.frame_duration
        notes["av_sync"] = (
            f"시작 (오디오−영상) {(audio_first - video_first) * 1000:+.3f}ms (원본 {want_start * 1000:+.3f})"
            f" · 끝 {(audio_last - video_last) * 1000:+.3f}ms (원본 {want_end * 1000:+.3f})"
            f" · 1프레임 {frames.frame_duration * 1000:.3f}ms"
        )

    return CutCheck(
        params=not different,
        decode=decode_ok,
        seams=seams_ok,
        start_end=start_end_ok,
        av_sync=av_ok,
        notes=notes,
    )
