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

from PySide6 import __file__ as pyside_file  # noqa: F401 - 존재 확인

a = Analysis(
    ["../src/gitclient/__main__.py"],
    pathex=["../src"],
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="gitclient",
)
