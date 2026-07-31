# PyInstaller 스펙 (design.md §9).
#
# **--onedir이 요구사항이다** — PySide6/Qt는 LGPLv3이고, 준수의 핵심은
# Qt 공유 라이브러리가 사용자가 교체할 수 있는 별도 파일로 남는 것이다
# (THIRD-PARTY-NOTICES.md). onefile로 묶으면 그 조건이 흐려진다.
#
# git 자체는 동봉하지 않는다 — 시스템 설치본을 쓴다 (README 요구사항,
# GPLv2 의무가 앱에 미치지 않는 경계이기도 하다).
#
# 빌드:  pyinstaller packaging/gitclient.spec
# 검증:  dist/gitclient/gitclient --version

import os

from PySide6 import __file__ as pyside_file  # noqa: F401 - 존재 확인
from PyInstaller.utils.hooks import copy_metadata

# 서명 신원 — 환경변수로만 받는다 (doc/release.md §4). 인증서는 이
# 저장소가 가질 수 없는 자원이라 스펙에 이름을 박아둘 수 없고, 비워두면
# PyInstaller가 arm64 필수인 ad-hoc 서명을 스스로 한다. 값을 주면
# 수집된 모든 Mach-O에 그 신원으로 서명한다 — dmg를 만들기 전에
# 바이너리가 먼저 서명돼 있어야 공증이 통과하기 때문에 여기가 그 자리다.
_sign_identity = os.environ.get("GITCLIENT_SIGN_IDENTITY") or None

a = Analysis(
    ["../src/gitclient/__main__.py"],
    pathex=["../src"],
    # dist-info 메타데이터 — --version이 importlib.metadata로 버전을 읽는데,
    # 이것이 없으면 배포본이 자기 버전 대신 "(개발 트리)"를 말한다
    # (배포 패키지 준비 중 실측). 버전의 출처는 pyproject.toml 한 곳이다.
    datas=copy_metadata("gitclient"),
    hiddenimports=[
        # pygit2는 cffi 기반이다 — 백엔드 모듈을 정적 분석이 놓친다
        # (로컬 빌드 실측: 빠뜨리면 기동 즉시 ModuleNotFoundError).
        "_cffi_backend",
        # 지연 import(함수 안 from-import)를 정적 분석이 놓친다.
        "gitclient.ui.reflog_dialog",
        "gitclient.ui.remotes_dialog",
        "gitclient.ui.settings_dialog",
        "gitclient.ui.conflict_lines_dialog",
        "gitclient.ui.command_log_panel",
    ],
    excludes=[
        # Qt의 무거운 부속 중 앱이 쓰지 않는 것 — 배포 크기를 줄인다.
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.Qt3DCore",
    ],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="gitclient",
    console=False,
    codesign_identity=_sign_identity,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="gitclient",
)
