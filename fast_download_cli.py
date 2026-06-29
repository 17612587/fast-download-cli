#!/usr/bin/env python3
"""
Fast Download CLI  v3
  多线程 HTTP 下载 + 迅雷/BT/磁力链接支持
  总进度 + 每线程独立进度，Linux wget 风格
"""

import os
import re
import sys
import time
import ctypes
import base64
import shutil
import signal
import subprocess
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import urlparse, unquote, parse_qs

# ── 启用 Windows ANSI 转义序列 ────────────────────────
if sys.platform == "win32":
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

# ── 配置 ──────────────────────────────────────────────
CHUNK_SIZE = 1024 * 1024
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
MAX_RETRIES = 10
ARIA2C_URL = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"

# ── 共享 HTTP Session（大连接池，避免多线程竞争）──
from requests.adapters import HTTPAdapter

_shared_session = None
_session_lock = threading.Lock()

def _get_shared_session() -> requests.Session:
    global _shared_session
    if _shared_session is None:
        with _session_lock:
            if _shared_session is None:
                s = requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=64,
                    pool_maxsize=64,
                    pool_block=False,
                )
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                _shared_session = s
    return _shared_session

# ── 全局状态 ──────────────────────────────────────────
progress_lock = threading.Lock()
total_downloaded = 0
start_time = time.time()
done_flag = False
SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
global_referer = ""  # 全局 Referer，用于防盗链 CDN
global_cookies = {}  # 全局 Cookie，用于重定向链认证
global_download_url = ""  # 解析重定向后的最终 URL
global_part_files = []  # 多线程下载的碎片文件列表（用于 Ctrl+C 清理）
stop_event = threading.Event()  # Ctrl+C 停止信号
thread_progress = {}  # 每线程下载量: {thread_idx: bytes}
thread_speed = {}  # 每线程瞬时速度: {thread_idx: bytes_per_sec}


# ═══════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════

def format_size(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 ** 2):.1f} MB"
    else:
        return f"{n / (1024 ** 3):.2f} GB"


def format_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m:.0f}m{s:.0f}s"
    else:
        h, r = divmod(seconds, 3600)
        m = r // 60
        return f"{h:.0f}h{m:.0f}m"


def cleanup_part_files():
    """删除所有多线程下载产生的碎片文件"""
    global global_part_files
    remaining = list(global_part_files)
    global_part_files.clear()

    # 多轮重试，确保文件句柄全部释放
    for attempt in range(20):
        if not remaining:
            break
        still_there = []
        for pf in remaining:
            try:
                if os.path.exists(pf):
                    os.remove(pf)
            except (PermissionError, OSError):
                still_there.append(pf)
            except Exception:
                pass
        remaining = still_there
        if remaining and attempt < 19:
            time.sleep(0.5)  # 等线程释放文件句柄

    # 最后兜底：打印未删除的文件
    if remaining:
        for pf in remaining:
            if os.path.exists(pf):
                print(f"\n  [!] 无法删除碎片文件: {pf} (可能被其他程序占用)")


def stop_handler(*args):
    """设置停止标志，让各线程自然退出"""
    global stop_event, done_flag
    if not stop_event.is_set():
        stop_event.set()
        done_flag = True
        print("\n  [*] 停止信号已发出，等待线程退出...")


# Windows 上使用 SetConsoleCtrlHandler 处理 Ctrl+C，比 signal 更可靠
if sys.platform == "win32":
    _WIN_CTRL_C_EVENT = 0
    _PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    _handler = _PHANDLER_ROUTINE(lambda e: (stop_handler(), True)[1])
    if not kernel32.SetConsoleCtrlHandler(_handler, True):
        pass  # 注册失败则回退到 signal
else:
    signal.signal(signal.SIGINT, lambda sig, frame: stop_handler())


# ═══════════════════════════════════════════════════════
# 链接类型检测 & 解码
# ═══════════════════════════════════════════════════════

class LinkType:
    HTTP = "http"
    THUNDER = "thunder"
    MAGNET = "magnet"
    TORRENT = "torrent"


def detect_link_type(url: str) -> str:
    """识别链接类型"""
    u = url.strip().lower()
    if u.startswith("thunder://"):
        return LinkType.THUNDER
    if u.startswith("magnet:?"):
        return LinkType.MAGNET
    if u.endswith(".torrent") or "/torrent/" in u.lower():
        return LinkType.TORRENT
    if u.startswith("http://") or u.startswith("https://"):
        return LinkType.HTTP
    return LinkType.HTTP


def decode_thunder(thunder_url: str) -> str:
    """
    解码迅雷链接 thunder://<base64>
    解码后格式: AA<真实URL>ZZ
    """
    if not thunder_url.startswith("thunder://"):
        return thunder_url
    encoded = thunder_url[10:]
    try:
        padding = 4 - len(encoded) % 4
        if padding != 4:
            encoded += "=" * padding
        decoded = base64.b64decode(encoded)
        for enc in ["utf-8", "gbk", "gb2312"]:
            try:
                text = decoded.decode(enc)
                if text.startswith("AA") and text.endswith("ZZ"):
                    return text[2:-2]
            except (UnicodeDecodeError, ValueError):
                continue
    except Exception:
        pass
    return thunder_url


