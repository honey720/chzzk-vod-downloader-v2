"""실행 중 테마 전환 게이트 — OS 테마를 바꾸면 앱이 **반쪽 없이** 따라가는가 (#240, SPEC §8.5·§9).

핵심 질문 하나: **"시작부터 라이트로 띄운 화면"과 "다크로 띄웠다가 라이트로
전환한 화면"이 같은가.** 같으면 전환이 완전하고, 다르면 그 차이가 곧 빠뜨린
지점이다(SPEC §9 — 반쪽만 따라가는 것은 일관되게 안 따라가는 것보다 나쁘다).

- 비교 대상은 **렌더 픽셀**이다(`widget.grab()`), 스타일시트 문자열이 아니다.
  같은 방식으로 만든 두 화면의 같은 위젯을 같은 폰트로 그리므로 픽셀 단위
  동일성을 그대로 단언할 수 있다 — 폰트 의존 값을 절대치로 박지 않는다.
- 카드 다섯 상태(대기·진행·일시정지·완료·실패)를 실제 배선(`model.addItem` →
  `ContentListView`)으로 메인 창에 올린 채 전환한다. 헤더·입력창·버튼·목록
  배경·진행바·pill·경로 라벨·상태 텍스트·조작 아이콘을 각각 비교한다.
- 양방향(다크→라이트, 라이트→다크).
- 숨은 위젯은 낡은 값을 들고 있으므로 보이는 위젯만 잰다 — 다만 "보이는지"
  자체가 두 화면에서 같아야 하고(같은 상태이므로), 보여야 할 핵심 위젯은
  `shown()`으로 단언한다(#245의 거짓 초록 차단).
- offscreen에는 `colorSchemeChanged`가 오지 않는다. 전환은 시작 시점과 같은
  함수 `theme.apply_color_scheme()`에 스킴을 주입해 일으키고, OS 신호 배선
  자체는 `QStyleHints.setColorScheme()`(같은 신호를 낸다)으로 따로 잰다.

⚠️ 이 파일의 테스트는 본질적으로 전역 상태(팔레트·QSS·스킴)를 바꾼다.
`_theme_sandbox` 픽스처가 teardown에서 전부 원상 복구하고,
`TestGlobalStateRestored`가 그 복구를 잰다 — #246에서 전역 QSS를 복원하지
않는 파일 9개가 후행 테스트를 깨뜨린 것이 확인됐고, 이 파일이 열 번째가
되면 안 된다. 스타일 객체는 `hold_style()`을 거친다(#243 이중 해제).
"""

import sys
import time
from contextlib import contextmanager

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import main as main_module
import theme
from app.views.mainWindow import VodDownloader
from content.data import ContentItem
from core.models.download_state import DownloadState
from tests.unit.card_helpers import hold_style, shown

QSS_PATH = main_module.resource_path(theme.QSS_RELATIVE_PATH)


# ============ 전역 상태 샌드박스 ============


def _snapshot_global_state(qapp) -> dict:
    return {
        "scheme": theme._current_scheme,
        "palette": qapp.palette(),
        "stylesheet": qapp.styleSheet(),
    }


def _restore_global_state(qapp, snapshot: dict) -> None:
    theme.set_color_scheme(snapshot["scheme"])
    qapp.setPalette(snapshot["palette"])
    qapp.setStyleSheet(snapshot["stylesheet"])
    QApplication.processEvents()


@contextmanager
def theme_sandbox(qapp):
    """팔레트·QSS·스킴을 건드린 뒤 **반드시 원상 복구**하는 구역.

    스타일(`qapp.setStyle`)은 복구 대상에 넣지 않는다 — 이전 스타일 객체를
    다시 거는 것이 #243 이중 해제 경로 그 자체이고, 다른 GUI 게이트도 스타일은
    갈아 끼우기만 한다. 대신 `hold_style()`을 거쳐 참조를 붙든다.
    """
    snapshot = _snapshot_global_state(qapp)
    qapp.setStyle(hold_style(theme.build_style()))
    flash_time = qapp.cursorFlashTime()
    qapp.setCursorFlashTime(0)  # 캐럿 깜빡임 정지 — 렌더 비교가 시간에 흔들리지 않게
    try:
        yield
    finally:
        qapp.setCursorFlashTime(flash_time)
        _restore_global_state(qapp, snapshot)


