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
  없이 명확히 실패한다. 넘긴 세그먼트는 곧바로 지운다 — 실패하면 다시 받는다 (#309)
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
- postprocess: 구간마다 받은 세그먼트에서 구간 파일을 바로 쓴다(core/utils/fmp4_cut.py) —
  경계가 걸린 GOP만 다시 인코딩하고 가운데 세그먼트는 그대로 흘린다. 하나라도 실패하면
  다운로드 전체가 실패다. 세그먼트는 통째로 읽지 않는다
- 구간 파일로 넘긴 세그먼트는, 남은 구간이 쓰지 않으면 곧바로 지운다 — 구간 길이만큼을
  두 벌 들고 있지 않는다. 컷이 실패하면 임시 폴더와 남아 있는 세그먼트를 남기고, 다음
  실행은 없는 세그먼트만 다시 받는다
- 전체 다운로드(구간 없음)의 경로는 그대로다
"""

import dataclasses
import os
import shutil
import time as tm
from collections import Counter
from urllib.parse import urljoin, urlsplit

import requests

import core.downloaders.base as base_module
import core.downloaders.integrity as integrity
from core.api.hls import parse_media_playlist
from core.api.hls_fmp4 import fetch_fmp4_head, segment_file_name, segment_frames
from core.api.mp4 import Mp4Error
from core.api.session import get_thread_session
from core.downloaders.base import REQUEST_TIMEOUT, BaseDownloader, PostprocessError
from core.models.content import Content, ContentType
from core.models.cut import CutFrames, CutResult
from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from core.models.fmp4_index import Fmp4Head
from core.models.plan import DownloadPlan
from core.models.section_resume import SectionResume
from core.utils.fmp4_cut import cut_fmp4_section
from core.utils.fmp4_sections import Fmp4Section, plan_fmp4_sections
from core.utils.hybrid_cut import CutCancelled, CutError
from core.utils.paths import choose_temp_dir, release_output_paths


# 초기화 세그먼트를 다시 받게 하는 예외 — 본문이 잘려 왔다는 뜻인 것만 (#321).
# ChunkedEncodingError는 본문이 선언된 길이보다 먼저 끝났을 때, ContentDecodingError는
# 압축된 본문이 중간에 끊겨 풀리지 않을 때 난다
_INIT_TRUNCATION_ERRORS = (
    integrity.TruncatedSegmentError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)


def _cache_recovery_url(url: str) -> str:
    """실측된 네이버 CDN에서 손상 캐시를 피하도록 파일 앞 구분자를 중복한다.

    2026-10-12 실측: 일반 요청·Range·no-cache·쿼리 변경 모두 같은 손상 본문을
    반환했지만 이 경로는 정상 본문을 반환했다. 호스트·파일·인증 정보는 유지한다.
    다른 서버의 경로 의미는 알 수 없으므로 이 CDN에만 적용한다. 수신 후 구조 검사는
    그대로 수행하며 반복 실패해도 구분자를 계속 늘리지 않는다.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "ex-nlive-slitvod-streaming.navercdn.com":
        return url
    directory, separator, filename = parsed.path.rpartition("/")
    if not separator or not filename or directory.endswith("/"):
        return url
    return parsed._replace(path=directory + "//" + filename).geturl()


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
    # 구간의 컷 진행에서 프레임 정보 읽기의 몫 — moof만 읽어 짧다(세그먼트 400개 표본에서 컷의
    # 14%쯤)
    _cut_read_share = 0.1
    # 구 코드와 동일하게 모든 예외를 실패로 처리한다 (플레이리스트 파싱 실패 포함)
    _failure_exceptions = (Exception,)
    # 구간을 자를 때 조각마다 파라미터·패킷 수를 읽어 둘지 — 기본은 읽지 않는다.
    # 테스트가 True로 두고 cut_results를 정합 판정(check_cut)에 넘긴다
    _inspect_cuts: bool = False

    def __init__(self, data, logger, **callbacks):
        super().__init__(data, logger, **callbacks)
        self._cache_recovery: set[int] = set()
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
        # 이전 실행이 끝내 이번 실행이 건너뛰는 구간의 번호(0부터) — Content.section_resume에서 온다
        self._done_before: frozenset[int] = frozenset()
        # 이전 실행이 받아 둔 것을 이어받았는지 — 이어받았으면 실패 · 중단해도 임시 폴더를 남긴다
        self._resumed: bool = False
        self._wanted: list[int] = []  # 이번 실행이 구간을 자르는 데 쓰는 세그먼트의 인덱스
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
        """다운로드를 실행하고, 끝나면 구간 파일명의 예약을 푼다 (#309).

        캐시 복구로 표시한 세그먼트는 실행마다 새로 센다 (#375) — 같은 엔진으로 다시 실행하면
        앞 실행의 표시로 첫 요청부터 복구 경로로 가게 된다. base가 오류 재큐 수를 실행마다 새로
        만드는 것과 같이 락 안에서 새로 만든다.
        """
        with self.lock:
            self._cache_recovery = set()
        try:
            super().run()
        finally:
            release_output_paths(self.s.content.selection_paths)

    def prepare(self, content: Content) -> DownloadPlan:
        """플레이리스트를 받아 (index, 세그먼트) 목록의 계획을 만든다.

        구간이 있으면 구간에 필요한 세그먼트만 담은 계획을 만든다 (#309). 받을 양을 어림할 수
        있으면 여유 공간을 확인하고, 모자라면 받기를 시작하지 않는다.

        Raises:
            DiskSpaceError: 어림한 크기가 임시 폴더 · 저장 폴더의 여유 공간에 들어가지 않는 경우
        """
        self._space = None  # 실행마다 다시 만든다
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
        self._space = self._whole_space(self._durations_of(response.text), len(segments))
        self._require_space("start")
        # 총 바이트 크기는 미리 알 수 없고(total_size=None), 병합 후처리가 필요하다
        return DownloadPlan(
            items=tuple(enumerate(segments)),
            total_size=None,
            requires_postprocess=True,
        )

    @staticmethod
    def _durations_of(playlist_text: str) -> tuple[float, ...]:
        """플레이리스트의 세그먼트 길이(#EXTINF) — 크기 어림에만 쓴다. 읽지 못하면 빈 것이다.

        세그먼트 목록은 prepare가 읽은 것을 그대로 쓴다. 여기서 읽지 못해도 다운로드는 그대로
        가고 크기 어림만 하지 않는다.
        """
        try:
            return parse_media_playlist(playlist_text).durations
        except ValueError:
            return ()

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
        # 이전 실행이 만든 구간 파일은 이번 실행의 것이 아니다 — 이번 실행이 실패해도 지우지 않는다
        self._made_sections.clear()
        self.cut_results.clear()
        self.cut_frames.clear()
        self.s.section_resume = None
        resume = content.section_resume
        if resume is not None and (
            resume.fmp4_head is None or not resume.fits(content.selections, content.selection_paths)
        ):
            resume = None
        if resume is not None:
            # 끝낸 구간의 파일이 지워졌거나 바뀌었으면 그 구간은 다시 만든다
            resume = resume.with_files_checked()
        self._done_before = resume.done if resume is not None else frozenset()
        self._resumed = resume is not None
        # 이전 실행이 받아 둔 것(플레이리스트 · 프레임 정보 · 세그먼트)이 있으면 그것을 쓴다
        head = resume.fmp4_head if resume is not None else content.fmp4_head
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
        # 이전 실행이 끝낸 구간의 세그먼트는 받지 않는다
        wanted = sorted(
            {
                index
                for number, s in enumerate(sections)
                if number not in self._done_before
                for index in range(s.first_segment, s.last_segment + 1)
            }
        )
        self._wanted = wanted
        self._head = head
        self._sections = sections
        self.s.sections_total = len(sections)
        self.s.sections_done = self.s.sections_resumed = len(self._done_before)
        self.s.sections_failed = 0
        self._prefetched = self._whole_stored_segments(head, wanted)
        self.postprocess_kind = "cut"  # 구간마다 자른다 — 전체 다운로드의 remux와 구분한다
        self.s.merged_segments = 0
        self.width = len(str(len(head.playlist.segments)))
        self._space = self._section_space(
            head.playlist.durations,
            len(head.playlist.segments),
            wanted,
            {
                n: (s.first_segment, s.last_segment, s.last_pts - s.first_pts)
                for n, s in enumerate(sections)
                if n not in self._done_before
            },
            self._prefetched,
        )
        self._require_space("start")
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
        response = get_thread_session().get(init_url, timeout=REQUEST_TIMEOUT)
        # HTTP 오류는 그 오류로 실패시킨다 (#334) — 오류 본문을 쓰면 구조 검사가 잘림으로 보고
        # 다시 받다가 잘림 사유로 실패한다. HTTPError는 다시 받는 예외(_INIT_TRUNCATION_ERRORS)가 아니다
        response.raise_for_status()
        with open(init_segment_path, "wb") as f:
            f.write(response.content)

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
        지운다 (#309). 이전 실행을 이어받은 실행(Content.section_resume)은 임시
        폴더도 지우지 않는다 — 이전 실행의 구간 파일과 받아 둔 세그먼트가 남는다.
        """
        if not self._resumed and os.path.exists(self.temp_dir):
            # 이전 실행을 이어받은 실행은 임시 폴더를 남긴다 — 받아 둔 세그먼트는 이전 실행의
            # 것이다. 이번 실행이 만든 구간 파일만 지운다
            shutil.rmtree(self.temp_dir)
        made = self._made_sections if self.s.content.selections else [self.s.output_path]
        for path in made:
            if os.path.exists(path):
                os.remove(path)
        self._made_sections.clear()

    def _cleanup_after_run(self) -> None:
        """임시 폴더 삭제 (구 run의 (5) — 병합 후에는 빈 폴더만 남는다).

        이전 실행을 이어받은 실행이 중단됐으면 지우지 않는다 — 받아 둔 세그먼트는 이전 실행의
        것이고, 다음 실행이 다시 쓴다 (#309).
        """
        if self._resumed and self.state == DownloadState.WAITING:
            return
        shutil.rmtree(self.temp_dir)

    def _progress_total_size(self) -> int | None:
        # 전체 바이트 크기를 미리 알 수 없다 — 세그먼트 수 기반 계산은 어댑터가 한다
        return None

    # ============ 후처리: 순서 보장 병합 (구 run의 (4)) ============

    def postprocess(self) -> None:
        """세그먼트들을 인덱스 순서 그대로 ffmpeg stdin에 흘려 mp4로 재포장한다 (#88·#92).

        바이트 연결만으로는 fMP4 조각 구조(전역 인덱스 없음·라이브 타임라인
        보유)가 그대로라 편집 프로그램이 읽지 못한다. #92부터 중간 병합 파일
        없이 단일 패스로 재포장하며, 실패 시 폴백 없이 명확히 실패한다. 넘긴
        세그먼트는 곧바로 지운다 (규칙은 base의 _remux_streamed 참조).

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
        """구간마다 받은 세그먼트에서 구간 파일을 쓴다 — 넘긴 세그먼트는 곧바로 지운다.

        긴 구간부터 만든다 — 세그먼트를 가장 많이 쥔 구간이 먼저 디스크를 비운다. 구간 파일로
        넘긴 세그먼트는 남은 구간이 쓰지 않으면 그 자리에서 지우고, 경계 조각으로만 읽힌
        세그먼트는 그 구간이 만들어진 뒤에 지운다. 만들지 못한 구간이 쓰는 세그먼트는 그
        구간이 넘긴 것 말고는 지우지 않는다 — 다음 실행이 쓴다.

        만들지 못한 구간이 있어도 나머지 구간을 끝까지 만들고, 그 뒤에 PostprocessError로
        끝낸다 — 임시 폴더(남은 세그먼트)와 만든 구간 파일은 남는다. 구간마다의 결과는 공유
        데이터의 sections_done · sections_failed에 센다. 중단 · 일시정지는 구간 사이에서,
        그리고 컷이 도는 동안(세그먼트를 넘기는 사이 · 도는 ffmpeg)에 따른다.

        이전 실행이 끝낸 구간(Content.section_resume)은 만들지 않는다. 만들지 못한 구간이
        있으면 끝낸 구간과 남아 있는 세그먼트를 공유 데이터의 section_resume에 남긴다 — 다음
        실행은 없는 세그먼트만 다시 받는다.
        """
        head = self._head
        paths = self.s.content.selection_paths
        failures: list[Exception] = []
        done = set(self._done_before)
        todo = [n for n in range(len(self._sections)) if n not in self._done_before]
        lengths = {n: self._sections[n].last_pts - self._sections[n].first_pts for n in todo}
        progress = self._track_cuts({n + 1: lengths[n] for n in todo})
        # 세그먼트마다 그것을 쓰는, 아직 만들어지지 않은 구간의 수
        uses = Counter(
            index
            for n in todo
            for index in range(self._sections[n].first_segment, self._sections[n].last_segment + 1)
        )
        finished: dict[int, tuple[CutResult, CutFrames]] = {}
        for turn, position in enumerate(base_module.cut_order(lengths), start=1):
            number = position + 1  # 구간 번호(1부터 — 구간 목록의 순서)
            section, output_path = self._sections[position], paths[position]
            if self.state == DownloadState.PAUSED:
                self.s._pause_event.wait()
            if self.state == DownloadState.WAITING:
                return  # 정리(임시 폴더·만든 구간 파일 삭제)는 run()의 중단 경로가 한다
            indexes = range(section.first_segment, section.last_segment + 1)
            stages: list[tuple[str, float]] = []

            def drop_fed(place: int, first: int = section.first_segment) -> None:
                # 구간 파일로 다 넘어간 세그먼트 — 다른 구간이 쓰지 않으면 더 읽을 일이 없다
                if uses[first + place] == 1:
                    base_module._remove_fed_segment(self._segment_path(first + place))

            on_read, on_cut = self._cut_reporters(progress.section(number))
            self._begin_cut_reading()  # 컷의 read 단계가 끝나면 내려간다(_cut_stage_recorder)
            try:
                result, frames = cut_fmp4_section(
                    os.path.join(self.temp_dir, f"section_{number}"),
                    head,
                    section,
                    [self._segment_path(index) for index in indexes],
                    output_path,
                    on_fed=drop_fed,
                    inspect=self._inspect_cuts,
                    on_stage=self._cut_stage_recorder(stages),
                    on_progress=on_cut,
                    should_stop=self._stop_requested,
                    should_pause=self._pause_requested,
                    on_read=on_read,
                )
            except CutCancelled:
                self._end_cut_reading()
                return  # 컷이 도는 동안 중단됐다 — 정리는 run()의 중단 경로가 한다
            except (CutError, Mp4Error) as e:
                # 이 구간은 만들지 못했다 — 나머지 구간은 끝까지 만든다
                self.logger.log_error("Cut failed — remaining segments preserved for retry", e)
                failures.append(e)
                self.s.sections_failed += 1
            else:
                finished[position] = (result, frames)
                # 판정이 구간 목록의 순서로 읽는다 — 만든 순서가 아니다
                self.cut_results[:] = [finished[n][0] for n in sorted(finished)]
                self.cut_frames[:] = [finished[n][1] for n in sorted(finished)]
                self._made_sections.append(output_path)
                self.s.sections_done += 1
                done.add(position)
                for index in indexes:
                    uses[index] -= 1
                    if not uses[index]:  # 경계 조각으로만 읽힌 것까지 — 이제 쓰는 구간이 없다
                        base_module._remove_fed_segment(self._segment_path(index))
            self._end_cut_reading()  # 읽다가 실패한 구간에서도 준비 중 표시가 남지 않게
            self._log_cut_stages(number, stages)
            # 병합 진행(세그먼트 수 기반)을 구간 수에 비례해 올린다 — 어댑터의 분모는
            # 받은 세그먼트 수 + 초기화 세그먼트다
            self.s.merged_segments = (
                (self.s.max_threads + 1) * (len(self._done_before) + turn) // len(self._sections)
            )
            progress.finish(number)
            self._on_progress(
                ProgressEvent(
                    downloaded_size=self.s.total_downloaded_size,
                    total_size=self._progress_total_size(),
                    speed=0.0,
                    active_threads=0,
                )
            )
        # 지금 있는 구간 파일 크기의 합 — 이전 실행이 끝낸 구간의 것도 센다. 완료 카드가 적는다
        self.s.sections_output_size = sum(
            os.path.getsize(paths[number])
            for number in sorted(done)
            if os.path.exists(paths[number])
        )
        if failures:
            kept = {
                index
                for index in set(head.stored) | set(self._wanted)
                if os.path.exists(self._segment_path(index))
            }
            self.s.section_resume = SectionResume(
                selections=tuple(self.s.content.selections),
                paths=tuple(paths),
                done=frozenset(done),
                done_sizes=SectionResume.sizes_of(paths, done),
                # 넘겨받은 것에 적지 않고 새로 만든다. 남아 있는 세그먼트만 적는다 — 구간
                # 파일로 넘긴 것은 지워졌다. 다음 실행은 쓰기 전에 파일마다 다시 확인한다
                fmp4_head=dataclasses.replace(head, segment_dir=self.temp_dir, stored=kept),
            )
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
            self._complete_reused(part_num, self._prefetched[index])
            return part_num

        downloaded_size = 0
        segment_url = urljoin(self.s.base_url, segment)
        if index in self._cache_recovery:
            segment_url = _cache_recovery_url(segment_url)
        while not self.state == DownloadState.WAITING:
            try:
                # 스레드로컬 세션으로 같은 워커의 세그먼트 요청 간 연결을 재사용한다 (#31)
                response = get_thread_session().get(
                    segment_url, stream=True, timeout=REQUEST_TIMEOUT
                )
                response.raise_for_status()
                part_start_time = self._now()
                slow_watch = self._watch_slow(
                    part_num, integrity.declared_length(getattr(response, "headers", None))
                )
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
                        # 일시정지한 시간은 이 파트의 속도 판정에서 뺀다 — 재개 직후 느린 속도로 끊기지 않게
                        part_start_time += self._wait_while_paused()

                        if chunk:
                            write_start = tm.perf_counter()
                            f.write(chunk)
                            write_elapsed += tm.perf_counter() - write_start
                            downloaded_size += len(chunk)
                            elapsed = self._now() - part_start_time

                            if elapsed > 0:
                                speed_kb_s = downloaded_size / elapsed / 1024
                                self._check_speed_and_update_progress(
                                    part_num, downloaded_size, total_ranges, speed_kb_s
                                )
                                if slow_watch.is_slow(elapsed, downloaded_size):
                                    # 속도가 너무 느리면 스레드 재시작
                                    ratio = write_elapsed / elapsed * 100 if elapsed > 0 else 0.0
                                    diagnostic = (
                                        f"write={write_elapsed:.3f}s/{elapsed:.3f}s={ratio:.0f}%"
                                    )
                                    with self.lock:
                                        self._requeue_slow(
                                            (index, segment), part_num, diagnostic=diagnostic
                                        )
                                    self._abandon_response(part_num, response)
                                    return part_num

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
                self._note_received(index, downloaded_size)
                self.logger.log_thread_complete(part_num, downloaded_size)
                return part_num

            except (
                requests.RequestException,
                requests.Timeout,
                integrity.TruncatedSegmentError,
            ) as e:
                with self.lock:
                    if (
                        isinstance(e, integrity.TruncatedSegmentError)
                        and index not in self._cache_recovery
                        and _cache_recovery_url(segment_url) != segment_url
                    ):
                        self._cache_recovery.add(index)
                        self.logger.warning(
                            f"Segment {index + 1}: invalid cached body; retrying alternate resource path"
                        )
                    requeue = self._requeue_failed((index, segment), part_num, e)
                # 로그 · 실패 통지는 락을 놓은 뒤에 한다 (#329)
                self._settle_requeue(
                    requeue, f"Part {part_num} download failed (segment {index + 1})", e
                )
                return part_num
