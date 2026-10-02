# 小米电视 IPTV 频道列表

这个项目把配置的公开 M3U/TXT 列表和直链，按频道白名单整理为 M3U 播放列表。更新时会短暂检查 HTTP(S) 播放地址，并分别记录 IPv4、IPv6 的连接与视频解码结果；它不能证明某条线路长期稳定，也不能证明电视所在的家庭网络能访问该线路。

频道是否进入长辈列表由 `config/channels.yaml` 中的 `elderly: true` 明确决定。项目不登录账号，不绕过 DRM、付费墙或认证。公网能访问的地址不等于已获得节目转播授权，请只使用自己有权使用的公开内容。

## 发布与订阅地址

仓库为 `luozijideryouxiang-ui/xiaomi-tv-iptv`。当前已发布播放列表和程序；每日自动更新仅保留在本地工作流中，GitHub workflow 尚未启用，因为当前 GitHub 登录缺少 `workflow` 权限。

电视端已成功导入并使用以下 CDN 地址订阅长辈列表：

```text
https://fastly.jsdelivr.net/gh/luozijideryouxiang-ui/xiaomi-tv-iptv@main/output/elderly.m3u
```

对应的过滤后 EPG 地址是：

```text
https://fastly.jsdelivr.net/gh/luozijideryouxiang-ui/xiaomi-tv-iptv@main/output/epg.xml
```

Raw 地址保留为备用：

```text
https://raw.githubusercontent.com/luozijideryouxiang-ui/xiaomi-tv-iptv/main/output/elderly.m3u
```

这台 Android 6 电视直连 GitHub Raw/Pages 地址时出现 TLS 失败，`fastly.jsdelivr.net` 地址已实测可用；更换地址时应优先使用 CDN。清除系统全局代理后，CCTV-1 直连 1280×720 硬件解码已通过；广东卫视在电视上显示缓冲，未通过，其他频道不据此推断。

2026-10-02 扩展后主列表和长辈列表均为 27 个唯一频道，新增白城综合、赤峰新闻综合、楚雄新闻综合、哈尔滨新闻综合、哈尔滨影视、兰州新闻综合、兰州文旅、四平综合、浙江国际。9 个新增频道均通过本机短时视频解码检查；赤峰、兰州新闻综合、哈尔滨影视也在电视 VLC 中取得视频输出。电视 VLC 的结果不能代替 OpenTV 的逐台验证，地区限制及直播签名到期也可能影响后续访问。仍未达到 40～60 个目标；汕头三台候选连接被拒绝或旧域名解析失败，官网“直播汕头”入口是活动直播/回放，未发现可独立订阅的常态三台 HLS，没有填入失效地址。缺失频道、近期失败和保留的旧成功结果见 `output/report.json`。

## 本地安装与运行

GitHub Actions 使用 Python 3.12；建议本地也使用 Python 3.12。进入仓库根目录后运行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m scripts.update --location local
```

Windows PowerShell 创建并启用虚拟环境的命令是：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

随后运行相同的 `python -m pip install`、测试和更新命令。更新程序会读取 `config/`，将生成文件写入 `output/`，并把上游列表缓存放在 `.cache/iptv/`。

如果首次运行没有任何线路通过检查，命令会以失败状态退出并写入诊断报告，但不会生成空的假播放列表。已有有效列表时，短期大面积故障会保留上一版列表。

## 配置频道与来源

### 频道白名单

在 `config/channels.yaml` 的 `channels` 中添加或编辑频道。`name` 是唯一的标准频道名；`group` 决定分组；`epg_names` 是可匹配的节目单名称；`logo` 可选；只有 `elderly: true` 的频道会进入 `elderly.m3u`。

```yaml
max_channels: 100
elderly_max: 60
channels:
  - name: 汕头综合
    group: 汕头
    elderly: true
    epg_names: [汕头综合, 汕头电视台综合频道]
    logo: https://example.invalid/logo.png
```

主列表最多 100 个频道，长辈列表最多 60 个。未列入 `channels` 白名单的上游频道不会进入输出；频道名匹配使用标准名、EPG 名和别名，不使用任意子串猜测。

### M3U/TXT 来源

在 `config/sources.yaml` 的 `sources` 下添加来源。数值较小的 `priority` 表示较高优先级。`enabled: false` 的来源不会被抓取。

```yaml
sources:
  - id: my-public-list
    name: 我的公开列表
    url: https://example.invalid/list.m3u
    format: m3u
    priority: 50
    enabled: true
    project_url: https://example.invalid/project
    rights_note: 来源及使用范围说明
```

支持 M3U 和 `频道名,URL` 形式的 TXT 列表；TXT 可用 `分组名,#genre#` 设置分组。来源下载使用 HTTP(S)，每份响应最多 20 MB，最多跟随 5 次重定向，并发下载最多 4 个来源。成功内容按来源 ID 原子写入缓存；某来源下载失败时会尝试使用该来源的最后一次有效缓存，`report.json` 的 `fetch_errors` 会记录错误及是否用了缓存。

