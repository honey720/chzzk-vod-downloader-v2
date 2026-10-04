"""HLS 미디어 플레이리스트 파싱 — 세그먼트 목록과 암호화 키 정보 (#57, SPEC §8.1).

AES(SEA) VOD의 비디오 Representation은 DASH 매니페스트에 BaseURL 없이
``nvod:m3u``(HLS 미디어 플레이리스트)만 갖는다. 실제 세그먼트 목록과 복호화
키의 위치는 그 플레이리스트의 태그에 있으므로 여기서 파싱한다.

HTTP는 하지 않는다 — 텍스트를 받아 파싱만 하는 순수 함수다(테스트 용이성,
core 순수성). 요청·쿠키 처리는 호출부(다운로더·리졸버)의 몫이다.

IV 규칙 (RFC 8216 §5.2): ``#EXT-X-KEY``에 ``IV`` 속성이 있으면 그 값을 쓰고,
없으면 **각 세그먼트의 미디어 시퀀스 번호**를 128비트 빅엔디언으로 쓴다.
치지직 실측(videoId 54D17299…)은 IV 속성이 없어 후자에 해당한다.
"""

import math
import re
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

# #EXT-X-KEY:METHOD=AES-128,URI="...",IV=0x... 의 속성 추출용
_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _parse_attributes(tag_value: str) -> dict[str, str]:
    """태그 속성 목록(``KEY=VALUE,KEY="VALUE"``)을 dict로 만든다."""
    return {m.group(1): m.group(2).strip('"') for m in _ATTR_RE.finditer(tag_value)}


@dataclass(frozen=True)
class HlsKey:
    """``#EXT-X-KEY``가 기술하는 세그먼트 암호화 정보.

    iv가 None이면 세그먼트의 미디어 시퀀스 번호를 IV로 쓴다(RFC 8216 §5.2).
    """

    method: str
    uri: str
    iv: bytes | None = None

    @property
    def is_aes_128(self) -> bool:
        """AES-128 (HLS 표준 세그먼트 암호화)인지."""
        return self.method == "AES-128"


@dataclass(frozen=True)
class HlsPlaylist:
    """HLS 미디어 플레이리스트의 파싱 결과."""

    # 세그먼트 상대/절대 경로 목록 (플레이리스트 등장 순서 = 재생 순서)
    segments: tuple[str, ...]
    # 첫 세그먼트의 미디어 시퀀스 번호 (#EXT-X-MEDIA-SEQUENCE, 없으면 0)
    media_sequence: int = 0
    # 암호화 정보. 평문 플레이리스트면 None
    key: HlsKey | None = None
    # 초기화 세그먼트(#EXT-X-MAP URI). TS 플레이리스트에는 없다
    init_uri: str | None = None
    # 세그먼트별 길이(초, #EXTINF). segments와 같은 순서·같은 개수다 —
    # #EXTINF가 없는 세그먼트는 0.0이다. 직접 만든 객체는 기본값(빈 튜플)일 수 있다 (#309)
    durations: tuple[float, ...] = ()
    # 바로 앞에 #EXT-X-DISCONTINUITY가 놓인 세그먼트의 인덱스(오름차순). 그 세그먼트부터
    # 타임스탬프가 앞과 이어지지 않는다 (#309)
    discontinuities: tuple[int, ...] = ()
    # 플레이리스트에 나온 #EXT-X-MAP URI를 나온 순서대로, 겹치지 않게. 둘 이상이면 초기화
    # 세그먼트가 중간에 바뀌는 플레이리스트다 (#309)
    init_uris: tuple[str, ...] = ()
    # 세그먼트마다 바로 앞에 적힌 #EXT-X-PROGRAM-DATE-TIME(유닉스 시각, 초). segments와 같은
    # 순서·같은 개수이고 태그가 없는 세그먼트는 None이다. 직접 만든 객체는 빈 튜플일 수 있다.
    # 세그먼트가 실제로 놓인 시각의 추정에 쓴다 — #EXTINF의 누적은 실제 시각과 벌어질 수 있다 (#309)
    program_times: tuple[float | None, ...] = ()

    def sequence_of(self, index: int) -> int:
        """index번째 세그먼트의 미디어 시퀀스 번호 (IV 유도에 쓰인다)."""
        return self.media_sequence + index

    @property
    def duration(self) -> float:
        """플레이리스트가 말하는 영상 길이(초)를 반환한다 — #EXTINF의 합이다 (#309).

        세그먼트 길이는 고르지 않다(마지막 세그먼트는 보통 짧다). 세그먼트 수에
        목표 길이를 곱한 값이 아니다.
        """
        return math.fsum(self.durations)


