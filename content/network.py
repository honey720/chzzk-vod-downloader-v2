import re
import json
from urllib.parse import urljoin

from core.api.dash import parse_dash_manifest
from core.api.url_parser import extract_content_no
from core.models.content import VideoInfo

# Session 관리는 core/api/session.py로 이주했다 (#62, 원본 #31).
# _session은 아래 NetworkManager가 계속 사용하고, 나머지는 기존 호출부 호환용 re-export다.
from core.api.session import _session
from core.api.session import _make_session, get_thread_session  # noqa: F401

NAVER_API = "https://apis.naver.com"
CHZZK_API = "https://api.chzzk.naver.com"
VIDEOHUB_API = "https://api-videohub.naver.com"


class NetworkManager:

    @staticmethod
    def extract_content_no(vod_url: str) -> tuple[str, str]:
        """
        치지직 VOD URL에서 type과 content_no를 추출한다.

        구현은 core/api/url_parser.py로 이동했다 (#50).
        기존 호출부 호환을 위해 시그니처를 유지하고 core 함수에 위임한다.

        Args:
            vod_url (str): 치지직 VOD URL

        Returns:
            tuple[str, str]: (type, content_no) 형식의 튜플. 매칭되지 않으면 (None, None) 반환
        """
        return extract_content_no(vod_url)

    @staticmethod
    def get_video_info(video_no: str, cookies: dict) -> VideoInfo:
        """
        API를 통해 video_no에 대응하는 video_id, in_key, 메타데이터를 가져온다.

        기존 8-tuple 대신 VideoInfo 데이터 객체를 반환한다 (#61). 값·의미는 무변경.

        membership_benefit_type·encryption_type도 함께 반환한다 (#55).
        - 멤버십(구독자) 전용 VOD는 권한이 없으면 inKey가 null로 내려오므로,
          호출부에서 membership_benefit_type으로 "멤버십 필요" 안내를 구분할 수 있다.
        - encryption_type이 null이 아니면(AES 등) 세그먼트가 암호화되어 있어
          현재 다운로더로는 조립할 수 없으므로 호출부에서 조기에 안내한다.
        """
        api_url = f"{CHZZK_API}/service/v2/videos/{video_no}"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = _session.get(api_url, cookies=cookies, headers=headers)
        response.raise_for_status()

        content = response.json().get('content', {})
        metadata = {
            'title': re.sub(r'[\\/:\*\?"<>|\n]', '', content.get('videoTitle', 'Unknown Title')), # 정규식으로 특수문자 제거
            'thumbnailImageUrl': content.get('thumbnailImageUrl', ''),
            'category': content.get('videoCategoryValue', 'Unknown Category'),
            'channelName': content.get('channel', {}).get('channelName', 'Unknown Channel'),
            'channelImageUrl': content.get('channel', {}).get('channelImageUrl', ''),
            'createdDate': content.get('liveOpenDate', 'Unknown Date'),
            'duration': content.get('duration', 0),
        }
        return VideoInfo(
            video_id=content.get('videoId'),
            in_key=content.get('inKey'),
            adult=content.get('adult'),
            vod_status=content.get('vodStatus'),
            live_rewind_playback_json=content.get('liveRewindPlaybackJson'),
            membership_benefit_type=content.get('membershipBenefitType'),
            encryption_type=content.get('encryptionType'),
            metadata=metadata,
        )

    @staticmethod
    def get_video_dash_manifest(video_id: str, in_key: str, cookies: dict | None = None):
        """
        DASH 매니페스트를 요청하여 Representation 목록을 파싱한다.

        HTTP 요청만 담당하고, XML 파싱은 core/api/dash.py의 순수 함수에 위임한다 (#51).
        멤버십 전용 VOD 재생 검증을 위해 메타데이터 요청과 동일하게 쿠키를 실어 보낸다 (#55).
        """
        manifest_url = f"{NAVER_API}/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
        headers = {"Accept": "application/dash+xml"}
        response = _session.get(manifest_url, cookies=cookies, headers=headers)
        response.raise_for_status()

        return parse_dash_manifest(response.text)
    
    @staticmethod
    def get_video_m3u8_manifest(json_str: str):
        """
        m3u8 정보가 포함된 json형식의 문자열을 받아서 Representation 목록을 파싱한다.
        """
        data = json.loads(json_str)
        media = data.get("media", [])
        encoding_track = media[0].get("encodingTrack", [])
        reps = []
        for encoding in encoding_track:
            width = encoding.get("videoWidth")
            height = encoding.get("videoHeight")
            resolution = min(int(width), int(height))
            base_url = None
            reps.append([resolution, base_url])

        sorted_reps = sorted(reps, key=lambda x: x[0])
        auto_resolution = sorted_reps[-1][0]
        auto_base_url = sorted_reps[-1][1]
        return sorted_reps, auto_resolution, auto_base_url
    
    @staticmethod
    def get_video_m3u8_base_url(json_str: str, resolution: int, cookies: dict | None = None) -> str:
        """
        m3u8 정보가 포함된 json형식의 문자열을 받아서 base_url을 파싱한다.

        권한이 필요한 VOD의 플레이리스트 접근을 위해 쿠키를 실어 보낸다 (#55).
        """
        data = json.loads(json_str)
        media = data.get("media", [])
        path = media[0].get("path")
        response = _session.get(path, cookies=cookies)
        response.raise_for_status()
        content = response.text.splitlines()

        # 정규식으로 해상도 매칭
        resolution_pattern = re.compile(rf"RESOLUTION=\d+x{resolution}")
        
        for i, line in enumerate(content):
            if resolution_pattern.search(line):
                # 다음 줄이 해당 해상도의 세부 플레이리스트 경로
                relative_path = content[i + 1].strip()
                base_url = urljoin(path, relative_path)
                return base_url

        raise ValueError(f"{resolution} 해상도 스트림을 찾을 수 없습니다.")
    
    @staticmethod
    def get_clip_info(clip_no: str, cookies: dict):
        """
        API를 통해 clip_no에 대응하는 clip_id, in_key, 메타데이터를 가져온다.
        """
        api_url = f"{CHZZK_API}/service/v1/clips/{clip_no}/detail?optionalProperties=OWNER_CHANNEL"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = _session.get(api_url, cookies=cookies, headers=headers)
        response.raise_for_status()

        content = response.json().get('content', {})
        video_id = content.get('videoId')
        vodStatus = content.get('vodStatus')

        metadata = {
            'title': re.sub(r'[\\/:\*\?"<>|\n]', '', content.get('clipTitle', 'Unknown Title')), # 정규식으로 특수문자 제거
            'thumbnailImageUrl': content.get('thumbnailImageUrl', ''),
            'category': content.get('clipCategory', 'Unknown Category'),
            'channelName': content.get('optionalProperty', {}).get('ownerChannel', {}).get('channelName', 'Unknown Channel'),
            'channelImageUrl': content.get('optionalProperty', {}).get('ownerChannel', {}).get('channelImageUrl', ''),
            'createdDate': content.get('createdDate', 'Unknown Date'),
            'duration': content.get('duration', 0),
        }
        return video_id, vodStatus, metadata

    @staticmethod
    def get_clip_manifest(clip_id: str, cookies: dict):
        """
        DASH 매니페스트를 요청하여 Representation 목록을 파싱한다.
        """
        manifest_url = f"{VIDEOHUB_API}/shortformhub/feeds/v3/card?serviceType=CHZZK&seedMediaId={clip_id}&mediaType=VOD"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = _session.get(manifest_url, cookies=cookies, headers=headers)
        response.raise_for_status()

        data = response.json()

        resolutions = []

        #오류현상 예외처리

        content = data['card']['content']
        if 'error' in content:
            error = content['error']
            return None, None, None, error
        
        video_list = content['vod']['playback']['videos']['list']

        for video in video_list:
            encoding = video.get("encodingOption", {})
            width = encoding.get("width")
            height = encoding.get("height")
            source_url = video.get("source")

            if width and height and source_url:
                resolution = min(int(width), int(height))
                resolutions.append([resolution, source_url])

        sorted_resolutions = sorted(resolutions, key=lambda x: x[0])
        auto_resolution = sorted_resolutions[-1][0]
        auto_base_url = sorted_resolutions[-1][1]

        return sorted_resolutions, auto_resolution, auto_base_url, None