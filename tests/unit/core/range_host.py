"""테스트용 HTTP 범위 요청 호스트 — 소켓 없이 requests 안에서 응답한다 (#309).

테스트 네트워크 가드(tests/conftest.py)는 loopback을 포함한 모든 소켓 연결을 막는다.
그래서 서버를 띄우지 않고, requests의 전송 어댑터 자리에서 메모리의 bytes로 HTTP
응답을 만든다. 엔진 쪽은 진짜 ``requests.Session``으로 요청을 보내고 진짜
``requests.Response``를 받는다 — 머리(Range · Content-Range), 상태 코드(200 · 206 ·
404 · 416), 나눠 읽기(iter_content)가 실제 서버와 같은 모양이다.

받은 요청을 기록해 두어, 테스트가 어떤 범위를 요청했는지 대조할 수 있다.
"""

import io
import re
import threading

import requests
from requests.adapters import BaseAdapter
from urllib3.response import HTTPResponse

_RANGE = re.compile(r"bytes=(\d+)-(\d*)")


class _RangeAdapter(BaseAdapter):
    """요청을 호스트의 bytes로 답하는 전송 어댑터."""

    def __init__(self, host: "RangeHost"):
        super().__init__()
        self._host = host

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        name = request.url[len(RangeHost.BASE) :]
        range_header = request.headers.get("Range")
        self._host._record(request.method, name, range_header)
        data = self._host.files.get(name)
        headers = {"Accept-Ranges": "bytes"}
        body = b""
        if data is None:
            status = 404
        else:
            match = _RANGE.fullmatch(range_header or "")
            if match is None:
                status, body = 200, data
            elif int(match.group(1)) >= len(data):
                status = 416
                headers["Content-Range"] = f"bytes */{len(data)}"
            else:
                first = int(match.group(1))
                last = min(int(match.group(2)) if match.group(2) else len(data) - 1, len(data) - 1)
                status, body = 206, data[first : last + 1]
                headers["Content-Range"] = f"bytes {first}-{last}/{len(data)}"
        headers["Content-Length"] = str(len(body))
        if request.method == "HEAD":
            body = b""

        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.url = request.url
        response.request = request
        response.raw = HTTPResponse(
            body=io.BytesIO(body),
            headers=headers,
            status=status,
            preload_content=False,
            request_method=request.method,  # HEAD는 Content-Length가 있어도 본문이 없다
        )
        return response

    def close(self) -> None:
        pass


class RangeHost:
    """이름 → bytes를 내주는 호스트."""

    BASE = "http://range.test/"  # 이 주소로 가는 요청만 답한다 — 실제로 연결되지 않는다

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.requests: list[tuple[str, str, str | None]] = []  # (메서드, 이름, Range 머리)
        self._lock = threading.Lock()

    def _record(self, method: str, name: str, range_header: str | None) -> None:
        with self._lock:
            self.requests.append((method, name, range_header))

    def url(self, name: str) -> str:
        """그 이름의 파일을 받는 주소."""
        return self.BASE + name

    def session(self) -> requests.Session:
        """이 호스트로 가는 요청을 답하는 세션 — ``get_thread_session`` 자리에 넣는다."""
        session = requests.Session()
        session.mount(self.BASE, _RangeAdapter(self))
        return session

    def forget(self) -> None:
        """기록한 요청을 비운다."""
        with self._lock:
            self.requests.clear()
