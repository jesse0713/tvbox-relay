#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""点播站点体检与排序（给 TVBox 用）

TVBox 的点播站分两种：
  · 直连型：采集接口直接给出 m3u8 / mp4 —— 可以在地面直接测「要多少带宽 / 实际多少」
  · 解析型：接口只给出 /share/xxx、/play/xxx 这类网页，真实地址要 TVBox 内置爬虫现算
    —— 地面拿不到流，但能测「解析页所在服务器的归属地和时延抖动」，这决定了成片 CDN 大概在哪

排序规则（与用户约定一致）：
  1. 先筛可用：接口能取到片子
  2. 再按片源质量：国内 CDN > 海外；能实测的按「余量」排，解析型按「抖动、时延」排
  3. 余量 = 实测下载速度 / 播放所需带宽，>= 1.5 才算不卡

用法：
  python tools/vod-scan.py                  # 全量，写 relay/vod-rank.json
  python tools/vod-scan.py --limit 5        # 调试
  python tools/vod-scan.py --quiet          # 每日任务用，只打一行
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import socket
import ssl
import statistics as stats
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCES = os.path.join(ROOT, "sources.json")
OUT_DEFAULT = os.path.join(ROOT, "relay", "vod-rank.json")

UA = ("Mozilla/5.0 (Linux; Android 10; SM-A505F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

MARGIN_OK = 1.5
SEG_BYTES_CAP = 6 * 1024 * 1024
SEG_SECONDS_CAP = 8
WORKERS = 8

TIER_NAME = {
    1: "直连可测·余量达标",
    2: "直连可测·余量不足",
    3: "解析型·片源在国内",
    4: "解析型·片源在海外",
    5: "接口不可用",
}


def log(msg="", quiet=False):
    if not quiet:
        print(msg, flush=True)


def http(url, timeout=10, limit=None, deadline=None, headers=None):
    url = urllib.parse.quote(url, safe=":/?&=%#+~")
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    t0 = time.time()
    with urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                timeout=timeout, context=CTX) as r:
        ttfb = time.time() - t0
        buf, total = [], 0
        while True:
            b = r.read(65536)
            if not b:
                break
            buf.append(b)
            total += len(b)
            if limit and total >= limit:
                break
            if deadline and time.time() - t0 >= deadline:
                break
        return b"".join(buf), ttfb, time.time() - t0, r.geturl()


def load_jsm():
    src = json.load(open(SOURCES, "r", encoding="utf-8-sig"))
    active = src["video"]["active"]
    item = next(x for x in src["video"]["items"] if x["id"] == active)
    body, _, _, _ = http(item["url"], timeout=25)
    return json.loads(body.decode("utf-8-sig")), item


def site_entries(j):
    out = []
    for s in j.get("sites") or []:
        api = str(s.get("api") or "")
        if not api.lower().startswith("http"):
            continue
        if re.search(r"\.(js|json)(\?|$)", api):
            continue
        out.append(s)
    return out


def api_test_urls(api):
    base, _, query = api.strip().partition("?")
    keep = [p for p in query.split("&") if p and not p.startswith("ac=")]
    suffix = ("&" + "&".join(keep)) if keep else ""
    return [base + "?ac=videolist&pg=1" + suffix,
            base + "?ac=detail&pg=1" + suffix,
            base + "?ac=videolist" + suffix]


def decode(raw):
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", "ignore")


def split_play(raw):
    """vod_play_url 形如 '第01集$url#第02集$url$$$线路2$url'，$$$ 是多线路分隔。"""
    out = []
    for grp in str(raw).split("$$$"):
        for x in grp.split("#"):
            x = x.strip()
            if "http" in x:
                out.append(x)
    return out


def parse_items(text):
    t = text.lstrip("\ufeff \r\n\t")
    items = []
    if t.startswith("{") or t.startswith("["):
        try:
            j = json.loads(t)
        except Exception:
            return []
        lst = j.get("list") if isinstance(j, dict) else j
        for it in (lst or [])[:6]:
            items.append({"name": it.get("vod_name") or "",
                          "play": split_play(it.get("vod_play_url") or "")})
        return items
    for block in re.findall(r"<video>(.*?)</video>", text, re.S):
        nm = re.search(r"<name>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</name>", block, re.S)
        dd = re.search(r"<dd>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</dd>", block, re.S)
        if dd:
            items.append({"name": nm.group(1).strip() if nm else "",
                          "play": split_play(dd.group(1))})
    return items


def pick_url(entry):
    return entry.split("$", 1)[1].strip() if "$" in entry else entry.strip()


def host_of(url):
    m = re.match(r"https?://([^/:]+)", url or "")
    return m.group(1) if m else None


def ip_of(host):
    try:
        return socket.gethostbyname(host) if host else None
    except Exception:
        return None


