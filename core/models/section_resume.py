"""구간 다운로드를 이어서 처리할 때 넘기는 것 — 이전 실행이 끝낸 구간과 받아 둔 데이터 (#309).

구간 다운로드에서 일부 구간의 컷만 실패하면, 엔진은 끝낸 구간과 받아 둔 데이터를 이
모델에 담아 공유 데이터(``DownloadData.section_resume``)에 남긴다. 다운로드를 시작하는
쪽이 그것을 다음 실행의 ``Content.section_resume``으로 넘기면 엔진은 끝나지 않은 구간만
다시 처리한다.

끝낸 구간의 파일은 다음 실행이 다시 만들지 않는다. 그 파일이 그사이 지워졌거나 바뀌었으면
끝낸 것으로 치지 않는다 — 끝낸 구간마다 파일의 크기를 적어 두고(``done_sizes``), 다음 실행을
시작하는 쪽과 엔진이 쓰기 전에 파일을 확인한다(``with_files_checked``).

받아 둔 데이터는 이 모델이 가리킬 때만 쓴다 — 폴더에 남아 있다는 것만으로는 쓰지 않는다
(#190). 쓰기 전에 엔진이 다시 확인한다(세그먼트는 받은 세그먼트에 하는 것과 같은 검사,
mp4의 임시 원본은 크기와 머리의 바이트). 확인을 통과하지 못한 것은 다시 받는다.

복호화 키와 쿠키는 담지 않는다. 받아 둔 것(moov · 플레이리스트 · 프레임 정보)은 repr에
나오지 않는다 — 플레이리스트의 세그먼트 주소에는 요청마다 바뀌는 서명 값이 붙을 수 있고,
이 모델의 repr은 그것을 담은 Content의 repr을 따라 로그로 나갈 수 있다.
"""

import dataclasses
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from core.container.fmp4_index import Fmp4Head
from core.container.mp4_index import Mp4Head
from core.models.plan import TimeRange
from core.container.ts_index import TsHead


@dataclass(frozen=True)
class SectionResume:
    """이전 실행의 구간 목록 · 구간 파일 경로 · 끝낸 구간 · 받아 둔 데이터를 담는다.

    ``selections``와 ``paths``는 그 실행이 받은 것 그대로다. 다음 실행의 구간 목록이
    다르거나 끝낸 구간의 경로가 다르면 엔진은 이 모델을 쓰지 않고 처음부터 받는다.
    """

    selections: tuple[TimeRange, ...]  # 그 실행의 구간 목록
    paths: tuple[str, ...]  # 그 실행의 구간 파일 경로 — selections와 같은 순서
    done: frozenset[int]  # 끝낸 구간의 번호(0부터) — 그 구간의 파일을 만들었다
    # 끝낸 구간 파일의 크기 — (구간 번호, 바이트)의 묶음. 기록을 남기는 엔진이 적는다
    # (``sizes_of``). 다음 실행은 그 파일이 이 크기일 때만 끝낸 것으로 친다. 크기가 적히지 않은
    # 번호는 파일이 있는지만 본다
    done_sizes: tuple[tuple[int, int], ...] = ()
    # mp4: 구간을 정할 때 받은 moov. 넘기면 엔진이 다시 받지 않는다
    mp4_head: Mp4Head | None = field(default=None, repr=False)
    # mp4: 전송을 끝낸 임시 원본의 경로와 크기(바이트). 전송을 끝낸 실행만 적는다. 그 경로의
    # 파일이 이 크기이고 머리의 바이트가 같을 때만 다시 쓴다
    source_path: str | None = None
    source_size: int | None = None
    # mp4: 임시 원본이 범위를 담고 있는 구간의 번호(0부터). 끝나지 않은 구간이 모두 여기 들어
    # 있을 때만 그 임시 원본으로 자를 수 있다
    source_sections: frozenset[int] | None = None
    # 인코딩 전 다시보기: 플레이리스트 · 초기화 세그먼트 · 프레임 정보와, 받아 둔 세그먼트의
    # 폴더(``segment_dir``) · 목록(``stored``)
    fmp4_head: Fmp4Head | None = field(default=None, repr=False)
    # 암호화 VOD: 플레이리스트 · 프레임 정보와, 받아 둔 세그먼트(복호화한 것)의 폴더 · 목록.
    # 복호화 키는 들어 있지 않다
    ts_head: TsHead | None = field(default=None, repr=False)

    def fits(self, selections: tuple[TimeRange, ...], paths: tuple[str, ...]) -> bool:
        """이 모델을 그 구간 목록 · 경로의 실행에 쓸 수 있는지 — 구간이 같고 끝낸 구간의 경로가 같은지."""
        if tuple(selections) != self.selections or len(paths) != len(self.paths):
            return False
        return all(paths[number] == self.paths[number] for number in self.done)

    @staticmethod
    def sizes_of(paths: Sequence[str], done: Iterable[int]) -> tuple[tuple[int, int], ...]:
        """끝낸 구간 파일의 지금 크기 — ``done_sizes``에 적을 값. 읽지 못한 파일은 빠진다."""
        sizes = []
        for number in sorted(done):
            try:
                sizes.append((number, os.path.getsize(paths[number])))
            except OSError:
                continue
        return tuple(sizes)

    def with_files_checked(self) -> "SectionResume":
        """끝낸 구간 가운데 파일이 그대로인 것만 끝낸 것으로 남긴 기록을 돌려준다.

        그대로라는 것은 그 경로에 파일이 있고 크기가 적어 둔 값과 같다는 뜻이다. 사용자가 구간
        파일을 지웠거나 그 자리에 다른 파일이 놓였으면 그 구간은 끝낸 것이 아니다 — 다음
        실행이 다시 만든다. 다운로드를 시작하는 쪽이 구간 파일 이름을 예약하기 전에 부르고
        (바뀐 파일을 덮어쓰지 않고 새 이름을 받게), 엔진도 준비할 때 부른다. 모두 그대로면
        이 객체 그대로다.
        """
        sizes = dict(self.done_sizes)
        kept = set()
        for number in self.done:
            try:
                size = os.path.getsize(self.paths[number])
            except OSError:
                continue
            if sizes.get(number, size) == size:
                kept.add(number)
        if kept == self.done:
            return self
        return dataclasses.replace(
            self,
            done=frozenset(kept),
            done_sizes=tuple(pair for pair in self.done_sizes if pair[0] in kept),
        )
