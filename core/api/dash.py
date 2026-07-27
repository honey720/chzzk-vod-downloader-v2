"""DASH 매니페스트(XML) 파싱 유틸.

content/network.py의 NetworkManager.get_video_dash_manifest에서 HTTP를 떼어 내고
파싱만 이동한 순수 함수 (#51). 동작은 기존과 완전 동일하다.

AES(SEA) 암호화 매니페스트는 parse_sea_manifest가 따로 다룬다 (#57) —
평문 경로의 parse_dash_manifest는 기존 방어 동작(BaseURL 없는 항목 스킵)을
그대로 유지한다.
"""

import xml.etree.ElementTree as ET

NS = {
    "mpd": "urn:mpeg:dash:schema:mpd:2011",
    "nvod": "urn:naver:vod:2020",
    "sea": "urn:mpeg:dash:schema:sea:2012",
}

# MPEG-DASH SEA (ISO/IEC 23009-4) — 세그먼트 단위 암호화 스킴
SEA_SCHEME_ID = "urn:mpeg:dash:sea:2012"
# 지원하는 암호화 방식: AES-128-CBC (HLS METHOD=AES-128과 동형)
SEA_AES_128_CBC = "urn:mpeg:dash:sea:aes128-cbc:2013"
# 지원하는 키 시스템: 인증된 세션에 평문 HTTP로 키를 내주는 방식
SEA_KEYSYS_HTTP = "urn:mpeg:dash:sea:keysys:http:2013"


def parse_dash_manifest(xml_text: str) -> tuple[list[list], int, str]:
    """
    DASH 매니페스트 XML 문자열에서 Representation 목록을 파싱한다.

    해상도는 min(width, height)로 계산하고 오름차순으로 정렬한다.
    BaseURL이 '/hls/'로 끝나는 항목은 스킵한다.

    Args:
        xml_text (str): DASH 매니페스트 XML 문자열

    Returns:
        tuple[list[list], int | None, str | None]: ([해상도, base_url] 목록(오름차순),
            auto 해상도(최고), auto base_url) 형식의 튜플.
            사용 가능한 Representation이 하나도 없으면 ([], None, None)
    """
    root = ET.fromstring(xml_text)
    ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
    reps = []
    for rep in root.findall(".//mpd:Representation", namespaces=ns):
        width = rep.get('width')
        height = rep.get('height')
        # 오디오 전용 Representation(audio/mp4)은 width/height 속성이 없다.
        # 해상도를 계산할 수 없으므로 목록에서 제외한다 (#38)
        if width is None or height is None:
            continue
        resolution = min(int(width), int(height))
        # AES(SEA) 암호화 매니페스트의 비디오 Representation은 BaseURL 없이
        # ContentProtection만 갖는다. 직접 URL이 없어 다운로드할 수 없으므로
        # 크래시 대신 목록에서 제외한다 (#55) — 1차 방어는 worker의 encryptionType 검사
        base_url_el = rep.find(".//mpd:BaseURL", namespaces=ns)
        if base_url_el is None or not base_url_el.text:
            continue
        base_url = base_url_el.text
        if base_url.endswith('/hls/'):
            continue
        reps.append([resolution, base_url])

    if not reps:
        return [], None, None

    sorted_reps = sorted(reps, key=lambda x: x[0])
    auto_resolution = sorted_reps[-1][0]
    auto_base_url = sorted_reps[-1][1]

    return sorted_reps, auto_resolution, auto_base_url


def is_supported_sea(xml_text: str) -> bool:
    """이 매니페스트가 **지원하는** SEA 암호화(AES-128-CBC + HTTP 키)인지 판정한다 (#57).

    라이선스 서버형 DRM(cenc:pssh, Widevine·PlayReady UUID)은 여기서 False가
    되어 기존의 "지원하지 않음" 안내로 떨어진다 — 그런 보호조치의 우회는
    구현하지 않는다.
    """
    root = ET.fromstring(xml_text)
    found = False
    for cp in root.findall(".//mpd:ContentProtection", namespaces=NS):
        if cp.get("schemeIdUri") != SEA_SCHEME_ID:
            # SEA 외의 ContentProtection(=DRM 계열)이 하나라도 있으면 지원 대상이 아니다
            return False
        enc = cp.find("sea:SegmentEncryption", namespaces=NS)
        keysys = cp.find("sea:KeySystem", namespaces=NS)
        if enc is None or keysys is None:
            return False
        if enc.get("encryptionSystemUrn") != SEA_AES_128_CBC:
            return False
        if keysys.get("keySystemUrn") != SEA_KEYSYS_HTTP:
            return False
        found = True
    return found


def parse_sea_manifest(xml_text: str) -> tuple[list[list], int | None, str | None]:
    """SEA 암호화 매니페스트에서 비디오 Representation 목록을 파싱한다 (#57).

    암호화된 비디오 Representation은 BaseURL 없이 ``nvod:m3u``(HLS 미디어
    플레이리스트 URL)만 갖는다 — 세그먼트 목록과 키 위치가 그 플레이리스트에
    있기 때문이다. 따라서 평문 경로와 달리 nvod:m3u를 base_url로 삼는다.

    반환 형식은 parse_dash_manifest와 동일해 호출부가 같은 모양으로 쓴다.

    Returns:
        tuple: ([해상도, media.m3u8 URL] 목록(오름차순), auto 해상도, auto URL).
            암호화 비디오 Representation이 없으면 ([], None, None)
    """
    root = ET.fromstring(xml_text)
    reps = []
    for rep in root.findall(".//mpd:Representation", namespaces=NS):
        if rep.find("mpd:ContentProtection", namespaces=NS) is None:
            # 오디오 등 비암호화 트랙은 이 경로의 대상이 아니다
            continue
        width, height = rep.get("width"), rep.get("height")
        if width is None or height is None:
            continue
        m3u = rep.get(f"{{{NS['nvod']}}}m3u")
        if not m3u:
            continue
        reps.append([min(int(width), int(height)), m3u])

    if not reps:
        return [], None, None

    sorted_reps = sorted(reps, key=lambda x: x[0])
    return sorted_reps, sorted_reps[-1][0], sorted_reps[-1][1]
