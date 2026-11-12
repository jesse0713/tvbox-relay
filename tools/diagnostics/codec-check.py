#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""临时诊断：抓每个采集站的第一个分片，判断 H264 还是 HEVC。"""
import json
import sys
import time
import urllib.parse
import urllib.request

R = "http://127.0.0.1:8080"
UA = ("Mozilla/5.0 (Linux; Android 10; SM-A505F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36")


def get(u, t=45):
    r = urllib.request.urlopen(urllib.request.Request(
        u, headers={"User-Agent": UA, "Accept-Encoding": "identity"}), timeout=t)
    return r.read()


def codec_of(seg):
    # H.265: 00 00 01 40 / 00 00 01 26 (VPS/SPS) ; H.264: 00 00 01 65/67/68
    h265 = (b"\x00\x00\x01\x40" in seg or b"\x00\x00\x01\x26" in seg
            or b"\x00\x00\x00\x01\x40" in seg)
    h264 = (b"\x00\x00\x01\x65" in seg or b"\x00\x00\x01\x67" in seg
            or b"\x00\x00\x00\x01\x67" in seg)
    if h265:
        return "HEVC(H.265)"
    if h264:
        return "H.264"
    return "未知"


def main():
    j = json.loads(get(R + "/tv.json", 60).decode("utf-8"))
    sites = [s for s in j.get("sites") or [] if "/vod/cms/" in (s.get("api") or "")]
    print("共 %d 个走中转的采集站" % len(sites))
    for s in sites[:10]:
        api = s["api"]
        try:
            body = json.loads(get(api.split("?")[0] + "?ac=detail&pg=1", 40).decode("utf-8-sig"))
            it = (body.get("list") or [None])[0]
            if not it:
                continue
            raw = str(it.get("vod_play_url") or "")
            first = raw.split("$$$")[0].split("#")[0]
            u = first.split("$", 1)[1].strip() if "$" in first else ""
            if not u.startswith("http"):
                continue
            t0 = time.time()
            m3u = get(u, 45).decode("utf-8", "ignore")
            if "#EXT-X-STREAM-INF" in m3u:
                var = [l for l in m3u.splitlines() if l.strip() and not l.startswith("#")][0]
                m3u = get(urllib.parse.urljoin(u, var), 45).decode("utf-8", "ignore")
            segs = [l.strip() for l in m3u.splitlines() if l.strip() and not l.startswith("#")]
            if not segs:
                print("%-18s 无分片" % s.get("name"))
                continue
            seg = urllib.parse.urljoin(u, segs[0])
            sb = get(seg, 45)
            print("%-18s %6d 字节  %.1fs  %s" % (s.get("name"), len(sb),
                                                 time.time() - t0, codec_of(sb)))
        except Exception as e:
            print("%-18s ERR %s" % (s.get("name"), str(e)[:60]))


if __name__ == "__main__":
    main()
