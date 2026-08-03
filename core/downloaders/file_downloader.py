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
- postprocess 없음 (베이스 기본 no-op)

스레드 스케일링 기준 속도는 베이스 기본값(4 MB/s — 구 고정 임계 4/2와 동일)을
그대로 쓴다.
"""

import os
import time as tm

import requests

from core.api.session import get_thread_session
from core.downloaders.base import BaseDownloader
from core.downloaders.ranges import decide_part_size, split_ranges
from core.models.content import Content, ContentType
from core.models.download_state import DownloadState
from core.models.plan import DownloadPlan


class FileDownloader(BaseDownloader):
    """VOD 파일을 범위 분할 멀티스레드로 다운로드하는 엔진.

    호출 규약: 소유자(서비스·스크립트)가 DownloadTaskModel.start()로 RUNNING
    전이를 마친 뒤 run()을 호출한다. run()은 완료·중단·실패까지 블로킹한다.
    """

    run_thread_name = "DownloadThread"
    worker_pool_prefix = "DownloadWorker"
    # OSError를 실패 처리에 추가한다 (#147 E1). 구 코드(요청 예외만)는 run
    # 스레드의 출력 파일 I/O 오류(이어받기 스캔·수신 준비 — 디스크 부족·
    # 마운트 해제)를 실패 처리 밖으로 흘려보냈다: 통지·로그·부분 산출물
    # 정리가 전부 생략된 채 스레드만 죽었다(#146 감사 실측). 엔진에서 잡는
    # 쪽이 _cleanup_partial까지 수행해 정리 품질이 높다. Exception 전체로
    # 넓히지 않는 것은 selections 명시 거부(NotImplementedError)의 전파를
    # 박제 계약대로 보존하기 위함이다 — 그 밖의 예상 밖 예외는 서비스의
    # 최후 방어선(_run_handle)이 실패로 환원한다.
    _failure_exceptions = (requests.RequestException, OSError)

    def __init__(self, data, logger, **callbacks):
        super().__init__(data, logger, **callbacks)
        # 재큐잉된 파트가 이미 받아 쓴 바이트 수 — 재시도가 이어받는다 (#78).
        # (start, end) → 산출물에 쓰인 바이트 수. 완료·폴백 시 지운다
        self._part_progress: dict[tuple[int, int], int] = {}

    @classmethod
    def supports(cls, content: Content) -> bool:
        """일반 VOD(video)와 클립(clip)을 처리한다 — 매니페스트가 파일 URL을 준다."""
        return content.content_type in (ContentType.CHZZK_VIDEO, ContentType.CHZZK_CLIP)

    # ============ 작업 목록·수신 준비 (구 run의 파일 고유 부분) ============

    def prepare(self, content: Content) -> DownloadPlan:
        """총 크기를 조회하고 해상도별 part_size의 바이트 범위 계획을 만든다."""
        total_size = self._get_total_size()

        # part_size 결정(해상도별 가중 적용)
        self._part_size = decide_part_size(self.s.content_type, self.s.resolution)

        # 다운로드할 구간 분할 — 총 크기를 미리 아는 경로이므로 계획에 싣는다
        return DownloadPlan(
            items=tuple(split_ranges(total_size, self._part_size)),
            total_size=total_size,
        )

    def _download_start_log_args(self) -> tuple:
        return (self.s.total_size, self._part_size, self.s.total_ranges, self.s.adjust_threads)

    def _prepare_output(self) -> None:
        """빈 파일 생성(사이즈: 0)."""
        with open(self.s.output_path, "wb"):
            pass

    def _initial_queue(self, items: list) -> list:
        """중단 이후 재시작 같은 상황을 고려해 미수신 구간만 큐에 넣는다."""
        return self._get_remaining_ranges(items)

    def _log_item_start(self, part_num: int, item) -> None:
        start, end = item
        self.logger.log_thread_start(part_num, start, end)

    def _download_item(self, item, part_num: int):
        start, end = item
        return self._download_part(start, end, part_num, self.s.total_size)

    def _cleanup_partial(self) -> None:
        """실패·중단 시 다운로드 파일 삭제."""
        if os.path.exists(self.s.output_path):
            os.remove(self.s.output_path)

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
                    self.s.base_url, headers=headers, stream=True, timeout=30
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
                part_start_time = tm.time()

                with open(self.s.output_path, "r+b") as f:
                    f.seek(range_start)
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
                                        with self.lock:
                                            self._record_partial(
                                                start, end, resume_offset + downloaded_size
                                            )
                                            self._requeue_slow((start, end), part_num)
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
            file_size = os.path.getsize(self.s.output_path)
        except OSError:
            file_size = -1
        if file_size < start + recorded:
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
        response = get_thread_session().head(self.s.base_url)
        response.raise_for_status()
        size = int(response.headers.get("content-length", 0))
        if size == 0:
            resp = get_thread_session().get(self.s.base_url, stream=True)
            resp.raise_for_status()
            size = int(resp.headers.get("content-length", 0))
            resp.close()
        return size

    def _get_remaining_ranges(self, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """
        중단 이후 재시작 같은 상황 고려(현재 파일크기 등을 바탕으로),
        아직 다운로드되지 않은 구간만 남겨 반환한다.
        """
        with open(self.s.output_path, "r+b") as f:
            f.seek(0, 2)
            file_size = f.tell()

        remaining = []
        for start, end in ranges:
            if start >= file_size or end >= file_size:
                remaining.append((start, end))
        return remaining
