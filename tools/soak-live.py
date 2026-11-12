# -*- coding: utf-8 -*-
"""直播源“持续播放”体检：像播放器一样连续追分片，找出周期性卡顿。

对每个频道连续跟 60~90 秒，记录每个分片的：时长、下载耗时、大小、
以及“下载耗时 > 分片时长”（播放器必然卡）的次数。
"""
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
M3U = os.path.join(ROOT, "relay", "live.m3u")
UA = ("Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TARGETS = ["CCTV-1 ", "湖南卫视", "浙江卫视", "东方卫视"]


def http(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout, context=CTX)


def read_playlist(url):
    with http(url) as r:
        return r.read().decode("utf-8", "ignore")


def seg_urls(text, base):
    segs = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            segs.append(ln if ln.startswith("http") else base + ln)
    durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text)]
    return segs, durs


def soak(name, url, seconds=70):
    print("[start] %s" % name, flush=True)
    rec = {"name": name, "url": url, "stalls": 0, "segments": 0,
           "max_dl": 0, "bytes": 0, "errors": 0}
    try:
        text = read_playlist(url)
        base = url[: url.rfind("/") + 1]
        if "#EXT-X-STREAM-INF" in text:
            m = re.search(r"BANDWIDTH=(\d+)", text)
            rec["bw_hint"] = int(m.group(1)) if m else None
            for i, ln in enumerate(text.splitlines()):
                if ln.startswith("#EXT-X-STREAM-INF"):
                    nxt = text.splitlines()[i + 1].strip()
                    url = nxt if nxt.startswith("http") else base + nxt
                    base = url[: url.rfind("/") + 1]
                    text = read_playlist(url)
                    break
        seen = set()
        t_end = time.time() + seconds
        t_start = time.time()
        while time.time() < t_end:
            try:
                text = read_playlist(url)
            except Exception as e:
                rec["errors"] += 1
                print("  %5.0fs playlist err %s" % (time.time() - t_start, e), flush=True)
                time.sleep(2)
                continue
            segs, durs = seg_urls(text, base)
            fresh = [(s, durs[i] if i < len(durs) else 5) for i, s in enumerate(segs) if s not in seen]
            if not fresh:
                time.sleep(1.5)
                continue
            for su, dur in fresh[-2:]:
                if time.time() > t_end:
                    break
                t0 = time.time()
                try:
                    with http(su, timeout=20) as r:
                        n = 0
                        while True:
                            b = r.read(65536)
                            if not b:
                                break
                            n += len(b)
                    dt = time.time() - t0
                except Exception as e:
                    rec["errors"] += 1
                    print("  %5.0fs seg err %s" % (time.time() - t_start, e), flush=True)
                    break
                seen.add(su)
                rec["segments"] += 1
                rec["bytes"] += n
                rec["max_dl"] = max(rec["max_dl"], round(dt, 2))
                bad = dt > dur * 0.9
                if bad:
                    rec["stalls"] += 1
                print("  %5.1fs seg %.0fs 下载 %5.2fs %6.2fMB %5.2fMbps %s" % (
                    time.time() - t_start, dur, dt, n / 1e6, n * 8 / dt / 1e6,
                    "  <-- 追不上(会卡)" if bad else ""), flush=True)
            time.sleep(0.3)
        rec["avg_mbps"] = round(rec["bytes"] * 8 / max(1, time.time() - t_start) / 1e6, 2)
    except Exception as e:
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    print("[done] %s  分片=%d 卡顿=%d 最大下载=%.1fs 均速=%.2fMbps 错误=%d %s" % (
        name, rec["segments"], rec["stalls"], rec["max_dl"],
        rec.get("avg_mbps", 0), rec["errors"], rec.get("error", "")), flush=True)
    return rec


def main():
    targets = TARGETS
    if "--name" in sys.argv:
        targets = [sys.argv[sys.argv.index("--name") + 1]]
    picked = []
    for line_name, line_url in [(None, None)]:
        pass
    name = None
    for line in open(M3U, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if line.startswith("#EXTINF"):
            m = re.search(r'tvg-name="([^"]*)"', line)
            name = m.group(1) if m else ""
        elif line and not line.startswith("#"):
            for t in targets:
                if name and (name == t or name.startswith(t + "-备")):
                    picked.append((name, line))
                    break
            name = None
    results = []
    for n, u in picked:
        results.append(soak(n, u, seconds=60))
    out = os.path.join(HERE, "soak-live-result.json")
    json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\nsaved:", out)


if __name__ == "__main__":
    main()
