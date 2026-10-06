"""구간을 입력받는 데 필요한 기준값 조회 — 프레임률과 실제 영상 길이 (#309, Qt 무의존).

타임코드의 프레임 칸(FF)은 그 영상의 프레임률로 읽고, 구간은 실제 영상 길이 안에 있어야
한다. 두 값은 영상을 받아 봐야 안다 — 인코딩 완료 VOD는 mp4의 moov, 인코딩 전 다시보기와
암호화 VOD는 플레이리스트와 첫 · 마지막 세그먼트에 있다. 영상 정보 API의 ``duration``은
정수 초라 구간 검증에 쓰지 않는다.

값을 정하는 방법은 엔진이 구간 다운로드를 시작할 때와 같다(같은 core 함수를 부른다).

**조회 순서는 이 모듈 한 곳에만 있다.** 구간 편집 창(``probe_section_basis``)과 헤드리스
스크립트(``scripts/headless_download.py``의 구간 해석)가 같은 함수(``probe_mp4`` ·
``probe_fmp4`` · ``probe_ts``)를 부른다. 헤드리스는 받은 것을 엔진에 넘기려고 세그먼트를 둘
폴더(segment_dir)를 주고, 편집 창은 주지 않는다 — 값만 읽고 받은 본문은 버린다.

쿠키 · 치지직 API 조회가 앱 계층에 있어 core로 들어갈 수 없다(``app/download_resolvers.py``와
같은 이유). 워커 스레드에서 부른다 — 네트워크를 탄다.

**HLS 세그먼트는 전체 요청으로만 받는다.** 받는 함수(``core/api/hls_fmp4.py`` ·
``core/api/hls_ts.py``)가 그렇게 받는다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

import config.config as config
from app.download_resolvers import resolve_aes_key, resolve_m3u8_variant
from app.network import NetworkManager
from core.api.hls_fmp4 import fetch_fmp4_head, segment_frames
from core.api.hls_ts import fetch_ts_head, segment_streams, ts_key_uri
from core.api.mp4 import fetch_mp4_head
from core.models.content import Content, ContentType
from core.models.fmp4_index import Fmp4Head
from core.models.mp4_index import Mp4Head
from core.models.ts_index import TsHead
from core.utils.fmp4_sections import FPS_DECLARED, choose_frame_rate, fmp4_timeline
from core.utils.ts_sections import choose_ts_frame_rate, ts_timeline

logger = logging.getLogger(__name__)

# 구간을 받을 수 있는 컨텐츠 타입(ContentItem.content_type의 값). 클립 · 라이브는 없다
SECTION_CONTENT_TYPES = ("video", "m3u8", "hls_aes")


class SectionBasisError(Exception):
    """구간의 기준값을 조회할 수 없는 컨텐츠다 — 구간 기능이 없는 타입."""


@dataclass(frozen=True)
class SectionBasis:
    """구간 입력의 기준값을 담는다 — 고른 해상도의 스트림에서 읽은 값이다."""

    fps: Fraction  # 프레임률 — 타임코드의 FF를 읽고 구간을 검증하는 기준
    duration: float  # 영상 길이(초) — 마지막 영상 프레임이 끝나는 시각


@dataclass(frozen=True)
class SectionProbe:
    """조회 한 번의 결과 — 기준값과, 그것을 읽으면서 받은 것을 담는다.

    헤드리스는 ``head``를 엔진에 넘겨 같은 것을 다시 받지 않게 하고, ``segment_at``으로 구간마다
    받을 세그먼트를 계획한다. 편집 창은 ``basis``만 쓴다.

    암호화 VOD의 ``segment_at``은 복호화 키를 품고 있다 — 이 객체를 조회를 부른 함수 밖으로
    내보내지 않는다. ``head``에는 키가 없다.
    """

    basis: SectionBasis  # 프레임률 · 영상 길이
    # 프레임률을 정한 경로 — core.utils.section_plan의 FPS_DECLARED · FPS_STANDARD · FPS_MEASURED.
    # mp4는 샘플 표가 말한 값이라 FPS_DECLARED다
    fps_source: str
    head: Mp4Head | Fmp4Head | TsHead  # 받은 것 — 엔진에 넘길 수 있다
    # 세그먼트 번호 → 그 세그먼트의 프레임 정보(받아 둔 것은 다시 받지 않는다). mp4는 None
    segment_at: Callable[[int], object] | None = None


def probe_section_basis(item) -> SectionBasis:
    """카드가 고른 해상도의 프레임률과 실제 영상 길이를 조회한다.

    Args:
        item: 카드의 데이터(``ContentItem``) — content_type · base_url · vod_url ·
            resolution · stream을 읽는다. 바꾸지 않는다

    Raises:
        SectionBasisError: 구간 기능이 없는 타입인 경우
        Exception: 조회 · 해석 실패(네트워크 · 권한 · 형식). 호출자가 사용자 문구로 바꾼다 —
            원시 문자열에는 주소가 섞여 있어 그대로 보이지 않는다
    """
    kind = item.content_type
    if kind == "video":
        return probe_mp4(item.base_url).basis
    if kind == "m3u8":
        return probe_fmp4(item).basis
    if kind == "hls_aes":
        return probe_ts(item, declared=_declared_ts_rate(item)).basis
    raise SectionBasisError(f"구간 기능이 없는 타입이다: {kind!r}")


def probe_mp4(base_url: str) -> SectionProbe:
    """인코딩 완료 VOD — mp4의 moov를 받아 샘플 표에서 읽는다.

    Raises:
        Mp4Error: moov를 찾지 못했거나 해석하지 못한 경우
        requests.RequestException: 연결 실패 · 타임아웃 · HTTP 오류 상태
    """
    head = fetch_mp4_head(base_url)
    index = head.index
    return SectionProbe(
        basis=SectionBasis(fps=index.fps, duration=index.duration),
        fps_source=FPS_DECLARED,
        head=head,
    )


def probe_fmp4(item, segment_dir: str | None = None) -> SectionProbe:
    """인코딩 전 다시보기 — 플레이리스트 · 초기화 세그먼트 · 첫 세그먼트와 묶음의 끝 세그먼트에서 읽는다.

    프레임률은 마스터 플레이리스트의 FRAME-RATE, 없으면 첫 세그먼트의 프레임 평균 간격이다
    (``choose_frame_rate``). 정한 값은 받은 것에 실린다(``Fmp4Head.frame_rate``) — 엔진이 같은
    값으로 검증한다. 길이는 세그먼트의 실제 시각으로 잰 값이다(``fmp4_timeline``).

    Args:
        item: 받을 영상 — vod_url · resolution · stream(없어도 된다)을 읽는다
        segment_dir: 받은 세그먼트를 둘 폴더(엔진의 세그먼트 임시 폴더). None이면 본문을 버린다
    """
    content = Content(
        content_type=ContentType.CHZZK_VIDEO_M3U8,
        url=item.vod_url,
        resolution=item.resolution,
        # 목록에서 고른 변형 (#318) — 엔진이 받는 변형과 같은 변형에서 읽는다
        stream=getattr(item, "stream", None),
    )
    base_url, declared = resolve_m3u8_variant(content)
    head = fetch_fmp4_head(base_url, segment_dir)

    def segment_at(index: int):
        return segment_frames(head, base_url, index)

    choice = choose_frame_rate(head.init, [segment_at(0)], declared)
    head.frame_rate = choice.rate
    duration = fmp4_timeline(head.playlist, head.init, segment_at).duration
    return SectionProbe(
        basis=SectionBasis(fps=choice.rate, duration=duration),
        fps_source=choice.source,
        head=head,
        segment_at=segment_at,
    )


def probe_ts(
    item, segment_dir: str | None = None, declared: Fraction | None = None
) -> SectionProbe:
    """암호화 VOD — 플레이리스트와 복호화한 첫 · 끝 세그먼트에서 읽는다.

    프레임률은 매니페스트가 그 해상도에 선언한 값(declared)이 있으면 그 값이고, 없으면 첫
    세그먼트의 프레임 평균 간격이다(``choose_ts_frame_rate``). 정한 값은 받은 것에
    실린다(``TsHead.frame_rate``) — 엔진이 같은 값으로 계획한다.

    키는 엔진과 같은 리졸버(``resolve_aes_key`` — 본인 쿠키로 받는다)로 한 번 받는다. 받은
    것(``TsHead``)에 넣지 않고 로그에 적지 않는다 — 엔진은 받을 세그먼트가 남아 있으면 키를
    다시 받는다.

    Args:
        item: 받을 영상 — base_url(고른 해상도의 미디어 플레이리스트 주소) · vod_url ·
            resolution을 읽는다
        segment_dir: 받은(복호화한) 세그먼트를 둘 폴더. None이면 본문을 버린다
        declared: 매니페스트가 그 해상도에 선언한 프레임률. 없으면 None
    """
    base_url = item.base_url
    head = fetch_ts_head(base_url, segment_dir)
    content = Content(
        content_type=ContentType.CHZZK_VIDEO_HLS_AES,
        url=item.vod_url,
        resolution=item.resolution,
        base_url=base_url,
    )
    key = resolve_aes_key(content, ts_key_uri(base_url, head))

    def segment_at(index: int):
        return segment_streams(head, base_url, index, key)

    choice = choose_ts_frame_rate([segment_at(0)], declared)
    head.frame_rate = choice.rate
    duration = ts_timeline(head.playlist, segment_at, choice.rate).duration
    return SectionProbe(
        basis=SectionBasis(fps=choice.rate, duration=duration),
        fps_source=choice.source,
        head=head,
        segment_at=segment_at,
    )


def _declared_ts_rate(item) -> Fraction | None:
    """매니페스트가 고른 해상도에 선언한 프레임률. 읽지 못하면 None — 첫 세그먼트에서 잰다."""
    try:
        cookies = config.load_cookies()
        _kind, content_no = NetworkManager.extract_content_no(item.vod_url)
        info = NetworkManager.get_video_info(content_no, cookies)
        rates = NetworkManager.get_video_frame_rates(info.video_id, info.in_key, cookies)
    except Exception:
        logger.exception("선언된 프레임률을 읽지 못했다 — 첫 세그먼트에서 잰다")
        return None
    return rates.get(item.base_url)
