"""MPEG-TS 세그먼트에서 프레임 시각·키프레임 읽기 (#309).

암호화 VOD(hls_aes 경로)의 세그먼트 형식이 MPEG-TS다. 라이브 다시보기(m3u8
경로)는 fMP4 세그먼트라 이 모듈의 입력이 아니다.

TS 세그먼트에는 mp4의 moov 같은 색인이 없다. 프레임 시각과 키프레임 위치는
세그먼트를 받아 패킷을 따라가야 알 수 있다. 이 모듈은 받은 bytes에서 그 정보를
뽑는 순수 함수다 — 네트워크를 모른다.

두 단계로 나뉜다.

- ``parse_ts`` — 188바이트 패킷을 따라가 PAT → PMT로 영상·오디오 PID를 찾고,
  PES 머리의 PTS·DTS와 키프레임 여부를 읽는다. 값은 읽은 그대로의 33비트 틱이다.
  오디오가 AAC(ADTS)면 PES마다 든 프레임 수도 센다
- ``build_ts_index`` — VOD 시작을 0으로 맞추고 33비트 랩어라운드를 풀어
  초 단위 색인(``TsIndex``)을 만든다. 프레임 길이와 오디오가 끝나는 시각도 구한다
- ``ts_video_span`` — 색인의 영상이 차지하는 시각 범위를 돌려준다
- ``ts_audio_span`` — 오디오가 차지하는 시각 범위를 돌려준다. 영상 프레임이 없는
  세그먼트를 잴 때 쓴다(``build_ts_index``는 그런 세그먼트를 받지 않는다)
- ``join_ts_streams`` — 세그먼트마다 읽은 ``TsStreams``를 순서대로 하나로 잇는다

TS에는 mp4와 달리 프레임(샘플)의 길이가 적혀 있지 않다. 영상 프레임의 길이는 PTS
간격에서 재고, 오디오가 끝나는 시각은 마지막 오디오 PES에 든 프레임 수로 구한다 —
오디오 PES 하나에 프레임이 여럿 들고, 세그먼트의 마지막 PES는 프레임 수가 다르다.

입력은 **복호화된** bytes다. 암호화된 세그먼트(AES-128)는 호출하는 쪽이 먼저
복호화해서 넘긴다 — 암호화된 채로는 패킷의 동기 바이트부터 맞지 않는다.

전제:

- 영상 PES 하나가 프레임 하나다(HLS용 TS의 일반적인 모양)
- PAT·PMT는 패킷 하나에 들어간다
- 키프레임은 적응 필드의 랜덤 액세스 표시, 또는 H.264의 IDR NAL · HEVC의 IRAP
  NAL로 판정한다

실패 키는 번역하지 않은 i18n 키 원문이며 번역은 앱 계층이 한다
(``MetadataError``와 같은 방식).
"""

from collections import Counter
from collections.abc import Sequence

from core.models.ts_index import TsIndex, TsStreams

# 실패 키 — 번역하지 않은 i18n 키 원문
TS_INVALID = "Video segment is damaged"  # 패킷 경계·동기 바이트·PES 머리가 맞지 않는다
TS_UNSUPPORTED = "Video segment layout is not supported"  # 영상 스트림 없음·PTS 없는 PES 등

TS_PACKET_SIZE = 188  # TS 패킷 길이(바이트)
TS_CLOCK = 90_000  # PTS·DTS의 초당 틱 수

_SYNC_BYTE = 0x47  # 모든 TS 패킷의 첫 바이트
_WRAP = 1 << 33  # PTS·DTS는 33비트라 이 값에서 0으로 돌아간다(약 26.5시간)

# 첫 타임스탬프가 VOD 시작(origin)보다 앞설 수 있는 최대 틱 — 10초.
# DTS는 B프레임 재정렬 지연만큼 가장 이른 PTS보다 앞선다(수십 ms). 이 범위 안의
# "origin보다 조금 작은 값"은 랩어라운드가 아니라 음수로 읽는다
_MAX_LEAD_TICKS = 10 * TS_CLOCK

