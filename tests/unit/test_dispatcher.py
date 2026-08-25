"""Dispatcher 검증 (#210) — 스레드세이프 큐 기반 액션 디스패처.

Qt의 큐드 커넥션이 공짜로 주던 "워커 스레드 emit → 안전한 스레드에서 실행"
보장을 명시적으로 재구현한 것이다. #185(비동기 stop() 경쟁 상태)가 이
경계에서 난 사고였으므로, "만들면 될 것"이 아니라 부하 아래 실제로
안전한지·고장난 버전은 실제로 실패하는지까지 검증한다.
"""

import threading
import time

import pytest

from app.dispatcher import Dispatcher


class SpyEvaluateJS:
    """window.evaluate_js 대역 — 호출된 JS 문자열을 기록한다."""

    def __init__(self):
        self.calls: list[str] = []
        self.call_thread_ids: list[int] = []

    def __call__(self, js: str):
        self.calls.append(js)
        self.call_thread_ids.append(threading.get_ident())


@pytest.fixture
def spy():
    return SpyEvaluateJS()


@pytest.fixture
def dispatcher(spy):
    return Dispatcher(evaluate_js=spy)


class TestDispatchJsContract:
    """JS↔Python 계약 — #208 대체 게이트 중 필수 항목."""

    def test_dispatch_js_produces_call_with_json_encoded_spread_args(self, dispatcher, spy):
        dispatcher.dispatch_js("window.__cvdv2_onProgress", "item-1", "00:00:10", "1024", "2.5 MB/s", 42)
        dispatcher.pump(timeout=0)

        assert spy.calls == [
            'window.__cvdv2_onProgress(...["item-1", "00:00:10", "1024", "2.5 MB/s", 42])'
        ]

    def test_dispatch_js_json_escapes_special_characters_safely(self, dispatcher, spy):
        """따옴표·백슬래시가 섞인 문자열도 원시 포매팅이 아니라 JSON 이스케이프로 안전하게 나간다."""
        tricky = 'title with "quotes" and \\backslash\\'
        dispatcher.dispatch_js("window.__cvdv2_onFailed", "item-1", tricky)
        dispatcher.pump(timeout=0)

        [call] = spy.calls
        assert '\\"quotes\\"' in call
        assert "\\\\backslash\\\\" in call

    def test_dispatch_js_rejects_unsafe_function_name(self, dispatcher):
        with pytest.raises(ValueError, match="안전하지 않은"):
            dispatcher.dispatch_js("alert('x'); //", "item-1")


class TestPump:
    def test_pump_returns_false_when_queue_empty(self, dispatcher):
        assert dispatcher.pump(timeout=0.01) is False

    def test_pump_executes_arbitrary_action_not_just_js_calls(self, dispatcher):
        """dispatch()는 JS 호출뿐 아니라 임의의 콜러블(내부 후처리 등)도 큐에 넣을 수 있다."""
        calls = []
        dispatcher.dispatch(lambda: calls.append("ran"))

        dispatcher.pump(timeout=0)

        assert calls == ["ran"]

    def test_run_forever_stops_on_event(self, dispatcher, spy):
        stop_event = threading.Event()
        thread = threading.Thread(target=dispatcher.run_forever, args=(stop_event, 0.01))
        thread.start()

        dispatcher.dispatch_js("window.__cvdv2_onPaused", "item-1")
        time.sleep(0.1)
        stop_event.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert spy.calls == ['window.__cvdv2_onPaused(...["item-1"])']


class TestConcurrentLoadNormalCase:
    """#208 프로토타입의 실측치(초당 50회, 5스레드 동시)를 정식 테스트로 승격한다."""

    def test_single_producer_50_events_per_second_all_delivered_in_order(self, dispatcher, spy):
        N = 50

        def producer():
            for i in range(N):
                dispatcher.dispatch_js("window.__cvdv2_onProgress", "item-1", str(i))
                time.sleep(0.02)  # 초당 50회, 실제 progress 콜백 빈도 수준 (#208 실측)

        stop_event = threading.Event()
        consumer = threading.Thread(target=dispatcher.run_forever, args=(stop_event, 0.05))
        consumer.start()

        t = threading.Thread(target=producer)
        t.start()
        t.join()
        time.sleep(0.2)
        stop_event.set()
        consumer.join(timeout=2)

        assert len(spy.calls) == N
        # FIFO 순서 보존 — i번째로 넣은 이벤트가 i번째로 나온다
        for i, call in enumerate(spy.calls):
            assert f'"{i}"' in call

    def test_five_concurrent_producers_no_event_lost(self, dispatcher, spy):
        """SPEC 기본 동시 다운로드 수(5)를 흉내낸 동시 부하 — 이벤트 유실 없음이 핵심."""
        N_THREADS = 5
        CALLS_PER_THREAD = 20

        def producer(idx):
            for i in range(CALLS_PER_THREAD):
                dispatcher.dispatch_js("window.__cvdv2_onProgress", f"item-{idx}", str(i))

        stop_event = threading.Event()
        consumer = threading.Thread(target=dispatcher.run_forever, args=(stop_event, 0.01))
        consumer.start()

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.2)
        stop_event.set()
        consumer.join(timeout=2)

        assert len(spy.calls) == N_THREADS * CALLS_PER_THREAD
        # 소비는 항상 단일 스레드(백엔드 스레드)에서만 일어난다 — evaluate_js가
        # 여러 스레드에서 동시 호출될 위험이 이 설계로 원천 차단됨을 확인한다
        assert len(set(spy.call_thread_ids)) == 1


