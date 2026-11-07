"""헤더바 저장 위치 버튼의 상태 — 지금 위치 · 기본 위치 · 즐겨찾기 · 최근 (#495).

앱은 늘 기본 저장 위치로 시작한다(오너 2026-11-05). 기본 위치는 설정 `downloadPath`(실존할 때)
→ 시스템 다운로드 폴더 → 작업 폴더 순으로 호출자가 정해 넘긴다. 위치를 골라도 `downloadPath`는
바뀌지 않고, 고른 위치는 `recentPaths`에 남는다 — 다음 실행의 메뉴 「최근」에 보인다.

고른 위치는 그 뒤에 등록하는 카드부터 쓴다. 이미 있는 카드의 위치는 바꾸지 않는다 — 그것은
카드마다의 저장 위치다(#245).

화면을 모른다 — 메뉴의 줄은 `menu_entries`가 데이터로 내주고 위젯은 그 차례대로 그린다.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from app.system import settings
from app.viewmodels.path_gates import check_remember_path, normalize_path

RECENT_SHOWN = 3  # 메뉴 「최근」에 보이는 수 — 지금 위치 · 즐겨찾기를 뺀 앞에서부터(설계 §2)


@dataclass(frozen=True)
class LocationEntry:
    """저장 위치 메뉴의 한 줄 — 지금 위치 · 즐겨찾기 · 최근 중 하나."""

    kind: str  # "current" · "favorite" · "recent"
    path: str
    name: str  # 즐겨찾기 별칭. 지금 위치 · 최근이면 빈 문자열
    missing: bool  # 그 경로에 폴더가 없다 — 흐리게 「(없음)」, 고를 수 없다


def folder_name(path: str) -> str:
    """경로의 마지막 폴더 이름 — 루트(`C:\\` · `/`)처럼 이름이 없으면 경로 그대로."""
    return os.path.basename(path.rstrip("\\/")) or path


class SaveLocations(QObject):
    """저장 위치 버튼이 보이는 값과 고르기 · 즐겨찾기 더하기를 맡는다 (#495).

    바뀔 때마다(`changed`) 버튼이 글 · 메뉴를 다시 그린다. 저장은 즐겨찾기 · 최근이 바뀐 때만
    한다 — 바뀐 키만 쓴다(`settings.update_settings`).
    """

    changed = Signal()

    def __init__(self, default_path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._default = normalize_path(default_path)
        self._current = self._default
        self._favorites: list[dict] = [
            dict(entry) for entry in settings.load_setting("favoritePaths")
        ]
        self._recent: list[str] = list(settings.load_setting("recentPaths"))

    def current(self) -> str:
        """지금 위치 — 다음에 등록하는 카드의 저장 위치."""
        return self._current

    def default(self) -> str:
        """기본 저장 위치 — 시작 위치이자 「기본 위치로」가 돌아가는 곳."""
        return self._default

    def is_default(self) -> bool:
        """지금 위치가 기본 저장 위치인지 — 「기본 위치로」를 끌지 정한다(#495 inbox 71).

        즐겨찾기 겹침 막음 · 고르기와 같은 비교다: 둘 다 `normalize_path`(절대 경로 — 상대 경로와 끝
        구분자가 정리된다)를 거친 뒤 글자 그대로 견준다. 대소문자는 맞추지 않는다 — 지금 경로 비교
        어디에서도 맞추지 않는다.
        """
        return normalize_path(self._current) == self._default

    def favorites(self) -> list[dict]:
        """즐겨찾기 `{"name", "path"}` 목록의 사본(더한 차례)."""
        return [dict(entry) for entry in self._favorites]

    def favorite_for(self, path: str) -> dict | None:
        """그 경로의 즐겨찾기 줄. 없으면 None."""
        return next((dict(entry) for entry in self._favorites if entry["path"] == path), None)

    def display_name(self) -> str:
        """버튼 글 — 지금 위치가 즐겨찾기면 별칭, 아니면 폴더 이름."""
        favorite = self.favorite_for(self._current)
        return favorite["name"] if favorite else folder_name(self._current)

    def recent_shown(self) -> list[str]:
        """메뉴 「최근」의 경로 — 지금 위치 · 즐겨찾기를 뺀 앞 `RECENT_SHOWN`개."""
        favorite_paths = {entry["path"] for entry in self._favorites}
        shown = [p for p in self._recent if p != self._current and p not in favorite_paths]
        return shown[:RECENT_SHOWN]

    def menu_entries(self, exists: Callable[[str], bool] = os.path.isdir) -> list[LocationEntry]:
        """메뉴 위쪽 묶음의 줄 — 지금 위치 · 즐겨찾기 · 최근 차례(설계 §2).

        명령 줄(즐겨찾기에 더하기 · 폴더 찾기… · 기본 위치로)은 위젯이 붙인다. `exists`는 폴더가
        있는지 보는 함수다(테스트가 바꿔 끼운다). 지금 위치는 없어져도 고른 채로 둔다 — 등록 관문이
        막는다.
        """
        entries = [LocationEntry("current", self._current, "", not exists(self._current))]
        for entry in self._favorites:
            entries.append(
                LocationEntry("favorite", entry["path"], entry["name"], not exists(entry["path"]))
            )
        for path in self.recent_shown():
            entries.append(LocationEntry("recent", path, "", not exists(path)))
        return entries

    def can_add_favorite(self) -> bool:
        """지금 위치를 즐겨찾기에 더할 수 있는지 — 이미 있거나 가득 차면 못 한다."""
        return (
            self.favorite_for(self._current) is None
            and len(self._favorites) < settings.FAVORITES_MAX
        )

    def choose(self, path: str) -> bool:
        """위치를 고른다 — 그 뒤에 등록하는 카드부터. 실존 폴더가 아니면 고르지 않는다(False).

        고른 위치는 최근의 맨 앞에 올린다(즐겨찾기 · 기본 위치는 올리지 않는다 — 메뉴에 따로
        있다). 지금 위치를 다시 골라도 최근 차례만 앞으로 온다.
        """
        path = normalize_path(path)
        if not check_remember_path(path):
            return False
        self._current = path
        if path != self._default and self.favorite_for(path) is None:
            recent = [path] + [p for p in self._recent if p != path]
            self._saveRecent(recent[: settings.RECENT_PATHS_MAX])
        self.changed.emit()
        return True

    def reset(self) -> None:
        """「기본 위치로」 — 지금 위치를 기본 저장 위치로 되돌린다."""
        self._current = self._default
        self.changed.emit()

    def add_favorite(self, name: str, path: str) -> str:
        """즐겨찾기에 더한다 — 막히면 그 까닭(번역된 글), 되면 빈 문자열.

        별칭은 앞뒤 공백을 지운다. 비었거나 · 가득 찼거나 · 경로나 별칭이 이미 있으면 막는다.
        더한 경로는 최근에서 뺀다(메뉴에서 겹쳐 보이지 않게).
        """
        name = name.strip()
        path = normalize_path(path)
        if not name:
            return self.tr("Enter a name for the favorite.")
        if len(self._favorites) >= settings.FAVORITES_MAX:
            return self.tr("You can add up to {} favorites.").format(settings.FAVORITES_MAX)
        if self.favorite_for(path) is not None:
            return self.tr("This folder is already a favorite.")
        if any(entry["name"] == name for entry in self._favorites):
            return self.tr("A favorite with this name already exists.")
        self._favorites.append({"name": name, "path": path})
        changes: dict[str, object] = {"favoritePaths": self.favorites()}
        if path in self._recent:
            self._recent = [p for p in self._recent if p != path]
            changes["recentPaths"] = list(self._recent)
        settings.update_settings(changes)
        self.changed.emit()
        return ""

    def _saveRecent(self, recent: list[str]) -> None:
        """최근 목록을 바꾸고, 저장된 값과 다를 때만 저장한다."""
        self._recent = recent
        settings.update_settings({"recentPaths": list(recent)})  # 저장된 값과 같으면 쓰지 않는다