# ═══════════════════════════════════════════════════════
# 智能文件名解析
# ═══════════════════════════════════════════════════════

def parse_content_disposition(cd: str) -> str:
    """
    解析 Content-Disposition 头中的文件名
    支持: filename*=UTF-8''xxx  /  filename="xxx"  /  filename=xxx
    """
    if not cd:
        return None
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1))
    m = re.search(r'filename="([^"]+)"', cd, re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename=([^;]+)", cd, re.I)
    if m:
        val = m.group(1).strip().strip('"')
        return val
    return None


def extract_filename_from_url(url: str) -> str:
    """从 URL 路径或 query 参数中尝试提取文件名"""
    parsed = urlparse(url)
    # 1. 先尝试 query 参数里的 filename / file_name / name 等
    qs = parse_qs(parsed.query)
    for key in ("filename", "file_name", "name", "fname", "file"):
        vals = qs.get(key)
        if vals:
            candidate = unquote(vals[0]).strip()
            if candidate and "." in candidate:
                return candidate
    # 2. 再从路径中提取
    name = unquote(parsed.path.rstrip("/").split("/")[-1])
    if name and "." in name:
        return name
    return None


def resolve_filename(url: str, resp=None) -> tuple:
    """
    智能解析文件名
    优先级: Content-Disposition > 最终 URL 路径 > 原始 URL 路径
    返回: (filename_or_none, response_or_none)
    """
    if resp is not None:
        cd = resp.headers.get("Content-Disposition", "")
        fname = parse_content_disposition(cd)
        if fname:
            return fname, resp
        fname = extract_filename_from_url(resp.url)
        if fname:
            return fname, resp

    fname = extract_filename_from_url(url)
    if fname:
        return fname, resp

    return None, resp


# ═══════════════════════════════════════════════════════
# aria2c 管理
# ═══════════════════════════════════════════════════════

def find_aria2c() -> str:
    """查找 aria2c 可执行文件"""
    path = shutil.which("aria2c")
    if path:
        return path

    script_dir = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(script_dir, "aria2c.exe")
    if os.path.exists(local):
        return local

    common = [
        os.path.join(os.path.expanduser("~"), "scoop", "shims", "aria2c.exe"),
        r"C:\Program Files\aria2\aria2c.exe",
        r"C:\aria2\aria2c.exe",
    ]
    for p in common:
        if os.path.exists(p):
            return p

    return None


def install_aria2c_interactive() -> str:
    """交互式安装 aria2c"""
    script_dir = os.path.dirname(os.path.abspath(__file__))

    print("\n  [*] 未找到 aria2c，它是高性能多线程下载工具 (6MB)")
    print("  [>] 安装后 HTTP 下载速度可大幅提升")
    choice = input("  [>] 是否自动下载安装? [Y/n]: ").strip().lower()

    if choice and choice not in ("y", "yes"):
        print("  [!] 跳过安装")
        return None

    import zipfile
    import io

    print("  [*] 正在从 GitHub 下载 aria2c ...")
    try:
        resp = requests.get(ARIA2C_URL, stream=True, timeout=120,
                            headers={"User-Agent": "FastDownload/1.0"})
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        data = io.BytesIO()
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                data.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    sys.stdout.write(f"\r  [*] 下载 aria2c: {format_size(downloaded)}"
                                     f"/{format_size(total)} ({pct:.0f}%)")
                    sys.stdout.flush()
        print()

        print("  [*] 解压中...")
        data.seek(0)
        with zipfile.ZipFile(data) as zf:
            aria2_exe = None
            for name in zf.namelist():
                if name.endswith("aria2c.exe"):
                    aria2_exe = name
                    break
            if not aria2_exe:
                print("  [!] 压缩包中未找到 aria2c.exe")
                return None
            zf.extract(aria2_exe, script_dir)
            extracted = os.path.join(script_dir, aria2_exe)
            target = os.path.join(script_dir, "aria2c.exe")
            if extracted != target:
                shutil.move(extracted, target)
                parent = os.path.dirname(extracted)
                try:
                    while parent != script_dir:
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                except OSError:
                    pass

        print(f"  [OK] aria2c 已安装到: {target}")
        return target

    except Exception as e:
        print(f"\n  [!] 自动安装失败: {e}")
        print("  [>] 请手动下载: https://github.com/aria2/aria2/releases")
        print("  [>] 将 aria2c.exe 放到与本脚本相同的目录即可")
        return None


def download_via_aria2(url: str, dest: str):
    """使用 aria2c 下载 (磁力/BT/种子文件)"""
    aria2 = find_aria2c()
    if not aria2:
        aria2 = install_aria2c_interactive()
    if not aria2:
        print("  [!] aria2c 不可用，无法处理此链接")
        return False

    print(f"\n  [*] 使用 aria2c 下载...")
    print(f"  [>] 目标: {dest}")
    print()

    dest_dir = os.path.dirname(dest) or "."
    dest_name = os.path.basename(dest)

    cmd = [
        aria2,
        "--seed-time=0",
        "--file-allocation=none",
        "--console-log-level=notice",
        "--summary-interval=0",
        f"--dir={dest_dir}",
        f"--out={dest_name}",
        url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for line in proc.stdout:
            sys.stdout.write("  " + line.rstrip() + "\r")
            sys.stdout.flush()
        proc.wait()
        print()
        return proc.returncode == 0 and os.path.exists(dest)
    except KeyboardInterrupt:
        proc.terminate()
        print("\n  [*] 用户中断")
        return False
    except FileNotFoundError:
        print(f"  [!] 无法找到 aria2c: {aria2}")
        return False


def download_http_via_aria2(url: str, dest: str, num_connections: int = 16):
    """使用 aria2c 进行 HTTP 多连接下载"""
    aria2 = find_aria2c()
    if not aria2:
        aria2 = install_aria2c_interactive()
    if not aria2:
        return False

    print(f"\n  [*] 使用 aria2c (x{num_connections}) 下载...")
    print(f"  [>] 目标: {dest}")
    print()

    dest_dir = os.path.dirname(dest) or "."
    dest_name = os.path.basename(dest)

    cmd = [
        aria2,
        f"--split={num_connections}",
        f"--max-connection-per-server={num_connections}",
        "--min-split-size=1M",
        "--file-allocation=none",
        "--continue=true",
        "--console-log-level=notice",
        "--summary-interval=0",
        f"--dir={dest_dir}",
        f"--out={dest_name}",
        url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for line in proc.stdout:
            sys.stdout.write("  " + line.rstrip() + "\r")
            sys.stdout.flush()
        proc.wait()
        print()
        success = proc.returncode == 0 and os.path.exists(dest)
        if success:
            actual_size = os.path.getsize(dest)
            print(f"  [OK] 下载完成! {format_size(actual_size)}")
            print(f"  [>] 保存至: {dest}")
        return success
    except KeyboardInterrupt:
        proc.terminate()
        print("\n  [*] 已中断（可用相同命令续传）")
        return False


# ═══════════════════════════════════════════════════════
# HTTP 下载核心
# ═══════════════════════════════════════════════════════

def resolve_url_with_referer(url: str) -> tuple:
    """
    手动跟随重定向，记录各跳转 Host 用作 Referer + 保存 Cookie
    返回: (最终URL, referer_chain, final_headers, cookies)
    """
    session = requests.Session()
    referer = ""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    }
    current_url = url
    visited = set()
    max_hops = 10

    for _ in range(max_hops):
        parsed = urlparse(current_url)
        visited.add(current_url)

        req_headers = dict(headers)
        if referer:
            req_headers["Referer"] = referer

        resp = session.head(current_url, allow_redirects=False,
                            timeout=30, headers=req_headers)
        location = resp.headers.get("Location", "")

        if not location or resp.status_code not in (301, 302, 303, 307, 308):
            cookies = session.cookies.get_dict()
            return current_url, referer, dict(resp.headers), cookies

        referer = current_url  # 使用完整 URL（含路径），xiaoguanqiu.com 等需要校验此 Referer

        if location.startswith("/"):
            location = f"{parsed.scheme}://{parsed.netloc}{location}"
        elif not location.startswith("http"):
            base = f"{parsed.scheme}://{parsed.netloc}"
            path = parsed.path.rpartition("/")[0]
            location = f"{base}{path}/{location}"

        if location in visited:
            break
        current_url = location

    cookies = session.cookies.get_dict()
    return current_url, referer, {}, cookies


def get_file_info(url: str) -> tuple:
    """获取文件大小、Range 支持（使用已有的 global_referer / global_cookies）"""
    global global_referer, global_cookies
    print("\n  [*] 正在获取文件信息...", end="", flush=True)

    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    }
    if global_referer:
        req_headers["Referer"] = global_referer

    def _is_error(resp_obj, clen, ctype):
        if resp_obj.status_code >= 400:
            return True
        if clen > 0 and clen < 10000 and ("xml" in ctype.lower() or
                                           "html" in ctype.lower()):
            return True
        return False

    try:
        probe_headers = dict(req_headers)
        probe_headers["Range"] = "bytes=0-0"
        resp = requests.get(url, timeout=30, headers=probe_headers,
                            cookies=global_cookies, stream=True)
        resp.close()

        size = 0
        supports_range = False
        content_type = resp.headers.get("Content-Type", "")

        cr = resp.headers.get("Content-Range", "")
        m = re.search(r"/(\d+)", cr)
        if m:
            size = int(m.group(1))
            supports_range = True
        else:
            cl = resp.headers.get("Content-Length", "")
            if cl:
                size = int(cl)

        if _is_error(resp, size, content_type):
            print(f"\r  [!] 服务器返回错误: HTTP {resp.status_code}, {content_type}, "
                  f"{size} 字节")
            print(f"  [!] 可能原因: 防盗链/链接过期/需要登录")
            try:
                body_resp = requests.get(url, timeout=30, headers=req_headers,
                                         cookies=global_cookies)
                body_text = body_resp.text[:1000]
                print(f"  [DEBUG] 响应内容: {body_text}")
            except Exception:
                pass
            return 0, False, resp

        if size == 0:
            try:
                resp_head = requests.head(url, allow_redirects=False, timeout=30,
                                          headers=req_headers,
                                          cookies=global_cookies)
                size = int(resp_head.headers.get("Content-Length", 0))
            except Exception:
                pass

        if size == 0:
            print("\r  [!] 无法获取文件大小，将使用流式下载")
            return 0, False, resp

        print(f"\r  [OK] 文件大小: {format_size(size)}  |  "
              f"Range 支持: {'是' if supports_range else '否'}")
        return size, supports_range, resp
    except Exception as e:
        print(f"\r  [!] 获取文件信息失败: {e}")
        return 0, False, None


def download_chunk(idx: int, url: str, start: int, end: int, part_file: str):
    """下载一个分块，支持断点续传"""
    global total_downloaded, global_referer, global_cookies, stop_event, thread_progress
    current = start
    retries = 0

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    }
    if global_referer:
        base_headers["Referer"] = global_referer

    session = _get_shared_session()

    while retries < MAX_RETRIES and not stop_event.is_set():
        try:
            headers = dict(base_headers)
            headers["Range"] = f"bytes={current}-{end}"
            resp = session.get(url, headers=headers, stream=True,
                               cookies=global_cookies,
                               timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if resp.status_code not in (200, 206):
                if resp.status_code in (403, 404, 410):
                    print(f"\n  [!] 线程 #{idx+1} HTTP {resp.status_code}: 链接无效或防盗链")
                    resp.close()
                    return 0
                resp.close()
                retries += 1
                time.sleep(min(2 ** retries, 30))
                continue

            with open(part_file, "r+b") as f:
                f.seek(current - start)
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if stop_event.is_set():
                        resp.close()
                        return current - start
                    if chunk:
                        f.write(chunk)
                        current += len(chunk)
                        with progress_lock:
                            total_downloaded += len(chunk)
                            thread_progress[idx] = current - start
            resp.close()
            return current - start
        except Exception:
            if stop_event.is_set():
                return current - start
            retries += 1
            time.sleep(min(2 ** retries, 30))
    return current - start


def single_thread_download(url: str, dest: str, total_size: int):
    """单线程下载"""
    global total_downloaded, start_time, done_flag, global_referer, global_cookies
    start_time = time.time()
    downloaded = 0

    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    }
    if global_referer:
        req_headers["Referer"] = global_referer

    resp = requests.get(url, stream=True,
                        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                        headers=req_headers,
                        cookies=global_cookies)
    ct = resp.headers.get("Content-Type", "")
    cl = resp.headers.get("Content-Length", "")
    if resp.status_code == 403 or ("xml" in ct.lower() and cl and int(cl) < 10000):
        print(f"\n  [!] 服务器返回 HTTP {resp.status_code}，可能是防盗链或链接失效")
        done_flag = True
        return

    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                with progress_lock:
                    total_downloaded = downloaded
    done_flag = True


