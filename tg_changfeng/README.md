# tg_changfeng —— 长风频道 → 固定订阅链接

把 <https://t.me/s/changfengchannel> 里**每条消息都在变**的订阅链接，
转成一个**永远不变**的固定地址，每小时由 GitHub Actions 同步一次。

| 客户端 | 固定地址 |
| --- | --- |
| **Clash / Mihomo**（推荐） | `https://github.com/haolive/changfeng/releases/download/changfeng/clash.yaml` |
| SingBox | `https://github.com/haolive/changfeng/releases/download/changfeng/singbox.json` |
| Base64 通用订阅 | `https://github.com/haolive/changfeng/releases/download/changfeng/base64.txt` |

> 上游发布的是「早 8 点 / 晚 8 点」两条消息，每条里的
> `nodebuf.com/files/public/<id>/download` 都不同。本项目只做**镜像转发**：
> 内容原样搬运，不做任何转换，所以下游拿到的和打开原始链接看到的是同一份东西。

## 与仓库里已有项目的关系

本项目和现有的 `tools/` + `refresh-nodes.yml`（多订阅合并测速）**完全独立**：

* 不读 `多订阅合并配置.yaml`，不 import `tools/` 下任何脚本；
* 不下载/运行 mihomo 内核（只做内容结构校验，不做节点测速）；
* release tag 用的是新的 `changfeng`，和已有的 `best` / `fast` / `desk` 不冲突；
* 唯一的共享物是仓库本身（提交权限、公开仓库身份）。

`tools/` 是测速过滤那条线，`tg_changfeng/` 是频道镜像这条线，互不干扰。

## 目录结构

```
tg_changfeng/
├── sync_changfeng.py      # 全部逻辑：抓取 → 解析 → 校验 → 产出
└── README.md              # 本文件

.github/workflows/tg-changfeng-sync.yml   # 每小时定时任务
tg_changfeng-last-sync.json               # 保活状态（由 workflow 按需提交）
```

## 工作流程

```
https://t.me/s/changfengchannel
   │  GET（频道预览页，无需登录）
   ▼
按 data-post 切出每条消息 → 标签匹配（Clash/Mihomo、SingBox、Base64）
   │
   ▼
按消息 id 降序逐条尝试：下载 → 结构校验 → 第一个通过的就是它
   │
   ▼
原样写入 dist/{clash.yaml, singbox.json, base64.txt}
   │
   ▼
sha256 与上次一致？ ── 一致 → CHANGED=no，跳过上传
   │ 不一致
   ▼
gh release upload changfeng --clobber
```

## 容错设计（重点）

上游是别人维护的，随时可能改格式、挂链接、发空内容。所以：

1. **标签匹配 + 顺序兜底**：优先按「`Clash/Mihomo（推荐）：` 后面的第一个链接」取；
   标签解析全失败时退回「按出现顺序取第 1/2/3 个」。都不成立就不猜，宁可少一种格式。
2. **逐条回退**：最新消息的链接 404 / 超时 / 返回 HTML，就沿 `?before=` 往前翻，
   最多 3 页，直到找到内容合法的一条。
3. **结构校验**（避免把 404 页面当配置发出去）：
   * Clash：非 HTML、UTF-8、能解析、有非空 `proxies`（装了 PyYAML 才做完整解析）；
   * SingBox：合法 JSON 且有 `outbounds`；
   * Base64：能解码、解出来含协议链接。
4. **宁缺勿坏**：主格式（Clash）一条都拿不到就直接失败退出，**不覆盖**固定地址，
   客户端继续用上一版内容，比拿到一个坏配置强。
5. **幂等**：内容 sha256 没变就跳过上传，release 资产不会每小时被无意义重建。

## 本地使用

```bash
# 跑内置自测（不联网）
python tg_changfeng/sync_changfeng.py --selftest

# 真跑一次，产物在 dist/
python tg_changfeng/sync_changfeng.py --out dist

# 需要代理（国内直连 t.me 通常不通）
python tg_changfeng/sync_changfeng.py --out dist --proxy http://127.0.0.1:7890

# 拿上一版 latest.json 比对，内容没变就输出 CHANGED=no
python tg_changfeng/sync_changfeng.py --out dist --prev dist/latest.json
```

只依赖标准库；装了就用、没装也不影响（只影响 YAML 校验的严格程度）：

```bash
pip install pyyaml
```

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--out` | 产物目录，默认 `dist` |
| `--repo` / `--tag` | 写进 `links.txt` 的仓库 slug 与 release tag |
| `--prev` | 上一版 `latest.json`，用于内容哈希比对 |
| `--max-pages` | 最多往前翻几页找可用消息，默认 3 |
| `--state-file` | 保活状态文件；给了就额外输出 `KEEPALIVE=yes/no` |
| `--proxy` / `CF_PROXY` | HTTP 代理 |
| `--insecure` | 跳过 TLS 校验（排障用） |
| `--selftest` | 跑离线自测 |

### 输出约定

脚本最后两行是给 workflow 读的机器可读结论：

```
ASSETS=clash.yaml,singbox.json,base64.txt,links.txt,latest.json
CHANGED=yes
KEEPALIVE=yes      # 仅当传了 --state-file
```

同样写进 `dist/.changed` / `dist/.keepalive`，方便 shell 判断。

## 定时任务

`.github/workflows/tg-changfeng-sync.yml`：

* `cron: '23 * * * *'` —— 每小时第 23 分钟。**刻意避开 47 分**，那是 `refresh-nodes`
  的位置；两小时任务错开跑，免费 runner 的排队时段也错开。
* 也支持手动触发（Actions 页面 → Run workflow），可以带 `force` 输入强制覆盖上传。
* 首次运行前 release tag 不存在会自动创建。
* release **只有产物**：不设标题文案、不写说明正文，tag 名 `changfeng` 就是标题。
* 保活：公开仓库连续 60 天无提交会被 GitHub 自动停用 schedule。
  每次内容变化会产生一次提交；万一上游长期不更新，超过 7 天会兜底提交一次
  `tg_changfeng-last-sync.json`。

## 排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 任务失败在「取 t.me 超时」 | runner 到 t.me 的网络问题，重跑即可；也可临时在 workflow 里加 `CF_PROXY` |
| `内容校验未通过（返回的是 HTML 页面）` | 上游那条链接已经失效，脚本会自动往回退；日志里能看到退到了哪条消息 |
| 一直 `CHANGED=no` | 上游内容确实没变。想强制重传：手动触发时勾 `force` |
| 客户端说订阅格式错误 | 用 `latest.json` 里的 `upstream` 链接直接打开原始链接对比；本项目不加工内容，格式问题必然来自上游 |

## 免责声明

节点来自公开渠道，仅供学习交流，请遵守当地法律法规。