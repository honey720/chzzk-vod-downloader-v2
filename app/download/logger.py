import itertools
import os
import logging
from datetime import datetime
from typing import Optional
import config.config as config
from app.system import settings
from app.viewmodels.data import ContentItem
from core.utils.logging_setup import RedactingFormatter

# 시도마다 다른 로거 이름을 주는 순번 (#410). 이름이 같으면 시도들이 한 로거를 나눠 써, 앞 시도의
# 엔진이 남기는 줄이 새 시도의 파일에 들어가고 앞 시도가 닫으면 새 시도의 핸들러가 떨어졌다
_attempts = itertools.count(1)


class DownloadLogger:
    """
    다운로드 작업의 로깅을 담당하는 클래스
    """
    def __init__(self, log_level: int = logging.INFO):
        """
        Args:
            log_level: 로깅 레벨 (기본값: DEBUG)
        """
        self.log_level = log_level
        self.logger = None
        self.log_file = None
        self._handler = None  # 이 시도의 파일 핸들러 — 닫을 때 이것만 뗀다 (#410)
        self._setup_logging()

    def _setup_logging(self):
        """
        로깅 설정을 초기화합니다.
        """
        # 로그 디렉토리 생성
        log_dir = settings.logs_dir()
        os.makedirs(log_dir, exist_ok=True)
        
        # 로그 파일명 생성 (현재 시간 포함). 같은 초에 시도가 또 시작되면(중지 → 곧바로 다시
        # 시작) 뒤에 _2 · _3을 붙인다 — 두 시도가 한 파일에 쓰지 않게 (#410)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        stem = os.path.join(log_dir, f'download_{timestamp}')
        self.log_file = f'{stem}.log'
        number = 2
        while os.path.exists(self.log_file):
            self.log_file = f'{stem}_{number}.log'
            number += 1

        # 로거 설정 — 시도마다 이름이 다른 로거다 (#410). 다른 시도의 핸들러는 건드리지 않는다
        self.logger = logging.getLogger(f'DownloadLogger.{next(_attempts)}')
        self.logger.setLevel(self.log_level)

        # 파일 핸들러 설정 (다운로드 전용 로그 파일)
        file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
        file_handler.setLevel(self.log_level)

        # 포맷터 설정 — 주소의 서명 · 토큰 값은 가린다(앱 로그와 같은 포매터, #328)
        formatter = RedactingFormatter(
            '%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s'
        )
        file_handler.setFormatter(formatter)

        # 핸들러 추가 — 콘솔 출력은 자체 핸들러 대신 공통 로깅 설정(루트 로거,
        # core/utils/logging_setup.py)으로 전파되어 처리된다 (#30, #62)
        self.logger.addHandler(file_handler)
        self._handler = file_handler

    def save_and_close(self):
        """
        로그를 저장하고 로깅을 종료합니다.

        이 시도의 파일 핸들러만 닫고 뗀다 — 다른 시도의 로그는 그대로 쓰인다 (#410).
        다시 불러도 아무 일도 하지 않는다.
        """
        if self.logger:
            if self._handler is not None:
                self._handler.close()
                self.logger.removeHandler(self._handler)
                self._handler = None

            # 로그 파일 경로 로깅 — 파일 핸들러를 뗀 뒤라 앱 로그 · 콘솔에만 남는다
            self.logger.info(f"Log saved to: {self.log_file}")
            self.logger = None

    def debug(self, message: str):
        """디버그 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.debug(message)

    def info(self, message: str):
        """정보 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.info(message)

    def warning(self, message: str):
        """경고 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.warning(message)

    def error(self, message: str):
        """에러 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.error(message)
    
    def exception(self, message: str):
        """예외 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.exception(message)

    def critical(self, message: str):
        """치명적 에러 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.critical(message)
    
    def log_download_info(self, item: ContentItem, section_paths: tuple[str, ...] = ()):
        """다운로드 시작 정보 블록을 로깅합니다.

        Args:
            item: 다운로드할 컨텐츠
            section_paths: 구간 다운로드의 구간 파일 경로(구간 목록 순서). 비어 있으면
                전체 다운로드다. 구간 다운로드는 output_path에 파일을 만들지 않으므로
                output_path 대신 이 경로들을 적는다 (#309)
        """
        # 앱 버전을 시작 정보 블록 첫 줄에 남긴다 (#110 — 제보 분석·버전 간 비교용)
        self.info(f"app_version: {config.get_app_version()}")
        self.info(f"content_type: {item.content_type}")
        self.info(f"title: {item.title}")
        self.info(f"channel_name: {item.channel_name}")
        self.info(f"created_date: {item.live_open_date}")
        self.info(f"duration: {item.duration}")
        # 짧은 변이 같은 항목이 둘인 영상에서 원본을 골랐으면 "720(원본)" — 파일명과 같은
        # 조건이다(#318). 그 밖은 숫자만이다. 표시가 없는 아이템(스텁 등)도 숫자만
        self.info(f"resolution: {item.resolution}{getattr(item, 'resolution_tag', '')}")
        self.info(f"total_size: {item.total_size}")
        # 경로는 repr로 남긴다 (#148) — U+00A0 같은 공백 유사 문자가 그냥
        # 찍으면 U+0020과 육안 구분되지 않아 제보 진단이 불가능하다(#144 실측)
        if section_paths:
            for number, path in enumerate(section_paths, start=1):
                self.info(f"output_path_{number}: {path!r}")
        else:
            self.info(f"output_path: {item.output_path!r}")
        self.info(f"download_path: {item.download_path!r}")

    def log_download_start(self, total_size: int, part_size: int, segments: int, initial_threads: int):
        """다운로드 시작 정보를 로깅합니다."""
        self.info(f"Download started - Total size: {total_size} bytes")
        self.info(f"Part size: {part_size} bytes")
        self.info(f"Segments: {segments}")
        self.info(f"Initial threads: {initial_threads}")

    def log_thread_start(self, thread_id: int, start: int, end: int):
        """스레드 시작 정보를 로깅합니다."""
        self.debug(f"Thread {thread_id} started - Range: {start}-{end}")

    def log_m3u8_thread_start(self, thread_id: int, segment_url: str):
        """스레드 시작 정보를 로깅합니다."""
        self.debug(f"Thread {thread_id} started - Segment URL: {segment_url}")

    def log_thread_complete(self, thread_id: int, downloaded_size: int):
        """스레드 완료 정보를 로깅합니다."""
        self.debug(f"Thread {thread_id} completed - Downloaded: {downloaded_size} bytes")

    def log_download_progress(self, total_downloaded: int, total_size: int, speed: float):
        """다운로드 진행 상황을 로깅합니다."""
        progress = (total_downloaded / total_size) * 100
        self.debug(f"Progress: {progress:.2f}% - {total_downloaded}/{total_size} bytes - Speed: {speed:.2f} MB/s")

    def log_download_complete(self, total_time: float):
        """다운로드 완료 정보를 로깅합니다.

        주의: total_time은 전송+후처리를 합친 전체 시간이다. 줄 형식은
        요약 스크립트(scripts/summarize_download_log.py)의 파싱 계약이라
        바꾸지 않는다 — 구간 구분은 #110의 새 줄들이 담당한다.
        """
        self.info(f"Download completed in {total_time:.2f} seconds")

    # ============ 단계 경계 로그 (#110) — 새 줄만 추가, 기존 줄 형식 불변 ============

    def log_part_resume(self, part_num: int, offset: int, part_size: int):
        """파트 이어받기 시작을 로깅합니다 (#78) — 재시도가 어느 오프셋부터 받는지.

        이 줄이 없는 재시도는 파트 처음부터 받은 것이다. 이어받기가 거부되어
        처음부터 폴백한 경우는 "resume rejected" warning으로 구분된다.
        """
        self.info(f"Part {part_num} resuming from byte {offset}/{part_size}")

    def log_transfer_complete(
        self,
        elapsed: float,
        downloaded_bytes: int,
        retries: int,
        peak_threads: int,
        paused: float = 0.0,
    ):
        """전송 단계 종료를 한 줄로 로깅합니다 — 소요·총 바이트·재시도·정점 스레드.

        소요에는 일시정지한 시간이 들어 있지 않다 — 일시정지가 있었으면 그 합을 줄 끝에 따로
        적는다("(paused: 12.30s)", #309). 앞부분의 형식은 그대로다.
        """
        tail = f" (paused: {paused:.2f}s)" if paused > 0 else ""
        self.info(
            f"Transfer completed in {elapsed:.2f} seconds - Bytes: {downloaded_bytes}"
            f" - Retries: {retries} - Peak threads: {peak_threads}{tail}"
        )

    def log_prepare_complete(self, elapsed: float, note: str = ""):
        """준비 단계 종료를 로깅합니다 — 받을 것을 정하는 데 걸린 시간 (#309).

        구간 다운로드는 준비에서 moov · 플레이리스트를 받는다. note는 엔진이 덧붙이는 말이다
        ("moov reused" — 넘겨받은 moov를 다시 썼다 / "moov fetched" — 새로 받았다).
        """
        self.info(f"Prepare completed in {elapsed:.2f} seconds" + (f" - {note}" if note else ""))

    def log_file_size_check(self, filesystem, largest: int):
        """저장 폴더의 파일 시스템과 만들 파일 가운데 가장 큰 것의 크기를 로깅합니다 (#309).

        FAT 계열(FAT · FAT32)은 파일 하나가 4GiB를 넘지 못한다 — 받기 전에 이 둘로 가린다.
        filesystem은 OS가 준 이름이고, 읽지 못했으면 None이다(그때는 가리지 않는다).
        """
        self.info(
            f"File size check - Filesystem: {filesystem or 'unknown'}"
            f" - Largest file: {largest} bytes"
        )

    def log_space_check(self, stage: str, segments: int, output: int, checks):
        """받을 크기와 여유 공간을 견준 결과를 로깅합니다 — 세그먼트 다운로드와 구간 없는 mp4 (#309).

        구간 없는 mp4는 받기 전에 한 번 남긴다 — segments는 0(세그먼트가 없다), output은 파일
        크기, 본 곳은 output(저장 폴더) 하나다. 아래는 세그먼트 다운로드의 것이다.


        stage는 start(받기 전 — 선언된 비트레이트로 어림) · transfer(세그먼트를 몇 개 받은 뒤 —
        실제 평균으로 어림)다. segments는 받을 세그먼트 전체, output은 산출물의 어림 크기다.
        checks는 본 볼륨마다의 (본 곳, 필요한 여유, 여유 공간) — 본 곳은 both(임시 폴더와 저장
        폴더가 같은 볼륨) · temp · output이다. 어림이 실제와 얼마나 다른지는 이 줄의 segments를
        "Transfer completed"의 Bytes와 견주면 된다.
        """
        volumes = ", ".join(
            f"{where}: need {needed} / free {free}" for where, needed, free in checks
        )
        self.info(
            f"Space check ({stage}) - Segments: {segments} bytes - Output: {output} bytes"
            f" - {volumes or 'free space unknown'}"
        )

    def log_transfer_net(self, elapsed: float):
        """준비를 뺀 실제 전송 시간을 로깅합니다 (#309).

        "Transfer completed in"의 시간은 준비를 포함한다 — 그 줄의 뜻은 바꾸지 않고, 준비를 뺀
        값을 이 줄에 따로 남긴다. 두 줄 모두 일시정지한 시간은 들어 있지 않다.
        """
        self.info(f"Transfer without prepare: {elapsed:.2f} seconds")

    def log_transfer_reused(self, segments: int, size: int):
        """전송 단계 전에 받아 둔 것을 받지 않고 쓴 세그먼트의 수와 크기를 로깅합니다 (#376).

        받아 둔 것은 구간을 정할 때 받은 세그먼트와 이전 실행이 남긴 세그먼트다.
        "Transfer completed"의 Bytes는 전송 단계가 받은 것만이다 — 받아 둔 것은 이 줄에 따로
        남긴다. "Segments:"의 수에는 받아 둔 세그먼트가 들어 있다.
        """
        self.info(f"Stored segments used - Segments: {segments} - Bytes: {size}")

    def log_transfer_discarded(self, size: int):
        """다시 요청하며 받았다가 버린 바이트의 합을 로깅합니다 (#395).

        받다 만 세그먼트와 이어받지 못한 파트의 앞부분이다. "Transfer completed"의 Bytes는
        산출물에 남은 것만이다 — 버린 것은 이 줄에 따로 남긴다. 버린 것이 없으면 이 줄이 없다.
        """
        self.info(f"Discarded on retry - Bytes: {size}")

    def log_cut_setup(self, elapsed: float):
        """컷을 시작하기 전 색인에서 프레임 정보를 뽑는 데 걸린 시간을 로깅합니다 (#309)."""
        self.info(f"Cut frames prepared in {elapsed:.2f} seconds")

    def log_cut_stages(self, number: int, total: int, stages, paused: float = 0.0):
        """구간 하나의 컷이 단계마다 걸린 시간을 한 줄로 로깅합니다 (#309).

        단계는 probe(입력 읽기) · 조각마다 <번호>_<종류>(재인코딩 또는 복사) · audio · mux이고,
        세그먼트 방식은 앞에 join(세그먼트 잇기)이 붙는다. 실패한 컷은 실패한 단계까지 남는다.
        단계 시간에는 일시정지한 시간이 들어 있지 않다 — 일시정지가 있었으면 그 합을 끝에 따로
        적는다("paused: 12.30s").
        """
        parts = ", ".join(f"{name}: {seconds:.2f}s" for name, seconds in stages)
        total_seconds = sum(seconds for _name, seconds in stages)
        tail = f" (paused: {paused:.2f}s)" if paused > 0 else ""
        self.info(f"Cut {number}/{total} stages - {parts} = {total_seconds:.2f}s{tail}")

    def log_stopped(self, phase: str):
        """사용자가 다운로드를 중단했음을 로깅합니다 — 어느 단계였는지와 함께 (#309).

        phase는 prepare(준비) · transfer(전송) · postprocess(후처리 — 구간 다운로드는 컷)다.
        """
        self.info(f"Download stopped by user during {phase}")

    def log_stopped_after_failure(self, phase: str):
        """실패를 알린 뒤 엔진이 스스로 멈췄음을 로깅합니다 — 어느 단계였는지와 함께 (#309).

        사용자가 멈춘 것(``log_stopped``)과 가른다. 실패의 내용은 그 앞의 오류 줄에 있다.
        """
        self.info(f"Download stopped after a failure during {phase}")

    def log_postprocess_start(self, kind: str):
        """후처리 시작을 로깅합니다 (kind: remux 등)."""
        self.info(f"Postprocess started - {kind}")

    def log_postprocess_complete(self, elapsed: float, output_size: int, paused: float = 0.0):
        """후처리 종료를 로깅합니다 — 소요 시간과 최종 산출물 크기.

        소요에는 일시정지한 시간이 들어 있지 않다 — 일시정지가 있었으면 그 합을 줄 끝에 따로
        적는다("(paused: 12.30s)", #309). 앞부분의 형식은 그대로다.
        """
        tail = f" (paused: {paused:.2f}s)" if paused > 0 else ""
        self.info(
            f"Postprocess completed in {elapsed:.2f} seconds - Output size: {output_size} bytes{tail}"
        )

    def log_total_breakdown(
        self,
        transfer_elapsed: float,
        postprocess_elapsed: Optional[float],
        paused: float = 0.0,
    ):
        """전체 시간의 전송/후처리 구분을 로깅합니다.

        후처리가 없는 경로(file)는 "(no postprocess)"로 표기해 세 다운로더의
        로그가 같은 위치에서 같은 형태로 끝나게 한다 (#110). 두 값에는 일시정지한 시간이 들어
        있지 않다 — 일시정지가 있었으면 그 합을 줄 끝에 따로 적는다 (#309).
        """
        tail = f" (paused: {paused:.2f}s)" if paused > 0 else ""
        if postprocess_elapsed is None:
            self.info(
                f"Total time breakdown - Transfer: {transfer_elapsed:.2f}s (no postprocess){tail}"
            )
        else:
            total = transfer_elapsed + postprocess_elapsed
            self.info(
                f"Total time breakdown - Transfer: {transfer_elapsed:.2f}s"
                f" + Postprocess: {postprocess_elapsed:.2f}s = {total:.2f}s{tail}"
            )

    def log_error(self, error_message: str, exception: Optional[Exception] = None):
        """에러 정보를 로깅합니다."""
        if exception:
            self.error(f"{error_message} - Exception: {str(exception)}")
        else:
            self.error(error_message)

    def log_exception(self, error_message: str, exception: Optional[Exception] = None):
        """예외 정보를 로깅합니다."""
        self.exception(f"{error_message} - Exception: {str(exception)}")

    def log_thread_adjust(self, active_threads: int, avg_speed: float):
        """스레드 조정 정보를 로깅합니다."""
        self.info(f"Active threads: {active_threads} - Download speed: {avg_speed:.2f} MB/s")

    def log_thread_debug(self, active_threads: int, download_speed: float, avg_speed: float):
        """스레드 속도 정보를 로깅합니다."""
        self.debug(f"Active threads: {active_threads} - Download speed: {download_speed:.2f} MB/s - Avg speed: {avg_speed:.2f} MB/s")
