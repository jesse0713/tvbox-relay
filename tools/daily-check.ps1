param(
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [int]$VideoSample = 10,
    [int]$LiveSample = 16,
    [int]$Timeout = 12
)
$ErrorActionPreference = 'Continue'
$curl = "$env:SystemRoot\system32\curl.exe"
$srcPath = Join-Path $Root 'sources.json'
$src = [IO.File]::ReadAllText($srcPath, [Text.Encoding]::UTF8).TrimStart([char]0xFEFF) | ConvertFrom-Json
$now = Get-Date
$stamp = $now.ToString('yyyy-MM-dd HH:mm:ss')

function CGet([string]$url, [int]$t = $Timeout) {
    $f = [IO.Path]::GetTempFileName()
    & $curl --noproxy '*' -L -s --max-time $t -A 'Mozilla/5.0' -o $f $url 2>$null | Out-Null
    if (-not (Test-Path $f)) { return '' }
    $s = [IO.File]::ReadAllText($f, [Text.Encoding]::UTF8)
    Remove-Item $f -Force -ErrorAction SilentlyContinue
    return $s
}

function CurlCode([string]$url, [int]$t = 15) {
    $r = & $curl --noproxy '*' -L -s -o NUL -I --max-time $t -A 'Mozilla/5.0' -w '%{http_code}' --url $url 2>$null
    return ("$r").Trim()
}

function EvenSample($arr, [int]$n) {
    $out = @()
    if ($arr.Count -le $n) { return $arr }
    $step = [Math]::Floor($arr.Count / $n)
    for ($i = 0; $i -lt $arr.Count -and $out.Count -lt $n; $i += $step) { $out += $arr[$i] }
    return $out
}

# ---------------- 视频源检查 ----------------
$videoResults = @()
foreach ($it in $src.video.items) {
    $r = [ordered]@{ id = $it.id; role = $it.role; name = $it.name; url = $it.url; ok = $false; sites = 0; cmsTested = 0; cmsAlive = 0; ratio = 0; spider = 'n/a'; note = '' }
    $txt = CGet $it.url 25
    if (-not $txt) { $r.note = '配置地址无法访问'; $videoResults += [pscustomobject]$r; continue }
    # 有的配置开头带整行 // 注释（cluntop 就加了说明段），标准 JSON 解析器不认，先剔掉
    $clean = (($txt -split "`r?`n") | Where-Object { $_.Trim() -notlike '//*' }) -join "`n"
    try { $j = $clean.TrimStart([char]0xFEFF) | ConvertFrom-Json } catch { $r.note = '配置内容不是合法 JSON'; $videoResults += [pscustomobject]$r; continue }
    $sites = @($j.sites)
    $r.sites = $sites.Count
    if ($sites.Count -lt 10) { $r.note = '站点数量异常偏少'; $videoResults += [pscustomobject]$r; continue }

    $spider = $j.spider
    if ($spider) {
        $rel = ([string]$spider -split ';')[0]
        $jarUrl = if ($rel -match '^https?://') { $rel } else { ($it.url -replace '[^/]+$', '') + ($rel -replace '^\./', '') }
        $code = CurlCode $jarUrl 25
        $r.spider = $code
    }

    $cms = @($sites | Where-Object { $_.api -match '^https?://' -and $_.api -notmatch '\.(js|json)(\?|$)' })
    $pick = EvenSample $cms $VideoSample
    $alive = 0
    foreach ($s in $pick) {
        $api = ([string]$s.api).TrimEnd('/')
        $testUrl = if ($api -match '\?') { "$api&ac=list" } else { "$api`?ac=list" }
        $body = CGet $testUrl 10
        if ($body -match '"class"' -or $body -match '"list"' -or $body -match 'vod_') { $alive++ }
    }
    $r.cmsTested = $pick.Count
    $r.cmsAlive = $alive
    $r.ratio = if ($pick.Count) { [Math]::Round(100.0 * $alive / $pick.Count, 0) } else { 0 }
    $r.ok = ($r.ratio -ge ($src.thresholds.video_cms_alive_min * 100)) -and ($sites.Count -ge $src.thresholds.video_sites_min)
    if ($r.spider -ne 'n/a' -and $r.spider -notmatch '^(200|206|302)$') { $r.note = "spider 下载异常($($r.spider))" }
    $videoResults += [pscustomobject]$r
}

