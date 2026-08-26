"""`ContentListView`(#226) 단위 테스트 — QScrollArea+QVBoxLayout 직접 삽입.

`content/model.py`+`content/view.py`를 직접 겨냥한 전용 테스트가 지금까지
없었다(기존엔 `ContentManager`를 통해서만 간접 검증됐다). `setIndexWidget()`을
버리면서 새로 생긴 계약(삽입 위치, 삭제 시 생명주기, 오버레이 상태 전이)을
여기서 고정한다.
"""

import pytest
from PySide6.QtCore import Qt

from content.data import ContentItem
from content.model import ContentListModel
from content.view import ContentListView


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("content.widget.get_thread_session", lambda: _FailingSession())


def _make_item(title="제목", download_path="."):
    return ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": title, "category": "", "channelName": "채널", "createdDate": "", "duration": 0},
        [], None, "", download_path, "video", None,
    )


@pytest.fixture
def view(qapp):
    v = ContentListView()
    model = ContentListModel()
    v.setModel(model)
    yield v, model
    v.deleteLater()


class TestInsertion:
    def test_inserted_item_gets_a_widget(self, view, qapp):
        v, model = view
        item = _make_item()
        model.addItem(item)
        qapp.processEvents()

        widget = v.widgetFor(item)
        assert widget is not None
        assert widget.titleLabel.text() == "제목"

    def test_layout_position_matches_row(self, view, qapp):
        """카드는 항상 스트레치보다 앞, 삽입 순서대로 레이아웃에 놓인다."""
        v, model = view
        first, second = _make_item("첫번째"), _make_item("두번째")
        model.addItem(first)
        model.addItem(second)
        qapp.processEvents()

        w_first = v.widgetFor(first)
        w_second = v.widgetFor(second)
        assert v._layout.indexOf(w_first) == 0
        assert v._layout.indexOf(w_second) == 1
        # 스트레치가 항상 카드들 뒤에 있어야 새 카드가 끝에 쌓인다
        assert v._layout.indexOf(w_second) < v._layout.count() - 1

    def test_replace_at_same_row_keeps_position(self, view, qapp):
        """조회 완료로 자리표시가 완성 카드로 바뀔 때(같은 자리) 순서가 안 흐트러진다."""
        v, model = view
        before, placeholder, after = _make_item("앞"), _make_item("자리표시"), _make_item("뒤")
        model.addItem(before)
        model.addItem(placeholder)
        model.addItem(after)
        qapp.processEvents()

        row = model.getRow(placeholder)
        model.removeRows(row, 1)
        replacement = _make_item("완성됨")
        model.addItem(replacement, row)
        qapp.processEvents()

        assert v._layout.indexOf(v.widgetFor(before)) == 0
        assert v._layout.indexOf(v.widgetFor(replacement)) == 1
        assert v._layout.indexOf(v.widgetFor(after)) == 2


class TestDeletionLifecycle:
    def test_removed_widget_leaves_the_dict(self, view, qapp):
        v, model = view
        item = _make_item()
        model.addItem(item)
        qapp.processEvents()
        assert v.widgetFor(item) is not None

        model.removeRows(0, 1)
        qapp.processEvents()

        assert v.widgetFor(item) is None

    def test_removed_widget_is_taken_out_of_the_layout(self, view, qapp):
        """레이아웃에 남겨두면 다음 카드 위치가 밀리는 회귀가 재발한다."""
        v, model = view
        item = _make_item()
        model.addItem(item)
        qapp.processEvents()
        widget = v.widgetFor(item)

        model.removeRows(0, 1)
        qapp.processEvents()

        assert v._layout.indexOf(widget) == -1

    def test_removed_widget_is_scheduled_for_deletion(self, view, qapp, monkeypatch):
        """생명주기를 빠뜨리면 누수 — deleteLater가 실제로 호출되는지 스파이로 고정한다."""
        v, model = view
        item = _make_item()
        model.addItem(item)
        qapp.processEvents()
        widget = v.widgetFor(item)

        calls = []
        monkeypatch.setattr(widget, "deleteLater", lambda: calls.append(True))

        model.removeRows(0, 1)
        qapp.processEvents()

        assert calls == [True]

    def test_removing_last_item_does_not_crash_and_dict_is_empty(self, view, qapp):
        v, model = view
        item = _make_item()
        model.addItem(item)
        qapp.processEvents()

        model.removeRows(0, 1)
        qapp.processEvents()

        assert model.isEmpty()
        assert v._widgets == {}


