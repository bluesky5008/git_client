# 배포 — 환경과 폴더에 관한 결정 기록

버전 0.1.0 배포 패키지를 처음 준비하며 내린 결정과 그 이유다.
설계 원칙(design.md §9)이 "무엇을 만드는가"를 정했다면, 여기는
"어디서 만들고 어디에 두는가"를 정한다.

## 1. 배포 환경 — 어디서 만드는가

### 1.1 macOS는 로컬에서, Windows는 CI에서 만든다

| 산출물 | 만드는 곳 | 왜 |
|--------|----------|-----|
| `gitclient-<버전>-macos-arm64.dmg` | 로컬 (개발 머신, Apple Silicon) | `hdiutil`이 macOS에만 있고, 개발 머신이 macOS다 |
| `gitclient-<버전>-windows-x64-setup.exe` | GitHub Actions `windows-latest` | **개발 머신에서 Windows 바이너리를 만들 수 없다.** PyInstaller는 크로스 컴파일을 지원하지 않는다 — Windows 배포본은 Windows에서 빌드해야 한다 |

교차 빌드(Wine, 크로스 툴체인)는 고려에서 일찍 떨어졌다. PyInstaller가
공식적으로 지원하지 않는 경로이고, "빌드는 됐는데 실제 Windows에서
깨지는" 산출물을 걸러낼 방법이 로컬에 없다. CI의 Windows 러너는
빌드·스모크 테스트(`--version` 실행)·인스톨러 생성까지 실제 Windows에서
수행하므로, 그 산출물을 그대로 배포본으로 쓴다.

**CI 산출물을 배포본으로 쓸 때의 규칙**: 아티팩트를 받기 전에
CI 실행의 커밋(`headSha`)이 배포하려는 커밋과 같은지 확인한다.
이번 배포에서 실제로 이 확인이 일을 했다 — 첫 아티팩트는 버전
메타데이터 수정(§1.3) 이전 커밋의 것이라 버리고 다시 받았다.

### 1.2 아키텍처 — 지금은 arm64(macOS)와 x64(Windows)만

- macOS: GitHub의 `macos-latest` 러너도, 개발 머신도 Apple Silicon이다.
  Intel(x86_64)용 dmg는 **만들 곳이 없어서가 아니라 검증할 곳이 없어서**
  미룬다 — `macos-13`(Intel) 러너를 추가하면 만들 수 있지만, 실행해 볼
  Intel 맥이 없는 채로 내보내는 것은 §1.1에서 배제한 "빌드만 되는
  산출물"과 같다. 수요가 확인되면 러너를 추가한다.
- Windows: `windows-latest`가 x64다. ARM Windows는 x64 에뮬레이션으로
  동작하므로 별도 빌드를 두지 않는다.

### 1.3 배포 환경이 드러낸 결함 — 배포본이 자기 버전을 몰랐다

패키지 준비 중 dmg의 실행 파일이 `--version`에 `0.1.0`이 아니라
`(개발 트리)`를 답했다. 원인은 두 겹이다:

1. `--version`은 `importlib.metadata`로 버전을 읽는데(출처를
   pyproject.toml 한 곳에 두기 위해), PyInstaller는 dist-info
   메타데이터를 기본으로 담지 않는다 → 스펙에 `copy_metadata("gitclient")`
   를 추가했다.
2. `copy_metadata`는 빌드 환경에 패키지가 **설치돼** 있어야 동작한다.
   로컬 venv는 소스 경로 실행 구성이라 메타데이터가 없었다 →
   **배포 빌드 전 `uv pip install -e . --no-deps`가 필수 절차다**
   (CI는 `pip install -e .`를 이미 하므로 추가 조치가 없다).

교훈: 스모크 테스트가 "실행되는가"만 물으면 이런 결함이 통과한다.
CI 스모크가 `--version`의 **출력 내용**까지 확인하도록 강화했다 —
`(개발 트리)`가 나오면 붉어진다.

### 1.4 서명은 배포 절차에 없다 — 의도된 공백

코드서명(macOS Developer ID, Windows 인증서)은 이 저장소가 가질 수
없는 자원이라 배포 절차에서 분리했다. "키가 없으면 배포를 못 한다"가
아니라 "키가 없으면 서명만 빠진다"로 갈라 둔 것이다:

- 서명 없는 dmg: 첫 실행에서 Gatekeeper 경고를 한 번 넘겨야 한다
  (우클릭 → 열기).
