# 节点测速过滤流水线（refresh-nodes，往 haolive/changfeng 仓库加的文件）

## 为什么需要它

客户端（Clash Verge）靠 10 个 `proxy-providers` 在**本机**拉免费池，池子里常年混着大量
已经死掉的节点。客户端的健康检查只能"发现"某个节点不通，**删不掉它** —— 列表越长，
客户端每次要测的越多，界面上看到的废节点也越多。

这条流水线把活儿搬到 GitHub Actions：每小时把 10 个源合并成一个池子，用 mihomo 内核
**逐个测延迟 + 逐个真下 512KB 测速**，只把活下来的节点发出来。

## 加了哪些文件（保持目录结构）

| 本地文件 | 仓库路径 | 作用 |
|---|---|---|
| `.github/workflows/refresh-nodes.yml` | 同左 | 定时任务：拉源 → 合并清洗 → 测速 → 发布 release（+ 保活） |
| `tools/fetch_node_pool.py` | 同左 | 读配置里的 proxy-providers：逐源拉取、按字段清洗、加前缀、跨源去重 → 节点池 |
| `tools/test_nodes.py` | 同左 | 起临时内核做两轮测速（延迟 / 下载），产出 `best.yaml` 和 `nodes.yaml` |
| `tools/sanitize_provider.py` | 同左 | 按字段清洗单个源（REALITY 字段、YAML 类型陷阱、client-fingerprint） |
| `tools/local_cn_filter.py` | 同左 | 在自己网络下复筛节点，发布成 release `fast` |
| `tools/keepalive.py` | 同左 | 按需提交统计文件（防止公开仓库 60 天无提交被停用定时任务） |

**为什么每个源都要先"按字段清洗"**：上游免费池（尤其 s8 那条 `Barabama/FreeNodes`
的 `merged.yaml`，每小时机器重生成）时不时夹带字段非法的节点 —— 2026-09-23 实测到
`reality-opts.public-key: enabled`、`short-id: 062898e8`（裸标量被 YAML 当科学计数法）。
而 mihomo 解析 provider 时遇到**第一个**非法节点就整体中止：不是跳过那一个，
是整个源 0 个节点生效（缓存文件也不会落地），日志里只有一行 pull error，界面上看不出来。
所以清洗按**字段**判断而不是按节点名排除（按名字会随上游重编号失效，且失效时是静默的）。
详见 `tools/sanitize_provider.py` 的模块 docstring（六条规则）。

## 产物怎么用

release 下两个 tag，各自都是两个资产：

| tag | 资产 | 用途 |
|---|---|---|
| `best`（每小时自动更新） | `best.yaml` | **完整配置**：把「多订阅合并配置.yaml」的 `proxy-providers` 段换成测速后的 inline `proxies`，DNS / 策略组 / 分流规则 / 广告规则集原样保留。客户端里直接当订阅加即可 |
| | `nodes.yaml` | 只有 `proxies` 的清单。想保留自己那份配置、只把节点换掉的话，把它当 proxy-provider 用 |
| `fast`（手动刷新） | `best.yaml` / `nodes.yaml` | 同上结构，但节点是**在你自己的网络下复筛过的**（见「本机筛节点」一节） |

地址：

```
https://github.com/haolive/changfeng/releases/download/best/best.yaml
https://github.com/haolive/changfeng/releases/download/best/nodes.yaml
https://github.com/haolive/changfeng/releases/download/fast/best.yaml
https://github.com/haolive/changfeng/releases/download/fast/nodes.yaml
```

> 这里写的是**纯 GitHub 地址**。直连慢的时候，在客户端里自己往前面套一个镜像前缀即可
> （例如 `https://github.boki.moe/` + 上面的地址），换镜像不用改仓库里的任何东西。

Clash Verge 里：**订阅 → 新建 → 粘贴 best.yaml 地址 → 导入**。想让它跟着每小时更新，
把该 profile 的「更新间隔」调小（Verge 默认很长），或者每次手动点一下更新。