class TestEmptyOverlayState:
    def test_paints_empty_message_when_no_items_and_not_dragging(self, view):
        v, model = view
        assert model.isEmpty()
        assert v._dragActive is False
        # _paintOverlay는 위 두 조건에서 빈 리스트 안내 분기를 타야 한다 —
        # 실제 렌더링 대신 분기 조건 자체(모델 소스)를 고정한다
        assert model.isEmpty() and not v._dragActive

    def test_no_empty_message_once_an_item_exists(self, view, qapp):
        v, model = view
        model.addItem(_make_item())
        qapp.processEvents()
        assert not model.isEmpty()


class TestProgressUpdate:
    def test_notify_changed_refreshes_only_that_widget(self, view, qapp):
        v, model = view
        a, b = _make_item("A"), _make_item("B")
        model.addItem(a)
        model.addItem(b)
        qapp.processEvents()

        a.title = "A(갱신됨)"
        model.notifyChanged(a)
        qapp.processEvents()

        assert v.widgetFor(a).titleLabel.text() == "A(갱신됨)"
        assert v.widgetFor(b).titleLabel.text() == "B"


class TestNoHorizontalOverflow:
    """가로 스크롤 버그 회귀 테스트 (PR #229 후속) — 카드는 창 폭에 맞아야 한다.

    `QLabel`이 내용 폭만큼 넓어지려 해 긴 제목·경로·상태 문구가 카드를
    밀고 카드가 뷰포트를 밀던 문제. `setWidgetResizable(True)`만으로는
    부족했다 — 실측 결과 컨테이너가 실제로 수렴하기까지 이벤트 루프를
    여러 번 돌아야 했다(한 번만 돌리면 아직 완전히 반영되지 않은 중간
    상태를 보게 된다).
    """

    def test_horizontal_scrollbar_is_always_off(self, view):
        v, _ = view
        assert v.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    def test_long_title_and_path_do_not_widen_the_container_past_the_viewport(self, view, qapp):
        v, model = view
        v.resize(500, 400)
        v.show()
        qapp.processEvents()

        long_item = _make_item(
            title="VeryLongTitleWithNoSpacesAtAllThatCannotWrapOrBreakAnywhereSoItForcesWidth1234567890",
            download_path="C:/Users/LeeDH/Desktop/VeryLongFolderNameNoSpaces/AnotherLongSubfolder/FileNameHere.mp4",
        )
        model.addItem(long_item)
        for _ in range(8):  # 실측으로 확인한 수렴에 필요한 최소 횟수보다 넉넉히
            qapp.processEvents()

        # 인덱스·채널 아이콘 같은 고정폭 요소까지 0으로 만들 순 없어 약간의 여유를 둔다
        assert v._container.width() <= v.width() + 10, (
            f"container({v._container.width()})가 viewport({v.width()})보다 훨씬 넓다 "
            "— 가로 스크롤 버그 회귀"
        )

    def test_title_is_elided_right_and_path_is_elided_middle(self, view, qapp):
        v, model = view
        v.resize(300, 400)
        v.show()
        qapp.processEvents()

        item = _make_item(
            title="StartOfTitleThatMattersMostAndShouldStayVisibleAAAAAAAAAAAAAAAAAAAAAAAAA",
            download_path="C:/StartOfPathThatMatters/junk/junk/junk/junk/EndFileNameThatMatters.mp4",
        )
        model.addItem(item)
        for _ in range(8):
            qapp.processEvents()

        widget = v.widgetFor(item)
        rendered_title = type(widget.titleLabel).__mro__[1].text(widget.titleLabel)
        rendered_path = type(widget.directoryLabel).__mro__[1].text(widget.directoryLabel)

        assert rendered_title.startswith("StartOfTitle")
        assert rendered_path.startswith("C:")
        assert rendered_path.endswith(".mp4")