# ═══════════════════════════════════════════════════════
# 进度渲染（多行 ANSI）
# ═══════════════════════════════════════════════════════

def progress_renderer(total_size: int, num_threads: int):
    """多线程多行进度渲染：每线程独立进度+实时速度 + 总进度"""
    global total_downloaded, done_flag, thread_progress, thread_speed

    while total_downloaded == 0 and not done_flag:
        time.sleep(0.5)

    last_total = 0
    last_time = start_time
    speed_samples = []

    # 每线程上一轮数据（用于计算速度）
    thread_last = {i: 0 for i in range(num_threads)}
    thread_last_time = {i: start_time for i in range(num_threads)}

    print()  # 留空行供多行渲染

    while not done_flag:
        now = time.time()
        dt = now - last_time
        dd = total_downloaded - last_total
        if dt > 0:
            speed_samples.append(dd / dt)
            if len(speed_samples) > 10:
                speed_samples.pop(0)
        avg_speed = sum(speed_samples) / len(speed_samples) if speed_samples else 0

        chunk_sz = total_size // num_threads if total_size > 0 else 0

        # 快照
        with progress_lock:
            tp_snapshot = dict(thread_progress)

        lines = []

        # 计算每个线程的即时速度
        for i in range(num_threads):
            downloaded = tp_snapshot.get(i, 0)
            thread_total = chunk_sz if i < num_threads - 1 else (total_size - (num_threads - 1) * chunk_sz)

            # 计算该线程速度
            t_dd = downloaded - thread_last[i]
            t_dt = now - thread_last_time[i]
            if t_dt > 0:
                t_speed = t_dd / t_dt
            else:
                t_speed = 0
            thread_speed[i] = t_speed

            thread_last[i] = downloaded
            thread_last_time[i] = now

            if thread_total > 0:
                pct = downloaded / thread_total * 100
                bar_w = 20
                filled = int(bar_w * pct / 100)
                bar = "\u2588" * filled + "\u2591" * (bar_w - filled)
                lines.append(f"  线程 #{i+1:2d}  {bar}  {pct:5.1f}%  "
                             f"{format_size(downloaded)}/{format_size(thread_total)}  "
                             f"{format_size(t_speed)}/s")
            else:
                lines.append(f"  线程 #{i+1:2d}  {format_size(downloaded)}  (待开始)")

        # 总进度
        if total_size > 0:
            pct = total_downloaded / total_size * 100
            bar_w = 35
            filled = int(bar_w * total_downloaded / total_size)
            bar = "\u2588" * filled + "\u2591" * (bar_w - filled)
            eta_str = format_time((total_size - total_downloaded) / avg_speed) if avg_speed > 0 else "--:--"
            total_line = (f"  总计     {bar}  {pct:5.1f}%  "
                          f"{format_size(total_downloaded)}/{format_size(total_size)}  "
                          f"{format_size(avg_speed)}/s  ETA {eta_str}")
        else:
            total_line = f"  总计     {format_size(total_downloaded)}"

        lines.append(total_line)

        # Ctrl+C 提示
        lines.append("  \033[90m按 Ctrl+C 停止下载\033[0m")

        # ANSI 渲染
        total_lines = len(lines)
        sys.stdout.write(f"\033[{total_lines}A")
        for line in lines:
            sys.stdout.write("\033[K" + line + "\n")
        sys.stdout.flush()

        last_total = total_downloaded
        last_time = now
        time.sleep(0.3)

    # 最终帧
    chunk_sz_end = total_size // num_threads if total_size > 0 else 0
    lines_end = []
    for i in range(num_threads):
        thread_total_end = chunk_sz_end if i < num_threads - 1 else (total_size - (num_threads - 1) * chunk_sz_end)
        bar = "\u2588" * 20
        lines_end.append(f"  线程 #{i+1:2d}  {bar}  100.0%  "
                        f"{format_size(thread_total_end)}/{format_size(thread_total_end)}")

    elapsed = time.time() - start_time
    bar_final = "\u2588" * 35
    lines_end.append(f"  总计     {bar_final}  100.0%  "
                     f"{format_size(total_size)}/{format_size(total_size)}  耗时 {format_time(elapsed)}")

    tl = len(lines_end)
    sys.stdout.write(f"\033[{tl}A")
    for line in lines_end:
        sys.stdout.write("\033[K" + line + "\n")
    sys.stdout.flush()


