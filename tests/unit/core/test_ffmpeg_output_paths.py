"""저장 경로 특성별 remux 파이프라인 실측 (#145 — #144·#146 조사 후속).

pytest tmp_path 베이스에는 세 OS 모두 공백이 없어(리눅스 /tmp/..., Windows
%LOCALAPPDATA%\\Temp, macOS /private/var/folders/...) 기존 테스트 전체가
공백 경로를 한 번도 밟지 않는 사각지대가 있었다(#146 감사: 긴 디렉토리
프리픽스·산출물 이름 충돌·NFC/NFD도 동일). 이 파일은 그 경로 특성들에서
경로 조립(build_output_path/temp_dir_for)과 실제 ffmpeg remux까지 —
다운로드 꼬리 구간 — 를 실행으로 고정한다.

파일 이름에 "ffmpeg"를 넣은 이유: release.yml의 "Verify ffmpeg (3-OS)"
스텝이 `pytest -q -k ffmpeg`를 Windows·macOS·리눅스에서 릴리즈마다
실행하므로, 여기 담긴 케이스는 macOS·리눅스 실측이 영구적으로 따라온다.

케이스 구분(#144 조사 ⑤ · #145 확장):
- 공용(3-OS): 내부 공백·다중 공백, Windows에서도 합법인 특수문자 조합,
  긴 디렉토리 프리픽스(제목 절단 경로), 산출물 이름 충돌(" (n)" 회피)
- 공용이되 확인 대상: U+00A0·U+3000 — Windows NTFS 실측은 통과했고
  macOS·리눅스는 이 테스트의 CI 실행이 확정한다. 특정 OS에서 폴더 생성
  자체가 실패하면(테스트 설계 문제, 앱 결함 아님) 그 OS만 조건부 skip으로
  내린다
- Windows skip: 후행 공백 — Windows는 생성 시 조용히 절단한다(실측)
- macOS 전용: NFC/NFD 정규화 무관 조회 단언
- 다루지 않는 것: 실제 권한 거부(러너별 권한 구성이 달라 불안정 — 모의
  방식의 tests/unit/test_write_probe.py가 담당), OS 장경로 설정 의존 구간
  (전체 260자 초과는 러너 레지스트리 설정에 좌우된다)
"""

import os
import subprocess
import sys
import unicodedata

import pytest

from core.utils.ffmpeg import get_ffmpeg_exe, read_in_chunks, remux_stream
from core.utils.paths import build_output_path, temp_dir_for


@pytest.fixture(scope="module")
def fmp4_input(tmp_path_factory):
    """1초짜리 h264 fMP4 — remux 공급용(모듈당 1회 생성).

    fMP4를 쓰는 이유: 리눅스 동봉 빌드의 mpegts demux SIGSEGV(#94)를 타지
    않아 3-OS 전부에서 스킵 없이 돈다. libx264는 동봉 GPL 빌드 3-OS 공통
    (tests/unit/core/test_ffmpeg_utils.py의 _generate와 같은 근거).
    """
    path = str(tmp_path_factory.mktemp("space-src") / "src.mp4")
    subprocess.run(
        [
            get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=10",
            "-c:v",
            "libx264",
            "-movflags",
            "frag_keyframe+empty_moov",
            "-f",
            "mp4",
            path,
        ],
        check=True,
    )
    return path


def _run_download_tail(directory: str, fmp4_input: str) -> str:
    """다운로드 꼬리 구간을 실경로로 실행한다: 조립 → 임시 폴더 → remux → 크기 확인."""
    out = build_output_path(directory, "제목 스페이스 검증", 1080)
    temp_dir = temp_dir_for(out)
    os.makedirs(temp_dir)  # 엔진과 동일하게 exist_ok 없이 (m3u8 _prepare_output과 같은 조건)
    remux_stream(read_in_chunks(fmp4_input), out)
    assert os.path.getsize(out) > 0
    return out


# 공용(3-OS) 케이스 — id에 코드포인트를 명시한다(화면 표기로는 U+00A0과
# U+0020을 구분할 수 없다). U+00A0은 macOS에서 Option+Space로 쉽게 입력되는
# 문자라 #144류 제보 재현의 핵심 후보다.
PORTABLE_DIRNAMES = [
    pytest.param("test test", id="space-U+0020"),
    pytest.param("test  test", id="double-space"),
    pytest.param("test\u00a0test", id="nbsp-U+00A0"),
    pytest.param("test\u3000test", id="ideographic-space-U+3000"),
    pytest.param("test #1", id="hash-with-space"),
    pytest.param("test (1)", id="parens-with-space"),
    pytest.param("my test&run", id="ampersand-with-space"),
    pytest.param("한글 폴더", id="hangul-with-space"),
]


