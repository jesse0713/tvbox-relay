# -*- coding: utf-8 -*-
"""网络侧速查：本机 / 网关 / 电视的 TCP 连通与握手耗时、宽带下行实测。

用法：
  python tools/netprobe.py                           # 自动探测本机、网关、中转、外网
  python tools/netprobe.py --tv 192.168.1.50         # 顺带测电视（同网段设备）
  python tools/netprobe.py --tv 192.168.1.50:8080    # 指定端口
  python tools/netprobe.py --no-speed                # 跳过耗时的下载测速

排查思路：中转跑在本机，电视连不上时先看「本机 → 网关」是否通畅，
再看「本机 → 电视」的 8080 是否是电视那边被墙/不同网段 —— 全是 TCP 握手数据，
不依赖 ping（很多路由器禁 ping，结论会误导）。
"""
import argparse
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def gateway():
    """取默认网关：Windows 用 route print，Linux/macOS 用 ip route。"""
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(["route", "print", "-4", "0.0.0.0"],
                                 capture_output=True, text=True, timeout=8).stdout
            m = re.search(r"0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)", out)
            if m:
                return m.group(1)
        else:
            out = subprocess.run(["ip", "route", "show", "default"],
                                 capture_output=True, text=True, timeout=8).stdout
            m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def tcp(host, port, tries=5, timeout=3):
    ok, times = 0, []
    for _ in range(tries):
        s = socket.socket()
        s.settimeout(timeout)
        t0 = time.time()
        try:
            s.connect((host, port))
            times.append((time.time() - t0) * 1000)
            ok += 1
        except Exception:
            pass
        finally:
            s.close()
        time.sleep(0.2)
    if times:
        return "%d/%d 成功  握手 %.0f~%.0f ms（中位 %.0f）" % (
            ok, tries, min(times), max(times), sorted(times)[len(times) // 2])
    return "%d/%d 成功" % (ok, tries)


def http_head(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            body = r.read(4096)
            return "HTTP %s  %.0f ms  %d bytes  %s" % (
                r.status, (time.time() - t0) * 1000, len(body),
                r.headers.get("Server", ""))
    except Exception as e:
        return "失败 %s" % e


def speed(url, seconds=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
            while time.time() - t0 < seconds:
                b = r.read(262144)
                if not b:
                    break
                total += len(b)
        dt = time.time() - t0
        return "%.1f MB / %.1f s = %.1f Mbps" % (total / 1e6, dt, total * 8 / dt / 1e6)
    except Exception as e:
        return "失败 %s（已收 %.1f MB）" % (e, total / 1e6)


def split_host(spec, default_port):
    if ":" in spec:
        h, p = spec.rsplit(":", 1)
        return h, int(p)
    return spec, default_port


def main():
    ap = argparse.ArgumentParser(description="本机 / 网关 / 电视 连通性与带宽速查")
    ap.add_argument("--tv", action="append", default=[], metavar="HOST[:PORT]",
                    help="额外探测的设备（电视、NAS 等），可重复")
    ap.add_argument("--router-port", type=int, default=80, help="网关 Web 端口，默认 80")
    ap.add_argument("--no-speed", action="store_true", help="跳过下载测速")
    a = ap.parse_args()

    ip = lan_ip()
    gw = gateway()
    print("本机 IP：%s    默认网关：%s" % (ip, gw or "未识别"))
    print()

    print("== 局域网设备 ==")
    targets = [("127.0.0.1", 8080, "中转（本机）"), (ip, 8080, "中转（本机网卡）")]
    if gw:
        targets.append((gw, a.router_port, "路由器"))
    for spec in a.tv:
        h, p = split_host(spec, 8080)
        targets.append((h, p, "自定义设备"))
    for host, port, label in targets:
        print("tcp %-16s:%-5d %-12s %s" % (host, port, label, tcp(host, port)))

    if gw:
        print("\n== 路由器首页 ==")
        for u in ("http://%s/" % gw, "http://%s/cgi-bin/luci" % gw):
            print(u, "->", http_head(u))

    if not a.no_speed:
        print("\n== 宽带下行 ==")
        for u in ("http://mirrors.aliyun.com/ubuntu-releases/22.04/ubuntu-22.04.5-desktop-amd64.iso",
                  "https://speed.cloudflare.com/__down?bytes=50000000"):
            print(u.split("/")[2], "->", speed(u))

    print("\n提示：8080 不通先看中转是否在跑（/health）；电视不通再看防火墙和网段。")


if __name__ == "__main__":
    main()
