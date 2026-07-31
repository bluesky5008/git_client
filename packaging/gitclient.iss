; Windows 인스톨러 (Inno Setup 6+, design.md §9).
;
; **onedir 배포본을 그대로 담는다** — PySide6/Qt가 LGPLv3이라 공유
; 라이브러리가 교체 가능한 파일로 남아야 한다 (THIRD-PARTY-NOTICES.md).
; 하나로 묶는 옵션(onefile, 압축 SFX)을 쓰지 않는 이유가 그것이다.
;
; git은 **동봉하지 않는다** — 시스템 설치본을 쓴다(README 요구사항). 없는
; 환경을 위해 설치 마지막에 확인하고 안내만 한다: 우리가 GPLv2 바이너리를
; 재배포하지 않으면서도 사용자가 막히지 않는 선이다.
;
; 서명은 **인증서가 있을 때만** — /DSIGN 없이 빌드하면 서명 단계 자체가
; 스크립트에서 사라져, 없는 환경에서도 같은 파일이 끝까지 돈다
; (인증서 취득 절차는 doc/release.md §4):
;
;   iscc /DSIGN "/Ssigntool=signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /a $f" packaging\gitclient.iss
;
; 빌드(서명 없이):  iscc packaging\gitclient.iss

#define AppName "Git Client"
#define AppExe "gitclient.exe"

[Setup]
AppName={#AppName}
AppVersion=0.1.0
AppPublisher=yongs
DefaultDirName={autopf}\GitClient
DefaultGroupName={#AppName}
; 관리자 권한을 요구하지 않는다 — 사용자 폴더에 설치되면 충분하고,
; 권한 상승은 설치를 막는 가장 흔한 이유다.
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=gitclient-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
; 제3자 고지를 설치 화면에서 보여준다 (LGPL 준수의 일부).
InfoAfterFile=..\THIRD-PARTY-NOTICES.md
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#ifdef SIGN
SignTool=signtool
SignedUninstaller=yes
#endif

[Files]
#ifdef SIGN
; 앱 실행 파일은 sign 플래그로 함께 서명한다 — 인스톨러만 서명하면
; SmartScreen은 조용한데 설치된 앱이 실행될 때 다시 경고를 띄운다.
; (Qt DLL 수백 개까지 서명하는 것은 시간 대비 이득이 없다 — 경고를
; 내는 주체는 실행 파일이다.)
Source: "..\dist\gitclient\gitclient.exe"; DestDir: "{app}"; Flags: ignoreversion sign
Source: "..\dist\gitclient\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "gitclient.exe"
#else
Source: "..\dist\gitclient\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 아이콘 만들기"; GroupDescription: "추가 작업:"

[Run]
Filename: "{app}\{#AppExe}"; Description: "지금 실행"; Flags: nowait postinstall skipifsilent

[Code]
function GitIsInstalled(): Boolean;
var
  Code: Integer;
begin
  { git이 PATH에 있는지 본다 — 없으면 앱이 첫 실행에서 안내하지만,
    설치 시점에 미리 말해 주는 편이 친절하다. }
  Result := Exec('cmd.exe', '/c where git', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not GitIsInstalled()) then
    MsgBox('시스템에 git이 보이지 않습니다.' + #13#10 +
           'Git Client는 시스템에 설치된 git 2.40 이상을 사용합니다 — ' +
           'https://git-scm.com 에서 설치한 뒤 실행해 주세요.',
           mbInformation, MB_OK);
end;
