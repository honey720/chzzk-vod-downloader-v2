"""`ElidingLabel` 단위 테스트 (PR #229 후속 — 가로 스크롤 버그 수정).

좁아지면 `QLabel`이 내용 폭을 요구해 카드가 뷰포트를 밀던 문제(가로 스크롤)의
수정. 라벨이 실제로 좁아질 수 있는지(사이즈 정책)·좁을 때 말줄임(...)으로
잘리는지·전체 값이 툴팁으로 남는지·`text()`가 원본 그대로를 돌려주는지를
고정한다.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSizePolicy

from content.eliding_label import ElidingLabel


class TestSizePolicy:
    def test_horizontal_policy_is_ignored_so_layouts_can_shrink_it(self, qapp):
        label = ElidingLabel()
        assert label.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored


class TestTextContract:
    def test_text_returns_full_value_not_elided_display(self, qapp):
        label = ElidingLabel()
        label.resize(20, 20)  # 원문이 절대 안 들어갈 만큼 좁게
        full = "This is a very long value that will not fit in 20 pixels"
        label.setText(full)
        assert label.text() == full

    def test_tooltip_always_shows_full_value(self, qapp):
        label = ElidingLabel()
        label.resize(20, 20)
        full = "C:/Users/LeeDH/Desktop/AVeryLongPathThatWillDefinitelyBeElided/file.mp4"
        label.setText(full)
        assert label.toolTip() == full


class TestElision:
    def test_narrow_width_elides_the_rendered_text(self, qapp):
        label = ElidingLabel()
        label.resize(30, 20)
        full = "This is a very long value that will not fit in 30 pixels at all"
        label.setText(full)
        rendered = ElidingLabel.__mro__[1].text(label)  # QLabel.text() — 실제 표시 문자열
        assert rendered != full
        assert "…" in rendered

    def test_wide_enough_width_shows_full_text_unelided(self, qapp):
        label = ElidingLabel()
        label.resize(2000, 20)
        full = "short"
        label.setText(full)
        rendered = ElidingLabel.__mro__[1].text(label)
        assert rendered == full

    def test_resize_recomputes_elision(self, qapp):
        """폭이 바뀌면 다시 잘려야 한다 — 창 크기 변경 시나리오.

        show() 없이 top-level 위젯에 resize()만 연달아 호출하면 Qt가
        resizeEvent를 지연시켜(아직 생성되지 않은 네이티브 윈도우) 두 번째
        resize가 반영 안 될 수 있다 — 실제 카드처럼 레이아웃 안에 놓고
        보여준 상태에서 검증한다(실제 앱에서 이 경로로 동작함을 스크린샷으로
        이미 확인함).
        """
        label = ElidingLabel()
        label.show()
        full = "This is a moderately long piece of text for resize testing"
        label.setText(full)

        label.resize(2000, 20)
        qapp.processEvents()
        wide_rendered = ElidingLabel.__mro__[1].text(label)

        label.resize(40, 20)
        qapp.processEvents()
        narrow_rendered = ElidingLabel.__mro__[1].text(label)

        assert wide_rendered == full
        assert narrow_rendered != full
        assert len(narrow_rendered) < len(wide_rendered)
        label.deleteLater()

    def test_elide_right_keeps_the_beginning(self, qapp):
        label = ElidingLabel(elide_mode=Qt.TextElideMode.ElideRight)
        label.resize(60, 20)
        label.setText("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAZZZZZZZZ")
        rendered = ElidingLabel.__mro__[1].text(label)
        assert rendered.startswith("A")
        assert not rendered.endswith("Z")

    def test_elide_middle_keeps_both_ends(self, qapp):
        """경로는 드라이브(앞)·파일명(뒤)이 둘 다 정보라 가운데를 잘라야 한다.

        폭은 넉넉히 둔다 — 너무 좁으면 정확히 어디서 잘리는지가 플랫폼별
        폰트 메트릭에 좌우된다(3-OS CI에서 실제로 겪음, 다른 테스트 참고).
        """
        label = ElidingLabel(elide_mode=Qt.TextElideMode.ElideMiddle)
        label.resize(150, 20)
        label.setText("C:/StartOfPathThatMatters/Middle/Junk/Here/EndFileName.mp4")
        rendered = ElidingLabel.__mro__[1].text(label)
        assert rendered.startswith("C:")
        assert rendered.endswith(".mp4")

    def test_set_elide_mode_reapplies_immediately(self, qapp):
        # 폭을 넉넉히 둔다 — 너무 좁으면 정확히 몇 글자가 남는지가 플랫폼별
        # 폰트 메트릭에 좌우돼("mp4"는 남고 그 앞의 "."만 잘리는 등) CI
        # 3-OS에서 결과가 갈릴 수 있다(실제로 Windows 로컬에선 통과, CI
        # 3-OS에서 전부 이 이유로 실패해 이번에 폭을 넓혀 고쳤다).
        label = ElidingLabel(elide_mode=Qt.TextElideMode.ElideRight)
        label.resize(150, 20)
        label.setText("StartMiddlePartThatIsLong-EndFileName.mp4")
        right = ElidingLabel.__mro__[1].text(label)

        label.setElideMode(Qt.TextElideMode.ElideMiddle)
        middle = ElidingLabel.__mro__[1].text(label)

        assert right != middle
        assert middle.endswith(".mp4")
