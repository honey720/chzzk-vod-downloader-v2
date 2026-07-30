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
"""

import os
import shutil
import time as tm
from urllib.parse import urljoin

import requests

from core.api.session import get_thread_session
from core.downloaders.base import BaseDownloader
from core.models.content import Content, ContentType
from core.models.download_state import DownloadState
from core.models.plan import DownloadPlan
from core.utils.paths import temp_dir_for


class M3U8Downloader(BaseDownloader):
    """m3u8 VOD를 세그먼트 단위 멀티스레드로 다운로드·병합하는 엔진.

    호출 규약: 소유자(서비스·스크립트)가 data.base_url에 m3u8 플레이리스트 URL을
    채우고 DownloadTaskModel.start()로 RUNNING 전이를 마친 뒤 run()을 호출한다.
    run()은 완료·중단·실패까지 블로킹한다.
    """

    run_thread_name = "DownloadM3U8Thread"
    worker_pool_prefix = "DownloadM3U8Worker"
    requires_base_url_resolution = True
    # 구 코드와 동일하게 모든 예외를 실패로 처리한다 (플레이리스트 파싱 실패 포함)
    _failure_exceptions = (Exception,)

    def __init__(self, data, logger, **callbacks):
        super().__init__(data, logger, **callbacks)
        # 세그먼트 저장용 임시 폴더 경로 (실패 정리 경로가 참조하므로 실행 전에 확정).
        # 산출물 파일명에서 파생해 다운로드 간 폴더 공유·상호 삭제를 막는다 (#105)
        self.temp_dir = temp_dir_for(self.s.output_path)

    @classmethod
    def supports(cls, content: Content) -> bool:
        """라이브 다시보기(m3u8) VOD를 처리한다."""
        return content.content_type is ContentType.CHZZK_VIDEO_M3U8

    # ============ 작업 목록·수신 준비 (구 run의 m3u8 고유 부분) ============

    def prepare(self, content: Content) -> DownloadPlan:
        """플레이리스트를 받아 (index, 세그먼트) 목록의 계획을 만든다."""
        response = get_thread_session().get(self.s.base_url)
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

    def _download_start_log_args(self) -> tuple:
        # m3u8은 전체 크기·파트 크기를 미리 알 수 없다 (구 코드와 동일하게 0)
        return (0, 0, self.s.total_ranges, self.s.adjust_threads)

    def _prepare_output(self) -> None:
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
            f.write(get_thread_session().get(init_url).content)

    def _log_item_start(self, part_num: int, item) -> None:
        _index, segment = item
        self.logger.log_m3u8_thread_start(part_num, segment)

    def _download_item(self, item, part_num: int):
        index, segment = item
        return self._download_segment(
            index=index, segment=segment, part_num=part_num, total_ranges=self.s.total_ranges
        )

    def _cleanup_partial(self) -> None:
        """실패·중단 시 임시 폴더와 다운로드 파일 삭제."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        if os.path.exists(self.s.output_path):
            os.remove(self.s.output_path)

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
        """
        self._on_merge_start()
        segment_files = sorted(os.listdir(self.temp_dir))
        self._remux_streamed([os.path.join(self.temp_dir, f) for f in segment_files])

    # ============ 다운로드 동작 관련 메서드들 ============

    def _download_segment(self, index: int, segment: str, part_num: int, total_ranges: int):
        """
        개별 세그먼트 다운로드(재시도 포함)
        """
        slow_count = 0
        downloaded_size = 0
        segment_url = urljoin(self.s.base_url, segment)
        while not self.state == DownloadState.WAITING:
            try:
                # 스레드로컬 세션으로 같은 워커의 세그먼트 요청 간 연결을 재사용한다 (#31)
                response = get_thread_session().get(segment_url, stream=True, timeout=30)
                response.raise_for_status()
                part_start_time = tm.time()

                temp_file = os.path.join(self.temp_dir, f"{index + 1:0{self.width}d}.m4v")
                with open(temp_file, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.state == DownloadState.WAITING:
                            return part_num
                        if self.state == DownloadState.PAUSED:
                            self.s._pause_event.wait()

                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            elapsed = tm.time() - part_start_time

                            if elapsed > 0:
                                speed_kb_s = downloaded_size / elapsed / 1024
                                self._check_speed_and_update_progress(
                                    part_num, downloaded_size, total_ranges, speed_kb_s
                                )
                                if speed_kb_s < 100:
                                    slow_count += 1
                                    if slow_count > 5:
                                        # 속도가 너무 느리면 스레드 재시작
                                        with self.lock:
                                            self._requeue_slow((index, segment), part_num)
                                        return part_num
                                else:
                                    slow_count = 0

                # 성공적으로 마무리된 경우
                with self.lock:
                    self.s.completed_threads += 1
                    self.s.completed_progress += downloaded_size
                    self.s.threads_progress[part_num] = 0
                self.logger.log_thread_complete(part_num, downloaded_size)
                return part_num

            except (requests.RequestException, requests.Timeout) as e:
                with self.lock:
                    self._requeue_failed((index, segment), part_num, e)
                self.logger.log_error(f"Part {part_num} download failed", e)
                return part_num


