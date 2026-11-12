#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对 relay/live.m3u 做分片级测速，剔除卡顿线路。

只保留"下载一个真实分片 ≥ 阈值"的线路，同频道最多 2 条，按速度快慢排序。
用法: python speedfilter.py [--min-kb 550] [--file relay/live.m3u]
"""

import argparse
import concurrent.futures as cf
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UA = "Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36 Chrome/116 Safari/537.36"
MAX_CH = 2


def get(url, timeout, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if limit:
            data = r.read(limit)
        else:
            data = r.read()
    return data, time.time() - t0


def resolve(base, rel):
    rel = rel.strip()
    if rel.startswith("http"):
        return rel
    if rel.startswith("/"):
        m = re.match(r"(https?://[^/]+)", base)
        return m.group(1) + rel
    return base.rsplit("/", 1)[0] + "/" + rel


def measure(entry):
    name, url = entry
    try:
        pl, _ = get(url, 8)
        text = pl.decode("utf-8-sig", "ignore")
        if "#EXTM3U" not in text:
            return (name, url, 0, "no-playlist")
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if any(x.startswith("#EXT-X-STREAM-INF") for x in lines):
            v = next((x for x in lines if not x.startswith("#")), None)
            if not v:
                return (name, url, 0, "no-variant")
            vurl = resolve(url, v)
            pl2, _ = get(vurl, 8)
            lines = [x.strip() for x in pl2.decode("utf-8-sig", "ignore").splitlines() if x.strip()]
            base = vurl
        else:
            base = url
        seg = next((x for x in lines if not x.startswith("#")), None)
        if not seg:
            return (name, url, 0, "no-segment")
        segurl = resolve(base, seg)
        data, dt = get(segurl, 8, limit=2 * 1024 * 1024)
        kb = len(data) / 1024.0 / max(dt, 0.001)
        if len(data) < 60000:
            return (name, url, 0, "tiny")
        return (name, url, round(kb), "ok")
    except Exception as e:
        return (name, url, 0, str(e)[:28])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-kb", type=int, default=550)
    ap.add_argument("--file", default=os.path.join(ROOT, "relay", "live.m3u"))
    args = ap.parse_args()

    text = open(args.file, encoding="utf-8-sig").read()
    entries = []
    name = ""
    lines = text.splitlines()
    head = []
    for line in lines:
        if line.startswith("#EXTINF"):
            name = line.split(",")[-1]
        elif line.startswith("http"):
            entries.append((name, line, ))
    # 重新解析，保留 EXTINF 原文以便回写
    blocks = []
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF") and i + 1 < len(lines) and lines[i + 1].startswith("http"):
            blocks.append((line, lines[i + 1]))
    print("entries %d" % len(blocks))

    results = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(measure, [(b[0].split(",")[-1], b[1]) for b in blocks]):
            results.append(r)

    ok = [r for r in results if r[2] >= args.min_kb]
    print("speed>=%d: %d / %d" % (args.min_kb, len(ok), len(results)))
    if len(ok) < 10:
        print("too few survivors, keep original")
        return 1

    # 名字 -> 频道名（去掉 -备N）
    def chan(n):
        return re.sub(r"-备\d+$", "", n)

    by = {}
    for name, url, kb, _st in ok:
        by.setdefault(chan(name), []).append((kb, name, url))
    for c in by:
        by[c].sort(reverse=True)

    prio = {"CCTV": 1, "卫视": 2}
    def order(c):
        if re.match(r"^CCTV", c):
            return (1, c)
        if c.endswith("卫视"):
            return (2, c)
        return (3, c)

    out = []
    for c in sorted(by, key=order):
        for i, (kb, name, url) in enumerate(by[c][:MAX_CH]):
            label = c if i == 0 else "%s-备%d" % (c, i + 1)
            grp = "央视" if re.match(r"^CCTV", c) else ("卫视" if c.endswith("卫视") else "其他")
            out.append('#EXTINF:-1 tvg-name="%s" group-title="%s",%s' % (label, grp, label))
            out.append(url)
    with open(args.file, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n# 测速优选 %s · 阈值 %dKB/s\n" % (time.strftime("%Y-%m-%d %H:%M"), args.min_kb))
        f.write("\n".join(out) + "\n")
    print("kept %d channels / %d entries" % (len(by), len(out) // 2))
    top = sorted(results, key=lambda x: -x[2])[:5]
    for n, u, kb, st in top:
        print("  %6d KB/s  %-14s %s" % (kb, n, st))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