# ---------------- 直播源检查 ----------------
$liveResults = @()
$px = @()
foreach ($it in $src.live.items) {
    $r = [ordered]@{ id = $it.id; role = $it.role; name = $it.name; url = $it.url; ok = $false; channels = 0; tested = 0; alive = 0; ratio = 0; note = '' }
    $txt = CGet $it.url 40
    if (-not $txt) { $r.note = '播放列表无法访问'; $liveResults += [pscustomobject]$r; continue }
    $urls = @($txt -split "`r?`n" | Where-Object { $_ -match '^\s*https?://' } | ForEach-Object { $_.Trim() })
    $r.channels = $urls.Count
    if ($urls.Count -eq 0) { $r.note = '播放列表内没有可用地址'; $liveResults += [pscustomobject]$r; continue }

    $pick = EvenSample $urls $LiveSample
    $cargs = @('-Z', '--parallel-max', '20') + $px + @('-m', '8', '-s')
    foreach ($p in $pick) { $cargs += @('-o', 'NUL', '--write-out', '%{http_code} ', '--url', $p) }
    $out = & $curl @cargs 2>$null
    $codes = @(($out -join ' ') -split '\s+' | Where-Object { $_ -match '^\d{3}$' })
    $alive = @($codes | Where-Object { $_ -match '^(200|206|301|302|304)$' }).Count
    $r.tested = $codes.Count
    $r.alive = $alive
    $r.ratio = if ($codes.Count) { [Math]::Round(100.0 * $alive / $codes.Count, 0) } else { 0 }
    $r.ok = ($r.ratio -ge ($src.thresholds.live_alive_min * 100))
    $liveResults += [pscustomobject]$r
}

