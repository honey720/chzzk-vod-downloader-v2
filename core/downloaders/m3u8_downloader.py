"""m3u8(세그먼트 분할) 다운로드 엔진 — BaseDownloader 하위 구현 (#74, #82, SPEC §5·§6).

공통 실행 엔진(워커 풀·스케일링·관측·재큐잉·일시정지)은
core/downloaders/base.py의 BaseDownloader로 이주했다(#82). 이 클래스에는
m3u8 고유 부분만 남는다:

- prepare: 플레이리스트를 받아 세그먼트 목록의 DownloadPlan 생성 (#83 —
  총 크기 미상이라 total_size=None, 병합이 필요해 requires_postprocess=True).
  base_url은 m3u8
  플레이리스트 URL이며 **호출 전에 해석이 끝나 있어야 한다** —
  requires_base_url_resolution=True로 서비스가 주입된 resolver를 시작
  시점에 실행한다 (쿠키·치지직 API 조회는 아직 앱 영역)
- _prepare_output: 임시 폴더 재생성 + EXT-X-MAP 초기화 세그먼트 다운로드
- _download_segment: 세그먼트 단위 다운로드 — 저속 재시도·일시정지·중단
  핸들링 포함. 규칙은 tests/unit/core/test_m3u8_downloader_rules.py가 박제한다
- postprocess: 세그먼트를 인덱스 순서로 ffmpeg stdin에 흘리는 단일 패스
  재포장 (#88·#92) — 시작은 on_merge_start 콜백으로 알리고, 실패 시 폴백
  없이 명확히 실패한다 (세그먼트 보존)
- 전체 크기를 미리 알 수 없어 ProgressEvent.total_size는 None이다.
  진행률 계산(세그먼트 수 기반)은 어댑터의 몫이다.

구간 다운로드 (#309) — ``Content.selections``가 비어 있지 않을 때:

- prepare: 플레이리스트와 초기화 세그먼트, 그리고 구간의 양 끝이 든 세그먼트를 받아
  (core/api/hls_fmp4.py) 구간마다 받을 세그먼트와 첫·끝 프레임을 정한다
  (core/utils/fmp4_sections.py). 받을 항목은 구간들의 세그먼트를 합친 것이다 — 두
  구간이 같은 세그먼트를 쓰면 한 번만 받는다. ``Content.fmp4_head``로 이미 받은 것이
  오면 그것을 쓰고, 초기화 세그먼트는 다시 받지 않는다
- **세그먼트에 범위 요청을 보내지 않는다.** 프레임 정보를 읽을 세그먼트도 통째로 받아
  임시 폴더에 두고(``Fmp4Head.segment_dir`` · ``stored``), 전송 단계는 그 파일이 온전하면
  다시 받지 않는다. 구간을 해석한 쪽이 넘긴 ``Fmp4Head``에 폴더가 적혀 있으면 그 폴더가
  이 다운로드의 임시 폴더다
- 받은 세그먼트가 온전한지는 전체 다운로드와 같은 검사(core/downloaders/integrity.py,
  #321)로 확인한다. 구간을 정하면서 받아 둔 세그먼트도 같은 검사를 거치고, 온전하지
  않으면 전송 단계가 다시 받는다. 잘린 세그먼트로 컷을 하지 않는다
- postprocess: 구간마다 초기화 세그먼트 + 그 구간의 세그먼트를 순서대로 이은 임시
  fMP4를 만들고 hybrid_cut으로 자른다. 하나라도 실패하면 다운로드 전체가 실패다.
  세그먼트는 통째로 읽지 않는다 — moof만 골라 읽고 본문은 고정 크기 버퍼로 옮긴다
- 임시 폴더(받은 세그먼트 · 이은 파일)는 구간을 모두 만들면 지운다. 컷이 실패하면
  남긴다 — 후처리 실패가 세그먼트를 남기는 것(#92)과 같은 규칙이다
- 전체 다운로드(구간 없음)의 경로는 그대로다
"""

import os
import shutil
import time as tm
from bisect import bisect_left
from urllib.parse import urljoin

import requests

