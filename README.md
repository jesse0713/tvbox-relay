# TVBox 本机中转 · tvbox-relay

给电视一个**永远不用改**的配置地址：背后自动选源、自动降级、点播多连接加速。

```
电视 / TVBox  ──▶  http://<本机IP>:8080/tv.json   ← 地址固定，永不改变
                          │
                          ├─ 主源打不开？自动换备源
                          ├─ 直播列表？合并 + 测速后的优选清单
                          └─ 点播太卡？28 条连接预取整集，再用内网带宽喂电视
```

## 为什么需要它

在国内家庭宽带下用 TVBox，绕不开三个麻烦：

1. **配置地址经常打不开。** `raw.githubusercontent.com`、`cdn.jsdelivr.net` 时通时不通，电视上直接报「配置加载失败」。
2. **源是会坏的。** 免费的采集站和直播线路寿命以周计。源一挂，就得重新找地址、再跑到电视上改一遍。
3. **点播卡在物理线路上。** 免费采集站的正片大多放在海外机房，单条 TCP 连接从家宽过去只有 0.5~0.9 Mbps，而片源要 1~2 Mbps —— 表现就是「播几秒、缓冲几秒」，永远追不上。

这个项目把「取配置」这件事从电视搬到家中的一台常开设备（Windows 主机 / NAS / 软路由）上，用一份 Python 标准库写的中转服务解决上面三条。

## 它做了什么

| 能力 | 实现 |
| --- | --- |
| 电视端地址固定 | 只暴露 `/tv.json` 和 `/live.m3u`，真正用哪条上游由 `sources.json` 的 `active` 决定，服务端热加载，改完不用重启 |
| 源失效自动兜底 | 每条源一主两备；当前源取不到时按顺序试下一个，切换写进 `relay.log`，电视端零感知 |
| 点播多连接加速 | `/vod/cms/*` 把采集站 API 包一层、把播放地址改写成中转地址；`/vod/p` 取播放表时开后台任务，用 28 条连接把整集拉到本地；`/vod/s` 直接从磁盘按内网速度喂给电视 |
| 配置体检 | `tools/daily-check.ps1` 每天真请求采集接口、真拉直播分片，结果写 `status.json`，不可用就自动改 `active` |
| 静态资源反代 | `spider.jar`、`lib/`、`*.js` 等非接口路径按当前源反向代理并缓存（jar 还会校验 md5） |

## 目录结构

```
tvbox-relay/
├─ relay/
│  ├─ tvbox-relay.py     中转服务（纯标准库，无第三方依赖）
│  ├─ 启动中转.cmd        后台启动 / 重启，自动找 Python
│  └─ 放行防火墙.cmd      一次性放行 8080 入站（需要 UAC）
├─ tools/
│  ├─ daily-check.ps1    每日体检 + 自动切换 + 写报告
│  ├─ vod-scan.py        点播站体检与排序（产出 relay/vod-rank.json）
│  ├─ vod-test.py        模拟播放器拉一整集，验证加速效果
│  ├─ probe-live.py      直播源时延 / 分片时长 / 码率 / 实测速度
│  ├─ soak-live.py       直播源持续播放体检，找周期性卡顿
│  ├─ speedfilter.py     按分片速度剔除卡顿线路
│  ├─ build-live.py      合并公开直播源，生成优选清单
│  ├─ build-live6.py     IPv6 高清源（需家宽支持 IPv6）
│  ├─ build-live-ln.py   地区 IPTV 示例（辽宁移动，仅该 ISP 可用）
│  ├─ netprobe.py        本机 / 网关 / 电视 连通性与带宽速查
│  ├─ rebuild-live.cmd   一键重建全部直播清单
│  └─ diagnostics/       单点排查脚本（编码、Content-Type、TS、播放链）
├─ docs/
│  ├─ 部署指南.md         从零到电视能播的完整步骤
│  ├─ 接口说明.md         HTTP 接口与数据结构
│  └─ 常见问题.md         黑屏、卡顿、连不上等排查
├─ sources.json          源清单（主/备、阈值、当前指向）
└─ status.json           体检结果（运行时生成，不入库）
```

