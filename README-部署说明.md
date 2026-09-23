# s8 provider 清洗流水线（往 haolive/changfeng 仓库加的文件）

> 本仓库还有一条流水线：**节点测速过滤**（`refresh-nodes`，每小时把 10 个源合并后逐个测速，
> 只发布活节点给客户端拉）—— 见 [README-节点测速过滤.md](README-节点测速过滤.md)。

## 为什么要这层

上游 `Barabama/FreeNodes` 的 `feat/ai-crawler-v2/nodes/merged.yaml` 每小时重新生成，
时不时夹带**字段非法**的节点。而 mihomo 解析 proxy-provider 时遇到第一个非法节点就
**整体中止** —— 不是跳过那一个，而是整个源 0 个节点生效（`providers/s8_aicrawler.yaml`
从来没有落地就是这个原因）。

2026-09-23 实测的两个坏节点：

| 位置 | 字段 | 内核报错 |
|---|---|---|
| 第 2896 个（`分享日记_243`） | `reality-opts.public-key: enabled` | `invalid REALITY public key` |
| 第 3148 个（`分享日记_129`） | `short-id: 062898e8`（裸标量） | `invalid REALITY short ID`（YAML 当成科学计数法 6.2898e12） |

所以在中间加一层「按字段清洗」的产物：**按字段判断**不受上游重编号影响，
而按节点名排除（exclude-filter）会随编号漂移失效，且失效时是静默的。

## 要加的三个文件（保持目录结构）

| 本地文件 | 仓库路径 | 作用 |
|---|---|---|
| `changfeng-repo/.github/workflows/sync-s8-provider.yml` | `.github/workflows/sync-s8-provider.yml` | 定时任务：拉上游 → 清洗 → 内核自检 → 发布 release；按需提交统计（保活） |
| `changfeng-repo/tools/sanitize_provider.py` | `tools/sanitize_provider.py` | 按字段清洗 + 修 YAML 类型陷阱 + 修 `client-fingerprint` + 内核端到端自检 |
| `changfeng-repo/tools/keepalive.py` | `tools/keepalive.py` | 只在"上游坏节点签名变了"或"超 7 天没提交"时更新统计文件（见文末「保活」） |

建议顺便把 `多订阅合并配置-说明.md` 也传到仓库根目录 —— 那是配置的"长注释区"，
不放进去的话这份说明只存在于你本机。

## 操作步骤

1. 打开 `https://github.com/haolive/changfeng` → **Actions** → 首次会要求
   「I understand my workflows, go ahead and enable them」，点一下启用。
2. 用 **Add file → Create new file** 建上面两个文件：
   路径直接输入完整路径（如 `tools/sanitize_provider.py`），GitHub 会自动建目录；
   内容从本地这两个文件整体复制粘贴，提交到 `main`。
3. **Actions → sync-s8-provider → Run workflow** 手动跑一次。
   预期：release 里出现 tag `s8`、资产 `s8.yaml`（约 1.4 MB、3797 个节点），
   日志最后会打印 stats（剔除几个、修了几个引号）和内核自检结果。
4. 更新仓库根目录的 `多订阅合并配置.yaml`（**profile 是 remote 类型，仓库这份才是生效真源**）：
   - `s8-ai-crawler-v2` 的 `url` → **套镜像前缀**的 release 地址：
     `https://github.boki.moe/https://github.com/haolive/changfeng/releases/download/s8/s8.yaml`
     （实测 2026-09-23：本机直连 github.com 21~35s 后失败；boki 前缀 5.7~6.8s、seep 前缀 9~12s 都能下完 1.4MB）
   - `s5-sub445569` 的 `exclude-filter` → 收窄版（不能带 `tg频道`，会把该机场 26/26 全过滤掉）
   本地 `E:\Users\Documents\Python\多订阅合并配置.yaml` 已经改好，整文件覆盖上传即可。
5. Clash Verge 里对该 profile 点一次「更新」，之后确认：
   - 内核日志（`%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\logs\sidecar\sidecar_latest.log`）
     不再出现 `initial proxy provider s8-ai-crawler-v2 error` / `[Provider] s8-... pull error`；
   - 数据目录 `providers\s8_aicrawler.yaml` 出现（首次成功落盘）。

## 本地自测（可选，用你自己机器上的解释器和内核）

```powershell
# 1) 单元自检：内置样例覆盖两个陷阱 + 重名 + 缺字段，不需要网络
& "D:\ProgramFiles\install\Python311\python.exe" tools\sanitize_provider.py --selftest

# 2) 真拉一次 + 起临时内核实例做端到端自检
& "D:\ProgramFiles\install\Python311\python.exe" tools\sanitize_provider.py `
    --url "https://seep.eu.org/https://raw.githubusercontent.com/Barabama/FreeNodes/refs/heads/feat/ai-crawler-v2/nodes/merged.yaml" `
    --out dist\s8.yaml --stats dist\stats.json --min-nodes 500 `
    --core-check "D:\ProgramFiles\Portable\科学\Clash.Verge\verge-mihomo.exe"
```

2026-09-23 实测结果：`原始 3808 -> 保留 3797（剔除 11，修引号 1）`，内核加载 **3797** 个节点。

## 注意

- **`mihomo -t` 不校验 provider 内容**（含坏节点也 `exit=0`），所以流水线里的自检是
  「起临时实例 + 查 `/providers/proxies`」，别用 `-t` 代替。
- 发布用 **release 资产**而不是往仓库提交文件：产物每小时都在变，提交进 git 会让仓库
  每天涨几十 MB；release 资产是覆盖更新，不涨历史。
- 一旦发布成功，**不要改 tag 名 `s8` 和资产名 `s8.yaml`**，配置里的 url 指着它。
- `--min-nodes 500` 是兜底：上游哪天抽风返回空列表，job 会失败、release 保留上一版好数据。

## 保活（为什么会让机器人偶尔提交一次）

GitHub 对**公开仓库**的定时任务有个坑：连续 **60 天没有任何提交活动**，`schedule` 会被自动停用。
而这个 workflow 只更新 release 资产、不产生 commit —— 正好属于会被停用的状态。所以最后一步
（`tools/keepalive.py`）会**按需**提交一个小文件 `stats/s8-stats.json`：

- 只有「**上游坏节点签名变了**」（= 上游又开始/停止夹带非法节点）或「**超过 7 天没提交过**」才写，
  正常一周最多一两次，不会刷满提交历史；
- 它顺带是一份审计记录：`dropped_reasons` 里出现 `reality public-key 非法` / `reality short-id 非法`
  就说明上游那天有坏节点，`quoted_scalars` / `fingerprints` 记录被修好的节点
  （加引号的、`client-fingerprint` 改成 chrome 的）；
- 重名不计入签名（那只是常规噪音，mihomo 自己会跳过），否则签名每小时都变、等于每小时提交一次。
- 万一 `git push` 撞上你正在网页端编辑，job 会打个 warning 而不是变红，下次运行再补。