import core.downloaders.base as base_module
import core.downloaders.integrity as integrity
from core.api.fmp4 import build_fmp4_index, fmp4_origin
from core.api.hls_fmp4 import (
    fetch_fmp4_head,
    read_segment_file,
    segment_file_name,
    segment_frames,
)
from core.api.mp4 import Mp4Error
from core.api.session import get_thread_session
from core.downloaders.base import REQUEST_TIMEOUT, BaseDownloader, PostprocessError
from core.models.content import Content, ContentType
from core.models.cut import CutFrames, CutResult
from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from core.models.fmp4_index import Fmp4Head
from core.models.plan import DownloadPlan
from core.utils.fmp4_sections import Fmp4Section, plan_fmp4_sections
from core.utils.hybrid_cut import CUT_FAILED, CutError, cut_frames_from_fmp4, hybrid_cut
from core.utils.paths import choose_temp_dir, release_output_paths

# 받은 세그먼트의 프레임 PTS를 prepare가 정한 PTS와 견줄 때 허용하는 차이(초) — 같은
# 계산을 두 번 한 값이라 같아야 하고, float 오차만 흡수한다
_PTS_TOLERANCE = 1e-6

# 받은 세그먼트를 구간의 임시 파일로 이어 쓸 때 한 번에 옮기는 크기(바이트) — 1MB.
# 세그먼트를 통째로 메모리에 올리지 않는다
_JOIN_CHUNK_BYTES = 1024 * 1024


# 초기화 세그먼트를 다시 받게 하는 예외 — 본문이 잘려 왔다는 뜻인 것만 (#321).
# ChunkedEncodingError는 본문이 선언된 길이보다 먼저 끝났을 때, ContentDecodingError는
# 압축된 본문이 중간에 끊겨 풀리지 않을 때 난다
_INIT_TRUNCATION_ERRORS = (
    integrity.TruncatedSegmentError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)


