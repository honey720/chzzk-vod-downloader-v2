"""산출물 경로 유틸 — 파일명 조립·중복 회피·임시 폴더 명명 (#105).

산출물 파일명(`{제목} {해상도}p.mp4`) 조립이 GUI(content/manager.py)와
헤드리스 스크립트(scripts/headless_download.py)에 같은 식으로 중복돼
있었고, 어느 쪽도 기존 파일 존재를 확인하지 않아 같은 제목의 VOD를
받으면 이전 파일이 경고 없이 덮어써졌다. 이 모듈이 조립·중복 회피의
단일 지점이다 — 호출부는 build_output_path 하나만 부른다.

중복 회피 시점: 경로는 다운로드 시작 직전(output_path 배정 시점)에
확정된다. 앱은 다운로드를 한 건씩 순차 실행하므로(동시 실행 기본 1)
배정 시점의 존재 확인으로 충분하다. 같은 폴더에 같은 제목을 동시에
받는 별도 프로세스(GUI+헤드리스 병행 등)까지는 보장하지 않는다 —
존재 확인과 파일 생성 사이의 원자성이 없기 때문이며, 알려진 한계로
문서화한다.
"""

import hashlib
import os
import re
import shutil
import tempfile

from core.utils.disk_speed import measure_write_speed

# Windows에서 파일명에 쓸 수 없는 문자 + ASCII 제어 문자 전체(0x00–0x1F, 개행 포함).
# Windows는 제어 문자가 든 파일명 생성을 거부한다(EINVAL). 조회 단계
# (content/network.py)의 제목 정제도 이 함수를 쓰지만, core를 직접 쓰는 경로
# (헤드리스·다른 UI)가 정제를 빠뜨려도 안전하도록 여기서도 방어한다.
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:\*\?"<>|\x00-\x1f]')

# Windows 예약 장치명 — 이름 시작이 예약어이고 바로 뒤가 끝·점·공백이면 방어한다.
# 이 앱의 파일명은 항상 " {해상도}p.mp4"가 뒤에 붙어 이름 전체가 예약어와 일치할 수는
# 없지만, 레거시 장치명 판정(RtlIsDosDeviceName)은 "CON."처럼 예약어+점 시작도 장치로
# 볼 수 있다. Windows 11 실측으로는 CON.mp4도 생성되지만(예약 완화) 지원 대상인
# Windows 10이 여전히 예약하므로 보수적으로 막는다.
_RESERVED_DEVICE_NAMES = re.compile(r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?=$|[. ])")

# 산출물 전체 경로 길이 상한 — Windows 기본 MAX_PATH(260)에서 중복 회피
# 접미사 " (n)"과 임시 폴더 접두사("CVDv2_temp_") 여유분을 뺀 값
_MAX_FULLPATH = 240

# 파일명(구성요소 하나)의 UTF-8 바이트 길이 상한 — POSIX 파일시스템(ext4 등)의
# 구성요소 제한 255바이트에서 " (n)"·임시 폴더 접두사(11바이트) 여유분을 뺀 값.
# 한글은 UTF-8로 3바이트라 문자 수 상한(_MAX_FULLPATH)만으로는 리눅스에서
# ENAMETOOLONG이 날 수 있다 (헤드리스 스크립트·CI가 리눅스에서 돈다)
_MAX_FILENAME_BYTES = 240


def sanitize_filename(name: str) -> str:
    """파일명에 쓸 수 없는 문자를 제거하고 양끝 공백을 정리한다."""
    return _INVALID_FILENAME_CHARS.sub("", name).strip()