@pytest.fixture
def _theme_sandbox(qapp, monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    with theme_sandbox(qapp):
        yield


# ============ 장면 — 메인 창 + 카드 다섯 상태 ============

STATES = (
    DownloadState.WAITING,
    DownloadState.RUNNING,
    DownloadState.PAUSED,
    DownloadState.FINISHED,
    DownloadState.FAILED,
)
PROGRESS = {DownloadState.RUNNING: 42, DownloadState.PAUSED: 54, DownloadState.FINISHED: 100}


def _make_item(i: int) -> ContentItem:
    return ContentItem(
        f"https://chzzk.naver.com/video/{i}",
        {"title": f"제목 {i}", "category": "", "channelName": f"채널{i}", "createdDate": "", "duration": 3600},
        [["1080", "u1"], ["720", "u2"]],
        None,
        "",
        "C:/dl",
        "video",
        None,
    )


class Scene:
    """메인 창에 카드 다섯 상태를 올린 화면. `components()`가 비교 대상 위젯을 이름으로 돌려준다."""

    def __init__(self):
        self.window = VodDownloader()
        # 두 장면이 config 상태에 기대지 않게 입력값을 고정한다 — 앞 장면의 창이
        # 닫히며 경로를 저장(구분자 정규화)하고 뒤 장면이 그 값을 읽어 오면 글자만
        # 달라져 테마와 무관한 차이가 난다(Windows 실기 실측: 슬래시가 역슬래시로).
        self.window.downloadPathInput.setText("C:/dl")
        self.window.urlInput.setText("")
        self.items = []
        for i, state in enumerate(STATES):
            item = _make_item(i)
            item.downloadState = DownloadState.WAITING
            self.window.contentManager.model.addItem(item)
            widget = self.window.listView.widgetFor(item)  # 뷰가 pill까지 만들어 둔 실제 배선
            widget.setresolutionUrlSize("1080", "u1", 0, widget.buttons[0])  # 유저가 1080p를 고른 상태
            item.total_size = "595.34 MB"
            item.downloadState = state
            item.download_progress = PROGRESS.get(state, 0)
            item.download_speed = "8.2 MB/s"
            item.download_remain_time = "00:12:34"
            item.download_size = 624_000_000
            item.download_time = "00:05:12"
            item.stateMessage = "Failed to save file" if state == DownloadState.FAILED else ""
            widget.setData(item, i)
            self.items.append(item)
        self.window.resize(900, 760)
        self.window.show()
        QApplication.processEvents()
        # 입력창에 포커스가 남으면 캐럿 깜빡임 위상이 장면마다 달라 테마와 무관한
        # 픽셀 차이가 난다(전체 스위트 맥락에서 간헐 실측) — 포커스를 뺀다.
        self.window.urlInput.clearFocus()
        self.window.downloadPathInput.clearFocus()
        QApplication.processEvents()

    def close(self) -> None:
        self.window.close()
        self.window.deleteLater()
        QApplication.processEvents()

    def card(self, state) -> object:
        return self.window.listView.widgetFor(self.items[STATES.index(state)])

    def components(self) -> dict:
        """비교 대상 위젯 — 이름 → 위젯. 화면 구성 요소를 하나씩 셀 수 있게 이름을 붙인다."""
        w = self.window
        parts = {
            "header.frame": w.headerFrame,
            "header.urlInput": w.urlInput,
            "header.fetchButton": w.fetchButton,
            "header.downloadPathInput": w.downloadPathInput,
            "header.downloadPathButton": w.downloadPathButton,
            "header.settingButton": w.settingButton,
            "header.linkStatusLabel": w.linkStatusLabel,
            "list.viewport": w.listView.viewport(),
            "footer.frame": w.infoFrame,
            "footer.downloadCountLabel": w.downloadCountLabel,
            "footer.clearFinishedButton": w.clearFinishedButton,
            "footer.downloadButton": w.downloadButton,
            "footer.stopButton": w.stopButton,
        }
        for state in STATES:
            card = self.card(state)
            prefix = f"card[{state.name.lower()}]"
            parts[f"{prefix}.frame"] = card.contentFrame
            parts[f"{prefix}.thumbnail"] = card.thumbnailLabel
            parts[f"{prefix}.channelName"] = card.channelNameLabel
            parts[f"{prefix}.title"] = card.titleLabel
            parts[f"{prefix}.status"] = card.statusLabel
            parts[f"{prefix}.pathLabel"] = card.directoryLabel
            parts[f"{prefix}.pathIcon"] = card.pathIconButton
            parts[f"{prefix}.fileSize"] = card.fileSizeLabel
            parts[f"{prefix}.progressBar"] = card.progressBar
            parts[f"{prefix}.pauseButton"] = card.pauseButton
            parts[f"{prefix}.openDirectoryButton"] = card.openDirectoryButton
            parts[f"{prefix}.retryButton"] = card.retryButton
            parts[f"{prefix}.deleteButton"] = card.deleteButton
            for n, pill in enumerate(card.buttons):
                parts[f"{prefix}.pill{n}"] = pill
        return parts

    def snapshot(self, qtbot) -> dict:
        """보이는 위젯의 렌더 픽셀. 숨은 위젯은 `None`으로 기록해 '보임 여부'도 비교에 넣는다.

        찍기 전에 렌더가 **안정될 때까지** 기다린다 — `QLineEdit`의 지우기(✕)
        버튼은 텍스트가 생기면 페이드인 애니메이션으로 나타나서, 시작 직후에
        찍은 장면과 전환 뒤(시간이 지난) 장면이 테마와 무관하게 달랐다(실측:
        입력창 오른쪽 14×14px). 고정 대기가 아니라 "일정 시간 동안 픽셀이
        안 바뀌었다"를 조건으로 기다린다.
        """
        _wait_render_settled(qtbot, self.window)
        return {
            name: (widget.grab().toImage() if widget.isVisible() else None)
            for name, widget in self.components().items()
        }


def _wait_render_settled(qtbot, widget, quiet_ms: int = 150) -> None:
    """`widget`의 렌더 픽셀이 `quiet_ms` 동안 변하지 않을 때까지 기다린다(애니메이션 종료 대기)."""
    state = {"image": widget.grab().toImage(), "since": time.monotonic()}

    def _settled() -> bool:
        image = widget.grab().toImage()
        if image != state["image"]:
            state["image"] = image
            state["since"] = time.monotonic()
            return False
        return (time.monotonic() - state["since"]) * 1000 >= quiet_ms

    qtbot.waitUntil(_settled, timeout=5000)


def _fresh(qapp, qtbot, scheme: str) -> dict:
    """`scheme`으로 시작한 화면의 스냅샷."""
    theme.apply_color_scheme(qapp, scheme, QSS_PATH)
    scene = Scene()
    try:
        _assert_key_parts_shown(scene)
        return scene.snapshot(qtbot)
    finally:
        scene.close()


def _switched(qapp, qtbot, start: str, then: str, apply=None) -> dict:
    """`start`로 띄운 뒤 화면을 그대로 둔 채 `then`으로 전환한 화면의 스냅샷.

    `apply`를 주면 전환에 그 함수를 쓴다 — 고장 주입용(한 단계를 뺀 전환).
    """
    apply = apply or (lambda scheme: theme.apply_color_scheme(qapp, scheme, QSS_PATH))
    theme.apply_color_scheme(qapp, start, QSS_PATH)
    scene = Scene()
    try:
        apply(then)
        _assert_key_parts_shown(scene)
        return scene.snapshot(qtbot)
    finally:
        scene.close()


def _assert_key_parts_shown(scene: Scene) -> None:
    """반드시 보여야 하는 것들이 실제로 보이는지 — 숨은 채 통과하는 길을 막는다."""
    w = scene.window
    shown(w.urlInput)
    shown(w.fetchButton)
    shown(w.downloadButton)
    assert w.listView.viewport().isVisible()
    for state in STATES:
        card = scene.card(state)
        shown(card.titleLabel)
        assert card.contentFrame.isVisible()
    assert scene.card(DownloadState.RUNNING).progressBar.isVisible()
    assert scene.card(DownloadState.PAUSED).progressBar.isVisible()
    shown(scene.card(DownloadState.FAILED).statusLabel)
    assert scene.card(DownloadState.WAITING).buttons and scene.card(DownloadState.WAITING).buttons[0].isVisible()


def _differences(a: dict, b: dict) -> list[str]:
    assert a.keys() == b.keys()
    return [name for name in a if a[name] != b[name]]


# ============ 게이트 ============


@pytest.mark.usefixtures("_theme_sandbox")
class TestRuntimeSwitchIsComplete:
    @pytest.mark.parametrize("start,then", [("dark", "light"), ("light", "dark")])
    def test_switched_scene_renders_exactly_like_a_fresh_start(self, qapp, qtbot, start, then):
        fresh = _fresh(qapp, qtbot, then)
        switched = _switched(qapp, qtbot, start, then)

        assert _differences(fresh, switched) == []

    @pytest.mark.parametrize("start,then", [("dark", "light"), ("light", "dark")])
    def test_switch_actually_changes_the_scene(self, qapp, qtbot, start, then):
        """비교가 공허하지 않은지 — 전환 전 화면과는 달라야 한다(다크와 라이트가
        같은 픽셀이면 위 동일성 게이트는 아무것도 재지 않은 것)."""
        before = _fresh(qapp, qtbot, start)
        after = _switched(qapp, qtbot, start, then)

        changed = _differences(before, after)
        assert "header.frame" in changed
        assert "list.viewport" in changed
        assert f"card[{DownloadState.RUNNING.name.lower()}].progressBar" in changed
        assert f"card[{DownloadState.RUNNING.name.lower()}].frame" in changed


@pytest.mark.usefixtures("_theme_sandbox")
class TestFaultInjection:
    """게이트가 '반쪽 전환'을 실제로 잡는지 — 전환의 한 단계씩 빼 본다.

    제품 코드의 `apply_color_scheme()`은 세 단계(스킴 확정 → 팔레트 → QSS
    재로드)뿐이고 별도의 repolish 순회나 파이썬 색 갱신 코드가 없다 —
    `app.setStyleSheet()`이 모든 위젯을 다시 polish하고, `IconButton`은 paint
    시점에 토큰을 읽기 때문이다. 그래서 "빼 볼 한 줄"은 그 세 단계와, 파이썬
    페인트가 토큰을 **새로 읽지 않는** 상황(아이콘이 생성 시점 색을 캐시했다면
    생겼을 결함)이다. 각각 어느 구성 요소에서 드러나는지까지 단언한다.
    """

    def test_skipping_the_qss_reload_leaves_qss_driven_parts_stale(self, qapp, qtbot):
        def apply_without_qss(scheme):
            theme.set_color_scheme(scheme)
            qapp.setPalette(theme.build_palette())

        fresh = _fresh(qapp, qtbot, "light")
        switched = _switched(qapp, qtbot, "dark", "light", apply=apply_without_qss)

        stale = _differences(fresh, switched)
        assert "card[running].progressBar" in stale  # [state="running"] 진행바 색
        assert "card[running].frame" in stale  # 카드 배경(#contentFrame)
        assert "header.fetchButton" in stale  # 버튼 QSS

    def test_skipping_the_palette_leaves_palette_driven_parts_stale(self, qapp, qtbot):
        def apply_without_palette(scheme):
            theme.set_color_scheme(scheme)
            qapp.setStyleSheet(theme.load_stylesheet(QSS_PATH))

        fresh = _fresh(qapp, qtbot, "light")
        switched = _switched(qapp, qtbot, "dark", "light", apply=apply_without_palette)

        stale = _differences(fresh, switched)
        # 글자색은 QSS가 아니라 팔레트(WindowText)가 칠한다 — 라벨들이 낡은 색으로 남는다
        assert "header.linkStatusLabel" in stale
        assert "footer.downloadCountLabel" in stale
        assert "card[waiting].title" in stale
        assert "card[waiting].pathLabel" in stale

    def test_icons_that_cache_their_color_would_be_caught(self, qapp, qtbot, monkeypatch):
        """파이썬 색 계산 갱신을 한 곳 빼기 — `IconButton`이 토큰을 paint마다 새로
        읽지 않고 전환 전 표를 붙들고 있다면(캐시), 조작 아이콘이 낡은 색으로
        남는다. 그 상황을 만들어 게이트가 아이콘에서 잡는지 본다."""
        from app.widgets import icons

        frozen = dict(theme.DARK)
        original_paint = icons.IconButton.paintEvent

        def paint_with_frozen_tokens(self, event):
            monkeypatch.setattr(theme, "current_tokens", lambda: frozen)
            try:
                original_paint(self, event)
            finally:
                monkeypatch.setattr(theme, "current_tokens", real_current_tokens)

        real_current_tokens = theme.current_tokens
        fresh = _fresh(qapp, qtbot, "light")
        monkeypatch.setattr(icons.IconButton, "paintEvent", paint_with_frozen_tokens)
        switched = _switched(qapp, qtbot, "dark", "light")

        stale = _differences(fresh, switched)
        assert "card[running].pauseButton" in stale
        assert "card[waiting].deleteButton" in stale
        assert "card[failed].retryButton" in stale


@pytest.mark.usefixtures("_theme_sandbox")
class TestSwitchIsAtomic:
    """전환은 원자적이다 — QSS 로드가 실패하면 **아무것도 바뀌지 않는다**.

    팔레트·토큰은 새 테마인데 QSS만 옛 테마로 남는 "반쪽"은 SPEC §9가 금지한
    바로 그 상태다(일관되게 안 따라가는 것보다 나쁘다). 새 스타일시트를 먼저
    준비·검증하고 성공했을 때만 색을 커밋하는지, 실패 뒤 정상 전환이 되는지,
    그리고 원자성을 깨면(색 먼저 커밋) 이 게이트가 잡는지를 잰다.
    """

    def test_missing_qss_changes_nothing(self, qapp, tmp_path):
        theme.apply_color_scheme(qapp, "dark", QSS_PATH)
        before = _snapshot_global_state(qapp)

        with pytest.raises(OSError):
            theme.apply_color_scheme(qapp, "light", str(tmp_path / "missing.qss"))

        assert _snapshot_global_state(qapp) == before  # 팔레트·QSS·스킴 전부 옛 테마 그대로
        assert theme.current_tokens() is theme.DARK

    def test_broken_token_changes_nothing(self, qapp, tmp_path):
        broken = tmp_path / "broken.qss"
        broken.write_text("QWidget { color: @noSuchToken; }", encoding="utf-8")
        theme.apply_color_scheme(qapp, "dark", QSS_PATH)
        before = _snapshot_global_state(qapp)

        with pytest.raises(KeyError):
            theme.apply_color_scheme(qapp, "light", str(broken))

        assert _snapshot_global_state(qapp) == before

    def test_unknown_scheme_changes_nothing(self, qapp):
        theme.apply_color_scheme(qapp, "dark", QSS_PATH)
        before = _snapshot_global_state(qapp)

        with pytest.raises(ValueError):
            theme.apply_color_scheme(qapp, "sepia", QSS_PATH)

        assert _snapshot_global_state(qapp) == before

    def test_switch_recovers_after_a_failed_switch(self, qapp, qtbot, tmp_path):
        """실패한 전환이 상태를 망가뜨린 채 남기지 않는지 — 실패 뒤 정상 전환한
        화면이 처음부터 그 테마로 띄운 화면과 픽셀까지 같아야 한다."""
        missing = str(tmp_path / "missing.qss")

        def fail_then_switch(scheme):
            with pytest.raises(OSError):
                theme.apply_color_scheme(qapp, scheme, missing)
            theme.apply_color_scheme(qapp, scheme, QSS_PATH)

        fresh = _fresh(qapp, qtbot, "light")
        switched = _switched(qapp, qtbot, "dark", "light", apply=fail_then_switch)

        assert _differences(fresh, switched) == []

    def test_follower_keeps_the_old_theme_when_reload_fails(self, qapp, tmp_path):
        """OS 신호 경로에서 실패하면 예외 없이 옛 테마를 지킨다(로그만)."""
        theme.apply_color_scheme(qapp, "dark", QSS_PATH)
        before = _snapshot_global_state(qapp)
        slot = theme.follow_os_color_scheme(qapp, str(tmp_path / "missing.qss"))
        try:
            qapp.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
            QApplication.processEvents()
            assert _snapshot_global_state(qapp) == before
        finally:
            theme.unfollow_os_color_scheme(qapp, slot)

    def test_fault_injection_committing_colors_first_is_caught(self, qapp, monkeypatch, tmp_path):
        """원자성을 깨서(스킴·팔레트를 먼저 커밋) 위 게이트가 잡는지 — 반쪽 상태가
        실제로 만들어지는 것을 보인다."""

        def non_atomic(app, scheme, qss_path):
            theme.set_color_scheme(scheme)
            app.setPalette(theme.build_palette())
            app.setStyleSheet(theme.load_stylesheet(qss_path))

        theme.apply_color_scheme(qapp, "dark", QSS_PATH)
        before = _snapshot_global_state(qapp)
        monkeypatch.setattr(theme, "apply_color_scheme", non_atomic)

        with pytest.raises(OSError):
            theme.apply_color_scheme(qapp, "light", str(tmp_path / "missing.qss"))

        after = _snapshot_global_state(qapp)
        assert after != before  # 팔레트·스킴은 라이트, QSS는 다크 — 게이트가 잡아야 하는 반쪽
        assert after["stylesheet"] == before["stylesheet"]
        assert theme.current_tokens() is theme.LIGHT


@pytest.mark.usefixtures("_theme_sandbox")
class TestOsSignalWiring:
    def test_color_scheme_changed_signal_reapplies_the_theme(self, qapp):
        """`follow_os_color_scheme()`이 건 배선이 실제 신호에 반응하는지.

        offscreen에서는 `QStyleHints.setColorScheme()`조차 `Unknown`에 머물러
        신호가 안 난다(실측) — 같은 신호를 파이썬에서 직접 emit해 배선만 잰다.
        OS가 실제로 신호를 보내는지는 오너 실기 확인 대상이다."""
        hints = qapp.styleHints()
        theme.apply_color_scheme(qapp, "dark", QSS_PATH)
        slot = theme.follow_os_color_scheme(qapp, QSS_PATH)
        try:
            hints.colorSchemeChanged.emit(Qt.ColorScheme.Light)
            QApplication.processEvents()
            assert theme.current_tokens() is theme.LIGHT
            assert theme.LIGHT["windowBg"] in qapp.styleSheet()

            hints.colorSchemeChanged.emit(Qt.ColorScheme.Dark)
            QApplication.processEvents()
            assert theme.current_tokens() is theme.DARK
            assert theme.DARK["windowBg"] in qapp.styleSheet()
        finally:
            theme.unfollow_os_color_scheme(qapp, slot)

    def test_unfollow_disconnects(self, qapp):
        hints = qapp.styleHints()
        theme.apply_color_scheme(qapp, "dark", QSS_PATH)
        slot = theme.follow_os_color_scheme(qapp, QSS_PATH)
        theme.unfollow_os_color_scheme(qapp, slot)

        hints.colorSchemeChanged.emit(Qt.ColorScheme.Light)
        QApplication.processEvents()
        assert theme.current_tokens() is theme.DARK


class TestGlobalStateRestored:
    def test_sandbox_restores_palette_stylesheet_and_scheme(self, qapp):
        """이 파일이 전역 상태를 남기지 않는지 — 샌드박스를 지나온 뒤 팔레트·QSS·
        스킴이 들어가기 전과 같아야 한다."""
        before = _snapshot_global_state(qapp)

        with theme_sandbox(qapp):
            theme.apply_color_scheme(qapp, "light" if before["scheme"] == "dark" else "dark", QSS_PATH)
            assert _snapshot_global_state(qapp) != before  # 안에서는 실제로 바뀌었다

        assert _snapshot_global_state(qapp) == before

    def test_fault_injection_without_restore_the_gate_catches_it(self, qapp, monkeypatch):
        """복구를 빼면 위 게이트가 실패해야 한다 — 복구 게이트가 진짜로 재는지."""
        before = _snapshot_global_state(qapp)
        monkeypatch.setattr(sys.modules[__name__], "_restore_global_state", lambda qapp, snapshot: None)
        try:
            with theme_sandbox(qapp):
                theme.apply_color_scheme(qapp, "light" if before["scheme"] == "dark" else "dark", QSS_PATH)
            assert _snapshot_global_state(qapp) != before
        finally:
            monkeypatch.undo()
            _restore_global_state(qapp, before)
        assert _snapshot_global_state(qapp) == before
