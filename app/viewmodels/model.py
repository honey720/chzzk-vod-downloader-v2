"""카드 목록 데이터 저장소 — Qt 모델 아님 (#226).

`QAbstractListModel`을 상속하던 예전 버전은 `beginInsertRows`/`dataChanged`
등 Qt 아이템뷰 시그널을 통째로 물려받았는데, `content/view.py`가 그중
`rowsInserted`/`dataChanged`/`layoutChanged`를 전부 전체 재스캔 한 메서드에
연결해뒀었다 — 삽입·진행률 갱신마다 기존 카드 전체를 다시 훑는 원인이었다
(#208 실측: 카드 1000개 삽입 34.5초, `updateWidgets()`를 신규 행만 갱신하도록
패치해도 15%만 개선 — 진짜 원인은 `QListView`+`setIndexWidget()` 자체였다).

클래스 이름은 유지한다 — `QListView`를 더 상속하지 않지만, `content/view.py`가
여전히 카드의 순서 있는 컬렉션+변경 통지라는 같은 역할로 이 클래스를 쓰고,
이름을 바꾸면 번역 컨텍스트(`tr()`)·`project.json` lupdate 스캔 대상까지
갈아엎어야 한다(이 프로젝트에 기록된 함정) — 이번 변경 범위 밖이다.
"""

from PySide6.QtCore import QObject, Signal

from content.data import ContentItem


class ContentListModel(QObject):
    # 삽입/삭제/변경마다 해당 아이템 하나만 실어 보낸다 — 구독자(view)가
    # 자기 딕셔너리로 O(1) 조회해 그 위젯만 만지도록 강제하는 설계다.
    # 리스트 전체를 다시 훑을 근거(row/index)를 아예 안 준다.
    itemInserted = Signal(object)
    itemRemoved = Signal(object)
    itemChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[ContentItem] = []

    def rowCount(self) -> int:
        return len(self.items)

    def itemAt(self, row: int) -> ContentItem | None:
        if 0 <= row < len(self.items):
            return self.items[row]
        return None

    def addItem(self, item: ContentItem, row: int | None = None) -> None:
        """실제 ContentItem을 바로 모델에 삽입"""
        if row is None:
            row = self.rowCount()  # 항상 맨 끝에 삽입하거나, 필요한 위치를 row로 지정
        self.items.insert(row, item)
        self.itemInserted.emit(item)

    def removeRows(self, row: int, count: int = 1) -> None:
        """아이템 삭제 — 제거된 아이템별로 itemRemoved를 낸다."""
        removed = self.items[row: row + count]
        del self.items[row: row + count]
        for item in removed:
            self.itemRemoved.emit(item)

    def getRow(self, item: ContentItem) -> int | None:
        """ContentItem 객체의 row 찾기"""
        try:
            return self.items.index(item)
        except ValueError:
            return None

    def isEmpty(self) -> bool:
        return len(self.items) == 0

    def notifyChanged(self, item: ContentItem) -> None:
        """아이템 필드가 바뀌었으니 그 위젯만 갱신하라고 알린다(진행률 등)."""
        self.itemChanged.emit(item)
