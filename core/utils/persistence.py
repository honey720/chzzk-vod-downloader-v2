"""복구 기록을 원자적으로 저장하고 프로세스 사이의 쓰기를 잠근다."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class StorageBusyError(OSError):
    """다른 실행이 같은 저장소를 사용하고 있다."""


def atomic_json(path: Path, value: Any) -> None:
    """같은 디렉터리에서 기록을 완성한 후 원자적으로 교체한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class FileLease:
    """운영체제 파일 잠금으로 실행 중인 소유자 하나만 허용한다.

    잠금 파일은 지우지 않는다. 종료 후 운영체제가 잠금만 해제하므로 강제 종료에도
    낡은 PID 때문에 복구가 막히거나 파일 삭제 경쟁으로 잠금이 갈라지지 않는다.
    """

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> None:
        """쓰기 소유권을 즉시 얻거나 사용 중 오류를 낸다."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise StorageBusyError("다른 실행이 이 작업을 사용하고 있습니다") from exc
        self._file = stream

    def release(self) -> None:
        """소유한 잠금만 해제한다."""
        if self._file is not None:
            self._file.close()
            self._file = None


def publish_file(source: Path, destination: Path) -> None:
    """완성 파일을 원자적으로 공개하며 이미 있는 파일은 덮어쓰지 않는다."""
    import sys

    if sys.platform == "darwin":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(os.fsencode(source), os.fsencode(destination), 4) != 0:  # RENAME_EXCL
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
    elif os.name == "nt":
        os.rename(source, destination)  # Windows는 대상이 이미 있으면 실패한다.
    else:
        os.link(source, destination)
        source.unlink()
