"""파일 시스템 이름 읽기와 FAT 판정 (#309). 대상: core/utils/filesystem.py.

OS의 호출은 바꿔치기한다 — 러너의 디스크가 무엇인지에 기대지 않는다. 실제 호출은 "예외 없이
이름이나 None을 돌려준다"까지만 잰다.
"""

import sys

import pytest

import core.utils.filesystem as fs_module
from core.utils.filesystem import FAT_MAX_FILE_BYTES, filesystem_name, is_fat, mount_type

# 이 러너에서 filesystem_name이 부르는 읽기 함수의 이름
READER = {"win32": "_windows_name", "darwin": "_macos_name"}.get(sys.platform, "_linux_name")


def test_fat_limit_is_four_gibibytes_minus_one():
    """FAT 계열의 파일 하나의 최대 크기는 4GiB − 1바이트여야 한다.

    -> FAT_MAX_FILE_BYTES == 4,294,967,295
    """
    assert FAT_MAX_FILE_BYTES == 4_294_967_295


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("FAT32", True),  # Windows
        ("FAT", True),
        ("vfat", True),  # Linux
        ("msdos", True),  # macOS · Linux
        (" fat32 ", True),  # 앞뒤 빈칸 · 소문자
        ("exFAT", False),
        ("exfat", False),
        ("NTFS", False),
        ("ext4", False),
        ("apfs", False),
        ("", False),
        (None, False),  # 읽지 못했다
    ],
)
def test_is_fat_only_for_the_fat_family(name, expected):
    """is_fat은 FAT 계열의 이름에만 참이어야 한다 — exFAT과 읽지 못한 이름(None)은 거짓이다.

    이름마다의 기대값은 표의 것
    """
    assert is_fat(name) is expected


MOUNTS = (
    "/dev/sda1 / ext4 rw,relatime 0 0\n"
    "/dev/sdb1 /media/usb vfat rw,nosuid 0 0\n"
    "/dev/sdc1 /media/usb/inner exfat rw 0 0\n"
    "/dev/sdd1 /media/my\\040disk vfat rw 0 0\n"
    "broken line\n"
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/home/user/video", "ext4"),  # 뿌리만 덮는다
        ("/media/usb", "vfat"),  # 마운트 지점 그 자체
        ("/media/usb/folder", "vfat"),
        ("/media/usb/inner/folder", "exfat"),  # 더 긴 마운트 지점이 이긴다
        ("/media/usbstick", "ext4"),  # 이름의 앞부분만 같은 폴더는 그 마운트 안이 아니다
        ("/media/my disk/folder", "vfat"),  # 8진수로 적힌 빈칸
    ],
)
def test_mount_type_is_the_type_of_the_longest_mount_point_over_the_path(path, expected):
    """mount_type은 그 경로를 덮는 가장 긴 마운트 지점의 파일 시스템 종류를 돌려줘야 한다.

    마운트 넷(/ ext4 · /media/usb vfat · /media/usb/inner exfat · "/media/my disk" vfat)
    -> 경로마다의 기대값은 표의 것
    """
    assert mount_type(MOUNTS, path) == expected


def test_mount_type_is_none_when_no_mount_point_covers_the_path():
    """그 경로를 덮는 마운트 지점이 없으면 mount_type은 None이어야 한다.

    마운트가 /media/usb 하나뿐, 경로 /home -> None
    """
    assert mount_type("/dev/sdb1 /media/usb vfat rw 0 0\n", "/home") is None


def test_filesystem_name_asks_about_the_nearest_folder_that_exists(tmp_path, monkeypatch):
    """아직 없는 폴더의 파일 시스템은 실제로 있는 윗폴더에서 읽어야 한다.

    tmp_path/없음/더없음 -> 읽기 함수가 받은 경로 == tmp_path, 돌려준 이름 그대로
    """
    asked = []

    def reader(path):
        asked.append(path)
        return "FAT32"

    monkeypatch.setattr(fs_module, READER, reader)

    name = filesystem_name(str(tmp_path / "없음" / "더없음"))

    assert (name, asked) == ("FAT32", [str(tmp_path)])


@pytest.mark.parametrize("error", [OSError("읽기 실패"), ValueError("모양이 다르다")])
def test_filesystem_name_is_none_when_it_cannot_be_read(tmp_path, monkeypatch, error):
    """파일 시스템 이름을 읽다 실패하면 예외가 아니라 None이어야 한다.

    읽기 함수가 OSError 또는 ValueError -> None
    """

    def failing(path):
        raise error

    monkeypatch.setattr(fs_module, READER, failing)

    assert filesystem_name(str(tmp_path)) is None


def test_filesystem_name_of_a_real_folder_is_a_name_or_none(tmp_path):
    """실제 폴더에 물으면 예외 없이 이름(빈 글이 아닌 것)이나 None을 돌려줘야 한다.

    tmp_path -> None이거나 길이가 1 이상인 글
    """
    name = filesystem_name(str(tmp_path))

    assert name is None or (isinstance(name, str) and name)
