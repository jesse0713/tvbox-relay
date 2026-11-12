#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""合并多个公开直播源，按频道去重，生成优选清单（不做逐条测速，靠来源自带的 CI 筛选）。"""

import concurrent.futures as cf
import json
import os
import re
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "relay", "live.m3u")
REP = os.path.join(ROOT, "relay", "live-report.json")
UA = "Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36 Chrome/116 Safari/537.36"

# 顺序即优先级：前面的是仓库自己跑过连通性 / 每天重建的
FEEDS = [
    ("bestfan_st", 1, "https://ghfast.top/https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_all_status.m3u8"),
    ("bestfan_all", 2, "https://ghfast.top/https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_all.m3u8"),
    ("yang", 3, "https://gh-proxy.com/https://raw.githubusercontent.com/YanG-1989/m3u/main/Gather.m3u"),
    ("hujing", 4, "https://gh-proxy.com/https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/Global.m3u8"),
    ("iptvorg", 9, "https://ghfast.top/https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u"),
]

MAX_PER_CH = 2
MAX_CHANNELS = 160



_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read()


def canon(raw):
    n = raw.strip()
    n = re.sub(r"\s*[\(（][^\)\）]*[\)\）]\s*$", "", n)
    n = re.sub(r"\s+", "", n).upper()
    n = re.sub(r"^CCTV[-_ ]?(\d+)\+?$", r"CCTV-\1", n)
    n = re.sub(r"^中央(\d+)台?$", r"CCTV-\1", n)
    return n


def group_of(c):
    if re.match(r"^CCTV-\d", c):
        return "央视"
    if c.endswith("卫视"):
        return "卫视"
    if re.search(r"凤凰|翡翠|明珠|TVB|香港|澳门|台湾|中天|东森|三立|民视|台视|华视|中视|非凡|寰宇", c):
        return "港澳台"
    if re.search(r"卡通|少儿|动漫|动画", c):
        return "少儿"
    if re.search(r"体育|足球|篮球|赛事", c):
        return "体育"
    if re.search(r"电影|影视|剧场|电视剧|纪录|纪实", c):
        return "影剧"
    return "其他"


def quality(url, name):
    s = 0
    if "[1080" in name or "1080P" in name.upper():
        s += 3
    elif "[720" in name or "720P" in name.upper():
        s += 2
    if url.endswith(".m3u8"):
        s += 1
    return s


MIRRORS = ["https://gh-proxy.com/", "https://ghproxy.net/", "https://ghfast.top/"]


def fetch_mirrored(url):
    """把镜像地址还原成 raw 路径，按 MIRRORS 顺序重试，避免单个镜像挂掉整份列表失败。"""
    parts = url.split("/https://", 1)
    raw = ("https://" + parts[1]) if len(parts) == 2 else url
    last = None
    for m in MIRRORS:
        try:
            return get(m + raw)
        except Exception as e:
            last = e
            continue
    raise last


def main():
    rows = []
    for name, prio, url in FEEDS:
        try:
            text = fetch_mirrored(url).decode("utf-8-sig", "ignore")
        except Exception as e:
            print("feed %-12s ERR %s" % (name, str(e)[:70]))
            continue
        cur = ""
        n = 0
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF"):
                cur = line.split(",")[-1].strip()
            elif line.startswith("http") and cur:
                c = canon(cur)
                g = group_of(c)
                if len(c) >= 2 and not c.isdigit() and "NOT24" not in c and (g != "其他" or c.endswith("卫视")):
                    rows.append({"c": c, "g": g, "u": line, "p": prio, "q": quality(line, cur), "src": name})
                    n += 1
        print("feed %-12s %4d" % (name, n))

    # 去重 url
    seen = set()
    uniq = []
    for r in rows:
        if r["u"] in seen:
            continue
        seen.add(r["u"])
        uniq.append(r)

    prio = {"央视": 1, "卫视": 2, "港澳台": 3, "体育": 4, "少儿": 5, "影剧": 6, "其他": 7}
    chans = {}
    for r in uniq:
        chans.setdefault(r["c"], []).append(r)

    keep = sorted(chans.keys(), key=lambda c: (prio[group_of(c)], len(c), c))[:MAX_CHANNELS]

    picked = []
    for c in sorted(keep, key=lambda c: (prio[group_of(c)], c)):
        arr = sorted(chans[c], key=lambda r: (r["p"], -r["q"]))
        for i, r in enumerate(arr[:MAX_PER_CH]):
            picked.append((c, r["g"], r["u"], i))

    # ---- 轻量校验：只保留能返回 m3u8 正文的条目 ----
    def alive(u):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with _OPENER.open(req, timeout=6) as r:
                head = r.read(4096)
            return b"#EXTM3U" in head
        except Exception:
            return False

    urls_to_check = [x[2] for x in picked]
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        ok_map = dict(zip(urls_to_check, ex.map(alive, urls_to_check)))
    before = len(picked)
    picked = [x for x in picked if ok_map.get(x[2])]
    print("validated %d -> %d entries" % (before, len(picked)))
    if not picked:
        print("all entries failed validation")
        return 1

    out = ["#EXTM3U", "# 本机优选 %s · 多源合并" % time.strftime("%Y-%m-%d %H:%M")]
    for c, g, u, i in picked:
        label = c if i == 0 else "%s-备%d" % (c, i + 1)
        out.append('#EXTINF:-1 tvg-name="%s" group-title="%s",%s' % (label, g, label))
        out.append(u)

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")

    groups = {}
    for c, g, u, i in picked:
        groups[g] = groups.get(g, 0) + 1
    stat = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "urls_in": len(uniq),
        "channels": len({p[0] for p in picked}),
        "entries": len(picked),
        "groups": groups,
    }
    with open(REP, "w", encoding="utf-8") as f:
        json.dump(stat, f, ensure_ascii=False, indent=2)
    print(json.dumps(stat, ensure_ascii=False))
    return 0 if stat["channels"] >= 20 else 1


if __name__ == "__main__":
    raise SystemExit(main())
