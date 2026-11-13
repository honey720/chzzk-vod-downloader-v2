"""카드 상태 매트릭스 게이트 (#245 — 상태는 넷이 아니라 다섯이다).

오너 확정 표를 그대로 코드로 옮겨 **셀 하나씩** 단언한다:

| 상태     | 3행 상태 원 · 글           | 조작        | 진행바        |
|----------|----------------------------|-------------|---------------|
| 대기     | (없음) · 다운로드 대기     | 편집(✎)     | 없음          |
| 진행     | ↓ · 42.0% · 8.2 MB/s · 남음  | 일시정지    | 있음          |
| 일시정지 | ‖ · 54.0% · 일시정지됨       | 재개        | 있음(muted)   |
| 완료     | ✓ · 완료                   | 폴더 열기   | 없음          |
| 실패     | ✕ · 사유                   | 재시도      | 없음          |

완료 · 실패 표시는 글 머리 글자가 아니라 상태 원 아이콘(`statusIcon`)이 맡는다(#550).
카드 버튼은 호버 · 키보드 초점 때만 보인다 — 상태마다 놓이는 버튼은 `visibleCardButtons()`로,
보이는지는 호버(`setHovered(True)`) 뒤에 잰다.

`DownloadState.PAUSED`가 첫 슬롯 설계에서 빠진 채 "진행 중일 때만 바 /
일시정지에도 일시정지 버튼"으로 나갔던 것을 되돌린 게이트다 — 멈춰 있는
카드가 이미 한 일(일시정지)을 또 권하면 안 되고, 받은 양은 바가 계속
보여줘야 한다(색만 muted).

조작 아이콘은 폰트 글리프가 아니라 `app/widgets/icons.py`가 그리는 도형이라
`iconName()`으로 폰트 무관하게 단언한다. 슬롯 텍스트는 `text()`(모델 값)로
본다 — 렌더 픽셀은 폰트에 좌우된다.
"""

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QWidget

import CVDv2 as main_module
import app.theme as theme
from app.widgets import icons
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import hold_style, hover_from_tests_only, unfold
from typing import Never


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp) -> None:
    """실제 전역 QSS를 태운다. ⚠️ `scope="function"` 유지(macOS 종료 크래시 —
    `test_widget_theme.py`의 `_apply_dark_card_qss` 문서 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(
        hold_style(theme.build_style())
    )  # 참조 보관 — 이중 해제 우회 (#243, card_helpers.hold_style)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    class _FailingSession:
        def head(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")


@pytest.fixture(autouse=True)
def _hover_from_tests_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드 호버는 테스트가 정한다 — 실제 포인터 자리에 기대지 않는다 (#587)."""
    hover_from_tests_only(monkeypatch)