def ensure_unique_path(path: str) -> str:
    """같은 경로가 이미 있으면 확장자 앞에 " (n)"을 붙인 새 경로를 반환한다.

    기존 파일을 절대 덮어쓰지 않기 위한 장치다 (#105 필수 조건).
    비어 있는 이름이 나올 때까지 n을 1부터 올린다.
    """
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = f"{stem} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def build_output_path(directory: str, title: str, resolution: int) -> str:
    """산출물 전체 경로를 조립한다 — 정제·길이 제한·중복 회피 포함.

    파일명 형식은 기존과 동일한 `{제목} {해상도}p.mp4`이고, 같은 이름이
    이미 있을 때만 " (n)"이 붙는다. 전체 경로 문자 수 또는 파일명 바이트
    수가 상한을 넘으면 제목 부분만 잘라 맞추되(해상도 접미사·확장자 보존),
    원제목의 해시 6자리를 함께 붙인다 — 앞부분이 같은 긴 제목들이 절단 후
    동일해져 " (n)"만으로 구분되는(식별성 저하) 사태를 막기 위함이다.
    끝 점·공백 문제는 이름 끝에 항상 접미사가 붙는 구조라 발생하지
    않는다(제목의 점은 이름 중간에 놓인다).
    """
    safe_title = sanitize_filename(str(title)) or "video"
    if _RESERVED_DEVICE_NAMES.match(safe_title):
        safe_title = "_" + safe_title
    suffix = f" {resolution}p.mp4"
    candidate = os.path.join(directory, safe_title + suffix)
    # 상한 둘을 함께 지킨다: 전체 경로 문자 수(Windows MAX_PATH 대비)와
    # 파일명 구성요소의 UTF-8 바이트 수(POSIX 255바이트 대비)
    over_chars = len(candidate) - _MAX_FULLPATH
    over_bytes = len((safe_title + suffix).encode("utf-8")) - _MAX_FILENAME_BYTES
    if over_chars > 0 or over_bytes > 0:
        # 절단 표식 " ~{해시6}" — 같은 원제목이면 같은 이름(결정적),
        # 다른 원제목이면 절단 후에도 서로 다른 이름이 된다.
        # 문자·바이트 어느 쪽 절단이든 동일하게 붙는다
        digest = hashlib.sha1(safe_title.encode("utf-8")).hexdigest()[:6]
        marker = f" ~{digest}"
        keep = max(1, len(safe_title) - over_chars - len(marker))
        clipped = safe_title[:keep]
        # 바이트 예산으로 한 번 더 자른다 — UTF-8 바이트열을 자른 뒤 디코드에서
        # 깨진 꼬리 시퀀스를 버리는 방식이라 문자 경계(한글 3바이트·이모지
        # 4바이트)가 깨지지 않는다. 문자 절단만 일어난 경우엔 no-op이다
        byte_budget = _MAX_FILENAME_BYTES - len((marker + suffix).encode("utf-8"))
        clipped = clipped.encode("utf-8")[:byte_budget].decode("utf-8", "ignore")
        candidate = os.path.join(directory, clipped.rstrip() + marker + suffix)
    return ensure_unique_path(candidate)


def temp_dir_for(output_path: str, base_dir: str | None = None) -> str:
    """세그먼트 임시 폴더 경로를 산출물 파일명에서 파생한다.

    구 코드는 고정 이름("CVDv2_temp") 하나를 모든 다운로드가 공유해,
    같은 폴더로 향하는 두 다운로드가 겹치면 서로의 세그먼트 폴더를
    삭제·재생성할 수 있었다 (#105 확인 항목 4). 산출물 파일명(중복
    회피 후)에서 파생하면 다운로드마다 폴더가 구분된다.

    base_dir을 주면 그 디렉토리 아래에 만든다(#192 — 임시 폴더를 산출물과
    다른 매체로 보낼 때). 기본은 산출물과 같은 디렉토리(기존 동작 무변경).
    """
    directory = base_dir if base_dir is not None else os.path.dirname(output_path)
    stem = os.path.splitext(os.path.basename(output_path))[0]
    return os.path.join(directory, f"CVDv2_temp_{stem}")


# 임시 폴더를 분리할 스크래치 볼륨에 요구하는 최소 여유 공간 (#192).
# m3u8·hls_aes는 산출물 크기를 미리 모른다(DownloadPlan.total_size=None) —
# 정확한 소요량을 계산할 방법이 없으므로, 대부분의 VOD보다 넉넉한 고정
# 임계로 보수적으로 게이트한다. 이 추정이 틀려도(아주 긴 고화질 VOD 등)
# 최악의 결과는 디스크 부족 실패이며, 이는 분리하지 않았을 때도 스크래치
# 볼륨이 아닌 산출물 볼륨에서 똑같이 날 수 있는 기존 실패 부류다(#146
# 감사의 OSError 최후 방어선이 이미 처리한다) — 분리 여부와 무관한 위험이다.
_MIN_SCRATCH_FREE_BYTES = 10 * 1024**3  # 10 GiB

