"""m3u8 작업의 정상 조각과 목록 지문을 영구 저장한다."""

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from core.downloaders.integrity import TruncatedSegmentError, check_fmp4_media_segment
from core.models.content import Content
from core.utils.persistence import FileLease, atomic_json
from core.utils.paths import resume_dir_for


class ResumeMismatchError(Exception):
    """기존 조각과 새 목록의 호환성을 확인할 수 없다."""


def digest_file(path: Path) -> str:
    """파일의 해시를 메모리에 전체 파일을 올리지 않고 계산한다."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resource_identity(uri: str) -> str:
    """인증용 쿼리를 제외한 자원 식별자를 지문 계산에 사용한다."""
    parsed = urlsplit(uri)
    ephemeral = {"__gda__", "token", "signature", "expires", "policy", "key-pair-id"}
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in ephemeral]
    return parsed.path + "?" + urlencode(sorted(query))


def job_key(content: Content) -> str:
    """표시 제목과 만료되는 주소에 의존하지 않는 작업 식별자를 만든다."""
    identity = [
        urlsplit(content.url).path,
        content.content_type.value,
        str(content.resolution),
        asdict(content.stream) if content.stream else None,
    ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]


class ResumeStore:
    """한 영상 변형의 확정된 조각과 복구 기록을 소유한다."""

    def __init__(self, content: Content):
        self.directory = Path(resume_dir_for(content.output_path, job_key(content)))
        self.path = self.directory / "resume.json"
        self.lease = FileLease(self.directory / ".lock")
        self.content = content
        self.record: dict = {}
        self.count = 0
        self.width = 1
        self._mutex = threading.Lock()
        self._dirty = 0
        self._saved_at = time.monotonic()

    def open(self) -> None:
        """작업을 잠그고 기존 복구 기록을 읽는다."""
        self.lease.acquire()
        if not self.path.exists() and any(
            p.is_file() and p.name.endswith((".m4v", ".m4s", ".part", ".receiving"))
            for p in self.directory.iterdir()
        ):
            raise ResumeMismatchError("복구 기록 없는 조각은 자동 재사용하지 않습니다")
        if self.path.exists():
            try:
                record = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or record.get("version") != 1:
                    raise ValueError
                if not isinstance(record.get("segments"), dict):
                    raise ValueError
                self.record = record
            except (ValueError, TypeError) as exc:
                raise ResumeMismatchError(
                    "복구 기록을 읽을 수 없습니다. 데이터는 보존됩니다"
                ) from exc

    def adopt(self, lines: list[str], segments: list[str], init: bytes, output: str) -> None:
        """최신 목록과 초기화 조각이 기존 작업과 같을 때만 연결한다."""
        tags = [
            line
            for line in lines
            if line.startswith(
                (
                    "#EXTINF:",
                    "#EXT-X-MEDIA-SEQUENCE:",
                    "#EXT-X-DISCONTINUITY",
                )
            )
        ]
        identity = [
            tags,
            [resource_identity(s) for s in segments],
            hashlib.sha256(init).hexdigest(),
        ]
        fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if self.record and self.record.get("fingerprint") != fingerprint:
            raise ResumeMismatchError("영상 구성이 변경되었습니다. 기존 데이터는 보존됩니다")
        self.count = len(segments)
        self.width = len(str(self.count))
        if not self.record:
            self.record = {
                "version": 1,
                "fingerprint": fingerprint,
                "count": self.count,
                "output": Path(output).name,
                "segments": {},
                "complete": False,
                "video_path": urlsplit(self.content.url).path,
                "resolution": self.content.resolution,
                "stream": asdict(self.content.stream) if self.content.stream else None,
                "phase": "downloading",
            }
            self.flush()
        elif self.record.get("output") != Path(output).name:
            raise ResumeMismatchError("이 영상의 복구 파일명이 다릅니다. 기존 작업을 사용해 주세요")

    def segment_path(self, index: int) -> Path:
        """플레이리스트의 0 기반 순번에 대응하는 확정 조각 경로를 반환한다."""
        return self.directory / f"{index + 1:0{self.width}d}.m4v"

    def verified(self) -> dict[int, int]:
        """남은 파일의 구조와 기록된 해시를 확인하고 기록 전 종료된 조각도 복구한다."""
        result = {}
        for index in range(self.count):
            path = self.segment_path(index)
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
            try:
                with path.open("rb") as stream:
                    check_fmp4_media_segment(stream, size)
                digest = digest_file(path)
                entry = self.record["segments"].get(str(index))
                if entry is not None and entry != {"size": size, "sha256": digest}:
                    continue
            except (OSError, TruncatedSegmentError):
                continue
            self.record["segments"][str(index)] = {"size": size, "sha256": digest}
            result[index] = size
        self.flush()
        return result

    def commit(self, index: int, received: Path) -> int:
        """검증된 파일로 교체하고 완료 기록을 갱신한다."""
        size = received.stat().st_size
        digest = digest_file(received)
        os.replace(received, self.segment_path(index))
        with self._mutex:
            self.record["segments"][str(index)] = {"size": size, "sha256": digest}
            self._dirty += 1
            if self._dirty >= 64 or time.monotonic() - self._saved_at >= 1:
                self._flush_locked()
        return size

    def flush(self) -> None:
        """완료된 조각의 기록을 강제로 저장한다."""
        with self._mutex:
            if self.record:
                self._flush_locked()

    def _flush_locked(self) -> None:
        atomic_json(self.path, self.record)
        self._dirty = 0
        self._saved_at = time.monotonic()

    def close(self) -> None:
        """기록 저장 실패 시에도 잠금을 반드시 해제한다."""
        try:
            self.flush()
        finally:
            self.lease.release()