_VIDEO_STREAM_TYPES = {0x1B: "h264", 0x24: "hevc", 0x02: "mpeg2"}  # PMT stream_type → 코덱
_AUDIO_STREAM_TYPES = (0x0F, 0x11, 0x03, 0x04, 0x81)  # AAC(ADTS) · AAC(LATM) · MP3 · AC-3

_START_CODE = b"\x00\x00\x01"  # PES와 NAL의 시작 코드
_PES_TIMESTAMP_END = 19  # PES 머리에서 PTS·DTS까지 읽는 데 필요한 바이트 수

_ADTS_STREAM_TYPE = 0x0F  # PMT stream_type — AAC(ADTS). 프레임 수를 셀 수 있는 유일한 형식이다
_ADTS_HEADER_SIZE = 7  # 바이트 — CRC가 없는 ADTS 머리. 프레임 길이는 이 안에 있다
_AAC_FRAME_SAMPLES = 1024  # AAC 프레임(raw data block) 하나의 표본 수
# ADTS 머리의 sampling_frequency_index → 표본화율(Hz) (ISO/IEC 14496-3 표 1.18)
_ADTS_SAMPLE_RATES = (
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350,
)  # fmt: skip


class TsError(Exception):
    """TS 세그먼트를 해석하지 못했다.

    message_key는 번역하지 않은 i18n 키 원문이다(이 모듈의 ``TS_*`` 상수).
    """

    def __init__(self, message_key: str, detail: str = ""):
        super().__init__(f"{message_key}: {detail}" if detail else message_key)
        self.message_key = message_key


