#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""点播加速自测：像播放器一样把一集从头拉到尾，看还会不会「播几秒缓冲几秒」。

做法和电视上的 ExoPlayer 一致：先取播放表，然后按顺序一个分片一个分片地要，
每段记一次「要多久才拿到」。拿的时间超过这集本身的时长，就是卡顿。

用法：
  python tools/vod-test.py 量子            # 按站名关键字挑站点，测第 1 集前 20 段
  python tools/vod-test.py 极速 --segs 30
  python tools/vod-test.py 量子 --full     # 一直等到整集预取完，报总速度
  python tools/vod-test.py --url <m3u8>    # 直接测一个播放表地址

前提：中转服务在跑（默认 http://127.0.0.1:8080，可用 --relay 改）。
"""
import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_RELAY = "http://127.0.0.1:8080"


def b64(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "TVBox-vod-test/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def stats(relay):
    try:
        return json.loads(get(relay + "/vod/stats", 10).decode("utf-8"))
    except Exception:
        return {}


def pick_site(relay, keyword):
    j = json.loads(get(relay + "/tv.json", 40).decode("utf-8"))
    hits = [s for s in j.get("sites") or [] if keyword in (s.get("name") or "")]
    if not hits:
        raise SystemExit("没找到名字带「%s」的站点" % keyword)
    s = hits[0]
    if "/vod/cms/" not in (s.get("api") or ""):
        raise SystemExit("站点 %s 没走中转（api=%s），测不了" % (s.get("name"), s.get("api")))
    return s


def first_episode(api):
    body = json.loads(get(api + "?ac=detail&pg=1", 40).decode("utf-8-sig"))
    lst = body.get("list") or []
    if not lst:
        raise SystemExit("采集接口没有片子")
    it = lst[0]
    froms = str(it.get("vod_play_from") or "").split("$$$")
    groups = str(it.get("vod_play_url") or "").split("$$$")
    for i, g in enumerate(groups):
        first = g.split("#")[0]
        u = first.split("$", 1)[1].strip() if "$" in first else first.strip()
        if u.startswith("http"):
            return it.get("vod_name"), (froms[i] if i < len(froms) else "?"), u
    raise SystemExit("这部片没有可播地址")


def playlists(url):
    """返回 (分片地址列表, 每段时长列表, 播放表文本)。"""
    txt = get(url, 60).decode("utf-8", "ignore")
    if "#EXT-X-STREAM-INF" in txt:
        sub = [l.strip() for l in txt.splitlines() if l.strip() and not l.startswith("#")][0]
        txt = get(sub, 60).decode("utf-8", "ignore")
    segs = [l.strip() for l in txt.splitlines() if l.strip() and not l.startswith("#")]
    durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", txt)]
    return segs, durs, txt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site", nargs="?", help="站名关键字，比如 量子")
    ap.add_argument("--url", help="直接给播放表地址")
    ap.add_argument("--segs", type=int, default=20, help="测多少段（默认 20）")
    ap.add_argument("--full", action="store_true", help="等整集预取完，报总速度")
    ap.add_argument("--relay", default=DEFAULT_RELAY)
    args = ap.parse_args()

    relay = args.relay.rstrip("/")
    if args.url:
        name, line, url = "-", "-", args.url
    else:
        if not args.site:
            raise SystemExit("给个站名，或者用 --url")
        s = pick_site(relay, args.site)
        name, line, url = first_episode(s["api"])
        print("站点：%s ｜ 片子：%s ｜ 线路：%s" % (s.get("name"), name, line))

    t0 = time.time()
    segs, durs, _ = playlists(url)
    print("播放表 %.1fs，%d 段，共 %.0f 分钟" % (time.time() - t0, len(segs), sum(durs) / 60))

    stall = played = 0.0
    n = min(args.segs, len(segs))
    for i in range(n):
        a = time.time()
        get(segs[i], 120)
        dt = time.time() - a
        d = durs[i] if i < len(durs) else 0
        played += d
        late = max(0.0, dt - d)
        stall += late
        flag = ("卡 %.1fs" % late) if late > 0.5 else "顺"
        print("  第%02d段 时长 %5.2fs 取用 %5.2fs  %s" % (i + 1, d, dt, flag))

    pct = stall / max(played, 1e-9) * 100
    verdict = "没问题" if pct < 3 else ("勉强" if pct < 15 else "还是会卡")
    print("\n前 %d 段：播放 %.1fs，卡顿 %.1fs，卡顿率 %.0f%% → %s" % (n, played, stall, pct, verdict))

    if args.full:
        print("等整集预取完……")
        last = None
        while True:
            st = stats(relay)
            if not st or st.get("total") == 0:
                break
            if st.get("done", 0) + st.get("failed", 0) >= st.get("total", 0) or st.get("key") is None:
                break
            cur = "预取 %d/%d 分片，缓存 %.0f MB" % (st.get("done", 0), st.get("total", 0), st.get("cache_mb", 0))
            if cur != last:
                print("  " + cur)
                last = cur
            time.sleep(3)
        st = stats(relay)
        used = time.time() - (st.get("at") or time.time())
        print("  完成：%d/%d 分片，失败 %d，用时 %.0fs，均速 %.2f Mbps" % (
            st.get("done", 0), st.get("total", 0), st.get("failed", 0), used,
            st.get("bytes", 0) * 8 / max(used, 1) / 1e6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
