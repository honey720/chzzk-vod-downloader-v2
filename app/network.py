import json
from fractions import Fraction
from urllib.parse import urljoin, urlsplit

import requests

from core.api.dash import (
    is_supported_sea,
    parse_dash_manifest,
    parse_frame_rates,
    parse_sea_manifest,
)
from core.api.hls import variant_frame_rate
from core.api.playback_tracks import (
    StreamSelectionError,
    list_streams,
    playback_tracks,
    select_stream,
    select_variant,
    select_variant_by_height,
    track_for_resolution,
)
from core.api.representations import dedupe_by_resolution
from core.api.url_parser import extract_content_no
from core.models.content import StreamKey, VideoInfo
from core.utils.paths import sanitize_filename

# Session 관리는 core/api/session.py로 이주했다 (#62, 원본 #31).
# _session은 아래 NetworkManager가 사용한다. 호환용 re-export(get_thread_session·
# _make_session)는 #259 B3에서 제거했다 — 쓰는 쪽은 core.api.session에서 직접 import한다.
from core.api.session import _session

NAVER_API = "https://apis.naver.com"
CHZZK_API = "https://api.chzzk.naver.com"
VIDEOHUB_API = "https://api-videohub.naver.com"

# 조회(메타데이터·매니페스트) 요청 공통 타임아웃 (#129).
# 타임아웃이 없으면 네트워크 단절 시 OS TCP 타임아웃(관측 ~47초, 환경 따라 더 김)까지
# 조회가 갇힌다. connect 5초: 정상 연결은 1초 미만이라 여유가 충분하고 OS SYN
# 재시도보다 먼저 끊는다. read 15초: 응답이 작은 JSON/XML이라 평시 1초 미만 —
# 느린 회선·서버 지연에 여유를 두되 OS 타임아웃보다 훨씬 먼저 포기한다.
REQUEST_TIMEOUT = (5, 15)


# 서버 응답이 알려준 주소로 쿠키를 보내는 요청의 신뢰 검사.
# 인증 쿠키는 요청별 cookies= 인자로만 실리고(core/api/session.py — 응답 쿠키 저장
# 없음), 그것을 서버가 준 주소에 붙이는 지점은 둘뿐이다: 복호화 키(#EXT-X-KEY URI)와
# m3u8 마스터 플레이리스트(playback JSON의 path). 주소는 스트림 응답에서 오므로
# HTTP나 다른 호스트로 쿠키가 새어 나갈 수 있어, 요청 직전에 검사한다.
#
# 리다이렉트: requests는 리다이렉트 홉에도 요청별 쿠키를 다시 붙이므로 첫 주소만
# 검사하면 소용없다. 자동 추적을 끄고(allow_redirects=False) 홉마다 Location을
# 같은 검사에 통과시킨 뒤에만 다음 요청을 보낸다 — 검사에 걸린 홉에는 쿠키가 가지
# 않는다(요청 자체가 없다).
_TRUSTED_SCHEME = "https"
_MAX_REDIRECTS = 5


def _require_trusted_url(url: str, allowed_hosts: frozenset[str] | None) -> None:
    """쿠키를 실어 보내도 되는 주소인지 검사한다 — 아니면 InvalidURL.

    검사는 둘: ① https ② allowed_hosts가 주어지면 호스트가 그 안에 있을 것.
    예외 메시지에는 스킴과 호스트만 싣는다(주소의 경로·질의에는 토큰이 섞여 있다).
    쪼갤 수 없는 주소(urlsplit의 ValueError)는 메시지에 아예 싣지 않는다 — 어디까지가
    호스트인지 알 수 없으므로.
    """
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise requests.exceptions.InvalidURL("쿠키를 실어 보낼 수 없는 주소다(형식 오류)") from e
    host = (parts.hostname or "").lower()
    if parts.scheme != _TRUSTED_SCHEME:
        raise requests.exceptions.InvalidURL(
            f"쿠키를 실어 보낼 수 없는 주소다(https 아님): {parts.scheme}://{host}"
        )
    if allowed_hosts is not None and host not in allowed_hosts:
        raise requests.exceptions.InvalidURL(f"쿠키를 실어 보낼 수 없는 호스트다: {host}")


