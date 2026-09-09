"""설정 창 레이아웃 게이트 — QFormLayout을 걷어낸 뒤(v2.9.7 S2) 살려낸 것을 고정한다.

QFormLayout이 암묵적으로 해주던 것 가운데 이 화면이 실제로 쓰고 있던 것은 둘이었다
(1단계 실측 — buddy·니모닉(`&`)·행 줄바꿈은 쓰이지 않았고, 탭 순서는 생성 순서라
레이아웃과 무관하다):

  ① 필드 성장 — 남는 가로 공간은 필드 열이 전부 흡수하고 라벨 열은 넓어지지 않는다
  ② 라벨 정렬 — 라벨은 셀을 채우지 않고 자기 sizeHint 폭으로 왼쪽에 붙는다
     (라벨 열에 놓인 도움말 버튼도 같은 규칙)

여기에 레이아웃 교체가 건드리기 쉬운 것 둘을 더 잰다:

  ③ 탭 순서 — 위젯 여섯의 키보드 순서, 그 뒤에 OK/Cancel
  ④ 창 최소 크기 — 상수가 아니라 레이아웃에서 유도된다(그룹 상자 셋 중 가장 넓은 것)

전부 **폰트에 의존하지 않는 기하 관계**로 잰다(sizeHint·상대 위치·폭 차이만 본다).
고장 주입으로 확인한 것: 열 stretch를 빼면 ①이, 라벨 정렬을 빼면 ②가, 생성 순서를
바꾸면 ③이, QFormLayout으로 되돌리면 첫 게이트가 실패한다.
"""

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFormLayout, QGroupBox, QLayout

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
