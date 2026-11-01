# AION2 RedBag Map

用于 AION2 的本地红色背包记录与地图提醒工具。收到本机游戏连接中的背包记录后，显示坐标、提示新记录，并根据后续事件更新交互、开启或未知状态。

**版本：1.4.1 · Windows 桌面 · 原创代码与原创数据使用 MIT 许可证**

[English](README.en.md) · [架构](docs/architecture.md) · [数据与素材](docs/data-and-assets.md) · [构建与发布](docs/releasing.md)

本项目不是官方工具，也没有获得游戏运营方认可。只接收本机实际收到的记录，**不保证全地图、所有线路或所有服务器的刷新都能被发现**。收到记录不等于刚刷新，也不保证到达时仍可拾取。

## 能做什么

- 用一个桌面管理窗口开始或停止接收，打开悬浮地图、网页地图、历史记录和数据目录。
- 深色桌面界面与原生高 DPI 绘制；悬浮地图使用清晰描边图标区分背包、即将刷新、已开启和候选点，并提供图例和距离尺。
- 新红色背包触发声音与系统通知，并在地图上选中目标。
- 新目标保留近景；跟随模式保持角色参考点居中，远处目标显示画面边缘箭头。
- 拖动地图自动退出跟随，后续坐标更新不会拉回视角；点击“跟随”可重新居中，“全图”也可直接自由浏览。
- 悬浮地图与网页默认显示蓝色菱形候选点，支持显隐和点击查看；候选点不代表当前可拾取。
- 点击“取消连线”清除目标、连线和箭头，保留历史；新的背包提醒仍会正常出现。悬浮地图也支持右键取消。
- 收到后续事件时区分“有人交互”“已开启”和“状态未知”。
- 保存本地历史，按坐标查看记录；刷新窗口为估算，不冒充已确认刷新。
- 内置 18 张地图的底图、校准坐标和高清分块，打开即可使用；用户导入的配套资源可覆盖内置版本。
- 保留完整历史候选点和公开观测数据，来源与许可分别说明。

角色位置来自间歇收到的参考坐标，**10 秒后过期**，不是连续定位。过期时不显示实时距离；画面边缘箭头表示目标相对于当前地图画面的方向。

同一连接暂时没有数据时保留已确认地图，新记录到达后自动恢复定位。真正断开、更换连接或重新启动工具后，若显示“地图待确认”，正常切换一次线路或地图即可重新确认；浏览世界地图本身不作为角色地图的证据。

## 普通用户使用

1. 将 `AION2-RedBag-Map-1.4.1-windows-x64.zip` 完整解压。
2. 打开解压后的 `AION2 RedBag Map` 文件夹，双击 **`AION2 RedBag Map.exe`**，保留旁边的 `_internal` 文件夹。
3. 管理窗口启动后默认不接收游戏数据。需要实时提醒时，运行游戏并点击“开始实时接收”。
4. 点击“显示悬浮地图”或“打开完整地图”。发现新记录时会提示；点击目标可查看坐标与当前记录状态。
5. 在管理窗口点击“停止接收”结束接收。历史记录保存在数据目录，升级程序不会要求删除它们。

“浏览历史”打开已有记录，“打开数据目录”查看本机存档。内置底图无需导入；“导入地图资源”可选择有权使用的配套底图和坐标配置，覆盖内置资源。只浏览地图和历史不会隐式开始接收。

发布包只有一个供用户启动的 EXE 入口，运行依赖放在 `_internal` 内。不要只复制 EXE，也不要在压缩包内直接运行。普通用户无需另装 Python。

### 实时接收依赖

