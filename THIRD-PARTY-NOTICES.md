# 제3자 구성요소 고지

이 애플리케이션(MIT 라이선스)은 아래 구성요소를 사용한다. 각 구성요소는
자신의 라이선스를 따른다.

| 구성요소 | 라이선스 | 사용 방식 | 비고 |
|---|---|---|---|
| PySide6 / Shiboken6 (Qt for Python) | **LGPLv3** | 파이썬 모듈 **동적 링크** | LGPL 조건 준수의 핵심: 정적으로 묶지 않는다. 배포판(PyInstaller onedir)에서도 Qt 라이브러리는 별도 공유 라이브러리 파일로 존재해 사용자가 교체할 수 있다 (design.md §9·§11) |
| Qt 6 | LGPLv3 | PySide6를 통해 동적 링크 | 소스: <https://download.qt.io> |
| pygit2 | GPLv2 **with linking exception** | 파이썬 모듈 | linking exception이 비GPL 앱에서의 사용을 허용한다 |
| libgit2 | GPLv2 **with linking exception** | pygit2 휠에 동봉된 공유 라이브러리 | 상동 |
| git | GPLv2 | **별도 프로세스로 실행** (링크·동봉하지 않음) | 시스템 설치본을 사용한다 — README 요구사항. 배포판에 포함하지 않으므로 GPL 의무가 앱에 미치지 않는다 |
| CPython 표준 라이브러리 | PSF License | 런타임 | |

## LGPL(PySide6/Qt) 준수 방법

1. 이 고지 파일로 사용 사실과 라이선스를 밝힌다.
2. 동적 링크를 유지한다 — PyInstaller는 `--onedir`로 빌드해 Qt 공유
   라이브러리가 교체 가능한 파일로 남게 한다 (`packaging/gitclient.spec`).
3. Qt 소스 코드는 위 링크에서 구할 수 있다. 우리는 Qt를 수정하지 않았다.
