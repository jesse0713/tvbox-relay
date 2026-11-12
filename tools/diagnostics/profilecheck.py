#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""临时诊断：检查分片里 H.264 的 profile（8bit 还是 10bit），以及 playlist 是否有 ENDLIST。"""
import base64
import json
import urllib.parse
import urllib.request

R = "http://127.0.0.1:8080"
UA = ("Mozilla/5.0 (Linux; Android 10; SM-A505F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36")

PROFILES = {66: "Baseline(8bit)", 77: "Main(8bit)", 88: "Extended", 100: "High(8bit)",
            110: "High10(10bit!)", 122: "High422", 244: "High444"}


def get(u, t=45):
    req = urllib.request.Request(u, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=t)


def find_sps_profile(data):
    # 找 00 00 01 67 (SPS NAL)，取下一个字节 profile_idc
    idx = data.find(b"\x00\x00\x01\x67")
    if idx < 0:
        idx = data.find(b"\x00\x00\x00\x01\x67")
        if idx < 0:
            return None
        return data[idx + 5]
    return data[idx + 4]


def main():
    j = json.loads(get(R + "/tv.json", 60).read().decode("utf-8"))
    for kw in ("量子", "电影天堂", "极速"):
        site = [s for s in j["sites"] if kw in (s.get("name") or "")][0]
        try:
            api = site["api"]
            body = json.loads(get(api.split("?")[0] + "?ac=detail&pg=1", 40).read().decode("utf-8-sig"))
            it = (body.get("list") or [None])[0]
            raw = str(it.get("vod_play_url") or "")
            first = raw.split("$$$")[0].split("#")[0]
            u = first.split("$", 1)[1].strip() if "$" in first else ""
            m3u = get(u, 45).read().decode("utf-8", "ignore")
            has_endlist = "#EXT-X-ENDLIST" in m3u
            if "#EXT-X-STREAM-INF" in m3u:
                var = [l.strip() for l in m3u.splitlines() if l.strip() and not l.startswith("#")][0]
                m3u = get(urllib.parse.urljoin(u, var), 45).read().decode("utf-8", "ignore")
                has_endlist = has_endlist or ("#EXT-X-ENDLIST" in m3u)
            segs = [l.strip() for l in m3u.splitlines() if l.strip() and not l.startswith("#")]
            surl = urllib.parse.urljoin(u, segs[0])
            data = get(surl, 45).read()
            prof = find_sps_profile(data)
            print("%-12s ENDLIST=%s  分片=%d字节  H264profile=%s"
                  % (kw, has_endlist, len(data),
                     PROFILES.get(prof, str(prof)) if prof else "没找到SPS"))
        except Exception as e:
            print("%-12s ERR %s" % (kw, str(e)[:70]))


if __name__ == "__main__":
    main()
