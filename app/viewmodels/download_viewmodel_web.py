"""다운로드 viewmodel — Qt-free 대응 (#221, Phase B2).

`app/viewmodels/download_viewmodel.py`(Qt: `QtDownloadBridge` 소유)의
웹 버전. `main.py`가 그대로 쓰는 `download_viewmodel.py`·`download/qt_bridge.py`는
무변경으로 남긴다 — `#220`(Phase B1)과 같은 이유(병행 파일).

**B3("qt_bridge.py 로직 이식")는 이미 끝나 있었다** — `#210`(A2)의
`app/download_bridge.py`가 진행률 계산·실패 사유 매핑을 전부 이식해뒀다.
이 파일이 하는 일은 그 PR이 의도적으로 열어둔 두 인터페이스를 닫는 것뿐이다:

1. **완료/실패 시 content 쪽 상태 전이 + 배치 체인 배선** — 기존 Qt 경로에서
   `QtDownloadBridge.finished`/`.failed` Signal이 `DownloadViewModel`을 거쳐
   `ContentViewModel.finish`/`.fail`에 연결되던 자리. `WebDownloadBridge`의
   `on_finished`/`on_failed` 콜백 훅(`#221`에서 신설)에 `content.finish`/
   `.fail`을 그대로 주입한다.
2. **i18n 실배선** — `translate`에 `app.i18n.translate`를 언어 고정한
   partial로 주입한다(`#212`가 이미 만든 함수, 지금까지는 항등 함수였다).

**`downloadRequested`(Python 콜백) 쪽 배선도 여기서 맡는다**: `#220`의
`ContentViewModelWeb.on_download_requested`는 기본값이 no-op이다. 이
클래스를 만들면(바인더가) `content.on_download_requested = self.start`로
연결해야 한다 — 원본 mainWindow의 `startDownload`가 `contentManager.start(item)`
(카드 통지)과 `downloadViewModel.start(item)`(실제 엔진 제출) 둘 다 하던 것과
같은 자리이므로, 이 클래스의 `start(item)`도 둘 다 한다.

**진행률(`progress`/`paused`/`resumed`/`stopped`)은 content 쪽으로 다시
연결하지 않았다** — grep으로 확인한 결과 `item.download_progress` 등
Qt `content_viewmodel.update_progress`가 갱신하던 필드를 읽는 소비처가
저장소 어디에도 없다(`#220`이 이미 같은 이유로 `update_progress` 자체를
포팅하지 않았다). `WebDownloadBridge`가 이미 이 네 이벤트를 JS로 직접
보내므로 Python 쪽 상태 이중화를 만들지 않는다.

**언어 결정**: 지금은 `config.json`의 `language` 값만 쓴다(`app.i18n.resolve_language`의
1순위). 시스템 로케일 폴백(main.py의 `QLocale.system().name()`)은 Qt
의존이라 이 파일에 넣지 않았다 — 첫 실행 시 시스템 언어 자동 감지는 Phase C가
실제 설정 화면을 만들 때 Qt-free 대안(`locale` 표준 라이브러리 등)으로
다시 판단할 문제로 남겨둔다. 카탈로그가 없는 언어값이면 `resolve_language`가
`DEFAULT_LANGUAGE`로 떨어진다.
"""

from functools import partial
from typing import TYPE_CHECKING

import config.config as config

from app.dispatcher import Dispatcher
from app.download_bridge import WebDownloadBridge
from app.i18n import resolve_language, translate
from content.data import ContentItem
from core.services.download_service import DownloadService

if TYPE_CHECKING:
    from app.viewmodels.content_viewmodel_web import ContentViewModelWeb


class DownloadViewModelWeb:
    def __init__(
        self,
        dispatcher: Dispatcher,
        content: "ContentViewModelWeb",
        service: DownloadService | None = None,
    ):
        self._content = content
        language = resolve_language(config.load_config().get("language"))
        self._bridge = WebDownloadBridge(
            dispatcher,
            service=service,
            translate=partial(translate, language=language),
            on_finished=content.finish,
            on_failed=content.fail,
        )

    def isDownloading(self) -> bool:
        """활성 다운로드 핸들 존재 여부 — Qt 버전의 동일 메서드와 같은 계약."""
        return self._bridge.handle is not None

    @property
    def task(self):
        return self._bridge.task

    def start(self, item: ContentItem) -> None:
        """다운로드를 시작한다. mainWindow.startDownload와 같은 두 가지 일을 한다:
        카드 통지(itemStarted) + 실제 엔진 제출."""
        self._content.start(item)
        self._bridge.start(item.id, item)

    def pause(self) -> None:
        self._bridge.pause()

    def resume(self) -> None:
        self._bridge.resume()

    def stop(self) -> None:
        self._bridge.stop()

    def removeThreads(self) -> None:
        self._bridge.removeThreads()
