"""scripts/patch_nuitka_pywebview_plugin.py 검증 (#209).

Nuitka의 PywebViewPlugin.py가 Windows에서 webview.platforms.win32를 화이트
리스트에서 빠뜨려 pywebview onefile/standalone 빌드가 실행 시 죽는 버그를
회피하는 패치 스크립트다. 실제 설치된 Nuitka 패키지 파일은 건드리지 않고,
임시 파일에 최소 재현 내용을 만들어 그 파일을 대상으로 검증한다.
"""

import pytest

import scripts.patch_nuitka_pywebview_plugin as patch_module


FAKE_PLUGIN_SOURCE = '''\
class NuitkaPluginPywebview(NuitkaPluginBase):
    def onModuleEncounter(self, using_module_name, module_name, module_filename, module_kind):
        if module_name.isBelowNamespace("webview.platforms"):
            if isWin32Windows():
                result = module_name in (
                    "webview.platforms.winforms",
                    "webview.platforms.edgechromium",
                    "webview.platforms.edgehtml",
                    "webview.platforms.mshtml",
                    "webview.platforms.cef",
                )
                reason = "Platforms package of webview used on '%s'." % getOS()
            return result, reason
'''


@pytest.fixture
def fake_plugin_path(tmp_path, monkeypatch):
    """find_plugin_file()이 실제 Nuitka 설치 대신 임시 파일을 가리키게 한다."""
    path = tmp_path / "PywebViewPlugin.py"
    path.write_text(FAKE_PLUGIN_SOURCE, encoding="utf-8")
    monkeypatch.setattr(patch_module, "find_plugin_file", lambda: path)
    return path


def test_is_patched_false_on_original_source():
    assert patch_module.is_patched(FAKE_PLUGIN_SOURCE) is False


def test_is_patched_true_after_patch():
    patched = patch_module.apply_patch(FAKE_PLUGIN_SOURCE)
    assert patch_module.is_patched(patched) is True


def test_apply_patch_inserts_win32_right_after_winforms_with_matching_indent():
    patched = patch_module.apply_patch(FAKE_PLUGIN_SOURCE)
    lines = patched.splitlines()
    winforms_idx = next(i for i, line in enumerate(lines) if "webview.platforms.winforms" in line)
    win32_idx = next(i for i, line in enumerate(lines) if "webview.platforms.win32" in line)

    assert win32_idx == winforms_idx + 1
    # 들여쓰기가 winforms 줄과 정확히 같아야 한다 (문법 깨짐 방지)
    winforms_indent = lines[winforms_idx][: len(lines[winforms_idx]) - len(lines[winforms_idx].lstrip())]
    win32_indent = lines[win32_idx][: len(lines[win32_idx]) - len(lines[win32_idx].lstrip())]
    assert win32_indent == winforms_indent

    # 나머지 항목(edgechromium 등)은 그대로 보존돼야 한다
    assert '"webview.platforms.edgechromium",' in patched
    assert '"webview.platforms.cef",' in patched


def test_apply_patch_raises_if_marker_not_found():
    """플러그인 구조가 바뀌어 winforms 항목을 못 찾으면 조용히 넘어가지 않고 명확히 실패한다."""
    unrelated_source = "class SomethingElse:\n    pass\n"
    with pytest.raises(RuntimeError, match="찾지 못했다"):
        patch_module.apply_patch(unrelated_source)


def test_main_patches_unpatched_file_on_windows(fake_plugin_path, monkeypatch):
    monkeypatch.setattr(patch_module.sys, "platform", "win32")

    exit_code = patch_module.main()

    assert exit_code == 0
    assert patch_module.is_patched(fake_plugin_path.read_text(encoding="utf-8"))


def test_main_is_idempotent_on_already_patched_file(fake_plugin_path, monkeypatch):
    monkeypatch.setattr(patch_module.sys, "platform", "win32")
    fake_plugin_path.write_text(patch_module.apply_patch(FAKE_PLUGIN_SOURCE), encoding="utf-8")
    before = fake_plugin_path.read_text(encoding="utf-8")

    exit_code = patch_module.main()

    assert exit_code == 0
    assert fake_plugin_path.read_text(encoding="utf-8") == before  # 두 번째 실행이 내용을 안 바꾼다


def test_main_is_noop_on_non_windows(fake_plugin_path, monkeypatch):
    """macOS·Linux는 이 버그가 없다 (#208에서 코드 구조상 확인 — cocoa.py/gtk.py는
    형제 모듈에 의존하지 않는다) -- 파일을 아예 건드리지 않아야 한다."""
    monkeypatch.setattr(patch_module.sys, "platform", "darwin")
    before = fake_plugin_path.read_text(encoding="utf-8")

    exit_code = patch_module.main()

    assert exit_code == 0
    assert fake_plugin_path.read_text(encoding="utf-8") == before
    assert not patch_module.is_patched(fake_plugin_path.read_text(encoding="utf-8"))
