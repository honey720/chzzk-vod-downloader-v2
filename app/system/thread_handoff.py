"""워커 스레드가 Qt 객체를 잡지 않게 하는 두 도구 — 메인 스레드 부르기 · 풀 일 시작 (#564).

워커 스레드가 QWidget · QObject의 파이썬 참조를 들면, 다른 참조가 먼저 풀렸을 때 마지막 참조가
워커에 남는다. 워커가 그 참조를 놓는 순간 객체가 **워커 스레드에서** 지워진다 — QWidget이면
프로세스가 접근 위반으로 죽는다(메인 스레드가 이벤트를 처리하지 않고 기다리는 동안에도 죽는다).

- `MainCall` — 워커가 결과를 넘길 곳. 대상을 약한 참조로만 들고, 부르기는 메인 스레드에서 한다.
  대상이 그사이 지워졌으면 부르지 않는다
- `start_job` — `job.run()`을 풀 스레드에서 돌린다. 풀 스레드가 일을 다 쓴 뒤 메인 스레드가 마지막
  참조를 놓는다

두 도구의 중계 객체는 처음 쓸 때 메인 스레드에서 만들고 지우지 않는다 — 워커가 그것의 참조를 놓아도
지워지지 않는다.
"""

import itertools
import weakref
from collections.abc import Callable
from typing import Protocol

import shiboken6
from PySide6.QtCore import QObject, QThreadPool, Signal


class _Job(Protocol):
    """풀 스레드에서 돌릴 일 — `run()` 하나."""

    def run(self) -> None: ...


class _Relay(QObject):
    """워커 스레드의 부르기를 메인 스레드로 넘기고, 풀 일의 참조를 들고 있는다.

    시그널은 워커 스레드에서 나고, 이 객체(메인 스레드)의 바운드 메서드가 큐로 받는다.
    """

    called = Signal(object, str, object)  # (대상 약한 참조, 메서드 · 시그널 이름, 인자 튜플)
    released = Signal(int)  # 풀 스레드가 그 번호의 일을 다 썼다

    def __init__(self) -> None:
        super().__init__()
        self._held: dict[int, object] = {}  # 번호 → 도는 일. 풀 스레드가 놓을 때까지 든다
        self._numbers = itertools.count()
        self.called.connect(self._onCalled)
        self.released.connect(self._onReleased)

    def hold(self, job: object) -> int:
        """일을 들고 그 번호를 돌려준다 — 메인 스레드."""
        number = next(self._numbers)
        self._held[number] = job
        return number

    def held(self, number: int) -> object:
        """번호의 일 — 풀 스레드가 읽는다. 놓기 전이라 늘 있다."""
        return self._held[number]

    def heldCount(self) -> int:
        """아직 풀 스레드가 놓지 않은 일의 수."""
        return len(self._held)

    def _onCalled(self, target: weakref.ref, name: str, args: tuple) -> None:
        """메인 스레드 — 대상이 살아 있으면 그 메서드를 부르거나 그 시그널을 낸다."""
        obj = target()
        if obj is None or not shiboken6.isValid(obj):
            return  # 결과가 오기 전에 대상이 지워졌다 — 버린다
        member = getattr(obj, name)
        emit: Callable[..., None] | None = getattr(member, "emit", None)
        (emit or member)(*args)

    def _onReleased(self, number: int) -> None:
        """메인 스레드 — 풀 스레드가 다 쓴 일을 놓는다. 마지막 참조면 여기(메인)에서 지워진다."""
        self._held.pop(number, None)


_relay: _Relay | None = None  # 처음 쓸 때 메인 스레드에서 만든다


def _the_relay() -> _Relay:
    """중계 객체 — 없으면 만든다. 메인 스레드에서만 부른다(만든 스레드가 받는 스레드다)."""
    global _relay
    if _relay is None or not shiboken6.isValid(_relay):
        _relay = _Relay()
    return _relay


class MainCall:
    """워커 스레드가 메인 스레드의 객체에 결과를 넘기는 값 — 대상을 강하게 잡지 않는다.

    메인 스레드에서 만들어 워커에 넘긴다. 워커는 `call(name, *args)`로 대상의 메서드를 부르거나
    시그널을 낸다(이름이 시그널이면 emit). 실제 부르기는 메인 스레드에서 하고, 그때 대상이
    지워졌으면(파이썬 참조가 다 풀렸거나 Qt가 지웠으면) 하지 않는다. 인자는 그대로 메인 스레드로
    가므로 Qt 객체를 싣지 않는다.
    """

    __slots__ = ("_relay", "_target")

    def __init__(self, target: QObject) -> None:
        self._target = weakref.ref(target)
        self._relay = _the_relay()

    def __call__(self, name: str, *args: object) -> None:
        """대상의 `name`을 메인 스레드에서 `args`로 부르게 한다 — 어느 스레드에서 불러도 된다."""
        try:
            self._relay.called.emit(self._target, name, args)
        except RuntimeError:
            pass  # 앱이 끝나 중계 객체가 지워졌다


def start_job(pool: QThreadPool, job: _Job) -> None:
    """`job.run()`을 풀 스레드에서 돌린다 — 일의 마지막 참조는 메인 스레드에서 놓는다.

    메인 스레드에서 부른다. 일이 내는 결과 시그널을 받은 쪽이 참조를 먼저 놓아도, 중계 객체가
    풀 스레드가 다 쓸 때까지 들고 있다가 메인 스레드에서 놓는다. `pool.start(lambda: job.run())`은
    풀 스레드가 마지막 참조를 놓을 수 있어 일(QObject)이 풀 스레드에서 지워진다.
    """
    relay = _the_relay()
    number = relay.hold(job)

    def run() -> None:
        held = relay.held(number)
        try:
            held.run()  # type: ignore[attr-defined]
        finally:
            del held  # 놓은 뒤에 알린다 — 알림을 받은 메인이 마지막 참조를 놓는다
            try:
                relay.released.emit(number)
            except RuntimeError:
                pass  # 앱이 끝나 중계 객체가 지워졌다

    pool.start(run)


def held_job_count() -> int:
    """풀 스레드가 아직 놓지 않은 일의 수 — 테스트가 다 끝났는지 본다."""
    return 0 if _relay is None or not shiboken6.isValid(_relay) else _relay.heldCount()
