"""테스트용 합성 TS 조립기 — 188바이트 패킷을 직접 써서 작은 TS 바이트를 만든다 (#309).

제품 코드(core.api.mpegts)를 쓰지 않는다.
"""

from dataclasses import dataclass

PACKET = 188
PMT_PID = 0x1000
VIDEO_PID = 0x0100
AUDIO_PID = 0x0101

H264_IDR = b"\x00\x00\x01\x65"  # IDR 화면 데이터 NAL (nal_unit_type 5)
H264_NON_IDR = b"\x00\x00\x01\x41"  # IDR이 아닌 화면 데이터 NAL (nal_unit_type 1)
H264_AUD = b"\x00\x00\x01\x09\xf0"  # 액세스 유닛 구분 NAL


@dataclass
class Frame:
    """영상 프레임 하나의 재료."""

    pts: int  # 33비트 틱
    dts: int | None = None  # None이면 PES에 DTS를 쓰지 않는다
    idr: bool = False  # 본문에 IDR NAL을 넣는다
    random_access: bool = False  # 적응 필드에 랜덤 액세스 표시를 켠다
    filler: int = 0  # 화면 데이터 NAL 앞에 넣을 채움 바이트 수 — PES를 여러 패킷으로 늘린다
    size: int = 40  # 화면 데이터 NAL 뒤 본문 바이트 수
    nal: bytes | None = (
        None  # 화면 데이터 NAL을 직접 준다(시작 코드 포함). None이면 idr에 따라 고른다
    )


def _packet(
    pid: int, payload: bytes, *, unit_start: bool, counter: int, random_access: bool = False
) -> bytes:
    """TS 패킷 하나. 본문이 184바이트보다 짧으면 적응 필드로 채운다."""
    assert len(payload) <= PACKET - 4
    header = bytes([0x47, (0x40 if unit_start else 0) | (pid >> 8), pid & 0xFF])
    stuffing = PACKET - 4 - len(payload)
    if random_access and stuffing < 2:
        raise AssertionError("랜덤 액세스 표시를 넣을 자리가 없다 — 본문을 182바이트 이하로 준다")
    if stuffing == 0:
        return header + bytes([0x10 | counter]) + payload
    # 적응 필드: 길이(1) + 플래그(1) + 채움
    if stuffing == 1:
        field = bytes([0])
    else:
        field = bytes([stuffing - 1, 0x40 if random_access else 0x00]) + b"\xff" * (stuffing - 2)
    return header + bytes([0x30 | counter]) + field + payload


def _psi(table_id: int, body: bytes) -> bytes:
    """PSI 섹션 본문 — pointer_field + table_id + section_length + body + CRC 자리(4)."""
    length = len(body) + 4
    return bytes([0x00, table_id, 0xB0 | (length >> 8), length & 0xFF]) + body + bytes(4)


def pat_packet(pmt_pid: int = PMT_PID) -> bytes:
    body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00]) + bytes(
        [0x00, 0x01, 0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF]
    )
    return _packet(0, _psi(0x00, body), unit_start=True, counter=0)


def pmt_packet(
    video_pid: int | None = VIDEO_PID, audio_pid: int | None = AUDIO_PID, video_type: int = 0x1B
) -> bytes:
    body = bytes(
        [0x00, 0x01, 0xC1, 0x00, 0x00, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00]
    )
    if video_pid is not None:
        body += bytes([video_type, 0xE0 | (video_pid >> 8), video_pid & 0xFF, 0xF0, 0x00])
    if audio_pid is not None:
        body += bytes([0x0F, 0xE0 | (audio_pid >> 8), audio_pid & 0xFF, 0xF0, 0x00])
    return _packet(PMT_PID, _psi(0x02, body), unit_start=True, counter=0)


def _stamp(prefix: int, value: int) -> bytes:
    """PES 머리의 5바이트 타임스탬프."""
    return bytes([
        (prefix << 4) | ((value >> 30) & 0x07) << 1 | 1,
        (value >> 22) & 0xFF,
        ((value >> 15) & 0x7F) << 1 | 1,
        (value >> 7) & 0xFF,
        (value & 0x7F) << 1 | 1,
    ])  # fmt: skip


def pes(stream_id: int, pts: int, dts: int | None, payload: bytes) -> bytes:
    """PES 패킷 — 시작 코드 + stream_id + 길이(0) + 플래그 + 타임스탬프 + 본문."""
    if dts is None:
        stamps, flags = _stamp(0x2, pts), 0x80
    else:
        stamps, flags = _stamp(0x3, pts) + _stamp(0x1, dts), 0xC0
    return (
        b"\x00\x00\x01"
        + bytes([stream_id, 0x00, 0x00, 0x80, flags, len(stamps)])
        + stamps
        + payload
    )


def packetize(
    pid: int, data: bytes, *, random_access: bool = False, first_payload: int = 184
) -> bytes:
    """PES bytes를 TS 패킷들로 나눈다. 첫 패킷의 본문 크기를 줄일 수 있다."""
    if random_access:
        first_payload = min(first_payload, 182)
    packets = b""
    position, counter = 0, 0
    while position < len(data):
        room = first_payload if position == 0 else 184
        chunk = data[position : position + room]
        packets += _packet(pid, chunk, unit_start=position == 0, counter=counter,
                           random_access=random_access and position == 0)  # fmt: skip
        position += len(chunk)
        counter = (counter + 1) % 16
    return packets


def video_frame(frame: Frame, *, first_payload: int = 184) -> bytes:
    """프레임 하나를 영상 PES로 만들어 패킷으로 나눈다."""
    nal = frame.nal or (H264_IDR if frame.idr else H264_NON_IDR)
    # 채움은 SEI NAL(type 6) 본문으로 넣는다 — 0xFF라 시작 코드가 생기지 않는다
    filler = b"\x00\x00\x01\x06" + b"\xff" * frame.filler if frame.filler else b""
    # H.264의 구분 NAL은 nal을 직접 준 프레임(다른 코덱)에는 붙이지 않는다
    payload = (b"" if frame.nal else H264_AUD) + filler + nal + b"\xaa" * frame.size
    return packetize(VIDEO_PID, pes(0xE0, frame.pts, frame.dts, payload),
                     random_access=frame.random_access, first_payload=first_payload)  # fmt: skip


def audio_pes(pts: int, size: int = 30) -> bytes:
    """오디오 PES 하나를 패킷으로 나눈다."""
    return packetize(AUDIO_PID, pes(0xC0, pts, None, b"\xbb" * size))


def build_ts(frames: list[Frame], audio: list[int] | None = None, **pmt) -> bytes:
    """PAT · PMT 뒤에 영상 프레임과 오디오 PES를 놓은 TS bytes."""
    data = pat_packet() + pmt_packet(**pmt)
    for frame in frames:
        data += video_frame(frame)
    for pts in audio or []:
        data += audio_pes(pts)
    return data
