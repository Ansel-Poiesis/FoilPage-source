; Copyright (c) 2026 Ansel
; SPDX-License-Identifier: MPL-2.0
#define AppVersion "0.1.4"
#define AppExeName "FoilPage-Desktop.exe"

[Setup]
AppId={{5F2EE512-3A43-4E61-94D7-F77B76B8D245}
AppName=FoilPage
AppVersion={#AppVersion}
VersionInfoVersion=0.1.4.0
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
var
  // 先生 2026-11-16：卸载无残留。usUninstall 阶段问一次是否删除用户数据
  // （默认保留），usPostUninstall 按答案清理。数据根含文稿/任务/设置，
  // 绝不默认删除。
  DeleteUserData: Boolean;

procedure KillFoilPageProcesses();
var
  ResultCode: Integer;
  Names: array[0..5] of String;
  I: Integer;
begin
  // Worker/Agent 不持有 AppMutex，CloseApplications 管不到；Desktop 强杀
  // 保证单例互斥量立即释放。/T 连同子进程树。
  Names[0] := 'FoilPage-Desktop.exe';
  Names[1] := 'FoilPage-Worker.exe';
  Names[2] := 'FoilPage-Agent.exe';
  Names[3] := 'ShengJian-Agent.exe';
  Names[4] := 'VideoToArticle-Desktop.exe';
  Names[5] := 'VideoToArticle-Worker.exe';
  for I := 0 to 5 do
    Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM ' + Names[I] + ' /T 2>nul', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(2000);
end;

procedure RemoveDirectoryLink(const Path: String);
var
  ResultCode: Integer;
begin
  // junction 垫片用 rmdir 摘除：只删联接或空目录本身，绝不穿进目标删
  // 真实数据（DelTree 会）；非空真实目录 rmdir 直接失败不动。Inno Pascal
  // 无文件属性 API，rmdir 的天然语义即判别器。
  Exec(ExpandConstant('{cmd}'), '/C rmdir "' + Path + '"', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then begin
    // 2026-11-03 预防：升级时旧 Worker 可能残留（同进程名，CloseApplications
    // 不保证全部终止），导致双 Worker 竞争数据库、心跳互踩、任务集中判死。
    KillFoilPageProcesses();
    DeleteFile(ExpandConstant('{app}\browser-extension\capture.js'));
    DeleteFile(ExpandConstant('{app}\browser-extension\page-hook.js'));
    DeleteFile(ExpandConstant('{app}\browser-extension\browser-session.js'));
    DelTree(ExpandConstant('{app}\browser-extension\fonts\fonts'), True, True, True);
    DelTree(ExpandConstant('{app}\browser-extension\icons\icons'), True, True, True);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  LegacyRoot: String;
  DataRoot: String;
  TempDir: String;
begin
  if CurUninstallStep = usUninstall then begin
    KillFoilPageProcesses();
    // 运行时写入的开机自启值（app/startup.py）安装器不跟踪，卸载必须显式删。
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'ShengJian');
    // 先生 2026-11-16：静默卸载（/VERYSILENT）下 MsgBox 仍会弹窗并无限等待
    // ——实装验收实测卡死。静默模式默认保留数据（等效选「否」）。
    if UninstallSilent() then
      DeleteUserData := False
    else
      DeleteUserData := (MsgBox(
        '是否同时删除文稿与设置数据？' + #13#10 + #13#10 +
        '选择「是」将删除 %LOCALAPPDATA%\FoilPage 与旧版 %LOCALAPPDATA%\VideoToArticle 数据目录（含文稿、任务、下载与设置，不可恢复）。' + #13#10 +
        '选择「否」保留全部数据，便于以后重装续用。语音模型缓存（%USERPROFILE%\.cache\huggingface，数 GB）两种选择下都不会删除，可手动清理。',
        mbConfirmation, MB_YESNO) = IDYES);
  end;
  if CurUninstallStep = usPostUninstall then begin
    LegacyRoot := ExpandConstant('{localappdata}') + '\VideoToArticle';
    DataRoot := ExpandConstant('{localappdata}') + '\FoilPage';
    // junction 垫片无条件摘除（它只是指向新根的链接，1.3.8 迁移的回滚垫
    // 物，程序已卸载即失去意义）；rmdir 对非空真实旧数据目录无操作。
    RemoveDirectoryLink(LegacyRoot);
    if DeleteUserData then begin
      DelTree(LegacyRoot, True, True, True);
      DelTree(DataRoot, True, True, True);
    end;
    // 卸载日志只删安装器写过的文件；运行期落进 {app} 的散件（实机验收
    // 观察到 ui-kit.css 预览文件）会挡住安装根删除。此时产品文件已全部
    // 卸载、目录归本产品所有，整体清空收尾。
    if DirExists(ExpandConstant('{app}')) then
      DelTree(ExpandConstant('{app}'), True, True, True);
    // 完整卸载后重装会落在默认根 Programs\FoilPage；此时旧根
    // Programs\VideoToArticle（若与 {app} 不同根）成为跨根孤儿——只要
    // 里面还有本产品骨架（_internal\app），一并清掉，不误删无关目录。
    if (LowerCase(ExpandConstant('{app}')) <> LowerCase(LegacyRoot))
      and DirExists(LegacyRoot + '\_internal\app') then
      DelTree(LegacyRoot, True, True, True);
    // 转写崩溃残留的临时目录（run_engine 正常路径自清，异常退出时留下）。
    TempDir := GetEnv('TEMP');
    if TempDir <> '' then begin
      Exec(ExpandConstant('{cmd}'), '/C del /q "' + TempDir + '\v2a_*" 2>nul', '',
        SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(ExpandConstant('{cmd}'),
        '/C for /d %i in ("' + TempDir + '\v2a_*") do @rmdir /s /q "%i"', '',
        SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
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
