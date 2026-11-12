#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""辽宁移动 IPTV 源：抓取 + 分片测速，生成 relay/live-ln.m3u。

源：Long1982A/lntv（辽宁移动 OTT CDN，hwottcdn.ln.chinamobile.com）
只有辽宁移动宽带能直连，因此单独成一份，排在电视直播列表第一位。
"""

import concurrent.futures as cf
import json
import os
import re
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "relay", "live-ln.m3u")
REP = os.path.join(ROOT, "relay", "live-ln-report.json")
UA = "Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36 Chrome/116 Safari/537.36"

FILES = ["辽宁移动1.txt", "辽宁移动2.txt", "辽宁本溪移动.txt", "辽宁移动湖北CDN.txt"]
BASES = [
    "https://ghfast.top/https://raw.githubusercontent.com/Long1982A/lntv/main/",
    "https://gh-proxy.com/https://raw.githubusercontent.com/Long1982A/lntv/main/",
]
MIN_KB = 900
MAX_CH = 2



_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, timeout=10, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    t0 = time.time()
    with _OPENER.open(req, timeout=timeout) as r:
        data = r.read(limit) if limit else r.read()
    return data, time.time() - t0


def fetch_text(name):
    for base in BASES:
        try:
            data, _ = get(base + urllib.parse.quote(name), 30)
            text = data.decode("utf-8-sig", "ignore")
            if "," in text:
                return text
        except Exception as e:
            print("  %s via %s -> %s" % (name, base.split("/")[2], str(e)[:40]))
    return ""


def canon(name):
    n = name.strip()
    n = re.sub(r"\s+", "", n)
    m = re.match(r"^CCTV[- ]?(\d+)(\+)?$", n, re.I)
    if m:
        return "CCTV-%s%s" % (m.group(1), m.group(2) or "")
    m = re.match(r"^CCTV[- ]?(\d+)-?4K", n, re.I)
    if m:
        return "CCTV-%s-4K" % m.group(1)
    if re.match(r"^CCTV4K", n, re.I):
        return "CCTV-4K"
    return n


def group_of(c):
    if re.match(r"^CCTV-\d", c):
        return "4K" if c.endswith("4K") else "央视"
    if c.endswith("卫视") or c.endswith("卫视-4K"):
        return "卫视"
    if re.search(r"CHC|剧场|电影|影院|风云|纪实|纪录|戏曲|茶", c):
        return "影剧"
    return "其他"


def resolve(base, rel):
    rel = rel.strip()
    if rel.startswith("http"):
        return rel
    if rel.startswith("/"):
        m = re.match(r"(https?://[^/]+)", base)
        return m.group(1) + rel
    return base.rsplit("/", 1)[0] + "/" + rel


def measure(item):
    c, url = item
    try:
        pl, _ = get(url, 8)
        text = pl.decode("utf-8-sig", "ignore")
        if "#EXTM3U" not in text:
            return (c, url, 0, "no-list")
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if any(x.startswith("#EXT-X-STREAM-INF") for x in lines):
            v = next((x for x in lines if not x.startswith("#")), None)
            if not v:
                return (c, url, 0, "no-variant")
            vurl = resolve(url, v)
            pl2, _ = get(vurl, 8)
            lines = [x.strip() for x in pl2.decode("utf-8-sig", "ignore").splitlines() if x.strip()]
            base = vurl
        else:
            base = url
        seg = next((x for x in lines if not x.startswith("#")), None)
        if not seg:
            return (c, url, 0, "no-seg")
        segurl = resolve(base, seg)
        data, dt = get(segurl, 8, limit=1536 * 1024)
        kb = len(data) / 1024.0 / max(dt, 0.001)
        if len(data) < 80000:
            return (c, url, 0, "tiny")
        return (c, url, round(kb), "ok")
    except Exception as e:
        return (c, url, 0, str(e)[:26])


def main():
    rows = []
    for f in FILES:
        text = fetch_text(f)
        n = 0
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            m = re.match(r"^(.*?),(https?://.+)$", s)
            if m:
                rows.append((canon(m.group(1)), m.group(2).strip()))
                n += 1
        print("  %-18s %d" % (f, n))

    seen = set()
    uniq = []
    for c, u in rows:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((c, u))
    by_cand = {}
    for c, u in uniq:
        by_cand.setdefault(c, []).append(u)
    limited = []
    for c, us in by_cand.items():
        for u in us[:3]:
            limited.append((c, u))
    uniq = limited
    print("unique %d -> candidates %d, channels %d" % (len(rows), len(uniq), len(by_cand)))

    results = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(measure, uniq):
            results.append(r)
    good = [r for r in results if r[2] >= MIN_KB]
    print("passed %d (>=%d KB/s) of %d" % (len(good), MIN_KB, len(results)))

    if len(good) < 15:
        print("too few, abort")
        return 1

    by = {}
    for c, u, kb, _ in good:
        by.setdefault(c, []).append((kb, u))
    for c in by:
        by[c].sort(reverse=True)

    order = {"央视": 1, "卫视": 2, "4K": 3, "影剧": 4, "其他": 5}
    out = ["#EXTM3U", "# 辽宁移动 IPTV %s · %d 频道" % (time.strftime("%Y-%m-%d %H:%M"), len(by))]
    entries = 0
    for c in sorted(by, key=lambda x: (order[group_of(x)], x)):
        for i, (kb, u) in enumerate(by[c][:MAX_CH]):
            label = c if i == 0 else "%s-备%d" % (c, i + 1)
            out.append('#EXTINF:-1 tvg-name="%s" group-title="辽宁移动-%s",%s' % (label, group_of(c), label))
            out.append(u)
            entries += 1
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")

    groups = {}
    for c in by:
        g = group_of(c)
        groups[g] = groups.get(g, 0) + 1
    stat = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "channels": len(by),
            "entries": entries, "min_kb": MIN_KB, "groups": groups}
    with open(REP, "w", encoding="utf-8") as f:
        json.dump(stat, f, ensure_ascii=False, indent=2)
    print(json.dumps(stat, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
