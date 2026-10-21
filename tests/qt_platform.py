"""로컬 테스트가 Qt 창을 실제 화면에 띄우지 않게 플랫폼 기본값을 정하는 도구 (#399).

``QT_QPA_PLATFORM``을 정하지 않고 ``uv run pytest``를 돌리면 Qt가 실제 플랫폼(Windows · cocoa ·
xcb)을 써서, 테스트가 만드는 창이 화면에 떴다 사라진다. CI는 값을 정해 둔다(Windows · macOS는
``offscreen``, Linux는 정하지 않고 ``xvfb``의 가상 화면에서 xcb). ``conftest.py``가 Qt 앱을 만들기
전에 ``apply``를 부른다 — Qt는 ``QGuiApplication``을 만들 때 이 값을 읽는다.

- CI(``CI`` 환경 변수)에서는 아무것도 바꾸지 않는다 — Linux CI는 값을 정하지 않은 채 xvfb로 돈다.
- ``CVDV2_TEST_NATIVE=1``이면 실제 플랫폼을 쓴다(값을 정하지 않는다).
- 이미 정한 플랫폼은 그대로 둔다. 값이 없으면 ``offscreen``으로 둔다.
- 플랫폼이 ``offscreen``이면(직접 정했든 기본값이든) 폰트 폴더(``QT_QPA_FONTDIR``) 변수가 없을 때
  CI와 같은 폴더를 준다 — offscreen은 시스템 폰트를 스스로 찾지 않아 한글이 □로 그려지고(#154),
  대체 폰트로는 창 최소폭이 달라진다(#405). 이미 있는 값은 빈 문자열 · 빈 폴더라도 그대로 둔다.
"""

from collections.abc import MutableMapping

NATIVE_SWITCH = "CVDV2_TEST_NATIVE"  # 1이면 로컬 테스트도 실제 플랫폼을 쓴다
# offscreen일 때 CI가 주는 폰트 폴더 (.github/workflows/ci.yml). Linux CI는 offscreen을 쓰지 않아 값이 없다
CI_FONT_DIRS = {"win32": r"C:\Windows\Fonts", "darwin": "/System/Library/Fonts"}


def defaults(environ: MutableMapping[str, str], platform: str) -> dict[str, str]:
    """environ에 더할 Qt 플랫폼 기본값 — 더할 것이 없으면 빈 dict.

    Args:
        environ: 지금의 환경 변수
        platform: ``sys.platform`` 값
    """
    if environ.get("CI") or environ.get(NATIVE_SWITCH) == "1":
        return {}
    added = {} if "QT_QPA_PLATFORM" in environ else {"QT_QPA_PLATFORM": "offscreen"}
    if environ.get("QT_QPA_PLATFORM", "offscreen") != "offscreen":
        return added
    if "QT_QPA_FONTDIR" not in environ and platform in CI_FONT_DIRS:
        added["QT_QPA_FONTDIR"] = CI_FONT_DIRS[platform]
    return added


def apply(environ: MutableMapping[str, str], platform: str) -> dict[str, str]:
    """``defaults``의 값을 environ에 넣고, 넣은 것을 돌려준다."""
    added = defaults(environ, platform)
    environ.update(added)
    return added
