; Copyright (c) 2026 Ansel
; SPDX-License-Identifier: MPL-2.0
#define AppVersion "0.1.0"
#define AppExeName "FoilPage-Desktop.exe"

[Setup]
AppId={{5F2EE512-3A43-4E61-94D7-F77B76B8D245}
AppName=FoilPage
AppVersion={#AppVersion}
VersionInfoVersion=0.1.0.0
AppPublisher=Ansel
MinVersion=10.0.17763
DefaultDirName={localappdata}\Programs\FoilPage
DefaultGroupName=FoilPage
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist_installer
OutputBaseFilename=FoilPage-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\_internal\assets\icons\foilpage.ico
AppMutex=Local\FoilPageDesktop
SetupIconFile=..\assets\icons\foilpage.ico

[Files]
Source: "..\dist\FoilPage-Desktop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 1.3.8 品牌统一：清掉旧名主程序与 worker（agent 的旧名副本是刻意的兼容垫片，保留）
Type: files; Name: "{app}\VideoToArticle-Desktop.exe"
Type: files; Name: "{app}\worker\VideoToArticle-Worker.exe"
; Removed in 0.3.0: these page-level hooks caused stale-context and fetch errors.
Type: files; Name: "{app}\browser-extension\capture.js"
Type: files; Name: "{app}\browser-extension\page-hook.js"
Type: files; Name: "{app}\browser-extension\browser-session.js"
Type: filesandordirs; Name: "{app}\browser-extension\fonts\fonts"
Type: filesandordirs; Name: "{app}\browser-extension\icons\icons"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then begin
    // 2026-11-03 预防：升级时旧 Worker 可能残留（同进程名，CloseApplications
    // 不保证全部终止），导致双 Worker 竞争数据库、心跳互踩、任务集中判死。
    Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM FoilPage-Worker.exe /T 2>nul', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM VideoToArticle-Worker.exe /T 2>nul', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(2000);
    DeleteFile(ExpandConstant('{app}\browser-extension\capture.js'));
    DeleteFile(ExpandConstant('{app}\browser-extension\page-hook.js'));
    DeleteFile(ExpandConstant('{app}\browser-extension\browser-session.js'));
    DelTree(ExpandConstant('{app}\browser-extension\fonts\fonts'), True, True, True);
    DelTree(ExpandConstant('{app}\browser-extension\icons\icons'), True, True, True);
  end;
end;

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Icons]
Name: "{group}\FoilPage"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\icons\foilpage.ico"; AppUserModelID: "Ansel.ShengJian.Desktop"
Name: "{autodesktop}\FoilPage"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\_internal\assets\icons\foilpage.ico"; Tasks: desktopicon; AppUserModelID: "Ansel.ShengJian.Desktop"

[Registry]
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\Ansel.ShengJian.Desktop"; ValueType: string; ValueName: "DisplayName"; ValueData: "FoilPage"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\Ansel.ShengJian.Desktop"; ValueType: string; ValueName: "IconUri"; ValueData: "{app}\_internal\assets\icons\foilpage.png"
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\Ansel.ShengJian.Desktop"; ValueType: dword; ValueName: "ShowInSettings"; ValueData: "1"
; 先生 2026-10-19：shengjian:// 协议——扩展在桌面端未连接时可一键拉起应用
; （后台静默启动；应用参解析见 desktop_web._main 的协议参归一）。
Root: HKCU; Subkey: "Software\Classes\shengjian"; ValueType: string; ValueName: ""; ValueData: "URL:shengjian protocol"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\shengjian"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\shengjian\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\_internal\assets\icons\foilpage.png"
Root: HKCU; Subkey: "Software\Classes\shengjian\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" --background"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动FoilPage"; Flags: nowait postinstall skipifsilent
