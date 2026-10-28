"""오류 글을 자르는 자리 네 곳이 경로 한가운데서 자르지 않는지 (#429 · #398 후속).

ffmpeg의 실패 글(stderr)은 자리마다 끝 N 글자쯤만 실패 글에 붙인다(``error_tail``). 글자 수로만
자르면(``.strip()[-N:]``) 경로 한가운데 — 홈 폴더의 사용자 이름 안 — 에서 시작해 로그가 홈 경로를
가리는 규칙을 빠져나간다. ffmpeg 대역이 경로가 든 실패 글을 내게 하고, 글자 수 꼬리가 사용자
이름 안에서 시작하도록 글의 길이를 맞춘 뒤, 그 자리를 지나 만든 실패 글에 경로가 통째로 남는지 본다.
"""

import os
import stat
import sys
import textwrap
from types import SimpleNamespace

import pytest

import core.utils.ffmpeg as ffmpeg_module
import core.sections.hybrid as hybrid_cut
from core.utils.ffmpeg import RemuxError
from core.sections.hybrid import CutError

HOME_PATH = (
    r"C:\Users\Tester"  # 실패 글 속 경로의 홈 부분 — 글자 수 꼬리가 "Tester" 안에서 시작한다
)
FAILED_PATH = HOME_PATH + r"\Videos\clip.mp4"


def _stderr_cut_inside_the_name(limit: int) -> str:
    """끝 ``limit`` 글자의 꼬리가 "Tester"의 셋째 글자에서 시작하는 실패 글.

    경로가 든 줄 뒤에 다른 줄이 이어진다 — 줄 머리까지 늘리는 자르기는 경로가 든 줄을 통째로 남긴다.
    """
    head = "ffmpeg version test\n"
    line = f"Error opening input file {FAILED_PATH}.\n"
    cut_at = len(head) + line.index("Tester") + 2  # 글자 수 꼬리의 시작 — "Te|ster"
    after = "x" * (cut_at + limit - len(head) - len(line))
    text = head + line + after
    assert text.strip()[-limit:].startswith("ster")  # 전제: 글자 수로 자르면 이름 안에서 시작한다
    return text


def _failed(stderr: str):
    """returncode 1과 그 stderr를 돌려주는 ffmpeg 실행의 대역(hybrid_cut._ffmpeg 자리)."""

    def fake(args, **kwargs):
        return SimpleNamespace(returncode=1, stderr=stderr, stdout="")

    return fake


@pytest.mark.parametrize(
    ("site", "limit"),
    [
        (lambda: hybrid_cut._run(["-i", "in.mp4", "out.mp4"], 10, "."), 500),
        (lambda: hybrid_cut._read_params("in.mp4", 0), 300),
        (lambda: hybrid_cut.read_packets("in.mp4"), 300),
    ],
    ids=["_run", "_read_params", "read_packets"],
)
def test_cut_failure_text_keeps_the_whole_path_where_its_tail_is_cut(monkeypatch, site, limit):
    """컷의 ffmpeg 실패 글은 끝을 잘라 붙일 때 경로를 가르지 않고 경로가 든 줄을 통째로 남겨야 한다.

    hybrid_cut의 ffmpeg 실행 대역이 returncode 1과, 끝 limit 글자 꼬리가 사용자 이름 "Tester" 안에서
    시작하는 실패 글을 냄. 주석의 자리(_run 500 · _read_params 300 · read_packets 300)를 부름
    -> CutError의 글에 r"C:\\Users\\Tester\\Videos\\clip.mp4"가 통째로 있다
    """
    monkeypatch.setattr(hybrid_cut, "_ffmpeg", _failed(_stderr_cut_inside_the_name(limit)))

    with pytest.raises(CutError) as info:
        site()

    assert FAILED_PATH in str(info.value)


_FAKE_FFMPEG = textwrap.dedent(
    """
    import os
    import sys

    with open(os.environ["FAKE_FFMPEG_STDERR"], encoding="utf-8") as f:
        sys.stderr.write(f.read())
    sys.exit(1)
    """
)


@pytest.fixture
def failing_ffmpeg(tmp_path, monkeypatch):
    """실패 글을 내고 1로 끝나는 ffmpeg 대역 실행 파일 — get_ffmpeg_exe가 그것을 돌려준다.

    글은 ``FAKE_FFMPEG_STDERR``가 가리키는 파일에서 읽는다. Windows는 .cmd, 그 밖은 실행 비트를 준
    셸 스크립트로 파이썬 스크립트를 부른다.
    """
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(_FAKE_FFMPEG, encoding="utf-8")
    if sys.platform == "win32":
        exe = tmp_path / "fake_ffmpeg.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "fake_ffmpeg"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(ffmpeg_module, "get_ffmpeg_exe", lambda: str(exe))

    def give(stderr: str) -> None:
        path = tmp_path / "stderr.txt"
        path.write_text(stderr, encoding="utf-8")
        monkeypatch.setenv("FAKE_FFMPEG_STDERR", str(path))

    return give


def test_remux_through_failure_text_keeps_the_whole_path_where_its_tail_is_cut(
    tmp_path, failing_ffmpeg
):
    """두 ffmpeg를 잇는 재포장의 실패 글은 프로세스마다 끝을 잘라 붙일 때 경로를 가르지 않아야 한다.

    ffmpeg 대역 실행 파일: 변환 · 잇기 둘 다 1로 끝나며, 끝 250 글자 꼬리가 사용자 이름 "Tester" 안에서
    시작하는 실패 글을 냄. remux_through(넣을 바이트 없음)
    -> RemuxError의 글에 r"C:\\Users\\Tester\\Videos\\clip.mp4"가 (두 프로세스 몫으로) 통째로 있다
    """
    failing_ffmpeg(_stderr_cut_inside_the_name(250))

    with pytest.raises(RemuxError) as info:
        ffmpeg_module.remux_through(
            [], ["-i", "pipe:0", "pipe:1"], ["-i", "pipe:0", str(tmp_path / "out.mp4")],
            str(tmp_path / "out.mp4"), timeout=30,
        )  # fmt: skip

    assert str(info.value).count(FAILED_PATH) == 2
    assert not os.path.exists(tmp_path / "out.mp4")