def rtt_stats(ip, port=443, n=6):
    times = []
    for _ in range(n):
        t0 = time.time()
        try:
            s = socket.create_connection((ip, port), timeout=4)
            s.close()
            times.append((time.time() - t0) * 1000)
        except Exception:
            times.append(None)
    good = [t for t in times if t]
    if not good:
        return None, None
    return round(stats.median(good)), round(max(good) - min(good))


def measure_direct(url, referer):
    ref = {"Referer": referer} if referer else None
    if ".m3u8" in url.lower():
        body, ttfb, _, final = http(url, timeout=10, limit=512 * 1024, headers=ref)
        text = body.decode("utf-8", "ignore")
        if "#EXTM3U" not in text:
            raise ValueError("不是 m3u8")
        base = url[: url.rfind("/") + 1]
        if "#EXT-X-STREAM-INF" in text:
            lines = text.splitlines()
            best = None
            for i, ln in enumerate(lines):
                if ln.startswith("#EXT-X-STREAM-INF"):
                    m = re.search(r"BANDWIDTH=(\d+)", ln)
                    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    if not nxt or nxt.startswith("#"):
                        continue
                    bw = int(m.group(1)) if m else 0
                    if best is None or bw > best[0]:
                        best = (bw, nxt)
            if best:
                url = best[1] if best[1].startswith("http") else base + best[1]
                base = url[: url.rfind("/") + 1]
                body, ttfb, _, final = http(url, timeout=10, limit=512 * 1024, headers=ref)
                text = body.decode("utf-8", "ignore")
        segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text)]
        if not segs:
            raise ValueError("空播放表")
        seg_len = max(durs) if durs else None
        picks = segs[-2:] if len(segs) >= 2 else segs
        worst = None
        for s in picks:
            su = s if s.startswith("http") else base + s
            data, _, dt, _ = http(su, timeout=12, limit=SEG_BYTES_CAP,
                                  deadline=SEG_SECONDS_CAP, headers=ref)
            got = len(data) * 8 / max(dt, .001) / 1e6
            need = len(data) * 8 / seg_len / 1e6 if seg_len else None
            margin = got / need if need else None
            if worst is None or (margin is not None and margin < worst[2]):
                worst = (got, need, margin)
        got, need, margin = worst
        return {"kind": "hls", "seg_len": seg_len, "got": round(got, 2),
                "need": round(need, 2) if need else None,
                "margin": round(margin, 2) if margin else None, "final": final}
    hh = {"Range": "bytes=0-%d" % (SEG_BYTES_CAP - 1)}
    if referer:
        hh["Referer"] = referer
    data, _, dt, final = http(url, timeout=12, limit=SEG_BYTES_CAP,
                              deadline=SEG_SECONDS_CAP, headers=hh)
    got = len(data) * 8 / max(dt, .001) / 1e6
    return {"kind": "mp4", "got": round(got, 2), "need": None,
            "margin": round(got / 4.0, 2), "final": final}


def scan_site(s):
    rec = {"key": s.get("key"), "name": s.get("name") or "", "api": s.get("api"),
           "tier": 5, "kind": "", "ok": False, "note": ""}
    t0 = time.time()
    try:
        items, ttfb = [], None
        for u in api_test_urls(s["api"]):
            try:
                body, ttfb, _, _ = http(u, timeout=10, limit=2 * 1024 * 1024)
            except Exception:
                continue
            items = parse_items(decode(body))
            if items:
                break
        rec["api_ms"] = int((time.time() - t0) * 1000)
        if not items:
            rec["note"] = "采集接口无数据"
            return rec
        rec["ok"] = True
        m0 = re.match(r"https?://[^/]+", str(s.get("api") or ""))
        ref = m0.group(0) + "/" if m0 else None

        cands = []
        for it in items[:4]:
            for entry in it["play"][:4]:
                u = pick_url(entry)
                if not u.lower().startswith("http"):
                    continue
                direct = (".m3u8" in u.lower()
                          or bool(re.search(r"\.(mp4|mkv|flv)(\?|$)", u.lower())))
                cands.append({"kind": "direct" if direct else "parse",
                              "url": u, "host": host_of(u), "item": it["name"]})
        if not cands:
            rec["note"] = "没有可识别播放地址"
            return rec

        directs = [c for c in cands if c["kind"] == "direct"]
        if directs:
            measured = []
            for c in directs[:3]:
                try:
                    measured.append((c, measure_direct(c["url"], ref)))
                except Exception as e:
                    rec.setdefault("direct_err", type(e).__name__)
            if measured:
                # 取最差的那一片，宁严勿松
                measured.sort(key=lambda x: (x[1].get("margin") or 0))
                c, m = measured[0]
                rec.update({"kind": "direct", "host": c["host"], "sample": c["url"],
                            "ip": ip_of(host_of(m.get("final") or c["url"])),
                            "got": m["got"], "need": m["need"], "margin": m["margin"],
                            "seg_len": m.get("seg_len"), "final": m.get("final")})
                if rec["ip"]:
                    rec["rtt_ms"], rec["jitter_ms"] = rtt_stats(rec["ip"])
                rec["tier"] = 1 if (m["margin"] or 0) >= MARGIN_OK else 2
                rec["note"] = "实测 %.2f Mbps / 需 %s / 余量 %s" % (
                    m["got"], m["need"] or "-", m["margin"])
                return rec
            rec["note"] = "直连取流失败(%s)" % rec.get("direct_err", "异常")

        c = cands[0]
        rec.update({"kind": "parse", "host": c["host"], "sample": c["url"],
                    "ip": ip_of(c["host"]), "tier": 3})
        if rec["ip"]:
            rec["rtt_ms"], rec["jitter_ms"] = rtt_stats(rec["ip"])
        rec["note"] = rec["note"] or ("解析页，需爬虫现算（%s）" % (c["host"] or ""))
        return rec
    except Exception as e:
        rec["note"] = "%s: %s" % (type(e).__name__, str(e)[:60])
        return rec
    finally:
        rec["ms"] = int((time.time() - t0) * 1000)


