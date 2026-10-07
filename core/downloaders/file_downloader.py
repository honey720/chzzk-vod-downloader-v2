"""파일(범위 분할) 다운로드 엔진 — BaseDownloader 하위 구현 (#73, #82, SPEC §5·§6).

공통 실행 엔진(워커 풀·스케일링·관측·재큐잉·일시정지)은
core/downloaders/base.py의 BaseDownloader로 이주했다(#82). 이 클래스에는
파일 경로 고유 부분만 남는다:

- prepare: HEAD로 총 크기 조회 → 해상도별 part_size 결정 → 바이트 범위 분할
  (ranges.py 사용)을 DownloadPlan으로 반환한다 (#83). 총 크기를 미리 알므로
  계획의 total_size를 채우고, ProgressEvent.total_size로 이어진다
- _download_part: 파트(바이트 구간) 단위 다운로드 — 저속 재시도·일시정지·
  중단 핸들링 포함. 재큐잉된 파트는 이미 받은 바이트 뒤에서 Range로
  이어받는다(#78 — 206이 아니면 처음부터 폴백). 규칙은
  tests/unit/core/test_file_downloader_rules.py가 박제한다
- postprocess: 전체 다운로드에는 없다 (베이스 기본 no-op). 구간 다운로드에서는
  받은 부분 파일을 구간마다 잘라 파일로 만든다

구간 다운로드 (#309) — ``Content.selections``가 비어 있지 않을 때:

- prepare: moov를 받아(fetch_mp4_head) 구간을 검증하고, 구간마다 실제 프레임과
  받을 바이트 범위를 정한다(selection_byte_ranges). 받을 항목은 구간 범위들이고
  겹치는 범위는 합친다. moov는 한 번만 받는다 — 해석에 쓴 바이트를 임시 원본의
  머리로 그대로 쓰고, ``Content.mp4_head``로 이미 받은 것이 오면 받지 않는다
- 받은 바이트는 원래 위치가 아니라 **빈틈없이 이어서** 임시 원본 파일에 쓴다
  (core/utils/mp4_partial.py). 원래 위치에 쓰면 사이의 빈 자리를 실제로 채우는 파일
  시스템(NTFS의 일반 파일, exFAT)에서 파일이 원본만큼 커진다
- postprocess: 구간마다 hybrid_cut으로 자른다. 하나라도 실패하면 다운로드
  전체가 실패다
- 임시 원본은 구간을 모두 만들면 지운다. 컷이 실패하면 남긴다(다시 받지 않게) —
  세그먼트 경로의 후처리 실패(#92)와 같은 규칙이다. 전송 실패·중단이면 전체
  다운로드의 산출물처럼 지운다
- 일부 구간의 컷만 실패하면 끝낸 구간과 임시 원본을 공유 데이터에 남긴다
  (``DownloadData.section_resume``). 그것을 ``Content.section_resume``으로 받은 실행은
  끝나지 않은 구간만 자른다 — 임시 원본이 남긴 크기 그대로 있으면 다시 받지 않고(요청 0건),
  아니면 moov를 새로 받아 끝나지 않은 구간의 범위만 다시 받는다(남겨 둔 moov로 범위를
  정하지 않는다 — 새로 받는 바이트는 지금의 파일의 것이다). 이전 실행의 것(끝낸 구간 파일 ·
  다시 쓴 임시 원본)은 이 실행이 실패 · 중단해도 지우지 않는다
- m3u8·hls_aes는 구간을 받지 않는다(베이스가 거부한다). clip도 받지 않는다

스레드 스케일링 기준 속도는 베이스 기본값(4 MB/s — 구 고정 임계 4/2와 동일)을
그대로 쓴다.
"""

import os
import time as tm

import requests