class M3U8Downloader(BaseDownloader):
    """m3u8 VOD를 세그먼트 단위 멀티스레드로 다운로드·병합하는 엔진.

    호출 규약: 소유자(서비스·스크립트)가 data.base_url에 m3u8 플레이리스트 URL을
    채우고 DownloadTaskModel.start()로 RUNNING 전이를 마친 뒤 run()을 호출한다.
    run()은 완료·중단·실패까지 블로킹한다.
    """

    run_thread_name = "DownloadM3U8Thread"
    worker_pool_prefix = "DownloadM3U8Worker"
    requires_base_url_resolution = True
    supports_selections = True  # fMP4는 세그먼트의 moof로 구간의 프레임을 정할 수 있다 (#309)
    # 구 코드와 동일하게 모든 예외를 실패로 처리한다 (플레이리스트 파싱 실패 포함)
    _failure_exceptions = (Exception,)
    # 구간을 자를 때 조각마다 파라미터·패킷 수를 읽어 둘지 — 기본은 읽지 않는다.
    # 테스트가 True로 두고 cut_results를 정합 판정(check_cut)에 넘긴다
    _inspect_cuts: bool = False

    def __init__(self, data, logger, **callbacks):
        super().__init__(data, logger, **callbacks)
        # 세그먼트 저장용 임시 폴더 경로 (실패 정리 경로가 참조하므로 실행 전에 확정).
        # 산출물 파일명에서 파생해 다운로드 간 폴더 공유·상호 삭제를 막는다 (#105).
        # 조건이 맞으면 시스템 임시 폴더(로컬 매체)로 보낸다 — 산출물 폴더가
        # 느린 매체(exFAT SD 카드 등)일 때 읽기·쓰기 경쟁과 AppleDouble
        # 오버헤드를 우회한다(#192). 산출물 위치 자체는 그대로다.
        # 구간 다운로드는 첫 구간 파일의 이름에서 파생한다 — output_path는 쓰지 않는 이름이라
        # 같은 영상의 구간 다운로드 둘이 겹칠 수 있다. 구간 파일 이름은 배정할 때 갈라져 있다
        section_paths = self.s.content.selection_paths
        handed = self.s.content.fmp4_head
        if handed is not None and handed.segment_dir is not None:
            # 구간을 해석한 쪽이 세그먼트를 받아 둔 폴더 — 그 폴더를 그대로 쓴다
            self.temp_dir = handed.segment_dir
        else:
            self.temp_dir = choose_temp_dir(
                section_paths[0] if section_paths else self.s.output_path
            )
        # 구간 다운로드의 상태 (#309) — prepare가 채운다. 전체 다운로드면 비어 있다
        self._sections: tuple[Fmp4Section, ...] = ()
        # 구간을 정할 때 받은 플레이리스트·초기화 세그먼트·세그먼트의 프레임 정보
        self._head: Fmp4Head | None = None
        # 구간을 정하면서 이미 온전하게 받아 둔 세그먼트 — 인덱스 → 파일 크기. 다시 받지 않는다
        self._prefetched: dict[int, int] = {}
        self._made_sections: list[str] = []  # 이번 실행이 만든 구간 파일
        self.cut_results: list[CutResult] = []  # 구간마다의 컷 결과 — sections와 같은 순서
        self.cut_frames: list[CutFrames] = []  # 구간마다 컷에 쓴 프레임 정보 — 판정이 쓴다

    @property
    def sections(self) -> tuple[Fmp4Section, ...]:
        """구간 다운로드의 구간 목록 — 받을 세그먼트와 첫·끝 프레임의 시각. prepare 뒤에 채워진다."""
        return self._sections

    @classmethod
    def supports(cls, content: Content) -> bool:
        """라이브 다시보기(m3u8) VOD를 처리한다."""
        return content.content_type is ContentType.CHZZK_VIDEO_M3U8

    # ============ 작업 목록·수신 준비 (구 run의 m3u8 고유 부분) ============

    def run(self) -> None:
        """다운로드를 실행하고, 끝나면 구간 파일명의 예약을 푼다 (#309)."""
        try:
            super().run()
        finally:
            release_output_paths(self.s.content.selection_paths)

    def prepare(self, content: Content) -> DownloadPlan:
        """플레이리스트를 받아 (index, 세그먼트) 목록의 계획을 만든다.

        구간이 있으면 구간에 필요한 세그먼트만 담은 계획을 만든다 (#309).
        """
        if content.selections:
            return self._prepare_sections(content)
        response = get_thread_session().get(self.s.base_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        lines = response.text.splitlines()
        segments = [line for line in lines if line and not line.startswith("#")]
        # EXT-X-MAP(초기화 세그먼트) 추출을 위해 _prepare_output이 참조한다
        self._playlist_lines = lines
        self.s.merged_segments = 0
        # 세그먼트 임시 파일명 0채움 자릿수 — sorted() 병합 순서의 전제
        self.width = len(str(len(segments)))
        # 총 바이트 크기는 미리 알 수 없고(total_size=None), 병합 후처리가 필요하다
        return DownloadPlan(
            items=tuple(enumerate(segments)),
            total_size=None,
            requires_postprocess=True,
        )

    def _prepare_sections(self, content: Content) -> DownloadPlan:
        """구간 다운로드의 계획 — 구간들의 세그먼트를 합친 것을 받는다 (#309).

        플레이리스트·초기화 세그먼트·세그먼트의 프레임 정보는 ``content.fmp4_head``가 있으면
        그것을 쓰고, 없는 것만 여기서 받는다. 프레임 정보를 읽으려고 받는 세그먼트는 임시
        폴더에 둔다 — 전송 단계가 다시 받지 않는다.

        Raises:
            ValueError: 구간과 산출물 경로의 수가 다른 경우
            Mp4Error: fMP4가 아니거나 초기화 세그먼트·세그먼트를 해석하지 못한 경우
            SelectionError: 구간이 검증을 통과하지 못했거나, 녹화가 끊긴 자리를 넘거나,
                구간의 시각이 놓인 세그먼트를 찾지 못한 경우
        """
        if len(content.selection_paths) != len(content.selections):
            raise ValueError(
                f"구간 {len(content.selections)}개에 산출물 경로 {len(content.selection_paths)}개"
            )
        head = content.fmp4_head
        if head is not None and head.segment_dir is not None:
            # 구간을 해석한 쪽이 세그먼트를 받아 둔 폴더 — 엔진을 만든 뒤에 넘겨받았을 수 있다
            self.temp_dir = head.segment_dir
        else:
            # 받아 둔 세그먼트가 없다 — 임시 폴더를 비우고 여기서 받는 것을 그 폴더에 둔다
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            if head is None:
                head = fetch_fmp4_head(self.s.base_url, self.temp_dir)
            else:
                head.segment_dir = self.temp_dir
        sections = plan_fmp4_sections(
            head.playlist,
            head.init,
            content.selections,
            lambda index: segment_frames(head, self.s.base_url, index),
            head.frame_rate,
        )
        # 두 구간이 같은 세그먼트를 쓰면 한 번만 받는다
        wanted = sorted(
            {index for s in sections for index in range(s.first_segment, s.last_segment + 1)}
        )
        self._head = head
        self._sections = sections
        self.s.sections_total = len(sections)
        self.s.sections_done = self.s.sections_failed = 0
        self._prefetched = self._whole_stored_segments(head, wanted)
        self.postprocess_kind = "cut"  # 구간마다 자른다 — 전체 다운로드의 remux와 구분한다
        self.s.merged_segments = 0
        self.width = len(str(len(head.playlist.segments)))
        return DownloadPlan(
            items=tuple((index, head.playlist.segments[index]) for index in wanted),
            total_size=None,
            requires_postprocess=True,
            selections=tuple(content.selections),
        )

    def _whole_stored_segments(self, head: Fmp4Head, wanted: list[int]) -> dict[int, int]:
        """wanted 가운데 임시 폴더에 온전하게 받아 둔 세그먼트 — 인덱스 → 파일 크기.

        ``head.stored``에 적혀 있고, 파일이 있고, 전송 단계가 받은 세그먼트에 하는 것과 같은
        검사(integrity.check_fmp4_media_segment)를 통과한 것만이다. 하나라도 아니면 그
        세그먼트는 전송 단계가 다시 받는다 — 구간을 해석한 쪽이 받아 둔 파일을 믿지 않는다.
        """
        found = {}
        for index in wanted:
            if index not in head.stored:
                continue
            path = os.path.join(
                self.temp_dir, segment_file_name(len(head.playlist.segments), index)
            )
            try:
                size = os.path.getsize(path)
                with open(path, "rb") as f:
                    integrity.check_fmp4_media_segment(f, size)
            except (integrity.TruncatedSegmentError, OSError):
                continue
            found[index] = size
        return found

    def _download_start_log_args(self) -> tuple:
        # m3u8은 전체 크기·파트 크기를 미리 알 수 없다 (구 코드와 동일하게 0)
        return (0, 0, self.s.total_ranges, self.s.adjust_threads)

    def _prepare_output(self) -> None:
        """임시 폴더를 만들고 초기화 세그먼트(EXT-X-MAP)를 받아 온전한지 확인한다 (#321).

        구간 다운로드는 초기화 세그먼트를 다시 받지 않는다 — 구간을 정할 때 받아 확인한
        것을 쓴다(_prepare_section_output, #309).

        초기화 세그먼트가 잘려 받아졌으면 다시 받는다. 세그먼트의 일시 오류와 같은 횟수
        (base의 _TRANSIENT_ERROR_REQUEUE_LIMIT)까지 다시 받고, 그래도 안 되면 마지막 예외를
        그대로 던져 실패시킨다 — 잘린 초기화 세그먼트로는 영상 전체를 읽을 수 없다.

        다시 받는 것은 잘림 계열의 예외뿐이다(_INIT_TRUNCATION_ERRORS). 본문이 선언된
        길이보다 짧으면 구조 검사에 닿기 전에 본문을 읽는 단계에서 요청 예외가 난다 —
        그것도 잘린 것이다. 연결 실패 · 타임아웃 같은 그 밖의 네트워크 오류는 준비 단계의
        다른 요청과 같이 다시 받지 않고 그대로 실패한다(#320).

        예외의 종류는 바꾸지 않는다. 본문을 읽다 난 요청 예외는 연결이 끊겨도 나므로
        잘린 것으로 단정할 수 없다 — 잘림 실패(TruncatedSegmentError)는 내용 검사로
        확인된 경우에만 나간다. 미디어 세그먼트의 재큐와 같은 규칙이다.

        Raises:
            TruncatedSegmentError: 다시 받아도 초기화 세그먼트의 구조가 계속 온전하지 않을 때
            requests.RequestException: 다시 받아도 본문을 끝까지 읽지 못할 때
        """
        if self._head is not None:
            self._prepare_section_output(self._head)
            return
        init_segment_path = os.path.join(self.temp_dir, f"{0:0{self.width}d}.m4s")
        retries = base_module._TRANSIENT_ERROR_REQUEUE_LIMIT
        for attempt in range(retries + 1):
            try:
                self._receive_init_segment()
                with open(init_segment_path, "rb") as f:
                    integrity.check_fmp4_init_segment(f.read())
                return
            except _INIT_TRUNCATION_ERRORS as e:
                if attempt == retries:
                    raise
                self.logger.log_error("Init segment incomplete — retrying", e)

    def _prepare_section_output(self, head: Fmp4Head) -> None:
        """구간 다운로드의 임시 폴더를 준비한다 — 받아 둔 세그먼트는 남기고 초기화 세그먼트를 쓴다.

        폴더를 통째로 지우지 않는다 — 구간을 정하면서 받아 둔 세그먼트가 들어 있다. 온전한
        것으로 확인된 세그먼트(_prefetched) 말고 남아 있는 것(이전 실행의 것, 온전하지 않은
        것)만 지운다. 지운 세그먼트는 전송 단계가 다시 받는다.
        """
        os.makedirs(self.temp_dir, exist_ok=True)
        count = len(head.playlist.segments)
        kept = {segment_file_name(count, index) for index in self._prefetched}
        for name in os.listdir(self.temp_dir):
            if name not in kept:
                stale = os.path.join(self.temp_dir, name)
                shutil.rmtree(stale) if os.path.isdir(stale) else os.remove(stale)
        # 초기화 세그먼트는 구간을 정할 때 이미 받았다
        with open(self._init_path(), "wb") as f:
            f.write(head.init_data)

    def _receive_init_segment(self) -> None:
        """임시 폴더를 재생성하고 초기화 세그먼트(EXT-X-MAP)를 받는다."""
        # 세그먼트 저장용 임시 폴더가 있다면 내용 포함 삭제 후 재생성
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        os.makedirs(self.temp_dir)

        init_segment = None
        for line in self._playlist_lines:
            if line.startswith("#EXT-X-MAP:"):
                init_segment = line.split("URI=")[1].strip('"')
        init_url = urljoin(self.s.base_url, init_segment)
        init_segment_path = os.path.join(self.temp_dir, f"{0:0{self.width}d}.m4s")
        # 초기화 세그먼트 다운로드
        with open(init_segment_path, "wb") as f:
            f.write(get_thread_session().get(init_url, timeout=REQUEST_TIMEOUT).content)

    def _log_item_start(self, part_num: int, item) -> None:
        _index, segment = item
        self.logger.log_m3u8_thread_start(part_num, segment)

    def _download_item(self, item, part_num: int):
        index, segment = item
        return self._download_segment(
            index=index, segment=segment, part_num=part_num, total_ranges=self.s.total_ranges
        )

    def _cleanup_partial(self) -> None:
        """실패·중단 시 임시 폴더와 이번 실행이 만든 파일을 지운다.

        구간 다운로드는 output_path에 쓰지 않는다 — 그 자리의 파일은 이 실행이 만든 것이
        아니므로(같은 영상의 전체 다운로드일 수 있다) 지우지 않고, 이번에 만든 구간 파일만
        지운다 (#309).
        """
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        made = self._made_sections if self.s.content.selections else [self.s.output_path]
        for path in made:
            if os.path.exists(path):
                os.remove(path)
        self._made_sections.clear()

    def _cleanup_after_run(self) -> None:
        """임시 폴더 삭제 (구 run의 (5) — 병합 후에는 빈 폴더만 남는다)."""
        shutil.rmtree(self.temp_dir)

    def _progress_total_size(self) -> int | None:
        # 전체 바이트 크기를 미리 알 수 없다 — 세그먼트 수 기반 계산은 어댑터가 한다
        return None

    # ============ 후처리: 순서 보장 병합 (구 run의 (4)) ============

    def postprocess(self) -> None:
        """세그먼트들을 인덱스 순서 그대로 ffmpeg stdin에 흘려 mp4로 재포장한다 (#88·#92).

        바이트 연결만으로는 fMP4 조각 구조(전역 인덱스 없음·라이브 타임라인
        보유)가 그대로라 편집 프로그램이 읽지 못한다. #92부터 중간 병합 파일
        없이 단일 패스로 재포장하며, 실패 시 폴백 없이 명확히 실패하고
        세그먼트를 보존한다 (규칙은 base의 _remux_streamed 참조).

        구간 다운로드는 재포장하지 않고 구간마다 잘라 파일로 만든다 (#309).
        """
        self._on_merge_start()
        if self._sections:
            self._cut_sections()
            return
        segment_files = self._list_segment_files((".m4s", ".m4v"))
        self._remux_streamed([os.path.join(self.temp_dir, f) for f in segment_files])

    # ============ 구간 다운로드의 후처리 (#309) ============

    def _init_path(self) -> str:
        """임시 폴더 안 초기화 세그먼트의 경로."""
        return os.path.join(self.temp_dir, f"{0:0{self.width}d}.m4s")

    def _segment_path(self, index: int) -> str:
        """임시 폴더 안 index번째 세그먼트의 경로 — _download_segment가 쓰는 이름과 같다."""
        return os.path.join(self.temp_dir, f"{index + 1:0{self.width}d}.m4v")

    def _cut_sections(self) -> None:
        """구간마다 초기화 세그먼트 + 그 구간의 세그먼트를 이은 임시 fMP4를 만들고 자른다.

        구간 목록 순서대로 자른다. 자르지 못한 구간이 있어도 나머지 구간을 끝까지 자르고,
        그 뒤에 PostprocessError로 끝낸다 — 임시 폴더(받은 세그먼트)와 만든 구간 파일은
        남는다. 이은 파일은 그 구간의 컷이 끝나면(성공이든 실패든) 바로 지운다 — 받은
        세그먼트에서 다시 만들 수 있고, 구간이 여럿 실패해도 쌓이지 않는다. 구간마다의
        결과는 공유 데이터의 sections_done · sections_failed에 센다.
        구간 사이에서 중단·일시정지를 확인한다(컷 하나는 중간에 멈추지 않는다).
        """
        head = self._head
        paths = self.s.content.selection_paths
        failures: list[Exception] = []
        for number, (section, output_path) in enumerate(zip(self._sections, paths), start=1):
            if self.state == DownloadState.PAUSED:
                self.s._pause_event.wait()
            if self.state == DownloadState.WAITING:
                return  # 정리(임시 폴더·만든 구간 파일 삭제)는 run()의 중단 경로가 한다
            joined = os.path.join(self.temp_dir, f"section_{number}.mp4")
            try:
                parsed = []
                with open(joined, "wb") as out:
                    out.write(head.init_data)
                    for index in range(section.first_segment, section.last_segment + 1):
                        # 프레임 정보는 moof만 골라 읽고, 본문은 버퍼 크기만큼씩 옮긴다.
                        # 파일이 온전한지는 받을 때 확인했다
                        parsed.append(read_segment_file(self._segment_path(index), head.init))
                        with open(self._segment_path(index), "rb") as f:
                            shutil.copyfileobj(f, out, _JOIN_CHUNK_BYTES)
                index = build_fmp4_index(head.init, parsed, section.origin)
                # 이은 파일은 VOD의 중간에서 시작한다 — ffmpeg의 -ss는 파일의 시작부터 센다
                input_start = float(fmp4_origin(head.init, parsed[0]) - section.origin)
                frames = cut_frames_from_fmp4(head.init, parsed, index, input_start)
                result = hybrid_cut(
                    joined,
                    frames,
                    _frame_at(frames, section.first_pts),
                    _frame_at(frames, section.last_pts),
                    output_path,
                    inspect=self._inspect_cuts,
                )
            except (CutError, Mp4Error) as e:
                # 이 구간은 자르지 못했다 — 나머지 구간은 끝까지 자른다
                self.logger.log_error("Cut failed — segments preserved for retry", e)
                failures.append(e)
                self.s.sections_failed += 1
            else:
                self.cut_results.append(result)
                self.cut_frames.append(frames)
                self._made_sections.append(output_path)
                self.s.sections_done += 1
            finally:
                if os.path.exists(joined):
                    os.remove(joined)
            # 병합 진행(세그먼트 수 기반)을 구간 수에 비례해 올린다 — 어댑터의 분모는
            # 받은 세그먼트 수 + 초기화 세그먼트다
            self.s.merged_segments = (self.s.max_threads + 1) * number // len(self._sections)
            self._on_progress(
                ProgressEvent(
                    downloaded_size=self.s.total_downloaded_size,
                    total_size=self._progress_total_size(),
                    speed=0.0,
                    active_threads=0,
                )
            )
        if failures:
            raise PostprocessError(
                f"후처리(cut) 실패: 구간 {len(failures)}개 — {failures[0]}"
            ) from failures[0]

    def _postprocess_output_size(self) -> int:
        """후처리 종료 로그에 남길 크기 — 구간 다운로드는 구간 파일 크기의 합."""
        if self._sections:
            return sum(os.path.getsize(path) for path in self._made_sections)
        return super()._postprocess_output_size()

    # ============ 다운로드 동작 관련 메서드들 ============

    def _download_segment(self, index: int, segment: str, part_num: int, total_ranges: int):
        """
        개별 세그먼트 다운로드(재시도 포함)
        """
        if index in self._prefetched:
            # 구간을 정하면서 이미 온전하게 받아 둔 세그먼트 — 다시 받지 않는다 (#309)
            size = self._prefetched[index]
            with self.lock:
                self.s.completed_threads += 1
                self.s.completed_progress += size
                self.s.threads_progress[part_num] = 0
            self.logger.log_thread_complete(part_num, size)
            return part_num

        slow_count = 0
        downloaded_size = 0
        segment_url = urljoin(self.s.base_url, segment)
        while not self.state == DownloadState.WAITING:
            try:
                # 스레드로컬 세션으로 같은 워커의 세그먼트 요청 간 연결을 재사용한다 (#31)
                response = get_thread_session().get(
                    segment_url, stream=True, timeout=REQUEST_TIMEOUT
                )
                response.raise_for_status()
                part_start_time = tm.time()
                # 디스크 쓰기 누적 시간 — 저속 판정에는 더 이상 반영하지 않는다(#191).
                # f.write()는 OS 페이지 캐시에 즉시 반환되는 버퍼드 쓰기라 실기
                # 로그(write=0.000s/0.494s=0%)로 기여도가 정확히 0%임을 확인했다 —
                # 뺄 게 없어 판정에 실질적 영향이 없었다. 계측·진단 목적으로만
                # 남긴다(#191 이슈 기록 참조) — 판정에는 관여하지 않는다
                write_elapsed = 0.0

                temp_file = os.path.join(self.temp_dir, f"{index + 1:0{self.width}d}.m4v")
                with open(temp_file, "wb") as f:
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
                                speed_kb_s = downloaded_size / elapsed / 1024
                                self._check_speed_and_update_progress(
                                    part_num, downloaded_size, total_ranges, speed_kb_s
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
                                            self._requeue_slow(
                                                (index, segment), part_num, diagnostic=diagnostic
                                            )
                                        return part_num
                                else:
                                    slow_count = 0

                # 받은 세그먼트가 온전한지 내용으로 확인한다 (#321) — 잘린 본문이
                # 200과 맞는 Content-Length로 올 수 있어 상태 코드로는 알 수 없다.
                # 온전하지 않으면 아래 except가 일시 오류로 다시 받게 한다
                integrity.check_content_length(getattr(response, "headers", None), downloaded_size)
                with open(temp_file, "rb") as f:
                    integrity.check_fmp4_media_segment(f, downloaded_size)

                # 성공적으로 마무리된 경우
                with self.lock:
                    self.s.completed_threads += 1
                    self.s.completed_progress += downloaded_size
                    self.s.threads_progress[part_num] = 0
                self.logger.log_thread_complete(part_num, downloaded_size)
                return part_num

            except (
                requests.RequestException,
                requests.Timeout,
                integrity.TruncatedSegmentError,
            ) as e:
                with self.lock:
                    self._requeue_failed((index, segment), part_num, e)
                self.logger.log_error(f"Part {part_num} download failed", e)
                return part_num


def _frame_at(frames: CutFrames, pts: float) -> int:
    """PTS가 pts인 프레임의 번호. 받은 세그먼트에 그 프레임이 없으면 CutError다."""
    position = bisect_left(frames.frame_pts, pts - _PTS_TOLERANCE)
    if position < len(frames.frame_pts) and abs(frames.frame_pts[position] - pts) <= _PTS_TOLERANCE:
        return position
    raise CutError(CUT_FAILED, f"받은 세그먼트에 PTS {pts:.6f}초인 프레임이 없다")
