"""치지직 VOD URL 파싱 유틸.

content/network.py의 NetworkManager.extract_content_no에서 이동한 순수 함수 (#50).
"""

import re


def extract_content_no(vod_url: str) -> tuple[str, str]:
    """
    치지직 VOD URL에서 type과 content_no를 추출한다.

    브라우저 주소창에서 그대로 복사한 URL 변형을 허용한다 (#33):
    쿼리스트링(`?t=123` 등)은 무시하고, 후행 슬래시와 www 서브도메인을 허용한다.
    live URL·다른 도메인·형식이 깨진 URL은 계속 거부한다.

    Args:
        vod_url (str): 치지직 VOD URL

    Returns:
        tuple[str, str]: (type, content_no) 형식의 튜플. 매칭되지 않으면 (None, None) 반환
    """
    if not vod_url.startswith("http://") and not vod_url.startswith("https://"):
        vod_url = "https://" + vod_url
    match = re.fullmatch(
        r'https?://(?:www\.)?chzzk\.naver\.com'
        r'/(?P<content_type>video|clips)/(?P<content_no>\w+)/?(?:\?.*)?',
        vod_url,
    )
    if match:
        return match.group("content_type"), match.group("content_no")
    return None, None