> 注意：`best.yaml` 是**生成物**，别在它上面手工改配置（下次运行会覆盖）。
> 配置要改就改仓库根目录的 `多订阅合并配置.yaml` —— 它是源列表与规则的唯一真源，
> 流水线每小时读它。

> **客户端里还需要写排除词（exclude-filter）吗？不需要了** —— `best.yaml` 里没有
> `proxy-providers` 这一层，客户端根本不参与拉源，也就没有"按名字过滤"这回事。
> 而且自 2026-09-23 起**仓库那份配置里也不配 exclude-filter 了**：实测那套词在 10 个源
> （5845 个节点的池子）里命中 29 个，**29 个全是真节点配置** —— `🇭🇰_HK_中国香港`、
> `🇹🇼 中国台湾省-xxx`、`🇨🇳_CN_中国->🇺🇸_US_美国` 这类中转，全部由 `中国` 一个词误杀；
> 而「剩余流量：100GB」「到期时间」这类信息行命中 **0** 个。词表当时只剩误伤，所以整个删了。
> 现在过滤完全交给测速：信息行将来真出现也不用担心 —— 它不是有效配置，会在延迟轮被淘汰。
> 另外，测速过滤只保证"发布那一刻是活的"；节点在这之后随时会死，客户端 `自动选择`
> 那个 url-test 组的健康检查照样得留着（它负责在你手动更新订阅之前兜住这种情况）。

## 筛选标准（默认值都在 workflow 的参数里，改一行就行）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--latency-url` / `--latency-timeout` | gstatic 204 / 3000ms | 延迟测试地址与客户端健康检查一致，超时即淘汰 |
| `--max-latency` | 2000ms | 延迟超过就淘汰 |
| `--speed-url` / `--speed-url-fallback` | Google CDN 大文件 / Cloudflare speed | 下载测速目标。主目标跟延迟轮同属 Google（能通 gstatic 的节点基本都能通它）；主目标**一个字节都读不到**时才换兜底 |
| `--speed-bytes` / `--speed-timeout` | 512KB / 10s | 每个存活节点真下 512KB |
| `--min-speed-kbps` | 100 KB/s | 下载速度低于就淘汰（"速度不足"和"下载失败"分开记，日志里能看到各占多少） |
| `--speed-limit` | 800 | 只给延迟最好的 800 个做下载测速（流量与时间可控） |
| `--max-nodes` | 600 | 最终订阅最多 600 个节点（按延迟从好到差排） |
| `--min-keep` | 100 | 存活少于 100 个就**判失败、不发布**（release 里保住上一版） |
| `--concurrency` | 64 | 延迟轮并发 |

淘汰原因都会写进 `dist/filter-stats.json` 和 Actions 日志（哪个节点、什么原因）。

## 几个必须知道的点

1. **测速是在 GitHub runner（境外机房）上做的**：它回答的是"这个节点活着、能跑流量"，
   不等于"从国内连它也快"。但"死的 / 半死不活的"确实会被清掉 —— 这正是要的效果。
   想按国内网络的口味筛，在本机跑 `tools/test_nodes.py`（见下），两者不冲突。
2. **IPv6 节点**：runner 没有 IPv6 出口，IPv6 字面量地址的节点在那边测不了。
   默认 `--ipv6-policy keep`：跳过测速、原样保留（不冤枉好节点）；如果你本机没有 IPv6、
   想让它们彻底消失，改成 `drop`。（实测整池 5845 个节点里只有 **2** 个真 IPv6 字面量，
   影响可以忽略。注意判断用的是 `ipaddress`：免费池里有 `server` 写成 `用户@主机:443?参数`
   的垃圾节点也含冒号，那种必须照常测速淘汰，不能当 IPv6 放过去。）
3. **内核预检自愈**：mihomo 解析 inline proxies 时，遇到字段缺失/非法的节点不是跳过它，
   而是**整体拒绝整个配置**（`Parse config error: proxy 2: '' has unset fields: cipher`）——
   一个坏节点能让 6000 个节点全进不了内核。`tools/sanitize_provider.py` 的规则覆盖不到
   "缺必填字段"这一类，所以 `test_nodes.py` 会先用 `mihomo -t` 预检、按内核报的下标逐个剔掉，
   直到配置能过。日志里出现"预检剔除"是正常的，那是兜底，不是误杀。
