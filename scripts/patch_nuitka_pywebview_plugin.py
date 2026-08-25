"""Nuitka의 pywebview 플러그인 Windows 화이트리스트 누락 버그를 회피한다 (#209).

`nuitka/plugins/standard/PywebViewPlugin.py`의 `onModuleEncounter`가 Windows에서
허용하는 `webview.platforms.*` 모듈 목록에 `winforms`/`edgechromium`/`edgehtml`/
`mshtml`/`cef`는 있지만 **`win32`가 빠져 있다** — 그런데 `webview/platforms/winforms.py`가
`from webview.platforms import win32`를 무조건 import하므로, 이 누락 때문에 Nuitka
onefile/standalone 빌드가 컴파일은 되지만 실행 시 `webview.errors.WebViewException:
You must have pythonnet installed in order to use pywebview`로 죽는다(실제로는
pythonnet 문제가 아니라 win32 모듈이 통째로 빠진 것).

`#208` 조사에서 이 파일에 `"webview.platforms.win32"` 한 줄을 추가하면 정상
빌드+정상 실행(exit 0, 3회 재현)됨을 확인했다. 이 스크립트는 그 패치를
설치된 Nuitka 패키지에 자동 적용한다 — Windows에서 Nuitka 빌드 직전에 실행한다.

idempotent: 이미 패치돼 있으면 아무것도 안 하고 종료한다(exit 0).
Windows가 아니면 아무것도 안 하고 종료한다(macOS/Linux 분기는 이 버그가 없음 —
`#208`에서 코드 구조상 확인, `cocoa.py`/`gtk.py`는 형제 모듈에 의존하지 않는다).

사용: python scripts/patch_nuitka_pywebview_plugin.py
release.yml에 반영하는 방법은 #209 PR 본문의 제안 스니펫 참조(오너 전용 구역이라
이 저장소에서 직접 수정하지 않는다).
"""
import re
import sys
from pathlib import Path

# Windows 러너의 stdout은 파이프로 리다이렉트되면 로케일 기반 인코딩(cp1252 등)으로
# 열려 한글 출력이 UnicodeEncodeError로 죽는다 (#204와 동일 원인, scripts/ 공통 패턴).
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

TARGET_TUPLE_ENTRY = '"webview.platforms.win32",'
MARKER_LINE = '"webview.platforms.winforms",'


def find_plugin_file() -> Path:
    import nuitka.plugins.standard.PywebViewPlugin as plugin_module

    return Path(plugin_module.__file__)


def is_patched(text: str) -> bool:
    return "webview.platforms.win32" in text


def apply_patch(text: str) -> str:
    if MARKER_LINE not in text:
        raise RuntimeError(
            f"Nuitka PywebViewPlugin.py의 예상 위치({MARKER_LINE!r})를 찾지 못했다 — "
            "Nuitka 버전이 바뀌어 플러그인 구조가 달라졌을 수 있다. 수동으로 확인할 것."
        )
    # winforms 항목 바로 뒤에 win32 항목을 추가한다 (들여쓰기를 그대로 맞춘다)
    pattern = re.compile(r'([ \t]*)' + re.escape(MARKER_LINE))
    match = pattern.search(text)
    indent = match.group(1)
    replacement = f"{indent}{MARKER_LINE}\n{indent}{TARGET_TUPLE_ENTRY}"
    return pattern.sub(replacement, text, count=1)


def main() -> int:
    if sys.platform != "win32":
        print("[patch_nuitka_pywebview_plugin] Windows가 아니므로 건너뜀 (#208 조사상 다른 OS엔 이 버그가 없음).")
        return 0

    plugin_path = find_plugin_file()
    text = plugin_path.read_text(encoding="utf-8")

    if is_patched(text):
        print(f"[patch_nuitka_pywebview_plugin] 이미 패치됨: {plugin_path}")
        return 0

    patched = apply_patch(text)
    plugin_path.write_text(patched, encoding="utf-8")
    print(f"[patch_nuitka_pywebview_plugin] 패치 적용됨: {plugin_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
