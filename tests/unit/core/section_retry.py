"""일부 구간만 실패한 구간 다운로드를 다시 돌리는 테스트 도구 (#309).

세 경로(mp4 · fMP4 · TS)의 테스트가 같이 쓴다 — 컷 함수의 몇 번째 호출을 도중에 실패시키고,
실패한 실행이 남긴 것(``DownloadData.section_resume``)을 다음 실행에 넘기고, 파일이 다시
만들어졌는지를 수정 시각과 내용으로 가린다.
"""

import os
from collections.abc import Callable, Collection

from core.utils.paths import release_output_paths, reserve_section_output_paths
from tests.unit.core.midway_cut_failure import MidwayCutFailure

OUTPUT_ARG = 4  # 세 경로의 컷 함수 모두 출력 경로가 다섯째 위치 인자다


class CutCalls:
    """컷 함수를 감싸 호출마다의 출력 파일 이름을 적고, 고른 호출을 도중에 실패시킨다.

    ``fail_on``은 실패시킬 호출의 번호(1부터)다. ``after``는 호출이 끝난 뒤(성공했을 때)
    그 호출 번호를 받아 불린다 — 컷 사이에서 중단시키는 데 쓴다.
    """

    def __init__(self, monkeypatch, module, name: str, source_arg: int) -> None:
        self.outputs: list[str] = []  # 호출마다의 출력 파일 이름
        self.fail_on: Collection[int] = ()
        self.after: Callable[[int], None] | None = None
        self.failure = MidwayCutFailure(monkeypatch)
        self._real = getattr(module, name)
        self._source_arg = source_arg
        monkeypatch.setattr(module, name, self._call)

    def _call(self, *args, **kwargs):
        self.outputs.append(os.path.basename(args[OUTPUT_ARG]))
        number = len(self.outputs)
        if number in self.fail_on:
            self.failure.arm(source_path=args[self._source_arg], output_path=args[OUTPUT_ARG])
        result = self._real(*args, **kwargs)
        if self.after is not None:
            self.after(number)
        return result

    def restart(self) -> None:
        """다음 실행을 위해 적어 둔 것을 비우고 아무 호출도 실패시키지 않게 한다."""
        self.outputs.clear()
        self.fail_on = ()
        self.after = None


def hand_over(failed, again):
    """실패한 실행(failed)이 남긴 것을 새 실행(again)에 넘기고 again을 돌려준다.

    구간 파일 경로는 실패한 실행의 것을 그대로 다시 예약한다 — 다운로드를 시작하는 쪽이
    하는 일이다. again을 만들 때 배정된 새 이름의 예약은 푼다.
    """
    resume = failed.data.section_resume
    release_output_paths(again.paths)
    again.data.content.section_resume = resume
    again.paths = reserve_section_output_paths(resume.paths, resume.done)
    again.data.content.selection_paths = again.paths
    return again


def snapshot(paths: Collection[str]) -> dict[str, tuple[int, bytes]]:
    """파일마다의 (수정 시각 ns, 내용) — 다시 만들어졌는지 가리는 데 쓴다."""
    taken = {}
    for path in paths:
        with open(path, "rb") as f:
            taken[path] = (os.stat(path).st_mtime_ns, f.read())
    return taken