def parse_ts(data: bytes) -> TsStreams:
    """복호화된 TS bytes에서 영상 프레임의 PTS·DTS·키프레임과 오디오 PES의 PTS를 읽는다.

    세그먼트 하나든 여러 세그먼트를 이어 붙인 것이든 받는다. 값은 읽은 그대로의
    33비트 틱이다 — 0으로 맞추거나 랩어라운드를 푸는 일은 ``build_ts_index``가 한다.

    PES 하나가 여러 패킷에 걸치면 이어 붙여서 읽는다. PES 머리나 키프레임을 알리는
    NAL이 첫 패킷 밖에 있을 수 있다.

    오디오가 AAC(ADTS)면 PES마다 든 프레임 수와 표본화율도 읽는다(``audio_frames`` ·
    ``audio_sample_rate``). 다른 오디오 형식이면 프레임 수는 모두 0이다. 표본화율은
    프레임을 센 마지막 PES의 값이다 — 도중에 바뀌는 스트림이면 오디오가 끝나는 시각을
    구할 때 마지막 PES의 것이 필요하다.

    Raises:
        TsError: 길이가 188의 배수가 아니거나 동기 바이트가 틀린 경우(``TS_INVALID``),
            PAT·PMT가 없거나 영상 스트림이 없거나 PES에 PTS가 없는 경우(``TS_UNSUPPORTED``)
    """
    if not data or len(data) % TS_PACKET_SIZE:
        raise TsError(TS_INVALID, f"길이 {len(data)}바이트는 {TS_PACKET_SIZE}의 배수가 아니다")

    pmt_pid = video_pid = audio_pid = audio_type = -1
    codec = ""
    video_pts: list[int] = []
    video_dts: list[int] = []
    keyframes: list[int] = []
    audio_pts: list[int] = []
    audio_frames: list[int] = []
    audio_rates: list[int] = []  # 프레임을 센 PES마다 ADTS 머리에서 읽은 표본화율
    video_chunks: list[bytes] = []  # 지금 모으는 영상 PES의 조각
    video_random_access = False
    audio_chunks: list[bytes] = []  # 지금 모으는 오디오 PES의 조각

    def finish_video() -> None:
        if not video_chunks:
            return
        pes = b"".join(video_chunks)
        pts, dts, payload_start = _pes_timestamps(pes)
        if video_random_access or _starts_with_keyframe(pes, payload_start, codec):
            keyframes.append(len(video_pts))
        video_pts.append(pts)
        video_dts.append(dts)

    def finish_audio() -> None:
        if not audio_chunks:
            return
        pes = b"".join(audio_chunks)
        pts, _dts, payload_start = _pes_timestamps(pes)
        audio_pts.append(pts)
        frames, rate = (
            _adts_frames(pes, payload_start) if audio_type == _ADTS_STREAM_TYPE else (0, None)
        )
        audio_frames.append(frames)
        if rate is not None:
            audio_rates.append(rate)

    for offset in range(0, len(data), TS_PACKET_SIZE):
        if data[offset] != _SYNC_BYTE:
            raise TsError(TS_INVALID, f"{offset}바이트 위치에 동기 바이트가 없다")
        flags = data[offset + 1]
        pid = ((flags & 0x1F) << 8) | data[offset + 2]
        if pid not in (0, pmt_pid, video_pid, audio_pid):
            continue
        adaptation = (data[offset + 3] >> 4) & 0x3
        start = offset + 4
        random_access = False
        if adaptation & 0x2:
            length = data[start]
            if length > TS_PACKET_SIZE - 5:
                raise TsError(TS_INVALID, f"{offset}바이트 위치의 적응 필드 길이 {length}")
            random_access = bool(length and data[start + 1] & 0x40)
            start += 1 + length
        if not adaptation & 0x1 or start >= offset + TS_PACKET_SIZE:
            continue  # 본문이 없는 패킷
        payload = data[start : offset + TS_PACKET_SIZE]
        unit_start = bool(flags & 0x40)

        if pid == 0:
            if unit_start:
                pmt_pid = _program_map_pid(payload)
        elif pid == pmt_pid:
            if unit_start:
                video_pid, audio_pid, codec, audio_type = _elementary_pids(payload)
        elif pid == video_pid:
            if unit_start:
                finish_video()
                video_chunks = [payload]
                video_random_access = random_access
            elif video_chunks:
                video_chunks.append(payload)
        elif unit_start:
            finish_audio()
            audio_chunks = [payload]
        elif audio_chunks:
            audio_chunks.append(payload)

    finish_video()
    finish_audio()
    if pmt_pid < 0 or video_pid < 0:
        raise TsError(TS_UNSUPPORTED, "PAT·PMT에서 영상 스트림을 찾지 못했다")
    return TsStreams(
        video_pts=tuple(video_pts),
        video_dts=tuple(video_dts),
        video_keyframes=tuple(keyframes),
        audio_pts=tuple(audio_pts),
        audio_frames=tuple(audio_frames),
        # 마지막 값이다 — 오디오가 끝나는 시각은 마지막 PES의 프레임 수에 이 값을 쓴다
        audio_sample_rate=audio_rates[-1] if audio_rates else None,
    )


def ts_origin(streams: TsStreams) -> int:
    """세그먼트에서 가장 이른 PTS(33비트 원시 값)를 구한다.

    VOD의 **첫 세그먼트**로 부르면 그 값이 VOD 시작(0초)이다. 영상과 오디오를 통틀어
    본다. 세그먼트 안에서 랩어라운드가 일어나도 그 직전의 값을 고른다.

    Raises:
        TsError: 영상 프레임도 오디오 PES도 없는 경우(``TS_INVALID``)
    """
    stamps = [*streams.video_pts, *streams.audio_pts]
    if not stamps:
        raise TsError(TS_INVALID, "타임스탬프가 없다")
    base = stamps[0]
    return (base + min(_nearest(stamp - base) for stamp in stamps)) % _WRAP


