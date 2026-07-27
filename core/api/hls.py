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

import re
from dataclasses import dataclass

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

    def sequence_of(self, index: int) -> int:
        """index번째 세그먼트의 미디어 시퀀스 번호 (IV 유도에 쓰인다)."""
        return self.media_sequence + index


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
    media_sequence = 0
    key: HlsKey | None = None
    init_uri: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            segments.append(line)
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

    return HlsPlaylist(
        segments=tuple(segments),
        media_sequence=media_sequence,
        key=key,
        init_uri=init_uri,
    )
