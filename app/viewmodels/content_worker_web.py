"""메타데이터 조회 워커 — content/worker.py의 Qt-free 대응 (#220, Phase B1).

핵심 조회 로직(URL 파싱 → API 조회 → 에러 분기)은 이미
core/services/metadata_service.py에 있어 그대로 호출한다. content/worker.py와
다른 점은 QObject+Signal 대신 콜백 두 개(on_finished/on_error)를 받는 것과,
`.tr()` 리터럴 딕셔너리 대신 주입받는 `translate` 콜러블(#212 i18n JSON
카탈로그)을 쓰는 것뿐이다 — 메시지 키 목록은 content/worker.py와 문자열
그대로 동일해야 카탈로그 조회가 맞아떨어진다 (#169 트랩 — 번역 컨텍스트 소실).

동시성은 이 클래스가 모른다 — content_viewmodel_web.py가
ThreadPoolExecutor로 run()을 제출하는 방식으로 처리한다.
"""

import logging
from typing import Callable

from content.network import NetworkManager
from core.services import metadata_service
from core.services.metadata_service import MetadataError

logger = logging.getLogger("content.manager")


class ContentWorkerWeb:
    def __init__(
        self,
        vod_url: str,
        cookies: dict,
        downloadPath: str,
        translate: Callable[[str], str] | None = None,
    ):
        self.vod_url = vod_url
        self.cookies = cookies
        self.downloadPath = downloadPath
        self._translate = translate or (lambda key: key)

    def run(self, on_finished: Callable[[tuple, str], None], on_error: Callable[[str], None]) -> None:
        """메타데이터를 조회해 on_finished(성공) 또는 on_error(실패)를 호출한다."""
        try:
            result, content_type = metadata_service.fetch_content(
                self.vod_url, self.cookies, self.downloadPath, api=NetworkManager
            )
            on_finished(result, content_type)
        except Exception as e:
            # 크래시 지점 추적을 위해 traceback을 로그에 남긴다 (#55 디버깅, content/worker.py와 동일)
            logger.exception("컨텐츠 요청 실패: %s", self.vod_url)
            on_error(self._user_message(e))

    def _user_message(self, e: Exception) -> str:
        """예외를 사용자 표시용 메시지로 바꾼다. MetadataError는 i18n 키를 번역한다.

        MetadataError가 아닌 예외의 원시 문자열은 내부 API URL 등이 섞여 있어
        유저에게 보여주지 않는다 — 상세는 run()의 logger.exception이 남긴다 (#126).
        """
        if isinstance(e, MetadataError):
            return f"{e.url}\n{self._translate(e.message_key)}"
        return f"{self.vod_url}\n{self._translate('Failed to fetch video information')}"