def build_ts_index(streams: TsStreams, origin: int, expected_start: float | None = None) -> TsIndex:
    """원시 타임스탬프를 VOD 시작 = 0 기준의 초 단위 색인으로 바꾼다.

    DTS는 디코드 순서로 이어지므로 앞 값과의 차이로 랩어라운드를 풀고, PTS는 같은
    프레임의 DTS와의 차이로 푼다.

    첫 타임스탬프는 origin과의 차이를 0 이상으로 읽는다(origin보다 최대 10초 앞선
    값만 음수로 본다). 그래서 VOD 시작에서 약 26.5시간 안쪽의 세그먼트는 그대로
    맞는다. 그보다 뒤의 세그먼트는 ``expected_start``를 줘야 한다.

    프레임 길이(``frame_duration``)는 표시 순서로 이웃한 프레임의 PTS 간격 가운데 가장
    많은 값이다 — 프레임이 빠진 자리의 긴 간격에 끌려가지 않는다. 오디오가 끝나는
    시각(``audio_end``)은 마지막 오디오 PES의 PTS에 그 PES에 든 프레임의 길이(프레임 수 ×
    1024 ÷ 표본화율)를 더한 값이다. PES의 수로는 구할 수 없다 — PES 하나에 프레임이
    여럿 들고 마지막 PES는 프레임 수가 다르다.

    Args:
        streams: ``parse_ts``의 결과
        origin: VOD 시작의 PTS(33비트 원시 값) — 첫 세그먼트의 ``ts_origin``
        expected_start: 이 세그먼트가 시작하는 대략의 시각(초, 플레이리스트 기준).
            주면 랩어라운드 횟수를 이 값에 가장 가깝게 맞춘다

    Raises:
        TsError: 영상 프레임이 없는 경우(``TS_INVALID``)
    """
    if not streams.video_pts:
        raise TsError(TS_INVALID, "영상 프레임이 없다")

    def unwrap(stamps: tuple[int, ...]) -> list[int]:
        return _unwrap(stamps, origin, expected_start)

    decode = unwrap(streams.video_dts)
    present = [
        dts + _nearest(pts - raw_dts)
        for dts, pts, raw_dts in zip(decode, streams.video_pts, streams.video_dts)
    ]
    order = sorted(range(len(present)), key=present.__getitem__)
    key_samples = set(streams.video_keyframes)
    audio = unwrap(streams.audio_pts) if streams.audio_pts else []
    frame_ticks = _frame_ticks([present[sample] for sample in order])
    return TsIndex(
        frame_pts=tuple(present[sample] / TS_CLOCK for sample in order),
        frame_samples=tuple(order),
        keyframes=tuple(number for number, sample in enumerate(order) if sample in key_samples),
        decode_times=tuple(ticks / TS_CLOCK for ticks in decode),
        audio_pts=tuple(ticks / TS_CLOCK for ticks in audio),
        frame_duration=frame_ticks / TS_CLOCK if frame_ticks else None,
        audio_end=_audio_end(streams, audio),
    )


def ts_video_span(index: TsIndex, frame_duration: float | None = None) -> tuple[float, float]:
    """색인의 영상이 차지하는 시각 [가장 이른 PTS, 가장 늦은 PTS + 프레임 길이)를 돌려준다.

    세그먼트 하나의 색인으로 부르면 그 세그먼트가 실제로 놓인 시각이다(VOD 시작 = 0 기준,
    초). 이어지는 세그먼트의 범위는 틈도 겹침도 없이 맞닿는다 — 앞 세그먼트의 끝이 뒤
    세그먼트의 시작이다.

    Args:
        index: ``build_ts_index``의 결과
        frame_duration: 프레임 길이(초). 주지 않으면 색인이 잰 값을 쓴다. 프레임이 하나뿐인
            세그먼트는 색인에 잰 값이 없으므로 다른 세그먼트에서 잰 값을 준다

    Raises:
        TsError: 프레임 길이를 주지도 않았고 색인에도 없는 경우(``TS_UNSUPPORTED``)
    """
    length = index.frame_duration if frame_duration is None else frame_duration
    if length is None:
        raise TsError(TS_UNSUPPORTED, "프레임이 하나뿐이라 프레임 길이를 잴 수 없다")
    return index.frame_pts[0], index.frame_pts[-1] + length


