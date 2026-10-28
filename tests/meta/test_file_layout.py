"""파일 배치 게이트 — 루트에 둘 수 있는 파일과 끝 이름 겹침 (#472).

규칙(CLAUDE.md 「파일 배치」): 새 모듈은 역할이 맞는 패키지에 두고 `core/` · `app/` 바로 아래에는
만들지 않는다. 새 모듈 · 패키지 이름은 저장소 안의 다른 모듈과 끝 이름이 겹치지 않게 짓는다 —
서비스 폴더(`core/<서비스>/`) 사이의 같은 역할 모듈만 예외다.

역할이 맞는지는 재지 못한다(규칙 문장과 리뷰의 몫). 여기서는 기계로 잴 수 있는 둘만 잰다.
루트 허용 목록 · 겹침 예외 목록을 고치면 그 수정이 PR diff에 보인다 — 목록을 고치는 것이 곧 그 결정이다.
"""

from collections import defaultdict
from pathlib import Path

from tests.support.layers import (
    APP_DIR,
    CORE_DIR,
    ROOT_DIR,
    contains_py,
    is_skipped_dir,
    owned_top_level_names,
)

#: 패키지 바로 아래에 둘 수 있는 파일. 이 밖의 파일은 역할이 맞는 하위 패키지로 간다.
ROOT_FILES: dict[Path, frozenset[str]] = {
    CORE_DIR: frozenset({"__init__.py"}),
    APP_DIR: frozenset({"__init__.py", "network.py", "theme.py"}),
}

#: 서비스 폴더(`core/<서비스>/`) — 서로 사이에서는 같은 역할의 모듈 이름이 같아도 된다.
SERVICE_PACKAGES = frozenset({"chzzk"})

#: 이미 겹친 끝 이름 → (그 이름의 주인 전부, 이유). 주인이 하나라도 빠지면 여기서 고친다(남아 있으면
#: 실패한다). 주인이 늘어난 것은 새 겹침이다 — 끝 이름만 적으면 같은 이름의 새 모듈이 예외로 지나간다.
KNOWN_OVERLAPS: dict[str, tuple[frozenset[str], str]] = {
    "mainWindow": (
        frozenset({"app.views.mainWindow", "ui.mainWindow"}),
        "ui.mainWindow는 uic 생성물 — 셸 개편 때 .ui를 없애며 풀린다",
    ),
    "config": (
        frozenset({"config", "config.config"}),
        "config 패키지와 그 안의 모듈 — 2.12.0-rc1 뒤 app/config.py로 옮기며 풀린다",
    ),
}


def _names_under(directory: Path, dotted: str) -> tuple[list[str], set[str]]:
    """디렉토리 아래 패키지 · 모듈의 점 구분 이름 전부와 그중 패키지 이름.

    자기 자신을 포함하고 `__init__`은 패키지 이름으로 센다. `.py`가 없는 디렉토리(리소스 ·
    `__pycache__`만 남은 옛 폴더)는 import할 수 없으므로 세지 않는다 — 기준은 소유 이름의 유도와 같다.
    """
    names, packages = [dotted], {dotted}
    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            if not is_skipped_dir(entry.name) and contains_py(entry):
                sub_names, sub_packages = _names_under(entry, f"{dotted}.{entry.name}")
                names += sub_names
                packages |= sub_packages
        elif entry.suffix == ".py" and entry.name != "__init__.py":
            names.append(f"{dotted}.{entry.stem}")
    return names, packages


def _tails() -> tuple[dict[str, list[str]], set[str]]:
    """끝 이름 → 그 이름을 가진 모듈 · 패키지, 그리고 그중 패키지 이름.

    범위는 저장소가 소유한 최상위 이름 중 `tests` 밖. 같은 최상위 이름의 디렉토리와 루트 `.py`
    (`app/` · `app.py`)는 따로 센다 — 소유 이름은 하나로 접히지만 겹침은 둘이다.
    """
    tails: dict[str, list[str]] = defaultdict(list)
    packages: set[str] = set()
    for top in sorted(owned_top_level_names() - {"tests"}):
        names: list[str] = []
        directory = ROOT_DIR / top
        if directory.is_dir() and contains_py(directory):
            names, packages_under = _names_under(directory, top)
            packages |= packages_under
        if directory.with_suffix(".py").is_file():
            names.append(top)
        for name in names:
            tails[name.rsplit(".", 1)[-1]].append(name)
    return tails, packages