# 스크래치 볼륨이 산출물 볼륨보다 "뚜렷이" 빠르다고 볼 배수. 애매하면
# 분리하지 않는다는 원칙(#192)이라 노이즈에 흔들리지 않을 만큼 크게 잡는다
# — exFAT SD 카드 vs 로컬 SSD 실측(후처리 36배 차이)에 비하면 여유롭게
# 낮은 문턱이지만, 절반 정도 차이 나는 애매한 경우까지 분리로 몰지 않는다
_SEPARATION_SPEED_MARGIN = 2.0

# 시스템 임시 폴더 아래 스크래치 전용 서브폴더 이름. config.APP_NAME과
# 뜻은 같지만 core는 app(config)을 import할 수 없어(SPEC §3.1) 문자열을
# 이 자리에서 그대로 쓴다 — 실제 폴더 구분은 이름이 아니라 위치(시스템
# 임시 폴더 vs 산출물 폴더)가 하므로 문제되지 않는다
_SCRATCH_SUBDIR = "CVDv2_scratch"


def _scratch_base_dir() -> str:
    """시스템 임시 폴더 아래 앱 전용 스크래치 디렉토리 경로."""
    return os.path.join(tempfile.gettempdir(), _SCRATCH_SUBDIR)


def _same_volume(a: str, b: str) -> bool:
    """두 경로가 같은 볼륨(장치)에 있는지 — 실패하면 보수적으로 "같다"로 본다.

    확인할 수 없으면 분리할 근거도 없다("애매하면 분리하지 않는다").
    """
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True


def choose_temp_dir(output_path: str) -> str:
    """세그먼트 임시 폴더 위치를 정한다 (#192).

    기본은 산출물과 같은 폴더(기존 동작) — 아래 조건을 **모두** 만족할
    때만 시스템 임시 폴더(로컬 매체일 가능성이 높다)로 보낸다. 하나라도
    실패·불확실하면 분리하지 않는다("애매하면 분리하지 않는다" 원칙,
    #192 — 잘못 분리해 시스템 디스크를 채우는 것이 그냥 느린 것보다
    나쁘다):

    1. 스크래치 볼륨에 여유 공간이 넉넉한가(고정 임계 — 위 상수 참고)
    2. 산출물 폴더와 스크래치 폴더가 이미 같은 볼륨이면 분리할 이유가
       없다(비교할 것도 없이 그대로 둔다)
    3. 스크래치 폴더가 실제로 쓰기 가능하고, 산출물 폴더보다 뚜렷이
       (_SEPARATION_SPEED_MARGIN배 이상) 빠른가

    산출물 위치 자체는 절대 바꾸지 않는다 — ffmpeg가 세그먼트를 파이프로
    받아 산출물에 직접 쓰므로(core/utils/ffmpeg.py) 복사 단계가 없다.
    """
    default = temp_dir_for(output_path)
    output_dir = os.path.dirname(output_path) or "."
    scratch_base = _scratch_base_dir()

    try:
        os.makedirs(scratch_base, exist_ok=True)
    except OSError:
        return default

    try:
        usage = shutil.disk_usage(scratch_base)
    except OSError:
        return default
    if usage.free < _MIN_SCRATCH_FREE_BYTES:
        return default

    if _same_volume(scratch_base, output_dir):
        return default

    scratch_speed = measure_write_speed(scratch_base)
    if not scratch_speed:
        return default

    output_speed = measure_write_speed(output_dir)
    if not output_speed:
        # 산출물 폴더 속도를 못 재면 비교 기준이 없다 — 애매하니 분리하지 않는다
        return default

    if scratch_speed < output_speed * _SEPARATION_SPEED_MARGIN:
        return default

    return temp_dir_for(output_path, base_dir=scratch_base)
