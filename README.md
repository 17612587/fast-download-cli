# Fast Download CLI

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()

  Windows   Linux   macOS 通用多线程下载工具，**交互式命令行操作**，支持 HTTP/HTTPS 直链、迅雷链接、磁力链接、BT 种子文件。

---

## 特性

-   **多线程并行下载** — 最多 128 线程，实测比浏览器下载快 5~20 倍
-   **实时进度条** — Linux wget/curl 风格，显示百分比、速度、耗时、ETA
-   **智能 CDN 适配** — 自动检测带签名的 CDN 链接，智能选择单线程/多线程模式
-   **防盗链穿透** — 自动跟踪重定向链、设置 Referer、传递 Cookie
-   **断点续传** — 每个分块失败自动重试，最多 10 次
-   **可选 aria2c 引擎** — 检测到 aria2c 后可选启用，下载速度再提升 100 倍
-   **多协议支持**：
  - HTTP/HTTPS 直链
  - `thunder://` 迅雷链接（自动 Base64 解码）
  - `magnet:?` 磁力链接（调用 aria2c）
  - `.torrent` 种子文件（先下载种子再 BT）
-   **智能文件名解析** — Content-Disposition > 最终 URL 路径 > 原始 URL 路径
-   **交互式引导** — 输入链接   选择保存位置   设置线程数，所见即所得

---

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python 3.8+ | 核心运行环境 |
| requests | HTTP 库，首次运行自动提示安装 |
| aria2c (可选) | 高性能下载引擎，CDN 短链接 / BT / 磁力必备 |

### 安装依赖

```bash
pip install requests
```

### 安装 aria2c（可选，强烈推荐）

- **Windows**: 从 [aria2 releases](https://github.com/aria2/aria2/releases) 下载 `aria2-*-win-64bit-build1.zip`，解压后将 `aria2c.exe` 放到脚本同目录
- **macOS**: `brew install aria2`
- **Linux**: `apt install aria2` / `yum install aria2`

> 脚本内置了一键安装功能，首次遇到 CDN 短链接时会自动提示下载安装。

---

## 快速开始

### Windows

双击 `go_download.bat` 直接运行。

### 命令行

```bash
python fast_download_cli.py
```

### 交互流程

```
============================================================
   Fast Download CLI v3  --  多线程并行下载工具
   支持: HTTP/HTTPS | 迅雷 | 磁力 | BT 种子
============================================================

  [>] 请输入下载链接 (支持 http/thunder/magnet/.torrent):
  -> https://example.com/large_file.zip          ← 粘贴链接

  [*] 链接类型: HTTP                              ← 自动识别

  [*] 正在解析文件名...                           ← 智能获取文件名
  [>] 文件名: large_file.zip

  [>] 保存到 (回车确认，或输入新路径):
  -> 默认: C:\Users\xxx\Downloads\large_file.zip ← 回车=默认路径

  [>] 请输入线程数 (推荐 8~32，回车默认 16):
  -> 32                                           ← 输入线程数

  ------------------------------------------------------------
  URL:      https://example.com/large_file.zip
  保存到:   C:\Users\xxx\Downloads\large_file.zip
  线程数:   32
  ------------------------------------------------------------
  确认开始下载? [Y/n]:                            ← 确认

  [OK] 文件大小: 2.5 GB  |  Range 支持: 是
  ███████████████████░░░░░░░░░░░░░░  58.3%  1.46 GB/2.50 GB  32.1 MB/s  耗时 46s  ETA 33s
```

完成后显示汇总：

```
============================================================
  [+] 下载完成!
  [>] 文件: C:\Users\xxx\Downloads\large_file.zip
  [>] 大小: 2.50 GB
  [>] 耗时: 1m28s
  [>] 均速: 29.1 MB/s
============================================================
```

---

## 各类型链接用法

### 1. HTTP / HTTPS 直链

直接粘贴完整 URL，自动识别。

```
https://download.yunzhongzhuan.com/.../Win11_25H2_CJ.esd
https://example.com/files/ubuntu-24.04.iso
```

### 2. 迅雷链接 `thunder://`

脚本自动 Base64 解码为 HTTP URL 后下载。

```
thunder://QUFodHRwczovL2V4YW1wbGUuY29tL2ZpbGUuemlwWlo=
```

解码过程类似：
```
thunder://<base64>  →  Base64 解码  →  AA<URL>ZZ  →  提取 URL
```

### 3. 磁力链接 `magnet:?`

自动调用 aria2c 进行 BT 下载，无需手动操作。

```
magnet:?xt=urn:btih:abc123...&dn=filename
```

> **前提**：需要安装 aria2c

### 4. BT 种子文件 `.torrent`

先下载 `.torrent` 文件，再开始 BT 下载。

```
https://example.com/ubuntu-24.04.torrent
```

---

## aria2c 引擎说明

| 场景 | Python 模式 | aria2c 模式 |
|------|------------|-------------|
| **普通 HTTP** |    多线程，可视化进度 |    全速（C 语言原生） |
| **CDN 短链接** |    单线程，自动降级 |    全速，自动切 |
| **磁力链接** |    不支持 |    必须 |
| **BT 种子** |    不支持 |    必须 |

**默认行为**：检测到 aria2c 时，回车=用 Python 可视化模式，输入 `n`=切 aria2c 高速模式。

CDN 短链接会自动使用 aria2c（如果已安装），避免 Python requests 被 CDN 限速。

---

## 高级说明

### 防盗链处理

脚本手动跟踪 HTTP 重定向链，自动设置：
-   `Referer` 头（来源站点的域名）
-   `Cookie`（重定向过程中的会话）

这样能正确处理大多数 CDN 防盗链场景（如 cmecloud.cn、阿里云 OSS 等）。

### CDN 签名链接检测

自动识别以下特征的链接，推荐使用 aria2c：
- AWS S3 presigned URL（`x-amz-signature`）
- 阿里云 OSS（`aliyuncs.com`）
- 通用签名参数（`sign=`、`token=`、`signature=`）
- 已知限制并发的 CDN（`cmecloud.cn`）

### 断点续传

每个下载分块失败后自动重试（最多 10 次，指数退避），分块合并后才算最终完成。

---

## 项目结构

```
fast-download-cli/
  fast_download_cli.py    # 主脚本
  go_download.bat         # Windows 一键启动
  README.md               # 本文件
```

---

## FAQ

**Q: 为什么下载速度不如 IDM / 群晖 / aria2？**

Python requests 是纯 Python 实现，单线程 I/O 性能受 GIL 限制。推荐安装 aria2c 加速。

**Q: Windows 终端乱码？**

脚本已自动设置 UTF-8 代码页（`chcp 65001`），如果仍乱码，在终端属性中设置字体为 "Consolas" 或 "Microsoft YaHei Mono"。

**Q: 下载到一半中断了怎么办？**

Python 多线程模式下暂不支持中间文件续传（每次重新开始），aria2c 支持 `--continue=true` 断点续传。

**Q: 为什么 CDN 链接多线程反而更慢？**

部分 CDN（特别是带签名的短链接）限制单个 IP 的并发连接数，多线程会被限流。脚本会自动检测并降级为单线程。

---

## License

MIT License — 随意使用、修改、分发。
