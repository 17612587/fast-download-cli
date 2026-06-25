# Fast Download CLI

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()

Windows / Linux / macOS 通用多线程下载工具，**交互式命令行操作**，支持 HTTP/HTTPS 直链、迅雷链接、磁力链接、BT 种子文件。

---

## 快速开始

### Windows

双击 `go_download.bat` 或 `go_download.vbs` 直接运行。
> 推荐使用 **`go_download.vbs`**，按 Ctrl+C 停止时不会弹出 CMD 确认框。

### 命令行

```bash
pip install requests
python fast_download_cli.py
```

### 交互流程

```
============================================================
   Fast Download CLI  --  多线程并行下载工具
   支持: HTTP/HTTPS | 迅雷 | 磁力 | BT 种子
============================================================

  [>] 请输入下载链接:
  -> https://example.com/large_file.zip

  [>] 文件名: large_file.zip

  [>] 保存到 (回车确认，或输入新路径):
  -> C:\Users\xxx\Downloads\large_file.zip

  [>] 请输入线程数 (推荐 8~32，回车默认 16):
  -> 32

  确认开始下载? [Y/n]:

  [OK] 文件大小: 2.5 GB
  ███████████████████░░░░░░░░░░░░░░  58.3%  32.1 MB/s  耗时 46s  ETA 33s
```

---

## 支持链接类型

| 类型 | 示例 | 说明 |
|------|------|------|
| HTTP/HTTPS | `https://example.com/file.zip` | 普通直链 |
| 迅雷 | `thunder://QUFodHRw...` | 自动 Base64 解码 |
| 磁力 | `magnet:?xt=urn:btih:...` | 调用 aria2c |
| BT 种子 | `https://example.com/file.torrent` | 先下载种子再 BT |

---

## 特性

- **多线程并行下载** — 用户指定线程数，使用 HTTP Range 分块并发
- **断点续传** — 每个分块失败自动重试（最多 10 次，指数退避）
- **尊重用户选择** — 线程数完全由用户决定，aria2c 是否使用由用户选择
- **Ctrl+C 安全停止** — 随时中断，自动清理 .part 碎片文件
- **防盗链处理** — 自动跟随重定向、设置 Referer、传递 Cookie
- **智能文件名解析** — Content-Disposition > 最终 URL 路径 > 原始 URL 路径

---

## aria2c（可选）

磁力链接和 BT 种子必须使用 aria2c。HTTP 下载检测到 aria2c 时会询问用户是否启用。

- **Windows**: 下载 [aria2 releases](https://github.com/aria2/aria2/releases)，将 `aria2c.exe` 放到脚本同目录
- **macOS**: `brew install aria2`
- **Linux**: `apt install aria2`

---

## 项目结构

```
fast-download-cli/
  fast_download_cli.py    # 主脚本
  go_download.bat         # Windows 一键启动
  go_download.vbs         # Windows 一键启动（推荐，Ctrl+C 不弹确认框）
  README.md               # 本文件
```

---

## License

MIT License
