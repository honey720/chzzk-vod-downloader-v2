"""화면의 VOD 선택을 core 복구 저장소에 연결한다."""

import json
from pathlib import Path

from core.downloaders.resume import ResumeStore
from core.models.content import Content, ContentType


def resume_store(item):
    """카드의 현재 화질과 저장 경로에 해당하는 저장소를 반환한다."""
    return ResumeStore(
        Content(
            ContentType(item.content_type),
            item.vod_url,
            output_path=item.output_path or str(Path(item.download_path) / "video.mp4"),
            resolution=item.resolution,
            stream=item.stream,
        )
    )


def restore_output_path(item):
    """미완료 복구 기록의 파일명만 가져오며 주소나 절대 경로는 신뢰하지 않는다."""
    if item.content_type != "m3u8" or item.selections:
        return
    store = resume_store(item)
    try:
        record = json.loads(store.path.read_text(encoding="utf-8"))
        name = record.get("output")
        if not record.get("complete") and isinstance(name, str) and Path(name).name == name:
            item.output_path = str(store.directory.parent.parent / name)
            item.resume_available = True
            item.merge_only = record.get("phase") == "merging"
    except (OSError, ValueError, AttributeError) as exc:
        if item.resume_available:
            from core.downloaders.resume import ResumeMismatchError

            raise ResumeMismatchError("보존된 데이터를 확인할 수 없습니다") from exc


def discard_resume(item):
    """사용자가 명시적으로 요청한 작업의 복구 데이터만 잠금 하에 삭제한다."""
    store = resume_store(item)
    store.lease.acquire()
    try:
        for path in store.directory.iterdir():
            if path.name != ".lock" and path.is_file():
                path.unlink()
    finally:
        store.lease.release()
    item.resume_available = False
    item.merge_only = False
    item.output_path = ""
    item.download_progress = 0


def archive_resume(item):
    """기존 조각을 보관 하위 폴더로 옮겨 별개의 새 다운로드를 허용한다."""
    from uuid import uuid4

    store = resume_store(item)
    store.lease.acquire()
    try:
        archive = store.directory / ("saved-" + uuid4().hex)
        archive.mkdir()
        paths = [p for p in store.directory.iterdir() if p.is_file() and p.name != ".lock"]
        # 기록은 마지막에 옮긴다. 중간 종료에도 남은 기록이 다른 데이터와 섞이지 않는다.
        for path in sorted(paths, key=lambda p: p.name == "resume.json"):
            path.rename(archive / path.name)
    finally:
        store.lease.release()
    item.resume_available = item.merge_only = False
    item.output_path = ""