def _get_with_cookies_trusted(
    url: str, cookies: dict | None, allowed_hosts: frozenset[str] | None, **kwargs
) -> requests.Response:
    """주소를 검사한 뒤 쿠키를 실어 GET한다. 리다이렉트는 홉마다 다시 검사한 뒤 따라간다.

    허용 홉 수를 넘기면 requests와 같은 TooManyRedirects를 낸다.
    """
    for _ in range(_MAX_REDIRECTS + 1):
        _require_trusted_url(url, allowed_hosts)
        response = _session.get(url, cookies=cookies, allow_redirects=False, **kwargs)
        location = getattr(response, "headers", {}).get("Location")
        if 300 <= getattr(response, "status_code", 200) < 400 and location:
            url = urljoin(url, location)
            continue
        return response
    raise requests.exceptions.TooManyRedirects(f"리다이렉트가 {_MAX_REDIRECTS}회를 넘었다")


#: 복호화 키를 받아도 되는 호스트 — 코드 상수의 API 호스트뿐이다(실측 SEA 매니페스트의
#: keyUriTemplate이 이 호스트다). 새 목록을 두지 않는다.
_KEY_HOSTS = frozenset({urlsplit(CHZZK_API).hostname})


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
        response = _session.get(api_url, cookies=cookies, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        content = response.json().get('content', {})
        metadata = {
            'title': sanitize_filename(content.get('videoTitle', 'Unknown Title')), # 금지 문자·제어 문자 제거 (#105)
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
        response = _session.get(manifest_url, cookies=cookies, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        return parse_dash_manifest(response.text)

    @staticmethod
    def get_video_frame_rates(
        video_id: str, in_key: str, cookies: dict | None = None
    ) -> dict[str, Fraction]:
        """DASH 매니페스트를 요청해 Representation마다 선언된 프레임률을 읽는다 (#309).

        요청은 ``get_video_dash_manifest``와 같다. 파싱은 core/api/dash.py의
        ``parse_frame_rates``에 위임한다.

        Returns:
            ``{base_url: 프레임률}`` — ``get_video_dash_manifest`` ·
            ``get_video_sea_manifest``가 돌려주는 base_url로 찾는다. 프레임률을 선언하지
            않은 Representation은 들어 있지 않다
        """
        manifest_url = f"{NAVER_API}/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
        headers = {"Accept": "application/dash+xml"}
        response = _session.get(
            manifest_url, cookies=cookies, headers=headers, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()

        return parse_frame_rates(response.text)

    @staticmethod
    def get_video_sea_manifest(video_id: str, in_key: str, cookies: dict | None = None):
        """AES(SEA) 암호화 VOD의 매니페스트를 요청해 비디오 Representation을 파싱한다 (#57).

        암호화 비디오 Representation은 BaseURL 없이 nvod:m3u(HLS 미디어
        플레이리스트)만 가지므로 전용 파서를 쓴다. 지원 대상(AES-128-CBC +
        HTTP 키 시스템)이 아니면 빈 결과를 돌려줘 호출부가 기존 "지원하지
        않음" 안내로 떨어지게 한다 — 라이선스 서버형 DRM은 여기서 걸러진다.
        """
        manifest_url = f"{NAVER_API}/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
        headers = {"Accept": "application/dash+xml"}
        response = _session.get(manifest_url, cookies=cookies, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        if not is_supported_sea(response.text):
            return [], None, None
        return parse_sea_manifest(response.text)

    @staticmethod
    def get_aes_key(key_uri: str, cookies: dict) -> bytes:
        """세그먼트 복호화 키를 취득한다 (#57).

        유저 본인의 쿠키로 인증된 요청이며, 쿠키가 없거나 권한이 없으면
        서버가 403으로 거절해 아무것도 받아지지 않는다(실측). 앱은 권한을
        만들어내지 않는다.

        **키 값은 로그·예외 메시지에 싣지 않는다.**

        key_uri는 플레이리스트가 알려준 주소다 — https와 API 호스트(_KEY_HOSTS)를
        검사한 뒤에만 쿠키를 싣는다. 리다이렉트 홉도 같은 검사를 거친다.
        """
        headers = {"User-Agent": "Mozilla/5.0"}
        response = _get_with_cookies_trusted(
            key_uri, cookies, _KEY_HOSTS, headers=headers, timeout=30
        )
        response.raise_for_status()
        return response.content

    @staticmethod
    def get_video_m3u8_manifest(json_str: str):
        """
        m3u8 정보가 포함된 json형식의 문자열을 받아서 Representation 목록을 파싱한다.

        해상도는 트랙 이름(encodingTrackId, "1080p" → 1080)을 따르고, 이름이 그런 형식이
        아니면 짧은 변이다(core/api/representations.py의 track_resolution, #318).
        """
        # 해상도가 같은 트랙(비트레이트·프레임레이트 변형)은 하나로 합친다 —
        # 이 경로는 트랙별 URL이 없어 다운로드 대상은 get_video_m3u8_base_url이
        # 같은 규칙으로 트랙을 골라 마스터 플레이리스트에서 찾는다.
        # 규칙·근거는 core/api/representations.py.
        reps = [
            (track.resolution, None, track.video_bitrate) for track in playback_tracks(json_str)
        ]

        sorted_reps = dedupe_by_resolution(reps)
        auto_resolution = sorted_reps[-1][0]
        auto_base_url = sorted_reps[-1][1]
        return sorted_reps, auto_resolution, auto_base_url
    
    @staticmethod
    def get_video_m3u8_streams(json_str: str, cookies: dict | None = None):
        """마스터 플레이리스트를 받아 실제 변형으로 해상도 목록을 만든다 (#318).

        playback 정보의 트랙은 영상과 어긋날 수 있다(원본 트랙의 값이 0이거나, 다른 방송의
        트랙이 오는 경우가 있다). 받는 것은 마스터 플레이리스트의 변형이므로 목록도 거기서
        만든다. playback 정보는 원본 여부를 아는 데만 쓴다(core/api/playback_tracks.py).

        요청은 get_video_m3u8_base_url과 같다(같은 주소 검사 · 쿠키 · 타임아웃).

        Returns:
            ([해상도, None] 항목(StreamEntry) 목록(오름차순), auto 해상도, None).
            해상도는 짧은 변이고, 짧은 변이 같은 변형은 둘 다 들어 있다.
            변형이 하나도 없으면 ([], None, None)
        """
        path = json.loads(json_str).get("media", [])[0].get("path")
        response = _get_with_cookies_trusted(path, cookies, None, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        entries = list_streams(response.text, playback_tracks(json_str))
        if not entries:
            return [], None, None
        return entries, entries[-1][0], None

    @staticmethod
    def get_video_m3u8_base_url(
        json_str: str,
        resolution: int,
        cookies: dict | None = None,
        stream: StreamKey | None = None,
    ) -> str:
        """
        m3u8 정보가 포함된 json형식의 문자열을 받아서 base_url을 파싱한다.

        권한이 필요한 VOD의 플레이리스트 접근을 위해 쿠키를 실어 보낸다 (#55).
        path는 playback JSON이 알려준 주소다 — https만 검사한다(호스트는 잠그지
        않는다). 리다이렉트 홉도 같은 검사를 거친다.

        stream이 있으면 목록(get_video_m3u8_streams)에서 고른 변형의 정체다 — 마스터
        플레이리스트에서 같은 크기 · 프레임률 · BANDWIDTH의 변형을 다시 찾는다(#318).

        stream이 없으면 해상도는 get_video_m3u8_manifest의 값이다. 그 값이 가리키는 트랙을
        playback 정보에서 찾아 그 트랙의 크기 · 프레임률 · 비트레이트로 변형을 고르고,
        트랙이 없거나 트랙과 맞는 변형이 없으면 전처럼 세로값으로 찾는다 — playback 정보의
        트랙이 그 영상의 것이 아닐 수 있다.

        Raises:
            StreamSelectionError: 맞는 변형이 없거나 하나로 정해지지 않는 경우
        """
        return NetworkManager.get_video_m3u8_variant(json_str, resolution, cookies, stream)[0]

    @staticmethod
    def get_video_m3u8_variant(
        json_str: str,
        resolution: int,
        cookies: dict | None = None,
        stream: StreamKey | None = None,
    ) -> tuple[str, Fraction | None]:
        """마스터 플레이리스트에서 고른 변형의 (base_url, 선언된 프레임률)을 읽는다 (#309).

        요청과 변형을 고르는 규칙은 ``get_video_m3u8_base_url``의 것이다(그 메서드가 이것을
        부른다, #318) — 구간 다운로드도 목록에서 고른 변형과 같은 변형을 받는다. 프레임률은
        고른 변형의 ``#EXT-X-STREAM-INF``에 FRAME-RATE가 있을 때만 있고, 없으면 None이다.

        Raises:
            StreamSelectionError: 맞는 변형이 없거나 하나로 정해지지 않는 경우
        """
        data = json.loads(json_str)
        media = data.get("media", [])
        path = media[0].get("path")
        response = _get_with_cookies_trusted(path, cookies, None, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        relative_path = NetworkManager._select_m3u8_variant(
            response.text, json_str, resolution, stream
        )
        return urljoin(path, relative_path), variant_frame_rate(response.text, relative_path)

    @staticmethod
    def _select_m3u8_variant(
        master_text: str, json_str: str, resolution: int, stream: StreamKey | None
    ) -> str:
        """마스터 플레이리스트에서 받을 변형의 주소(태그 다음 줄)를 고른다 (#318).

        규칙은 ``get_video_m3u8_base_url``의 docstring에 있다.

        Raises:
            StreamSelectionError: 맞는 변형이 없거나 하나로 정해지지 않는 경우
        """
        if stream is not None:
            return select_stream(master_text, stream)

        track = track_for_resolution(playback_tracks(json_str), resolution)
        if track is None:
            # playback 정보에 그 해상도의 트랙이 없다 — 정체를 모르므로 전처럼 세로값으로 찾는다
            return select_variant_by_height(master_text, resolution)
        try:
            return select_variant(master_text, track)
        except StreamSelectionError as selection_error:
            # 트랙과 맞는 변형이 없다 — 트랙이 이 영상의 것이 아닐 수 있다. 세로값으로 찾는다
            try:
                return select_variant_by_height(master_text, resolution)
            except StreamSelectionError:
                raise selection_error from None
    
    @staticmethod
    def get_clip_info(clip_no: str, cookies: dict):
        """
        API를 통해 clip_no에 대응하는 clip_id, in_key, 메타데이터를 가져온다.
        """
        api_url = f"{CHZZK_API}/service/v1/clips/{clip_no}/detail?optionalProperties=OWNER_CHANNEL"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = _session.get(api_url, cookies=cookies, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        content = response.json().get('content', {})
        video_id = content.get('videoId')
        vodStatus = content.get('vodStatus')

        metadata = {
            'title': sanitize_filename(content.get('clipTitle', 'Unknown Title')), # 금지 문자·제어 문자 제거 (#105)
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
        response = _session.get(manifest_url, cookies=cookies, headers=headers, timeout=REQUEST_TIMEOUT)
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
                # 비트레이트 정보가 없어 같은 높이는 먼저 나온 트랙이 남는다
                resolutions.append((resolution, source_url, 0))

        sorted_resolutions = dedupe_by_resolution(resolutions)
        auto_resolution = sorted_resolutions[-1][0]
        auto_base_url = sorted_resolutions[-1][1]

        return sorted_resolutions, auto_resolution, auto_base_url, None