def ts_audio_span(
    streams: TsStreams, origin: int, expected_start: float | None = None
) -> tuple[float, float | None] | None:
    """오디오가 차지하는 시각 (첫 PES의 PTS, 끝나는 시각)을 돌려준다 — VOD 시작 = 0 기준 초.

    영상 프레임이 없는 세그먼트(오디오만 든 마지막 세그먼트 등)가 놓인 시각을 잴 때 쓴다.
    ``build_ts_index``는 그런 세그먼트를 거부한다. 시각을 맞추는 방법(origin ·
    ``expected_start`` · 랩어라운드)은 ``build_ts_index``와 같다.

    Returns:
        (시작, 끝). 끝은 마지막 PES의 프레임 수를 읽지 못했으면 None이다. 오디오 PES가
        하나도 없으면 None
    """
    if not streams.audio_pts:
        return None
    audio = _unwrap(streams.audio_pts, origin, expected_start)
    return min(audio) / TS_CLOCK, _audio_end(streams, audio)


def join_ts_streams(parts: Sequence[TsStreams]) -> TsStreams:
    """세그먼트마다 읽은 ``TsStreams``를 준 순서대로 하나로 잇는다.

    세그먼트의 bytes를 이어 붙여 ``parse_ts``에 넣은 것과 같은 결과다 — 세그먼트를 이미
    읽어 둔 쪽이 다시 읽지 않고 여러 세그먼트의 색인을 만들 때 쓴다.

    오디오 프레임 수(``audio_frames``)는 모든 조각이 PES마다의 값을 갖고 있을 때만 잇는다.
    하나라도 없으면 빈 튜플이다. 표본화율은 값을 가진 마지막 조각의 것이다.
    """
    video_pts: list[int] = []
    video_dts: list[int] = []
    keyframes: list[int] = []
    audio_pts: list[int] = []
    audio_frames: list[int] = []
    counted = all(len(part.audio_frames) == len(part.audio_pts) for part in parts)
    rate: int | None = None
    for part in parts:
        keyframes += [len(video_pts) + key for key in part.video_keyframes]
        video_pts += part.video_pts
        video_dts += part.video_dts
        audio_pts += part.audio_pts
        if counted:
            audio_frames += part.audio_frames
        if part.audio_sample_rate is not None:
            rate = part.audio_sample_rate
    return TsStreams(
        video_pts=tuple(video_pts),
        video_dts=tuple(video_dts),
        video_keyframes=tuple(keyframes),
        audio_pts=tuple(audio_pts),
        audio_frames=tuple(audio_frames),
        audio_sample_rate=rate,
    )


# ================================================================ 내부


def _unwrap(stamps: Sequence[int], origin: int, expected_start: float | None) -> list[int]:
    """33비트 원시 타임스탬프를 origin = 0 기준의 이어지는 틱으로 푼다.

    첫 값은 origin과의 차이를 0 이상으로 읽고(origin보다 최대 10초 앞선 값만 음수),
    ``expected_start``가 있으면 랩어라운드 횟수를 그 시각에 가장 가깝게 맞춘다. 그 뒤의
    값은 앞 값과의 차이로 잇는다.
    """
    first = (stamps[0] - origin) % _WRAP
    if first > _WRAP - _MAX_LEAD_TICKS:
        first -= _WRAP
    if expected_start is not None:
        first += round((expected_start * TS_CLOCK - first) / _WRAP) * _WRAP
    ticks = [first]
    for previous, current in zip(stamps, stamps[1:]):
        ticks.append(ticks[-1] + _nearest(current - previous))
    return ticks