4. **别改 tag 名 `best` 和资产名**：客户端订阅地址指着它们。
5. **源列表跟着配置走，随便加删**：流水线每次运行都现读仓库根目录 `多订阅合并配置.yaml` 的
   `proxy-providers` —— 加源、删源、换 `url`、改 `additional-prefix` 都只改那一个文件，
   workflow 一行都不用动。加源时注意两点：① 前缀用还没被占的（`S11 |`、`S12 |`…），
   产物里靠它区分来源，也是客户端"记住手动选过的节点"的依据；② 某个源拉不动、或返回的
   不是节点清单时，只会在日志里记一条"拉取失败 / 源内容异常"并跳过它，不会拖累其它源
   （`--min-keep` 兜底：活节点太少就整体不发布，release 保留上一版）。
   另外：新源如果套了别的 GitHub 镜像前缀（不是 `github.boki.moe` / `seep.eu.org`），
   把前缀加进 `tools/fetch_node_pool.py` 的 `MIRROR_PREFIXES` 就能让它在 runner 上走直连；
   不加也只是拉得慢一点，不影响结果。
6. **下载测速用的是 listeners**：给每个存活节点开一个本机 HTTP 入站（`proxy:` 绑定到该节点），
   再经它真下 512KB。这是唯一能区分"能握手但传不动"（免费池里很常见）的方法 ——
   mihomo 的 delay 接口只量首字节耗时，不是带宽。
7. **为什么测速目标选 Google CDN（第一次跑踩的坑）**：最初用 `speed.cloudflare.com`，
   在 runner 上实测 **800 个存活节点 0 个通过** —— 那些节点的出口连 Cloudflare 普遍超时
   （日志里全是 `context deadline exceeded`），而不是节点本身不能用。换成 `dl.google.com`
   的大文件（与延迟轮的 gstatic 同属 Google）后同一批节点 **761/800 通过**。
   所以：**测速目标要挑"节点出口普遍能到"的家**；主目标一个字节都读不到时才会去试 CF 兜底。
8. **客户端里为什么一堆超时**（2026-09-23 实测）：两件事叠在一起 ——
   ① 免费池的服务器**大部分从国内连不上**（同一份 602 个节点：境外看 18% 服务器 TCP 可达，
   国内直连实测也只有 111/602 能连上，且这些还只是 TCP 层）；
   ② **客户端的健康检查超时太紧**：拿客户端当时正在用的节点验证，`curl` 经它访问国内站
   **8.3 秒**才返回 200 —— 而配置里 `自动选择` 组的 `timeout` 是 2000ms，测速轮 3 秒，
   于是"慢但能用"的节点在界面上全被标成超时、也不会被 `自动选择` 挑中。
   所以配合这份订阅，客户端的组超时建议放宽（`timeout: 5000` 起步）；
   想让"看到的确实都能用"，就用上面的 `tools/local_cn_filter.py` 在本机筛一遍。
9. **CF 优选 IP 试过了、没用（别再折腾）**：拿 `stock.hostmonit.com` / `api.hostmonit.com`
   的国内优选 CF IP（52~161ms、0% 丢包）跟池子里的 CF 前置节点做对照实验
   （同一份节点配置，一半保留原 `server`、一半换成优选 IP，成对比较）：
   两组**同样是全灭** —— 说明卡住这些免费节点的不是"连到哪个 CF 边缘 IP"，
   而是节点配置本身已过期/伪装域名在墙内被阻断。优选 IP 的正确用途是
   **自己的 CF 加速域名/自建节点挑 IP**，对上游免费池没用。

## 排查

- Actions 日志里有每轮的分段输出（池子大小、延迟通过数、测速通过数、最终节点数、各源存活数）。
- 产物 `dist/core.log`（临时内核实例的日志）会作为 artifact `node-test-log` 保留 7 天。
- 某次运行失败（比如存活节点少于 `--min-keep`）时**不会覆盖 release**，客户端拿到的还是上一版，
  所以看到 workflow 红了一次不用慌，看日志定位就行。