- 서명 없는 인스톨러: SmartScreen 경고를 "추가 정보 → 실행"으로 넘긴다.

키를 가진 사람이 이어서 돌릴 명령은 `packaging/make_dmg.sh`와
`packaging/gitclient.iss`의 머리 주석에 있다.

## 2. 배포 폴더 — 어디에 두는가

### 2.1 구조

```
release/
└── 0.1.0/
    ├── gitclient-0.1.0-macos-arm64.dmg
    ├── gitclient-0.1.0-windows-x64-setup.exe
    └── SHA256SUMS.txt
```

- **버전별 하위 폴더** — 다음 버전을 준비할 때 이전 배포본이 덮이지
  않는다. "지금 사용자에게 준 파일이 정확히 무엇이었나"는 배포 후에
  가장 자주 받는 질문이다.
- **파일명에 버전·OS·아키텍처를 전부 넣는다** — 파일은 폴더를 떠나
  돌아다닌다(메일, 메신저, 다운로드 폴더). 파일명만으로 정체가 확인되지
  않으면 "이거 최신 맞아요?"에 답할 수 없다. `gitclient.dmg` 같은
  무명 파일명은 빌드 산출물(`dist/`)에서만 허용한다.
- **SHA256SUMS.txt** — 서명이 없는 지금(§1.4), 받은 파일이 온전한지
  확인할 유일한 수단이다. 검증: `shasum -a 256 -c SHA256SUMS.txt`
  (Windows는 `certutil -hashfile <파일> SHA256`으로 값 비교).

### 2.2 git이 추적하지 않는다

`release/`는 `.gitignore`에 있다. 이유는 두 가지다:

1. **바이너리는 git 히스토리에 넣는 순간 영구히 저장소를 무겁게 한다.**
   80MB짜리 배포본 몇 번이면 clone이 느려진다 — 느린 회선을 위한
   앱의 저장소가 느린 회선에서 못 받는 저장소가 되는 모순이다.
2. 공개 채널은 **GitHub Releases**가 맞다. 태그에 묶이고, 다운로드
   수가 보이고, clone 크기에 영향이 없다. `release/<버전>/`은 그
   업로드 직전의 준비대(staging)다:
   `gh release create v<버전> release/<버전>/*`

### 2.3 dist/와 release/의 구분

| 폴더 | 성격 | 파일명 | git |
|------|------|--------|-----|
| `dist/` | 빌드 작업대 — PyInstaller·hdiutil이 쓰고 지우는 곳 | 무명 (`gitclient.dmg`) | 무시 |
| `release/<버전>/` | 배포 준비대 — 검증 끝난 산출물만 옮겨 담는 곳 | 정식 (`gitclient-0.1.0-macos-arm64.dmg`) | 무시 (§2.2) |

`dist/`에서 `release/`로 옮기는 조건: 해당 커밋의 CI가 전부 녹색이고,
산출물이 `--version`에 제 버전을 답하고, 체크섬을 기록했다.

## 3. 이번 배포(0.1.0)의 절차 기록

재현 가능하도록 실제 수행한 순서 그대로다.

```sh
# 0. 배포 커밋 확정 — CI 전부 녹색인지 확인
gh run list --limit 1        # 커밋과 결론 확인

# 1. macOS (로컬, Apple Silicon)
uv pip install -e . --no-deps --python .venv/bin/python   # §1.3-2
.venv/bin/pyinstaller -y --distpath dist --workpath build packaging/gitclient.spec
./dist/gitclient/gitclient --version    # "gitclient 0.1.0"이어야 한다
sh packaging/make_dmg.sh

# 2. Windows (CI 산출물 회수)
gh run download <run-id> -n gitclient-Windows -D <임시폴더>
#    <run-id>의 headSha == 배포 커밋인지 먼저 확인 (§1.1)

# 3. 준비대에 정식 이름으로 담고 체크섬 기록
mkdir -p release/0.1.0
cp dist/gitclient.dmg release/0.1.0/gitclient-0.1.0-macos-arm64.dmg
cp <임시폴더>/gitclient-setup.exe release/0.1.0/gitclient-0.1.0-windows-x64-setup.exe
(cd release/0.1.0 && shasum -a 256 *.dmg *.exe > SHA256SUMS.txt)

# 4. (선택) 공개 — GitHub Releases
# gh release create v0.1.0 release/0.1.0/* --title "0.1.0"
```