实时接收需要已安装的 **[Npcap](https://npcap.com/#download)**。Npcap 由用户从官方单独获取并遵守其许可，本项目不捆绑安装程序或驱动。未安装或当前账户无法访问 Npcap 时，仍可查看内置地图和历史记录。

工具不会自行提权、修改 PowerShell 执行策略、安装驱动或设置开机自启。不要为排错关闭安全软件。当前 EXE 未签名，不能承诺 SmartScreen 或杀毒软件不提示；报告疑似误报时，请提供版本、文件 SHA-256 和检测名称。

Git 仅用于可选的本地历史版本归档，普通地图与接收功能不依赖 Git。归档不自动推送到远端。

## 从源码运行

在仓库根目录使用 Windows PowerShell。建议使用 64 位 Python 3.12，且包含 Tkinter。

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -e .
.venv/Scripts/python.exe -m aion2_redbag_map
```

这会打开同一个管理入口，默认不开始接收。直接调用虚拟环境里的 Python，无需激活脚本或更改执行策略。依赖定义见 [pyproject.toml](pyproject.toml) 和 [requirements.txt](requirements.txt)。

## 构建与测试

安装构建依赖后，一条命令生成 Windows 包：

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-build.txt
.venv/Scripts/python.exe scripts/build.py
```

构建输出位于 `dist/`：

- `AION2 RedBag Map/AION2 RedBag Map.exe` 与 `_internal/`：可直接运行的单目录版本。
- `AION2-RedBag-Map-1.4.1-windows-x64.zip`：完整 Windows 包。
- `aion2-redbag-map-1.4.1-source.zip`：源码包。
- `SHA256SUMS.txt`：上述两个 ZIP 的 SHA-256。

Windows 包的 `_internal/` 内还包含本次构建对应的源码、公开数据、构建信息和实际依赖许可。构建不会发布或上传 GitHub。具体布局与检查方法见 [构建与发布](docs/releasing.md)。

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
node tests/test_i18n.cjs
node tests/test_player_ui.cjs
node tests/test_refresh_ui.cjs
```

Node.js 仅用于网页测试，不是用户运行 EXE 的前置依赖。协议与状态测试使用离线、合成数据；测试通过不代表所有游戏版本、加速器或网络环境均已验证。

## 数据、底图与隐私

默认数据目录是 Windows 的“文档”文件夹下：

```text
AION2-RedBag-Map/data/
```

管理窗口可打开该目录。个人历史、设置、日志和导入素材保存在这里；不是程序安装目录。没有遥测、远端更新检查或自动上传。网页服务仅监听本机地址，默认端口为 `18726`。

| 文件或目录 | 内容 |
| --- | --- |
| [`src/aion2_redbag_map/static/candidates.json`](src/aion2_redbag_map/static/candidates.json) | A2Helper 来源的完整候选点、分组与溯源信息；历史候选不等于红色或当前可拾取。 |
| [`data/observations.json`](data/observations.json) | 本项目公开的脱敏观测数据；记录数以文件内 `record_count` 为准。 |
| [`static/maps/`](src/aion2_redbag_map/static/maps) 与 [`maps.json`](src/aion2_redbag_map/maps.json) | 内置的 18 张总览、321 张高清分块及原坐标标定。 |
| [`map-source.json`](src/aion2_redbag_map/map-source.json) | 地图素材的来源版本、发布链接、核对时间及全部 339 张图片的 SHA-256。 |
| 本机数据目录中的 `history.json` | 用户自己的持续观测历史，不自动写回仓库或公开数据集。 |
| 本机数据目录中的 `assets/` | 用户导入的底图与坐标配置。 |

完整候选数据保留 **3,722 条来源记录、3,721 个去重坐标点**。两种计数口径分别保留，不能据此认定存在同样数量的红色背包。来源与转换记录见 [data/provenance.json](data/provenance.json)。

仓库与发布包完整保留当前 339 张底图及分块和校准版 `maps.json`，无需下载或另行导入。素材最初来自 A2Helper 0.1.25 分发包，已逐项核对与 0.1.29 一致。可选导入目录包含 `maps.json` 与 `maps/`，也兼容旧资源包的 `static/maps/` 布局；导入配置和同名图片优先于内置资源。工具不自动下载地图。格式、覆盖方式和权利说明见 [数据与素材](docs/data-and-assets.md)。

## 实现边界

接收器查询 Windows 进程名称、PID 和 TCP 连接表，为游戏连接设置精确入站过滤，然后通过 Npcap 接收并解析数据。它不打开游戏进程句柄、不读取或写入游戏内存、不注入、不操作游戏输入，也不回发游戏数据包。

`git_archive.py` 中的 `OpenProcess(SYNCHRONIZE)` 只用于等待工具自己的父进程退出，不访问游戏内存。完整的数据流和模块职责见 [架构说明](docs/architecture.md)。

以上是实现范围，不是对游戏规则或反作弊判断的保证。未收到某个对象时，工具不能判断它不存在；离开接收范围、暂停或断线也不能等同于“已被打开”。

## 许可证与贡献

[MIT 许可证](LICENSE) 覆盖本项目原创代码和原创公开数据。第三方组件、点位与素材保留各自权利，见 [第三方声明](THIRD_PARTY_NOTICES.md) 和 [数据与素材](docs/data-and-assets.md)。

**A2Helper 来源的候选点、地图图片与坐标标定的再分发授权尚未确认。** 这些文件按项目作者要求完整纳入源码与发行配置，并保留出处和原权利状态；这不代表本项目有权将它们重新许可为 MIT，也不构成游戏官方认可。下游使用与再分发仍需核实来源许可。

提交修复、点位校正或问题报告前，请阅读 [贡献说明](CONTRIBUTING.md)。不要在公开问题中附上个人存档、原始网络抓包、控制令牌或未脱敏日志。
