"""pywebview 셸 진입점 (#209, Phase A1) — 최소 골격.

기존 main.py(PySide6)는 무변경으로 병행 유지한다. 5탭 UI·엔진 연결은
Phase C 이후 항목이며, 이 파일은 "pywebview + Nuitka 빌드 파이프라인이
실제로 동작하는가"만 증명하는 셸이다.

리소스 경로 해석은 main.py의 resource_path와 동일한 패턴을 쓴다 —
Nuitka onefile은 리소스를 임시 해제 경로에 풀고 __file__도 그 안을
가리키므로, CWD가 아니라 이 파일의 위치를 기준으로 해석해야 한다 (#43).

**배경색 (#217)**: 이 프로젝트에는 아직 테마 개념이 없다 — config.json에
theme 키가 없고, 시스템 테마 추종(darkdetect 등)도 붙어 있지 않다. 지금은
`_BACKGROUND_COLOR` 하나가 유일한 배경색 값이라 "테마를 따라간다"는 건 이
상수를 바꾸는 것과 같다. 실제 테마 시스템(설정 또는 시스템 다크/라이트
추종)이 생기면 그때 이 상수를 그 값으로 대체해야 한다 — 지금 범위는
Python(`background_color`)과 CSS(`body` 배경)가 서로 다른 값을 쓰는
불일치만 없애는 것이다(리사이즈 중 OS가 먼저 칠하는 배경색이
`webview.create_window`의 `background_color` 기본값 `#FFFFFF`라 CSS의
어두운 배경과 어긋나 흰색이 번쩍였다).

`background_color`는 pywebview에서 창 생성 시점에만 적용된다(Windows
백엔드 기준 `winforms.py`의 `BackColor`, `edgechromium.py`의 WebView2
기본 배경색 — 둘 다 생성 시 1회 대입, 이후 재적용 경로 없음). 즉 실행 중
테마를 바꿔도 리사이즈 번쩍임 색은 재시작 전까지 안 바뀐다 — 테마 시스템이
생기기 전까지는 해당 없는 제약이지만, 만들 때 감안해야 한다.
"""
import os

import webview

from config.log_setup import setup_logging

_BACKGROUND_COLOR = "#1e1e1e"

_PLACEHOLDER_HTML = f"""
<html>
<head>
<style>
  body {{ margin: 0; font-family: sans-serif; background: {_BACKGROUND_COLOR}; color: #eee;
         display: flex; align-items: center; justify-content: center; height: 100vh; }}
</style>
</head>
<body>
  <h1>CVDv2 — pywebview shell skeleton (#209)</h1>
</body>
</html>
"""


def resource_path(relative_path: str) -> str:
    """소스 실행과 Nuitka onefile/standalone 빌드 양쪽에서 동작하는 리소스 절대 경로 (#43)."""
    base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def main():
    setup_logging()

    webview.create_window(
        "CVDv2",
        html=_PLACEHOLDER_HTML,
        width=1000,
        height=700,
        background_color=_BACKGROUND_COLOR,
    )
    webview.start()


if __name__ == "__main__":
    main()
