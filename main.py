import sys
import re
import requests
import xml.etree.ElementTree as ET
import threading
import resources
import os
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLineEdit, QPushButton, QLabel, QFileDialog, QProgressBar, QHBoxLayout, QMessageBox)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QPixmap, QIcon
from io import BytesIO
from time import time, strftime, gmtime
from concurrent.futures import ThreadPoolExecutor, as_completed


class DownloadThread(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(str)
    paused = pyqtSignal()
    resumed = pyqtSignal()
    stopped = pyqtSignal(str)
    update_threads = pyqtSignal(int, int, int, int)
    update_time = pyqtSignal(str, str)
    update_active_threads = pyqtSignal(int)
    update_avg_speed = pyqtSignal(float)

    def __init__(self, video_url, output_path, height, initial_threads=min(32, os.cpu_count() + 4)):
        super().__init__()
        self.video_url = video_url
        self.output_path = output_path
        self.height = height
        self._is_paused = False
        self._is_stopped = False
        self.lock = threading.Lock()
        self.completed_threads = 0
        self.failed_threads = 0
        self.restart_threads = 0
        self.completed_progress = 0
        self.adjust_threads = initial_threads
        self.max_threads = initial_threads
        self.total_active_speed = 0
        # self.thread_speed = [] # Debugging
        self.future_count = 0
        self.future_dict = {}
        self.remaining_ranges = []

    def run(self):
        try:
            self.start_time = time()
            response = requests.head(self.video_url)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))

            part_size = 1024 * 1024  # 1MB
            
            if self.height == '144':
                part_size *= 1  # 1MB
            elif self.height == '360' or '480':
                part_size *= 2 # 2MB
            elif self.height == '720':
                part_size *= 5  # 5MB
            else:
                part_size *= 10  # 10MB
            
            ranges = [(i * part_size, min((i + 1) * part_size - 1, total_size - 1)) for i in range((total_size + part_size - 1) // part_size)]
            self.total_ranges = len(ranges)
            self.thread_progress = [0] * len(ranges)
            # self.thread_speed = [0] * len(ranges) # Debugging
            self.max_threads = len(ranges)

            with open(self.output_path, 'wb') as f:
                pass

            def download_part(start, end, part_num):
                slow_download_speed_count = 0
                # self.thread_speed[part_num] = 0 # Debugging
                while not self._is_stopped :
                    try:
                        # print(f"시작 {part_num} , {start} , {end}") # Debugging
                        headers = {'Range': f'bytes={start}-{end}'}
                        response = requests.get(self.video_url, headers=headers, stream=True)
                        response.raise_for_status()
                        downloaded_size = 0
                        part_start_time = time()
                        with open(self.output_path, 'r+b') as f:
                            f.seek(start)
                            for chunk in response.iter_content(chunk_size=8192):
                                if self._is_stopped:
                                    return
                                while self._is_paused:
                                    self.paused.emit()
                                    self.sleep(1)
                                if chunk:
                                    f.write(chunk)
                                    downloaded_size += len(chunk)
                                    elapsed_time = time() - part_start_time
                                    if elapsed_time > 0:
                                        with self.lock:
                                            # print(f"{part_num} 작업 중...") # Debugging
                                            speed = downloaded_size / elapsed_time / 1024  # KB/s
                                            if speed < 100:
                                                if slow_download_speed_count == 5:
                                                    # print(f"스레드 {part_num} 속도가 느려 재시작합니다.")
                                                    download_stop_callback(start, end, part_num)
                                                    return part_num
                                                else:
                                                    slow_download_speed_count += 1
                                            else:
                                                slow_download_speed_count = 0
                                            # self.thread_speed[part_num] = speed  # 스레드 속도 업데이트 # Debugging
                                            self.thread_progress[part_num] = downloaded_size
                                            self.update_progress(total_size)  # 진행 상황을 더 자주 업데이트, 이것 때문에 다운 속도 저하됨. 아마 함수 자체에서 리소스를 많이 먹는 듯
                                    if downloaded_size >= end - start + 1:
                                        break
                            with self.lock:
                                # print(f"{part_num} 스레드 다운로드 완료") # Debugging
                                self.completed_threads += 1
                                self.completed_progress += downloaded_size
                                self.thread_progress[part_num] = 0  # 스레드 진행 상황 초기화
                                # self.thread_speed[part_num] = 0 # Debugging
                        return part_num
                    except requests.RequestException as e:
                        with self.lock:
                            # print(f"다운로드 실패 (스레드 {part_num}, {self.thread_speed[part_num]}): {e}") # Debugging
                            download_failed_callback(start, end, part_num)

            def adjust_threads():
                adjust_count = 0
                while not self._is_stopped and len(self.remaining_ranges) != 0:
                    if self._is_paused:
                        continue

                    threading.Event().wait(1)
                    # print([s for s in self.thread_speed if s > 0]) # Debugging

                    average_active_speed = self.total_active_speed / self.future_count if self.future_count > 0 else 0

                    if average_active_speed > 2:
                        adjust_count += 1
                    elif average_active_speed < 1:
                        adjust_count -= 1
                    else:
                        if adjust_count > 0:
                            adjust_count -= 1
                        elif adjust_count < 0:
                            adjust_count += 1
                    
                    if adjust_count > 4:
                        self.adjust_threads = min(self.max_threads, self.adjust_threads * 2)
                        adjust_count = 0
                        # print(self.adjust_threads) # Debugging
                    elif adjust_count < -4:
                        self.adjust_threads = max(1, self.adjust_threads // 2)
                        adjust_count = 0
                        # print(self.adjust_threads) # Debugging
                # print("조정 중지") # Debugging

            def get_remaining_ranges():
                with open(self.output_path, 'r+b') as f:
                    f.seek(0, 2)  # 파일의 끝으로 이동
                    file_size = f.tell()
                self.remaining_ranges = []
                for start, end in ranges:
                    if start >= file_size or end >= file_size:
                        self.remaining_ranges.append((start, end))
                return self.remaining_ranges
            
            def download_failed_callback(start, end, part_num):
                self.failed_threads += 1
                self.thread_progress[part_num] = 0  # 스레드 진행 상황 초기화
                # self.thread_speed[part_num] = 0 # Debugging
                self.remaining_ranges.append((start, end))

            def download_stop_callback(start, end, part_num):
                self.restart_threads += 1
                self.thread_progress[part_num] = 0  # 스레드 진행 상황 초기화
                # self.thread_speed[part_num] = 0 # Debugging
                self.remaining_ranges.append((start, end))
            
            def download_completed_callback(future):
                try:
                    future.result()
                    for part_num, (start, end, f) in self.future_dict.items():
                        if f == future:
                            # print(f"끝남. {part_num}") # Debugging
                            del self.future_dict[part_num]
                            self.future_count -= 1
                            self.update_progress(total_size)  # 진행 상황을 더 자주 업데이트
                            self.update_active_threads.emit(self.future_count)  # 활성 스레드 수
                            self.update_threads.emit(self.completed_threads, self.total_ranges, self.failed_threads, self.restart_threads)
                            break
                except Exception as e:
                    # print(f"다운로드 실패: 일부 스레드가 오류로 인해 중단되었습니다. {e}")
                    self._is_stopped = True
                    self.stopped.emit("다운로드 실패")

            with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                self.remaining_ranges = get_remaining_ranges()
                adjust_thread = threading.Thread(target=adjust_threads)
                adjust_thread.start()

                futures = []

                while not self._is_stopped:
                    while self.future_count < self.adjust_threads and self.remaining_ranges:
                        for part_num in range(0, self.adjust_threads):
                            if len(self.remaining_ranges) == 0:
                                break
                            with self.lock:  # 임계 구역 보호
                                if self.future_dict.get(part_num) is None:
                                    start, end = self.remaining_ranges.pop(0)
                                    # print(f"시작.{part_num}")
                                    self.future_count += 1
                                    future = executor.submit(download_part, start, end, part_num)
                                    futures.append(future)
                                    self.future_dict[part_num] = (start, end, future)
                                    self.update_active_threads.emit(self.future_count)  # 활성 스레드 수
                    
                    for future in as_completed(futures):
                        future.add_done_callback(download_completed_callback)
                        futures.remove(future)
                        break

                    if not self.remaining_ranges and not self.future_dict:
                        break

                if not self._is_stopped:
                    self.completed.emit("다운로드 완료!")
                else:
                    self.stopped.emit("다운로드 중지됨")

        except requests.RequestException as e:
            # print(f"다운로드 실패: {e}") # Debugging
            self._is_stopped = True
            self.stopped.emit("다운로드 실패")
        
    def update_progress(self, total_size):
        active_downloaded_size = sum(self.thread_progress)
        total_downloaded_size = self.completed_progress + active_downloaded_size

        elapsed_time = time() - self.start_time
        elapsed_time_str = strftime('%H:%M:%S', gmtime(elapsed_time))

        total_active_speed = total_downloaded_size / elapsed_time / 1024 / 1024  # KB/s에서 MB/s로 변환
        self.total_active_speed = total_active_speed
        average_active_speed = total_active_speed / self.future_count if self.future_count > 0 else 0
        progress = int((total_downloaded_size / total_size) * 100)
        status_message = f"{total_downloaded_size / (1024 * 1024):.2f}MB/{total_size / (1024 * 1024):.2f}MB ({total_active_speed:.1f} MB/s)"
         
        if total_active_speed > 0:
            remaining_time = (total_size - total_downloaded_size) / (total_active_speed * 1024 * 1024)  # 남은 시간 계산
            completion_time = elapsed_time + remaining_time
            completion_time_str = strftime('%H:%M:%S', gmtime(completion_time))
        else:
            completion_time_str = "N/A"
            
        self.progress.emit(progress, status_message)
        self.update_time.emit(elapsed_time_str, completion_time_str)
        self.update_avg_speed.emit(average_active_speed)
        
    def pause(self):
        self._is_paused = True

    def resume(self):
        self._is_paused = False

    def stop(self):
        self._is_paused = False
        self._is_stopped = True
    
class VodDownloader(QWidget):
    def __init__(self):
        super().__init__()
        self.downloadThread = None
        self.metadata = None
        self.initUI()

    def initUI(self):
        layout = QVBoxLayout()

        self.urlInput = QLineEdit(self)
        self.urlInput.setPlaceholderText("치지직 URL을 입력하세요.")
        layout.addWidget(self.urlInput)

        self.fetchButton = QPushButton('Fetch Resolutions', self)
        self.fetchButton.clicked.connect(self.onFetch)
        layout.addWidget(self.fetchButton)

        self.toggleButton = QPushButton('Show Cookies', self)
        self.toggleButton.clicked.connect(self.toggleCookies)
        layout.addWidget(self.toggleButton)

        self.nidaut = QLineEdit(self)
        self.nidaut.setPlaceholderText("NID_AUT 쿠키 값을 입력하세요.")
        self.nidaut.setVisible(False)
        layout.addWidget(self.nidaut)

        self.nidses = QLineEdit(self)
        self.nidses.setPlaceholderText("NID_SES 쿠키 값을 입력하세요.")
        self.nidses.setVisible(False)
        layout.addWidget(self.nidses)

        self.cookiehelp = QLabel("<a href='#'>쿠키를 찾으시나요?</a>", self)
        self.cookiehelp.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.cookiehelp.linkActivated.connect(self.showPopup)
        self.cookiehelp.setVisible(False)
        layout.addWidget(self.cookiehelp)

        self.channelNameLabel = self.create_label(layout)
        self.channelImageLabel = self.create_image_label(layout, 100, 100)
        self.titleLabel = self.create_label(layout)
        self.durationLabel = self.create_label(layout)
        self.categoryLabel = self.create_label(layout)
        self.liveOpenDateLabel = self.create_label(layout)
        self.thumbnailLabel = self.create_image_label(layout, 256, 144)
        self.linkStatusLabel = self.create_label(layout, '')
        self.resolutionLabel = self.create_label(layout, 'Available Resolutions:')

        self.resolutionButtonsLayout = QVBoxLayout()
        layout.addLayout(self.resolutionButtonsLayout)

        self.maxThreadsLabel = self.create_label(layout, 'Current Threads: 0')
        self.avgSpeedLabel = self.create_label(layout, 'Average Speed: 0 MB/s')
        self.progressBar = QProgressBar(self)
        layout.addWidget(self.progressBar)
        self.downloadStatusLabel = self.create_label(layout, '')

        time_layout = QHBoxLayout()
        self.timeLabel = self.create_label(time_layout, '')
        layout.addLayout(time_layout)

        self.pauseResumeButton = QPushButton('Pause', self)
        self.pauseResumeButton.clicked.connect(self.onPauseResume)
        self.pauseResumeButton.setEnabled(False)
        layout.addWidget(self.pauseResumeButton)

        self.stopButton = QPushButton('Stop', self)
        self.stopButton.clicked.connect(self.onStop)
        self.stopButton.setEnabled(False)
        layout.addWidget(self.stopButton)

        self.threadStatusLabel = self.create_label(layout, '')

        self.setLayout(layout)
        self.setWindowTitle('치지직 VOD 다운로더')
        self.setWindowIcon(QIcon('chzzk.ico'))
        self.setGeometry(300, 300, 300, 300)
        self.show()

    def toggleCookies(self):
        current_visibility = self.nidaut.isVisible()
        self.nidaut.setVisible(not current_visibility)
        self.nidses.setVisible(not current_visibility)
        self.cookiehelp.setVisible(not current_visibility)
        if current_visibility:
            self.toggleButton.setText('Show Cookies')
        else:
            self.toggleButton.setText('Hide Cookies')

    def showPopup(self):
        link = "https://chzzk.naver.com"
        msg = "치지직 쿠키 얻는 방법<br><br>1. <a href='%s'>치지직</a>에 로그인 하세요. <br>2. F12를 눌러 개발자 도구를 열어주세요. <br>3. Application 탭에서 Cookies > https://chzzk.naver.com을 클릭하세요. <br>4. \'NID_AUT\', \'NID_SES\' Name의 Value 값을 붙여 넣으세요." % link
        QMessageBox.information(self, "치지직 vod 다운로더 도우미", msg)

    def create_label(self, layout, text=''):
        label = QLabel(text, self)
        layout.addWidget(label)
        return label

    def create_image_label(self, layout, width, height):
        label = QLabel(self)
        label.setScaledContents(True)
        label.setFixedSize(width, height)
        layout.addWidget(label)
        return label

    def clear_resolutions(self):
        while self.resolutionButtonsLayout.count():
            child = self.resolutionButtonsLayout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def set_resolution_buttons_enabled(self, enabled):
        for i in range(self.resolutionButtonsLayout.count()):
            self.resolutionButtonsLayout.itemAt(i).widget().setEnabled(enabled)

    def clear_metadata_display(self):
        self.channelNameLabel.clear()
        self.titleLabel.clear()
        self.durationLabel.clear()
        self.categoryLabel.clear()
        self.liveOpenDateLabel.clear()
        self.channelImageLabel.clear()
        self.thumbnailLabel.clear()
        self.maxThreadsLabel.clear()
        self.progressBar.setValue(0)
        self.downloadStatusLabel.clear()
        self.timeLabel.clear()
        self.threadStatusLabel.clear()

    def onFetch(self):
        self.clear_metadata_display()
        self.clear_resolutions()
        vod_url = self.urlInput.text()
        cookies = {
            'NID_AUT': self.nidaut.text(),
            'NID_SES': self.nidses.text()
        }

        try:
            self.linkStatusLabel.setText('Fetching resolutions...')

            video_no = self.extract_video_no(vod_url)
            if not video_no:
                raise ValueError("Invalid VOD URL")

            video_id, in_key, metadata = self.get_video_info(video_no, cookies)
            if not video_id or not in_key:
                raise ValueError("Invalid cookies value")

            unique_reps = self.get_dash_manifest(video_id, in_key)
            if not unique_reps:
                raise ValueError("Failed to get DASH manifest")

            self.metadata = metadata
            self.display_metadata(metadata)
            for width, height, base_url in unique_reps:
                self.add_representation_button(width, height, base_url)

            self.linkStatusLabel.setText('Resolutions fetched successfully.')

        except Exception as e:
            self.linkStatusLabel.setText(f'오류 발생: {e}')

    def extract_video_no(self, vod_url):
        if not vod_url.startswith("http://") and not vod_url.startswith("https://"):
            vod_url = "https://" + vod_url
        match = re.match(r'https?://chzzk\.naver\.com/video/(?P<video_no>\d+)', vod_url)
        if match:
            return match.group("video_no")
        return None

    def get_video_info(self, video_no, cookies):
        api_url = f"https://api.chzzk.naver.com/service/v2/videos/{video_no}"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(api_url, cookies=cookies, headers=headers)
        response.raise_for_status()
        content = response.json().get('content', {})
        video_id = content.get('videoId')
        in_key = content.get('inKey')

        metadata = {
            'title': content.get('videoTitle', 'Unknown Title'),
            'thumbnailImageUrl': content.get('thumbnailImageUrl', 'Unknown URL'),
            'videoCategoryValue': content.get('videoCategoryValue', 'Unknown Category'),
            'channelName': content.get('channel', {}).get('channelName', 'Unknown Channel'),
            'channelImageUrl': content.get('channel', {}).get('channelImageUrl', 'Unknown URL'),
            'liveOpenDate': content.get('liveOpenDate', 'Unknown Date'),
            'duration': content.get('duration', 0)
        }

        return video_id, in_key, metadata

    def get_dash_manifest(self, video_id, in_key):
        manifest_url = f"https://apis.naver.com/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
        headers = {"Accept": "application/dash+xml"}
        response = requests.get(manifest_url, headers=headers)
        response.raise_for_status()
        root = ET.fromstring(response.text)

        unique_reps = set()
        ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011", "nvod": "urn:naver:vod:2020"}
        for rep in root.findall(".//mpd:Representation", namespaces=ns):
            width = rep.get('width')
            height = rep.get('height')
            base_url = rep.find(".//mpd:BaseURL", namespaces=ns).text
            # print(width, height, base_url) #  Debugging
            if (width, height) not in [(w, h) for w, h, b in unique_reps]:
                # print("생성중") #  Debugging
                unique_reps.add((width, height, base_url))
        return unique_reps

    def display_metadata(self, metadata):
        title = metadata.get('title', 'Unknown Title')
        thumbnail_url = metadata.get('thumbnailImageUrl', 'Unknown URL')
        category = metadata.get('videoCategoryValue', 'Unknown Category')
        channel_name = metadata.get('channelName', 'Unknown Channel')
        channel_image_url = metadata.get('channelImageUrl', 'Unknown URL')
        live_open_date = metadata.get('liveOpenDate', 'Unknown Date')
        duration = metadata.get('duration', 0)

        duration_str = f"{duration // 3600}시간 {(duration % 3600) // 60}분 {duration % 60}초"

        self.channelNameLabel.setText(f"Channel Name: {channel_name}")
        self.titleLabel.setText(f"Title: {title}")
        self.categoryLabel.setText(f"Category: {category}")
        self.liveOpenDateLabel.setText(f"Live Open Date: {live_open_date}")
        self.durationLabel.setText(f"Duration: {duration_str}")

        self.load_image_from_url(self.channelImageLabel, channel_image_url, 100, 100)
        self.load_image_from_url(self.thumbnailLabel, thumbnail_url, 256, 144)

    def load_image_from_url(self, label, url, width, height):
        response = requests.get(url)
        image = QPixmap()
        image.loadFromData(BytesIO(response.content).read())
        scaled_image = image.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(scaled_image)

    def add_representation_button(self, width, height, base_url):
        button = QPushButton(f'{width}x{height}', self)
        button.clicked.connect(lambda: self.onDownload(base_url, height))
        self.resolutionButtonsLayout.addWidget(button)
        
        def update_button_text():
            try:
                response = requests.head(base_url)
                response.raise_for_status()

                size = int(response.headers.get('content-length', 0))
                units = ["B", "KB", "MB", "GB", "TB"]
                unit_index = 0

                while size >= 1024 and unit_index < len(units) - 1:
                    size /= 1024
                    unit_index += 1

                new_text = f'{width}x{height} ({size:.2f} {units[unit_index]})'
                button.setText(new_text)
            except Exception as e:
                print(f"Failed to retrieve content length: {e}")

        threading.Thread(target=update_button_text).start()

    def onDownload(self, base_url, height):
        if self.metadata:
            title = self.metadata.get('title', 'Unknown Title')
            category = self.metadata.get('videoCategoryValue', 'Unknown Category')
            live_open_date = self.metadata.get('liveOpenDate', 'Unknown Date')

            title = re.sub(r'[\\/:\*\?"<>|]', '', title)
            category = re.sub(r'[\\/:\*\?"<>|]', '', category)

            if not category:
                default_filename = f"{live_open_date.split(' ')[0]}) {title}.mp4"
            else:
                default_filename = f"{live_open_date.split(' ')[0]}) [{category}] {title}.mp4"
        else:
            default_filename = "video.mp4"

        options = QFileDialog.Options()
        output_path, _ = QFileDialog.getSaveFileName(self, "Save Video File", default_filename, "Video Files (*.mp4);;All Files (*)", options=options)
        
        if output_path:
            self.fetchButton.setEnabled(False)
            self.set_resolution_buttons_enabled(False)

            self.downloadThread = DownloadThread(base_url, output_path, height)
            self.downloadThread.progress.connect(self.updateProgress)
            self.downloadThread.completed.connect(self.onDownloadCompleted)
            self.downloadThread.paused.connect(self.onPaused)
            self.downloadThread.resumed.connect(self.onResumed)
            self.downloadThread.stopped.connect(self.onStopped)
            self.downloadThread.update_threads.connect(self.updateThreadStatus)
            self.downloadThread.update_time.connect(self.updateTimeStatus)
            self.downloadThread.update_active_threads.connect(self.updateActiveThreads)  # 활성화된 스레드 수 업데이트 신호 연결
            self.downloadThread.update_avg_speed.connect(self.updateAvgSpeed)
            self.downloadThread.start()
            self.pauseResumeButton.setEnabled(True)
            self.stopButton.setEnabled(True)

    def onPauseResume(self):
        if self.downloadThread:
            if self.pauseResumeButton.text() == 'Pause':
                self.downloadThread.pause()
                self.pauseResumeButton.setText('Resume')
            else:
                self.downloadThread.resume()
                self.pauseResumeButton.setText('Pause')

    def onStop(self):
        if self.downloadThread:
            self.pauseResumeButton.setText('Pause')
            self.downloadThread.stop()
            self.pauseResumeButton.setEnabled(False)
            self.stopButton.setEnabled(False)

    def updateProgress(self, value, status_message):
        self.progressBar.setValue(value)
        self.downloadStatusLabel.setText(status_message)

    def onDownloadCompleted(self, message):
        # print("다운로드 완료!") # Debugging
        self.downloadStatusLabel.setText(message)
        self.pauseResumeButton.setEnabled(False)
        self.stopButton.setEnabled(False)
        self.fetchButton.setEnabled(True)
        self.set_resolution_buttons_enabled(True)

    def onPaused(self):
        self.downloadStatusLabel.setText("다운로드 일시정지")

    def onResumed(self):
        self.downloadStatusLabel.setText("다운로드 재개")

    def onStopped(self, message):
        self.downloadStatusLabel.setText(message)
        self.fetchButton.setEnabled(True)
        self.set_resolution_buttons_enabled(True)

    def updateThreadStatus(self, completed, total, failed, restart):
        self.threadStatusLabel.setText(f"Segments: {completed}/{total} (Failed: {failed}, Restart: {restart})")

    def updateTimeStatus(self, elapsed, remaining):
        self.timeLabel.setText(f'경과 시간/예상 시간: {elapsed}/{remaining}')

    def updateActiveThreads(self, active_threads):
        self.maxThreadsLabel.setText(f'Current Threads: {active_threads}')
        
    def updateAvgSpeed(self, avg_speed):
        self.avgSpeedLabel.setText(f'Average Speed: {avg_speed:.2f} MB/s')

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = VodDownloader()
    ex.setWindowIcon(QIcon(resources.icon))
    sys.exit(app.exec_())
