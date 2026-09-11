"""설정 창 레이아웃 게이트 — QFormLayout을 걷어낸 뒤(v2.9.7 S2) 살려낸 것을 고정한다.

QFormLayout이 암묵적으로 해주던 것 가운데 이 화면이 실제로 쓰고 있던 것은 둘이었다
(1단계 실측 — buddy·니모닉(`&`)·행 줄바꿈은 쓰이지 않았고, 탭 순서는 생성 순서라
레이아웃과 무관하다):

  ① 필드 성장 — 남는 가로 공간은 필드 열이 전부 흡수하고 라벨 열은 넓어지지 않는다
  ② 라벨 정렬 — 라벨은 셀을 채우지 않고 자기 sizeHint 폭으로 왼쪽에 붙고,
     세로 위치·높이는 **같은 라벨을 QFormLayout에 넣었을 때와 같다**(행 상단, 높이는
     sizeHint × 7/4 — 가운데 정렬이면 2px 내려앉는다, 오너 실기 확인). 라벨 열의
     도움말 버튼도 가로 규칙은 같다

여기에 레이아웃 교체가 건드리기 쉬운 것 둘을 더 잰다:

  ③ 탭 순서 — 위젯 여섯의 키보드 순서, 그 뒤에 OK/Cancel
  ④ 창 최소 크기 — 상수가 아니라 레이아웃에서 유도된다(그룹 상자 셋 중 가장 넓은 것)

전부 **폰트에 의존하지 않는 기하 관계**로 잰다(sizeHint·상대 위치·폭 차이만 본다).
고장 주입으로 확인한 것: 열 stretch를 빼면 ①이, 라벨 정렬을 빼면 ②(가로)가, 세로 정렬을
AlignVCenter로 되돌리거나 라벨을 평범한 QLabel(×7/4 없음)로 되돌리면 ②(세로 대조군)가,
생성 순서를 바꾸면 ③이, QFormLayout으로 되돌리면 첫 게이트가 실패한다.
"""

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFormLayout, QGroupBox, QLabel, QLayout, QWidget

import config.config as config
from app.views.dialog import SettingDialog

#: 각 그룹 상자의 (라벨, 필드) 짝 — objectName. 도움말 버튼은 라벨 열에 홀로 있다.
ROWS = {
    "cookiesBox": [("nidautLabel", "nidaut"), ("nidsesLabel", "nidses")],
    "downloadBox": [("afterDownloadLabel", "afterDownload")],
    "commonBox": [("languageLabel", "language"), ("logsFolderLabel", "logsFolder")],
}
#: 키보드 순서 — 위에서 아래로, 라벨 열 항목(도움말)은 자기 행 차례에
TAB_ORDER = ["nidaut", "nidses", "helpButton", "afterDownload", "language", "logsFolder"]


def _seed_config() -> None:
    path = Path(config.CONFIG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.default_config()), encoding="utf-8")


@pytest.fixture
def dialog(qapp):
    _seed_config()
    dlg = SettingDialog()
    dlg.show()
    QApplication.processEvents()
    yield dlg
    dlg.close()
    dlg.deleteLater()
    QApplication.processEvents()


def _at_width(dlg: SettingDialog, extra: int) -> None:
    """창을 최소폭 + extra 로 놓고 레이아웃을 정착시킨다."""
    dlg.resize(dlg.minimumSizeHint().width() + extra, dlg.height())
    QApplication.processEvents()
    assert dlg.width() == dlg.minimumSizeHint().width() + extra, "폭이 요청대로 안 잡혔다 — 전제"


def _widget(dlg: SettingDialog, name: str):
    w = getattr(dlg, name)
    assert w.objectName() == name
    return w


def _right(w) -> int:
    return w.geometry().x() + w.geometry().width()


def _box_inner_right(box: QGroupBox) -> int:
    """그룹 상자 안에서 필드가 닿을 수 있는 오른쪽 끝 — 상자 폭에서 레이아웃 오른쪽 여백을 뺀 값."""
    return box.contentsRect().right() + 1 - box.layout().contentsMargins().right()


# ================================================================ 교체 자체


def test_no_form_layout_remains(dialog):
    """설정 창 어디에도 QFormLayout이 없다 — S2의 목적."""
    forms = [layout for layout in dialog.findChildren(QLayout) if isinstance(layout, QFormLayout)]
    assert forms == [], f"QFormLayout이 남아 있다: {[f.objectName() for f in forms]}"
    assert not isinstance(dialog.layout(), QFormLayout)


# ================================================================ ① 필드 성장


@pytest.mark.parametrize("extra", [0, 240])
def test_fields_reach_the_inner_right_edge(dialog, extra):
    """필드는 어느 폭에서든 그룹 상자 안쪽 오른쪽 끝까지 닿는다."""
    _at_width(dialog, extra)
    for box_name, rows in ROWS.items():
        box = _widget(dialog, box_name)
        for _, field_name in rows:
            field = _widget(dialog, field_name)
            assert _right(field) == _box_inner_right(box), (
                f"{field_name}: 오른쪽 끝 {_right(field)} ≠ 상자 안쪽 끝 {_box_inner_right(box)} (extra={extra})"
            )


