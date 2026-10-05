"""구간 다운로드를 이어서 처리할 때 넘기는 것 — 이전 실행이 끝낸 구간과 받아 둔 데이터 (#309).

구간 다운로드에서 일부 구간의 컷만 실패하면, 엔진은 끝낸 구간과 받아 둔 데이터를 이
모델에 담아 공유 데이터(``DownloadData.section_resume``)에 남긴다. 다운로드를 시작하는
쪽이 그것을 다음 실행의 ``Content.section_resume``으로 넘기면 엔진은 끝나지 않은 구간만
다시 처리한다.

받아 둔 데이터는 이 모델이 가리킬 때만 쓴다 — 폴더에 남아 있다는 것만으로는 쓰지 않는다
(#190). 쓰기 전에 엔진이 다시 확인한다(세그먼트는 받은 세그먼트에 하는 것과 같은 검사,
mp4의 임시 원본은 크기와 머리의 바이트). 확인을 통과하지 못한 것은 다시 받는다.

복호화 키와 쿠키는 담지 않는다. 받아 둔 것(moov · 플레이리스트 · 프레임 정보)은 repr에
나오지 않는다 — 플레이리스트의 세그먼트 주소에는 요청마다 바뀌는 서명 값이 붙을 수 있고,
이 모델의 repr은 그것을 담은 Content의 repr을 따라 로그로 나갈 수 있다.
"""

from dataclasses import dataclass, field

from core.models.fmp4_index import Fmp4Head
from core.models.mp4_index import Mp4Head
from core.models.plan import TimeRange
from core.models.ts_index import TsHead


@dataclass(frozen=True)
class SectionResume:
    """이전 실행의 구간 목록 · 구간 파일 경로 · 끝낸 구간 · 받아 둔 데이터를 담는다.

    ``selections``와 ``paths``는 그 실행이 받은 것 그대로다. 다음 실행의 구간 목록이
    다르거나 끝낸 구간의 경로가 다르면 엔진은 이 모델을 쓰지 않고 처음부터 받는다.
    """

    selections: tuple[TimeRange, ...]  # 그 실행의 구간 목록
    paths: tuple[str, ...]  # 그 실행의 구간 파일 경로 — selections와 같은 순서
    done: frozenset[int]  # 끝낸 구간의 번호(0부터) — 그 구간의 파일을 만들었다
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