GitHub Actions 中的 `127.0.0.1:8080` 指向临时 GitHub runner 本身，不是家里的电脑。如果启用 `http://127.0.0.1:8080/m3u` 这一类本机来源，必须在本地运行相应服务；它不会自动连接家庭网络。即使来源列表能从本机读取，播放地址仍须通过公开地址检查。

也可以在 `config/sources.yaml` 的 `candidates` 中直接登记经确认可用的播放地址。只有通过频道白名单匹配的候选才会进入检查流程。

### 频道别名

`config/aliases.yaml` 使用标准频道名作为键，值为该频道允许的别名列表。标准名必须已存在于 `channels.yaml`：

```yaml
aliases:
  CCTV-5 体育:
    - CCTV5
    - CCTV-5
    - 中央五套
```

匹配会折叠全角字符、标点和常见 HD/高清后缀。CCTV 编号和“中央几套”等常见写法也有受限的规则；仍须能对应配置中的标准频道。修改来源或别名后，可在本地运行 `python -m scripts.update --location local` 检查结果。

## 输出文件

成功更新时，`output/` 中包含：

| 文件 | 用途 |
| --- | --- |
| `iptv.m3u` | 主列表 |
| `main.m3u`、`iptv_all.m3u` | 主列表的兼容副本 |
| `iptv_ipv4.m3u`、`iptv_ipv6.m3u` | 已分别观察到 IPv4 或 IPv6 连接的列表 |
| `elderly.m3u` | 仅含 `elderly: true` 频道的长辈列表 |
| `backup.m3u`、`backup2.m3u` | 主线路之外的第一、第二备用线路 |
| `report.json` | 最近一次运行状态、检查结果、缺失频道和错误摘要 |
| `check_state.json` | 线路连续失败次数及最近成功状态，用于保留上一版可用线路 |
| `epg.xml`、`epg.json` | 过滤到已配置频道的 XMLTV 节目单及映射信息；XML 可用时生成 |

`report.json` 的检查 URL 会隐藏查询参数，来源下载错误按来源 ID 汇报。播放列表和 `check_state.json` 为了能直接播放和恢复线路，会保存原始播放 URL；不要放入需要保密的账号、密码或令牌，也不要把需要认证的个人地址提交到公开仓库。

更新连续 3 次检查失败后才会从活动线路中移除此前成功的地址；恢复后会重新纳入。若本轮没有可发布主列表，或主列表频道数低于上一版的 60%，程序会保留上一版播放文件并在报告中标记 `preserved_previous`。首次运行没有上一版且没有可用线路时会标记 `failed`，不会造出空列表。IPv4/IPv6 子列表若本轮没有有效线路，也会保留已有的有效文件。

检查默认对每条线路解码 3 秒短样本，包括观测到的地址族、首帧时间、分辨率、码率、样本时长和卡顿间隔。短样本不代表长期稳定；GitHub runner 的网络也不等同于小米电视所在的家庭网络。最终能否播放仍需在目标电视的网络中确认。

`checking.network_mode` 默认是 `auto`。检测到系统代理时，程序通过系统网络获取有限视频样本，成功线路可进入主列表，但上游地址族记为未验证，不进入 IPv4/IPv6 专用列表；没有代理时分别直连检测两个地址族。可配置 `direct` 强制直连，或 `system_proxy` 使用系统网络；程序不会把本机代理地址或凭据写入输出。未执行的地址族检测不会累计线路失败次数。

`epg.published_url` 当前指向 `https://fastly.jsdelivr.net/gh/luozijideryouxiang-ui/xiaomi-tv-iptv@main/output/epg.xml`，减少电视读取的节目单体积。Fork 到自己的仓库时，同时修改此地址和电视订阅地址。

## GitHub Actions 更新

`.github/workflows/update.yml` 在本地配置了每日计划、手动运行，以及 `main` 分支上的配置、脚本、测试、依赖或 workflow 文件变化时的更新。计划时间是 `20:17 UTC`，即中国时间次日 `04:17`。由于当前 GitHub 登录缺少 `workflow` 权限，GitHub 端 workflow 尚未启用，当前只能在本地运行更新命令；不要把本地文件中的计划时间当成已运行的定时任务。

以后启用 GitHub workflow 后，计划任务可能因 Actions 高负载而延迟；公开仓库连续 60 天没有活动时，计划任务可能被自动停用。当前没有可供查看的 GitHub workflow 运行记录；启用后可在仓库的 **Actions → Update IPTV** 查看，若任务停用，可按 [GitHub 文档](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows) 重新启用。计划时间和延迟说明见 [GitHub schedule 事件文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)。

## 在小米电视导入

建议使用 OpenTV，只添加一个长辈列表，减少入口和频道重复。电视端本轮已成功导入上面的 CDN 播放列表和 EPG：

1. 在电视安装并打开 OpenTV。
2. 在播放列表或网络 M3U 导入页面，粘贴上方 CDN 地址。
3. 在节目单设置中使用同前缀的 `epg.xml` 地址，保存并刷新列表；能否播放以电视当前网络和逐频道验证结果为准。

如果 CDN 订阅返回错误，可临时尝试上面的 Raw 备用地址；Android 6 上 Raw/Pages 可能继续出现 TLS 错误。若返回 404，请检查仓库默认分支及 `output/elderly.m3u` 是否已上传。
