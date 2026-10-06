"""구간 기준값 조회의 단계별 시간 기록 (#309, Qt 무의존).

조회(``app/section_basis.py``)가 어디서 시간을 쓰는지 로그 한 줄로 남긴다. 단계마다 걸린
시간과, 그 단계가 보낸 요청의 수 · 응답이 말한 바이트 수 · 응답 머리까지 걸린 시간을 적는다.

**적지 않는 것**: 주소 · 쿼리(토큰) · 쿠키 · 키 · 영상 번호 · 제목. 줄에 들어가는 것은 이 모듈이
정한 이름표와 숫자뿐이다 — 요청에서는 상태 코드 · ``Content-Length`` · 걸린 시간만 읽는다.

**조회의 동작을 바꾸지 않는다.** 재는 일이 실패해도(시계 · 훅 · 로그) 조회는 그대로 끝나야
하므로, 이 모듈 안의 실패는 모두 삼킨다. 단계 안에서 난 예외(조회의 실패)는 그대로 올린다.

요청은 ``requests``의 응답 훅으로 센다. 훅은 **조회를 돌리는 스레드의 전용 세션 하나에만**,
**조회 한 번 동안만** 건다(``watching``) — 끝나면 성공이든 예외든 뗀다.

- 그 세션은 그 스레드만 쓴다(``core.api.session.get_thread_session``). 엔진의 다운로드 워커는
  저마다 다른 스레드라 다른 세션을 쓰고, 조회가 끝난 뒤에는 훅이 없으므로 **엔진의 요청에는
  걸리지 않는다.** 조회가 동시에 둘 돌아도 스레드가 달라 세션이 다르고 셈이 섞이지 않는다
- **공유 세션에는 걸지 않는다.** 치지직 API 조회(변형 해석 · 키 · 선언 프레임률)는 모든 스레드가
  함께 쓰는 세션으로 나가므로, 거기에 걸면 다른 스레드의 요청(목록 조회 · 엔진의 주소 해석)이
  훅을 지난다. 그래서 그 단계들은 걸린 시간만 적고 요청 수 · 크기는 적지 않는다
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from core.api.session import get_thread_session

logger = logging.getLogger("app.section_basis")

LOG_PREFIX = "구간 기준값 조회"  # 로그에서 이 줄을 찾는 문자열


class ProbeTiming:
    """조회 한 번의 단계별 시간 · 요청을 모아 로그 한 줄로 낸다."""

    def __init__(self, kind: str):
        """
        Args:
            kind: 전달 방식의 이름표 — "mp4" · "fmp4" · "ts"
        """
        self._kind = kind
        self._thread = threading.get_ident()
        self._started = _now()
        self._stages: list[dict] = []  # 단계마다 {"name", "seconds", "requests"}
        self._current: dict | None = None
        self._facts: list[str] = []
        self._failure = ""

    @contextmanager
    def watching(self) -> Iterator[None]:
        """이 안에서 이 스레드의 전용 세션으로 나간 요청을 세고, 끝나면 로그 한 줄을 낸다.

        훅은 여기서 걸고 여기서 뗀다 — 성공이든 예외든 이 블록을 나가면 세션에 남지 않는다.
        """
        sessions = self._attach()
        try:
            yield
        except BaseException as e:
            self._failure = type(e).__name__  # 이름만 — 예외 문구에는 주소가 섞일 수 있다
            raise
        finally:
            self._detach(sessions)
            self.log()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """단계 하나의 시간을 잰다. 단계 안의 예외는 그대로 올린다."""
        entry = {"name": name, "seconds": None, "requests": []}
        started = _now()
        try:
            self._stages.append(entry)
            self._current = entry
        except Exception:
            pass
        try:
            yield
        finally:
            try:
                finished = _now()
                if started is not None and finished is not None:
                    entry["seconds"] = finished - started
                self._current = None
            except Exception:
                pass

    def note(self, label: str, value: Callable[[], object]) -> None:
        """함께 적을 사실 하나 — 세그먼트 수 · 묶음 수 · moov 크기 · 영상 길이 등.

        Args:
            label: 이름표
            value: 값을 돌려주는 함수. 여기서 부른다 — 값을 읽다 난 예외도 조회를 막지 않는다.
                숫자만 돌려준다(주소 · 이름을 넘기지 않는다)
        """
        try:
            self._facts.append(f"{label} {value()}")
        except Exception:
            pass

    def log(self) -> None:
        """모은 것을 INFO 로그 한 줄로 낸다."""
        try:
            logger.info("%s", self.line())
        except Exception:
            pass

    def line(self) -> str:
        """로그에 낼 한 줄."""
        finished = _now()
        requests = [r for stage in self._stages for r in stage["requests"]]
        size = sum(r["bytes"] or 0 for r in requests)
        head = [f"{LOG_PREFIX} [{self._kind}]"]
        if self._started is not None and finished is not None:
            head.append(f"합계 {finished - self._started:.2f}초")
        head.append(f"요청 {len(requests)}건")
        head.append(f"응답이 말한 크기 {size:,}바이트")
        head.extend(self._facts)
        if self._failure:
            head.append(f"실패 {self._failure}")
        stages = " · ".join(_stage_text(stage) for stage in self._stages)
        return " · ".join(head) + (f" | {stages}" if stages else "")

    # ---- 요청 세기 ----

    def _attach(self) -> list:
        sessions = []
        try:
            # 이 스레드의 전용 세션에만 건다 — 공유 세션에는 걸지 않는다(모듈 설명 참조)
            for session in (get_thread_session(),):
                session.hooks["response"].append(self._on_response)
                sessions.append(session)
        except Exception:
            pass
        return sessions

    def _detach(self, sessions: list) -> None:
        for session in sessions:
            try:
                session.hooks["response"].remove(self._on_response)
            except Exception:
                pass

    def _on_response(self, response, *args, **kwargs) -> None:
        """응답 훅 — 상태 코드 · Content-Length · 걸린 시간만 읽는다. 주소 · 머리 · 본문은 읽지 않는다."""
        try:
            if threading.get_ident() != self._thread or self._current is None:
                return
            length = response.headers.get("Content-Length")
            self._current["requests"].append(
                {
                    "status": response.status_code,
                    "bytes": int(length) if length and length.isdigit() else None,
                    "ms": response.elapsed.total_seconds() * 1000,
                }
            )
        except Exception:
            pass


def _now() -> float | None:
    """지금 시각(초). 시계를 읽지 못하면 None — 재는 일의 실패가 조회를 막지 않는다."""
    try:
        return time.perf_counter()
    except Exception:
        return None


def _stage_text(stage: dict) -> str:
    """단계 하나를 글로 — `이름 0.21초(요청 2건: 210ms/1,256B, 95ms/?)`."""
    seconds = stage["seconds"]
    text = stage["name"] + (f" {seconds:.2f}초" if seconds is not None else " ?초")
    requests = stage["requests"]
    if requests:
        detail = ", ".join(
            f"{r['ms']:.0f}ms/" + (f"{r['bytes']:,}B" if r["bytes"] is not None else "?")
            for r in requests
        )
        text += f"(요청 {len(requests)}건: {detail})"
    return text
