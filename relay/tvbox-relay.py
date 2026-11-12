#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TVBox 本机中转服务

电视只认 http://<本机IP>:8080/tv.json 和 /live.m3u，永不改变。
真正用哪条源由 sources.json 的 active 决定（每日检测脚本会自动改），
服务每次请求都会重新读取，所以换源不需要重启、也不需要动电视。

   /            状态页（浏览器可看）
   /tv.json     当前视频源配置（含自动故障切换）
   /live.m3u    当前直播源
   /status.json 最近一次体检结果
   /vod/cms/…   点播接口：包一层采集站 API，把播放地址改写成中转地址
   /vod/p?u=…   点播播放表（m3u8）：改写分片地址、顺带解析分享页
   /vod/s?u=…   点播分片：多线程预取到本地缓存，再用内网速度喂给电视
   /其它路径     反代到当前视频源的同一路径（spider.jar / lib / js 等）

点播加速（/vod/*）为什么要有：
  这些免费采集站的正片几乎全放在海外机房（美国、BVI），晚高峰从家宽过去
  「一条连接」只有 0.5~0.9 Mbps，而片源本身要 1~2 Mbps —— 播放器于是
  播几秒、缓冲几秒，永远追不上。实测同一条线路开 24~32 条连接能到 6~9 Mbps，
  所以由中转并行把整集拉到本地，再按内网速度（千兆）喂给电视。
"""

import base64
import hashlib
import json
import socket
import os
import sys
import threading
import time
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCES = os.path.join(ROOT, "sources.json")
STATUS = os.path.join(ROOT, "status.json")
CACHE = os.path.join(HERE, "cache")
RANK = os.path.join(HERE, "vod-rank.json")
LOG = os.path.join(HERE, "relay.log")
PORT = int(os.environ.get("TVBOX_RELAY_PORT", "8080"))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


LAN_IP = lan_ip()
UA = "Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
ASSET_TTL = 86400
CFG_TTL = 120

# ---- 点播加速参数（可用环境变量改，不用改代码） ----
VODCACHE = os.path.join(HERE, "vodcache")
SEGDIR = os.path.join(VODCACHE, "s")
VOD_CONNS = max(2, int(os.environ.get("TVBOX_VOD_CONNS", "28")))      # 同时拉多少条连接
VOD_CACHE_MB = max(200, int(os.environ.get("TVBOX_VOD_CACHE_MB", "4096")))  # 缓存上限
VOD_WAIT = float(os.environ.get("TVBOX_VOD_WAIT", "30"))              # 分片没下完时最多等几秒
VOD_UA = ("Mozilla/5.0 (Linux; Android 10; SM-A505F) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36")

os.makedirs(CACHE, exist_ok=True)
os.makedirs(SEGDIR, exist_ok=True)

_lock = threading.Lock()
_src_cache = {"mtime": 0, "data": None}
_pick = {"video": None, "live": None, "ts": 0}
_stats = {"since": time.strftime("%Y-%m-%d %H:%M:%S"), "hits": 0, "errors": 0, "switches": []}


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 2 * 1024 * 1024:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_sources():
    try:
        mt = os.path.getmtime(SOURCES)
    except OSError:
        return {}
    with _lock:
        if _src_cache["mtime"] == mt and _src_cache["data"] is not None:
            return _src_cache["data"]
        try:
            with open(SOURCES, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            log("sources.json unreadable: %s" % e)
            return _src_cache["data"] or {}
        _src_cache["mtime"] = mt
        _src_cache["data"] = data
        return data


def http_get(url, timeout=25):
    req = urllib.request.Request(_quote(url), headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.status, dict(r.headers)


def ordered(items, active_id):
    first = [x for x in items if x.get("id") == active_id]
    rest = [x for x in items if x.get("id") != active_id]
    return first + rest


def base_of(url):
    return url[: url.rfind("/") + 1]


def _quote(u):
    """把 URL 里的非 ASCII 字符转成百分号编码，避免 urllib 报 ascii 编码错
    （典型：暴風等源的正片地址带「第01集」这类中文）。"""
    try:
        return urllib.parse.quote(u, safe=":/?#[]@!$&'()*+,;=%~")
    except Exception:
        return u


def pick_video(force=False):
    """返回 (item, body_bytes)。active 优先，失败自动降级到备源。"""
    src = load_sources()
    items = (src.get("video") or {}).get("items") or []
    active = (src.get("video") or {}).get("active")
    if not items:
        return None, None
    cached = _pick["video"]
    if (not force) and cached and (time.time() - _pick["ts"] < CFG_TTL) and (time.time() - cached["at"] < CFG_TTL):
        return cached["item"], cached["body"]
    for it in ordered(items, active):
        try:
            body, code, _ = http_get(it["url"])
            if code >= 400:
                continue
            j = json.loads(strip_json_comments(body.decode("utf-8-sig")))
            if not j.get("sites"):
                continue
            if it.get("id") != active:
                _stats["switches"].append({"time": time.strftime("%H:%M:%S"), "to": it.get("name")})
                log("video failover -> %s (%s)" % (it.get("name"), it.get("id")))
            sp = j.get("spider") or ""
            msp = re.search(r"md5;([0-9a-fA-F]{32})", str(sp))
            _pick["spider_md5"] = msp.group(1).lower() if msp else None
            _pick["video"] = {"item": it, "body": body, "at": time.time()}
            _pick["ts"] = time.time()
            return it, body
        except Exception as e:
            log("video GET fail %s: %s" % (it.get("url"), e))
            continue
    _stats["errors"] += 1
    return None, None


def pick_live(force=False):
    """优先返回本机生成的优选列表 relay/live.m3u，没有才回退到远端源。"""
    lp = os.path.join(HERE, "live.m3u")
    try:
        if os.path.exists(lp):
            body = open(lp, "rb").read()
            if b"EXTINF" in body:
                return {"id": "local", "name": "本机优选", "url": "local:live.m3u"}, body
    except Exception as e:
        log("local live read fail: %s" % e)
    src = load_sources()
    items = (src.get("live") or {}).get("items") or []
    active = (src.get("live") or {}).get("active")
    if not items:
        return None, None
    cached = _pick["live"]
    if (not force) and cached and (time.time() - cached["at"] < CFG_TTL):
        return cached["item"], cached["body"]
    for it in ordered(items, active):
        try:
            body, code, _ = http_get(it["url"])
            if code >= 400 or b"EXTINF" not in body:
                continue
            if it.get("id") != active:
                _stats["switches"].append({"time": time.strftime("%H:%M:%S"), "to": it.get("name")})
                log("live failover -> %s" % it.get("name"))
            _pick["live"] = {"item": it, "body": body, "at": time.time()}
            return it, body
        except Exception as e:
            log("live GET fail %s: %s" % (it.get("url"), e))
            continue
    _stats["errors"] += 1
    return None, None


_rank_cache = {"mtime": 0, "data": None}


def load_rank():
    """读取 vod-scan.py 产出的站点体检结果（有缓存，文件变了才重读）。"""
    try:
        mt = os.path.getmtime(RANK)
    except OSError:
        return None
    with _lock:
        if _rank_cache["mtime"] == mt:
            return _rank_cache["data"]
        try:
            with open(RANK, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log("vod-rank.json unreadable: %s" % e)
            return _rank_cache["data"]
        _rank_cache["mtime"] = mt
        _rank_cache["data"] = data
        return data


def apply_rank(j):
    """按体检结果重排站点：已知能播且快的在前，已知差的沉底，没测过的保持原顺序。

    分桶规则（和 tools/vod-scan.py 的 tier 对应）：
      1 直连实测余量达标、3 解析型但片源在国内  -> 提到最前
      2 直连实测偏慢                            -> 其次
      未测过的（爬虫型）                        -> 保持原顺序
      4 解析型且片源在海外、5 接口失效            -> 沉到最后
    """
    data = load_rank()
    if not data:
        return j
    tier = {}
    for r in data.get("results") or []:
        if r.get("key"):
            tier[r["key"]] = r.get("tier", 5)
    sites = j.get("sites") or []
    if not sites or not tier:
        return j
    head, mid, slow, tail = [], [], [], []
    for s in sites:
        t = tier.get(s.get("key"))
        if t == 1 or t == 3:
            head.append(s)
        elif t == 2:
            slow.append(s)
        elif t in (4, 5):
            tail.append(s)
        else:
            mid.append(s)
    pos = {k: i for i, k in enumerate(data.get("order") or [])}
    head.sort(key=lambda s: pos.get(s.get("key"), 9999))
    slow.sort(key=lambda s: pos.get(s.get("key"), 9999))
    tail.sort(key=lambda s: pos.get(s.get("key"), 9999))
    j["sites"] = head + slow + mid + tail
    return j


def strip_json_comments(txt):
    """去掉整行的 // 注释。cluntop 的配置会在开头加一段说明注释，
    TVBox 能容忍，但标准 JSON 解析器不行。"""
    out = []
    for line in txt.splitlines():
        if line.strip().startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)

def inject_lives(body):
    """把直播源固定成中转自带的优选列表，避免 TVBox 用配置里自带的旧直播。"""
    try:
        j = json.loads(strip_json_comments(body.decode("utf-8-sig")))
    except Exception:
        return body
    url = "http://%s:%d/live.m3u" % (LAN_IP, PORT)
    old = j.get("lives") or []
    keep = [x for x in old if isinstance(x, dict) and x.get("url") == url]
    lives = []
    ln = os.path.join(HERE, "live-ln.m3u")
    if os.path.exists(ln) and os.path.getsize(ln) > 500:
        lives.append({"name": "辽宁移动IPTV", "type": 0, "url": "http://%s:%d/live-ln.m3u" % (LAN_IP, PORT), "playerType": 0})
    lives.append({"name": "流畅优选", "type": 0, "url": url, "playerType": 0})
    full = os.path.join(HERE, "live-full.m3u")
    if os.path.exists(full) and os.path.getsize(full) > 500:
        lives.append({"name": "全量线路", "type": 0, "url": "http://%s:%d/live-full.m3u" % (LAN_IP, PORT), "playerType": 0})
    v6 = os.path.join(HERE, "live6.m3u")
    if os.path.exists(v6) and os.path.getsize(v6) > 500:
        lives.append({"name": "IPv6 高清", "type": 0, "url": "http://%s:%d/live6.m3u" % (LAN_IP, PORT), "playerType": 0})
    j["lives"] = lives + keep
    j = apply_rank(j)
    # 采集站的接口指到中转，点播分片才能走多线程加速
    j = wrap_vod_sites(j)
    # 给 spider 地址加版本号：上游换 jar 时 TVBox 会当成新文件重新下载，
    # 避免它一直复用本地那个坏掉的旧 jar（表现为“md5缓存失效 / jar load err”）。
    sp = j.get("spider")
    if isinstance(sp, str) and ";md5;" in sp:
        parts = sp.split(";")          # [path, "md5", hash]
        if len(parts) >= 3 and "?v=" not in parts[0]:
            ver = parts[-1][:8]
            if ver:
                parts[0] = parts[0].split("?")[0] + "?v=" + ver
                j["spider"] = ";".join(parts)
    try:
        return json.dumps(j, ensure_ascii=False).encode("utf-8")
    except Exception:
        return body


def cache_get(key):
    """返回 (body, ctype, fresh)。过期也返回，交给调用方决定是否先用旧值再后台刷新。"""
    p = os.path.join(CACHE, key)
    m = p + ".meta"
    if os.path.exists(p) and os.path.exists(m):
        try:
            meta = json.load(open(m, "r", encoding="utf-8"))
            body = open(p, "rb").read()
            fresh = meta.get("exp", 0) > time.time()
            if meta.get("size") and meta["size"] != len(body):
                return None, None, False
            return body, meta.get("ctype", "application/octet-stream"), fresh
        except Exception:
            pass
    return None, None, False


def cache_put(key, body, ctype):
    try:
        with open(os.path.join(CACHE, key), "wb") as f:
            f.write(body)
        with open(os.path.join(CACHE, key + ".meta"), "w", encoding="utf-8") as f:
            json.dump({"exp": time.time() + ASSET_TTL, "ctype": ctype, "size": len(body)}, f)
    except Exception:
        pass


def refresh_asset_async(key, url, ctype):
    def run():
        try:
            body, code, _ = http_get(url, timeout=60)
            if code < 400:
                cache_put(key, body, ctype)
                log("refreshed %s (%d bytes)" % (url.rsplit("/", 1)[-1], len(body)))
        except Exception as e:
            log("refresh fail %s: %s" % (url.rsplit("/", 1)[-1], e))
    t = threading.Thread(target=run, daemon=True)
    t.start()


CTYPES = {
    ".json": "application/json; charset=utf-8",
    ".jar": "application/java-archive",
    ".js": "application/javascript; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".m3u": "audio/x-mpegurl",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".apk": "application/vnd.android.package-archive",
}


# ---------------- 点播加速 ----------------
#
# 思路：把采集站 API 包一层（/vod/cms），把里面每条播放地址都换成中转地址。
# 电视来取播放表时 /vod/p 把 m3u8 里的分片地址也换掉，同时开后台任务用
# VOD_CONNS 条连接把整集拉进 vodcache；电视来取分片时 /vod/s 直接从本地
# 磁盘喂，走的是内网千兆，不再是那条挤爆的国际线路。


def _b64e(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(s):
    s = (s or "").strip()
    return base64.urlsafe_b64decode((s + "=" * (-len(s) % 4)).encode("ascii")).decode("utf-8")


def _vod_url(kind, url):
    return "http://%s:%d/vod/%s?u=%s" % (LAN_IP, PORT, kind, _b64e(url))


def vod_http(url, timeout=30, referer=None):
    """取一个 http 资源，返回 (body, code, final_url, headers)。"""
    h = {"User-Agent": VOD_UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        h["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(_quote(url), headers=h), timeout=timeout) as r:
        return r.read(), r.status, r.geturl(), dict(r.headers)


def _seg_paths(url):
    k = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(SEGDIR, k), os.path.join(SEGDIR, k + ".meta")


def seg_cached(url):
    """已下完的分片返回 (路径, meta)，否则 (None, None)。"""
    p, m = _seg_paths(url)
    try:
        if os.path.exists(p) and os.path.exists(m):
            meta = json.load(open(m, "r", encoding="utf-8"))
            if meta.get("size") == os.path.getsize(p):
                return p, meta
    except Exception:
        pass
    return None, None


def seg_store(url, body, ctype):
    p, m = _seg_paths(url)
    tmp = "%s.%d.part" % (p, threading.get_ident())
    try:
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, p)
        with open(m, "w", encoding="utf-8") as f:
            json.dump({"size": len(body), "ctype": ctype, "url": url, "at": time.time()}, f)
        return p
    except Exception as e:
        log("vod seg store fail: %s" % e)
        return None


_vod_state = {"key": None, "total": 0, "done": 0, "failed": 0, "at": 0,
              "stop": False, "bytes": 0, "served": 0, "episodes": 0}
_vod_guard = threading.Lock()
_vod_done = set()      # 已下好的分片
_vod_bad = set()       # 预取里失败过的分片，别再干等
_vod_ex = None
_vod_ex_guard = threading.Lock()
VOD_FAST_START = max(0, int(os.environ.get("TVBOX_VOD_FAST_START", "6")))  # 开头几段用分段并发抢时间


def _vod_executor():
    global _vod_ex
    with _vod_ex_guard:
        if _vod_ex is None:
            _vod_ex = ThreadPoolExecutor(max_workers=VOD_CONNS, thread_name_prefix="vod")
    return _vod_ex


def _ensure_seg(url, referer=None):
    p, meta = seg_cached(url)
    if p:
        return p, meta
    last = None
    for attempt in range(2):
        try:
            body, code, final, hdrs = vod_http(url, timeout=45, referer=referer)
            if code >= 400:
                raise IOError("upstream %s" % code)
            ctype = hdrs.get("Content-Type") or "video/mp2t"
            if final and final != url and seg_cached(final)[0]:
                url = final
            p = seg_store(url, body, ctype)
            if not p:
                raise IOError("cache write failed")
            with _vod_guard:
                _vod_state["bytes"] += len(body)
            return p, {"size": len(body), "ctype": ctype, "url": url}
        except Exception as e:
            last = e
            time.sleep(0.4 + attempt)
    raise last or IOError("unknown")


def _range_total(url, referer=None, timeout=15):
    """问一下服务器支不支持断点：返回 (总字节数 或 None)。"""
    h = {"User-Agent": VOD_UA, "Accept": "*/*", "Accept-Encoding": "identity", "Range": "bytes=0-0"}
    if referer:
        h["Referer"] = referer
    try:
        with urllib.request.urlopen(urllib.request.Request(_quote(url), headers=h), timeout=timeout) as r:
            m = re.match(r"bytes\s+\d+-\d+/(\d+)", r.headers.get("Content-Range") or "")
            if r.status == 206 and m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def _fetch_split(url, total, referer=None, timeout=60, pool=None):
    """把一个分片切成多段并发拉，开头那几秒才不会干等一条 0.5 Mbps 的连接。"""
    parts_n = max(2, min(VOD_CONNS, int(total // 65536) or 2))
    step = (total + parts_n - 1) // parts_n
    plan = []
    for i in range(0, total, step):
        plan.append((i, min(total - 1, i + step - 1)))
    parts = [None] * len(plan)

    def one(k, a, b):
        h = {"User-Agent": VOD_UA, "Accept": "*/*", "Accept-Encoding": "identity",
             "Range": "bytes=%d-%d" % (a, b)}
        if referer:
            h["Referer"] = referer
        with urllib.request.urlopen(urllib.request.Request(_quote(url), headers=h), timeout=timeout) as r:
            parts[k] = r.read()

    futs = [(pool or _vod_executor()).submit(one, k, a, b) for k, (a, b) in enumerate(plan)]
    for f in futs:
        f.result(timeout=timeout + 10)
    body = b"".join(p for p in parts if p)
    if len(body) != total:
        raise IOError("分段拼不起来：%d/%d" % (len(body), total))
    return body


def _range_get(url, a, b, referer=None, timeout=60):
    h = {"User-Agent": VOD_UA, "Accept": "*/*", "Accept-Encoding": "identity",
         "Range": "bytes=%d-%d" % (a, b)}
    if referer:
        h["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(_quote(url), headers=h), timeout=timeout) as r:
        return r.read()


_vod_prio = None
_vod_prio_guard = threading.Lock()


def _prio_pool():
    """给「电视正在等的这一段」单独一组连接，不跟大部队排队。"""
    global _vod_prio
    with _vod_prio_guard:
        if _vod_prio is None:
            _vod_prio = ThreadPoolExecutor(max_workers=8, thread_name_prefix="vodprio")
    return _vod_prio


def _priority_seg(url, referer=None):
    """抢电视当前要的那一段：能分段就 8 路并发，拿完马上落盘。"""
    p, meta = seg_cached(url)
    if p:
        return p, meta
    try:
        total = _range_total(url, referer, timeout=8)
        if total and 0 < total <= 16 * 1024 * 1024:
            body = _fetch_split(url, total, referer, pool=_prio_pool())
            p = seg_store(url, body, "video/mp2t")
            if p:
                with _vod_guard:
                    _vod_state["bytes"] += len(body)
                    if url not in _vod_done:
                        _vod_done.add(url)
                        _vod_state["done"] += 1
                return p, {"size": len(body), "ctype": "video/mp2t", "url": url}
    except Exception as e:
        log("vod prio fail: %s" % e)
    return None, None


def _fast_start(urls, referer, key):
    """开头几段一起抢：每段切成几份并发下，几秒钟就攒出十几秒的播放量。"""
    if not urls:
        return
    pool = ThreadPoolExecutor(max_workers=VOD_CONNS, thread_name_prefix="vodfast")
    try:
        # 先并发问长度，别一段一段问（问一次要一个来回）
        probes = {u: pool.submit(_range_total, u, referer) for u in urls}
        per = max(2, min(8, VOD_CONNS // max(1, len(urls))))
        plans = []
        for u in urls:
            try:
                total = probes[u].result(timeout=30)
            except Exception:
                total = None
            if not total or total > 16 * 1024 * 1024:
                plans.append((u, None, None))
                continue
            step = (total + per - 1) // per
            fs = [pool.submit(_range_get, u, a, min(total - 1, a + step - 1), referer)
                  for a in range(0, total, step)]
            plans.append((u, total, fs))
        for u, total, fs in plans:
            if _vod_state.get("key") != key or _vod_state.get("stop"):
                return
            try:
                if total is None:
                    _ensure_seg(u, referer)
                else:
                    body = b"".join(f.result(timeout=90) for f in fs)
                    if len(body) != total:
                        raise IOError("分段拼不起来：%d/%d" % (len(body), total))
                    seg_store(u, body, "video/mp2t")
                    with _vod_guard:
                        _vod_state["bytes"] += len(body)
            except Exception as e:
                log("vod fast start fail: %s" % e)
                # 抢不到就交给普通队列重试，别让电视干等
                try:
                    _vod_executor().submit(_vod_download, u, key, referer)
                except Exception:
                    pass
                continue
            with _vod_guard:
                if u not in _vod_done:
                    _vod_done.add(u)
                    _vod_state["done"] += 1
            if seg_cached(u)[0] is None:
                with _vod_guard:
                    _vod_bad.add(u)
    finally:
        pool.shutdown(wait=False)


def _vod_cache_size():
    total = 0
    try:
        for name in os.listdir(SEGDIR):
            if name.endswith(".meta") or name.endswith(".part"):
                continue
            try:
                total += os.path.getsize(os.path.join(SEGDIR, name))
            except OSError:
                pass
    except OSError:
        pass
    return total


def _vod_prune():
    """缓存超了就按修改时间从旧到新删，别把硬盘吃满。"""
    try:
        files = []
        total = 0
        for name in os.listdir(SEGDIR):
            fp = os.path.join(SEGDIR, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if name.endswith(".meta"):
                continue
            files.append((st.st_mtime, fp))
            total += st.st_size
        limit = VOD_CACHE_MB * 1024 * 1024
        if total <= limit:
            return
        files.sort()
        for mt, fp in files:
            if total <= limit * 0.8:
                break
            try:
                size = os.path.getsize(fp)
                os.remove(fp)
                if os.path.exists(fp + ".meta"):
                    os.remove(fp + ".meta")
                total -= size
            except OSError:
                pass
        log("vod cache pruned to %.0f MB" % (total / 1048576.0))
    except Exception as e:
        log("vod prune fail: %s" % e)


def _vod_download(url, key, referer=None):
    if _vod_state.get("key") != key:
        return
    if seg_cached(url)[0]:
        with _vod_guard:
            if url not in _vod_done:
                _vod_done.add(url)
                _vod_state["done"] += 1
        return
    try:
        _ensure_seg(url, referer)
        with _vod_guard:
            _vod_state["done"] += 1
            _vod_done.add(url)
    except Exception as e:
        with _vod_guard:
            _vod_state["failed"] += 1
            _vod_bad.add(url)
        log("vod seg fail %s: %s" % (url.rsplit("/", 1)[-1][:60], e))


def vod_prefetch(playlist_url, segs, referer=None):
    """开后台任务，用 VOD_CONNS 条连接按顺序把整集拉到本地。"""
    key = hashlib.sha1(playlist_url.encode("utf-8")).hexdigest()
    with _vod_guard:
        if _vod_state.get("key") == key and not _vod_state.get("stop"):
            return
        _vod_state.update({"key": key, "total": len(segs), "done": 0, "failed": 0,
                           "at": time.time(), "stop": False})
        _vod_state["episodes"] += 1
        _vod_done.clear()
        _vod_bad.clear()
    log("vod prefetch start: %d 分片" % len(segs))

    def run():
        _vod_prune()
        head = [u for u in segs[:VOD_FAST_START] if not seg_cached(u)[0]]
        if head:
            threading.Thread(target=_fast_start, args=(head, referer, key), daemon=True).start()
        ex = _vod_executor()
        for u in segs[VOD_FAST_START:]:
            if _vod_state.get("key") != key or _vod_state.get("stop"):
                return
            if seg_cached(u)[0]:
                with _vod_guard:
                    if u not in _vod_done:
                        _vod_done.add(u)
                        _vod_state["done"] += 1
                continue
            ex.submit(_vod_download, u, key, referer)

    threading.Thread(target=run, daemon=True).start()


def rewrite_playlist(text, base_url, playlist_url, referer=None):
    """把 m3u8 里的分片/密钥/子播放表地址换成中转地址，并触发预取。"""
    lines = text.splitlines()
    master = any(l.startswith("#EXT-X-STREAM-INF") for l in lines)
    out, segs = [], []
    for ln in lines:
        s = ln.strip()
        if not s:
            out.append(ln)
            continue
        if s.startswith("#"):
            if re.match(r"#EXT-X-(KEY|MAP|MEDIA|PART|PRELOAD-HINT|SESSION-KEY)", s):
                out.append(re.sub(r'URI="([^"]*)"',
                                  lambda m: 'URI="%s"' % _vod_url("p", urllib.parse.urljoin(base_url, m.group(1))), ln))
            else:
                out.append(ln)
            continue
        u = urllib.parse.urljoin(base_url, s)
        if master:
            out.append(_vod_url("p", u))
        else:
            out.append(_vod_url("s", u))
            segs.append(u)
    if segs:
        vod_prefetch(playlist_url, segs, referer)
    return ("\n".join(out) + "\n").encode("utf-8")


def vod_playlist(url, depth=0):
    """取播放表；分享页（/share/xxx）顺手把里面的 m3u8 解析出来。"""
    body, code, final, hdrs = vod_http(url, timeout=30, referer=base_of(url))
    ctype = (hdrs.get("Content-Type") or "").lower()
    head = body[:512].lstrip().lower()
    if head.startswith(b"#extm3u") or "mpegurl" in ctype:
        return rewrite_playlist(body.decode("utf-8", "ignore"), final, url, base_of(url)), "application/vnd.apple.mpegurl"
    if depth < 2 and (head.startswith(b"<") or "html" in ctype):
        txt = body.decode("utf-8", "ignore")
        m = (re.search(r"""["']([^"']*?\.m3u8[^"']*)["']""", txt)
             or re.search(r"""(https?://[^\s"'<>\\]+\.m3u8[^\s"'<>\\]*)""", txt))
        if m:
            real = urllib.parse.urljoin(final, m.group(1).replace("\\/", "/"))
            log("vod share page -> %s" % real[:120])
            return vod_playlist(real, depth + 1)
        raise ValueError("分享页里没找到 m3u8")
    return body, (hdrs.get("Content-Type") or "application/octet-stream")


def rewrite_play_url(raw, base_api):
    """'第01集$https://…' -> '第01集$http://中转/vod/p?u=…'（多线路 $$$ 一并处理）。"""
    groups = []
    for grp in str(raw).split("$$$"):
        eps = []
        for e in grp.split("#"):
            nm, sep, u = e.partition("$")
            if not sep:
                nm, u = "", e
            u = u.strip()
            if u.startswith("http"):
                eps.append("%s$%s" % (nm, _vod_url("p", u)) if nm else _vod_url("p", u))
            elif u:
                eps.append(("%s$%s" % (nm, u)) if nm else u)
        groups.append("#".join([x for x in eps if x]))
    return "$$$".join([g for g in groups if g])


def wrap_vod_sites(j):
    """把采集站（type 0/1 且 http API）的接口指到中转，其它站（JS/PY/网盘/官源）不动。"""
    for s in j.get("sites") or []:
        api = str(s.get("api") or "")
        if not api.startswith("http") or "/vod/cms/" in api:
            continue
        if re.search(r"\.json|\.js(\?|$)|\.xml|at/xml", api):
            continue
        s["api"] = "http://%s:%d/vod/cms/%s" % (LAN_IP, PORT, _b64e(api))
        # 中转的 /vod/p 返回的是直连 m3u8，不再需要 TVBox 的网页嗅探器。
        # 原来带 playerType:2 的站（电影天堂、建安、天涯、金蝉、小绵阳）在电视上
        # 会走 WebView 嗅探，表现为「正在嗅探播放地址 → 有声音没画面 → 嗅探错误」，
        # 这里统一改成 0，让 TVBox 用本机播放器直接播中转给的 m3u8。
        s["playerType"] = 0
    return j


def vod_cms(url_params, query):
    """转发采集站 API，并把返回里的播放地址改写成中转地址。"""
    try:
        upstream = _b64d(url_params)
    except Exception:
        raise ValueError("bad api token")
    base, _, uq = upstream.partition("?")
    keep = [p for p in uq.split("&") if p and not p.startswith("ac=")]
    params = [p for p in (query or "").split("&") if p]
    url = base + ("?" + "&".join(keep + params) if (keep or params) else "")
    sp = urllib.parse.urlsplit(upstream)
    referer = "%s://%s/" % (sp.scheme, sp.netloc)
    body, code, final, hdrs = vod_http(url, timeout=25, referer=referer)
    if code >= 400:
        return code, body, hdrs.get("Content-Type") or "text/plain"
    try:
        j = json.loads(strip_json_comments(body.decode("utf-8-sig")))
    except Exception:
        return 200, body, hdrs.get("Content-Type") or "application/json; charset=utf-8"
    lst = j.get("list") if isinstance(j, dict) else None
    if isinstance(lst, list):
        for it in lst:
            if isinstance(it, dict) and it.get("vod_play_url"):
                it["vod_play_url"] = rewrite_play_url(it["vod_play_url"], final)
    return 200, json.dumps(j, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TVBoxRelay/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_segment(self, url):
        """分片：优先本地缓存（内网速度）；没下完就等预取一会儿，最后才自己拉。"""
        p, meta = seg_cached(url)
        if not p:
            start = time.time()
            prio_done = False
            while time.time() - start < VOD_WAIT:
                p, meta = seg_cached(url)
                if p:
                    break
                if url in _vod_bad:
                    break                      # 预取放弃了，别干等
                waited = time.time() - start
                if not prio_done and waited > 6:
                    # 电视已经等了一会儿：这一段自己抢，不排在大部队后面
                    prio_done = True
                    p, meta = _priority_seg(url)
                    if p:
                        break
                time.sleep(0.15)
        if not p:
            try:
                p, meta = _ensure_seg(url)
            except Exception as e:
                log("vod s fail %s: %s" % (url.rsplit("/", 1)[-1][:60], e))
                return self._send(502, "分片取不到：%s" % e)
        try:
            data = open(p, "rb").read()
        except OSError as e:
            return self._send(502, "缓存读不了：%s" % e)
        with _vod_guard:
            _vod_state["served"] += len(data)
        ctype = (meta or {}).get("ctype") or "video/mp2t"
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if m and (m.group(1) or m.group(2)):
            total = len(data)
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else total - 1
            else:
                start = max(0, total - int(m.group(2)))
                end = total - 1
            if start >= total:
                return self._send(416, b"")
            end = min(end, total - 1)
            chunk = data[start:end + 1]
            try:
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _status_page(self):
        src = load_sources()
        v = (src.get("video") or {}).get("active")
        l = (src.get("live") or {}).get("active")
        vitem = _pick["video"]["item"] if _pick.get("video") else None
        litem = _pick["live"]["item"] if _pick.get("live") else None
        try:
            st = json.load(open(STATUS, "r", encoding="utf-8-sig"))
            last = st.get("time", "-")
        except Exception:
            last = "尚未运行检测"
        rows = [
            ("服务地址", "http://%s:%d/tv.json" % (LAN_IP, PORT)),
            ("视频源 active", "%s（实际：%s）" % (v, vitem.get("name") if vitem else "-")),
            ("直播源 active", "%s（实际：%s）" % (l, litem.get("name") if litem else "-")),
            ("最近体检", last),
            ("启动时间", _stats["since"]),
            ("请求数", str(_stats["hits"])),
            ("错误数", str(_stats["errors"])),
        ]
        rank = load_rank()
        if rank:
            rows.append(("站点排序", "%s（%d 个站已实测）" % (rank.get("time", "-"),
                                                        len(rank.get("results") or []))))
        st = _vod_state
        if st.get("key"):
            done, total = st["done"], st["total"]
            pct = int(done * 100 / total) if total else 0
            rows.append(("点播加速", "预取 %d/%d 分片（%d%%）失败 %d ｜ 已缓存 %.0f MB ｜ 已送出 %.1f GB"
                         % (done, total, pct, st["failed"],
                            _vod_cache_size() / 1048576.0, st["served"] / 1073741824.0)))
            rows.append(("点播连接数", "%d（单条约 0.5 Mbps，合计建议 ≥ 24）" % VOD_CONNS))
        html = ["<!doctype html><meta charset='utf-8'><title>TVBox 中转</title>",
                "<body style='font-family:Microsoft YaHei,sans-serif;padding:24px;line-height:1.9'>",
                "<h2 style='margin:0 0 6px'>TVBox 本机中转运行中</h2>",
                "<div style='color:#888;margin-bottom:14px'>电视端固定地址：<b>http://%s:%d/tv.json</b> ｜ 直播：<b>http://%s:%d/live.m3u</b></div>"
                % (LAN_IP, PORT, LAN_IP, PORT),
                "<table cellpadding=6 style='border-collapse:collapse'>"]
        for k, val in rows:
            html.append("<tr><td style='color:#666;border-bottom:1px solid #eee'>%s</td><td style='border-bottom:1px solid #eee'>%s</td></tr>" % (k, val))
        html.append("</table></body>")
        return "".join(html)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        _stats["hits"] += 1
        path = self.path.split("?")[0]
        try:
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                log("client %s %s %s" % (self.client_address[0], self.command, path))
        except Exception:
            pass
        if path in ("/", "/index.html"):
            return self._send(200, self._status_page(), "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, "ok")
        if path == "/vod/stats":
            st = dict(_vod_state)
            st["cache_mb"] = round(_vod_cache_size() / 1048576.0, 1)
            st["conns"] = VOD_CONNS
            st["since"] = round(time.time() - st.get("at", time.time()), 1)
            return self._send(200, json.dumps(st, ensure_ascii=False), "application/json; charset=utf-8")
        # ---- 点播加速 ----
        if path.startswith("/vod/cms/"):
            token = path[len("/vod/cms/"):].strip("/")
            q = self.path.partition("?")[2]
            try:
                code, body, ctype = vod_cms(token, q)
            except Exception as e:
                log("vod cms fail: %s" % e)
                return self._send(502, "采集接口失败：%s" % e)
            return self._send(code, body, ctype)
        if path in ("/vod/p", "/vod/s"):
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                target = _b64d((q.get("u") or [""])[0])
            except Exception:
                return self._send(400, "bad u")
            if not target.startswith("http"):
                return self._send(400, "bad u")
            if path == "/vod/s":
                return self._serve_segment(target)
            try:
                body, ctype = vod_playlist(target)
            except Exception as e:
                log("vod p fail %s: %s" % (target[:120], e))
                return self._send(502, "播放表取不到：%s" % e)
            return self._send(200, body, ctype)
        if path == "/status.json":
            try:
                with open(STATUS, "rb") as f:
                    return self._send(200, f.read(), "application/json; charset=utf-8")
            except Exception:
                return self._send(404, "no status yet")
        if path in ("/tv.json", "/tvbox.json", "/api.json"):
            it, body = pick_video()
            if body is None:
                return self._send(502, "所有视频源都取不到，请查看 relay.log")
            body = inject_lives(body)
            return self._send(200, body, "application/json; charset=utf-8")
        if path in ("/live-ln.m3u", "/live-ln.m3u8"):
            pln = os.path.join(HERE, "live-ln.m3u")
            if os.path.exists(pln):
                with open(pln, "rb") as f:
                    return self._send(200, f.read(), "audio/x-mpegurl; charset=utf-8")
            return self._send(404, "no liaoning list")
        if path in ("/live-full.m3u", "/live-full.m3u8"):
            pf = os.path.join(HERE, "live-full.m3u")
            if os.path.exists(pf):
                with open(pf, "rb") as f:
                    return self._send(200, f.read(), "audio/x-mpegurl; charset=utf-8")
            return self._send(404, "no full list")
        if path in ("/live6.m3u", "/live6.m3u8"):
            p6 = os.path.join(HERE, "live6.m3u")
            if os.path.exists(p6):
                with open(p6, "rb") as f:
                    return self._send(200, f.read(), "audio/x-mpegurl; charset=utf-8")
            return self._send(404, "ipv6 list not built")
        if path in ("/live.m3u", "/live.m3u8", "/live.txt"):
            it, body = pick_live()
            if body is None:
                return self._send(502, "所有直播源都取不到，请查看 relay.log")
            return self._send(200, body, "audio/x-mpegurl; charset=utf-8")
        # 其余路径：反代到当前视频源同路径（spider.jar、lib、js、json 等）
        it, _ = pick_video()
        if not it:
            return self._send(502, "没有可用视频源")
        url = base_of(it["url"]) + path.lstrip("/")
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        ext = os.path.splitext(path)[1].lower()
        want_md5 = _pick.get("spider_md5") if ext == ".jar" else None
        body, ctype, fresh = cache_get(key)
        if body is not None and want_md5 and hashlib.md5(body).hexdigest() != want_md5:
            log("jar cache stale (%s != %s), refetch" % (hashlib.md5(body).hexdigest()[:8], want_md5[:8]))
            body = None
        if body is not None:
            if not fresh:
                refresh_asset_async(key, url, ctype)
            return self._send(200, body, ctype)
        if body is None:
            try:
                body, code, headers = http_get(url, timeout=40)
                if code >= 400:
                    return self._send(code, "upstream %s" % code)
                ctype = CTYPES.get(ext) or headers.get("Content-Type") or "application/octet-stream"
                if want_md5:
                    got = hashlib.md5(body).hexdigest()
                    if got != want_md5:
                        # 上游可能刚更新，重取一次；仍不一致就直说，交给 TVBox 判断
                        log("jar md5 mismatch: got %s want %s" % (got[:8], want_md5[:8]))
                        body, code2, headers2 = http_get(url, timeout=40)
                        got = hashlib.md5(body).hexdigest()
                        if got != want_md5:
                            log("jar md5 still mismatch after retry")
                        else:
                            log("jar md5 ok after retry")
                cache_put(key, body, ctype)
                log("asset %s %d bytes" % (path, len(body)))
            except Exception as e:
                log("asset fail %s: %s" % (path, e))
                return self._send(502, "资源获取失败：%s" % e)
        return self._send(200, body, ctype)


def main():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        srv.daemon_threads = True
        log("relay listening on 0.0.0.0:%d" % PORT)
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        log("relay crashed: %s" % e)
        log(traceback.format_exc())


if __name__ == "__main__":
    main()
