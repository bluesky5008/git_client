#!/bin/sh
# macOS 배포본 만들기 (design.md §9).
#
# **서명·공증은 여기서 하지 않는다.** 서명 키(Developer ID)는 이 저장소가
# 가질 수 없는 자원이라, 키를 가진 사람이 아래 두 줄을 이어서 돌리면 된다:
#
#   codesign --deep --force --options runtime --timestamp \
#            --sign "Developer ID Application: <이름> (<팀ID>)" dist/gitclient
#   xcrun notarytool submit dist/gitclient.dmg --keychain-profile <프로필> --wait
#   xcrun stapler staple dist/gitclient.dmg
#
# 서명하지 않은 dmg도 **동작한다** — 사용자가 처음 열 때 Gatekeeper 경고를
# 한 번 넘겨야 할 뿐이다. 그래서 "키가 없으면 아무것도 못 만든다"가 아니라
# "키가 없으면 서명만 빠진다"로 갈라 둔다.
#
# 사용법:  sh packaging/make_dmg.sh [출력경로]
set -eu

if [ "$(uname)" != "Darwin" ]; then
    echo "macOS에서만 만들 수 있습니다 (hdiutil이 필요합니다)." >&2
    exit 1
fi

root="$(cd "$(dirname "$0")/.." && pwd)"
app="$root/dist/gitclient"
out="${1:-$root/dist/gitclient.dmg}"

if [ ! -x "$app/gitclient" ]; then
    echo "먼저 빌드하세요: pyinstaller packaging/gitclient.spec" >&2
    exit 1
fi

# 스테이징 디렉터리에 앱과 Applications 심볼릭 링크를 둔다 — 사용자가
# 드래그 한 번으로 설치하는 관례다.
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
cp -R "$app" "$stage/gitclient"
ln -s /Applications "$stage/Applications"
cp "$root/LICENSE" "$stage/LICENSE.txt"
cp "$root/THIRD-PARTY-NOTICES.md" "$stage/THIRD-PARTY-NOTICES.md"

rm -f "$out"
hdiutil create -volname "Git Client" -srcfolder "$stage" -ov -format UDZO "$out"
echo "만들었습니다: $out"
echo "서명은 위 주석의 codesign/notarytool 두 줄로 이어서 하세요 (키 필요)."