def _between_services(
    names: list[str], packages: set[str], services: frozenset[str] = SERVICE_PACKAGES
) -> bool:
    """겹친 이름이 전부 서로 다른 서비스 폴더 바로 아래의 모듈(패키지 아님)인가."""
    parts = [name.split(".") for name in names]
    in_services = all(len(p) == 3 and p[0] == "core" and p[1] in services for p in parts)
    modules_only = not packages.intersection(names)
    return in_services and modules_only and len({p[1] for p in parts}) == len(parts)


def test_core_and_app_hold_only_the_listed_files_directly() -> None:
    """core/ · app/ 바로 아래에 목록 밖의 파일이 있으면 그 파일이 실패 글에 찍혀야 한다.

    core/ · app/ 바로 아래 파일(숨김 파일 제외) · ROOT_FILES
    -> 목록 밖 파일 0 · 목록에 있는데 없는 파일 0
    """
    outside, missing = [], []
    for directory, allowed in ROOT_FILES.items():
        present = {
            entry.name
            for entry in directory.iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        }
        outside += [
            (directory / name).relative_to(ROOT_DIR).as_posix() for name in present - allowed
        ]
        missing += [
            (directory / name).relative_to(ROOT_DIR).as_posix() for name in allowed - present
        ]

    assert not missing, f"ROOT_FILES에 있는데 없는 파일 — 목록에서 지울 것: {sorted(missing)}"
    assert not outside, (
        "core/ · app/ 바로 아래에 새 파일 — 역할이 맞는 하위 패키지에 둔다(CLAUDE.md 「파일 배치」): "
        + ", ".join(sorted(outside))
    )


def test_no_two_modules_or_packages_share_a_last_name() -> None:
    """모듈 · 패키지 둘이 끝 이름이 같으면 그 이름과 주인들이 실패 글에 찍혀야 한다.

    저장소가 소유한 최상위 이름(tests 제외) 아래 모듈 · 패키지 전부 · SERVICE_PACKAGES · KNOWN_OVERLAPS
    -> 예외 주인 밖의 겹침 0 · 예외 목록에 남은 풀린 주인 0
    """
    tails, packages = _tails()
    assert len(tails) > 50, f"모듈 · 패키지를 {len(tails)}개만 찾았다 — 순회 범위를 확인할 것"
    overlaps = {
        tail: names
        for tail, names in tails.items()
        if len(names) > 1 and not _between_services(names, packages)
    }
    known = {tail: owners for tail, (owners, _) in KNOWN_OVERLAPS.items()}
    new = {
        tail: names for tail, names in overlaps.items() if set(names) - known.get(tail, frozenset())
    }
    resolved = {
        tail: sorted(gone)
        for tail, owners in known.items()
        if (gone := owners - set(overlaps.get(tail, ())))
    }

    assert not resolved, f"겹침이 풀렸다 — KNOWN_OVERLAPS에서 고칠 것: {resolved}"
    assert not new, "끝 이름이 겹친다(CLAUDE.md 「파일 배치」):\n" + "\n".join(
        f"{tail}: {names}" for tail, names in sorted(new.items())
    )


def test_modules_in_two_service_packages_may_share_a_last_name() -> None:
    """서로 다른 서비스 폴더 바로 아래의 같은 이름 모듈만 겹침으로 세지 않아야 한다.

    서비스 폴더 chzzk · youtube(가정, 파일 없음)
    core.chzzk.metadata_service · core.youtube.metadata_service(모듈) -> 예외
    core.chzzk.foo · core.youtube.foo(패키지) -> 예외 아님(모듈만)
    core.chzzk.metadata_service · core.media.metadata_service -> 예외 아님(서비스 밖)
    core.chzzk.metadata_service · core.chzzk.sub.metadata_service -> 예외 아님(같은 서비스 안)
    """
    services = SERVICE_PACKAGES | {
        "youtube"
    }  # 서비스 폴더를 하나 더 가정한다 — 파일은 만들지 않는다
    packages = {"core.chzzk.foo", "core.youtube.foo"}

    assert _between_services(
        ["core.chzzk.metadata_service", "core.youtube.metadata_service"], packages, services
    )
    assert not _between_services(["core.chzzk.foo", "core.youtube.foo"], packages, services)
    assert not _between_services(
        ["core.chzzk.metadata_service", "core.media.metadata_service"], packages, services
    )
    assert not _between_services(
        ["core.chzzk.metadata_service", "core.chzzk.sub.metadata_service"], packages, services
    )
