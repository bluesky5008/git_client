"""git에 자격증명을 공급하는 askpass shim.

**왜 shim 파일인가.** git은 `GIT_ASKPASS`를 **실행 파일 경로 하나**로만 받는다.
인자를 붙인 문자열을 주면 조용히 무시하고 호출조차 하지 않는다(실측 확인).
따라서 우리가 모아둔 값을 돌려주려면 작은 스크립트를 그때그때 써야 한다.

**왜 값을 파일에 쓰지 않는가.** 비밀번호가 디스크에 남으면 우리가 지우기
전에 프로세스가 죽었을 때 그대로 남는다. 값은 자식 프로세스의 **환경변수**로
넘기고 shim은 그것을 읽기만 한다 — 파일에는 비밀번호가 들어가지 않는다.
(같은 사용자의 다른 프로세스가 환경을 들여다볼 수 있다는 한계는 남지만,
그건 자격증명 자체와 같은 신뢰 경계다.)

git은 사용자 이름과 비밀번호를 **따로** 묻는다:

    Username for 'https://example.com':
    Password for 'https://alice@example.com':

프롬프트 문구로 어느 쪽인지 가른다.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

USERNAME_ENV = "GITCLIENT_ASKPASS_USERNAME"
PASSWORD_ENV = "GITCLIENT_ASKPASS_PASSWORD"


@dataclass(frozen=True, slots=True)
class Credentials:
    """사용자가 다이얼로그에 입력한 값.

    **저장하지 않는다.** 이 객체는 한 번의 작업 동안만 살아 있고, 저장은
    git의 credential helper에 위임한다(ADR-3). `remember`는 그 위임을
    할지 말지의 표시일 뿐 우리가 어딘가에 쓰겠다는 뜻이 아니다.
    """

    username: str
    password: str
    remember: bool = True

    def __repr__(self) -> str:  # pragma: no cover - 방어적
        # 비밀번호가 로그·트레이스백에 실려 나가지 않게 한다.
        return f"Credentials(username={self.username!r}, password=***)"


# **`%VAR%`가 아니라 `!VAR!`인 이유** (확정된 결함의 재발 방지):
# cmd.exe는 퍼센트 확장을 특수문자 파싱 **앞** 단계에서 하고, 확장된 결과를
# 다시 파싱한다. 그래서 비밀번호에 `& | < > ^ ( )`가 들어 있으면 그 문자들이
# 셸 문법으로 재해석된다. 실측: 비밀번호 `a>b-secret`이 사용자의 워킹 트리에
# `b-secret`이라는 파일을 만들고 그 안에 `a`를 남겼다 — 즉 **비밀번호가
# 파일로 흘렀고**, `& echo ...` 형태로는 임의 명령까지 실행됐다.
#
# 지연 확장(`!VAR!`)은 특수문자 파싱 **뒤** 단계에서 치환되고 결과를 다시
# 파싱하지 않으므로 값이 문자 그대로 전달된다.
#
# `echo(`는 `echo `와 달리 값이 비어도 "ECHO is on."을 출력하지 않는다.
_WINDOWS_SHIM = """@echo off
setlocal EnableDelayedExpansion
echo %* | findstr /C:"Username" >nul
if %errorlevel%==0 (
  echo(!{username_env}!
) else (
  echo(!{password_env}!
)
"""

_POSIX_SHIM = """#!/bin/sh
case "$*" in
  *Username*) printf '%s\\n' "${username_env}" ;;
  *)          printf '%s\\n' "${password_env}" ;;
esac
"""


def write_shim(directory: Path) -> Path:
    """askpass shim을 만들고 경로를 돌려준다.

    내용에는 비밀번호가 들어가지 않는다 — 환경변수 이름만 들어간다.
    """
    if os.name == "nt":
        path = directory / "gitclient-askpass.bat"
        path.write_text(
            _WINDOWS_SHIM.format(
                username_env=USERNAME_ENV, password_env=PASSWORD_ENV
            ),
            encoding="utf-8",
        )
    else:
        path = directory / "gitclient-askpass.sh"
        path.write_text(
            _POSIX_SHIM.format(
                username_env=USERNAME_ENV, password_env=PASSWORD_ENV
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def shim_environment(path: Path, credentials: Credentials) -> dict[str, str]:
    """shim을 쓰기 위한 환경변수.

    `GIT_ASKPASS`를 우리 shim으로 덮으므로, 비대화형 강제(§4.6.2)에서
    빈 값으로 막아두었던 경로가 이 작업에 한해 열린다.
    """
    return {
        "GIT_ASKPASS": str(path),
        USERNAME_ENV: credentials.username,
        PASSWORD_ENV: credentials.password,
    }