- 想看"现在到底哪些节点活着"，直接把 release 的 `best.yaml` 下下来看 `proxies:` 段（按延迟排序）。

## 在自己的网络下复筛节点（可选）

**想要"客户端里看到的确实都能用"，只能用本机网络测一遍** —— 仓库那条流水线在境外机房，
它量不到「你 → 节点」这一跳。为此有个一键脚本：

```powershell
# 候选 = release 里那份 best.yaml（流水线筛过"全球活着"的），用本机网络实测一遍
python tools/local_cn_filter.py --repo "<仓库目录>"

# 候选换成 10 个源合并的完整池子（慢很多，但可能捞出流水线没留的节点）
python tools/local_cn_filter.py --full

# 只测前 300 个（冒烟）
python tools/local_cn_filter.py --limit 300
```

内核路径会自动找（`--core` > 环境变量 `MIHOMO`/`CLASH_CORE` > 常见安装位置 > PATH），
找不到时用 `--core "<verge-mihomo.exe 的路径>"` 指定。

产物在 `_cn_filter\best-cn.yaml`（完整配置，Verge 里「导入本地配置」）和 `nodes-cn.yaml`。
加 `--publish` 可以把它们发成 release **`fast`** 的 `best.yaml` / `nodes.yaml`（需要 `GITHUB_TOKEN` 环境变量）：

```
https://github.com/haolive/changfeng/releases/download/fast/best.yaml
https://github.com/haolive/changfeng/releases/download/fast/nodes.yaml
```

> ⚠ `fast` 是**快照**，不自动更新（这一轮筛选依赖本地网络，GitHub 上没法定时跑）。
> 想刷新就再跑一次脚本 + `--publish`，会覆盖同一份资产。
> 2026-09-24 实测一次：仓库那份 602 个候选 → 延迟合格 55 → 测速合格 6（另 2 个 IPv6 未测）；
> 用 `curl` 完全绕过 mihomo 复验：gstatic 返回 204、256KB 也真能下下来 ✓。
> 筛出来的节点里有 `84.17.47.x:9002` 这类 http 型 —— 和当时手动选中的节点是同族。

> 脚本内部已经处理了两件容易踩的事：
> 1. **DoH DNS**（`--dns-doh doh.pub / alidns`）：国内系统 DNS 对 Google/CF 域名可能给出被污染的结果，
>    配上 DoH 后就能用**和流水线一样的目标**（gstatic / Google CDN），只有测速点不同。
> 2. **超时放宽到 8 秒**：实测国内能用的节点，握手也可能要 3~8 秒（拿客户端正在用的那个节点
>    用 curl 验证：8.3 秒才返回 200）。用流水线那套 3 秒超时，好节点会被全部冤枉掉。

手工跑 `tools/test_nodes.py` 也可以，记得带上 `--dns-doh https://doh.pub/dns-query`、
`--latency-timeout 8000 --max-latency 10000`，并发别开太大（`--concurrency 24` 左右，
本机跑大了会把自家带宽占满、连本地内核的 API 都会被 RST）。

> **国内本机跑的通过率天然很低**：2026-09-23 实测，同一份 5872 个节点的池子，
> 境外 runner 有 1062 个通过延迟轮，国内本机只有十几个（0.2%）——
> 免费池的服务器绝大多数**从国内连不上**。所以本机筛出来的清单很短是正常的（短但准）；
> 免费池本来每小时都在换，清单短不影响用，下一次再跑就是了。

## 保活（公开仓库定时任务会被自动停用的坑）

GitHub 对**公开仓库**的定时任务：连续 60 天没有任何提交活动，`schedule` 会被自动停用。
这条流水线同样只更新 release 资产、不产生 commit，所以最后一步会**按需**提交
`stats/node-filter-stats.json` —— 只有「**有源拉取失败/恢复**」或「**上游夹带的坏节点集合变了**」
或「**超过 7 天没提交**」才写，正常一周最多一两次。