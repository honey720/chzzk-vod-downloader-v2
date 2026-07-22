import logging

from content.network import NetworkManager

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class ContentWorker(QObject):
    # 작업이 완료되면 결과를 tuple로 전달 (혹은 필요한 데이터 구조로 전달)
    finished = Signal(object, str)
    error = Signal(str)
    
    def __init__(self, vod_url: str, cookies: dict, downloadPath: str):
        super().__init__()
        self.vod_url = vod_url
        self.cookies = cookies
        self.downloadPath = downloadPath

    def run(self):
        try:
            content_type, content_no = NetworkManager.extract_content_no(self.vod_url)
            if not content_type or not content_no:
                errorMessage = self.tr("Invalid VOD URL")
                raise ValueError(f"{self.vod_url}\n{errorMessage}")
            
            if content_type == 'video':
                result = self.fetchVideo(content_no)
                if result[6]:
                    content_type = "m3u8"
            elif content_type == 'clips':
                content_type = 'clip'
                result = self.fetchClip(content_no)
                
            self.finished.emit(result, content_type)
        except Exception as e:
            # 크래시 지점 추적을 위해 traceback을 로그에 남긴다 (#55 디버깅).
            # str(e)만으로는 AttributeError 등의 발생 위치를 알 수 없다
            logger.exception("컨텐츠 요청 실패: %s", self.vod_url)
            self.error.emit(str(e))

    def fetchVideo(self, video_no: str):
        video_id, in_key, adult, vodStatus, liveRewindPlaybackJson, membershipBenefitType, encryptionType, metadata = NetworkManager.get_video_info(video_no, self.cookies)
        if adult and not video_id:
            errorMessage = self.tr("Invalid cookies value")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        elif encryptionType:
            # 암호화(AES/SEA) VOD는 세그먼트를 복호화할 수 없어 다운로드 불가.
            # 권한(멤버십) 유무와 무관하므로 매니페스트 요청 전에 조기 안내한다 (#55)
            errorMessage = self.tr("Encrypted content is not supported")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        elif liveRewindPlaybackJson:
            unique_reps, resolution, base_url = NetworkManager.get_video_m3u8_manifest(liveRewindPlaybackJson)
        else:
            # 멤버십 전용 VOD는 권한이 없으면 inKey가 null로 내려온다.
            # 이대로 매니페스트를 요청하면 원인을 알 수 없는 401이 노출되므로 먼저 안내한다 (#55)
            if not in_key and membershipBenefitType == "MEMBER_ONLY":
                errorMessage = self.tr("Channel membership required")
                raise ValueError(f"{self.vod_url}\n{errorMessage}")
            unique_reps, resolution, base_url = NetworkManager.get_video_dash_manifest(video_id, in_key, self.cookies)
        if not unique_reps:
            errorMessage = self.tr("Failed to get DASH manifest")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
        # 네트워크 작업 결과를 tuple 형태로 묶어서 전달
        return (self.vod_url, metadata, unique_reps, resolution, base_url, self.downloadPath, liveRewindPlaybackJson)
            

    def fetchClip(self, clip_no: str):
        video_id, vodStatus, metadata = NetworkManager.get_clip_info(clip_no, self.cookies)
        if vodStatus == 'NONE':
            errorMessage = self.tr("Unencoded Video(.m3u8)")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
        unique_reps, resolution, base_url, error = NetworkManager.get_clip_manifest(video_id, self.cookies)
        if error and error.get("errorCode") == "ADULT_AUTH_REQUIRED":
            errorMessage = self.tr("Invalid cookies value")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
        if not unique_reps:
            errorMessage = self.tr("Failed to get DASH manifest")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
        return (self.vod_url, metadata, unique_reps, resolution, base_url, self.downloadPath, None)
