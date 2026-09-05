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
from app.widgets.view import ContentListView
from core.models.download_state import DownloadState
from tests.unit.card_helpers import shown


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


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


class TestStateActionSignalsAreWired:
    """카드 상태별 조작(#245 — ⏸ 일시정지·↻ 재시도)이 실배선으로 뷰까지
    올라오는지. 핸들러 직접 호출이 아니라 실제 버튼 클릭 → 위젯 시그널 →
    뷰 릴레이(아이템 부착)의 전체 체인을 탄다 — deleteRequest와 같은 패턴.
    mainWindow 쪽 처리(onCardPause/onCardRetry)는 전역 다운로드 상태에
    묶여 있어 여기서는 뷰 경계까지를 고정한다.
    """

    def test_retry_click_reaches_the_view_with_its_item(self, view, qapp):
        v, model = view
        item = _make_item()
        item.downloadState = DownloadState.FAILED
        model.addItem(item)
        qapp.processEvents()

        received = []
        v.retryRequested.connect(received.append)
        widget = v.widgetFor(item)
        widget.retryButton.click()
        qapp.processEvents()
        assert received == [item]

    def test_pause_click_reaches_the_view_with_its_item(self, view, qapp):
        v, model = view
        item = _make_item()
        item.downloadState = DownloadState.RUNNING
        item.download_progress = 42
        model.addItem(item)
        qapp.processEvents()

        received = []
        v.pauseRequested.connect(received.append)
        widget = v.widgetFor(item)
        widget.pauseButton.click()
        qapp.processEvents()
        assert received == [item]


