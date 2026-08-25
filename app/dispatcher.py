"""스레드세이프 큐 기반 액션 디스패처 (#210, Phase A2).

Qt의 큐드 커넥션(Signal/Slot)이 공짜로 주던 보장 — "워커 스레드에서 emit해도
안전하게 GUI 스레드에서 슬롯이 실행된다" — 을 pywebview에는 그런 게 없으므로
명시적으로 재구현한다. `#185`(후처리 실패 시 세그먼트 보존 정책의 비동기
stop() 경쟁 상태)가 정확히 이 경계에서 난 사고였다 — 워커 스레드와 "메인"
스레드 사이의 상태 전이 타이밍이 어긋나 보존돼야 할 세그먼트가 도로
지워지는 결함이었다. 이 클래스가 그 경계를 단일 지점에서 책임진다.

사용법:
- 임의의 스레드(워커 스레드 포함)에서 `dispatch(action)`으로 콜러블을
  큐에 넣는다 — `queue.Queue`가 내부적으로 스레드세이프하므로 락 없이도
  안전하다.
- pywebview의 "백엔드 스레드"(`webview.start(func)`에 넘긴 함수가 도는
  스레드 — `#208` 실측으로 이 스레드에서의 `evaluate_js` 호출이 안전함을
  확인했다)에서 `run_forever(stop_event)`를 돌려 큐를 비운다.

**왜 제3의 스레드에서 직접 `evaluate_js`를 호출하지 않는가**: `#208`
Windows 실측에서는 워커 스레드 직접 호출도 안전했다(예외 없음, 5스레드
동시 부하도 통과). 하지만 이 결과는 Windows/WebView2 한정이고
macOS(WKWebView)·Linux(WebKitGTK)는 아직 실측이 없다. 이 클래스는 그
낙관적인 결과에 기대지 않고, 모든 JS 호출을 pywebview가 공식적으로
문서화한 사용 패턴(백엔드 스레드)으로 좁혀 플랫폼 불문 안전 마진을
확보한다.
"""

import json
import queue
import re
import threading
from typing import Callable

_SAFE_JS_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.]*$")


class Dispatcher:
    """스레드세이프 액션 큐 — 임의 스레드에서 넣고, 백엔드 스레드에서 뺀다."""

    def __init__(self, evaluate_js: Callable[[str], object]):
        """
        Parameters
        ----------
        evaluate_js: pywebview의 window.evaluate_js와 같은 시그니처의 콜러블.
            백엔드 스레드에서만 호출된다 (#208에서 안전성 확인한 그 지점).
        """
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._evaluate_js = evaluate_js

    def dispatch(self, action: Callable[[], None]) -> None:
        """임의의 스레드에서 안전하게 호출 가능. action은 백엔드 스레드에서 나중에 실행된다."""
        self._queue.put(action)

    def dispatch_js(self, js_function_name: str, *args) -> None:
        """dispatch()의 편의 래퍼 — `<js_function_name>(...JSON인자)` 형태로 JS를 호출한다.

        인자는 JSON으로 직렬화되므로 문자열에 따옴표·백슬래시가 섞여도
        안전하다(원시 문자열 포매팅이 아니다). 스프레드 문법(`...`)으로
        풀어 넘기므로 JS 쪽 함수는 평범한 위치 인자로 받으면 된다:
        `window.__cvdv2_onProgress = (itemId, remain, size, speed, pct) => {...}`
        """
        if not _SAFE_JS_IDENTIFIER.match(js_function_name):
            raise ValueError(f"안전하지 않은 JS 함수명: {js_function_name!r}")
        payload = json.dumps(list(args))

        def action() -> None:
            self._evaluate_js(f"{js_function_name}(...{payload})")

        self.dispatch(action)

    def pump(self, timeout: float | None = None) -> bool:
        """백엔드 스레드에서 호출한다. 큐에서 액션 하나를 꺼내 실행한다.

        Returns
        -------
        처리했으면 True, timeout 안에 아무것도 없었으면 False.
        """
        try:
            action = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        action()
        return True

    def run_forever(self, stop_event: threading.Event, poll_interval: float = 0.1) -> None:
        """`stop_event`가 set()될 때까지 pump를 반복한다 — 백엔드 스레드에서 블로킹 호출."""
        while not stop_event.is_set():
            self.pump(timeout=poll_interval)
