"""카드 편집 창 — 대기 카드의 파일명 · 해상도 · 저장 위치 · 구간을 한 창에서 고친다 (#545).

창은 고친 값을 들고만 있다가 저장 때 카드에 한 번에 쓴다. 취소는 아무것도 쓰지 않는다.

- 파일명 칸은 카드 제목(`item.title`)만 고친다 — 제목 그 자리 고치기와 같은 값이다. 받을 때 붙는
  꼬리(「 1080p.mp4」 · 구간 「 1080p_1.mp4」)는 칸 뒤에 흐린 글로 모양만 보인다. 빈 이름은
  처음 제목(`default_title`)으로 쓴다(#543)
- 해상도는 카드의 해상도 pill과 같은 목록이다. 크기는 카드가 조회한 값이고, 창이 열린 사이 도착한
  값은 `updateRepSize`로 고친다. 저장 때 해상도가 바뀌었고 구간이 있으면 구간을 새 해상도에 다시
  맞추게 한다(pill을 누른 것과 같다)
- 「구간 편집…」은 지금 구간 창을 연다. 그 창은 카드의 사본(초안)에 쓰고, 초안의 구간은 이 창을
  저장할 때 카드에 들어간다. 이 창을 취소하면 카드의 구간은 그대로다
- 저장 위치는 카드의 저장 위치 바꾸기와 같은 폴더 선택이다

창이 열린 사이 카드가 대기가 아니게 됐거나 목록에서 지워졌으면 저장은 쓰지 않고 닫는다.
"""

import copy
import logging
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import app.theme as theme
from app.section_input.basis import SECTION_CONTENT_TYPES
from app.viewmodels import card_text
from app.viewmodels.data import ContentItem
from app.viewmodels.section_edit import SectionEditViewModel, keep_section_head
from app.views.section_dialog import SectionEditDialog
from app.widgets.eliding_label import ElidingLabel, PathLabel
from app.widgets.tip_bubble import install_tips
from app.widgets.widget import path_display_parts
from core.media.representations import file_tag
from core.models.download_state import DownloadState

logger = logging.getLogger(__name__)

# 구간 창이 초안에 쓰는 구간 값 — 저장 때 카드로 옮긴다(SectionEditViewModel.commit이 쓰는 칸).
# 받아 둔 moov(section_head)는 주인이 하나라 keep_section_head로 따로 옮긴다
SECTION_FIELDS = (
    "selections",
    "section_frame_rate",
    "section_verified",
    "section_check",
    "section_refit_fps",
    "section_end_pulled",
    "section_end_extended",
    "section_unfit",
    "section_bytes",
)
DIALOG_WIDTH = 520  # 창 폭(px) — 프로토 카드 편집 창 값에서 시작(설계 §12.3 — 크기는 정하는 중)


