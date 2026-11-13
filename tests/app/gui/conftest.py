"""GUI 테스트 공통 픽스처."""

from collections.abc import Iterator

import pytest

from tests.app.gui.mouse_buttons import release_left_by


@pytest.fixture(autouse=True)
def _release_held_mouse_buttons(request: pytest.FixtureRequest) -> Iterator[None]:
    """테스트가 마우스 버튼을 눌린 채 남기면 풀고, 그 테스트를 실패시킨다(#590).

    남은 버튼은 같은 워커에서 뒤에 도는 호버 테스트의 합성 포인터 이동을 끌기로 만든다 — 실패는 그
    뒤 테스트에서 나서 원인을 찾기 어렵다. 그래서 버튼을 남긴 테스트를 그 자리에서 실패시키고, 버튼은
    먼저 풀어 뒤 테스트가 「버튼 없음」에서 시작하게 한다.
    """
    yield
    message = release_left_by(request.node.nodeid)
    if message is not None:
        pytest.fail(message, pytrace=False)