from core.api.mp4 import MP4_UNSUPPORTED, Mp4Error, fetch_mp4_head
from core.api.session import get_thread_session
from core.downloaders.base import (
    REQUEST_TIMEOUT,
    BaseDownloader,
    PostprocessError,
    TruncatedBodyError,
)
from core.downloaders.integrity import declared_content_length
from core.downloaders.ranges import decide_part_size, split_ranges, split_span
from core.models.content import Content, ContentType
from core.models.cut import CutResult, CutSection
from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from core.models.mp4_index import Mp4Index
from core.models.plan import DownloadPlan
from core.models.section_resume import SectionResume
from core.utils.hybrid_cut import CutError, cut_frames_from_mp4, hybrid_cut
from core.utils.mp4_partial import PartialLayout, build_head, plan_partial
from core.utils.mp4_ranges import selection_byte_ranges
from core.utils.paths import partial_source_path_for, release_output_paths
from core.utils.selections import SelectionError, validate_selections


class FileDownloader(BaseDownloader):
    """VOD 파일을 범위 분할 멀티스레드로 다운로드하는 엔진.

    호출 규약: 소유자(서비스·스크립트)가 DownloadTaskModel.start()로 RUNNING
    전이를 마친 뒤 run()을 호출한다. run()은 완료·중단·실패까지 블로킹한다.
    """

    run_thread_name = "DownloadThread"
    worker_pool_prefix = "DownloadWorker"
    supports_selections = True  # mp4는 moov로 구간의 바이트 범위를 정할 수 있다 (#309)
    postprocess_kind = "cut"  # 후처리는 구간 다운로드에만 있다 — 구간마다 자른다
    # OSError를 실패 처리에 추가한다 (#147 E1). 구 코드(요청 예외만)는 run
    # 스레드의 출력 파일 I/O 오류(이어받기 스캔·수신 준비 — 디스크 부족·
    # 마운트 해제)를 실패 처리 밖으로 흘려보냈다: 통지·로그·부분 산출물
    # 정리가 전부 생략된 채 스레드만 죽었다(#146 감사 실측). 엔진에서 잡는
    # 쪽이 _cleanup_partial까지 수행해 정리 품질이 높다. Exception 전체로
    # 넓히지 않는 것은 selections 명시 거부(NotImplementedError)의 전파를
    # 박제 계약대로 보존하기 위함이다 — 그 밖의 예상 밖 예외는 서비스의
    # 최후 방어선(_run_handle)이 실패로 환원한다.
    # Mp4Error·SelectionError는 구간 다운로드의 prepare가 내는 키 기반 예외다 (#309)
    _failure_exceptions = (requests.RequestException, OSError, Mp4Error, SelectionError)
    # 구간을 자를 때 조각마다 파라미터·패킷 수를 읽어 둘지 — 기본은 읽지 않는다.
    # 테스트가 True로 두고 cut_results를 정합 판정(check_cut)에 넘긴다. 조각을 한 번씩
    # 더 읽으므로 제품 경로에서는 켜지 않는다
    _inspect_cuts: bool = False

    def __init__(self, data, logger, **callbacks):
        super().__init__(data, logger, **callbacks)
        # 재큐잉된 파트가 이미 받아 쓴 바이트 수 — 재시도가 이어받는다 (#78).
        # (start, end) → 산출물에 쓰인 바이트 수. 완료·폴백 시 지운다
        self._part_progress: dict[tuple[int, int], int] = {}
        # 구간 다운로드의 상태 (#309) — prepare가 채운다. 전체 다운로드면 비어 있다
        self._sections: tuple[CutSection, ...] = ()
        self._index: Mp4Index | None = None  # 구간을 정할 때 받은 색인 — 컷이 프레임 정보로 쓴다
        self._layout: PartialLayout | None = None  # 받을 범위와 임시 원본 안의 위치
        self._head: bytes = b""  # 임시 원본의 머리(ftyp · 위치를 고친 moov · mdat 머리)
        self._source_path: str | None = None  # 임시 원본(받은 범위만 이어 쓴 mp4)
        self._made_sections: list[str] = []  # 이번 실행이 만든 구간 파일
        # 이전 실행이 끝내 이번 실행이 건너뛰는 구간의 번호(0부터) — Content.section_resume에서 온다
        self._done_before: frozenset[int] = frozenset()
        self._mp4_head = None  # 구간을 정할 때 쓴 moov — 일부 실패로 끝나면 다음 실행에 넘긴다
        # 이전 실행이 전송을 끝낸 임시 원본을 그대로 쓰는지 — 쓰면 받지 않고 지우지도 않는다
        self._reuses_source: bool = False
        # 임시 원본이 범위를 담고 있는 구간의 번호 — 일부 실패로 끝나면 다음 실행에 넘긴다
        self._source_sections: frozenset[int] = frozenset()
        self.cut_results: list[CutResult] = []  # 이번 실행이 자른 구간의 컷 결과 — 자른 순서

    @property
    def sections(self) -> tuple[CutSection, ...]:
        """구간 다운로드의 구간 목록 — 요청한 시각과 그것을 맞춘 프레임. prepare 뒤에 채워진다."""
        return self._sections

    @property
    def _target_path(self) -> str:
        """받은 바이트를 쓰는 파일 — 전체 다운로드는 산출물, 구간 다운로드는 임시 원본."""
        return self._source_path or self.s.output_path

    def _file_position(self, offset: int) -> int:
        """원본의 바이트 위치가 받는 파일에서 놓이는 위치 — 전체 다운로드는 그대로다."""
        if self._layout is None:
            return offset
        return self._layout.position(offset)

    @classmethod
    def supports(cls, content: Content) -> bool:
        """일반 VOD(video)와 클립(clip)을 처리한다 — 매니페스트가 파일 URL을 준다."""
        return content.content_type in (ContentType.CHZZK_VIDEO, ContentType.CHZZK_CLIP)

    # ============ 작업 목록·수신 준비 (구 run의 파일 고유 부분) ============

    def run(self) -> None:
        """다운로드를 실행하고, 끝나면 구간 파일명의 예약을 푼다 (#309)."""
        try:
            super().run()
        finally:
            release_output_paths(self.s.content.selection_paths)

    def prepare(self, content: Content) -> DownloadPlan:
        """총 크기를 조회하고 해상도별 part_size의 바이트 범위 계획을 만든다.

        구간이 있으면 moov를 받아 구간에 필요한 범위만 담은 계획을 만든다 (#309).
        """
        if content.selections:
            return self._prepare_sections(content)
        total_size = self._get_total_size()

        # part_size 결정(해상도별 가중 적용)
        self._part_size = decide_part_size(self.s.content_type, self.s.resolution)

        # 다운로드할 구간 분할 — 총 크기를 미리 아는 경로이므로 계획에 싣는다
        return DownloadPlan(
            items=tuple(split_ranges(total_size, self._part_size)),
            total_size=total_size,
        )

    def _release_after_run(self) -> None:
        """구간을 정할 때 쓴 moov와 색인, 임시 원본의 머리를 놓는다 (#309).

        긴 영상의 해석된 색인은 수백 MB~1GB다. 일부 구간만 실패했으면 다음 실행이 쓸 moov는
        이어받기 기록(공유 데이터의 section_resume)이 들고 있다 — 엔진이 따로 들 까닭이 없다.
        """
        self._mp4_head = None
        self._index = None
        self._head = b""

    def _prepare_note(self) -> str:
        """준비 단계의 로그에 moov를 넘겨받아 다시 썼는지(reused) 새로 받았는지(fetched)를 적는다."""
        reused = getattr(self, "_moov_reused", None)
        if reused is None:
            return ""  # 구간 다운로드가 아니다 — moov를 쓰지 않는다
        return "moov reused" if reused else "moov fetched"

    def _prepare_sections(self, content: Content) -> DownloadPlan:
        """구간 다운로드의 계획 — 구간마다의 바이트 범위를 받는다 (#309).

        moov는 ``content.mp4_head``가 있으면 그것을 쓰고 없으면 여기서 한 번 받는다.
        받을 항목에는 넣지 않는다 — 해석에 쓴 바이트로 임시 원본의 머리를 만든다.

        Raises:
            NotImplementedError: clip인 경우 — clip에는 구간 다운로드가 없다
            ValueError: 구간과 산출물 경로의 수가 다른 경우
            Mp4Error: moov를 읽지 못했거나 다룰 수 없는 배치인 경우
            SelectionError: 구간이 검증을 통과하지 못한 경우
        """
        if content.content_type is ContentType.CHZZK_CLIP:
            raise NotImplementedError("clip은 구간 선택 다운로드(selections)를 지원하지 않는다")
        if len(content.selection_paths) != len(content.selections):
            raise ValueError(
                f"구간 {len(content.selections)}개에 산출물 경로 {len(content.selection_paths)}개"
            )
        # 이전 실행이 만든 구간 파일은 이번 실행의 것이 아니다 — 이번 실행이 실패해도 지우지 않는다
        self._made_sections.clear()
        self.cut_results.clear()
        self.s.section_resume = None
        resume = content.section_resume
        if resume is not None and not resume.fits(content.selections, content.selection_paths):
            resume = None
        self._done_before = resume.done if resume is not None else frozenset()
        handed = (resume.mp4_head if resume is not None else None) or content.mp4_head
        head = handed or fetch_mp4_head(self.s.base_url)
        self._moov_reused = handed is not None
        index, picked = self._pick_ranges(head, content)
        source_path = partial_source_path_for(content.selection_paths[0])
        stored = self._stored_layout(resume, index, picked, head.data) if resume else None
        if resume is not None and stored is None and head is resume.mp4_head:
            # 임시 원본을 다시 쓸 수 없어 새로 받는다. 새로 받는 바이트는 지금의 파일의 것이다 —
            # 남겨 둔 moov가 아니라 지금의 moov로 범위를 정한다(그 사이 파일이 바뀌었을 수 있다)
            head = fetch_mp4_head(self.s.base_url)
            self._moov_reused = False
            index, picked = self._pick_ranges(head, content)
        self._reuses_source = stored is not None
        if stored is not None:
            # 이전 실행이 전송을 끝낸 임시 원본을 그대로 쓴다 — 받지 않는다
            layout = stored
            source_path = resume.source_path
            self._source_sections = resume.source_sections
        else:
            # 끝나지 않은 구간의 범위만 받는다. 이전 실행이 없으면 모든 구간이다
            self._source_sections = frozenset(range(len(picked))) - self._done_before
            layout = plan_partial(
                index,
                [
                    span
                    for number, item in enumerate(picked)
                    if number in self._source_sections
                    for span in item.ranges
                ],
            )
        self._head = build_head(head.data, layout, index.moov_range)
        self._mp4_head = head
        self._part_size = decide_part_size(self.s.content_type, self.s.resolution)
        self._index = index
        self._layout = layout
        self._sections = tuple(
            CutSection(selection, item.first_frame, item.last_frame, path)
            for selection, item, path in zip(content.selections, picked, content.selection_paths)
        )
        self._source_path = source_path
        self.s.sections_total = len(self._sections)
        self.s.sections_done = self.s.sections_resumed = len(self._done_before)
        self.s.sections_failed = 0
        return DownloadPlan(
            items=tuple(
                part
                for first, last in layout.ranges
                for part in split_span(first, last, self._part_size)
            ),
            total_size=layout.download_size,
            requires_postprocess=True,
            selections=tuple(content.selections),
        )

    def _download_start_log_args(self) -> tuple:
        return (self.s.total_size, self._part_size, self.s.total_ranges, self.s.adjust_threads)

    def _prepare_output(self) -> None:
        """빈 파일 생성(사이즈: 0). 구간 다운로드는 임시 원본에 머리를 먼저 써 둔다.

        이전 실행이 전송을 끝낸 임시 원본을 그대로 쓸 때는 건드리지 않는다 (#309).
        """
        if self._reuses_source:
            return
        with open(self._target_path, "wb") as f:
            f.write(self._head)

    def _initial_queue(self, items: list) -> list:
        """중단 이후 재시작 같은 상황을 고려해 미수신 구간만 큐에 넣는다."""
        if self._reuses_source:
            return []  # 이전 실행이 전송을 끝낸 임시 원본을 그대로 쓴다 — 받을 것이 없다
        if self._layout is not None:
            # 임시 원본은 위치가 원본과 달라 파일 크기로 미수신 구간을 가릴 수 없다 — 전부 받는다
            return list(items)
        return self._get_remaining_ranges(items)

    def _log_item_start(self, part_num: int, item) -> None:
        start, end = item
        self.logger.log_thread_start(part_num, start, end)

    def _download_item(self, item, part_num: int):
        start, end = item
        return self._download_part(start, end, part_num, self.s.total_size)

    def _cleanup_partial(self) -> None:
        """실패·중단 시 다운로드 파일 삭제. 구간 다운로드는 임시 원본과 이번에 만든 구간 파일을 지운다.

        이전 실행이 전송을 끝낸 임시 원본을 그대로 쓴 실행은 그 임시 원본을 지우지 않는다 —
        이번 실행이 만든 것만 지운다 (#309).
        """
        targets = [] if self._reuses_source else [self._target_path]
        for path in (*targets, *self._made_sections):
            if os.path.exists(path):
                os.remove(path)
        self._made_sections.clear()

    # ============ 구간 다운로드의 후처리 (#309) ============

    def postprocess(self) -> None:
        """임시 원본을 구간마다 잘라 파일로 만든다.

        구간 목록 순서대로 자른다. 자르지 못한 구간이 있어도 나머지 구간을 끝까지 자르고,
        그 뒤에 PostprocessError로 끝낸다 — 임시 원본과 만든 구간 파일은 남는다. 모두 만들면
        임시 원본을 지운다. 구간 사이에서 중단·일시정지를 확인한다(컷 하나는 중간에 멈추지
        않는다). 구간마다의 결과는 공유 데이터의 sections_done · sections_failed에 센다.

        이전 실행이 끝낸 구간(Content.section_resume)은 자르지 않는다. 자르지 못한 구간이
        있으면 끝낸 구간과 임시 원본을 공유 데이터의 section_resume에 남긴다.
        """
        self._on_merge_start()
        started = tm.perf_counter()
        try:
            frames = cut_frames_from_mp4(self._index)
        except (CutError, Mp4Error) as e:
            self.logger.log_error("Cut failed — partial source preserved for retry", e)
            raise PostprocessError(f"후처리(cut) 실패: {e}") from e
        # 색인에서 컷의 프레임 정보를 뽑는 데 걸린 시간 — 긴 영상은 샘플이 수백만 개다
        self._log_if_supported("log_cut_setup", tm.perf_counter() - started)
        failures: list[CutError] = []
        done = set(self._done_before)
        progress = self._track_cuts(
            {
                number: section.last_frame - section.first_frame + 1
                for number, section in enumerate(self._sections)
                if number not in self._done_before
            }
        )
        for number, section in enumerate(self._sections):
            if number in self._done_before:
                continue  # 이전 실행이 만든 구간 — 다시 만들지 않는다
            if self.state == DownloadState.PAUSED:
                self.s._pause_event.wait()
            if self.state == DownloadState.WAITING:
                return  # 정리(임시 원본·만든 구간 파일 삭제)는 run()의 중단 경로가 한다
            stages: list[tuple[str, float]] = []
            try:
                result = hybrid_cut(
                    self._source_path,
                    frames,
                    section.first_frame,
                    section.last_frame,
                    section.output_path,
                    inspect=self._inspect_cuts,
                    on_stage=lambda name, seconds: stages.append((name, seconds)),
                    on_progress=progress.section(number),
                )
            except (CutError, Mp4Error) as e:
                self.logger.log_error("Cut failed — partial source preserved for retry", e)
                failures.append(e)
                self.s.sections_failed += 1
            else:
                self.cut_results.append(result)
                self._made_sections.append(section.output_path)
                self.s.sections_done += 1
                done.add(number)
            self._log_cut_stages(number + 1, stages)
            progress.finish(number)
            self._on_progress(
                ProgressEvent(
                    downloaded_size=self.s.total_downloaded_size,
                    total_size=self._progress_total_size(),
                    speed=0.0,
                    active_threads=0,
                )
            )
        if failures:
            self.s.section_resume = SectionResume(
                selections=tuple(self.s.content.selections),
                paths=tuple(self.s.content.selection_paths),
                done=frozenset(done),
                mp4_head=self._mp4_head,
                source_path=self._source_path,
                source_size=os.path.getsize(self._source_path),
                source_sections=self._source_sections,
            )
            raise PostprocessError(
                f"후처리(cut) 실패: 구간 {len(failures)}개 — {failures[0]}"
            ) from failures[0]
        os.remove(self._source_path)

    @staticmethod
    def _pick_ranges(head, content: Content) -> tuple[Mp4Index, list]:
        """그 moov로 구간을 검증하고 구간마다 받을 바이트 범위를 정한다.

        Raises:
            SelectionError: 구간이 검증을 통과하지 못한 경우
            Mp4Error: moov가 파일 앞부분에 없는 경우
        """
        index = head.index
        violations = validate_selections(content.selections, index.duration, index.fps)
        if violations:
            raise SelectionError(violations)
        if head.data is None:
            raise Mp4Error(MP4_UNSUPPORTED, "moov가 파일 앞부분에 없다")
        return index, [selection_byte_ranges(index, selection) for selection in content.selections]

    def _stored_layout(
        self, resume: SectionResume, index: Mp4Index, picked: list, head_data: bytes
    ) -> PartialLayout | None:
        """이전 실행이 남긴 임시 원본을 그대로 쓸 수 있으면 그 배치를, 아니면 None을 돌려준다.

        쓸 수 있는 것은 그 임시 원본이 끝나지 않은 구간의 범위를 모두 담고 있고, 남긴 경로에
        남긴 크기 그대로 있고, 그 배치로 만든 머리와 파일의 앞부분이 같을 때다. 임시 원본에는
        세그먼트와 같은 내용 검사가 없다 — 전송을 끝낸 실행이 남긴 크기와 머리로 확인한다.
        """
        if resume.source_path is None or resume.source_sections is None:
            return None
        pending = set(range(len(picked))) - resume.done
        if not pending <= resume.source_sections:
            return None
        layout = plan_partial(
            index,
            [
                span
                for number, item in enumerate(picked)
                if number in resume.source_sections
                for span in item.ranges
            ],
        )
        head = build_head(head_data, layout, index.moov_range)
        try:
            if os.path.getsize(resume.source_path) != resume.source_size:
                return None
            if resume.source_size != len(head) + layout.download_size:
                return None
            with open(resume.source_path, "rb") as f:
                if f.read(len(head)) != head:
                    return None
        except OSError:
            return None
        return layout

    @staticmethod
    def _require_whole_file_on_200(response, file_size: int | None, range_start: int = 0) -> None:
        """범위 요청에 200이 왔을 때 그 본문을 써도 되는지 확인한다 (#309).

        200은 "범위를 무시하고 파일의 처음부터 보낸다"는 뜻이다. 본문은 파일의 0번째
        바이트부터인데 받는 쪽은 그것을 요청한 범위의 시작 위치에 쓴다.

        - 요청한 범위가 파일의 처음에서 시작하지 않으면 실패로 처리한다 — 그대로 쓰면 파일의
          앞부분이 그 파트 자리에 들어간 채 완료 처리된다
        - 요청한 범위만큼만 잘라 200으로 보내는 서버가 있다. 서버가 말한 길이
          (Content-Length)가 파일 전체 크기와 다르면 실패로 처리한다. 길이를 말하지 않은
          응답은 가릴 수 없어 지금처럼 둔다

        Args:
            response: 범위 요청의 응답
            file_size: 파일 전체 크기(바이트). 모르면 None — 그때는 길이를 말한 200을
                모두 실패로 처리한다(구간 다운로드: 받는 범위가 파일 전체일 수 없다)
            range_start: 요청한 범위가 시작하는 파일 위치(바이트)

        Raises:
            TruncatedBodyError: 200인데 범위가 파일의 처음에서 시작하지 않거나, 본문의 길이가
                파일 전체 크기와 다른 경우
        """
        if getattr(response, "status_code", None) != 200:
            return
        if range_start > 0:
            response.close()
            raise TruncatedBodyError(
                f"범위 요청(시작 {range_start})에 200 — 본문이 파일의 처음부터다"
            )
        declared = declared_content_length(getattr(response, "headers", None))
        if declared is not None and declared != file_size:
            response.close()
            raise TruncatedBodyError(
                f"범위 요청에 200 · 본문 {declared}바이트 · 파일 전체 {file_size}바이트"
            )

    def _postprocess_output_size(self) -> int:
        """후처리 종료 로그에 남길 크기 — 구간 파일 크기의 합."""
        return sum(os.path.getsize(path) for path in self._made_sections)

    # ============ 다운로드 동작 관련 메서드들 ============

    def _download_part(self, start: int, end: int, part_num: int, total_size: int):
        """
        파일의 특정 구간(start~end)을 다운로드하는 함수.
        속도가 느릴 경우 재시도 로직, 일시정지/중지 핸들링을 포함한다.

        재큐잉된 파트는 이미 받아 쓴 바이트 뒤에서 Range로 이어받는다 (#78).
        서버가 이어받기 Range를 존중하지 않으면(206이 아니면) 파트 처음부터
        다시 받는 폴백을 탄다. 판정 규칙(저속·실패 재큐잉)은 무변경이다.
        """
        slow_count = 0
        resume_offset = self._resume_offset(start, end)
        downloaded_size = 0
        if resume_offset > 0:
            # 이어받기 발생 사실과 오프셋을 남긴다 (#78 스모크 확인 수단) —
            # 이 줄 없는 재시도는 파트 처음부터 받은 것이다
            self.logger.log_part_resume(part_num, resume_offset, end - start + 1)
        while not self.state == DownloadState.WAITING:
            try:
                range_start = start + resume_offset
                headers = {"Range": f"bytes={range_start}-{end}"}
                # 스레드로컬 세션으로 같은 워커의 반복 요청 간 연결을 재사용한다 (#31)
                response = get_thread_session().get(
                    self.s.base_url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT
                )
                response.raise_for_status()
                if resume_offset > 0 and response.status_code != 206:
                    # 이어받기 Range가 거부됐다(200 전체 응답 등) — 기록을 버리고
                    # 파트 처음부터 받는다 (#78 폴백)
                    self.logger.warning(
                        f"Part {part_num} resume rejected "
                        f"(status {response.status_code}), retrying from start"
                    )
                    response.close()
                    self._part_progress.pop((start, end), None)
                    resume_offset = 0
                    continue
                # 구간 다운로드의 total_size는 받을 바이트의 합이지 파일 크기가 아니다
                self._require_whole_file_on_200(
                    response, total_size if self._layout is None else None, range_start
                )
                part_start_time = tm.time()
                # 디스크 쓰기 누적 시간 — 저속 판정에는 더 이상 반영하지 않는다(#191).
                # f.write()는 OS 페이지 캐시에 즉시 반환되는 버퍼드 쓰기라 실기
                # 로그(write=0.000s/0.494s=0%)로 기여도가 정확히 0%임을 확인했다 —
                # 뺄 게 없어 판정에 실질적 영향이 없었다. 그래도 계측·진단
                # 목적으로는 남긴다(느린 재큐가 실제로 디스크 탓인지 한눈에
                # 보려는 목적, #191 이슈 기록 참조) — 판정에는 관여하지 않으므로
                # tm.perf_counter() 측정 자체가 결과를 바꾸지 않는다
                write_elapsed = 0.0

                with open(self._target_path, "r+b") as f:
                    f.seek(self._file_position(range_start))
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.state == DownloadState.WAITING:
                            return part_num
                        if self.state == DownloadState.PAUSED:
                            self.s._pause_event.wait()

                        if chunk:
                            write_start = tm.perf_counter()
                            f.write(chunk)
                            write_elapsed += tm.perf_counter() - write_start
                            downloaded_size += len(chunk)
                            elapsed = tm.time() - part_start_time

                            if elapsed > 0:
                                # 속도 판정은 이번 시도가 받은 바이트 기준,
                                # 진행 표시는 이어받은 바이트를 포함한다 (#78)
                                speed_kb_s = downloaded_size / elapsed / 1024
                                self._check_speed_and_update_progress(
                                    part_num,
                                    resume_offset + downloaded_size,
                                    total_size,
                                    speed_kb_s,
                                )
                                if speed_kb_s < self._slow_speed_threshold_kb_s:
                                    slow_count += 1
                                    if slow_count > 5:
                                        # 속도가 너무 느리면 스레드 재시작
                                        ratio = (
                                            write_elapsed / elapsed * 100 if elapsed > 0 else 0.0
                                        )
                                        diagnostic = f"write={write_elapsed:.3f}s/{elapsed:.3f}s={ratio:.0f}%"
                                        with self.lock:
                                            self._record_partial(
                                                start, end, resume_offset + downloaded_size
                                            )
                                            self._requeue_slow(
                                                (start, end), part_num, diagnostic=diagnostic
                                            )
                                        return part_num
                                else:
                                    slow_count = 0

                            if downloaded_size >= (end - range_start + 1):
                                break

                # 성공적으로 마무리된 경우
                with self.lock:
                    self.s.completed_threads += 1
                    self.s.completed_progress += resume_offset + downloaded_size
                    self.s.threads_progress[part_num] = 0
                self._part_progress.pop((start, end), None)
                self.logger.log_thread_complete(part_num, resume_offset + downloaded_size)
                return part_num

            except (requests.RequestException, requests.Timeout) as e:
                with self.lock:
                    self._record_partial(start, end, resume_offset + downloaded_size)
                    self._requeue_failed((start, end), part_num, e)
                self.logger.log_error(f"Part {part_num} download failed", e)
                return part_num

    def _record_partial(self, start: int, end: int, downloaded: int) -> None:
        """재큐잉 직전까지 산출물에 받아 쓴 바이트 수를 기록한다 (#78)."""
        if downloaded > 0:
            self._part_progress[(start, end)] = downloaded
        else:
            self._part_progress.pop((start, end), None)

    def _resume_offset(self, start: int, end: int) -> int:
        """파트의 이어받기 오프셋을 반환한다. 확신이 없으면 0(처음부터)이다 (#78).

        무결성 확인: 산출물 파일이 기록된 오프셋(start+기록)까지는 실제로
        자라 있어야 한다 — 못 미치면 쓰기가 유실된 것이므로 기록을 버린다.
        """
        recorded = self._part_progress.get((start, end), 0)
        if recorded <= 0:
            return 0
        try:
            file_size = os.path.getsize(self._target_path)
        except OSError:
            file_size = -1
        if file_size < self._file_position(start) + recorded:
            self._part_progress.pop((start, end), None)
            return 0
        return recorded

    # ============ 유틸 메서드 ============

    def _get_total_size(
        self,
    ) -> int:  #: TODO: 컨텐츠 아이템에 content-length 추가(중복된 로직 제거)
        """
        HEAD 요청으로 total_size를 구한다.
        """
        response = get_thread_session().head(self.s.base_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        size = int(response.headers.get("content-length", 0))
        if size == 0:
            resp = get_thread_session().get(self.s.base_url, stream=True, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            size = int(resp.headers.get("content-length", 0))
            resp.close()
        return size

    def _get_remaining_ranges(self, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """
        중단 이후 재시작 같은 상황 고려(현재 파일크기 등을 바탕으로),
        아직 다운로드되지 않은 구간만 남겨 반환한다.
        """
        with open(self._target_path, "r+b") as f:
            f.seek(0, 2)
            file_size = f.tell()

        remaining = []
        for start, end in ranges:
            if start >= file_size or end >= file_size:
                remaining.append((start, end))
        return remaining
