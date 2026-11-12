#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""IPv6 高清直播源：抓取 + 校验，输出 relay/live6.m3u。

需要家宽支持 IPv6（路由器开启、电视也拿到 v6 地址）。
"""

import concurrent.futures as cf
import json
import os
import re
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "relay", "live6.m3u")
REP = os.path.join(ROOT, "relay", "live6-report.json")
UA = "Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36 Chrome/116 Safari/537.36"

FEEDS = [
    "https://gh-proxy.com/https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u",
    "https://ghfast.top/https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u",
]



_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read()


def canon(raw):
    n = re.sub(r"\s*[\(（][^\)\）]*[\)\）]\s*$", "", raw.strip())
    n = re.sub(r"\s+", "", n).upper()
    n = re.sub(r"^CCTV[-_ ]?(\d+)\+?$", r"CCTV-\1", n)
    return n


def group_of(c):
    if re.match(r"^CCTV-\d", c):
        return "央视"
    if c.endswith("卫视"):
        return "卫视"
    if re.search(r"凤凰|翡翠|明珠|TVB|香港|澳门|台湾", c):
        return "港澳台"
    if re.search(r"卡通|少儿|动漫", c):
        return "少儿"
    return "其他"


def main():
    text = None
    for u in FEEDS:
        try:
            text = get(u).decode("utf-8-sig", "ignore")
            if "#EXTINF" in text:
                break
        except Exception as e:
            print("feed ERR %s %s" % (u[:50], str(e)[:50]))
            text = None
    if not text:
        print("no feed")
        return 1

    rows = []
    cur = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            cur = line.split(",")[-1].strip()
        elif line.startswith("http") and cur:
            rows.append({"c": canon(cur), "u": line, "raw": cur})

    seen = set()
    uniq = []
    for r in rows:
        if r["u"] in seen:
            continue
        seen.add(r["u"])
        uniq.append(r)
    print("candidates %d" % len(uniq))

    def alive(r):
        try:
            req = urllib.request.Request(r["u"], headers={"User-Agent": UA})
            with _OPENER.open(req, timeout=8) as resp:
                head = resp.read(4096)
            return (r, b"#EXTM3U" in head)
        except Exception:
            return (r, False)

    ok = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for r, good in ex.map(alive, uniq):
            if good:
                ok.append(r)
    print("passed %d" % len(ok))
    if not ok:
        print("nothing alive")
        return 1

    by = {}
    for r in ok:
        by.setdefault(r["c"], []).append(r)

    prio = {"央视": 1, "卫视": 2, "港澳台": 3, "少儿": 4, "其他": 5}
    out = ["#EXTM3U", "# IPv6 高清 %s" % time.strftime("%Y-%m-%d %H:%M")]
    entries = 0
    for c in sorted(by.keys(), key=lambda x: (prio[group_of(x)], x)):
        for i, r in enumerate(by[c][:2]):
            label = c if i == 0 else "%s-备%d" % (c, i + 1)
            out.append('#EXTINF:-1 tvg-name="%s" group-title="IPv6-%s",%s' % (label, group_of(c), label))
            out.append(r["u"])
            entries += 1

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")

    groups = {}
    for c in by:
        g = "IPv6-" + group_of(c)
        groups[g] = groups.get(g, 0) + 1
    stat = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channels": len(by),
        "entries": entries,
        "groups": groups,
    }
    with open(REP, "w", encoding="utf-8") as f:
        json.dump(stat, f, ensure_ascii=False, indent=2)
    print(json.dumps(stat, ensure_ascii=False))
    return 0 if len(by) >= 20 else 1


if __name__ == "__main__":
    raise SystemExit(main())
