#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""临时诊断：对比 中转 /vod/s 与上游直取的分片，解析 TS 的 PMT 看音视频流。"""
import base64
import hashlib
import json
import urllib.parse
import urllib.request

R = "http://127.0.0.1:8080"
UA = ("Mozilla/5.0 (Linux; Android 10; SM-A505F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36")


def get(u, t=45):
    return urllib.request.urlopen(urllib.request.Request(
        u, headers={"User-Agent": UA, "Accept-Encoding": "identity"}), timeout=t).read()


def b64d(s):
    return base64.urlsafe_b64decode((s + "=" * (-len(s) % 4)).encode("ascii")).decode("utf-8")


def b64e(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def ts_streams(data):
    """解析第一个 PMT，返回 [(pid, stream_type)]。"""
    # 收集 pid -> payload 的 PAT(0x00) 和 PMT
    pmt_pids = []
    streams = []
    for i in range(0, len(data) - 187, 188):
        if data[i] != 0x47:
            continue
        pid = ((data[i + 1] & 0x1F) << 8) | data[i + 2]
        pusi = (data[i + 1] & 0x40) != 0
        afc = (data[i + 3] >> 4) & 0x3
        off = i + 4
        if afc in (2, 3):
            alen = data[i + 4]
            off = i + 5 + alen
        if not pusi or off >= i + 188:
            continue
        payload = data[off:i + 188]
        if pid == 0:
            # PAT: pointer_field
            if len(payload) < 8:
                continue
            ptr = payload[0]
            p = payload[1 + ptr:]
            if len(p) < 8:
                continue
            # table_id(1) section_len(2) tsid(2) ver(1) sec(1) last(1) then program loop
            for j in range(8, len(p) - 3, 4):
                prog = (p[j] << 8) | p[j + 1]
                pmtpid = ((p[j + 2] & 0x1F) << 8) | p[j + 3]
                if prog != 0:
                    pmt_pids.append(pmtpid)
        elif pid in pmt_pids:
            if len(payload) < 12:
                continue
            ptr = payload[0]
            p = payload[1 + ptr:]
            if len(p) < 12:
                continue
            # table_id(1) section_len(2) prog(2) ver(1) sec(1) last(1) pcr(2) pilen(2)
            pilen = ((p[10] & 0x0F) << 8) | p[11]
            j = 12 + pilen
            while j + 4 < len(p):
                st = p[j]
                epid = ((p[j + 1] & 0x1F) << 8) | p[j + 2]
                eslen = ((p[j + 3] & 0x0F) << 8) | p[j + 4]
                streams.append((epid, st))
                j += 5 + eslen
    return streams


def main():
    j = json.loads(get(R + "/tv.json", 60).decode("utf-8"))
    site = [s for s in j.get("sites") or [] if "量子" in (s.get("name") or "")][0]
    api = site["api"]
    body = json.loads(get(api.split("?")[0] + "?ac=detail&pg=1", 40).decode("utf-8-sig"))
    it = (body.get("list") or [None])[0]
    raw = str(it.get("vod_play_url") or "")
    first = raw.split("$$$")[0].split("#")[0]
    purl = first.split("$", 1)[1].strip() if "$" in first else ""
    print("playlist via relay:", purl[:90])
    m3u = get(purl, 45).decode("utf-8", "ignore")
    print("---- relay m3u8 ----")
    for ln in m3u.splitlines()[:8]:
        print("  " + ln[:150])
    if "#EXT-X-STREAM-INF" in m3u:
        var = [l.strip() for l in m3u.splitlines() if l.strip() and not l.startswith("#")][0]
        m3u = get(urllib.parse.urljoin(purl, var), 45).decode("utf-8", "ignore")
        print("---- variant m3u8 ----")
        for ln in m3u.splitlines()[:8]:
            print("  " + ln[:150])
    segs = [l.strip() for l in m3u.splitlines() if l.strip() and not l.startswith("#")]
    surl_relay = urllib.parse.urljoin(purl, segs[0])
    print("seg relay:", surl_relay[:100])
    data_relay = get(surl_relay, 45)
    print("relay seg: %d bytes md5=%s" % (len(data_relay), hashlib.md5(data_relay).hexdigest()[:12]))
    # 解出上游地址：/vod/s?u=<b64>
    up = None
    q = urllib.parse.urlsplit(surl_relay).query
    for part in q.split("&"):
        if part.startswith("u="):
            up = b64d(part[2:])
    print("upstream seg:", up[:100] if up else "?")
    if up:
        data_up = get(up, 45)
        print("upstream seg: %d bytes md5=%s" % (len(data_up), hashlib.md5(data_up).hexdigest()[:12]))
        print("same:", data_relay == data_up)
        st = ts_streams(data_relay)
        print("TS streams (pid, type):", [(hex(p), hex(t)) for p, t in st])
        # 0x1b=H264, 0x0f=AAC, 0x24=HEVC
        kinds = []
        for pid, t in st:
            kinds.append({0x1b: "H264视频", 0x0f: "AAC音频", 0x24: "HEVC视频", 0x03: "MP3音频", 0x81: "AC3"}.get(t, "type0x%02x" % t))
        print("stream kinds:", kinds)
        # 统计各类 PID 的包数
        counts = {}
        for i in range(0, len(data_relay) - 187, 188):
            if data_relay[i] != 0x47:
                continue
            pid = ((data_relay[i + 1] & 0x1F) << 8) | data_relay[i + 2]
            counts[pid] = counts.get(pid, 0) + 1
        print("packet counts by pid:", {hex(k): v for k, v in sorted(counts.items())})


if __name__ == "__main__":
    main()
