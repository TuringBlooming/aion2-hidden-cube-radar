# 构建与发布

目标是让一个 Windows 桌面入口、对应源码和构建步骤可以互相核对。当前版本为 1.4.2。构建只生成本地产物，不自动创建仓库、上传 Release 或发布数据。

## 环境

使用 Windows x64 与 Python 3.12 x64，Python 安装需包含 Tkinter。首次在仓库根目录准备环境：

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -e .
.venv/Scripts/python.exe -m pip install -r requirements-build.txt
```

无需激活虚拟环境或修改执行策略。[Python 文档](https://docs.python.org/3/library/venv.html#how-venvs-work) 支持直接运行虚拟环境中的解释器。

## 测试与构建

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
node tests/test_i18n.cjs
node tests/test_player_ui.cjs
node tests/test_refresh_ui.cjs
.venv/Scripts/python.exe scripts/build.py
.venv/Scripts/python.exe scripts/verify_package.py
```

构建输出为 `dist/AION2 Hidden Cube Radar/`、`dist/AION2-Hidden-Cube-Radar-1.4.2-windows-x64.zip`、`dist/aion2-hidden-cube-radar-1.4.2-source.zip`，以及记录两个 ZIP 哈希的 `dist/SHA256SUMS.txt`。构建依赖以 [requirements-build.txt](../requirements-build.txt) 为准；版本由源码包元数据维护。若某项测试需要特定平台或依赖，应区分通过、跳过和未运行，不能把跳过记录写成运行通过。

测试采用合成夹具和本地状态，不应默认启动真实游戏接收、访问个人历史或下载第三方素材。需要真实游戏验证时，应单独记录版本、环境及实际验证范围。

双击入口默认不接收数据。需要由外部启动器明确启动接收时，可使用 `--start-receiving --show-overlay --minimized`：管理窗口最小化后依次启动接收与悬浮地图，子进程仍归该窗口管理，退出时一并停止。单独使用 `--show-overlay` 只浏览历史，不隐式开启实时接收。

## 发布包布局

Windows 清单声明 `PerMonitorV2`，源码运行在创建 Tk 窗口前设置同一模式。布局尺寸按屏幕 DPI 缩放，字体使用 point 单位，点位图标按最终物理像素重新绘制。打包检查同时验证该清单声明，避免 Windows 把整个窗口作为低分辨率位图放大。参见 [Microsoft 高 DPI 文档](https://learn.microsoft.com/en-us/windows/win32/hidpi/high-dpi-desktop-application-development-on-windows)。

用户启动入口只有 `AION2 Hidden Cube Radar.exe`；运行文件放在 `_internal/` 内：

```text
dist/AION2 Hidden Cube Radar/
├── AION2 Hidden Cube Radar.exe
└── _internal/
    ├── build-info.json
    ├── SHA256SUMS.txt
    ├── source/                 # 本次构建对应的源码、测试、脚本与 README
    ├── reference-data/
    │   ├── observations.json
    │   └── provenance.json
    ├── licenses/               # 实际安装依赖的许可文本
    ├── aion2_redbag_map/
    │   ├── maps.json           # 18 张地图的坐标标定
    │   ├── map-source.json     # 来源与逐图 SHA-256
    │   └── static/maps/        # 18 张总览和 321 张分块
    └── 运行依赖与程序资源
```

完整目录压缩为 Windows ZIP 后分发，解压得到 `AION2 Hidden Cube Radar/` 文件夹，不把 EXE 单独拿出。源码 ZIP 的顶层目录为 `aion2-hidden-cube-radar-1.4.2/`。这个布局使用 [PyInstaller 的单目录模式](https://pyinstaller.org/en/stable/operating-mode.html#bundling-to-one-folder)，普通用户不需要 Python。构建目标与运行平台对应；在 Windows 上验证 Windows 产物。

`build-info.json` 记录版本、构建时间、Python 与依赖版本，以及源码文件哈希。`_internal/SHA256SUMS.txt` 记录包内文件的哈希，`dist/SHA256SUMS.txt` 记录最终两个 ZIP 的哈希。构建脚本同时核对候选点、公开观测、坐标标定和来源清单的字节一致性，并按 `map-source.json` 验证全部 339 张地图图片。

源码与发布包均保留完整候选点、公开观测数据、全部现有地图图片和分块及原坐标标定，来源与权利说明随包保留。Npcap 安装器、个人运行历史和原始抓包不进入公开包。

## 离线自检

可以先检查入口、模块与必需资源，不打开窗口、不启动后台接收，也不需要运行游戏：

```powershell
.venv/Scripts/python.exe -m aion2_redbag_map --self-check build/source-self-check.json
& 'dist/AION2 Hidden Cube Radar/AION2 Hidden Cube Radar.exe' --self-check build/exe-self-check.json
```

检查生成的 JSON 中 `ok` 是否为 `true`；EXE 报告还应有 `frozen: true`。资源计数应为 18 张地图、3,721 个候选点和 `bundled_map_images: 339`。自检也记录 `capture_started: false` 与 `gui_started: false`。`verify_package.py` 进一步校验全部图片哈希，并在临时只读服务中读取一张总览及一张分块；报告保存在 `build/qa/frozen-service-check.json`。这些检查不替代真实窗口操作或接收验证。

## 发布前核对

- 从干净源码目录完成构建，记录源码版本、Python 版本、构建依赖和构建命令。
- 从解压后的 EXE 启动管理窗口，确认默认不采集；检查“开始实时接收”“停止接收”“显示悬浮地图”“打开完整地图”“浏览历史”“打开数据目录”“导入地图资源”。
- 未安装 Npcap 的环境应能打开管理界面、内置地图和历史记录，接收失败应给出明确状态。
- 核对候选数据的来源数量与溯源信息、观测数据 `record_count`、许可证和第三方声明。
- 核对内置图片、坐标标定和来源清单完整一致，保留上游权利说明；检查包内没有个人运行存档、令牌、原始抓包、测试产物或 Npcap 驱动。
- 对最终 ZIP 和 EXE 计算 SHA-256，并记录实际测试范围。二进制哈希验证文件身份，不代表代码签名或完整安全认证。

可在 PowerShell 中计算本地文件哈希：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath 'dist/AION2 Hidden Cube Radar/AION2 Hidden Cube Radar.exe'
```

不要把本地构建完成写成已经公开发布；未确定的仓库地址、下载地址或签名状态不能补成示例事实。

## 许可与签名状态

MIT 仅覆盖本项目原创代码和原创数据。**A2Helper 来源候选点、地图图片和坐标标定的再分发许可尚未确认**；完整纳入源码和构建资源不等于获得上游授权。数据与素材保持完整，来源和权利状态也必须随文档保留，详见 [数据与素材](data-and-assets.md)。

当前产物未签名。SmartScreen 或杀毒软件可能给出提示，不能承诺永不报毒。疑似误报应记录版本、哈希、检测名称及报告时间，核对来源后按安全厂商的正式误报流程处理。不要提供关闭防护、隐藏进程、替换驱动或绕过检测的步骤。

构建记录和内容清单使源码与发布包可审查，不等同于已经实现逐字节可复现构建。未经实际验证，不声称不同机器生成完全相同的二进制文件。