def test_extra_width_goes_to_fields_not_labels(dialog):
    """창을 넓히면 늘어난 만큼 필드만 넓어지고 라벨 열(라벨 x·폭, 필드 x)은 그대로다."""
    _at_width(dialog, 0)
    narrow = {
        name: (_widget(dialog, name).geometry().x(), _widget(dialog, name).geometry().width())
        for rows in ROWS.values()
        for pair in rows
        for name in pair
    }
    _at_width(dialog, 240)
    for rows in ROWS.values():
        for label_name, field_name in rows:
            label, field = _widget(dialog, label_name), _widget(dialog, field_name)
            assert (label.geometry().x(), label.geometry().width()) == narrow[label_name], (
                f"{label_name}: 창을 넓혔더니 라벨이 움직였다/넓어졌다"
            )
            assert field.geometry().x() == narrow[field_name][0], (
                f"{field_name}: 필드 시작이 밀렸다"
            )
            assert field.geometry().width() == narrow[field_name][1] + 240, (
                f"{field_name}: 늘어난 240px 을 필드가 전부 흡수하지 않았다"
            )


# ================================================================ ② 라벨 정렬


def test_labels_hug_their_size_hint_on_the_left(dialog):
    """라벨은 열 폭이 아니라 자기 sizeHint 폭이고, 상자 안쪽 왼쪽 끝에 붙는다."""
    _at_width(dialog, 240)
    for box_name, rows in ROWS.items():
        box = _widget(dialog, box_name)
        left = box.contentsRect().left() + box.layout().contentsMargins().left()
        for label_name, _ in rows:
            label = _widget(dialog, label_name)
            assert label.geometry().x() == left, f"{label_name}: 왼쪽 끝이 상자 안쪽 끝과 다르다"
            assert label.geometry().width() == label.sizeHint().width(), (
                f"{label_name}: 폭 {label.geometry().width()} ≠ sizeHint {label.sizeHint().width()} — 셀 폭으로 늘어났다"
            )


def _form_reference(label_font, label_text: str, field_height: int) -> tuple[int, int]:
    """같은 라벨을 같은 높이의 필드와 QFormLayout에 넣었을 때의 (행 상단 대비 y, 높이) — 대조군.

    구 폼의 규칙(qformlayout.cpp: 라벨 높이 = min(행 높이, sizeHint 높이 × 7/4), 행 상단
    배치)을 테스트가 직접 적지 않고 **QFormLayout 자체에게 묻는다** — 제품(_FormLabel)이
    규칙을 틀리게 옮겨도 대조군은 틀리지 않는다. 폰트 무의존.
    """
    host = QWidget()
    form = QFormLayout(host)
    label = QLabel(label_text)
    label.setFont(label_font)
    field = QWidget()
    field.setFixedHeight(field_height)
    form.addRow(label, field)
    host.resize(300, field_height + 40)
    host.show()
    QApplication.processEvents()
    result = (label.geometry().y() - field.geometry().y(), label.geometry().height())
    host.close()
    host.deleteLater()
    return result


def test_labels_sit_where_a_form_layout_would_put_them(dialog):
    """라벨의 세로 위치·높이가 구 QFormLayout과 같다 — 위쪽 끝은 필드와 같고 높이는 폼 규칙.

    구 폼은 라벨을 행 상단에 놓되 높이를 sizeHint × 7/4까지 늘려 글자가 그 안에서
    가운데 온다. sizeHint 그대로 상단에 붙이면 글자가 위로, 가운데 정렬이면 아래로
    어긋난다(2px, 오너 실기 확인). 대조군은 QFormLayout에게 직접 묻는다.
    """
    _at_width(dialog, 120)
    for rows in ROWS.values():
        for label_name, field_name in rows:
            label, field = _widget(dialog, label_name), _widget(dialog, field_name)
            expected_dy, expected_h = _form_reference(label.font(), label.text(), field.geometry().height())
            actual = (label.geometry().y() - field.geometry().y(), label.geometry().height())
            assert actual == (expected_dy, expected_h), (
                f"{label_name}: (필드 대비 y, 높이) {actual} ≠ QFormLayout 대조군 {(expected_dy, expected_h)}"
            )


def test_help_button_sits_in_the_label_column_at_hint_width(dialog):
    """도움말 버튼은 라벨 열에 있고, 열이 더 넓어도 자기 sizeHint 폭을 지킨다."""
    _at_width(dialog, 0)
    help_button = _widget(dialog, "helpButton")
    assert help_button.geometry().x() == _widget(dialog, "nidautLabel").geometry().x()
    assert help_button.geometry().width() == help_button.sizeHint().width()


