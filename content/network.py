import re
import requests
import xml.etree.ElementTree as ET

NAVER_API = "https://apis.naver.com"
CHZZK_API = "https://api.chzzk.naver.com"
VIDEOHUB_API = "https://api-videohub.naver.com"

class NetworkManager:

    @staticmethod
    def extract_content_no(vod_url: str) -> tuple[str, str]:
        """
        치지직 VOD URL에서 type과 content_no를 추출한다.
        
        Args:
            vod_url (str): 치지직 VOD URL
            
        Returns:
            tuple[str, str]: (type, content_no) 형식의 튜플. 매칭되지 않으면 (None, None) 반환
        """
        if not vod_url.startswith("http://") and not vod_url.startswith("https://"):
            vod_url = "https://" + vod_url
        match = re.fullmatch(r'https?://chzzk\.naver\.com/(?P<content_type>video|clips)/(?P<content_no>\w+)', vod_url)
        if match:
            return match.group("content_type"), match.group("content_no")
        return None, None

    @staticmethod
    def get_video_info(video_no: str, cookies: dict):
        """
        API를 통해 video_no에 대응하는 video_id, in_key, 메타데이터를 가져온다.
        """
        api_url = f"{CHZZK_API}/service/v2/videos/{video_no}"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(api_url, cookies=cookies, headers=headers)
        response.raise_for_status()

        content = response.json().get('content', {})
        video_id = content.get('videoId')
        in_key = content.get('inKey')
        adult = content.get('adult')
        vodStatus = content.get('vodStatus')

        metadata = {
            'title': content.get('videoTitle', 'Unknown Title'),
            'thumbnailImageUrl': content.get('thumbnailImageUrl', ''),
            'category': content.get('videoCategoryValue', 'Unknown Category'),
            'channelName': content.get('channel', {}).get('channelName', 'Unknown Channel'),
            'channelImageUrl': content.get('channel', {}).get('channelImageUrl', ''),
            'createdDate': content.get('liveOpenDate', 'Unknown Date'),
            'duration': content.get('duration', 0),
        }
        return video_id, in_key, adult, vodStatus, metadata
    
    @staticmethod
    def get_video_dash_manifest(video_id: str, in_key: str):
        """
        DASH 매니페스트를 요청하여 Representation 목록을 파싱한다.
        """
        manifest_url = f"{NAVER_API}/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
        headers = {"Accept": "application/dash+xml"}
        response = requests.get(manifest_url, headers=headers)
        response.raise_for_status()

        root = ET.fromstring(response.text)
        ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
        reps = []
        for rep in root.findall(".//mpd:Representation", namespaces=ns):
            width = rep.get('width')
            height = rep.get('height')
            resolution = min(int(width), int(height))
            # print(width, height) # Debugging
            # print(f"Resolution: {resolution}") # Debugging
            base_url = rep.find(".//mpd:BaseURL", namespaces=ns).text
            if base_url.endswith('/hls/'):
                continue
            reps.append([resolution, base_url])
        
        sorted_reps = sorted(reps, key=lambda x: x[0])
        auto_resolution = sorted_reps[-1][0]
        auto_base_url = sorted_reps[-1][1]

        # 중복 제거한 뒤, 리스트로 변환
        return sorted_reps, auto_resolution, auto_base_url
    
    @staticmethod
    def get_clip_info(clip_no: str, cookies: dict):
        """
        API를 통해 clip_no에 대응하는 clip_id, in_key, 메타데이터를 가져온다.
        """
        api_url = f"{CHZZK_API}/service/v1/clips/{clip_no}/detail?optionalProperties=OWNER_CHANNEL"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(api_url, cookies=cookies, headers=headers)
        response.raise_for_status()

        content = response.json().get('content', {})
        video_id = content.get('videoId')
        vodStatus = content.get('vodStatus')

        metadata = {
            'title': content.get('clipTitle', 'Unknown Title'),
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
        response = requests.get(manifest_url, cookies=cookies, headers=headers)
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