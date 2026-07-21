"""HTTP 응답 목(mock) 객체.

테스트에서 실제 네트워크 호출 없이 `requests.Response`의 최소 인터페이스를 흉내 낸다.
"""

import json

import requests


class MockResponse:
    """`requests.Response`를 대신하는 최소 목 객체."""

    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """실제 Response처럼 4xx/5xx 상태 코드면 HTTPError를 던진다."""
        if self.status_code >= 400:
            raise requests.HTTPError(f"Mock HTTP {self.status_code}")

    def json(self) -> object:
        """본문 텍스트를 JSON으로 파싱해 반환한다."""
        return json.loads(self.text)