## 快速开始（Windows）

**1. 装 Python 3.8+**

到 [python.org](https://www.python.org/downloads/) 下载安装，安装时勾选 **Add python.exe to PATH**。

**2. 取代码**

```bash
git clone https://github.com/jesse0713/tvbox-relay.git
cd tvbox-relay
```

**3. 启动中转**

双击 `relay\启动中转.cmd`（后台运行），或在命令行里：

```bash
python relay/tvbox-relay.py
```

浏览器打开 <http://127.0.0.1:8080/> 能看到状态页就说明起来了。

**4. 让电视连上来**

先查到这台电脑的局域网 IP（`ipconfig`），然后：

| TVBox 里的位置 | 填什么 |
| --- | --- |
| 设置 → 配置地址 | `http://192.168.x.x:8080/tv.json` |
| 直播源 → 添加 | `http://192.168.x.x:8080/live.m3u` |

第一次加载会慢几秒（中转要去上游取配置），之后就快了。

**5. 电视提示连不上？**

双击 `relay\放行防火墙.cmd`，UAC 点「是」，只放行 TCP 8080 入站。多数情况不用这步——先在电视浏览器里打开 `http://192.168.x.x:8080/` 试试，能开就是通的。

完整流程（含开机自启、计划任务、NAS 部署）见 [docs/部署指南.md](docs/部署指南.md)。

## 配置：sources.json

```json
{
  "video": {
    "active": "v3",
    "items": [
      { "id": "v3", "role": "主",  "name": "七星 qist/tvbox · jsm", "url": "https://…/jsm.json" },
      { "id": "v1", "role": "备1", "name": "…", "url": "https://…" }
    ]
  },
  "live": {
    "active": "l1",
    "items": [ … ]
  },
  "thresholds": {
    "video_sites_min": 40,
    "video_cms_alive_min": 0.5,
    "live_alive_min": 0.5
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `video.active` / `live.active` | 当前使用哪条，值必须等于某个 `items[].id` |
| `items[].id` | 稳定标识，体检脚本靠它写回 `active` |
| `items[].role` | 仅用于报告展示（主 / 备1 / 备2） |
| `items[].url` | 配置或 m3u 的直链；GitHub 源建议带加速镜像前缀 |
| `thresholds.video_sites_min` | 站点数低于此值判为异常 |
| `thresholds.video_cms_alive_min` | 采集接口存活率下限（0~1） |
| `thresholds.live_alive_min` | 直播抽测存活率下限（0~1） |

要点：

- 中转**每次请求都重新读取**本文件（按 mtime 缓存），改完立即生效，不用重启，更不用动电视。
- `active` 指向的源取不到时，服务端会按 `items` 顺序自动试下一条，并把切换记到 `relay.log`。
- 想加源就加 `items`，`id` 不要重复；`daily-check.ps1` 会自动把 `active` 调到可用的那条并写回文件。

## HTTP 接口

| 路径 | 作用 |
| --- | --- |
| `/` | 状态页：当前源、体检时间、请求数、点播预取进度 |
| `/health` | 健康检查，返回 `ok` |
| `/tv.json`（`/tvbox.json`、`/api.json`） | 电视要的配置：上游 JSON + 注入直播源 + 站点排序 + 采集接口改写 |
| `/live.m3u`（`/live.m3u8`、`/live.txt`） | 直播列表：优先 `relay/live.m3u`，没有就回退到 `sources.json` 的 live 源 |
| `/live-ln.m3u`、`/live-full.m3u`、`/live6.m3u` | 本机生成的附加清单，文件不存在时返回 404 |
| `/status.json` | 最近一次体检结果（原样返回 `status.json`） |
| `/vod/stats` | 点播加速运行状态（进度、连接数、缓存、已送出字节） |
| `/vod/cms/<b64url>` | 采集站 API 代理，`b64url` 是上游 API 的 URL-safe Base64 |
| `/vod/p?u=<b64url>` | 播放表代理：改写分片地址并触发整集预取 |
| `/vod/s?u=<b64url>` | 分片：命中本地缓存直接读盘，未命中先等下预取再自己拉 |
| 其它路径 | 反代到当前视频源的同一路径（`spider.jar`、`lib/*.js`、`*.json` …） |

编码细节、返回头、错误码见 [docs/接口说明.md](docs/接口说明.md)。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TVBOX_RELAY_PORT` | `8080` | 监听端口；改端口后电视地址也要跟着改 |
| `TVBOX_VOD_CONNS` | `28` | 点播预取并发连接数。单连 0.5~0.9 Mbps，24 以上才有明显效果 |
| `TVBOX_VOD_CACHE_MB` | `4096` | 分片缓存上限（MB），超了按最旧删到 80% |
| `TVBOX_VOD_WAIT` | `30` | 分片还没下完时，最多等预取多少秒 |
| `TVBOX_VOD_FAST_START` | `6` | 开头几个分片走分段并发，专门抢起播速度 |

```bash
set TVBOX_VOD_CONNS=32
python relay/tvbox-relay.py
```

## 每日体检

`tools/daily-check.ps1` 做四件事：拉取每条视频源配置并抽测采集接口、抽测直播分片、跑 `vod-scan.py` 给点播站排序、确认 8080 在监听（没在跑会自动拉起）。结果写 `report.txt` 和 `status.json`，`active` 不可用时会自动改指向。

挂成计划任务（每天 07:50）：

```powershell
schtasks /Create /SC DAILY /ST 07:50 /TN "TVBox-Source-DailyCheck" ^
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\path\to\tvbox-relay\tools\daily-check.ps1"
```

手动跑一次：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\daily-check.ps1
```

## 自建直播清单（可选）

不跑这些脚本也能用：`/live.m3u` 会直接回退到 `sources.json` 里的直播源。想只用自家宽带跑得动的线路时：

```bash
python tools/build-live-ln.py   # 地区 IPTV（需对应 ISP，可删）
python tools/build-live.py      # 合并公开源 + 分片测速
python tools/build-live6.py     # IPv6 高清
```

或双击 `tools\rebuild-live.cmd`。生成的 `relay/live*.m3u` 属于运行时产物，不入库。

## 部署到 NAS / Linux

只要 Python 3，无第三方依赖：

```bash
python3 relay/tvbox-relay.py
```

systemd 单元示例（`/etc/systemd/system/tvbox-relay.service`）：

```ini
[Unit]
Description=TVBox relay
After=network-online.target

[Service]
WorkingDirectory=/opt/tvbox-relay
ExecStart=/usr/bin/python3 /opt/tvbox-relay/relay/tvbox-relay.py
Restart=always
Environment=TVBOX_RELAY_PORT=8080

[Install]
WantedBy=multi-user.target
```

注意 `sources.json` 与服务在同一父目录（程序按 `relay/` 的上一级找它）。

## 关于脱敏

本仓库的代码逻辑与上游项目一致，但**不含**任何个人环境信息：

- 内网 IP（路由器 / 电脑 / 电视）→ 已改为运行时探测或示例占位符；
- 本机 Python 绝对路径 → 改为 PATH 探测（`python` / `py -3`）；
- 上游配置转储、APK 安装包、运行日志、`status.json`、自动生成的 m3u → 均未入库；
- 文档中的设备、目录、端口均为通用写法。

## 免责声明

- 上游配置与直播源均来自公开仓库，本项目只做地址中转与缓存，不存储、不分发任何音视频内容。
- 公开源随时可能失效或变更，请自行判断可用性与合规性，仅用于个人测试与学习。
- 点播加速会把分片暂存到本机磁盘（默认上限 4 GB），请留意所在磁盘容量。

## 相关项目

- [TVBoxOSC](https://github.com/o0HalfLife0o/TVBoxOSC) — TVBox 客户端
- [qist/tvbox](https://github.com/qist/tvbox)、[cluntop/tvbox](https://github.com/cluntop/tvbox) — 视频源配置
- [best-fan/iptv-sources](https://github.com/best-fan/iptv-sources) — 直播源
- [Long1982A/lntv](https://github.com/Long1982A/lntv) — 地区 IPTV 源示例
