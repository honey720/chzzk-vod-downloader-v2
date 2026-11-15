"""GUI 테스트 공통 픽스처."""

from collections.abc import Iterator

import pytest

from app.widgets import tip_bubble
from tests.app.gui.mouse_buttons import release_left_by


@pytest.fixture(autouse=True)
def _no_tip_fade(monkeypatch: pytest.MonkeyPatch) -> None:
    """말풍선 페이드 시간을 0으로 둔다 — 바로 뜨고 바로 닫힌다(#610).

    말풍선이 뜨고 닫히는 자리 · 글을 재는 테스트는 페이드를 기다리지 않는다. 페이드를 재는 테스트는 스스로
    시간을 다시 건다(`tests/app/gui/test_tip_fade.py`).
    """
    monkeypatch.setattr(tip_bubble, "TIP_FADE_IN_MS", 0)
    monkeypatch.setattr(tip_bubble, "TIP_FADE_OUT_MS", 0)


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
