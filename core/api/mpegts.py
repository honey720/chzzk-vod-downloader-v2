"""MPEG-TS 세그먼트에서 프레임 시각·키프레임 읽기 (#309).

암호화 VOD(hls_aes 경로)의 세그먼트 형식이 MPEG-TS다. 라이브 다시보기(m3u8
경로)는 fMP4 세그먼트라 이 모듈의 입력이 아니다.

TS 세그먼트에는 mp4의 moov 같은 색인이 없다. 프레임 시각과 키프레임 위치는
세그먼트를 받아 패킷을 따라가야 알 수 있다. 이 모듈은 받은 bytes에서 그 정보를
뽑는 순수 함수다 — 네트워크를 모른다.

두 단계로 나뉜다.

- ``parse_ts`` — 188바이트 패킷을 따라가 PAT → PMT로 영상·오디오 PID를 찾고,
  PES 머리의 PTS·DTS와 키프레임 여부를 읽는다. 값은 읽은 그대로의 33비트 틱이다
- ``build_ts_index`` — VOD 시작을 0으로 맞추고 33비트 랩어라운드를 풀어
  초 단위 색인(``TsIndex``)을 만든다

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

    Raises:
        TsError: 길이가 188의 배수가 아니거나 동기 바이트가 틀린 경우(``TS_INVALID``),
            PAT·PMT가 없거나 영상 스트림이 없거나 PES에 PTS가 없는 경우(``TS_UNSUPPORTED``)
    """
    if not data or len(data) % TS_PACKET_SIZE:
        raise TsError(TS_INVALID, f"길이 {len(data)}바이트는 {TS_PACKET_SIZE}의 배수가 아니다")

    pmt_pid = video_pid = audio_pid = -1
    codec = ""
    video_pts: list[int] = []
    video_dts: list[int] = []
    keyframes: list[int] = []
    audio_pts: list[int] = []
    video_chunks: list[bytes] = []  # 지금 모으는 영상 PES의 조각
    video_random_access = False
    audio_head = b""  # 지금 모으는 오디오 PES의 앞부분(머리만 필요하다)

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
        if audio_head:
            audio_pts.append(_pes_timestamps(audio_head)[0])

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
                video_pid, audio_pid, codec = _elementary_pids(payload)
        elif pid == video_pid:
            if unit_start:
                finish_video()
                video_chunks = [payload]
                video_random_access = random_access
            elif video_chunks:
                video_chunks.append(payload)
        elif unit_start:
            finish_audio()
            audio_head = payload
        elif audio_head and len(audio_head) < _PES_TIMESTAMP_END:
            audio_head += payload

    finish_video()
    finish_audio()
    if pmt_pid < 0 or video_pid < 0:
        raise TsError(TS_UNSUPPORTED, "PAT·PMT에서 영상 스트림을 찾지 못했다")
    return TsStreams(
        video_pts=tuple(video_pts),
        video_dts=tuple(video_dts),
        video_keyframes=tuple(keyframes),
        audio_pts=tuple(audio_pts),
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

    def first_ticks(stamp: int) -> int:
        ticks = (stamp - origin) % _WRAP
        if ticks > _WRAP - _MAX_LEAD_TICKS:
            ticks -= _WRAP
        if expected_start is not None:
            ticks += round((expected_start * TS_CLOCK - ticks) / _WRAP) * _WRAP
        return ticks

    def unwrap(stamps: tuple[int, ...]) -> list[int]:
        ticks = [first_ticks(stamps[0])]
        for previous, current in zip(stamps, stamps[1:]):
            ticks.append(ticks[-1] + _nearest(current - previous))
        return ticks

    decode = unwrap(streams.video_dts)
    present = [
        dts + _nearest(pts - raw_dts)
        for dts, pts, raw_dts in zip(decode, streams.video_pts, streams.video_dts)
    ]
    order = sorted(range(len(present)), key=present.__getitem__)
    key_samples = set(streams.video_keyframes)
    return TsIndex(
        frame_pts=tuple(present[sample] / TS_CLOCK for sample in order),
        frame_samples=tuple(order),
        keyframes=tuple(number for number, sample in enumerate(order) if sample in key_samples),
        decode_times=tuple(ticks / TS_CLOCK for ticks in decode),
        audio_pts=tuple(ticks / TS_CLOCK for ticks in unwrap(streams.audio_pts))
        if streams.audio_pts
        else (),
    )


# ================================================================ 내부


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


def _elementary_pids(payload: bytes) -> tuple[int, int, str]:
    """PMT에서 (영상 PID, 오디오 PID, 영상 코덱)을 읽는다. 없는 쪽은 −1이다."""
    start, end = _section(payload, 0x02)
    position = start + 12 + (((payload[start + 10] & 0x0F) << 8) | payload[start + 11])
    video_pid = audio_pid = -1
    codec = ""
    while position + 5 <= end:
        stream_type = payload[position]
        pid = ((payload[position + 1] & 0x1F) << 8) | payload[position + 2]
        if video_pid < 0 and stream_type in _VIDEO_STREAM_TYPES:
            video_pid, codec = pid, _VIDEO_STREAM_TYPES[stream_type]
        elif audio_pid < 0 and stream_type in _AUDIO_STREAM_TYPES:
            audio_pid = pid
        position += 5 + (((payload[position + 3] & 0x0F) << 8) | payload[position + 4])
    return video_pid, audio_pid, codec


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