def _frame_ticks(present: list[int]) -> int | None:
    """표시 순서의 PTS(틱)에서 가장 많은 간격을 고른다. 수가 같으면 짧은 쪽이다.

    Returns:
        프레임 하나의 길이(틱). 프레임이 하나뿐이거나 간격이 모두 0이면 None
    """
    gaps = Counter(b - a for a, b in zip(present, present[1:]) if b > a)
    if not gaps:
        return None
    return min(gaps, key=lambda gap: (-gaps[gap], gap))


def _audio_end(streams: TsStreams, audio: list[int]) -> float | None:
    """오디오가 끝나는 시각(초)을 구한다 — 마지막 오디오 PES의 PTS + 그 PES에 든 프레임의 길이.

    Args:
        streams: ``parse_ts``의 결과
        audio: 랩어라운드를 푼 오디오 PES의 PTS(틱), ``streams.audio_pts``와 같은 순서

    Returns:
        끝나는 시각. 오디오가 없거나, 프레임 수·표본화율을 읽지 못했으면 None
    """
    if not audio or len(streams.audio_frames) != len(audio) or not streams.audio_sample_rate:
        return None
    frames = streams.audio_frames[-1]
    if frames <= 0:
        return None
    return audio[-1] / TS_CLOCK + frames * _AAC_FRAME_SAMPLES / streams.audio_sample_rate


def _nearest(delta: int) -> int:
    """33비트 틱 차이를 가장 가까운 부호 있는 값으로 — (−2³², 2³²] 범위."""
    delta %= _WRAP
    return delta - _WRAP if delta > _WRAP // 2 else delta


def _section(payload: bytes, table_id: int) -> tuple[int, int]:
    """PSI 섹션의 (본문 시작, CRC 직전 끝)을 돌려준다. pointer_field를 건너뛴다."""
    start = 1 + payload[0]
    if start + 3 > len(payload) or payload[start] != table_id:
        raise TsError(TS_UNSUPPORTED, f"table_id {table_id} 섹션이 아니다")
    length = ((payload[start + 1] & 0x0F) << 8) | payload[start + 2]
    end = start + 3 + length - 4
    if end > len(payload):
        raise TsError(TS_UNSUPPORTED, "PSI 섹션이 패킷 하나를 넘는다")
    return start, end


def _program_map_pid(payload: bytes) -> int:
    """PAT에서 첫 프로그램의 PMT PID를 읽는다."""
    start, end = _section(payload, 0x00)
    for entry in range(start + 8, end - 3, 4):
        program = (payload[entry] << 8) | payload[entry + 1]
        if program:  # 0번은 NIT다
            return ((payload[entry + 2] & 0x1F) << 8) | payload[entry + 3]
    raise TsError(TS_UNSUPPORTED, "PAT에 프로그램이 없다")


def _elementary_pids(payload: bytes) -> tuple[int, int, str, int]:
    """PMT에서 (영상 PID, 오디오 PID, 영상 코덱, 오디오 stream_type)을 읽는다. 없는 쪽은 −1이다."""
    start, end = _section(payload, 0x02)
    if end < start + 12:
        # 스트림 목록 앞의 고정 칸(12바이트)도 다 없다 — 그대로 읽으면 IndexError가 난다
        raise TsError(TS_INVALID, "PMT 섹션이 너무 짧다")
    position = start + 12 + (((payload[start + 10] & 0x0F) << 8) | payload[start + 11])
    video_pid = audio_pid = audio_type = -1
    codec = ""
    while position + 5 <= end:
        stream_type = payload[position]
        pid = ((payload[position + 1] & 0x1F) << 8) | payload[position + 2]
        if video_pid < 0 and stream_type in _VIDEO_STREAM_TYPES:
            video_pid, codec = pid, _VIDEO_STREAM_TYPES[stream_type]
        elif audio_pid < 0 and stream_type in _AUDIO_STREAM_TYPES:
            audio_pid, audio_type = pid, stream_type
        position += 5 + (((payload[position + 3] & 0x0F) << 8) | payload[position + 4])
    return video_pid, audio_pid, codec, audio_type


