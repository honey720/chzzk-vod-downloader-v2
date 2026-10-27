"""config.json 내구성 게이트 (#255) — 원자적 저장 · 깨진 파일 비켜 두기 · 기본값 복사본.

세 결함이 합쳐져 저장된 인증 정보가 사라졌다: ①`save_config`가 대상에 직접 써서 쓰기 도중
종료되면 파일이 깨지고 ②`load_config`가 깨진 파일에 기본값을 돌려주면 시작 시
`update_config`가 그것을 저장해 원본을 덮어썼다 ③그 기본값이 모듈 전역 객체 자체라
호출부의 수정이 기본값 표를 오염시켰다(#257부터 기본값 표는 스키마 표에서 `default_config()`로 도출된다). 세 게이트는 서로 다른 결함을 하나씩 잰다 —
①만 되돌리면 원자성 게이트만, ②만 되돌리면 보존 게이트만, ③만 되돌리면 오염 게이트만
실패해야 한다(고장 주입으로 확인).

config 경로는 conftest의 autouse 픽스처가 테스트마다 임시 폴더로 격리한다. 위젯을 만들지
않는다(순수 파일 게이트). 실제 앱 시작 경로(`update_config`)로 덮어쓰기 여부를 잰다.
"""

import json
import os

import pytest

import config.config as config_module

#: 테스트가 쓰는 자리표시 인증 값 — 실제 쿠키가 아니다.
SAVED = {
    "version": 2,
    "cookies": {"NID_AUT": "aut-placeholder", "NID_SES": "ses-placeholder"},
    "afterDownload": "none",
    "language": "ko_KR",
    "downloadPath": "",
    "window": {},
}