def _make_widget(qapp, state, progress=0, *, post_process=False) -> ContentItemWidget:
    """상태별 카드. `post_process=True`면 세그먼트 기반(m3u8) 진행 카드의 후처리
    국면 — 전송 100% 뒤 후처리 진행률이 0부터 다시 차오르는 상태다."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 3600,
        },
        [["1080", "u1"], ["720", "u2"]],
        None,
        "",
        "C:/dl",
        "m3u8" if post_process else "video",
        None,
    )
    item.post_process = post_process
    item.downloadState = DownloadState.WAITING
    item.total_size = "595.34 MB"
    widget = ContentItemWidget(item, 0)
    # 해상도 정렬 · 기본 선택(가장 높은 1080) — 크기 조회는 네트워크 차단으로 안 돈다
    widget.loadRepresentations()
    assert item.resolution == "1080", "전제: 가장 높은 해상도가 기본으로 골라진다"
    item.total_size = "595.34 MB"
    item.downloadState = state
    item.download_progress = progress
    item.download_speed = "8.2 MB/s"
    item.download_remain_time = "00:12:34"
    item.download_size = 624_000_000
    item.download_time = "00:05:12"  # 어댑터가 주는 전체 소요(전송+후처리) "HH:MM:SS"
    item.stateMessage = "Failed to save file" if state == DownloadState.FAILED else ""
    widget.setData(item, 0)
    widget.resize(900, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()
    return widget


#: 상태 → (조작 버튼 이름, 도형 이름, 진행바 표시, 진행바 색 상태)
#: 완료 · 실패는 끝까지 찬 상태 색 막대다(#554)
MATRIX = {
    DownloadState.WAITING: ("editButton", "edit", False, "waiting"),
    DownloadState.RUNNING: ("pauseButton", "pause", True, "running"),
    DownloadState.PAUSED: ("pauseButton", "resume", True, "paused"),
    DownloadState.FINISHED: (
        "openDirectoryButton",
        "folder",
        False,
        "finished",
    ),  # 막대 없음 — 색조(#571)
    DownloadState.FAILED: ("retryButton", "retry", False, "failed"),  # 막대 없음 — 색조(#571)
}

ACTION_BUTTONS = ("editButton", "pauseButton", "openDirectoryButton", "retryButton")

#: 상태 → 3줄 상태 원의 그림(StateBubble.state()) — 대기는 그림이 없다
STATUS_ICON = {
    DownloadState.WAITING: "",
    DownloadState.RUNNING: "running",
    DownloadState.PAUSED: "paused",
    DownloadState.FINISHED: "done",
    DownloadState.FAILED: "all_failed",
}


def _left_x(widget: ContentItemWidget, child) -> int:
    """카드 좌표계에서 `child`의 왼쪽 끝 x — 서로 다른 줄의 시작 자리를 견준다."""
    return child.mapTo(widget, QPoint(0, 0)).x()


def _row_head(widget: ContentItemWidget) -> QWidget:
    """3줄의 첫 항목 — 보이는 상태 원, 원이 숨었으면(대기 · 조회 중) 상태 글."""
    return widget.statusIcon if widget.statusIcon.isVisible() else widget.statusLabel


PROGRESS = {DownloadState.RUNNING: 42, DownloadState.PAUSED: 54, DownloadState.FINISHED: 100}


class TestSlotColumn:
    """3행 슬롯 — 상태마다 다른 질문에 답한다."""

    def test_waiting_shows_the_waiting_text_without_an_icon(self, qapp) -> None:
        """대기 카드의 3줄 왼쪽은 그림 없는 상태 원과 "Download waiting"이어야 한다 (#550).

        대기 카드 -> 상태 글 "Download waiting"(보임), 상태 원 그림 ""
        """
        widget = _make_widget(qapp, DownloadState.WAITING)
        assert widget.statusLabel.isVisible()
        assert widget.statusLabel.text() == "Download waiting"
        assert widget.statusIcon.state() == ""

    @pytest.mark.parametrize("state", list(MATRIX))
    def test_status_icon_follows_the_state(self, qapp, state) -> None:
        """3줄 상태 원의 그림은 상태마다 STATUS_ICON 표와 같아야 한다 (#550).

        대기 "" · 진행 "running" · 일시정지 "paused" · 완료 "done" · 실패 "all_failed"
        """
        widget = _make_widget(qapp, state, PROGRESS.get(state, 0))
        assert widget.statusIcon.state() == STATUS_ICON[state]

    def test_running_shows_percent_speed_and_remaining(self, qapp) -> None:
        widget = _make_widget(qapp, DownloadState.RUNNING, 42)
        text = widget.statusLabel.text()
        assert widget.statusLabel.isVisible()
        assert text.startswith("42.0%"), text
        assert "8.2 MB/s" in text and "12:34" in text

    def test_postprocessing_shows_percent_first_then_label(self, qapp) -> None:
        """ "13.0% · 후처리 중" — 후처리 진행률은 이미 있던 값(실기에서 일시정지로
        확인)인데 슬롯이 "후처리 중" 텍스트로 대체되며 퍼센트만 빠졌던 것.
        퍼센트는 **앞** — 전송·일시정지와 자리를 맞춘다(#245 정정)."""
        widget = _make_widget(qapp, DownloadState.RUNNING, 13, post_process=True)
        text = widget.statusLabel.text()
        assert text.startswith("13.0%"), f"후처리 퍼센트가 앞에 없다: {text!r}"
        assert text.endswith("Post-processing"), text
        assert "MB/s" not in text and "left" not in text, (
            "후처리 국면에 전송 속도·남은 시간이 붙어 있다"
        )

    def test_postprocessing_keeps_pause_action_and_bar(self, qapp) -> None:
        """후처리 중 일시정지는 가능하다 — 조작을 숨기거나 비활성화하지 않는다.
        진행바는 후처리에서 0부터 다시 차오르는 값을 그대로 보인다(오너 확정)."""
        widget = _make_widget(qapp, DownloadState.RUNNING, 13, post_process=True)
        # 카드 버튼은 호버 · 키보드 초점 때만 보인다(#550) — 창 활성으로 초점이 들어오는지는 플랫폼마다
        # 달라(xvfb는 들어오지 않음) 호버를 직접 켠다
        widget.setHovered(True)
        QApplication.processEvents()
        assert widget.pauseButton.isVisible() and widget.pauseButton.isEnabled()
        assert widget.pauseButton.iconName() == "pause"
        assert widget.progressBar.isVisible() and widget.progressBar.value() == 1300

    def test_paused_shows_percent_first_then_paused(self, qapp) -> None:
        widget = _make_widget(qapp, DownloadState.PAUSED, 54)
        text = widget.statusLabel.text()
        assert widget.statusLabel.isVisible()
        assert text.startswith("54.0%"), f"진행분이 앞에 와야 한다: {text!r}"
        assert text.endswith("Paused"), f"뒤 문구는 '일시정지됨'(Paused)이어야 한다: {text!r}"
        assert "Download paused" not in text, (
            "옛 문구('다운로드 정지')가 남아 있다 — 정지는 완전 중단으로 읽힌다"
        )
        assert "left" not in text, "일시정지에 남은 시간이 붙어 있다 — 멈춘 상태에 남은 시간은 없다"

    def test_finished_shows_check_completed_and_elapsed(self, qapp) -> None:
        """ "완료 · 5:12" — 소요 시간은 진행 중의 남은 시간과 같은 짧은 포맷.
        전송 성능에 들인 공이 유저에게 보이는 유일한 자리다(#245). ✓는 상태 원이 맡는다(#550)."""
        widget = _make_widget(qapp, DownloadState.FINISHED, 100)
        assert widget.statusLabel.text() == "Completed · 5:12", widget.statusLabel.text()

    def test_finished_elapsed_uses_the_same_short_format_as_remaining(self, qapp) -> None:
        widget = _make_widget(qapp, DownloadState.FINISHED, 100)
        widget.item.download_time = "01:02:03"
        widget.setData(widget.item, 0)
        assert widget.statusLabel.text().endswith("· 1:02:03")
        widget.item.download_time = "00:00:07"
        widget.setData(widget.item, 0)
        assert widget.statusLabel.text().endswith("· 0:07")

    def test_finished_without_elapsed_shows_completed_only(self, qapp) -> None:
        """값이 없으면 억지로 채우지 않는다 — "완료"만."""
        widget = _make_widget(qapp, DownloadState.FINISHED, 100)
        widget.item.download_time = ""
        widget.setData(widget.item, 0)
        assert widget.statusLabel.text() == "Completed"

    def test_failed_shows_reason_without_a_cross_glyph(self, qapp) -> None:
        """실패 카드의 상태 글은 「✕ 」 머리 없이 사유만이어야 한다 — ✕는 상태 원이 맡는다 (#550).

        사유 "Failed to save file" -> 상태 글 "Failed to save file"
        """
        widget = _make_widget(qapp, DownloadState.FAILED)
        assert widget.statusLabel.text() == "Failed to save file"

    def test_failed_without_mapped_reason_says_unknown_error_not_silence(self, qapp) -> None:
        """매핑 밖 예외(사유 빈 문자열) — 침묵 대신 "알 수 없는 오류 · 로그를
        확인해 주세요". str(e)는 노출하지 않는다(SPEC §5). 틀린 문구도 없는
        문구도 진단을 막는다(#183) — 틀리지 않으면서 다음 행동(로그 폴더
        열기 #181)을 준다(#245)."""
        widget = _make_widget(qapp, DownloadState.FAILED)
        widget.item.stateMessage = ""
        widget.setData(widget.item, 0)
        assert widget.statusLabel.text() == "Unknown error - check the log"
        assert "Download failed" not in widget.statusLabel.text()

    def test_failure_reason_shows_only_the_headline_and_keeps_detail_in_the_tooltip(
        self, qapp
    ) -> None:
        """사유 문구는 "첫 줄=핵심(할 일 포함) / 둘째 줄부터=상세"다 — 3행에는 첫
        줄만, 전문은 툴팁. 실패 사유는 마우스를 올려야 보이면 안 된다(#245)."""
        widget = _make_widget(qapp, DownloadState.FAILED)
        widget.item.stateMessage = "Segments look corrupted · download again\nPostprocessing failed because segments were corrupted."
        widget.setData(widget.item, 0)
        # 사유 뒤(조치 안내)는 좁으면 접힌다(#554) — 대체 글꼴에서도 다 들어가는 폭에서 잰다
        widget.resize(2400, widget.height())
        QApplication.processEvents()
        assert widget.statusLabel.text() == "Segments look corrupted · download again"
        assert widget.statusLabel.toolTip() == widget.item.stateMessage

    #: 첫 줄 글자 수 상한 — 언어별. 640px 카드 3행 실측(Windows 실기, 기본 폰트):
    #: ko 44자 온전·47자 잘림 → 40 / en 65자 온전·76자 잘림 → 60. 한글은 전각,
    #: 영문은 반각·비례폭이라 같은 글자 수의 폭이 다르다(실측 비 약 1.5배) —
    #: 한 상한을 두 언어에 같이 쓰면 한쪽은 헐겁고 한쪽은 잘린다.
    HEADLINE_LIMIT = {"ko_KR": 40, "en_US": 60}

    @pytest.mark.parametrize("language", ("ko_KR", "en_US"))
    @pytest.mark.parametrize(
        "context", ("DownloadViewModel", "ContentViewModel", "ContentItemWidget")
    )
    def test_every_failure_headline_fits_the_card_row(self, language, context) -> None:
        """모든 사유의 **첫 줄**이 640px 카드 3행에 들어간다 — 폰트 무의존 대리
        지표로 언어별 글자 수 상한을 건다(HEADLINE_LIMIT). 원문 전체가 아니라
        첫 줄만 재는 것이 규칙이다 — 상세(둘째 줄)는 툴팁이 맡는다. en은
        en_US.ts의 translation(=표시 문자열)을 본다 — 소스가 아니라 실제로
        뜨는 쪽이다."""
        import io
        import re

        source = io.open(
            main_module.resource_path(f"translations/{language}.ts"), encoding="utf-8"
        ).read()
        block = re.search(rf"<name>{context}</name>(.*?)</context>", source, re.S).group(1)
        failures = [
            (src, tr)
            for src, tr in re.findall(
                r"<source>([^<]*)</source>\s*<translation>([^<]*)</translation>", block
            )
            if any(
                k in src
                for k in (
                    "fail",
                    "Fail",
                    "required",
                    "not found",
                    "Decryption",
                    "Invalid",
                    "Network",
                    "Unknown",
                    "corrupted",
                    "ffmpeg",
                )
            )
        ]
        assert failures, f"{language}/{context}: 실패 문구를 하나도 못 찾았다 — 필터를 확인할 것"
        limit = self.HEADLINE_LIMIT[language]
        too_long = {
            src: tr.splitlines()[0] for src, tr in failures if len(tr.splitlines()[0]) > limit
        }
        assert not too_long, f"{language}: 640px에서 잘릴 첫 줄({limit}자 초과): {too_long}"

    def test_english_source_strings_follow_the_headline_detail_convention(self) -> None:
        """영문 원문(tr() 소스)도 첫 줄=핵심 / 둘째 줄=상세다 — ko 번역만 갈라
        두면 영어로 띄울 때 긴 한 줄이 그대로 잘린다(#245 정정)."""
        import io
        import re

        source = io.open(
            main_module.resource_path("translations/en_US.ts"), encoding="utf-8"
        ).read()
        block = re.search(r"<name>DownloadViewModel</name>(.*?)</context>", source, re.S).group(1)
        sources = re.findall(r"<source>([^<]*)</source>", block)
        multi = [s for s in sources if "\n" in s]
        assert len(multi) >= 7, (
            f"두 줄 규약을 따르는 다운로드 실패 사유가 {len(multi)}개뿐이다: {sources}"
        )
        for s in multi:
            headline, detail = s.split("\n", 1)
            assert "·" in headline, f"첫 줄에 '할 일'(· 뒤)이 없다: {headline!r}"
            assert detail.strip(), f"둘째 줄(상세)이 비었다: {s!r}"

    def test_long_failure_reason_is_elided_but_the_tooltip_keeps_the_whole_text(self, qapp) -> None:
        """긴 사유는 3행에서 ElideRight로 잘릴 수 있다 — 전문은 툴팁이 준다.
        (ko 실측: 640px에서 9종 중 2종이 잘린다 — 후처리 손상·시청 권한 안내)"""
        from PySide6.QtWidgets import QLabel

        widget = _make_widget(qapp, DownloadState.FAILED)
        reason = (
            "Postprocessing failed because the received segments look corrupted, please download again "
            * 2
        )
        widget.item.stateMessage = reason
        widget.setData(widget.item, 0)
        widget.resize(640, widget.sizeHint().height())
        QApplication.processEvents()
        assert QLabel.text(widget.statusLabel) != widget.statusLabel.text(), (
            "이 폭에서 안 잘렸다 — 전제 확인"
        )
        assert reason in widget.statusLabel.toolTip(), "잘린 사유의 전문이 툴팁에 없다"


class TestPercentStartsAtTheSameX:
    """★ 퍼센트가 있는 국면(전송·후처리·일시정지) 전부에서 퍼센트가 **같은 x**에서
    시작한다 — 슬롯 텍스트의 첫 토큰이 퍼센트이고 슬롯 라벨은 컨텐츠 열
    기준선에 있다. 어느 한 국면만 퍼센트를 뒤로 보내면 상태가 바뀔 때마다
    숫자 위치가 흔들려 눈이 움직인다(#245)."""

    PERCENT_PHASES = (
        ("transfer", DownloadState.RUNNING, 42, False),
        ("postprocess", DownloadState.RUNNING, 13, True),
        ("paused", DownloadState.PAUSED, 54, False),
    )

    def test_percent_is_the_first_token_in_every_phase(self, qapp) -> None:
        for name, state, progress, post in self.PERCENT_PHASES:
            widget = _make_widget(qapp, state, progress, post_process=post)
            assert widget.statusLabel.text().split(" ")[0] == f"{progress:.1f}%", (
                f"{name}: 첫 토큰이 퍼센트가 아니다 — {widget.statusLabel.text()!r}"
            )

    def test_percent_column_x_is_identical_across_phases(self, qapp) -> None:
        """전송 · 후처리 · 일시정지에서 상태 글의 시작 x가 같고, 상태 원이 제목 시작 x에 있어야 한다.

        세 국면 카드(900px) -> 상태 글 왼쪽 x 셋이 같다, 상태 원 왼쪽 x == 제목 왼쪽 x
        """
        xs = {}
        for name, state, progress, post in self.PERCENT_PHASES:
            widget = _make_widget(qapp, state, progress, post_process=post)
            xs[name] = _left_x(widget, widget.statusLabel)
            assert _left_x(widget, widget.statusIcon) == _left_x(widget, widget.titleLabel), (
                f"{name}: 3줄이 컨텐츠 열 기준선에 없다"
            )
        assert len(set(xs.values())) == 1, f"국면에 따라 퍼센트 시작 x가 다르다: {xs}"

    def test_all_five_states_and_postprocess_keep_the_slot_baseline(self, qapp) -> None:
        """다섯 상태와 후처리 카드에서 3줄 첫 항목의 시작 x가 모두 같아야 한다.

        3줄 첫 항목은 보이는 상태 원, 원이 없으면(대기) 상태 글이다(프로토와 같다, #550)
        대기 · 진행 · 일시정지 · 완료 · 실패 · 후처리 카드(900px) -> 첫 항목 왼쪽 x 여섯이 같다
        """
        xs = {}
        for state in MATRIX:
            widget = _make_widget(qapp, state, PROGRESS.get(state, 0))
            xs[state.name] = _left_x(widget, _row_head(widget))
        widget = _make_widget(qapp, DownloadState.RUNNING, 13, post_process=True)
        xs["POSTPROCESS"] = _left_x(widget, _row_head(widget))
        assert len(set(xs.values())) == 1, f"슬롯 시작 x가 상태마다 다르다: {xs}"


class TestActionColumn:
    """1행 우측 조작 — "지금 할 수 있는 것" 하나와 삭제. 호버 · 키보드 초점 때만 보인다(#550)."""

    @pytest.mark.parametrize("state", list(MATRIX))
    def test_exactly_the_matrix_action_is_visible(self, qapp, state) -> None:
        """호버한 카드는 상태마다 MATRIX의 조작 하나와 삭제만 보여야 한다.

        상태마다 카드 하나 → 호버 -> 보이는 버튼 == [MATRIX 조작, "deleteButton"], 조작 도형 == MATRIX 도형
        """
        button_name, icon, _, _ = MATRIX[state]
        widget = _make_widget(qapp, state, PROGRESS.get(state, 0))
        assert widget.visibleCardButtons() == [button_name, "deleteButton"]
        widget.setHovered(True)
        QApplication.processEvents()
        visible = [name for name in ACTION_BUTTONS if getattr(widget, name).isVisible()]
        assert visible == [button_name], (
            f"{state.name}: 보이는 조작 {visible} — 매트릭스는 {button_name}"
        )
        assert widget.deleteButton.isVisible(), f"{state.name}: 삭제는 늘 함께 보여야 한다"
        assert getattr(widget, button_name).iconName() == icon, (
            f"{state.name}: 조작 도형이 {getattr(widget, button_name).iconName()!r} — {icon!r}이어야 한다"
        )

    @pytest.mark.parametrize("state", list(MATRIX))
    def test_card_buttons_hide_without_hover_or_key_focus(self, qapp, state) -> None:
        """호버도 키보드 초점도 없는 카드는 카드 버튼이 하나도 보이지 않아야 한다 (#550).

        상태마다 카드 하나 → 초점을 빼고 호버 끔 -> 보이는 카드 버튼 없음, 놓인 버튼 목록은 그대로
        """
        widget = _make_widget(qapp, state, PROGRESS.get(state, 0))
        button_name = MATRIX[state][0]
        widget.clearFocus()
        widget.setHovered(False)
        QApplication.processEvents()
        assert not widget.hasKeyFocus() and not widget.isHovered(), "전제: 호버 · 초점이 없다"
        names = ACTION_BUTTONS + ("deleteButton",)
        assert not any(getattr(widget, name).isVisible() for name in names)
        assert widget.visibleCardButtons() == [button_name, "deleteButton"]

    def test_paused_offers_resume_not_pause_again(self, qapp) -> None:
        """멈춰 있는데 일시정지 버튼이 그대로면 이미 한 일을 또 권하는 것이다."""
        widget = _make_widget(qapp, DownloadState.PAUSED, 54)
        assert widget.pauseButton.iconName() == "resume"
        assert widget.pauseButton.toolTip() == "Resume"

    def test_running_offers_pause(self, qapp) -> None:
        widget = _make_widget(qapp, DownloadState.RUNNING, 42)
        assert widget.pauseButton.iconName() == "pause"
        assert widget.pauseButton.toolTip() == "Pause"

    def test_pause_and_resume_toggle_back_and_forth(self, qapp) -> None:
        """같은 버튼이 상태를 따라 도형·툴팁을 오간다 — 한쪽으로 굳으면 회귀다."""
        widget = _make_widget(qapp, DownloadState.RUNNING, 42)
        seen = []
        for state in (DownloadState.PAUSED, DownloadState.RUNNING, DownloadState.PAUSED):
            widget.item.downloadState = state
            widget.setData(widget.item, 0)
            seen.append(widget.pauseButton.iconName())
        assert seen == ["resume", "pause", "resume"], seen

    def test_delete_highlights_with_the_failure_colour_on_hover(self, qapp) -> None:
        """삭제는 평소 muted, 호버에서만 실패색 — 항상 빨간 ✕는 위계 역전(#244)."""
        widget = _make_widget(qapp, DownloadState.WAITING)
        assert widget.deleteButton.hoverToken() == "stateFailed"
        assert widget.deleteButton.colorToken() == "textMuted"
        for name in ACTION_BUTTONS:
            assert getattr(widget, name).hoverToken() == "text"


class TestProgressBarColumn:
    """진행바 — 진행분이 있을 때(진행·일시정지)만, 일시정지는 muted."""

    @pytest.mark.parametrize("state", list(MATRIX))
    def test_bar_visibility_and_colour_state_follow_the_matrix(self, qapp, state) -> None:
        _, _, bar_visible, bar_state = MATRIX[state]
        widget = _make_widget(qapp, state, PROGRESS.get(state, 0))
        assert widget.progressBar.isVisible() == bar_visible, (
            f"{state.name}: 진행바 표시 {widget.progressBar.isVisible()} — 매트릭스는 {bar_visible}"
        )
        assert widget.progressBar.property("state") == bar_state
        assert widget.statusLabel.property("state") == bar_state

    def test_paused_bar_keeps_the_progress_value(self, qapp) -> None:
        widget = _make_widget(qapp, DownloadState.PAUSED, 54)
        assert widget.progressBar.value() == 5400

    def test_paused_colour_is_distinct_from_running_and_waiting(self) -> None:
        """muted는 "다른 색"이어야 정보다 — 진행 파랑·대기 회색 둘 다와 달라야 한다."""
        for table in (theme.DARK, theme.LIGHT):
            assert table["statePaused"] not in (table["stateRunning"], table["stateWaiting"])


class TestRightColumnResolutionAndSize:
    """3행 우측 — 확정 해상도를 파일 크기 옆에 붙인다("1080p · 595.34 MB").
    한 자리라 행이 늘지 않는다. 대기에도 해상도 · 크기 · 길이가 붙는다(#550 — 해상도 버튼이 없다).
    받는 중 · 일시정지의 인코딩 완료 VOD는 "받은 크기 / 전체 크기"다 (#309 — v2.9.6의
    표기를 되살렸다. 폭이 모자라면 받은 크기만 남는다 — tests/app/gui/test_card_size_text.py)."""

    @pytest.mark.parametrize("state", (DownloadState.RUNNING, DownloadState.PAUSED))
    def test_running_and_paused_show_resolution_with_total_size(self, qapp, state) -> None:
        """받는 중 · 일시정지 카드가 3줄이 안 접히는 폭이면 정보 글이 해상도 · 받은 크기 / 전체 크기여야 한다.

        1080 · 받은 624,000,000바이트 · 전체 595.34 MB, 폭 = 3줄 독립 합산 + 3줄 밖 폭(#555)
        -> "1080p · 595.09 MB / 595.34 MB"
        """
        widget = _make_widget(qapp, state, PROGRESS[state])
        unfold(widget)
        assert widget.fileSizeLabel.isVisible()
        assert widget.fileSizeLabel.text() == "1080p · 595.09 MB / 595.34 MB", (
            widget.fileSizeLabel.text()
        )

    def test_finished_shows_resolution_with_downloaded_size(self, qapp) -> None:
        widget = _make_widget(qapp, DownloadState.FINISHED, 100)
        assert widget.fileSizeLabel.text().startswith("1080p · ")
        assert "MB" in widget.fileSizeLabel.text()

    def test_waiting_shows_resolution_size_and_length(self, qapp) -> None:
        """대기 카드의 정보 글은 해상도 · 크기 · 길이여야 한다 (#550).

        1080 · 595.34 MB · 3600초 대기 카드 -> "1080p · 595.34 MB · 1:00:00"
        """
        widget = _make_widget(qapp, DownloadState.WAITING)
        assert widget.fileSizeLabel.text() == "1080p · 595.34 MB · 1:00:00"

    def test_failed_shows_resolution_and_length(self, qapp) -> None:
        """실패 카드의 정보 글은 해상도 · 크기 · 길이여야 한다 (#554).

        1080 · 595.34 MB · 3600초 실패 카드 -> "1080p · 595.34 MB · 1:00:00"
        """
        widget = _make_widget(qapp, DownloadState.FAILED)
        assert widget.fileSizeLabel.isVisible()
        assert widget.fileSizeLabel.text() == "1080p · 595.34 MB · 1:00:00"


class TestHeightIsInvariantAcrossFiveStates:
    """다섯 상태 전부에서 카드 높이가 같다 — 목록이 들썩이면 안 된다."""

    def test_same_height_in_all_five_states(self, qapp) -> None:
        heights = {}
        for state in MATRIX:
            widget = _make_widget(qapp, state, PROGRESS.get(state, 0))
            heights[state.name] = widget.height()
        assert len(set(heights.values())) == 1, f"상태별 카드 높이가 다르다: {heights}"

    def test_same_height_when_one_card_walks_through_all_five_states(self, qapp) -> None:
        widget = _make_widget(qapp, DownloadState.WAITING)
        heights = {}
        for state in MATRIX:
            widget.item.downloadState = state
            widget.item.download_progress = PROGRESS.get(state, 0)
            widget.setData(widget.item, 0)
            widget.resize(900, widget.sizeHint().height())
            QApplication.processEvents()
            heights[state.name] = widget.height()
        assert len(set(heights.values())) == 1, (
            f"한 카드가 상태를 옮겨갈 때 높이가 변한다: {heights}"
        )


class TestDrawnIconsAreSharedAndFontFree:
    """조작 아이콘은 폰트 글리프가 아니라 그린 도형이고, 아이콘당 한 번만
    그려 공유한다 — 카드마다 페인트 객체가 붙으면 O(1) 삽입이 무너진다."""

    def test_every_icon_paints_something_without_a_font(self, qapp) -> None:
        for name in icons.ICON_NAMES:
            image = icons.action_pixmap(
                name, theme.DARK["text"], theme.METRICS["actionGlyph"]
            ).toImage()
            painted = sum(
                1
                for x in range(image.width())
                for y in range(image.height())
                if image.pixelColor(x, y).alpha() > 0
            )
            assert painted > 0, f"{name} 도형이 비어 있다"

    @pytest.mark.parametrize("scheme", ["DARK", "LIGHT"])
    def test_change_location_icon_is_drawn_apart_from_open_folder(self, qapp, scheme) -> None:
        """저장 위치 변경 아이콘은 폴더 열기 아이콘과 다른 그림이어야 하고, 폴더 몸통은 함께 가져야 한다.

        같은 색 · 16px로 그린 "folder"와 "folder_edit"
        -> 불투명도가 다른 픽셀이 있다(연필 둘레를 파냈다)
        -> 폴더 몸통의 왼쪽 가운데(4, 8)는 둘 다 꽉 칠해져 있다(가장자리가 아니다)
        """
        color = getattr(theme, scheme)["text"]
        folder = icons.action_pixmap("folder", color, 16).toImage()
        change = icons.action_pixmap("folder_edit", color, 16).toImage()

        differing = sum(
            1
            for x in range(16)
            for y in range(16)
            if folder.pixelColor(x, y).alpha() != change.pixelColor(x, y).alpha()
        )
        assert differing > 0
        assert folder.pixelColor(4, 8).alpha() == 255 and change.pixelColor(4, 8).alpha() == 255

    def test_path_mark_and_open_folder_button_use_different_folder_icons(
        self, qapp, monkeypatch
    ) -> None:
        """3줄 저장 위치 표시와 완료 카드의 폴더 열기 버튼은 다른 아이콘을 써야 한다 (#550).

        기본 위치 "C:/other", 카드 저장 위치 "C:/dl"
        -> 대기 카드의 보이는 pathIcon 아이콘 == "folder_outline"
        -> 호버한 완료 카드의 보이는 openDirectoryButton 아이콘 == "folder"
        """
        monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/other")
        waiting = _make_widget(qapp, DownloadState.WAITING)
        finished = _make_widget(qapp, DownloadState.FINISHED)
        finished.setHovered(True)
        QApplication.processEvents()

        assert waiting.pathIcon.isVisible()
        assert finished.openDirectoryButton.isVisible()
        assert waiting.pathIcon.iconName() == "folder_outline"
        assert finished.openDirectoryButton.iconName() == "folder"

    def test_icon_pixmaps_are_shared_across_cards(self, qapp) -> None:
        before = icons.cache_size()
        widgets = [_make_widget(qapp, DownloadState.FAILED) for _ in range(5)]
        for widget in widgets:
            widget.retryButton.grab()  # paintEvent를 실제로 태운다
        after = icons.cache_size()
        # 카드 5장이 그려도 캐시는 (retry·delete) × 색 조합만큼만 는다 — 카드 수와 무관
        assert after - before <= 4, f"카드마다 픽스맵을 새로 만들고 있다: +{after - before}"
        first = icons.action_pixmap(
            "retry", theme.DARK["textMuted"], theme.METRICS["actionGlyph"], 1.0
        )
        assert first is icons.action_pixmap(
            "retry", theme.DARK["textMuted"], theme.METRICS["actionGlyph"], 1.0
        )

    def test_buttons_carry_no_text_glyph(self, qapp) -> None:
        widget = _make_widget(qapp, DownloadState.RUNNING, 42)
        for name in ACTION_BUTTONS + ("deleteButton",):
            assert getattr(widget, name).text() == "", f"{name}이 아직 문자 글리프를 쓴다"

    def test_unknown_icon_name_is_rejected(self, qapp) -> None:
        with pytest.raises(ValueError):
            icons.action_pixmap("emoji", theme.DARK["text"], 10)
