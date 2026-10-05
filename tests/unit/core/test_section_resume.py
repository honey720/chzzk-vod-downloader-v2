"""SectionResume — 이전 실행이 끝낸 구간과 받아 둔 데이터를 담는 모델 (#309).

- 어느 실행에 쓸 수 있는지(``fits``)는 구간 목록과 끝낸 구간의 경로로 가린다
- 받아 둔 것(moov · 플레이리스트 · 프레임 정보)은 repr에 나오지 않는다 — 서명 값이 붙은
  세그먼트 주소 · 키 주소 · 받은 바이트가 이 모델의 repr을 따라 로그로 나가지 않는다
"""

from core.api.hls import HlsKey, HlsPlaylist
from core.models.content import Content, ContentType
from core.models.mp4_index import Mp4Head
from core.models.plan import TimeRange
from core.models.section_resume import SectionResume
from core.models.ts_index import TsHead

SELECTIONS = (TimeRange(1.0, 2.0), TimeRange(3.0, 4.0), TimeRange(5.0, 6.0))
PATHS = ("out/방송 1080p_1.mp4", "out/방송 1080p_2.mp4", "out/방송 1080p_3.mp4")
# 요청마다 바뀌는 서명 값의 자리 — 실제 값이 아니다
SIGNED_SEGMENT = "https://cdn.test/v/segment-000001.ts?hdntl=exp~SIGNED-SEGMENT-VALUE"
SIGNED_KEY_URI = "https://key.test/k?token=SIGNED-KEY-VALUE"
MOOV_BYTES = b"MOOV-BYTES-MARKER"


def test_fits_a_run_with_the_same_sections_and_the_same_paths_of_the_finished_ones():
    """fits는 구간 목록이 같고 끝낸 구간의 경로가 같으면 참이어야 한다 — 끝나지 않은 구간의 경로는 달라도 된다.

    끝낸 구간 {0, 2}. 같은 구간 목록에 1번 경로만 "… (1).mp4"로 바뀐 경로
    -> True
    """
    resume = SectionResume(selections=SELECTIONS, paths=PATHS, done=frozenset({0, 2}))
    renamed = (PATHS[0], "out/방송 1080p_2 (1).mp4", PATHS[2])

    assert resume.fits(SELECTIONS, renamed) is True


def test_does_not_fit_a_run_whose_finished_section_has_another_path():
    """fits는 끝낸 구간의 경로가 다르면 거짓이어야 한다.

    끝낸 구간 {0, 2}. 같은 구간 목록에 0번 경로가 "… (1).mp4"로 바뀐 경로
    -> False
    """
    resume = SectionResume(selections=SELECTIONS, paths=PATHS, done=frozenset({0, 2}))
    renamed = ("out/방송 1080p_1 (1).mp4", PATHS[1], PATHS[2])

    assert resume.fits(SELECTIONS, renamed) is False


def test_does_not_fit_a_run_with_other_sections():
    """fits는 구간 목록이 다르면 거짓이어야 한다.

    끝낸 구간 {0}. 둘째 구간의 끝이 다른 구간 목록, 경로는 그대로
    -> False
    """
    resume = SectionResume(selections=SELECTIONS, paths=PATHS, done=frozenset({0}))
    others = (SELECTIONS[0], TimeRange(3.0, 4.5), SELECTIONS[2])

    assert resume.fits(others, PATHS) is False


def test_repr_does_not_show_what_was_received():
    """SectionResume과 그것을 담은 Content의 repr에는 받아 둔 것의 내용이 없어야 한다.

    서명 값이 붙은 세그먼트 주소 · 키 주소를 든 플레이리스트의 TsHead와, 표식 바이트를 든 Mp4Head를 담음
    -> repr(SectionResume) · repr(Content) 어디에도 서명 값 · 키 주소 · 표식 바이트가 없고,
       구간 파일 경로는 있다(repr이 비어 있지 않다)
    """
    playlist = HlsPlaylist(
        segments=(SIGNED_SEGMENT,), key=HlsKey(method="AES-128", uri=SIGNED_KEY_URI)
    )
    resume = SectionResume(
        selections=SELECTIONS,
        paths=PATHS,
        done=frozenset({0}),
        mp4_head=Mp4Head(index=None, data=MOOV_BYTES),
        ts_head=TsHead(playlist=playlist, segment_dir="out/CVDv2_temp"),
    )
    content = Content(content_type=ContentType.CHZZK_VIDEO_HLS_AES, url="https://chzzk.test/v/1")
    content.section_resume = resume

    for text in (repr(resume), repr(content)):
        assert PATHS[0] in text
        for secret in ("SIGNED-SEGMENT-VALUE", "SIGNED-KEY-VALUE", "hdntl", "MOOV-BYTES-MARKER"):
            assert secret not in text