@pytest.mark.parametrize("dirname", PORTABLE_DIRNAMES)
def test_ffmpeg_remux_into_special_char_dir(tmp_path, fmp4_input, dirname):
    """공백·특수문자 폴더에서 경로 조립→임시 폴더→remux가 완주한다 (#144)."""
    directory = tmp_path / dirname
    directory.mkdir()

    _run_download_tail(str(directory), fmp4_input)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows는 생성 시 후행 공백을 조용히 절단한다(실측) — POSIX 전용 케이스",
)
def test_ffmpeg_remux_into_trailing_space_dir(tmp_path, fmp4_input):
    """후행 공백 폴더(POSIX 합법)에서도 파이프라인이 완주한다."""
    directory = tmp_path / "trail test "
    directory.mkdir()

    _run_download_tail(str(directory), fmp4_input)


# ================================================================ 긴 디렉토리 프리픽스


def test_ffmpeg_remux_into_long_directory_prefix(tmp_path, fmp4_input):
    """긴 폴더 경로에서 제목 절단(~해시) 경로로 remux가 완주한다 (#146 감사 확장).

    디렉토리를 210자 이상으로 만들어 전체 경로 상한(_MAX_FULLPATH=240)에
    걸리게 한다 — 제목이 " ~해시" 표식으로 절단되는 경로가 실행된다.
    260자를 넘기지는 않는다: 그 구간은 OS 장경로 설정(레지스트리)에
    좌우되어 러너별 결과가 달라진다(문서화된 미커버 구간).
    """
    # 목표 길이를 정확히 맞춘다 — 러너마다 tmp_path 베이스 길이가 달라,
    # 고정 폭 세그먼트를 쌓으면 총길이가 튀어 Windows 기본 상한(260)을
    # 장경로 설정 여부에 따라 넘나들 수 있다
    # 225 = 전체 경로가 상한(_MAX_FULLPATH=240)을 확실히 넘도록 하는 값
    # (파일명 "제목 스페이스 검증 1080p.mp4" ≈ 20자 → 총 246자 → 절단 발동).
    # 절단 후 임시 폴더까지 260자 미만이라 Windows 장경로 설정과 무관하다
    target = 225
    directory = tmp_path
    while len(str(directory)) < target:
        directory = directory / ("d" * min(50, target - len(str(directory)) - 1))
    directory.mkdir(parents=True)

    out = _run_download_tail(str(directory), fmp4_input)

    assert "~" in os.path.basename(out)  # 절단 표식 경로를 실제로 지났다
    assert len(out) <= 260  # Windows 기본 MAX_PATH 안 — 장경로 설정 무의존


# ================================================================ 산출물 이름 충돌


def test_ffmpeg_remux_output_collision_gets_suffix(tmp_path, fmp4_input):
    """같은 이름 산출물이 이미 있으면 " (1)"로 회피한 경로에 remux가 완주한다."""
    directory = tmp_path / "collision test"
    directory.mkdir()
    first = build_output_path(str(directory), "제목 스페이스 검증", 1080)
    with open(first, "wb") as f:
        f.write(b"existing")

    out = _run_download_tail(str(directory), fmp4_input)

    assert out != first and out.endswith(" (1).mp4")
    assert os.path.getsize(first) == 8  # 기존 파일은 덮어쓰지 않는다 (#105)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS 파일 시스템의 정규화 무관 조회 검증 — 타 OS는 코드포인트 그대로 저장되어 성립하지 않는다(실측)",
)
def test_ffmpeg_space_path_nfd_dir_visible_via_nfc(tmp_path, fmp4_input):
    """NFD로 만든 한글 폴더가 NFC 경로 문자열로도 보이고 파이프라인이 완주한다.

    macOS 로컬 디스크(APFS/HFS+)의 정규화 무관 조회가 실제로 성립하는지의
    실측이다. 이 단언이 깨지는 환경(정규화 민감 마운트 등)이라면 "겉보기
    같은 경로가 존재하지 않음"류 증상(#144)이 코드 결함 없이 발생할 수 있다.
    """
    nfc_name = "한글 테스트"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    (tmp_path / nfd_name).mkdir()

    nfc_path = str(tmp_path / nfc_name)
    assert os.path.isdir(nfc_path)
    _run_download_tail(nfc_path, fmp4_input)