def progress_renderer_single(total_size: int):
    """单线程进度渲染"""
    global total_downloaded, done_flag
    while total_downloaded == 0 and not done_flag:
        time.sleep(0.5)

    last_downloaded = 0
    last_time = start_time
    speed_samples = []

    while not done_flag:
        now = time.time()
        elapsed = now - start_time
        dt = now - last_time
        dd = total_downloaded - last_downloaded
        if dt > 0:
            speed_samples.append(dd / dt)
            if len(speed_samples) > 10:
                speed_samples.pop(0)
        avg_speed = sum(speed_samples) / len(speed_samples) if speed_samples else 0

        if total_size > 0 and total_downloaded > 0:
            pct = total_downloaded / total_size * 100
            bar_w = 30
            filled = int(bar_w * total_downloaded / total_size)
            bar = "\u2588" * filled + "\u2591" * (bar_w - filled)
            eta_str = format_time((total_size - total_downloaded) / avg_speed) if avg_speed > 0 else "--:--"
            line = (f"\r  {bar}  {pct:5.1f}%  "
                    f"{format_size(total_downloaded)}/{format_size(total_size)}  "
                    f"{format_size(avg_speed)}/s  "
                    f"耗时 {format_time(elapsed)}  ETA {eta_str}")
        else:
            avg_speed = total_downloaded / elapsed if elapsed > 0 else 0
            line = (f"\r  {format_size(total_downloaded)}  已用时 "
                    f"{format_time(elapsed)}  {format_size(avg_speed)}/s")

        sys.stdout.write(line + "  \033[90mCtrl+C 停止\033[0m")
        sys.stdout.flush()
        last_downloaded = total_downloaded
        last_time = now
        time.sleep(0.25)

    elapsed = time.time() - start_time
    if total_size > 0:
        line = (f"\r  {'\u2588' * 30}  100.0%  "
                f"{format_size(total_size)}/{format_size(total_size)}  "
                f"耗时 {format_time(elapsed)}  ")
    else:
        line = f"\r  {format_size(total_downloaded)}  "
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# ═══════════════════════════════════════════════════════
# 下载流程
# ═══════════════════════════════════════════════════════

