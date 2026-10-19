"""지울 수 없는 경로를 흉내 내는 테스트 도구 (#389).

Windows에서는 다른 프로세스가 연 파일이나 그 자리의 폴더를 ``os.remove``가 지우지 못한다
(``PermissionError``). 파일 잠금은 OS마다 다르게 동작하므로 잠그지 않는다 — 고른 경로의
``os.remove`` · ``os.replace``가 ``PermissionError``를 내게 한다. 고르지 않은 경로는 그대로 둔다.
"""

import os


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(path))


class Undeletable:
    """``block()``한 경로를 지우지 못하게, ``block_replace_into()``한 경로로는 옮기지 못하게 한다.

    거절한 경로를 ``refused`` · ``replace_refused``에 적는다 — 주입이 실제로 일어났는지 단언하는 데 쓴다.
    """

    def __init__(self, monkeypatch) -> None:
        self.refused: list[str] = []  # 지우기를 거절한 경로
        self.replace_refused: list[str] = []  # 그리로 옮기기를 거절한 경로
        self._blocked: set[str] = set()
        self._replace_blocked: set[str] = set()
        self._real_remove = os.remove
        self._real_replace = os.replace
        monkeypatch.setattr(os, "remove", self._remove)
        monkeypatch.setattr(os, "replace", self._replace)

    def block(self, path) -> None:
        """그 경로의 ``os.remove``가 PermissionError를 내게 한다."""
        self._blocked.add(_key(path))

    def block_replace_into(self, path) -> None:
        """그 경로로 옮기는 ``os.replace``가 PermissionError를 내게 한다."""
        self._replace_blocked.add(_key(path))

    def release(self) -> None:
        """막은 것을 모두 푼다."""
        self._blocked.clear()
        self._replace_blocked.clear()

    def _remove(self, path, *args, **kwargs):
        if _key(path) in self._blocked:
            self.refused.append(str(path))
            raise PermissionError(13, "시험 — 지울 수 없다", str(path))
        return self._real_remove(path, *args, **kwargs)

    def _replace(self, src, dst, *args, **kwargs):
        if _key(dst) in self._replace_blocked:
            self.replace_refused.append(str(dst))
            raise PermissionError(13, "시험 — 그 자리에 둘 수 없다", str(dst))
        return self._real_replace(src, dst, *args, **kwargs)
