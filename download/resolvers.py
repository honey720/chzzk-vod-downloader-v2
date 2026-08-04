"""다운로드 시작 시점의 해석 콜러블 — DownloadService 주입용 (#75·#57, Qt 무의존).

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


def _load_cookies() -> dict:
    """설정에 저장된 유저 본인의 쿠키를 읽는다 (미설정이면 빈 값).

    조립은 config.load_cookies가 단일 지점으로 담당한다 (#170).
    """
    return config.load_cookies()


def resolve_aes_key(content: Content, key_uri: str) -> bytes:
    """AES(SEA) 세그먼트 복호화 키를 취득한다 (#57).

    다운로드 시작 시점(워커 스레드)에 호출된다 — 최신 쿠키를 반영한다.
    유저 본인의 쿠키로 인증된 요청이며, 쿠키가 없거나 해당 컨텐츠 권한이
    없으면 서버가 403으로 거절해 예외가 되고 다운로드는 실패한다.
    앱은 권한을 만들어내지 않는다.

    **키 값은 로그·예외 메시지에 싣지 않는다.**

    Raises:
        Exception: 키 취득 실패 (권한 없음·네트워크 등. 서비스가 실패 콜백으로 환원한다)
    """
    return NetworkManager.get_aes_key(key_uri, _load_cookies())


def resolve_m3u8_base_url(content: Content) -> str:
    """쿠키를 읽고 치지직 API로 선택 해상도의 m3u8 플레이리스트 URL을 해석한다.

    다운로드 시작 시점(워커 스레드)에 호출된다 — 최신 쿠키·해상도를 반영한다.

    Raises:
        Exception: 조회 실패 (네트워크·권한 등. 서비스가 실패 콜백으로 환원한다)
    """
    cookies = _load_cookies()
    content_type, content_no = NetworkManager.extract_content_no(content.url)
    info = NetworkManager.get_video_info(content_no, cookies)
    return NetworkManager.get_video_m3u8_base_url(
        info.live_rewind_playback_json, content.resolution, cookies
    )