def run_http_download(url: str, dest: str, num_threads: int):
    """HTTP(S) 下载入口"""
    global total_downloaded, start_time, done_flag
    global global_referer, global_cookies, global_download_url
    global global_part_files
    total_downloaded = 0
    start_time = time.time()
    done_flag = False
    global_referer = ""
    global_cookies = {}
    global_download_url = ""
    global_part_files = []

    # 1. 手动跟随重定向，获取最终 CDN URL、Referer、Cookie
    final_url, referer, _, cookies = resolve_url_with_referer(url)
    global_referer = referer
    global_cookies = cookies
    global_download_url = final_url

    if final_url != url:
        print(f"\n  [*] 重定向到: {final_url[:100]}{'...' if len(final_url) > 100 else ''}")

    # 2. 获取文件信息
    total_size, supports_range, resp = get_file_info(final_url)

    if total_size == 0:
        if final_url != url:
            print("\n  [*] 直接请求失败，尝试通过原始链接流式下载...")
            _download_via_redirect_stream(url, dest)
        else:
            print("  [*] 文件信息获取失败，尝试直接流式下载...")
            _do_single_download(final_url, dest, 0)
        return

    # ── 下载器选择（用户决定） ─────────────────
    use_aria2 = False
    aria2 = find_aria2c()

    if aria2:
        # 检测到 aria2c，询问用户是否使用
        print(f"\n  [>] 检测到 aria2c")
        choice = input("  [>] 是否使用 aria2c 高速下载? [y/N]: ").strip().lower()
        use_aria2 = choice in ("y", "yes")
    elif total_size > 100 * 1024 * 1024:
        # 大文件且没有 aria2c，提示安装
        print(f"\n  [*] 文件较大 ({format_size(total_size)})，推荐使用 aria2c 加速")
        choice = input("  [>] 是否安装 aria2c? [y/N]: ").strip().lower()
        if choice in ("y", "yes"):
            aria2 = install_aria2c_interactive()
            use_aria2 = bool(aria2)

    if use_aria2 and aria2:
        success = download_http_via_aria2(url, dest, num_threads)
        if success:
            return
        print("  [!] aria2c 下载失败，回退到 Python 下载\n")

    # ── Python 多线程 / 单线程 ────────────────
    if supports_range and num_threads > 1 and total_size > 0:
        print(f"  [*] 使用 {num_threads} 线程并行下载\n")
        _multi_thread_download(final_url, dest, total_size, num_threads)
    else:
        if num_threads > 1 and total_size > 0 and not supports_range:
            print(f"  [!] 服务器不支持 Range 请求，无法使用 {num_threads} 线程")
            print(f"  [>] 将使用单线程下载")
        print("  [*] 使用单线程下载\n")
        _do_single_download(final_url, dest, total_size)


