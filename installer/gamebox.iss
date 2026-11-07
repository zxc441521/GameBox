; Inno Setup 脚本：游匣 GameBox Windows 安装包
; 说明：本项目不提供编译入口，此文件仅保留版本信息配置

#define MyAppName "游匣"
#define MyAppVersion "1.0"
#define MyAppPublisher "zxc4415"
#define MyAppExeName "GameBox.exe"

[Setup]
AppId={{7F2C4B1A-9D5E-4A2B-8E1C-3F6A0B7C9D10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\GameBox
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=GameBox-Setup-v{#MyAppVersion}
SetupIconFile=..\data\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
DisableDirPage=no
AppMutex=GameBox_SingleInstance_Mutex,GameBox_App_Mutex,GameVault_App_Mutex
CloseApplications=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
Source: "..\dist\GameBox\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "config.json,games_cache.json,meta_cache.json,cover_cache\*,media\*"

; 覆盖安装时清理旧入口文件，避免开始菜单/桌面出现重复程序
[InstallDelete]
Type: files; Name: "{app}\游戏收藏夹.exe"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