class _WheelGuard(QObject):
    """초점이 없는 드롭다운이 휠로 값을 바꾸지 않게 휠을 막는다(설계 §4).

    그 드롭다운 하나에만 건다 — 앱 전체에 거는 필터가 아니다.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel and not watched.hasFocus():
            event.ignore()
            return True
        return False


class CardEditDialog(QDialog):
    """대기 카드 하나의 편집 창. `open()`으로 열고 저장 · 취소는 `finished`로 받는다."""

    def __init__(
        self,
        item: ContentItem,
        default_dir: str,
        refit: Callable[[ContentItem], None],
        notify: Callable[[ContentItem], None],
        is_present: Callable[[ContentItem], bool],
        parent: QWidget | None = None,
    ) -> None:
        """
        Args:
            default_dir: 「기본 위치」가 돌아가는 저장 위치(헤더 저장 위치 버튼의 기본 위치)
            refit: 해상도를 바꾼 구간 카드의 구간을 다시 맞추게 하는 함수(뷰모델의 ``refitSections``)
            notify: 카드에 쓴 뒤 부르는 함수(목록 모델의 ``notifyChanged``)
            is_present: 그 카드가 아직 목록에 있는지
        """
        super().__init__(parent)
        self._item = item
        self._defaultDir = default_dir
        self._refit = refit
        self._notify = notify
        self._isPresent = is_present
        # 구간 창이 쓰는 사본 — 저장 때까지 카드는 그대로다. 얕은 사본이라 해상도 목록 등은 함께 본다
        self._draft = copy.copy(item)
        self._draftSections = False  # 구간 창이 초안에 구간을 썼다
        # 열 때 카드가 들고 있던 moov — 구간 창 확인이 주인을 초안으로 옮기므로 취소 때 되돌린다
        self._heldHead = getattr(item, "section_head", None)
        self._path = item.download_path
        self.sectionDialog: SectionEditDialog | None = None  # 열린 구간 창(테스트가 본다)
        self.setObjectName("cardEditDialog")
        self.setModal(True)
        self._setupUi()
        self._retranslateUi()
        self._refreshTail()
        self._refreshSections()
        self._refreshPath()
        # 툴팁은 모두 말풍선(0ms)으로 뜬다 — 창의 위젯마다 필터를 건다(설계 §12.1 「툴팁」, #550)
        install_tips(self)

    # ---- 조립 ----

    def _setupUi(self) -> None:
        item = self._item
        root = QVBoxLayout(self)

        self.titleLabel = ElidingLabel(self)
        self.titleLabel.setObjectName("cardEditTitle")
        self.titleLabel.setText(item.title)
        root.addWidget(self.titleLabel)
        self.metaLabel = QLabel(self)
        self.metaLabel.setObjectName("cardEditMeta")
        root.addWidget(self.metaLabel)

        form = QFormLayout()
        root.addLayout(form)

        self.nameEdit = QLineEdit(item.title, self)
        self.nameEdit.setObjectName("cardEditName")
        self.nameEdit.setCursorPosition(0)  # 열 때 이름의 처음이 보이게
        self.tailLabel = QLabel(self)
        self.tailLabel.setObjectName("cardEditTail")
        self.defaultNameButton = QPushButton(self)
        self.defaultNameButton.setObjectName("cardEditDefaultName")
        self.defaultNameButton.setProperty("role", "subtle")
        self.defaultNameButton.clicked.connect(lambda: self.nameEdit.setText(item.default_title))
        name_row = QHBoxLayout()
        name_row.addWidget(self.nameEdit, 1)
        name_row.addWidget(self.tailLabel)
        name_row.addWidget(self.defaultNameButton)
        self.nameRowLabel = QLabel(self)
        form.addRow(self.nameRowLabel, name_row)

        self.resolutionCombo = QComboBox(self)
        self.resolutionCombo.setObjectName("cardEditResolution")
        self.resolutionCombo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._wheelGuard = _WheelGuard(self)
        self.resolutionCombo.installEventFilter(self._wheelGuard)
        for index, rep in enumerate(item.unique_reps):
            self.resolutionCombo.addItem(self._repText(rep), index)
        self.resolutionCombo.setCurrentIndex(max(self._pickedIndex(), 0))
        self.resolutionCombo.currentIndexChanged.connect(self._onResolutionChanged)
        self.resolutionRowLabel = QLabel(self)
        form.addRow(self.resolutionRowLabel, self.resolutionCombo)

        # 구간 줄: 왼쪽 요약(요약 글 · 구간 줄) | 오른쪽 끝 「구간 편집…」(위끝 = 첫 줄 위끝). 버튼은 다른 줄의
        # 버튼(기본값 · 찾기… · 기본 위치)과 같은 오른쪽 열에 선다 — 프로토 배치(오너 2026-11-09)
        self.sectionsBox = QWidget(self)
        sections = QHBoxLayout(self.sectionsBox)
        sections.setContentsMargins(0, 0, 0, 0)
        summary = QVBoxLayout()
        self.sectionSummaryLabel = QLabel(self.sectionsBox)
        self.sectionSummaryLabel.setObjectName("cardEditSectionSummary")
        self.sectionSummaryLabel.setWordWrap(True)
        self.sectionLinesLabel = QLabel(self.sectionsBox)
        self.sectionLinesLabel.setObjectName("cardEditSectionLines")
        summary.addWidget(self.sectionSummaryLabel)
        summary.addWidget(self.sectionLinesLabel)
        self.sectionButton = QPushButton(self.sectionsBox)
        self.sectionButton.setObjectName("cardEditSectionButton")
        self.sectionButton.clicked.connect(self.openSections)
        sections.addLayout(summary, 1)
        sections.addWidget(self.sectionButton, 0, Qt.AlignmentFlag.AlignTop)
        self.sectionsRowLabel = QLabel(self)
        form.addRow(self.sectionsRowLabel, self.sectionsBox)
        # 구간 기능이 없는 타입이면 구간 줄을 두지 않는다(카드의 구간 편집과 같은 판정)
        form.setRowVisible(self.sectionsBox, item.content_type in SECTION_CONTENT_TYPES)

        self.pathLabel = PathLabel(self)
        self.pathLabel.setObjectName("cardEditPath")
        self.browseButton = QPushButton(self)
        self.browseButton.setObjectName("cardEditBrowse")
        self.browseButton.clicked.connect(self.browse)
        self.defaultPathButton = QPushButton(self)
        self.defaultPathButton.setObjectName("cardEditDefaultPath")
        self.defaultPathButton.clicked.connect(lambda: self._setPath(self._defaultDir))
        path_row = QHBoxLayout()
        path_row.addWidget(self.pathLabel, 1)
        path_row.addWidget(self.browseButton)
        path_row.addWidget(self.defaultPathButton)
        self.pathRowLabel = QLabel(self)
        form.addRow(self.pathRowLabel, path_row)

        # 버튼 차례는 Qt 버튼 상자가 OS 관례로 정한다(설계 §12.1). 저장이 주 동작(채움 색 규칙)
        self.buttonBox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.saveButton = self.buttonBox.button(QDialogButtonBox.StandardButton.Save)
        self.saveButton.setProperty("role", "primary")
        # 버튼 상자가 만들며 이미 스타일을 입혔다 — 속성을 바꾼 뒤 다시 입힌다
        theme.repolish(self.saveButton)
        self.cancelButton = self.buttonBox.button(QDialogButtonBox.StandardButton.Cancel)
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)
        # 받는 중인 배치가 이 카드를 기다릴 때만 보이는 안내 — 구간 창과 같은 자리 · 같은 문구(#545)
        self.waitHintLabel = QLabel(self)
        self.waitHintLabel.setObjectName("cardEditWaitHintLabel")
        self.waitHintLabel.setWordWrap(True)
        self.waitHintLabel.setVisible(False)
        root.addWidget(self.waitHintLabel)
        root.addWidget(self.buttonBox)
        # Enter가 칸에서 창을 닫지 않게 — 어느 버튼도 기본 버튼이 되지 않는다(keyPressEvent)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        self._fitMinimumWidth()

    def _fitMinimumWidth(self) -> None:
        """창 최소폭을 「`DIALOG_WIDTH`와 내용 최소폭 중 큰 값」으로 건다(#555).

        최소폭을 직접 걸면 Qt는 레이아웃의 최소폭을 더는 최소폭으로 쓰지 않는다. 글자가 넓은 글꼴이나
        긴 번역에서 내용 최소폭이 `DIALOG_WIDTH`를 넘으면, 그 폭까지 줄인 창이 버튼을 글 폭보다 좁게
        누르고 감기는 구간 요약을 잘랐다. 내용이 바뀌어 레이아웃이 다시 잡힐 때마다(`event`의
        `LayoutRequest`) 다시 부른다.
        """
        layout = self.layout()
        content = layout.totalMinimumSize().width() if layout is not None else 0
        width = max(DIALOG_WIDTH, content)
        if self.minimumWidth() != width:
            self.setMinimumWidth(width)

    def event(self, event: QEvent) -> bool:
        """레이아웃이 다시 잡힌 뒤 최소폭을 내용에 맞춘다 — 글 · 글꼴 · 번역이 바뀐 때다(#555)."""
        handled = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self._fitMinimumWidth()
        return handled

    def _retranslateUi(self) -> None:
        self.setWindowTitle(self.tr("Edit"))
        self._refreshMeta()
        self.nameRowLabel.setText(self.tr("File name"))
        self.defaultNameButton.setText(self.tr("Default"))
        self.resolutionRowLabel.setText(self.tr("Resolution"))
        self.sectionsRowLabel.setText(self.tr("Sections"))
        self.sectionButton.setText(self.tr("Edit sections…"))
        self.pathRowLabel.setText(self.tr("Save location"))
        self.browseButton.setText(self.tr("Browse…"))
        self.defaultPathButton.setText(self.tr("Default location"))
        # 구간 창의 문구를 그대로 쓴다 — 번역 컨텍스트도 구간 창 것이라 번역 항목이 하나다
        self.waitHintLabel.setText(
            QCoreApplication.translate(
                "SectionEditDialog",
                "A download is waiting for this card — it continues when you close this window",
            )
        )

    # ---- 해상도 ----

    @staticmethod
    def _repText(rep) -> str:
        """해상도 목록의 한 줄 — 「1080p(원본) 60fps · 595.34 MB」. 크기를 모르면 조회 중 글 그대로."""
        text = card_text.rep_label(rep)
        secondary = card_text.rep_secondary(rep)
        if secondary:
            text = f"{text} {secondary}"
        size = rep[-1] if len(rep) > 2 else ""
        return f"{text} · {size}" if size else text

    def _pickedIndex(self) -> int:
        """카드가 지금 고른 해상도의 목록 번호. 없으면 -1.

        해상도 · 주소 · 스트림으로 찾는다 — 세그먼트 목록(m3u8)은 항목마다 주소가 비어 있어, 해상도 ·
        주소만 보면 같은 해상도의 다른 스트림(원본 · 60fps)을 가르지 못한다. 카드의
        `syncPickedResolution`과 같은 기준이다(#550).
        """
        item = self._item
        picked = (item.resolution, item.base_url, getattr(item, "stream", None))
        for index, rep in enumerate(item.unique_reps):
            if (rep[0], rep[1], getattr(rep, "stream", None)) == picked:
                return index
        return -1

    def _chosenRep(self) -> list | None:
        """드롭다운에서 고른 해상도 항목. 목록이 비었으면 None."""
        index = self.resolutionCombo.currentData()
        if index is None or index >= len(self._item.unique_reps):
            return None
        return self._item.unique_reps[index]

    def updateRepSize(self, index: int, size_text: str) -> None:
        """카드의 크기 조회가 창이 열린 사이 도착했다 — 그 해상도 줄의 글을 고친다.

        카드의 크기 조회 알림(`ContentItemWidget.repSizeChanged`)에 잇는다. 창이 지워지면 연결이
        함께 끊기므로 늦게 온 조회가 지운 창을 건드리지 않는다.
        """
        if 0 <= index < self.resolutionCombo.count() and index < len(self._item.unique_reps):
            self.resolutionCombo.setItemText(index, self._repText(self._item.unique_reps[index]))

    def setWaitingHint(self, waiting: bool) -> None:
        """받는 중인 배치가 이 카드를 기다리는지에 맞춰 아래쪽 안내를 켜고 끈다(구간 창과 같다)."""
        self.waitHintLabel.setVisible(waiting)

    def _onResolutionChanged(self) -> None:
        self._refreshTail()
        self._refreshMeta()

    def _refreshMeta(self) -> None:
        """머리 줄 — 「채널 · 업로드 날짜 · 고른 해상도」. 해상도는 드롭다운에서 고른 것이다."""
        rep = self._chosenRep()
        resolution = card_text.rep_label(rep) if rep is not None else ""
        meta = [self._item.channel_name, card_text.upload_date_text(self._item), resolution]
        self.metaLabel.setText(" · ".join(part for part in meta if part))

    # ---- 파일명 꼬리 · 구간 · 저장 위치 표시 ----

    def _refreshTail(self) -> None:
        rep = self._chosenRep()
        if rep is None:
            self.tailLabel.setText("")
            return
        tag = file_tag(self._item.unique_reps, rep)
        self.tailLabel.setText(
            card_text.file_tail(rep[0], tag, card_text.section_count(self._draft))
        )

    def _refreshSections(self) -> None:
        draft = self._draft
        if card_text.section_count(draft):
            self.sectionSummaryLabel.setText(card_text.section_summary(draft))
            self.sectionLinesLabel.setText("\n".join(card_text.section_lines(draft)))
            self.sectionLinesLabel.setVisible(True)
        else:
            self.sectionSummaryLabel.setText(self.tr("Whole video"))
            self.sectionLinesLabel.setVisible(False)

    def _refreshPath(self) -> None:
        self.pathLabel.setPathParts(*path_display_parts(self._path))
        self.pathLabel.setToolTip(self._path)
        self.defaultPathButton.setEnabled(self._path != self._defaultDir)

    def _setPath(self, path: str) -> None:
        if path:
            self._path = path
            self._refreshPath()

    def browse(self) -> None:
        """폴더 선택 대화상자로 저장 위치를 고른다 — 카드의 저장 위치 바꾸기와 같은 대화상자다."""
        chosen = QFileDialog.getExistingDirectory(
            self, self.tr("Select download folder"), self._path or ""
        )
        self._setPath(chosen)

    # ---- 구간 창 ----

    def openSections(self) -> None:
        """지금 구간 창을 초안에 대해 연다. 초안의 해상도는 드롭다운에서 고른 것이다."""
        if self.sectionDialog is not None:
            return
        rep = self._chosenRep()
        if rep is not None:
            self._draft.select_rep(rep)
        viewmodel = SectionEditViewModel(self._draft, None, self)
        dialog = SectionEditDialog(viewmodel, self)
        self.sectionDialog = dialog
        dialog.finished.connect(lambda result: self._onSectionsClosed(viewmodel, dialog, result))
        dialog.open()
        viewmodel.start()

    def _onSectionsClosed(self, viewmodel, dialog, result: int) -> None:
        if result == QDialog.DialogCode.Accepted:
            self._draftSections = True
            self._refreshSections()
            self._refreshTail()
        viewmodel.release()  # 조회하며 받은 moov를 놓는다(확인했으면 초안이 넘겨받았다)
        viewmodel.deleteLater()
        dialog.deleteLater()
        self.sectionDialog = None

    # ---- 저장 · 취소 ----

    def resizeEvent(self, event) -> None:
        """폭에 맞는 높이를 최소 높이로 건다 — 감기는 글(구간 경고 안내)이 잘리지 않게.

        최상위 창의 최소 높이는 감긴 글의 높이(높이-폭 관계)를 따라가지 않는다. 폭이 바뀔 때마다
        그 폭에서 레이아웃이 바라는 높이를 다시 걸면, 모자란 창은 Qt가 그 높이로 늘린다.
        """
        super().resizeEvent(event)
        layout = self.layout()
        if layout is not None and layout.hasHeightForWidth():
            self.setMinimumHeight(layout.totalHeightForWidth(self.width()))

    def keyPressEvent(self, event) -> None:
        """Enter는 초점이 있는 버튼만 누른다 — 칸에서 친 Enter는 창을 닫지 않는다. Esc는 취소."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focused = self.focusWidget()
            if isinstance(focused, QAbstractButton) and focused.isEnabled():
                focused.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def writable(self) -> bool:
        """카드에 써도 되는지 — 아직 목록에 있고 대기 상태다."""
        return self._item.downloadState == DownloadState.WAITING and self._isPresent(self._item)

    def accept(self) -> None:
        """고친 값을 카드에 한 번에 쓰고 닫는다. 쓸 수 없는 카드면 쓰지 않고 닫는다."""
        if not self.writable():
            logger.info("카드 편집 무시 — 창이 열린 사이 카드가 바뀜(%s)", self._item.downloadState)
            self._restoreHead()
            super().reject()
            return
        self._write()
        super().accept()

    def reject(self) -> None:
        """아무것도 쓰지 않고 닫는다 — 구간 창이 초안으로 옮긴 moov 주인은 카드로 되돌린다."""
        self._restoreHead()
        super().reject()

    def _restoreHead(self) -> None:
        if not self._draftSections:
            return
        held = self._heldHead
        if held is not None:
            keep_section_head(self._item, held[0], held[1])
        else:
            keep_section_head(self._draft, self._draft.base_url, None)

    def _write(self) -> None:
        item = self._item
        item.title = self.nameEdit.text().strip() or item.default_title
        item.download_path = self._path
        rep = self._chosenRep()
        changed = False
        if rep is not None:
            before = (item.resolution, item.base_url, item.stream)
            # 해상도 pill을 누른 것과 같은 쓰기다(ContentItemWidget.setresolutionUrlSize)
            item.resolution, item.base_url = rep[0], rep[1]
            item.select_rep(rep)
            changed = (item.resolution, item.base_url, item.stream) != before
            if not item.is_segment_based and len(rep) > 2:
                item.total_size = rep[-1]  # 카드가 해상도 항목 끝에 붙여 둔 크기 글
        if self._draftSections:
            for name in SECTION_FIELDS:
                setattr(item, name, getattr(self._draft, name))
            head = getattr(self._draft, "section_head", None)
            keep_section_head(item, head[0] if head else item.base_url, head[1] if head else None)
        logger.info(
            "카드 편집 저장: 해상도 %sp · 구간 %d개 · 해상도 바뀜 %s",
            item.resolution,
            card_text.section_count(item),
            changed,
        )
        if changed and card_text.section_count(item):
            self._refit(item)
        self._notify(item)
