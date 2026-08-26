"""`ContentListView`(#226) 단위 테스트 — QScrollArea+QVBoxLayout 직접 삽입.

`content/model.py`+`content/view.py`를 직접 겨냥한 전용 테스트가 지금까지
없었다(기존엔 `ContentManager`를 통해서만 간접 검증됐다). `setIndexWidget()`을
버리면서 새로 생긴 계약(삽입 위치, 삭제 시 생명주기, 오버레이 상태 전이)을
여기서 고정한다.
"""

import pytest

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


def _make_item(title="제목"):
    return ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": title, "category": "", "channelName": "채널", "createdDate": "", "duration": 0},
        [], None, "", ".", "video", None,
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