# ---------------- 点播站体检与排序 ----------------
# 顺序：先筛「接口拿得到片子」的站，再按实测余量排，解析型按片源归属地（国内 CDN 优先）排。
# 结果写 relay/vod-rank.json，中转会按它重排 /tv.json 里的站点顺序，电视端不用动。
$vodLine = '未执行（找不到 python）'
$scanPy = Join-Path $Root 'tools\vod-scan.py'
# 找 Python：优先 PATH 里的 python，其次 py 启动器（Windows 官方安装器默认装它）
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$pyArgs = @()
if (-not $py) {
    $py = (Get-Command py -ErrorAction SilentlyContinue).Source
    if ($py) { $pyArgs = @('-3') }
}
if ($py -and (Test-Path $scanPy)) {
    $oldEnc = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        $scanOut = & $py @pyArgs $scanPy --quiet 2>&1
        $vodLine = (@($scanOut) | Where-Object { $_ -match 'vod-scan:' } | Select-Object -Last 1)
        if (-not $vodLine) {
            $rankFile = Join-Path $Root 'relay\vod-rank.json'
            if (Test-Path $rankFile) {
                try {
                    $rk = [IO.File]::ReadAllText($rankFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
                    $cnt = ($rk.counts.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' '
                    $vodLine = "已扫描 $(@($rk.results).Count) 个点播站  $cnt"
                } catch { $vodLine = '扫描无输出' }
            } else { $vodLine = '扫描无输出' }
        }
    } catch {
        $vodLine = "扫描失败：$($_.Exception.Message)"
    } finally {
        $env:PYTHONIOENCODING = $oldEnc
    }
}

# ---------------- 判定与切换 ----------------
function Pick-Working($results, $items, [string]$activeId) {
    $cur = $results | Where-Object { $_.id -eq $activeId } | Select-Object -First 1
    if ($cur -and $cur.ok) { return [pscustomobject]@{ id = $activeId; changed = $false; from = $activeId } }
    $alt = $results | Where-Object { $_.ok } | Select-Object -First 1
    if ($alt) { return [pscustomobject]@{ id = $alt.id; changed = ($alt.id -ne $activeId); from = $activeId } }
    return [pscustomobject]@{ id = $null; changed = $false; from = $activeId }
}

$vPick = Pick-Working $videoResults $src.video.items $src.video.active
$lPick = Pick-Working $liveResults $src.live.items $src.live.active

if ($vPick.id -and $vPick.id -ne $src.video.active) { $src.video.active = $vPick.id }
if ($lPick.id -and $lPick.id -ne $src.live.active) { $src.live.active = $lPick.id }
$src.updated = $now.ToString('yyyy-MM-dd')
$tmpSrc = "$srcPath.tmp"
[IO.File]::WriteAllText($tmpSrc, ($src | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
Move-Item -Path $tmpSrc -Destination $srcPath -Force

$status = [ordered]@{
    time      = $stamp
    video     = @{ active = $src.video.active; switched = $vPick.changed; results = $videoResults }
    live      = @{ active = $src.live.active; switched = $lPick.changed; results = $liveResults }
    vod       = @{ scan = "$vodLine"; rank = 'relay\vod-rank.json' }
    all_ok    = (($videoResults | Where-Object { $_.ok }).Count -gt 0) -and (($liveResults | Where-Object { $_.ok }).Count -gt 0)
}
$statusPath = Join-Path $Root 'status.json'
[IO.File]::WriteAllText($statusPath, ($status | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))

# ---------------- 中转服务 ----------------
$lanIp = $null
$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.' -and $_.InterfaceAlias -notmatch 'vEthernet|Loopback' })
$lanIp = ($ips | Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1)
if (-not $lanIp) { $lanIp = $ips | Select-Object -First 1 }
$lanIp = if ($lanIp) { $lanIp.IPAddress } else { '127.0.0.1' }

function Test-Relay {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:8080/health" -UseBasicParsing -TimeoutSec 6
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

$relayOk = Test-Relay
$relayStarted = $false
if (-not $relayOk) {
    $starter = Join-Path $Root 'relay\启动中转.cmd'
    if (Test-Path $starter) {
        cmd /c "`"$starter`"" | Out-Null
        Start-Sleep -Seconds 4
        $relayOk = Test-Relay
        $relayStarted = $relayOk
    }
}

$status['relay'] = [ordered]@{ running = $relayOk; started = $relayStarted; lan_ip = $lanIp; tv = "http://$lanIp`:8080/tv.json"; live = "http://$lanIp`:8080/live.m3u" }
[IO.File]::WriteAllText($statusPath, ($status | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))

# ---------------- 报告 ----------------
$lines = @()
$lines += "TVBox 源连通性检测  $stamp"
$lines += ""
$lines += "【视频源】"
foreach ($r in $videoResults) {
    $flag = if ($r.ok) { '可用' } else { '异常' }
    $lines += ("  {0,-4} {1,-28} 站点={2,-4} 采集存活={3}/{4} ({5}%)  spider={6}  {7} {8}" -f $r.role, $r.name, $r.sites, $r.cmsAlive, $r.cmsTested, $r.ratio, $r.spider, $flag, $r.note)
}
$lines += ""
$lines += "【直播源】"
foreach ($r in $liveResults) {
    $flag = if ($r.ok) { '可用' } else { '异常' }
    $lines += ("  {0,-4} {1,-30} 频道={2,-4} 抽测={3,-3} 可用={4,-3} ({5}%)  {6} {7}" -f $r.role, $r.name, $r.channels, $r.tested, $r.alive, $r.ratio, $flag, $r.note)
}
$lines += ""
$vActive = $src.video.items | Where-Object { $_.id -eq $src.video.active } | Select-Object -First 1
$lActive = $src.live.items | Where-Object { $_.id -eq $src.live.active } | Select-Object -First 1
$lines += "【结论】"
if ($vPick.changed) { $lines += "  视频源主源失效，已切换到：$($vActive.role) $($vActive.name)" } else { $lines += "  视频源主源正常：$($vActive.name)" }
if ($lPick.changed) { $lines += "  直播源主源失效，已切换到：$($lActive.role) $($lActive.name)" } else { $lines += "  直播源主源正常：$($lActive.name)" }
if (-not $vPick.id) { $lines += "  警告：所有视频源均不可用，需要人工更换线路" }
if (-not $lPick.id) { $lines += "  警告：所有直播源均不可用，需要人工更换线路" }
$lines += ""
$lines += "【点播站排序】"
$lines += "  $vodLine"
$lines += "  明细：relay\vod-rank.json（每日重测，中转按它重排 /tv.json 的站点顺序）"
$lines += ""
$lines += "【中转服务】"
$lines += ("  状态：{0}" -f $(if ($relayOk) { '运行中' } elseif ($relayStarted) { '原未运行，已自动拉起' } else { '未运行（电视会打不开，请双击 relay\启动中转.cmd）' }))
$lines += "  电视固定地址：http://$lanIp`:8080/tv.json"
$lines += "  直播固定地址：http://$lanIp`:8080/live.m3u"
$lines += ""
$lines += "【电视上要不要动手】"
if ($relayOk) {
    if ($vPick.changed -or $lPick.changed) {
        $lines += "  不用动。中转已自动切到可用备源，电视下次打开会直接拿到新配置。"
    } else {
        $lines += "  不用动。中转取到的就是主源，电视照常用。"
    }
} else {
    $lines += "  需要处理：中转没起来，电视端会加载失败。双击 relay\启动中转.cmd 即可。"
}
$lines += ""
$lines += "中转当前实际使用的上游："
$lines += "  视频：$($vActive.name)  $($vActive.url)"
$lines += "  直播：$($lActive.name)  $($lActive.url)"

$reportPath = Join-Path $Root 'report.txt'
[IO.File]::WriteAllText($reportPath, ($lines -join "`r`n"), (New-Object Text.UTF8Encoding($true)))
$lines | ForEach-Object { Write-Host $_ }

if ($status.all_ok) { exit 0 } else { exit 1 }