def geo_batch(ips):
    ips = [x for x in dict.fromkeys(ips) if x]
    out = {}
    for i in range(0, len(ips), 90):
        chunk = ips[i:i + 90]
        data = json.dumps([{"query": ip} for ip in chunk]).encode()
        req = urllib.request.Request(
            "http://ip-api.com/batch?fields=query,countryCode,country,isp,as",
            data=data, headers={"Content-Type": "application/json"})
        try:
            for r in json.loads(urllib.request.urlopen(req, timeout=15).read()):
                out[r.get("query")] = r
        except Exception:
            pass
        time.sleep(1.2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    quiet = args.quiet

    jsm, item = load_jsm()
    sites = site_entries(jsm)
    if args.limit:
        sites = sites[: args.limit]
    log("视频源：%s ｜ 可测站点 %d 个（其余为爬虫型，只能电视上实测）" % (item["name"], len(sites)), quiet)

    results, t0 = [], time.time()
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = [ex.submit(scan_site, s) for s in sites]
        for i, f in enumerate(cf.as_completed(futs), 1):
            r = f.result()
            geo = None
            results.append(r)
            log("[%2d/%2d] %-22s %s" % (i, len(sites), r["name"][:22],
                                        r["note"] or ("tier%d" % r["tier"])), quiet)

    geo = geo_batch([r.get("ip") for r in results])
    for r in results:
        g = geo.get(r.get("ip") or "") or {}
        r["country"] = g.get("countryCode") or ""
        r["asn"] = (g.get("as") or "")[:44]
        r["cn"] = r["country"] == "CN"
        if r["kind"] == "parse" and r["ok"]:
            r["tier"] = 3 if r["cn"] else 4

    def key(r):
        m = r.get("margin") or 0
        jitter = r.get("jitter_ms")
        jitter = 9999 if jitter is None else jitter
        rtt = r.get("rtt_ms") or 9999
        if r["tier"] == 1:
            return (1, -m, jitter, rtt)
        if r["tier"] == 2:
            return (2, -m, jitter, rtt)
        if r["tier"] == 3:
            return (3, 0, jitter, rtt)
        if r["tier"] == 4:
            return (4, 0, jitter, rtt)
        return (5, 0, 0, 0)

    results.sort(key=key)
    payload = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": item["name"],
        "rule": "先筛接口可用；直连型按实测余量(>=%.1f)排；解析型按片源归属地(CN优先)、抖动、时延排" % MARGIN_OK,
        "counts": {str(k): sum(1 for r in results if r["tier"] == k) for k in TIER_NAME},
        "order": [r["key"] for r in results],
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    log("")
    for tier in TIER_NAME:
        rs = [r for r in results if r["tier"] == tier]
        if not rs:
            continue
        log("=== %s（%d）===" % (TIER_NAME[tier], len(rs)), quiet)
        for r in rs:
            extra = ""
            if tier in (1, 2):
                extra = "实测 %5.2f Mbps 需 %-5s 余量 %-5s" % (r.get("got") or 0, r.get("need") or "-", r.get("margin"))
            else:
                extra = "%s %s RTT %-5s 抖动 %-5s" % (r.get("country") or "?", (r.get("asn") or "")[:24],
                                                      r.get("rtt_ms") or "-", r.get("jitter_ms") or "-")
            log("  %-24s %s" % (r["name"][:24], extra), quiet)
        log("")
    log("耗时 %.0f 秒 → %s" % (time.time() - t0, args.out), quiet)
    if quiet:
        c = payload["counts"]
        print("vod-scan: " + " ".join("t%d=%s" % (k, c.get(str(k), 0)) for k in TIER_NAME))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