def _adts_frames(pes: bytes, start: int) -> tuple[int, int | None]:
    """오디오 PES 본문의 ADTS 머리를 따라가 (AAC 프레임 수, 표본화율)을 센다.

    머리의 프레임 길이만큼 건너뛰며 센다 — 본문을 훑어 동기 워드를 찾지 않는다. ADTS
    프레임 하나에 AAC 프레임(raw data block)이 여럿 들 수 있어 머리에 적힌 수만큼 센다.
    본문이 PES 끝에서 잘린 프레임도 이 PES에서 시작했으므로 센다.

    Returns:
        (프레임 수, 표본화율). 본문이 ADTS 머리로 시작하지 않으면 (0, None)
    """
    frames = 0
    rate: int | None = None
    position = start
    while position + _ADTS_HEADER_SIZE <= len(pes):
        # 동기 워드 12비트(0xFFF) + layer 2비트(00)
        if pes[position] != 0xFF or pes[position + 1] & 0xF6 != 0xF0:
            break
        rate_index = (pes[position + 2] >> 2) & 0x0F
        length = (pes[position + 3] & 0x03) << 11 | pes[position + 4] << 3 | pes[position + 5] >> 5
        if rate_index >= len(_ADTS_SAMPLE_RATES) or length < _ADTS_HEADER_SIZE:
            break
        if rate is None:
            rate = _ADTS_SAMPLE_RATES[rate_index]
        frames += (pes[position + 6] & 0x03) + 1
        position += length
    return frames, rate


def _pes_timestamps(pes: bytes) -> tuple[int, int, int]:
    """PES 머리에서 (PTS, DTS, 본문 시작 위치)를 읽는다. DTS가 없으면 PTS와 같다."""
    if len(pes) < 14 or pes[:3] != _START_CODE:
        raise TsError(TS_INVALID, "PES 시작 코드가 없다")
    marker = pes[7] >> 6
    if not marker & 0x2:
        raise TsError(TS_UNSUPPORTED, "PES에 PTS가 없다")
    pts = _timestamp(pes, 9)
    dts = pts
    if marker == 0x3:
        if len(pes) < _PES_TIMESTAMP_END:
            raise TsError(TS_INVALID, "PES 머리가 잘렸다")
        dts = _timestamp(pes, 14)
    return pts, dts, 9 + pes[8]


def _timestamp(pes: bytes, at: int) -> int:
    """PES 머리의 5바이트 타임스탬프(33비트)를 읽는다."""
    return (
        ((pes[at] >> 1) & 0x07) << 30
        | pes[at + 1] << 22
        | (pes[at + 2] >> 1) << 15
        | pes[at + 3] << 7
        | pes[at + 4] >> 1
    )


def _starts_with_keyframe(pes: bytes, start: int, codec: str) -> bool:
    """PES 본문의 첫 화면 데이터 NAL이 키프레임(H.264 IDR · HEVC IRAP)인지 본다.

    한 프레임의 화면 데이터 NAL은 모두 같은 종류이므로 첫 것만 보면 된다 —
    프레임 전체를 훑지 않는다.
    """
    if codec not in ("h264", "hevc"):
        return False
    position = pes.find(_START_CODE, start)
    while 0 <= position < len(pes) - 3:
        header = pes[position + 3]
        if codec == "h264":
            nal_type = header & 0x1F
            if 1 <= nal_type <= 5:
                return nal_type == 5
        else:
            nal_type = (header >> 1) & 0x3F
            if nal_type < 32:
                return 16 <= nal_type <= 23
        position = pes.find(_START_CODE, position + 3)
    return False
