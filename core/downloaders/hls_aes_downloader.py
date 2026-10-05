"""AES(SEA) 암호화 VOD 다운로드 엔진 — BaseDownloader 하위 구현 (#57, SPEC §6·§8.1).

치지직의 암호화 VOD는 HLS(MPEG-TS) 세그먼트에 AES-128-CBC가 걸려 있고, 키는
플레이리스트의 ``#EXT-X-KEY URI``가 가리키는 치지직 API가 **인증된 세션에**
평문 HTTP로 내준다. 즉 이 경로는 유저 본인의 쿠키로 유저 본인이 볼 권한이
있는 컨텐츠를 받는 것이며, 쿠키가 없으면 키 요청이 403으로 실패해 아무것도
받아지지 않는다(실측). 권한을 만들어내지 않는다.

m3u8(라이브 다시보기) 경로와의 차이:
- 컨테이너가 MPEG-TS(.ts)다 — fMP4가 아니므로 ``EXT-X-MAP`` 초기화 세그먼트가
  없고, 병합 대상은 세그먼트 파일 그 자체뿐이다
- 세그먼트마다 복호화 단계가 있다. IV는 ``#EXT-X-KEY``에 명시되면 그 값을,
  없으면 미디어 시퀀스 번호를 쓴다(RFC 8216 §5.2 — 치지직 실측은 후자)

키 취득은 쿠키가 필요해 core가 직접 할 수 없다(core→app 의존 금지).
``requires_key_resolution=True``로 선언하고 서비스가 주입한 리졸버를 쓴다 —
base_url 해석(#75)과 같은 주입 방식이다.

**키 값은 로그·예외 메시지·커밋에 절대 싣지 않는다.**

구간 다운로드 (#309) — ``Content.selections``가 비어 있지 않을 때:

- prepare: 플레이리스트와, 시각 축을 재는 세그먼트 · 구간의 양 끝이 든 세그먼트를 받아
  복호화해 읽고(core/api/hls_ts.py) 구간마다 받을 세그먼트와 첫·끝 프레임을 정한다
  (core/utils/ts_sections.py). 받을 항목은 구간들의 세그먼트를 합친 것이다 — 두 구간이
  같은 세그먼트를 쓰면 한 번만 받는다. ``Content.ts_head``로 이미 받은 것이 오면 그것을 쓴다.
  여기서 받다가 실패한 세그먼트는 다시 받지 않고 다운로드가 실패한다
- **세그먼트에 범위 요청을 보내지 않는다.** 구간을 정하면서 받은 세그먼트(복호화한 것)는
  임시 폴더에 두고(``TsHead.segment_dir`` · ``stored``), 전송 단계는 그 파일이 온전하면 다시
  받지 않는다. 온전한지는 받은 세그먼트에 하는 것과 같은 검사(integrity.check_ts_segment)로
  다시 확인하고, 아니면 전송 단계가 다시 받는다
- **받아 둔 세그먼트는 그것을 받은 플레이리스트에 묶인다**(``TsHead.playlist_ref``). 엔진이 받을
  플레이리스트(``base_url``)와 다르면 — 구간을 해석한 뒤 해상도를 바꾼 경우 — 받아 둔 것을
  쓰지 않고 지운 뒤 처음부터 받는다
- **키는 받을 세그먼트가 있을 때만, 한 번 받는다.** 키가 맞는지 확인하려고 세그먼트를 따로
  받지 않는다 — 엔진이 처음 받는 세그먼트의 복호화 · TS 검사가 그 확인이다. 받을 세그먼트가
  모두 받아 둔 것이면 키를 받지 않는다
- postprocess: 구간마다 그 구간의 세그먼트를 mp4로 다시 싸 잘라 파일로 만든다
  (core/utils/ts_cut.py). 하나라도 실패하면 다운로드 전체가 실패다. 다시 싼 파일은 임시
  폴더에 두고 그 구간을 자르면 바로 지운다
- 쓰는 쪽은 한 번에 하나다 — 구간을 정하는 일(prepare)이 끝난 뒤에 작업자가 시작하고,
  작업자는 세그먼트마다 다른 이름의 파일에 쓴다. 잠금을 두지 않는다
- 임시 폴더(받은 세그먼트 · 다시 싼 파일)는 구간을 모두 만들면 지운다. 컷이 실패하면
  남긴다(#92 · #185). 사용자가 중단하면 전체 다운로드와 같이 지운다. 이전 실행이 남긴
  파일은 쓰지 않는다(#190) — ``TsHead.stored``에 적혀 있고 검사를 통과한 것만 쓴다
- 전체 다운로드(구간 없음)의 경로는 그대로다
"""

