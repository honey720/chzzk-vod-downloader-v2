"""테스트가 Windows에서 띄우는 자식 프로세스에 창 숨김 플래그를 기본으로 주는 도구 (#399).

콘솔 없이 띄운 pytest(IDE의 테스트 실행기 등)에서 콘솔 프로그램(ffmpeg · python · tasklist 등)을
띄우면 자식마다 새 콘솔 창이 뜬다. 제품 코드(``core/utils/ffmpeg.py``)는 ffmpeg마다
``CREATE_NO_WINDOW``를 직접 주지만, 테스트가 입력 영상을 만들거나 자식 파이썬을 띄우는 호출에는
없다. ``conftest.py``가 세션을 시작할 때 ``install``을 부른다.

``creationflags``를 직접 준 호출은 그 값을 그대로 쓴다 — 0을 주어도 바꾸지 않는다. Qt가 띄우는
프로세스(``QProcess``)는 해당하지 않는다.
"""

CREATE_NO_WINDOW = 0x08000000  # Windows 프로세스 생성 플래그 — 창 없는 콘솔로 띄운다
# subprocess.Popen의 위치 인자 가운데 creationflags의 자리 — args, bufsize, executable, stdin,
# stdout, stderr, preexec_fn, close_fds, shell, cwd, env, universal_newlines, startupinfo 다음
_CREATIONFLAGS_POSITION = 13


def with_hidden_window(args: tuple, kwargs: dict) -> dict:
    """``Popen``에 넘길 키워드 인자에, creationflags가 없으면 ``CREATE_NO_WINDOW``를 더해 돌려준다.

    creationflags를 키워드나 위치 인자로 준 호출은 받은 그대로 돌려준다.
    """
    if "creationflags" in kwargs or len(args) > _CREATIONFLAGS_POSITION:
        return kwargs
    return {**kwargs, "creationflags": CREATE_NO_WINDOW}


def install(module, platform: str) -> bool:
    """``module.Popen``을 창 숨김 플래그를 기본으로 주는 하위 클래스로 바꾼다 — Windows일 때만.

    ``subprocess.run`` · ``check_output`` 등은 모두 ``subprocess.Popen``을 거치므로 ``subprocess``
    모듈 하나를 바꾸면 된다. 두 번 불러도 한 번만 감싼다.

    Args:
        module: ``Popen``을 가진 모듈(``subprocess``) — 테스트는 대역을 넘긴다
        platform: ``sys.platform`` 값

    Returns:
        바꿨으면 True
    """
    if platform != "win32" or getattr(module.Popen, "_hides_console_window", False):
        return False
    base = module.Popen

    class _HiddenWindowPopen(base):
        _hides_console_window = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **with_hidden_window(args, kwargs))

    module.Popen = _HiddenWindowPopen
    return True
