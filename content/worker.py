"""메타데이터 조회 워커 — core 서비스를 호출해 Signal로 중계하는 얇은 어댑터 (#72).

조회 로직(URL 파싱 → API 조회 → 에러 분기)은 core/services/metadata_service.py로
이동했다. 이 클래스는 다음만 담당한다:
- core 서비스 호출 결과를 finished/error Signal로 emit (시그니처·페이로드 무변경)
- MetadataError의 i18n 키를 tr()로 번역해 기존 에러 메시지 형식("<url>\\n<메시지>") 유지
"""

import logging

from content.network import NetworkManager
from core.services import metadata_service
from core.services.metadata_service import MetadataError

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class ContentWorker(QObject):
    # 작업이 완료되면 결과를 tuple로 전달 (혹은 필요한 데이터 구조로 전달)
    finished = Signal(object, str)
    error = Signal(str)

    def __init__(self, vod_url: str, cookies: dict, downloadPath: str):
        super().__init__()
        self.vod_url = vod_url
        self.cookies = cookies
        self.downloadPath = downloadPath

    def run(self):
        """메타데이터를 조회해 finished(성공) 또는 error(실패) Signal을 emit한다."""
        try:
            result, content_type = metadata_service.fetch_content(
                self.vod_url, self.cookies, self.downloadPath, api=NetworkManager
            )
            self.finished.emit(result, content_type)
        except Exception as e:
            # 크래시 지점 추적을 위해 traceback을 로그에 남긴다 (#55 디버깅).
            # str(e)만으로는 AttributeError 등의 발생 위치를 알 수 없다
            logger.exception("컨텐츠 요청 실패: %s", self.vod_url)
            self.error.emit(self._user_message(e))

    def fetchVideo(self, video_no: str) -> tuple:
        """VOD 조회를 core 서비스에 위임한다 (기존 시그니처·예외 형식 유지)."""
        try:
            return metadata_service.fetch_video(
                self.vod_url, video_no, self.cookies, self.downloadPath, api=NetworkManager
            )
        except MetadataError as e:
            raise ValueError(self._user_message(e)) from e

    def fetchClip(self, clip_no: str) -> tuple:
        """클립 조회를 core 서비스에 위임한다 (기존 시그니처·예외 형식 유지)."""
        try:
            return metadata_service.fetch_clip(
                self.vod_url, clip_no, self.cookies, self.downloadPath, api=NetworkManager
            )
        except MetadataError as e:
            raise ValueError(self._user_message(e)) from e

    def _user_message(self, e: Exception) -> str:
        """예외를 사용자 표시용 메시지로 바꾼다. MetadataError는 i18n 키를 번역한다."""
        if isinstance(e, MetadataError):
            return f"{e.url}\n{self._translate_key(e.message_key)}"
        return str(e)

    def _translate_key(self, message_key: str) -> str:
        """i18n 키를 현재 언어로 번역한다.

        lupdate가 `-no-obsolete`로 .ts를 재생성하므로(compile_translations.py 참고)
        키가 소스에서 사라지면 번역 항목도 삭제된다 — 반드시 리터럴로 tr()을 호출해
        추출 대상을 유지한다. 키 목록은 core/services/metadata_service.py가 던지는
        message_key 전체와 1:1이다.
        """
        translated = {
            "Invalid VOD URL": self.tr("Invalid VOD URL"),
            "Invalid cookies value": self.tr("Invalid cookies value"),
            "Encrypted content is not supported": self.tr("Encrypted content is not supported"),
            "Channel membership required": self.tr("Channel membership required"),
            "Unencoded Video(.m3u8)": self.tr("Unencoded Video(.m3u8)"),
            "Failed to get DASH manifest": self.tr("Failed to get DASH manifest"),
        }
        return translated.get(message_key, message_key)
