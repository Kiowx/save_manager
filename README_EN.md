# Steam Save Manager

[中文](./README.md) | [English](./README_EN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D6.svg)](https://www.microsoft.com/windows/)

A Windows-focused Steam save management tool with save scanning, backup and restore, file monitoring, local cloud-folder sync, conflict handling, tray mode, and remote update support.

<img width="1624" height="1127" alt="354836e1fd549c13a5c9121764531f5b" src="https://github.com/user-attachments/assets/360a0e61-4330-44b4-bf66-c2a1fc79c0bd" />

## Features

- Automatically scans installed Steam games and tries to detect common local save locations
- Supports manually adding games, editing save paths, and importing external saves
- Supports manual backup, batch backup, restore, and backup deletion
- Supports scheduled, file-change, and game-exit backups; archives are deferred while a game is running to avoid reading saves that are still being written
- Supports syncing with local cloud folders such as OneDrive, Dropbox, and Google Drive
- Supports Smart Cloud Save mode: download on game launch and upload on game exit
- Supports bidirectional sync baselines, conflict detection, and retry queues
- Supports bilingual UI, minimize-to-tray, and double-click tray restore
- Supports remote update checks and downloading newer builds

## Environment

- Python 3.10+
- Windows

## Local Setup

### 1. Clone the repository

```bash
git clone https://github.com/Kiowx/save_manager.git
cd save_manager
```

### 2. Create a virtual environment

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

### 3. Install dependencies

```bash
pip install customtkinter pillow psutil watchdog pystray pyinstaller
```

If you only want the minimum runtime setup, install the core packages first:

```bash
pip install customtkinter pillow
```

### 4. Run the app

```bash
python main.py
```

### 5. Optional: build an exe

```bash
pyinstaller SteamSaveManager.spec
```

Default output:

- `dist/SteamSaveManager.exe`

## Dependencies

Core dependencies:

- `customtkinter`
- `Pillow`

Optional dependencies:

- `psutil`
  For more complete game-process detection
- `watchdog`
  For file-change-triggered automatic backup
- `pystray`
  For system tray support
- `PyInstaller`
  For packaging to `.exe`

Example installation:

```bash
pip install customtkinter pillow psutil watchdog pystray pyinstaller
```

## Run

Run directly:

```bash
python main.py
```

The main window starts centered on screen by default.

## Packaging

This project already includes a PyInstaller spec file: [SteamSaveManager.spec](d:\project\steam\SteamSaveManager.spec)

Example:

```bash
pyinstaller SteamSaveManager.spec
```

Default output:

- `dist/SteamSaveManager.exe`

## Remote Updates

Built-in update manifest URL:

```text
https://raw.githubusercontent.com/Kiowx/save_manager/refs/heads/main/update/update.json
```

Minimum supported JSON format:

```json
{
  "version": "1.1.0",
  "notes": "Added remote update support and fixed tray interaction",
  "url": "https://example.com/releases/SteamSaveManager-1.1.0.exe"
}
```

Recommended format:

```json
{
  "version": "1.1.0",
  "notes": "Added remote update support and fixed tray interaction",
  "url": "https://example.com/releases/SteamSaveManager-1.1.0.exe",
  "sha256": "SHA256 of the downloaded file"
}
```

Update behavior:

- The app silently checks for updates on startup
- If a newer version is found, the version area at the lower-left sidebar shows the update hint
- You can also manually check for updates from the About dialog

## Project Structure

- [main.py](d:\project\steam\main.py)
  Main entry point and core logic
- [SteamSaveManager.spec](d:\project\steam\SteamSaveManager.spec)
  PyInstaller build configuration
- [backups](d:\project\steam\backups)
  Default backup output folder
- `dist`
  Packaged executable output folder

## Core Capabilities

### Save Path Detection

The app combines multiple signals to detect save paths:

- Built-in known save path templates
- Steam `userdata` / `remote`
- `remotecache.vdf`
- `steam_autocloud.vdf`
- Fuzzy search in install folders and common Windows save locations

### Sync Modes

- Smart Cloud Save
  Download on launch, upload on exit
- Bidirectional
  Uses the last sync baseline to decide whether local or remote changed
- Upload Only
  Local save overwrites the sync folder
- Download Only
  Sync folder overwrites the local save

### Conflict Handling

When both local and sync copies changed, the app does not overwrite blindly. It records the conflict and shows a dialog so you can choose which side to keep.

## Notes

- This project is currently aimed primarily at Windows usage
- Real save locations vary by game, so auto-detected results should still be verified manually
- Sync may retry briefly if a cloud client is locking files
- Remote update currently downloads and launches a newer installer package, rather than hot-replacing the running Python script

## License

This project is licensed under the [MIT License](d:\project\steam\LICENSE).
