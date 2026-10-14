"""구간 컷을 도중에 실패시키는 테스트 도구 (#309).

컷 함수를 통째로 바꿔 예외를 내면 컷이 만드는 것(작업 폴더 · 출력 파일 · 구간별 중간
파일)이 하나도 생기지 않아, "실패한 구간이 아무것도 남기지 않는다"를 잴 수 없다. 이
도구는 실제 ``hybrid_cut``을 그대로 돌리다가 그 안의 첫 ffmpeg 실행 자리에서 출력 파일을
반쯤 쓰고 ``CutError``를 낸다 — 정리는 제품 코드가 한다.
"""

import os

import core.utils.hybrid_cut as hybrid_cut_module
from core.utils.hybrid_cut import CUT_FAILED, CutError

HALF_WRITTEN = b"\x00" * 64  # 반쯤 쓰인 출력 파일의 내용 — mp4가 아니면 된다


class MidwayCutFailure:
    """``arm()``한 뒤 처음 도는 ``hybrid_cut``을 첫 ffmpeg 실행 자리에서 한 번 실패시킨다.

    실패시킨 순간에 무엇이 있었는지를 적어 둔다 — 정리된 뒤 "없다"를 단언하기 전에
    그것들이 실제로 있었음을 먼저 단언하는 데 쓴다.
    """

    def __init__(self, monkeypatch) -> None:
        self.fired = 0  # 실패시킨 횟수
        self.source_existed = False  # 실패시킨 순간 컷의 입력 파일이 있었는지
        self.work_dir = ""  # 실패한 컷의 작업 폴더 (hybrid_cut이 만든 것)
        self.work_dir_existed = False  # 실패시킨 순간 작업 폴더가 있었는지
        self.source_path = ""  # 실패한 컷의 입력 파일
        self.output_path = ""  # 실패한 컷의 출력 파일
        self._armed = False
        self._real_run = hybrid_cut_module._run
        monkeypatch.setattr(hybrid_cut_module, "_run", self._run)

    def arm(self, source_path: str, output_path: str) -> None:
        """다음 ffmpeg 실행을 실패시키게 한다. 컷의 입력 · 출력 경로를 함께 받는다."""
        self.source_path = os.path.abspath(source_path)
        self.output_path = os.path.abspath(output_path)
        self._armed = True

    def _run(self, args: list[str], timeout: float, cwd: str) -> str:
        if not self._armed:
            return self._real_run(args, timeout, cwd)
        self._armed = False
        self.fired += 1
        self.work_dir = cwd
        self.work_dir_existed = os.path.isdir(cwd)
        self.source_existed = os.path.exists(self.source_path)
        with open(self.output_path, "wb") as f:
            f.write(HALF_WRITTEN)
        raise CutError(CUT_FAILED, "시험")