def _write_raw(text: str) -> None:
    """config.json에 문자열을 그대로 쓴다(깨진 상태를 만들 때)."""
    os.makedirs(config_module.CONFIG_DIR, exist_ok=True)
    with open(config_module.CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def _read_raw() -> str:
    """config.json의 현재 바이트를 문자열로 읽는다."""
    with open(config_module.CONFIG_FILE, encoding="utf-8") as f:
        return f.read()


def _entries() -> list[str]:
    """config 디렉토리의 파일 이름 목록(logs 제외) — 임시 파일이 남았는지 본다."""
    return sorted(name for name in os.listdir(config_module.CONFIG_DIR) if name != "logs")


class TestBrokenFileIsKeptNotOverwritten:
    """깨진 config.json으로 시작하면 원본은 비켜 놓이고, 거기서 인증 정보를 여전히 읽을 수 있다."""

    @pytest.fixture
    def broken(self):
        """저장된 인증 정보가 든 파일을 중간에서 잘라 깨뜨린다."""
        good = json.dumps(SAVED, indent=4)
        cut = good[: good.index('"afterDownload"')]  # 인증 값은 온전히 남고 뒤가 잘린 상태
        _write_raw(cut)
        return cut

    def test_startup_path_does_not_overwrite_the_broken_original(self, broken):
        """실제 시작 경로(`update_config`)를 탄 뒤 — 원본은 `.broken`으로 남고 인증 값이 그 안에 있다."""
        config_module.update_config()
        broken_path = config_module.broken_config_path()
        assert os.path.exists(broken_path), "깨진 원본이 비켜 놓이지 않았다(지워졌거나 덮어써졌다)"
        with open(broken_path, encoding="utf-8") as f:
            kept = f.read()
        assert kept == broken, "비켜 둔 파일이 원본 바이트 그대로가 아니다"
        assert "aut-placeholder" in kept and "ses-placeholder" in kept, (
            "비켜 둔 파일에서 인증 값을 읽을 수 없다"
        )

    def test_app_starts_with_defaults_and_a_fresh_file(self, broken):
        """앱은 기본값으로 뜬다 — 새 config.json은 기본값이고, 인증 값은 비어 있다(원본은 .broken에)."""
        cfg = config_module.update_config()
        assert cfg["cookies"] == config_module.default_config()["cookies"]
        assert json.loads(_read_raw())["cookies"] == {"NID_AUT": "", "NID_SES": ""}

    def test_the_broken_path_is_logged(self, broken, caplog):
        """비켜 둔 파일의 경로가 ERROR 로그에 남는다(유저 알림은 범위 밖 — 로그까지)."""
        with caplog.at_level("ERROR", logger="config.config"):
            config_module.load_config()
        assert any(config_module.broken_config_path() in r.getMessage() for r in caplog.records), (
            "비켜 둔 파일 경로가 로그에 없다"
        )

    def test_only_one_broken_file_is_kept(self, broken):
        """두 번째 손상은 앞의 .broken을 덮어쓴다 — 최신 손상본 하나만 남는다."""
        config_module.load_config()
        _write_raw("{second corruption")
        config_module.load_config()
        with open(config_module.broken_config_path(), encoding="utf-8") as f:
            assert f.read() == "{second corruption"
        assert [n for n in _entries() if n.endswith(config_module.BROKEN_SUFFIX)] == [
            os.path.basename(config_module.broken_config_path())
        ]

    def test_a_valid_file_is_read_as_is(self):
        """정상 파일은 그대로 읽히고 .broken은 생기지 않는다(기존 config.json 호환)."""
        _write_raw(json.dumps(SAVED))
        assert config_module.load_config() == SAVED
        assert not os.path.exists(config_module.broken_config_path())


class TestSaveIsAtomic:
    """임시 파일에 쓴 뒤 갈아끼운다 — 갈아끼우기 전에는 대상이 옛 내용 그대로다."""

    def test_target_is_untouched_until_the_replace(self, monkeypatch):
        """갈아끼우기 직전 시점을 잡아 대상 파일을 읽는다 — 옛 내용 전체여야 하고 중간 상태가 없어야 한다."""
        config_module.save_config(SAVED)
        before = _read_raw()
        seen = {}
        real_replace = os.replace

        def spy(src, dst):
            """갈아끼우기 직전에 대상·임시 파일을 들여다보는 os.replace 대역."""
            if dst == config_module.CONFIG_FILE:
                seen["target_during_write"] = _read_raw()
                with open(src, encoding="utf-8") as f:
                    seen["tmp_is_complete_json"] = json.load(f)
                assert os.path.dirname(os.path.abspath(src)) == os.path.dirname(
                    os.path.abspath(dst)
                ), "임시 파일이 대상과 다른 디렉토리에 있다 — 다른 볼륨이면 원자성이 깨진다"
            return real_replace(src, dst)

        monkeypatch.setattr(config_module.os, "replace", spy)
        new = {**SAVED, "language": "en_US"}
        config_module.save_config(new)
        assert "target_during_write" in seen, "os.replace로 갈아끼우지 않았다(대상에 직접 썼다)"
        assert seen["target_during_write"] == before, (
            "갈아끼우기 전에 대상 파일이 이미 바뀌었다(중간 상태 노출)"
        )
        assert seen["tmp_is_complete_json"] == new
        assert json.loads(_read_raw()) == new
        assert _entries() == ["config.json"], f"임시 파일이 남았다: {_entries()}"

    def test_a_failed_write_leaves_the_old_file_intact(self, monkeypatch):
        """쓰기 도중 예외가 나면 대상은 옛 내용 그대로이고 임시 파일도 남지 않는다."""
        config_module.save_config(SAVED)
        before = _read_raw()

        def bad_dump(obj, fp, **kw):
            """절반만 쓰고 실패하는 json.dump 대역(디스크 가득 참 흉내)."""
            fp.write('{"half": ')
            raise OSError("disk full")

        monkeypatch.setattr(config_module.json, "dump", bad_dump)
        with pytest.raises(OSError):
            config_module.save_config({**SAVED, "language": "en_US"})
        assert _read_raw() == before
        assert _entries() == ["config.json"]

    def test_replace_failure_keeps_the_old_file_and_raises(self, monkeypatch):
        """갈아끼우기가 끝내 실패하면(Windows 잠금 등) 예외가 올라오고 대상은 옛 내용, 임시 파일은 없다."""
        config_module.save_config(SAVED)
        before = _read_raw()
        monkeypatch.setattr(config_module, "_REPLACE_INTERVAL", 0)

        def locked(src, dst):
            """항상 잠겨 있는 os.replace 대역."""
            raise PermissionError("locked")

        monkeypatch.setattr(config_module.os, "replace", locked)
        with pytest.raises(PermissionError):
            config_module.save_config({**SAVED, "language": "en_US"})
        assert _read_raw() == before
        assert _entries() == ["config.json"]

    def test_replace_retries_through_a_transient_lock(self, monkeypatch):
        """처음 몇 번 잠겨 있다 풀리면 저장이 성공한다."""
        config_module.save_config(SAVED)
        monkeypatch.setattr(config_module, "_REPLACE_INTERVAL", 0)
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            """두 번 잠겼다가 세 번째에 풀리는 os.replace 대역."""
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("locked")
            return real_replace(src, dst)

        monkeypatch.setattr(config_module.os, "replace", flaky)
        config_module.save_config({**SAVED, "language": "en_US"})
        assert json.loads(_read_raw())["language"] == "en_US"
        assert calls["n"] == 3

    def test_file_is_written_as_utf8(self):
        """인코딩 명시 — 한글 경로를 저장해도 OS 기본 로케일과 무관하게 utf-8로 읽힌다."""
        config_module.save_config({**SAVED, "downloadPath": "D:/영상/보관"})
        with open(config_module.CONFIG_FILE, encoding="utf-8") as f:
            assert json.load(f)["downloadPath"] == "D:/영상/보관"


class TestDirectoryFsyncWiring:
    """디렉토리 fsync **배선** 게이트 — 내구성 자체를 재지 않는다.

    ⚠️ 정전은 재현할 수 없다. 이 클래스가 재는 것은 "rename 뒤에 디렉토리를 열어 `fsync`를
    부르는가"와 "그 fsync가 실패해도 저장이 성공하는가"뿐이다. 디스크에 실제로 남는지는
    파일시스템의 몫이고 여기서 보장되지 않는다.

    러너별 동작: POSIX 갈래는 모듈 상수 `_DIRECTORY_FSYNC_SUPPORTED`를 True로 놓고
    `os.open`/`os.fsync`/`os.close`를 가짜로 바꿔 **어느 러너에서든**(Windows 포함) 배선을
    잰다. Windows 갈래(상수 False)는 디렉토리를 열지 않는 것을 잰다 — 스킵이 아니다.
    """

    @pytest.fixture
    def dir_fs(self, monkeypatch):
        """디렉토리 열기·fsync·닫기와 `os.replace`를 가로채는 스파이 — **순서**를 `events`에 기록한다.

        디렉토리 fd는 가짜 번호, 파일은 진짜 그대로. `events`는 ("replace", 대상) /
        ("dir_open", 경로) / ("dir_fsync",) / ("dir_close",)가 일어난 순서다 — 순서 단언이 없으면
        `_fsync_directory`가 rename **앞**에서 불리는 회귀(엔트리 변경이 지속되지 않는다)도
        호출 여부 단언을 통과한다.
        """
        real_open, real_fsync, real_close, real_replace = os.open, os.fsync, os.close, os.replace
        record = {"opened": [], "fsynced": [], "closed": [], "fail": False, "events": []}
        FAKE_FD = 987654321

        def spy_replace(src, dst):
            """진짜 os.replace를 부르고 순서를 기록한다."""
            real_replace(src, dst)
            record["events"].append(("replace", os.path.abspath(dst)))

        def spy_open(path, flags, *a, **k):
            """디렉토리면 가짜 fd를 돌려주고 기록, 파일이면 진짜 open."""
            if os.path.isdir(path):
                record["opened"].append(os.path.abspath(path))
                record["events"].append(("dir_open", os.path.abspath(path)))
                return FAKE_FD
            return real_open(path, flags, *a, **k)

        def spy_fsync(fd):
            """디렉토리 fd면 기록(옵션으로 실패), 파일 fd면 진짜 fsync."""
            if fd == FAKE_FD:
                record["fsynced"].append(fd)
                record["events"].append(("dir_fsync",))
                if record["fail"]:
                    raise OSError("fsync not supported on this filesystem")
                return None
            return real_fsync(fd)

        def spy_close(fd):
            """디렉토리 가짜 fd는 기록만, 파일 fd는 진짜 close."""
            if fd == FAKE_FD:
                record["closed"].append(fd)
                record["events"].append(("dir_close",))
                return None
            return real_close(fd)

        monkeypatch.setattr(config_module, "_DIRECTORY_FSYNC_SUPPORTED", True)
        monkeypatch.setattr(config_module.os, "open", spy_open)
        monkeypatch.setattr(config_module.os, "fsync", spy_fsync)
        monkeypatch.setattr(config_module.os, "close", spy_close)
        monkeypatch.setattr(config_module.os, "replace", spy_replace)
        return record

    @staticmethod
    def _assert_replace_then_fsync(events, target: str) -> None:
        """`target`으로의 replace가 먼저, 그 뒤에 디렉토리 fsync·close가 온다 — 순서 단언."""
        kinds = [e[0] for e in events]
        key = ("replace", os.path.abspath(target))
        assert key in events, f"{target}로의 os.replace가 없다: {events}"
        assert "dir_fsync" in kinds, f"디렉토리 fsync가 없다: {events}"
        i_replace, i_fsync = events.index(key), kinds.index("dir_fsync")
        assert i_replace < i_fsync, (
            f"디렉토리 fsync가 os.replace보다 먼저다(엔트리 변경이 지속되지 않는다): {events}"
        )
        assert "dir_close" in kinds and kinds.index("dir_close") > i_fsync, (
            f"fsync 뒤에 close가 없다: {events}"
        )

    def test_save_fsyncs_the_directory_after_the_replace(self, dir_fs):
        """저장: 임시 파일 → os.replace → 디렉토리 fsync → close **이 순서**(배선)."""
        config_module.save_config(SAVED)
        assert dir_fs["opened"] == [os.path.abspath(config_module.CONFIG_DIR)], (
            "디렉토리를 열지 않았다"
        )
        assert dir_fs["fsynced"] and dir_fs["closed"], "디렉토리 fd를 fsync·close하지 않았다"
        self._assert_replace_then_fsync(dir_fs["events"], config_module.CONFIG_FILE)

    def test_moving_a_broken_file_aside_fsyncs_the_directory(self, dir_fs):
        """깨진 파일 이동: `.broken`으로 os.replace → 디렉토리 fsync → close **이 순서**(배선)."""
        _write_raw("{broken")
        config_module.load_config()
        assert os.path.abspath(config_module.CONFIG_DIR) in dir_fs["opened"]
        assert dir_fs["fsynced"], "깨진 파일을 옮긴 뒤 디렉토리 fsync가 없다"
        self._assert_replace_then_fsync(dir_fs["events"], config_module.broken_config_path())

    def test_directory_fsync_failure_does_not_fail_the_save(self, dir_fs, caplog):
        """★ 디렉토리 fsync가 OSError로 실패해도 save_config는 성공한다 — 파일은 이미 갈아끼워졌다."""
        dir_fs["fail"] = True
        with caplog.at_level("WARNING", logger="config.config"):
            config_module.save_config({**SAVED, "language": "en_US"})  # 예외가 올라오면 실패
        assert json.loads(_read_raw())["language"] == "en_US", "저장 자체는 끝나 있어야 한다"
        assert dir_fs["fsynced"], "전제: fsync가 시도됐어야 한다"
        assert any("Directory fsync failed" in r.getMessage() for r in caplog.records), (
            "실패가 로그에 남지 않았다"
        )
        assert dir_fs["closed"], "실패해도 디렉토리 fd는 닫는다"

    def test_directory_that_cannot_be_opened_is_logged_and_skipped(self, monkeypatch, caplog):
        """디렉토리를 열 수 없으면(권한·특수 파일시스템) 경고만 남기고 저장은 성공한다."""
        monkeypatch.setattr(config_module, "_DIRECTORY_FSYNC_SUPPORTED", True)
        real_open = os.open

        def refuse_dirs(path, flags, *a, **k):
            """디렉토리 열기만 거부하는 os.open 대역."""
            if os.path.isdir(path):
                raise PermissionError("cannot open directory")
            return real_open(path, flags, *a, **k)

        monkeypatch.setattr(config_module.os, "open", refuse_dirs)
        with caplog.at_level("WARNING", logger="config.config"):
            config_module.save_config(SAVED)
        assert json.loads(_read_raw()) == SAVED
        assert any("Directory fsync skipped" in r.getMessage() for r in caplog.records)

    def test_windows_branch_does_not_open_the_directory(self, monkeypatch):
        """Windows 갈래: 디렉토리를 열지 않는다(`os.open`이 디렉토리에 실패하는 플랫폼) — 분기 확인."""
        monkeypatch.setattr(config_module, "_DIRECTORY_FSYNC_SUPPORTED", False)
        real_open = os.open
        opened_dirs = []

        def spy_open(path, flags, *a, **k):
            """디렉토리를 열려는 시도만 기록하는 os.open 대역."""
            if os.path.isdir(path):
                opened_dirs.append(path)
            return real_open(path, flags, *a, **k)

        monkeypatch.setattr(config_module.os, "open", spy_open)
        config_module.save_config(SAVED)
        _write_raw("{broken")
        config_module.load_config()
        assert opened_dirs == [], f"Windows 갈래에서 디렉토리를 열었다: {opened_dirs}"

    def test_the_branch_constant_follows_the_platform(self):
        """상수는 플랫폼에서 유도된다 — Windows(nt)에서만 False."""
        assert config_module._DIRECTORY_FSYNC_SUPPORTED == (os.name != "nt")


class TestDefaultsAreNeverShared:
    """`load_config` 결과를 고쳐도 기본값 표(`default_config`, #257부터 스키마 표에서 도출)는 바뀌지 않는다 — 중첩 dict(cookies)까지."""

    def test_mutating_a_default_result_does_not_pollute_the_module_table(self):
        """기본값 경로의 반환 dict를 최상위·중첩(cookies·window)까지 고쳐도 모듈 표는 그대로다."""
        _write_raw("{broken")  # 기본값 경로
        pristine = json.loads(json.dumps(config_module.default_config()))
        cfg = config_module.load_config()
        assert cfg is not config_module.default_config()
        cfg["cookies"]["NID_AUT"] = "polluted"  # 중첩 dict — 얕은 복사면 여기서 새어 들어간다
        cfg["window"]["x"] = 1
        cfg["language"] = "xx"
        cfg["extra"] = True
        assert config_module.default_config() == pristine, (
            f"기본값 표가 오염됐다: {config_module.default_config()}"
        )

    def test_two_default_results_are_independent(self):
        """기본값 경로를 두 번 타도 두 반환값이 중첩 dict를 공유하지 않는다."""
        _write_raw("{broken")
        first = config_module.load_config()
        _write_raw("{broken")
        second = config_module.load_config()
        first["cookies"]["NID_SES"] = "a"
        assert second["cookies"]["NID_SES"] == "", "두 호출이 같은 중첩 dict를 공유한다"

    def test_a_fresh_file_is_created_from_the_table_not_a_polluted_copy(self):
        """파일이 없을 때 만들어지는 기본 파일은 모듈 표 그대로다."""
        cfg = config_module.load_config()  # 파일 생성
        cfg["cookies"]["NID_AUT"] = "polluted"
        os.remove(config_module.CONFIG_FILE)
        assert config_module.load_config()["cookies"]["NID_AUT"] == ""