def parse_media_playlist(text: str) -> HlsPlaylist:
    """HLS 미디어 플레이리스트 텍스트에서 세그먼트·키·시퀀스 정보를 뽑는다.

    ``#EXT-X-KEY``가 여러 번 나오면 마지막 것을 쓴다 — 치지직 SEA는 비디오당
    키 1개(로테이션 없음)라 실질적으로 하나뿐이다. METHOD=NONE은 암호화
    해제 지시이므로 키 없음으로 처리한다.

    Args:
        text: 플레이리스트 본문

    Returns:
        HlsPlaylist: 세그먼트 목록과 암호화 정보
    """
    segments: list[str] = []
    durations: list[float] = []
    pending_duration = 0.0  # 다음 세그먼트에 붙을 #EXTINF 길이
    media_sequence = 0
    key: HlsKey | None = None
    init_uri: str | None = None
    init_uris: list[str] = []
    discontinuities: list[int] = []
    program_times: list[float | None] = []
    pending_time: float | None = None  # 다음 세그먼트에 붙을 #EXT-X-PROGRAM-DATE-TIME

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            segments.append(line)
            durations.append(pending_duration)
            pending_duration = 0.0
            program_times.append(pending_time)
            pending_time = None
        elif line.startswith("#EXTINF:"):
            pending_duration = _parse_extinf(line)
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1].strip())
        elif line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attributes(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE")
            if method == "NONE":
                key = None
            else:
                iv_text = attrs.get("IV")
                key = HlsKey(
                    method=method,
                    uri=attrs.get("URI", ""),
                    iv=bytes.fromhex(iv_text[2:]) if iv_text else None,
                )
        elif line.startswith("#EXT-X-MAP:"):
            init_uri = _parse_attributes(line.split(":", 1)[1]).get("URI")
            if init_uri and init_uri not in init_uris:
                init_uris.append(init_uri)
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_time = _parse_date_time(line.split(":", 1)[1])
        elif line == "#EXT-X-DISCONTINUITY":
            # 다음에 나올 세그먼트의 인덱스다. 첫 세그먼트 앞의 것은 끊길 앞이 없어 적지 않는다
            if segments and len(segments) not in discontinuities:
                discontinuities.append(len(segments))

    return HlsPlaylist(
        segments=tuple(segments),
        media_sequence=media_sequence,
        key=key,
        init_uri=init_uri,
        durations=tuple(durations),
        discontinuities=tuple(discontinuities),
        init_uris=tuple(init_uris),
        program_times=tuple(program_times),
    )


def _parse_date_time(text: str) -> float | None:
    """#EXT-X-PROGRAM-DATE-TIME의 값(ISO 8601)을 유닉스 시각(초)으로 — 읽을 수 없으면 None."""
    try:
        return datetime.fromisoformat(text.strip()).timestamp()
    except ValueError:
        return None


def stream_frame_rate(line: str) -> Fraction | None:
    """마스터 플레이리스트의 ``#EXT-X-STREAM-INF`` 줄에서 FRAME-RATE를 정확한 비로 읽는다 (#309).

    선언한 글자 그대로의 유리수다("60.000" → 60, "59.940" → 2997/50) — 다른 값으로
    바꾸지 않는다(``core.utils.timecode.frame_rate``와 같은 원칙).

    Returns:
        프레임률. 그 줄에 속성이 없거나 0 이하이거나 수가 아니면 None
    """
    if not line.startswith("#EXT-X-STREAM-INF:"):
        return None
    text = _parse_attributes(line.split(":", 1)[1]).get("FRAME-RATE")
    if not text:
        return None
    try:
        rate = Fraction(text.strip())
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def variant_frame_rate(text: str, uri: str) -> Fraction | None:
    """마스터 플레이리스트에서 주소가 uri인 변형의 FRAME-RATE를 정확한 비로 읽는다 (#309).

    변형을 고르는 것은 core/api/playback_tracks.py다(#318). 여기서는 고른 변형의
    ``#EXT-X-STREAM-INF`` 줄을 다시 찾아 읽기만 한다 — 같은 주소의 변형이 여럿이면 먼저
    나온 것의 값이다.

    Args:
        text: 마스터 플레이리스트
        uri: 고른 변형의 주소 — 태그 다음 줄에 적힌 그대로(상대일 수 있다)

    Returns:
        프레임률. 그 변형이 없거나 FRAME-RATE를 선언하지 않았으면 None
    """
    lines = [line.strip() for line in text.splitlines()]
    for number, line in enumerate(lines[:-1]):
        if line.startswith("#EXT-X-STREAM-INF:") and lines[number + 1] == uri:
            return stream_frame_rate(line)
    return None


def _parse_extinf(line: str) -> float:
    """``#EXTINF:<길이>,<제목>``에서 길이(초)를 읽는다. 읽을 수 없거나 음수면 0.0이다."""
    value = line.split(":", 1)[1].split(",", 1)[0].strip()
    try:
        seconds = float(value)
    except ValueError:
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0