def test_rows_in_a_box_share_columns(dialog):
    """같은 상자의 행들은 라벨 x 와 필드 x·폭을 공유한다 — 열이 하나로 정렬돼 있다."""
    _at_width(dialog, 120)
    for rows in ROWS.values():
        if len(rows) < 2:
            continue
        label_x = {_widget(dialog, label).geometry().x() for label, _ in rows}
        field_span = {
            (_widget(dialog, field).geometry().x(), _widget(dialog, field).geometry().width())
            for _, field in rows
        }
        assert len(label_x) == 1 and len(field_span) == 1


# ================================================================ ③ 탭 순서


def test_tab_order_runs_top_to_bottom_then_ok_cancel(dialog):
    """Tab 은 쿠키 두 칸 → 도움말 → 다운로드 후 동작 → 언어 → 로그 폴더 → OK/Cancel 순이다."""
    chain = []
    cursor = dialog
    for _ in range(64):
        cursor = cursor.nextInFocusChain()
        if cursor is dialog:
            break
        if cursor.focusPolicy() != Qt.FocusPolicy.NoFocus and cursor.isVisibleTo(dialog):
            chain.append(cursor)
    names = [w.objectName() for w in chain[: len(TAB_ORDER)]]
    assert names == TAB_ORDER, f"탭 순서: {names}"
    button_box = _widget(dialog, "dialogButtonBox")
    assert isinstance(button_box, QDialogButtonBox)
    tail = chain[len(TAB_ORDER) :]
    assert tail and all(w.parent() is button_box for w in tail), "OK/Cancel 이 맨 뒤여야 한다"
    assert {w.text() for w in tail} == {b.text() for b in button_box.buttons()}


# ================================================================ ④ 창 최소 크기


def test_minimum_width_is_derived_from_the_widest_group_box(dialog):
    """창 최소폭은 상수가 아니라 그룹 상자 셋 중 가장 넓은 것 + 바깥 여백이다."""
    outer = dialog.layout().contentsMargins()
    widest = max(_widget(dialog, name).minimumSizeHint().width() for name in ROWS)
    assert dialog.minimumSizeHint().width() == widest + outer.left() + outer.right()
    assert dialog.minimumSize().width() == dialog.minimumSizeHint().width(), (
        "표시 직후 창은 최소 크기에서 시작한다"
    )


# ================================================================ ⑤ 남는 세로 공간


def _rows_top_left(dlg: SettingDialog) -> dict[str, int]:
    """상자마다 행에 놓인 위젯(라벨·필드·도움말)의 상자 기준 y — 행이 어디 붙어 있는지."""
    result = {}
    for box_name, rows in ROWS.items():
        box = _widget(dlg, box_name)
        names = [name for pair in rows for name in pair]
        if box_name == "cookiesBox":
            names.append("helpButton")
        for name in names:
            result[name] = _widget(dlg, name).mapTo(box, _widget(dlg, name).rect().topLeft()).y()
    return result


def test_extra_height_stays_below_the_rows(dialog):
    """창을 세로로 키우면 남는 공간은 각 상자의 행 **아래**에 남고 행은 움직이지 않는다.

    구 QFormLayout의 동작이다. 격자는 늘어날 수 있는 행이 없으면 남는 공간을 행 앞·사이·
    뒤에 나눠 뿌려서(qGeomCalc) 행 하나짜리 상자는 행이 가운데로 내려오고 행 둘은
    벌어진다 — 쿠키 상자만 3행째 스페이서가 공간을 흡수해 홀로 상단에 남아, 상자마다
    정렬이 달라 보였다(오너 실기 확인). 폰트 무의존 — sizeHint 높이 때의 y와 같은지만 본다.

    기준은 **최소 높이가 아니라 sizeHint 높이**다. 라벨 sizeHint(×7/4)가 필드보다 높은
    글꼴에서는 행의 sizeHint가 최소 높이보다 커서, 최소 높이에서는 행이 눌려 있다가
    창이 커지면 먼저 제 sizeHint까지 펴진다(구 폼의 `min(행 높이, ×7/4)` 규칙 그대로).
    그 몇 px은 이 게이트가 재려는 것이 아니다 — sizeHint에서 시작하면 그 단계가 없다.
    """
    dialog.resize(dialog.sizeHint())
    QApplication.processEvents()
    at_hint = _rows_top_left(dialog)
    box_heights = {name: _widget(dialog, name).height() for name in ROWS}

    dialog.resize(dialog.sizeHint().width(), dialog.sizeHint().height() + 240)
    QApplication.processEvents()
    for name in ROWS:
        assert _widget(dialog, name).height() > box_heights[name], f"{name}: 상자가 같이 안 늘어났다 — 전제"
    after = _rows_top_left(dialog)
    moved = {name: (y, after[name]) for name, y in at_hint.items() if after[name] != y}
    assert moved == {}, f"창을 키웠더니 행이 움직였다 (이름: sizeHint 높이 y → 키운 뒤 y): {moved}"
