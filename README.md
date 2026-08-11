# Steam Save Manager

[中文](./README.md) | [English](./README_EN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D6.svg)](https://www.microsoft.com/windows/)

一个面向 Windows 的 Steam 游戏存档管理工具，提供存档扫描、备份、恢复、自动监控、云同步、冲突处理、托盘运行和远程更新等功能。

<img width="1624" height="1127" alt="995eda24b4bb45c6a09e335600e41e52" src="https://github.com/user-attachments/assets/30779f8b-8515-44ad-9211-719df286077d" />

## 功能特性

- 自动扫描已安装的 Steam 游戏，并尝试识别常见本地存档目录
- 支持手动添加游戏、编辑存档路径、导入外部存档
- 支持手动备份、批量备份、恢复备份、删除备份
- 支持定时备份、文件变动监控和游戏退出后备份；游戏运行期间自动延后归档，避免读取正在写入的存档
- 支持本地同步文件夹联动，可用于 OneDrive、Dropbox、Google Drive 等云盘
- 支持智能云存档模式：游戏启动时下载、退出时上传
- 支持双向同步基线、冲突检测、重试队列
- 支持中英文界面、系统托盘最小化和双击托盘恢复主窗口
- 支持远程更新检查和新版本下载安装

## 运行环境

- Python 3.10+
- Windows

## 本地部署

### 1. 克隆项目

```bash
git clone https://github.com/Kiowx/save_manager.git
cd save_manager
```

### 2. 创建虚拟环境

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Windows CMD:

```bat
.venv\Scripts\activate.bat
```

### 3. 安装依赖

```bash
pip install customtkinter pillow psutil watchdog pystray pyinstaller
```

如果你只想本地运行最小功能，也可以先安装核心依赖：

```bash
pip install customtkinter pillow
```

### 4. 启动程序

```bash
python main.py
```

### 5. 可选：打包为 exe

```bash
pyinstaller SteamSaveManager.spec
```

生成文件默认位于：

- `dist/SteamSaveManager.exe`

## 依赖

核心依赖：

- `customtkinter`
- `Pillow`

可选依赖：

- `psutil`
  用于更完整的游戏进程检测
- `watchdog`
  用于文件变动监控自动备份
- `pystray`
  用于系统托盘运行
- `PyInstaller`
  用于打包为 `.exe`

安装示例：

```bash
pip install customtkinter pillow psutil watchdog pystray pyinstaller
```

## 启动方式

直接运行：

```bash
python main.py
```

启动后主窗口会默认居中显示。

## 打包

项目已包含 PyInstaller 配置文件 [SteamSaveManager.spec](d:\project\steam\SteamSaveManager.spec)。

打包示例：

```bash
pyinstaller SteamSaveManager.spec
```

输出文件默认位于：

- `dist/SteamSaveManager.exe`

## 远程更新

当前内置的更新清单地址：

```text
https://raw.githubusercontent.com/Kiowx/save_manager/refs/heads/main/update/update.json
```

支持的最简 JSON 格式：

```json
{
  "version": "1.1.0",
  "notes": "新增远程更新、修复托盘交互",
  "url": "https://example.com/releases/SteamSaveManager-1.1.0.exe"
}
```

推荐格式：

```json
{
  "version": "1.1.0",
  "notes": "新增远程更新、修复托盘交互",
  "url": "https://example.com/releases/SteamSaveManager-1.1.0.exe",
  "sha256": "下载文件的 SHA256"
}
```

更新行为：

- 软件启动后会静默检查更新
- 如果发现新版本，会在左侧边栏左下角版本区域显示提示
- 在“关于”窗口中可以手动检查更新并下载新版本

## 目录说明

- [main.py](d:\project\steam\main.py)
  主程序入口和全部核心逻辑
- [SteamSaveManager.spec](d:\project\steam\SteamSaveManager.spec)
  PyInstaller 打包配置
- [backups](d:\project\steam\backups)
  默认备份输出目录
- `dist`
  打包后的可执行文件输出目录

## 主要能力说明

### 存档识别

程序会综合多种线索识别存档路径：

- 内置常见游戏路径模板
- Steam `userdata` / `remote`
- `remotecache.vdf`
- `steam_autocloud.vdf`
- 安装目录和系统常见文档目录模糊搜索

### 同步模式

- 智能云存档
  游戏启动时下载、退出后上传
- 双向同步
  依据上次同步基线判断本地或云端单边变更
- 仅上传
  本地覆盖同步目录
- 仅下载
  同步目录覆盖本地

### 冲突处理

当本地和同步目录都发生变化时，程序不会盲目覆盖，而是记录冲突并弹出窗口让用户手动选择保留哪一侧。

## 注意事项

- 本项目当前主要面向 Windows 使用场景
- 不同游戏的真实存档位置可能并不一致，自动识别结果仍建议手动确认
- 如果云盘客户端正在占用文件，同步时可能会出现短暂重试
- 远程更新默认仅负责下载并启动新安装包，不会原地热替换当前 Python 脚本

## License

本项目使用 [MIT License](d:\project\steam\LICENSE)。
