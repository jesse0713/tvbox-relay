#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""临时诊断：检查中转 /vod/s 与上游分片的 Content-Type。"""
import base64
import json
import urllib.parse
import urllib.request

R = "http://127.0.0.1:8080"
UA = ("Mozilla/5.0 (Linux; Android 10; SM-A505F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36")


def get(u, t=45):
    req = urllib.request.Request(u, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=t)


def main():
    j = json.loads(get(R + "/tv.json", 60).read().decode("utf-8"))
    site = [s for s in j["sites"] if "量子" in (s.get("name") or "")][0]
    api = site["api"]
    body = json.loads(get(api.split("?")[0] + "?ac=detail&pg=1", 40).read().decode("utf-8-sig"))
    it = (body.get("list") or [None])[0]
    raw = str(it.get("vod_play_url") or "")
    first = raw.split("$$$")[0].split("#")[0]
    u = first.split("$", 1)[1].strip() if "$" in first else ""
    m3u = get(u, 45).read().decode("utf-8", "ignore")
    if "#EXT-X-STREAM-INF" in m3u:
        var = [l.strip() for l in m3u.splitlines() if l.strip() and not l.startswith("#")][0]
        m3u = get(urllib.parse.urljoin(u, var), 45).read().decode("utf-8", "ignore")
    segs = [l.strip() for l in m3u.splitlines() if l.strip() and not l.startswith("#")]
    surl = urllib.parse.urljoin(u, segs[0])
    r = get(surl, 45)
    data = r.read()
    print("relay /vod/s  status=%d  CT=%r  CL=%r  AR=%r  bytes=%d"
          % (r.status, r.headers.get("Content-Type"), r.headers.get("Content-Length"),
             r.headers.get("Accept-Ranges"), len(data)))
    q = urllib.parse.urlsplit(surl).query
    up = None
    for p in q.split("&"):
        if p.startswith("u="):
            s = p[2:]
            up = base64.urlsafe_b64decode((s + "=" * (-len(s) % 4)).encode()).decode()
    if up:
        rr = get(up, 45)
        print("upstream     CT=%r  CL=%r" % (rr.headers.get("Content-Type"), rr.headers.get("Content-Length")))


if __name__ == "__main__":
    main()
