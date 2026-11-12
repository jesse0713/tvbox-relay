# -*- coding: utf-8 -*-
"""直播源实测：时延、分片时长、码率、实际下载速度。

用法:
  python tools/probe-live.py                 # 测 live.m3u 里的央视/卫视主线路
  python tools/probe-live.py --name 湖南卫视  # 只测匹配的频道
  python tools/probe-live.py --all           # 全测（慢）

输出每个频道的：起播首字节耗时、分片时长、单分片字节、码率、
连续下载 3 个分片的耗时/速度，以及“码率需求 vs 实测带宽”的余量。
"""
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
M3U = os.path.join(ROOT, "relay", "live.m3u")
UA = ("Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url, timeout=12, limit=None, deadline=None):
    """limit: 最多读多少字节；deadline: 最多读多少秒。两者都可能先到。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        ttfb = time.time() - t0
        chunks = []
        total = 0
        while True:
            b = r.read(65536)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if limit and total >= limit:
                break
            if deadline and time.time() - t0 >= deadline:
                break
        return b"".join(chunks), ttfb, time.time() - t0, dict(r.headers)


def parse_m3u(path):
    out = []
    name = None
    for line in open(path, "r", encoding="utf-8", errors="ignore"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r'tvg-name="([^"]*)"', line) or re.search(r",([^,]*)$", line)
            name = m.group(1) if m else line
        elif not line.startswith("#"):
            out.append((name, line))
            name = None
    return out


def best_variant(playlist, base):
    lines = playlist.splitlines()
    best = None
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", ln)
            bw = int(m.group(1)) if m else 0
            res = re.search(r"RESOLUTION=(\d+x\d+)", ln)
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith("#"):
                    url = nxt if nxt.startswith("http") else base + nxt
                    if best is None or bw > best[0]:
                        best = (bw, url, res.group(1) if res else "")
                    break
    return best


def probe(name, url):
    rec = {"name": name, "url": url}
    try:
        body, ttfb, total, _ = fetch(url)
        text = body.decode("utf-8", "ignore")
        rec["playlist_ttfb"] = round(ttfb, 2)
        base = url[: url.rfind("/") + 1]
        if "#EXT-X-STREAM-INF" in text:
            v = best_variant(text, base)
            if v:
                rec["bandwidth_hint"] = v[0]
                rec["resolution"] = v[2]
                url2 = v[1]
                base = url2[: url2.rfind("/") + 1]
                body, ttfb2, _, _ = fetch(url2)
                text = body.decode("utf-8", "ignore")
                rec["media_ttfb"] = round(ttfb2, 2)
        segs = [l.strip() for l in text.splitlines()
                if l.strip() and not l.startswith("#")]
        durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text)]
        endlist = "#EXT-X-ENDLIST" in text
        rec["segments_in_playlist"] = len(segs)
        rec["target_seg_len"] = round(max(durs), 2) if durs else None
        rec["vod"] = endlist
        if not segs:
            rec["error"] = "no segment lines"
            return rec
        # 连续下载 3 个分片（模拟播放器追直播边）
        times, sizes = [], []
        for s in segs[-3:] if len(segs) >= 3 else segs:
            su = s if s.startswith("http") else base + s
            t0 = time.time()
            data, _, _, _ = fetch(su, timeout=20)
            times.append(round(time.time() - t0, 2))
            sizes.append(len(data))
        rec["seg_bytes"] = sizes
        rec["seg_seconds"] = times
        if rec["target_seg_len"]:
            need = sizes[-1] * 8 / rec["target_seg_len"] / 1e6
            rec["need_mbps"] = round(need, 2)
        got = sizes[-1] * 8 / times[-1] / 1e6 if times[-1] > 0 else 0
        rec["got_mbps"] = round(got, 2)
        # 余量：下载耗时 / 分片时长，>1 说明追不上直播边
        if rec["target_seg_len"] and times:
            rec["realtime_ratio"] = round(times[-1] / rec["target_seg_len"], 2)
    except urllib.error.HTTPError as e:
        rec["error"] = "HTTP %s" % e.code
    except Exception as e:
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    return rec


def main():
    args = sys.argv[1:]
    name_filter = None
    take_all = "--all" in args
    if "--name" in args:
        name_filter = args[args.index("--name") + 1]
    items = parse_m3u(M3U)
    if name_filter:
        items = [x for x in items if name_filter in (x[0] or "")]
    elif not take_all:
        keep = ("CCTV-1", "CCTV-5", "CCTV-13", "湖南卫视", "浙江卫视",
                "东方卫视", "江苏卫视", "北京卫视", "广东卫视", "凤凰香港")
        items = [x for x in items if any(k in (x[0] or "") for k in keep)
                 and "备2" not in (x[0] or "")]
    results = []
    for n, u in items:
        r = probe(n or "?", u)
        results.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
    print("\n=== 汇总 ===")
    for r in results:
        if r.get("error"):
            print("%-14s ERROR %s" % (r["name"][:14], r["error"]))
        else:
            print("%-14s 分片%-5s 需 %5s Mbps 实测 %6s Mbps 余量 %-5s 起播 %ss" % (
                r["name"][:14], r.get("target_seg_len"), r.get("need_mbps"),
                r.get("got_mbps"), r.get("realtime_ratio"), r.get("playlist_ttfb")))
    out = os.path.join(HERE, "probe-live-result.json")
    json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\nsaved:", out)


if __name__ == "__main__":
    main()