def _download_via_redirect_stream(url: str, dest: str):
    """
    通过原始链接自然重定向下载
    最接近 curl -L / 浏览器的行为
    """
    global total_downloaded, start_time, done_flag

    print(f"  [*] 通过原始链接流式下载: {url[:80]}...")
    total_downloaded = 0
    start_time = time.time()
    done_flag = False

    try:
        session = requests.Session()
        resp = session.get(url, stream=True, allow_redirects=True,
                           timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                           headers={
                               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; "
                                             "Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) "
                                             "Chrome/120.0.0.0 Safari/537.36",
                           })

        if resp.status_code >= 400:
            ct = resp.headers.get("Content-Type", "")
            try:
                body = resp.text[:1000]
            except Exception:
                body = ""
            print(f"\n  [!] 流式下载失败: HTTP {resp.status_code}, "
                  f"{ct}, 响应: {body}")
            return

        total_size = int(resp.headers.get("Content-Length", 0))
        if total_size > 0:
            print(f"  [OK] 文件大小: {format_size(total_size)}")
        else:
            print("  [*] 未知文件大小，正在下载...")

        printer = threading.Thread(target=progress_renderer_single,
                                    args=(total_size,), daemon=True)
        printer.start()

        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    total_downloaded = downloaded

        done_flag = True
        printer.join(timeout=2)

        if total_size > 0 and downloaded != total_size:
            print(f"\n  [!] 警告：下载大小 ({format_size(downloaded)}) "
                  f"与预期 ({format_size(total_size)}) 不一致")
        else:
            print(f"\n  [OK] 下载完成: {format_size(downloaded)}")

    except Exception as e:
        print(f"\n  [!] 流式下载失败: {e}")


def _do_single_download(url: str, dest: str, total_size: int):
    global done_flag
    printer = threading.Thread(target=progress_renderer_single,
                                args=(total_size,), daemon=True)
    printer.start()
    single_thread_download(url, dest, total_size)
    done_flag = True
    printer.join(timeout=2)

    if total_size > 0:
        actual = os.path.getsize(dest)
        if actual != total_size:
            print(f"\n  [!] 警告：下载大小 ({format_size(actual)}) "
                  f"与预期 ({format_size(total_size)}) 不一致")


def _multi_thread_download(url: str, dest: str, total_size: int,
                            num_threads: int):
    global done_flag, global_part_files, thread_progress

    # 初始化每线程进度
    thread_progress = {i: 0 for i in range(num_threads)}

    chunk_sz = total_size // num_threads
    ranges_data = []
    for i in range(num_threads):
        start = i * chunk_sz
        end = total_size - 1 if i == num_threads - 1 else (start + chunk_sz - 1)
        ranges_data.append((start, end, i))

    part_files = []
    for start, end, idx in ranges_data:
        pf = f"{dest}.part{idx}"
        part_files.append(pf)
        if os.path.exists(pf):
            os.remove(pf)
        with open(pf, "wb") as f:
            f.truncate(end - start + 1)

    global_part_files = part_files[:]

    printer = threading.Thread(target=progress_renderer,
                                args=(total_size, num_threads),
                                daemon=True)
    printer.start()

    executor = None
    futures = []
    try:
        executor = ThreadPoolExecutor(max_workers=num_threads)
        for start, end, idx in ranges_data:
            future = executor.submit(download_chunk, idx, url, start, end,
                                     part_files[idx])
            futures.append(future)

        # 使用 wait/FIRST_COMPLETED 轮询，每 0.5s 检查一次停止标志
        pending = set(futures)
        while pending and not stop_event.is_set():
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    future.result()
                except Exception as e:
                    print(f"\n  [!] 某一线程出错: {e}")

    except KeyboardInterrupt:
        stop_event.set()
        print("\n\n  [*] 收到中断信号，正在停止所有线程...")
    finally:
        if executor:
            executor.shutdown(wait=False)
            # 等待所有线程完全退出（最多等 5 秒）
            for _ in range(50):
                if all(f.done() for f in futures):
                    break
                time.sleep(0.1)

    done_flag = True
    printer.join(timeout=2)

    if stop_event.is_set():
        print("\n  [*] 正在清理碎片文件...")
        time.sleep(1.0)  # 给 OS 一点时间释放文件句柄
        cleanup_part_files()
        print("  [*] 碎片文件已清理")
        return

    print("\n  [*] 正在合并分块...")
    with open(dest, "wb") as out:
        for i, pf in enumerate(part_files):
            with open(pf, "rb") as f:
                while True:
                    data = f.read(CHUNK_SIZE)
                    if not data:
                        break
                    out.write(data)
            os.remove(pf)
    global_part_files.clear()
    print("  [OK] 合并完成")


