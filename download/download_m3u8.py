import requests
import threading
import time as tm
import os
import shutil
import config.config as config
from urllib.parse import urljoin

from PySide6.QtCore import QThread, Signal
from concurrent.futures import ThreadPoolExecutor
from download.task import DownloadTask
from download.state import DownloadState
from content.network import NetworkManager

class DownloadM3U8Thread(QThread):
    """
    m3u8 파일을 multi-thread로 다운로드하는 작업 스레드 클래스
    """
    completed = Signal()
    stopped = Signal(str)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self.s = self.task.data
        self.future_dict = {}
        self.lock = self.task.lock
        self.logger = self.task.logger

    def run(self):
        """
        스레드가 시작될 때 자동으로 호출되는 메서드.
        실제 다운로드 파이프라인이 여기서 진행된다.
        """
        try:
            data = config.load_config().get("cookies", {})
            cookies = {
                'NID_AUT': data.get("NID_AUT", ""),
                'NID_SES': data.get("NID_SES", "")
            }
            content_type, content_no = NetworkManager.extract_content_no(self.s.vod_url)
            video_id, in_key, adult, vodStatus, liveRewindPlaybackJson, metadata = NetworkManager.get_video_info(content_no, cookies)
            self.s.base_url = NetworkManager.get_video_m3u8_base_url(liveRewindPlaybackJson, self.s.resolution)
            threading.current_thread().name = "DownloadM3U8Thread"  # 스레드 시작 시 이름 재설정
            self.s.start_time = tm.time()

            response = requests.get(self.s.base_url)
            response.raise_for_status()
            lines = response.text.splitlines()
            segments = [line for line in lines if line and not line.startswith("#")]
            init_segment = None
            self.s.merged_segments = 0
            
            self.s.max_threads = self.s.total_ranges = len(segments)
            self.width = len(str(self.s.total_ranges))
            self.s.adjust_threads = min(self.s.adjust_threads, self.s.max_threads)
            self.s.threads_progress = [0] * self.s.total_ranges
            self.logger.log_download_start(0, 0, self.s.total_ranges, self.s.adjust_threads)


            # 세그먼트 저장용 임시 폴더 경로 설정
            self.temp_dir = os.path.join(os.path.dirname(self.s.output_path), "CVDv2_temp")
            # 세그먼트 저장용 임시 폴더가 있다면 내용 포함 삭제
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            # 세그먼트 저장용 임시 폴더 생성
            os.makedirs(self.temp_dir)

            for line in lines:
                if line.startswith("#EXT-X-MAP:"):
                    init_segment = line.split("URI=")[1].strip('"')
            init_url = urljoin(self.s.base_url, init_segment)
            init_segment_path = os.path.join(self.temp_dir, f"{0:0{self.width}d}.m4s")
            # 초기화 세그먼트 다운로드
            with open(init_segment_path, 'wb') as f:
                f.write(requests.get(init_url).content)

            with ThreadPoolExecutor(max_workers=self.s.max_threads, thread_name_prefix="DownloadM3U8Worker") as executor:
                self.s.remaining_ranges = list(enumerate(segments))
                # 재사용 시 초기화 필수
                with self.lock:
                    self.s.future_count = 0
                    self.future_dict = {}

                while not self.task.state == DownloadState.WAITING:
                    while self.s.future_count < self.s.adjust_threads and self.s.remaining_ranges:
                        for part_num in range(self.s.adjust_threads):
                            if not self.s.remaining_ranges:
                                break
                            with self.lock:
                                if part_num not in self.future_dict:
                                    segment_index, segment = self.s.remaining_ranges.pop(0)
                                    self.s.future_count += 1
                                    self.logger.log_m3u8_thread_start(part_num, segment)
                                    future = executor.submit(
                                        self._download_segment,
                                        index=segment_index,
                                        segment=segment,
                                        part_num=part_num,
                                        total_ranges=self.s.total_ranges
                                    )
                                    future.add_done_callback(self._download_completed_callback)
                                    self.future_dict[part_num] = (segment, future)

                    # (2) 주기적으로 상태 확인 (non-blocking)
                    tm.sleep(0.1)

                    # (3) 남은 작업이 없고 스레드도 없으면 종료
                    if not self.s.remaining_ranges and not self.future_dict:
                        break
                    
                if self.task.state == DownloadState.RUNNING:
                    # (4) 다운로드 완료 후 파일 합치기
                    self.task.item.post_process = True
                    with open(self.s.output_path, 'wb') as final_f:
                        segment_files = sorted(os.listdir(self.temp_dir))
                        for seg_file in segment_files:
                            # 다운로드 중지 상태라면 중단
                            if self.task.state == DownloadState.RUNNING:
                                seg_path = os.path.join(self.temp_dir, seg_file)
                                with open(seg_path, 'rb') as seg_f:
                                    while True:
                                        chunk = seg_f.read(8192)
                                        if not chunk:
                                            break
                                        final_f.write(chunk)
                                        # 일시정지 상태라면 대기
                                        if self.task.state == DownloadState.PAUSED:
                                            self.s._pause_event.wait()
                                # 세그먼트 파일 합친 후 삭제
                                os.remove(seg_path)
                                self.s.merged_segments += 1
                                
                    self.s.end_time = tm.time()
                    total_time = self.s.end_time - self.s.start_time
                    self.logger.log_download_complete(total_time)
                    self.logger.save_and_close()
                    self.completed.emit()
                
                # (5) 임시 폴더 삭제
                shutil.rmtree(self.temp_dir)

        except Exception as e:
            # 오류 발생 시 임시 파일과 다운로드 파일 삭제
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            if os.path.exists(self.s.output_path):
                os.remove(self.s.output_path)
                self.task.item.post_process = False
            self.stopped.emit(self.tr("Download failed"))
            self.logger.log_error("Download failed", e)
            self.logger.save_and_close()

        # 사용자가 강제로 중단한 경우 임시 파일과 다운로드 파일 삭제
        if self.task.state == DownloadState.WAITING:
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            if os.path.exists(self.s.output_path):
                os.remove(self.s.output_path)
            self.task.item.post_process = False
                
    # ============ 다운로드 동작 관련 메서드들 ============

    def _download_segment(self, index, segment, part_num, total_ranges):
        """
        개별 세그먼트 다운로드(재시도 포함)
        """
        slow_count = 0
        downloaded_size = 0
        segment_url = urljoin(self.s.base_url, segment)
        while not self.task.state == DownloadState.WAITING:
            try:
                response = requests.get(segment_url, stream=True, timeout=30)
                response.raise_for_status()
                part_start_time = tm.time()

                temp_file = os.path.join(self.temp_dir, f"{index+1:0{self.width}d}.m4v")
                with open(temp_file, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.task.state == DownloadState.WAITING:
                            return part_num
                        if self.task.state == DownloadState.PAUSED:
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
                                            self._download_stop_callback(index, segment, part_num)
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
                    self._download_failed_callback(index, segment, part_num)
                self.logger.log_error(f"Part {part_num} download failed", e)
                return part_num
                

    def _check_speed_and_update_progress(self, part_num, downloaded_size, total_size, speed_kb_s):
        """
        스레드가 다운로드 중일 때 속도 체크 및 진행 상황 업데이트.
        """
        with self.lock:
            self.s.threads_progress[part_num] = downloaded_size
            self.update_progress()

    # ============ 다운로드 조정 및 콜백 메서드 ============

    def _download_completed_callback(self, future):
        """
        특정 future(스레드)가 끝났을 때 호출되는 콜백.
        """
        try:
            part_num = future.result()
            # part_num 식별 후 future_dict에서 제거
            with self.lock:
                if part_num in self.future_dict:
                    del self.future_dict[part_num]
                    self.s.future_count -= 1
            self.update_progress()  # 즉각적 진행도 반영

        except Exception as e:
            # 일부 스레드가 오류로 중단된 경우
            self.stopped.emit(self.tr("Download failed"))
            self.logger.log_error("Thread failed", e)

    def _download_failed_callback(self, index, segment, part_num):
        """
        예외 발생 시 파일 구간을 다시 다운로드할 수 있도록 remaining_ranges에 등록.
        """
        self.s.failed_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append((index, segment))

    def _download_stop_callback(self, index, segment, part_num):
        """
        특정 스레드를 중도 중단하고, 해당 구간을 재시작하도록 설정.
        """
        self.s.restart_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append((index, segment))
        self.logger.warning(f"Part {part_num} stopped due to slow speed, will retry")

    # ============ 진행 상황 업데이트 / 제어 메서드 ============

    def update_progress(self):
        """
        다운로드된 총량을 저장한다.
        """
        if self.task.state in [DownloadState.PAUSED, DownloadState.WAITING]:    # 중단 플래그 확인
            return
        
        active_downloaded_size = sum(self.s.threads_progress)
        self.s.total_downloaded_size = self.s.completed_progress + active_downloaded_size