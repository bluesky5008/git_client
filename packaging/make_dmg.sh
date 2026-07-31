#!/bin/sh
# macOS 배포본 만들기 (design.md §9, 서명 절차는 doc/release.md §4).
#
# 서명·공증은 **환경변수가 있을 때만** 한다 — 인증서는 이 저장소가 가질
# 수 없는 자원이라, 없는 환경에서도 같은 스크립트가 끝까지 돌아야 한다.
# "키가 없으면 아무것도 못 만든다"가 아니라 "키가 없으면 서명만 빠진다".
#
#   GITCLIENT_SIGN_IDENTITY   "Developer ID Application: <이름> (<팀ID>)"
#                             — 빌드 단계(pyinstaller)에도 같은 값을 줘야
#                             한다: 공증은 dmg 속 바이너리의 서명을 본다.
#                             ad-hoc 시험은 "-" (공증은 불가).
#   GITCLIENT_NOTARY_PROFILE  `xcrun notarytool store-credentials`로 만든
#                             키체인 프로필 이름. 있으면 공증+스테이플까지.
#
# 서명하지 않은 dmg도 **동작한다** — 사용자가 처음 열 때 Gatekeeper 경고를
# 한 번 넘겨야 할 뿐이다 (우클릭 → 열기).
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

if [ -n "${GITCLIENT_SIGN_IDENTITY:-}" ]; then
    codesign --force --timestamp --sign "$GITCLIENT_SIGN_IDENTITY" "$out"
    echo "dmg에 서명했습니다: $GITCLIENT_SIGN_IDENTITY"
else
    echo "GITCLIENT_SIGN_IDENTITY가 없어 서명 없이 끝냅니다 (doc/release.md §4)."
fi

if [ -n "${GITCLIENT_NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "$out" \
        --keychain-profile "$GITCLIENT_NOTARY_PROFILE" --wait
    xcrun stapler staple "$out"
    echo "공증·스테이플까지 끝났습니다."
fi
