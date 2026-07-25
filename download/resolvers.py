"""m3u8 플레이리스트 URL 해석 — DownloadService 주입용 (#75, Qt 무의존).

구 download/download_m3u8.py의 DownloadM3U8Thread._resolve_base_url을 함수로
옮겼다. 쿠키 로드(config)·치지직 API 조회(NetworkManager)는 네트워크 계층이
아직 앱 영역이라 core로 들어갈 수 없다 — core/services/download_service.py의
base_url_resolver 매개변수로 주입한다.

Qt 어댑터(download/qt_bridge.py)와 헤드리스 스크립트가 공용으로 쓰므로
이 모듈은 PySide6를 import하지 않는다.
"""

import config.config as config
from content.network import NetworkManager
from core.models.content import Content


def resolve_m3u8_base_url(content: Content) -> str:
    """쿠키를 읽고 치지직 API로 선택 해상도의 m3u8 플레이리스트 URL을 해석한다.

    다운로드 시작 시점(워커 스레드)에 호출된다 — 최신 쿠키·해상도를 반영한다.

    Raises:
        Exception: 조회 실패 (네트워크·권한 등. 서비스가 실패 콜백으로 환원한다)
    """
    data = config.load_config().get("cookies", {})
    cookies = {
        "NID_AUT": data.get("NID_AUT", ""),
        "NID_SES": data.get("NID_SES", ""),
    }
    content_type, content_no = NetworkManager.extract_content_no(content.url)
    info = NetworkManager.get_video_info(content_no, cookies)
    return NetworkManager.get_video_m3u8_base_url(
        info.live_rewind_playback_json, content.resolution, cookies
    )
