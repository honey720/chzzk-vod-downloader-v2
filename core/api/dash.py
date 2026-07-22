"""DASH 매니페스트(XML) 파싱 유틸.

content/network.py의 NetworkManager.get_video_dash_manifest에서 HTTP를 떼어 내고
파싱만 이동한 순수 함수 (#51). 동작은 기존과 완전 동일하다.
"""

import xml.etree.ElementTree as ET


def parse_dash_manifest(xml_text: str) -> tuple[list[list], int, str]:
    """
    DASH 매니페스트 XML 문자열에서 Representation 목록을 파싱한다.

    해상도는 min(width, height)로 계산하고 오름차순으로 정렬한다.
    BaseURL이 '/hls/'로 끝나는 항목은 스킵한다.

    Args:
        xml_text (str): DASH 매니페스트 XML 문자열

    Returns:
        tuple[list[list], int, str]: ([해상도, base_url] 목록(오름차순),
            auto 해상도(최고), auto base_url) 형식의 튜플
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
        base_url = rep.find(".//mpd:BaseURL", namespaces=ns).text
        if base_url.endswith('/hls/'):
            continue
        reps.append([resolution, base_url])

    sorted_reps = sorted(reps, key=lambda x: x[0])
    auto_resolution = sorted_reps[-1][0]
    auto_base_url = sorted_reps[-1][1]

    return sorted_reps, auto_resolution, auto_base_url