# ═══════════════════════════════════════════════════════
# 交互式入口
# ═══════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("   Fast Download CLI v3  --  多线程并行下载工具")
    print("   支持: HTTP/HTTPS | 迅雷 | 磁力 | BT 种子")
    print("=" * 60)

    # ── 步骤 1：输入链接 ──
    while True:
        print("\n  [>] 请输入下载链接 (支持 http/thunder/magnet/.torrent):")
        url = input("  -> ").strip()
        if not url:
            print("  [!] 链接不能为空，请重新输入")
            continue
        break

    link_type = detect_link_type(url)
    print(f"  [*] 链接类型: {link_type.upper()}")

    # ── 迅雷链接 → 解码为 HTTP ──
    if link_type == LinkType.THUNDER:
        original = url
        url = decode_thunder(url)
        if url == original or not url.startswith("http"):
            print(f"  [!] 无法解码迅雷链接: {original[:60]}...")
            return
        print(f"  [*] 迅雷解码 -> HTTP")
        print(f"  [>] {url[:80]}{'...' if len(url) > 80 else ''}")
        link_type = LinkType.HTTP

    # ── 种子文件链接 → 先下载 .torrent → 再 aria2c ──
    if link_type == LinkType.TORRENT:
        print("  [*] 这是一个 BT 种子文件链接")
        filename = extract_filename_from_url(url) or "download.torrent"
        default_save = os.path.join(SCRIPT_DIR, filename)
        print(f"\n  [>] 保存到 (回车确认，或输入新路径):")
        print(f"  -> 默认: {default_save}")
        user_path = input("  -> ").strip()
        dest = user_path if user_path else default_save

        print("\n  [*] 先下载种子文件...")
        resp = requests.get(url, stream=True,
                            headers={"User-Agent": "FastDownload/1.0"})
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(65536):
                if chunk:
                    f.write(chunk)
        print(f"  [OK] 种子已保存: {dest}")

        success = download_via_aria2(dest, dest.replace(".torrent", ""))
        if not success:
            print("  [!] BT 下载未完成")
        input("\n  按 Enter 退出...")
        return

    # ── 磁力链接 → aria2c ──
    if link_type == LinkType.MAGNET:
        print("  [*] 这是一个磁力链接，将使用 aria2c 下载")
        m = re.search(r"dn=([^&]+)", url, re.I)
        magnet_name = unquote(m.group(1)) if m else "magnet_download"

        default_save = os.path.join(SCRIPT_DIR, magnet_name)
        print(f"\n  [>] 保存到 (回车确认，或输入新路径):")
        print(f"  -> 默认: {default_save}")
        user_path = input("  -> ").strip()
        dest = user_path if user_path else default_save

        print("\n" + "-" * 60)
        print(f"  类型:     磁力链接 (BT)")
        print(f"  InfoHash: {url[20:60]}...")
        print(f"  名称:     {magnet_name}")
        print(f"  保存到:   {dest}")
        print("-" * 60)
        confirm = input("\n  确认开始下载? [Y/n]: ").strip().lower()
        if confirm and confirm not in ("y", "yes"):
            print("  已取消")
            return

        success = download_via_aria2(url, dest)
        if success:
            actual = dest
            if not os.path.exists(dest):
                dest_dir = os.path.dirname(dest) or "."
                files = sorted(
                    [f for f in os.listdir(dest_dir)
                     if os.path.getsize(os.path.join(dest_dir, f)) > 0],
                    key=lambda f: os.path.getmtime(os.path.join(dest_dir, f)),
                    reverse=True,
                )
                for f in files:
                    if magnet_name[:10] in f or f.endswith(".aria2"):
                        continue
                    actual = os.path.join(dest_dir, f)
                    break
            print(f"\n{'=' * 60}")
            print(f"  [+] 下载完成!")
            print(f"  [>] 文件: {actual}")
            if os.path.exists(actual):
                print(f"  [>] 大小: {format_size(os.path.getsize(actual))}")
            print(f"{'=' * 60}")
        else:
            print(f"\n  [!] 下载未完成或失败")
        input("\n  按 Enter 退出...")
        return

    # ── HTTP 下载流程 ──

    # 步骤 2：智能解析文件名
    print("\n  [*] 正在解析文件名...", end="", flush=True)
    try:
        final_url, referer, _, _cookies = resolve_url_with_referer(url)
        resp = requests.head(final_url, allow_redirects=False, timeout=30,
                             headers={
                                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                                               "Chrome/120.0.0.0 Safari/537.36",
                                 "Referer": referer,
                             } if referer else {
                                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                                               "Chrome/120.0.0.0 Safari/537.36",
                             })
    except Exception:
        resp = None
    filename, _ = resolve_filename(url, resp)

    # 两种情况需要 GET Range 探测真实 Content-Disposition：
    # 1. 文件名是脚本扩展名（index.php / download.aspx 等网关注入链接）
    # 2. 根本没获取到文件名
    need_probe = (not filename) or (filename and re.search(r'\.(php|asp|aspx|jsp|cgi|pl)$', filename, re.I))
    if need_probe:
        if not filename:
            print("\r  [*] 未能从链接自动识别文件名，发起探测请求...", end="", flush=True)
        else:
            print("\r  [*] 检测到网关注入链接，尝试获取真实文件名...", end="", flush=True)
        try:
            probe_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Range": "bytes=0-0",
            }
            if referer:
                probe_headers["Referer"] = referer
            probe_resp = requests.get(final_url, timeout=30, headers=probe_headers,
                                       stream=True)
            # 优先从 Content-Disposition 拿文件名
            cd = probe_resp.headers.get("Content-Disposition", "")
            real_name = parse_content_disposition(cd)
            if real_name:
                filename = real_name
            # 若 Content-Disposition 也没有，从最终响应 URL 再试一次
            if not filename or re.search(r'\.(php|asp|aspx|jsp|cgi|pl)$', filename, re.I):
                fname_from_resp_url = extract_filename_from_url(probe_resp.url)
                if fname_from_resp_url and not re.search(r'\.(php|asp|aspx|jsp|cgi|pl)$', fname_from_resp_url, re.I):
                    filename = fname_from_resp_url
            probe_resp.close()
        except Exception:
            pass

    if filename:
        print(f"\r  [>] 文件名: {filename}")
    else:
        print("\r  [!] 无法从链接自动识别文件名")
        print("  [>] 请输入文件名 (回车使用 download.bin):")
        filename = input("  -> ").strip()
        if not filename:
            filename = "download.bin"
            print(f"  [*] 使用默认: {filename}")

    # 步骤 3：保存路径
    default_save = os.path.join(SCRIPT_DIR, filename)
    print(f"\n  [>] 保存到 (回车确认，或输入新路径):")
    print(f"  -> 默认: {default_save}")
    user_path = input("  -> ").strip()
    dest = user_path if user_path else default_save

    if os.path.isdir(dest):
        dest = os.path.join(dest, filename)

    dest_dir = os.path.dirname(dest)
    if dest_dir and not os.path.exists(dest_dir):
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as e:
            print(f"  [!] 无法创建目录 {dest_dir}: {e}")
            return

    # 步骤 4：线程数
    while True:
        print(f"\n  [>] 请输入线程数 (推荐 8~32，回车默认 16):")
        threads_input = input("  -> ").strip()
        if not threads_input:
            num_threads = 16
            print(f"  [*] 使用默认值: 16 线程")
            break
        try:
            num_threads = int(threads_input)
            if num_threads < 1:
                print("  [!] 线程数至少为 1")
                continue
            if num_threads > 128:
                print("  [!] 线程数不建议超过 128")
                continue
            break
        except ValueError:
            print("  [!] 请输入有效数字")

    # 步骤 5：确认
    print("\n" + "-" * 60)
    print(f"  URL:      {url[:55]}{'...' if len(url) > 55 else ''}")
    print(f"  保存到:   {dest}")
    print(f"  线程数:   {num_threads}")
    print("-" * 60)
    confirm = input("\n  确认开始下载? [Y/n]: ").strip().lower()
    if confirm and confirm not in ("y", "yes"):
        print("  已取消")
        return

    # 执行下载
    print("\n" + "=" * 60)
    run_http_download(url, dest, num_threads)

    # 完成
    elapsed = time.time() - start_time
    if os.path.exists(dest):
        final_size = os.path.getsize(dest)
        print(f"\n{'=' * 60}")
        print(f"  [+] 下载完成!")
        print(f"  [>] 文件: {dest}")
        print(f"  [>] 大小: {format_size(final_size)}")
        print(f"  [>] 耗时: {format_time(elapsed)}")
        if elapsed > 0:
            print(f"  [>] 均速: {format_size(final_size / elapsed)}/s")
        print(f"{'=' * 60}")
    else:
        print(f"\n  [!] 下载失败，文件未生成")

    input("\n  按 Enter 退出...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  [*] 用户中断下载")
    except Exception as e:
        print(f"\n  [!] 发生错误: {e}")
        import traceback
        traceback.print_exc()
        input("\n  按 Enter 退出...")