import os
import shutil
import time as tm
from fractions import Fraction
from urllib.parse import urljoin

import requests

import core.downloaders.integrity as integrity
from core.api.hls import parse_media_playlist
from core.api.hls_ts import (
    fetch_ts_head,
    open_ts_segment,
    playlist_ref,
    segment_iv,
    segment_streams,
    ts_key_uri,
    ts_segment_file_name,
)
from core.api.mpegts import TsError, parse_ts
from core.api.session import get_thread_session
from core.downloaders.base import REQUEST_TIMEOUT, BaseDownloader, PostprocessError
from core.downloaders.decrypt import DecryptionError, decrypt_segment, looks_like_ts
from core.models.content import Content, ContentType
from core.models.cut import CutFrames, CutResult
from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from core.models.plan import DownloadPlan
from core.models.ts_index import TsHead, TsStreams
from core.utils.hybrid_cut import CutError
from core.utils.paths import choose_temp_dir, release_output_paths
from core.utils.section_plan import PlannedSection
from core.utils.ts_cut import cut_ts_section
from core.utils.ts_sections import TsSectionSource, choose_ts_frame_rate, plan_ts_sections


__all__ = ["DecryptionError", "HlsAesDownloader"]  # DecryptionError는 decrypt.py에 있다


class HlsAesDownloader(BaseDownloader):
    """AES-128-CBC로 암호화된 HLS(TS) VOD를 세그먼트 단위로 받아 복호화·병합하는 엔진.

    호출 규약: 소유자(서비스·스크립트)가 data.base_url에 HLS 미디어 플레이리스트
    URL을 채우고 DownloadTaskModel.start()로 RUNNING 전이를 마친 뒤 run()을
    호출한다. run()은 완료·중단·실패까지 블로킹한다.
    """

    run_thread_name = "DownloadHlsAesThread"
    worker_pool_prefix = "DownloadHlsAesWorker"
    # base_url(HLS 미디어 플레이리스트 URL)은 SEA 매니페스트 파싱 시점에 해상도별로
    # 확정되어 Content에 실려 온다 — m3u8(라이브 다시보기)처럼 재해석하지 않는다
    requires_base_url_resolution = False
    requires_key_resolution = True
    supports_selections = True  # 복호화한 TS의 PES 머리로 구간의 프레임을 정할 수 있다 (#309)
    # m3u8 경로와 동일하게 모든 예외를 실패로 처리한다 (플레이리스트·키 취득 실패 포함)
    _failure_exceptions = (Exception,)
    # 구간을 자를 때 조각마다 파라미터·패킷 수를 읽어 둘지 — 기본은 읽지 않는다.
    # 테스트가 True로 두고 cut_results를 정합 판정(check_cut)에 넘긴다
    _inspect_cuts: bool = False
    # 받은 키로 복호화에 성공한 세그먼트가 아직 없는지 — 구간 다운로드에서만 True가 된다.
    # True인 동안 받는 세그먼트는 복호화 결과가 TS로 보이지 않으면 키가 틀린 것으로 보고
    # 다운로드를 끝낸다(다시 받지 않는다). 전체 다운로드는 prepare가 키를 확인하므로 False다
    _key_unconfirmed: bool = False
    # 복호화 실패를 이미 알렸는지 — 키가 틀리면 동시에 받던 작업자가 모두 같은 실패를 만난다.
    # 처음 만난 작업자만 알리고(_fail_fatally) 나머지는 알리지 않고 끝난다. self.lock으로 지킨다
    _decrypt_failed: bool = False

    def __init__(self, data, logger, **callbacks):
        super().__init__(data, logger, **callbacks)
        # 세그먼트 저장용 임시 폴더 (실패 정리 경로가 참조하므로 실행 전에 확정).
        # 산출물 파일명에서 파생해 다운로드 간 폴더 공유·상호 삭제를 막는다 (#105).
        # 조건이 맞으면 시스템 임시 폴더(로컬 매체)로 보낸다(#192) — m3u8과 동일.
        # 구간 다운로드는 첫 구간 파일의 이름에서 파생한다 — output_path는 쓰지 않는 이름이라
        # 같은 영상의 구간 다운로드 둘이 겹칠 수 있다. 구간을 해석한 쪽이 세그먼트를 받아 둔
        # 폴더가 있으면 그 폴더를 쓴다(prepare가 플레이리스트를 견준 뒤 확정한다)
        content = self.s.content
        section_paths = content.selection_paths if content.selections else ()
        handed = content.ts_head if content.selections else None
        if handed is not None and handed.segment_dir is not None:
            self.temp_dir = handed.segment_dir
        else:
            self.temp_dir = choose_temp_dir(
                section_paths[0] if section_paths else self.s.output_path
            )
        self._key: bytes | None = None
        self._playlist = None
        # 구간 다운로드의 상태 (#309) — prepare가 채운다. 전체 다운로드면 비어 있다
        self._sections: tuple[PlannedSection, ...] = ()
        # 구간을 정할 때 받은 플레이리스트 · 세그먼트의 프레임 정보
        self._head: TsHead | None = None
        self._frame_rate: Fraction | None = None  # 구간을 정한 프레임률
        # 구간을 정하면서 이미 온전하게 받아 둔 세그먼트 — 인덱스 → 파일 크기. 다시 받지 않는다
        self._prefetched: dict[int, int] = {}
        self._made_sections: list[str] = []  # 이번 실행이 만든 구간 파일
        self.cut_results: list[CutResult] = []  # 구간마다의 컷 결과 — sections와 같은 순서
        # 구간마다 컷이 원본으로 삼은(다시 싼 mp4의) 프레임 정보 — 판정이 쓴다
        self.cut_frames: list[CutFrames] = []

    @property
    def sections(self) -> tuple[PlannedSection, ...]:
        """구간 다운로드의 구간 목록 — 받을 세그먼트와 첫·끝 프레임의 시각. prepare 뒤에 채워진다."""
        return self._sections

    @classmethod
    def supports(cls, content: Content) -> bool:
        """AES(SEA) 암호화 VOD를 처리한다."""
        return content.content_type is ContentType.CHZZK_VIDEO_HLS_AES

    # ============ 작업 목록·수신 준비 ============

    def run(self) -> None:
        """다운로드를 실행하고, 끝나면 구간 파일명의 예약을 푼다 (#309)."""
        try:
            super().run()
        finally:
            release_output_paths(self.s.content.selection_paths)

    def prepare(self, content: Content) -> DownloadPlan:
        """플레이리스트를 받아 세그먼트 목록을 만들고 복호화 키를 취득한다.

        구간이 있으면 구간에 필요한 세그먼트만 담은 계획을 만든다 (#309).
        """
        # 구간 다운로드의 상태는 실행마다 다시 채운다 — 같은 엔진을 다시 돌려도 남지 않는다
        self._sections = ()
        self._head = None
        self._prefetched = {}
        self._key_unconfirmed = False
        self._decrypt_failed = False
        if content.selections:
            return self._prepare_sections(content)
        response = get_thread_session().get(self.s.base_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        playlist = parse_media_playlist(response.text)

        if playlist.key is None:
            raise DecryptionError("암호화 정보(#EXT-X-KEY)가 없는 플레이리스트다")
        if not playlist.key.is_aes_128:
            # AES-128 외의 방식은 지원하지 않는다 — 우회를 시도하지 않고 실패시킨다
            raise DecryptionError(f"지원하지 않는 암호화 방식: {playlist.key.method}")

        key_uri = urljoin(self.s.base_url, playlist.key.uri)
        self._key = self._resolve_key(content, key_uri)
        self._playlist = playlist
        self.s.merged_segments = 0
        # 세그먼트 임시 파일명 0채움 자릿수 — sorted() 병합 순서의 전제
        self.width = len(str(len(playlist.segments)))

        # 워커 풀을 띄우기 전에 키·IV 규칙을 첫 세그먼트로 검증한다.
        # 여기서 실패하면 run()의 실패 경로로 깔끔히 빠져 수 GB짜리 쓰레기
        # 파일을 만들지 않는다 (풀 안에서 터지면 정리 경로가 복잡해진다)
        if playlist.segments:
            self._verify_key(playlist)

        # 전체 바이트 크기는 미리 알 수 없고(total_size=None), 병합 후처리가 필요하다
        return DownloadPlan(
            items=tuple(enumerate(playlist.segments)),
            total_size=None,
            requires_postprocess=True,
        )

    def _prepare_sections(self, content: Content) -> DownloadPlan:
        """구간 다운로드의 계획 — 구간들의 세그먼트를 합친 것을 받는다 (#309).

        플레이리스트와 세그먼트의 프레임 정보는 ``content.ts_head``가 있고 그것이 이 다운로드가
        받을 플레이리스트의 것이면 그것을 쓰고, 없는 것만 여기서 받는다. 프레임 정보를 읽으려고
        받는 세그먼트는 임시 폴더에 둔다 — 전송 단계가 다시 받지 않는다. 여기서 받다가 실패하면
        다시 받지 않고 그대로 실패한다.

        프레임률은 넘겨받은 값(``TsHead.frame_rate`` — 구간을 해석한 쪽이 매니페스트의 선언값,
        없으면 잰 값으로 정한 것)이 있으면 그것이고, 없으면 첫 세그먼트에서 잰다. 매니페스트의
        선언값은 엔진까지 오지 않는다 — 구간을 해석하는 쪽이 넣어 준다.

        키는 여기서 받을 세그먼트가 있거나 전송 단계가 받을 세그먼트가 있을 때만 받는다.

        Raises:
            ValueError: 구간과 산출물 경로의 수가 다른 경우, 프레임률을 정할 수 없는 경우
            DecryptionError: 플레이리스트를 복호화할 수 없거나 키가 맞지 않는 경우
            TruncatedSegmentError: 여기서 받은 세그먼트가 온전하지 않은 경우
            TsError: 세그먼트를 해석하지 못한 경우
            SelectionError: 구간이 검증을 통과하지 못했거나, 녹화가 끊긴 자리를 넘거나,
                구간의 시각이 놓인 세그먼트를 찾지 못한 경우
        """
        if len(content.selection_paths) != len(content.selections):
            raise ValueError(
                f"구간 {len(content.selections)}개에 산출물 경로 {len(content.selection_paths)}개"
            )
        self._key = None  # 이번 실행에서 받을 것이 있을 때만 받는다
        # 이전 실행이 만든 구간 파일은 이번 실행의 것이 아니다 — 이번 실행이 실패해도 지우지 않는다
        self._made_sections.clear()
        self.cut_results.clear()
        self.cut_frames.clear()

        head = content.ts_head
        if head is not None and head.playlist_ref != playlist_ref(self.s.base_url):
            # 다른 플레이리스트(해상도)에서 받아 둔 것이다 — 쓰지 않고 지운 뒤 처음부터 받는다.
            # 어느 플레이리스트의 것인지 모르는 것(None)도 쓰지 않는다
            if head.segment_dir is not None and os.path.exists(head.segment_dir):
                shutil.rmtree(head.segment_dir)
            head = None
            self.temp_dir = choose_temp_dir(content.selection_paths[0])
        if head is not None and head.segment_dir is not None:
            # 구간을 해석한 쪽이 세그먼트를 받아 둔 폴더 — 엔진을 만든 뒤에 넘겨받았을 수 있다
            self.temp_dir = head.segment_dir
        else:
            # 받아 둔 세그먼트가 없다 — 임시 폴더를 비우고 여기서 받는 것을 그 폴더에 둔다.
            # 이전 실행이 남긴 파일은 쓰지 않는다 (#190)
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            if head is None:
                head = fetch_ts_head(self.s.base_url, self.temp_dir)
            else:
                head.segment_dir = self.temp_dir

        confirmed = False  # 여기서 받은 세그먼트로 키가 맞는 것을 확인했는지

        def key() -> bytes:
            if self._key is None:
                self._key = self._resolve_key(content, ts_key_uri(self.s.base_url, head))
            return self._key

        def segment_at(index: int) -> TsStreams:
            nonlocal confirmed
            found = head.segments.get(index)
            if found is None:
                # 복호화 결과가 TS가 아니면 키가 틀린 것으로 보고 DecryptionError가 난다
                found = segment_streams(head, self.s.base_url, index, key())
                confirmed = True
            return found

        fps = head.frame_rate
        if fps is None:
            fps = choose_ts_frame_rate([segment_at(0)]).rate
        sections = plan_ts_sections(head.playlist, content.selections, segment_at, fps)
        # 두 구간이 같은 세그먼트를 쓰면 한 번만 받는다
        wanted = sorted(
            {index for s in sections for index in range(s.first_segment, s.last_segment + 1)}
        )
        self._head = head
        self._playlist = head.playlist
        self._frame_rate = fps
        self._sections = sections
        self.s.sections_total = len(sections)
        self.s.sections_done = self.s.sections_failed = 0
        self.width = len(str(len(head.playlist.segments)))
        self._prefetched = self._whole_stored_segments(head, wanted)
        if any(index not in self._prefetched for index in wanted):
            # 전송 단계가 받을 세그먼트가 있다 — 키를 받는다. 키가 맞는지 확인하려고 세그먼트를
            # 따로 받지 않는다. 아직 확인하지 못했으면 전송 단계가 처음 받는 세그먼트가 확인한다
            key()
            self._key_unconfirmed = not confirmed
        self.postprocess_kind = "cut"  # 구간마다 자른다 — 전체 다운로드의 remux와 구분한다
        self.s.merged_segments = 0
        return DownloadPlan(
            items=tuple((index, head.playlist.segments[index]) for index in wanted),
            total_size=None,
            requires_postprocess=True,
            selections=tuple(content.selections),
        )

    def _whole_stored_segments(self, head: TsHead, wanted: list[int]) -> dict[int, int]:
        """wanted 가운데 임시 폴더에 온전하게 받아 둔 세그먼트 — 인덱스 → 파일 크기.

        ``head.stored``에 적혀 있고, 파일이 있고, 전송 단계가 받은 세그먼트에 하는 것과 같은
        검사(integrity.check_ts_segment)를 통과한 것만이다. 하나라도 아니면 그 세그먼트는
        전송 단계가 다시 받는다 — 구간을 해석한 쪽이 받아 둔 파일을 믿지 않는다. 폴더에
        있어도 ``head.stored``에 없는 파일(이전 실행이 남긴 것)은 쓰지 않는다 (#190).
        """
        found = {}
        for index in wanted:
            if index not in head.stored:
                continue
            try:
                with open(self._segment_path(index), "rb") as f:
                    data = f.read()
                integrity.check_ts_segment(data)
            except (integrity.TruncatedSegmentError, OSError):
                continue
            found[index] = len(data)
        return found

    def _download_start_log_args(self) -> tuple:
        # 전체 크기·파트 크기를 미리 알 수 없다 (m3u8 경로와 동일하게 0)
        return (0, 0, self.s.total_ranges, self.s.adjust_threads)

    def _prepare_output(self) -> None:
        """임시 폴더를 재생성한다 (TS 경로에는 초기화 세그먼트가 없다).

        구간 다운로드는 폴더를 통째로 지우지 않는다 — 구간을 정하면서 받아 둔 세그먼트가
        들어 있다(_prepare_section_output, #309).
        """
        if self._sections:
            self._prepare_section_output()
            return
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        os.makedirs(self.temp_dir)

    def _prepare_section_output(self) -> None:
        """구간 다운로드의 임시 폴더를 준비한다 — 온전하게 받아 둔 구간의 세그먼트만 남긴다.

        남기는 것은 이 다운로드의 구간에 쓰이고 온전한 것으로 확인된 세그먼트(_prefetched)뿐이다.
        그 밖의 것은 지운다 — 구간에 쓰이지 않는데 받아 둔 세그먼트(시각 축과 프레임률을 재려고
        받은 첫 · 마지막 세그먼트), 온전하지 않은 것, 이전 실행이 남긴 것. 지운 세그먼트 가운데
        구간에 쓰이는 것은 전송 단계가 다시 받는다.
        """
        os.makedirs(self.temp_dir, exist_ok=True)
        kept = {os.path.basename(self._segment_path(index)) for index in self._prefetched}
        for name in os.listdir(self.temp_dir):
            if name not in kept:
                stale = os.path.join(self.temp_dir, name)
                shutil.rmtree(stale) if os.path.isdir(stale) else os.remove(stale)

    def _log_item_start(self, part_num: int, item) -> None:
        _index, segment = item
        self.logger.log_m3u8_thread_start(part_num, segment)

    def _download_item(self, item, part_num: int):
        index, segment = item
        if index in self._prefetched:
            # 구간을 정하면서 이미 온전하게 받아 둔 세그먼트 — 다시 받지 않는다 (#309)
            size = self._prefetched[index]
            with self.lock:
                self.s.completed_threads += 1
                self.s.completed_progress += size
                self.s.threads_progress[part_num] = 0
            self.logger.log_thread_complete(part_num, size)
            return part_num
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
        """임시 폴더 삭제 (병합 후에는 빈 폴더만 남는다)."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _progress_total_size(self) -> int | None:
        # 전체 바이트 크기를 미리 알 수 없다 — 세그먼트 수 기반 계산은 어댑터가 한다
        return None

    # ============ 키 취득 ============

    def _resolve_key(self, content: Content, key_uri: str) -> bytes:
        """주입된 리졸버로 복호화 키를 취득한다 (쿠키가 필요해 앱 계층이 수행).

        Raises:
            DecryptionError: 리졸버 미주입이거나 키 길이가 AES-128이 아닌 경우
        """
        if self._key_resolver is None:
            raise DecryptionError("암호화 VOD 다운로드에는 key_resolver 주입이 필요하다")
        key = self._key_resolver(content, key_uri)
        if len(key) != 16:
            # 값은 싣지 않고 길이만 보고한다
            raise DecryptionError(f"AES-128 키 길이가 올바르지 않다 (길이: {len(key)})")
        return key

    def _verify_key(self, playlist) -> None:
        """첫 세그먼트를 받아 복호화 결과가 MPEG-TS인지 확인한다 (prepare 단계).

        키가 틀리면 출력이 난수라 이 검사를 통과할 수 없다. 실행 전에 확인해
        잘못된 결과 파일을 만들지 않는 것이 목적이다. 전체 다운로드만 쓴다 — 구간
        다운로드는 확인하려고 세그먼트를 따로 받지 않는다(#309).

        Raises:
            DecryptionError: 복호화 결과가 유효한 미디어가 아닌 경우
        """
        url = urljoin(self.s.base_url, playlist.segments[0])
        response = get_thread_session().get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        plain = decrypt_segment(response.content, self._key, self._segment_iv(0))
        if not looks_like_ts(plain):
            raise DecryptionError("복호화 결과가 MPEG-TS가 아니다 — 키 또는 IV 규칙이 맞지 않는다")

    def _segment_iv(self, index: int) -> bytes:
        """세그먼트의 IV — 명시 IV가 있으면 그 값, 없으면 미디어 시퀀스 번호."""
        return segment_iv(self._playlist, index)

    # ============ 후처리: 순서 보장 병합 ============

    def postprocess(self) -> None:
        """복호화 세그먼트들을 순서 그대로 ffmpeg stdin에 흘려 mp4로 재포장한다 (#88·#92).

        바이트 연결만으로는 MPEG-TS 스트림이 .mp4 이름으로 저장되는 컨테이너
        불일치에 더해, 라이브 원본 타임라인(시작 오프셋≠0)과 전역 인덱스
        부재가 그대로다 — m3u8(fMP4) 경로와 같은 제약이라 같은 처리를 한다.
        #92부터 중간 병합 파일 없이 단일 패스로 재포장하며, 실패 시 폴백
        없이 명확히 실패하고 세그먼트를 보존한다 (규칙은 base의
        _remux_streamed 참조).

        구간 다운로드는 재포장하지 않고 구간마다 잘라 파일로 만든다 (#309).
        """
        self._on_merge_start()
        if self._sections:
            self._cut_sections()
            return
        segment_files = self._list_segment_files((".ts",))
        self._remux_streamed([os.path.join(self.temp_dir, f) for f in segment_files])

    # ============ 구간 다운로드의 후처리 (#309) ============

    def _segment_path(self, index: int) -> str:
        """임시 폴더 안 index번째 세그먼트의 경로 — _download_segment가 쓰는 이름과 같다."""
        name = ts_segment_file_name(len(self._playlist.segments), index)
        return os.path.join(self.temp_dir, name)

    def _cut_sections(self) -> None:
        """구간마다 그 구간의 세그먼트를 mp4로 다시 싸 자른다 (core/utils/ts_cut.py).

        구간 목록 순서대로 자른다. 자르지 못한 구간이 있어도 나머지 구간을 끝까지 자르고,
        그 뒤에 PostprocessError로 끝낸다 — 임시 폴더(받은 세그먼트)와 만든 구간 파일은
        남는다. 구간마다의 결과는 공유 데이터의 sections_done · sections_failed에 센다.
        다시 싼 mp4는 임시 폴더에 두고,
        그 구간의 컷이 끝나면(성공이든 실패든) 바로 지운다 — 구간이 여럿이어도 쌓이지 않는다.
        구간 사이에서 중단·일시정지를 확인한다(컷 하나는 중간에 멈추지 않는다).

        프레임 정보는 구간을 정할 때 읽어 둔 것을 쓰고, 전송 단계가 받은 세그먼트는 그 파일을
        읽는다. 넘겨받은 ``TsHead``에는 적지 않는다 — 그 객체에 쓰는 일은 prepare에서 끝난다.
        """
        head = self._head
        paths = self.s.content.selection_paths
        read: dict[int, TsStreams] = {}  # 전송 단계가 받은 세그먼트를 여기서 읽은 것

        def segment_at(index: int) -> TsStreams:
            found = head.segments.get(index)
            if found is None:
                found = read.get(index)
            if found is None:
                with open(self._segment_path(index), "rb") as f:
                    found = read[index] = parse_ts(f.read())
            return found

        source = TsSectionSource(head.playlist, segment_at, self._frame_rate)
        failures: list[Exception] = []
        for number, (section, output_path) in enumerate(zip(self._sections, paths), start=1):
            if self.state == DownloadState.PAUSED:
                self.s._pause_event.wait()
            if self.state == DownloadState.WAITING:
                return  # 정리(임시 폴더·만든 구간 파일 삭제)는 run()의 중단 경로가 한다
            try:
                indexes = range(section.first_segment, section.last_segment + 1)
                ts_frames = source.frames_of(
                    section.first_segment, section.last_segment, section.origin
                )
                result, frames = cut_ts_section(
                    [self._segment_path(index) for index in indexes],
                    ts_frames,
                    section.first_pts,
                    section.last_pts,
                    output_path,
                    os.path.join(self.temp_dir, f"section_{number}.mp4"),
                    inspect=self._inspect_cuts,
                )
            except (CutError, TsError) as e:
                # 이 구간은 자르지 못했다 — 나머지 구간은 끝까지 자른다
                self.logger.log_error("Cut failed — segments preserved for retry", e)
                failures.append(e)
                self.s.sections_failed += 1
            else:
                self.cut_results.append(result)
                self.cut_frames.append(frames)
                self._made_sections.append(output_path)
                self.s.sections_done += 1
            # 병합 진행(세그먼트 수 기반)을 구간 수에 비례해 올린다 — 어댑터의 분모는
            # 받은 세그먼트 수다(TS 경로에는 초기화 세그먼트가 없다)
            self.s.merged_segments = self.s.max_threads * number // len(self._sections)
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

    # ============ 다운로드 동작 ============

    def _download_segment(self, index: int, segment: str, part_num: int, total_ranges: int):
        """개별 세그먼트를 받아 복호화해 임시 파일로 저장한다 (재시도 포함).

        저속 재시도·일시정지·중단 판정 규칙은 m3u8 경로와 동일하다. 차이는
        세그먼트 전체를 메모리에 모은 뒤 CBC 복호화해서 쓴다는 점이다 —
        CBC는 앞 블록에 의존하므로 스트리밍 중 부분 기록을 할 수 없다.
        """
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

                buffer = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if self.state == DownloadState.WAITING:
                        return part_num
                    if self.state == DownloadState.PAUSED:
                        self.s._pause_event.wait()

                    if chunk:
                        buffer.extend(chunk)
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
                                    with self.lock:
                                        self._requeue_slow((index, segment), part_num)
                                    return part_num
                            else:
                                slow_count = 0

                # 받은 세그먼트가 온전한지 확인하고 복호화한다 (#321) — 잘린 본문이 200과
                # 맞는 Content-Length로 올 수 있다. 블록 중간에서 잘린 암호문은 복호화가
                # 키 문제로 보고 전체를 실패시키므로 복호화 전에 걸러 다시 받고, 블록
                # 경계에서 잘린 암호문은 복호화를 통과하므로 복호화한 TS로 확인한다.
                # 확인하는 순서는 구간 해석과 같은 함수에 있다(open_ts_segment, #309).
                # 온전하지 않으면(TruncatedSegmentError) 아래 except가 일시 오류로 다시 받게 한다.
                # 구간 다운로드에서 키를 아직 확인하지 못했으면 이 세그먼트가 그 확인이다 —
                # 복호화 결과가 TS로 보이지 않으면 다시 받지 않고 DecryptionError로 끝낸다
                try:
                    plain = open_ts_segment(
                        bytes(buffer),
                        getattr(response, "headers", None),
                        self._key,
                        self._segment_iv(index),
                        key_check=self._key_unconfirmed,
                    )
                except (ValueError, DecryptionError) as e:
                    # 복호화 실패는 재시도해도 낫지 않는다(키·정렬 문제). 재큐잉하면
                    # 무한 루프가 되고, 그대로 전파하면 future_dict가 정리되지 않아
                    # 실행 루프가 끝나지 않는다 — 다운로드 전체를 중단시킨다.
                    # 알리는 것은 한 번이다 — 키가 틀리면 동시에 받던 작업자가 모두 여기로 온다
                    with self.lock:
                        first = not self._decrypt_failed
                        self._decrypt_failed = True
                    if first:
                        self._fail_fatally(e, "Segment decryption failed")
                    return part_num
                self._key_unconfirmed = False

                temp_file = os.path.join(self.temp_dir, f"{index:0{self.width}d}.ts")
                with open(temp_file, "wb") as f:
                    f.write(plain)

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
