from metadata.network import NetworkManager

from PySide6.QtCore import QThread, QObject, Signal

class MetadataWorker(QObject):
    # 작업이 완료되면 결과를 tuple로 전달 (혹은 필요한 데이터 구조로 전달)
    finished = Signal(object)
    error = Signal(str)
    
    def __init__(self, vod_url: str, cookies: dict, downloadPath: str):
        super().__init__()
        self.vod_url = vod_url
        self.cookies = cookies
        self.downloadPath = downloadPath

    def run(self):
        try:
            video_no = NetworkManager.extract_video_no(self.vod_url)
            if not video_no:
                raise ValueError(f"{self.vod_url}\nInvalid VOD URL")
        
            video_id, in_key, adult, vodStatus, metadata = NetworkManager.get_video_info(video_no, self.cookies)
            if adult and not video_id:
                raise ValueError(f"{self.vod_url}\nInvalid cookies value")
            elif vodStatus == 'NONE':
                raise ValueError(f"{self.vod_url}\nUnencoded Video(.m3u8)")
            
            unique_reps, height, base_url = NetworkManager.get_dash_manifest(video_id, in_key)
            if not unique_reps:
                raise ValueError(f"{self.vod_url}\nFailed to get DASH manifest")
            
            # 네트워크 작업 결과를 tuple 형태로 묶어서 전달
            result = (self.vod_url, metadata, unique_reps, height, base_url, self.downloadPath, self.cookies)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))