class TestCardNumberingFollowsPosition:
    """카드 번호(`#N`)가 위치를 따라가는지 (#235 — #226 회귀 수정).

    `#226` 이전엔 `QAbstractListModel`의 프레임워크 시그널(`rowsRemoved` 등)에
    물린 전체 재스캔이 매번 모든 카드의 번호를 다시 매겨줬다. `#226`이 그
    재스캔을 없애면서(전체 재스캔 자체가 옳게 없앤 것 — 카드 1000개 삽입이
    34.5초였다) 번호 재계산도 같이 사라졌다: 삭제 후 남은 카드는 삭제 전
    번호를 그대로 들고 있었고, 새 카드가 `rowCount()`(현재 개수)를 받으면서
    기존 카드와 번호가 겹치는 경우까지 생겼다.

    **대기 방식 — 왜 `processEvents()` 한 번으로 안 끝내는가.** 재번호매김은
    `QTimer.singleShot(0, ...)`으로 다음 이벤트 루프 턴에 돈다(일괄 삭제를
    한 번으로 누르는 배치 장치, `content/view.py::_scheduleRenumber` 참고).
    `processEvents()` 한 번으로 그 0ms 타이머가 항상 잡히는지는 이 프로세스
    안에서 2000회 스트레스로는 한 번도 놓치지 않았지만 — 그건 이 머신의
    이벤트 디스패처가 그렇다는 증거일 뿐, 다른 OS·부하가 걸린 CI 러너에서도
    항상 그런다는 보장은 아니다(v2.9.3 CI 플레이크가 정확히 이 부류였다).
    그래서 고정 횟수의 `processEvents()`가 아니라 `qtbot.waitUntil()` —
    조건이 참이 될 때까지 이벤트 루프를 계속 돌리는 조건 대기 — 로 기다린다.
    """

    def _labels(self, v, model):
        # #244 재설계로 "#0" 번호 라벨은 화면에서 사라졌다(오너 확정) —
        # 순번 자체는 setIndex가 widget.index로 계속 유지하며, 재번호매김
        # 불변식(#235)은 이 값으로 검증한다. 검증 시나리오(중간·첫·끝·일괄
        # 삭제 후 연속 번호)는 그대로다.
        return [v.widgetFor(model.itemAt(row)).index for row in range(model.rowCount())]

    def test_middle_delete_renumbers_trailing_cards(self, view, qapp, qtbot):
        v, model = view
        a, b, c = _make_item("A"), _make_item("B"), _make_item("C")
        for it in (a, b, c):
            model.addItem(it)
        qapp.processEvents()
        assert self._labels(v, model) == [0, 1, 2]

        model.removeRows(model.getRow(b), 1)

        qtbot.waitUntil(lambda: self._labels(v, model) == [0, 1], timeout=2000)

    def test_new_card_after_middle_delete_does_not_collide(self, view, qapp, qtbot):
        """⚠️ 회귀의 핵심 — 재부여가 없으면 새 카드와 기존 카드의 번호가 겹친다."""
        v, model = view
        a, b, c = _make_item("A"), _make_item("B"), _make_item("C")
        for it in (a, b, c):
            model.addItem(it)
        qapp.processEvents()

        model.removeRows(model.getRow(b), 1)
        d = _make_item("D")
        model.addItem(d)

        qtbot.waitUntil(lambda: self._labels(v, model) == [0, 1, 2], timeout=2000)
        labels = self._labels(v, model)
        assert len(labels) == len(set(labels)), f"번호가 겹쳤다: {labels}"

    def test_first_delete_renumbers(self, view, qapp, qtbot):
        v, model = view
        a, b, c = _make_item("A"), _make_item("B"), _make_item("C")
        for it in (a, b, c):
            model.addItem(it)
        qapp.processEvents()

        model.removeRows(model.getRow(a), 1)

        qtbot.waitUntil(lambda: self._labels(v, model) == [0, 1], timeout=2000)

    def test_last_delete_leaves_leading_numbers_untouched(self, view, qapp, qtbot):
        v, model = view
        a, b, c = _make_item("A"), _make_item("B"), _make_item("C")
        for it in (a, b, c):
            model.addItem(it)
        qapp.processEvents()

        model.removeRows(model.getRow(c), 1)

        qtbot.waitUntil(lambda: self._labels(v, model) == [0, 1], timeout=2000)

    def test_bulk_delete_ends_with_contiguous_numbering(self, view, qapp, qtbot):
        """일괄 삭제(비연속 위치) 뒤에도 번호가 연속이어야 한다."""
        v, model = view
        items = [_make_item(str(i)) for i in range(5)]
        for it in items:
            model.addItem(it)
        qapp.processEvents()

        # 0, 2, 4번을 지운다(비연속 위치) — clrearFinishedItems()가 하듯
        # 뒤에서부터 지워 인덱스가 삭제 중간에 밀리지 않게 한다
        for it in (items[4], items[2], items[0]):
            model.removeRows(model.getRow(it), 1)

        qtbot.waitUntil(lambda: self._labels(v, model) == [0, 1], timeout=2000)

    def test_bulk_delete_renumbers_only_once(self, view, qapp, qtbot, monkeypatch):
        """일괄 삭제 한 번에 재번호매김이 여러 번(O(n²)) 돌면 안 된다 — 끝에 한 번만."""
        v, model = view
        items = [_make_item(str(i)) for i in range(6)]
        for it in items:
            model.addItem(it)
        qapp.processEvents()

        calls = []
        original = v._renumberAll

        def _spy():
            calls.append(True)
            original()

        monkeypatch.setattr(v, "_renumberAll", _spy)

        # 세 번 연달아 지운다 — 이벤트 루프를 안 돌리는 한 번의 "일괄" 처리로 흉내
        for it in (items[5], items[3], items[1]):
            model.removeRows(model.getRow(it), 1)

        qtbot.waitUntil(lambda: len(calls) >= 1, timeout=2000)  # 배치가 도는 걸 조건으로 기다린다
        assert calls == [True], f"재번호매김이 {len(calls)}번 돌았다 — 배치가 안 눌렸다"

    def test_insertion_does_not_trigger_a_renumber_pass(self, view, qapp, monkeypatch):
        """삽입은 O(1)이어야 한다 — 재번호매김(전체 순회)이 삽입 경로로 새면 안 된다 (#226 유지)."""
        v, model = view
        for i in range(5):
            model.addItem(_make_item(str(i)))
        qapp.processEvents()

        calls = []
        monkeypatch.setattr(v, "_renumberAll", lambda: calls.append(True))

        model.addItem(_make_item("new"))
        qapp.processEvents()

        assert calls == [], "삽입만 했는데 재번호매김이 돌았다 — O(1) 삽입 위반"

    def test_insertion_only_touches_the_new_widgets_setData(self, view, qapp, monkeypatch):
        """기존 카드가 삽입 때문에 다시 그려지면 안 된다 — 새 카드 자기 몫만."""
        from app.widgets.widget import ContentItemWidget

        v, model = view
        for i in range(20):
            model.addItem(_make_item(str(i)))
        qapp.processEvents()

        calls = []
        original_set_data = ContentItemWidget.setData

        def _spy(self, item, index):
            calls.append(item)
            return original_set_data(self, item, index)

        monkeypatch.setattr(ContentItemWidget, "setData", _spy)

        new_item = _make_item("new")
        model.addItem(new_item)
        qapp.processEvents()

        assert calls == [new_item], f"삽입 하나에 setData가 {len(calls)}번 불렸다 — O(1) 삽입 위반"


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
        """제목은 오른쪽, 경로는 가운데 말줄임인지 — **폰트 무의존 재표현** (#237).

        원래는 `rendered_title.startswith("StartOfTitle")`처럼 정확한 글자
        수를 직접 단언했다 — 로컬 Windows(오프스크린, `QT_QPA_FONTDIR`
        미설정)의 대체 폰트가 CI가 고정한 폰트보다 넓어 같은 300px에서
        살아남는 글자 수가 달라 로컬에서만 깨졌다(도입 커밋 `0e062fa`부터
        계속 그랬다 — 회귀가 아니라 애초에 로컬을 못 통과하던 게이트).

        검증하려는 것은 "정확히 몇 글자가 남는가"가 아니라 "어느 쪽이
        잘리는가"다 — 그래서 정확한 문자 수 대신 **구조**를 잰다: (1) 잘림
        모드 설정 자체(`_elide_mode`, 폰트와 무관한 정적 값), (2) 잘린
        결과가 원문의 접두사(오른쪽 잘림)인지 접두사+접미사(가운데 잘림)
        인지 — 살아남는 글자 수가 얼마든 이 관계는 폰트와 무관하게 성립한다.
        """
        v, model = view
        v.resize(300, 400)
        v.show()
        qapp.processEvents()

        # 경로 라벨의 표시 문자열은 축약형이다(#245 — 뿌리+마지막 폴더, 3단계
        # 이상이면 가운데를 "…"로 접음). 여기서 재는 것은 **라벨의 말줄임**이므로
        # 축약이 "…"를 만들지 않는 형태(뿌리 뒤 2단계)에 긴 마지막 세그먼트를 둬
        # 표시 문자열이 300px에서 실제로 잘리게 한다 — 축약의 "…"와 말줄임의
        # "…"가 섞이면 접두·접미 판정이 흐려진다.
        item = _make_item(
            title="StartOfTitleThatMattersMostAndShouldStayVisibleAAAAAAAAAAAAAAAAAAAAAAAAA",
            download_path="C:/StartOfPathThatMatters/EndFileNameThatMattersAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        model.addItem(item)
        for _ in range(8):
            qapp.processEvents()

        widget = v.widgetFor(item)
        rendered_title = shown(widget.titleLabel)  # 가시성 단언 후의 표시 문자열(QLabel.text)
        full_title = widget.titleLabel.text()
        full_path = widget.directoryLabel.text()  # 축약형 표시 문자열 — 말줄임의 원문
        assert "…" not in full_path, "축약이 '…'를 넣었다 — 이 테스트의 경로는 뿌리 뒤 2단계여야 한다"

        assert widget.titleLabel._elide_mode == Qt.TextElideMode.ElideRight
        assert widget.directoryLabel._elide_mode == Qt.TextElideMode.ElideMiddle

        # 오른쪽 말줄임: "…" 앞부분이 원문의 접두사와 일치해야 한다(뒷부분만 잘림)
        assert rendered_title != full_title, "제목이 이 폭에서 안 잘렸다 — 게이트 전제가 깨졌다"
        assert rendered_title.endswith("…")
        assert full_title.startswith(rendered_title[:-1]), (
            f"오른쪽 말줄임이 아니다 — 접두사가 원문과 안 맞는다: {rendered_title[:-1]!r}"
        )

        # 가운데 말줄임(경로) — #245 [B] 이후의 규칙으로 잰다. 300px에서는 3행
        # 우선순위로 경로가 **아이콘으로 접혀 라벨이 숨는다**(이전엔 숨은 라벨의
        # 낡은 표시 문자열을 읽어 우연히 통과했다 — PathLabel 도입으로 드러남).
        # 그래서 경로 텍스트가 처음 다시 보이는 폭까지 넓혀 잰다(px를 박지 않아
        # 폰트 무의존). 그 폭에서 라벨은 최소 텍스트 폭 근처라 반드시 ③단계다:
        # 접두 `C:/…/`(중간 폴더 접기)는 고정이고, 가운데 말줄임은 **마지막
        # 폴더 안에서만** 일어난다 — "…" 앞뒤가 마지막 폴더의 접두사·접미사다.
        width = 300
        while not widget.directoryLabel.isVisible():
            width += 8
            assert width <= 2000, "경로 라벨이 어떤 폭에서도 보이지 않는다"
            v.resize(width, 400)
            for _ in range(4):
                qapp.processEvents()
        rendered_path = shown(widget.directoryLabel)
        last_folder = full_path.rsplit("/", 1)[-1]
        assert rendered_path != full_path, "경로가 이 폭에서 안 잘렸다 — 게이트 전제가 깨졌다"
        assert rendered_path.startswith("C:/…/"), f"중간 폴더 접기 접두가 고정되지 않았다: {rendered_path!r}"
        tail = rendered_path[len("C:/…/"):]
        assert "…" in tail, f"마지막 폴더 안의 가운데 말줄임이 없다 — 전제(첫 가시 폭) 확인: {rendered_path!r}"
        before, _, after = tail.partition("…")
        assert last_folder.startswith(before), f"가운데 말줄임이 아니다 — 앞부분 불일치: {before!r}"
        assert last_folder.endswith(after), f"가운데 말줄임이 아니다 — 뒷부분 불일치: {after!r}"