class TestStopRaceLikeIssue185:
    """#185(후처리 실패 시 세그먼트 보존 정책의 비동기 stop() 경쟁 상태)와 같은
    유형의 경쟁 — "진행 중 이벤트"와 "종료 신호"가 서로 다른 스레드에서 거의
    동시에 도착해도, 소비는 단일 스레드에서 순서대로 일어나 상태가 뒤섞이지
    않는다는 것을 확인한다.
    """

    def test_progress_event_and_stop_signal_from_different_threads_are_serialized(
        self, dispatcher, spy
    ):
        order: list[str] = []

        def on_progress_dispatched():
            order.append("progress")

        def on_stop_dispatched():
            order.append("stop")

        # progress는 "워커 스레드"에서, stop은 "유저 조작 스레드"에서 — #185와
        # 같은 모양의 경쟁: 서로 다른 스레드가 거의 동시에 상태를 건드리려 한다
        worker = threading.Thread(target=lambda: dispatcher.dispatch(on_progress_dispatched))
        stopper = threading.Thread(target=lambda: dispatcher.dispatch(on_stop_dispatched))

        worker.start()
        worker.join()
        stopper.start()
        stopper.join()

        # 두 액션이 큐에 들어간 뒤에는 백엔드 스레드 하나가 순서대로만 실행한다
        while dispatcher.pump(timeout=0):
            pass

        assert order == ["progress", "stop"]  # 넣은 순서 그대로, 뒤섞이지 않는다
        assert len(set(spy.call_thread_ids)) <= 1  # JS 호출이 있었다면 전부 같은 스레드


class TestBrokenNaiveQueueLosesEvents:
    """"고장난 상태로 돌려 실제로 실패하는지" — 오너가 명시 요청한 검증.

    queue.Queue 대신 "카운터를 읽고 → 그 값을 키로 저장"하는 흔한 실수(비원자적
    read-modify-write)로 만든 "고장난" 버전을 여기서 직접 구성한다. 최초
    버전은 CPython의 GIL 덕에 `list.append`/`pop(0)`이 사실상 원자적으로
    동작해 레이스가 안 잡혔다(#191과 같은 교훈 — 확률에 기대면 안 잡힌다) —
    그래서 `#191`이 썼던 것과 같은 수법으로, read와 write 사이에 명시적
    `time.sleep()`을 넣어 경쟁 창을 강제로 벌린다. 프로덕션 코드
    (app/dispatcher.py)는 건드리지 않는다 — 이 테스트 파일 안에서만 깨진
    대역을 만든다.
    """

    class NaiveUnsafeDispatcher:
        """카운터 읽기와 증가 사이에 창이 있는 고장난 버전 — 두 스레드가 같은
        키를 읽으면 하나가 다른 하나의 액션을 덮어써 유실시킨다."""

        def __init__(self):
            self._items: dict = {}
            self._counter = 0

        def dispatch(self, action):
            key = self._counter  # 읽기
            time.sleep(0.001)  # 다른 스레드가 끼어들 창을 강제로 연다 (#191 수법)
            self._counter = key + 1  # 쓰기 — 그 사이 다른 스레드가 같은 key를 읽었을 수 있다
            self._items[key] = action  # 같은 key면 먼저 쓴 액션이 덮어써져 사라진다

        def drain_all(self):
            actions = list(self._items.values())
            self._items.clear()
            for action in actions:
                action()
            return actions

    def test_naive_counter_based_queue_loses_events_under_concurrent_producers(self):
        naive = self.NaiveUnsafeDispatcher()
        received = []

        N_THREADS = 4
        CALLS_PER_THREAD = 5

        def producer(idx):
            for i in range(CALLS_PER_THREAD):
                naive.dispatch(lambda idx=idx, i=i: received.append((idx, i)))

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        naive.drain_all()

        expected = N_THREADS * CALLS_PER_THREAD
        # 고장난 버전은 동시에 같은 카운터 값을 읽은 액션끼리 서로 덮어써
        # 유실이 난다 — sleep으로 창을 강제로 벌렸으므로 결정적으로 재현된다
        assert len(received) < expected, (
            f"고장난 버전에서 유실이 재현되지 않았다 — received={len(received)}/{expected}"
        )

    def test_real_dispatcher_using_queue_Queue_never_loses_events_same_load(self):
        """같은 부하를 진짜 Dispatcher(queue.Queue 기반)로 돌리면 유실이 없다 — 대조군."""
        spy_calls = []
        real = Dispatcher(evaluate_js=lambda js: spy_calls.append(js))

        N_THREADS = 8
        CALLS_PER_THREAD = 200

        def producer(idx):
            for i in range(CALLS_PER_THREAD):
                real.dispatch(lambda idx=idx, i=i: spy_calls.append((idx, i)))

        stop_event = threading.Event()
        consumer = threading.Thread(target=real.run_forever, args=(stop_event, 0.005))
        consumer.start()

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.3)
        stop_event.set()
        consumer.join(timeout=2)

        assert len(spy_calls) == N_THREADS * CALLS_PER_THREAD  # 유실 0건
