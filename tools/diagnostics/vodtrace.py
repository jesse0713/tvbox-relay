#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""临时诊断：追一条点播播放链，看 m3u8 结构、分片是否能取、编码是否 HEVC。"""
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

R = "http://127.0.0.1:8080"
UA = ("Mozilla/5.0 (Linux; Android 10; SM-A505F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36")


def get(u, t=60, referer=None):
    h = {"User-Agent": UA, "Accept-Encoding": "identity"}
    if referer:
        h["Referer"] = referer
    r = urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=t)
    return r.read()


def b64d(s):
    return base64.urlsafe_b64decode((s + "=" * (-len(s) % 4)).encode("ascii")).decode("utf-8")


def log(msg):
    print(msg, flush=True)


def main():
    j = json.loads(get(R + "/tv.json", 60).decode("utf-8"))
    sites = [s for s in j.get("sites") or [] if "/vod/cms/" in (s.get("api") or "")]
    log("wrapped cms sites: %d" % len(sites))
    for s in sites[:8]:
        api = s["api"]
        try:
            detail = api.split("?")[0] + "?ac=detail&pg=1"
            body = json.loads(get(detail, 40).decode("utf-8-sig"))
            lst = body.get("list") or []
            if not lst:
                continue
            it = lst[0]
            raw = str(it.get("vod_play_url") or "")
            grp = raw.split("$$$")[0]
            first = grp.split("#")[0]
            # '第01集$http://...' 或直接 http
            if "$" in first:
                u = first.split("$", 1)[1].strip()
            else:
                u = first.strip()
            if not u.startswith("http"):
                continue
            log("=== site %s | %s" % (s.get("name"), it.get("vod_name")))
            log("play url: %s" % u[:110])
            # 取 master
            t0 = time.time()
            m3u = get(u, 60).decode("utf-8", "ignore")
            log("master %.1fs %d lines" % (time.time() - t0, len(m3u.splitlines())))
            for ln in m3u.splitlines()[:12]:
                log("  M " + ln[:140])
            # 取 variant
            var_url = None
            for ln in m3u.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    var_url = urllib.parse.urljoin(u, ln)
                    break
            if not var_url:
                log("  no variant")
                break
            t0 = time.time()
            vm = get(var_url, 60).decode("utf-8", "ignore")
            log("variant %.1fs url=%s" % (time.time() - t0, var_url[:100]))
            codecs = ""
            for ln in vm.splitlines()[:30]:
                log("  V " + ln[:150])
            segs = [l.strip() for l in vm.splitlines() if l.strip() and not l.startswith("#")]
            if segs:
                seg = urllib.parse.urljoin(var_url, segs[0])
                t0 = time.time()
                sb = get(seg, 60, referer=var_url)
                log("segment0 %.1fs %d bytes head=%s" % (time.time() - t0, len(sb), sb[:24].hex()))
                # 检查 TS 里是否带视频：粗略看 PAT/PMT 或 H265/H264
                hints = []
                for pat in (b"\x00\x00\x01\x65", b"\x00\x00\x01\x41",  # h264 idr / aac?
                            b"\x47\x40", b"\x47\x01"):  # 188-byte TS sync + pid
                    if pat in sb[:4000]:
                        hints.append(pat.hex())
                log("  hints=%s" % hints)
            break
        except Exception as e:
            log("site %s ERR: %s" % (s.get("name"), str(e)[:110]))


if __name__ == "__main__":
    main()
