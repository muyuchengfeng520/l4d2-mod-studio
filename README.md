# L4D2 Mod Studio

《求生之路 2》mod 管理器（Windows 桌面程序）。围绕 **VPK 内部结构**做管理，而不是只看文件名：
分类依据 `addoninfo.txt` 与内部路径，修改保持在字节级可校验的范围内。

> 非官方项目，与 Valve Corporation 无关联。Left 4 Dead 2 及相关名称、内容为 Valve 的商标与版权财产。
> 本工具只读取与修改本地文件，不包含任何游戏资源。

- 运行时仅依赖 Python 标准库（构建 exe 时用 pywebview + PyInstaller）
- 界面为 WebView2 原生窗口，单文件 exe，无需安装 Python
- 兼容 FireAxe 式仓库：mod 实体放仓库、`addons/` 下放符号链接

## 功能

| 功能 | 说明 |
|---|---|
| 分类与标签 | 按官方标签、素材级标签、内部路径启发式三级判定，输出判定依据 |
| 组件精修 | 按标签/目录/单条路径剔除 VPK 内部文件，重写后逐条校验 |
| 启用管理 | 读写 `addonlist.txt`，支持批量开关、隔离卸载、还原 |
| 游戏文件替换 | 启动动画、加载画面、主菜单背景等位置的替换与还原 |
| 冲突审计 | 逐文件冲突、目录聚合、冗余 VPK、前置缺失检测 |
| AI 冲突复核 | 可选，接 OpenAI 兼容接口，判断冲突的实际后果 |
| 模型归属 | 将某生还者的替换模型改到另一位生还者槽位 |
| 获取 mod | 本地 VPK 一键入库；创意工坊链接直接下载并入仓库（无需 SteamCMD） |

## 快速开始

**exe**：从 [Releases](../../releases) 下载 `L4D2ModStudio.exe` 后运行。首次启动自动创建 `data/` 目录并探测游戏路径。

**源码**：需要 Python 3.10+。

```bash
python desktop.py          # 或 START.bat（保留控制台，便于排障）
```

首次启动读取 `libraryfolders.vdf` 探测 Steam 库（支持游戏分布在多个库），
对候选游戏目录按完整性打分，避免选中残留安装。

## 使用前提

工具管理的是**已经装好在 `addons/` 里的 mod**，不负责下载来源（创意工坊订阅或第三方下载器）。

推荐仓库结构（FireAxe 模式）：

```
<仓库>/<分类>/<标题>.workshop/
    ├─ <标题>-time_updated-<时间戳>.vpk     实体文件
    └─ .workshop                            元数据 JSON
addons/<链接名>.vpk -> 上述实体              符号链接
```

`.workshop` 元数据格式：

```json
{
  "PublishedFileId": 2947181894,
  "TimeUpdated": 1734084358,
  "CurrentFile": "<标题>-time_updated-1734084358.vpk"
}
```

不在该结构内的普通 VPK（直接放在 `addons/` 下）同样支持，此时仅能使用 addons 侧的功能。

## 技术说明

- **VPK**：支持 v1/v2 读取，写入受限于 v1；重写后按 CRC 与逐条内容校验
- **addonlist.txt**：按行修改，保留原有缩进、顺序与手工条目
- **标签库**：内置 75 标签 / 204 条路径规则（源自 NekoVpk 的 `TaggedAssets.jsonc`）；`data/taglib/` 下放同名文件可覆盖
- **数据目录**：与 exe 同级，整个目录可迁移；无写权限时退到 `%LOCALAPPDATA%`
- **创意工坊下载**：Steam Web API 取直链后经系统 shell（优先 PowerShell 7）下载，
  因此能复用系统代理配置；不可达时回退为打开浏览器手动下载
- **服务**：仅监听 `127.0.0.1`，前端为本地 HTTP 服务 + 原生 JS，无框架

## 安全设计

- 所有写操作前备份，写入采用临时文件 + 原子替换
- 删除走回收站
- 批量操作单批上限 10 项，预演与执行分离
- 每个接口校验请求来源，拒绝跨站调用

## 构建

```bat
tools\build_exe.bat
```

自动创建隔离 venv、安装构建依赖、生成图标并冻结为单文件 exe，构建产物均在项目目录内。

## 发布

仓库只包含源码。可执行文件通过 Releases 页面分发，不在 git 历史中：

- `L4D2ModStudio.exe` —— 单文件，无需安装

需要自行构建时见上一节。

## 已知限制

- 分类为启发式判定，最终以游戏内表现与日志为准
- 前置缺失检测仅覆盖已知的四类依赖关系
- 拒绝重写疑似压缩的 VPK（v2 写入显式报错）
- 加载顺序只查看与改写 `addonlist.txt`，不自动排序
- 模型归属不保证游戏内表现：内部动画层名无法逐一校验
- AI 复核结论为参考，取决于所接模型能力
- 创意工坊下载依赖系统代理可达；无可用代理时回退为浏览器下载

## 许可

本项目代码以 MIT 许可发布（见 `LICENSE`）。内置标签库数据源自
[NekoVpk](https://github.com/Starfelll/NekoVpk)（GPL-3.0），以字面量形式内联，
随本仓库分发。构建期依赖：pywebview（BSD）、PyInstaller（GPL-2.0 含例外）。
