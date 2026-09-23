#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机（国内）筛节点：拿仓库发布的订阅当候选，用**你这台机器的网络**实测一遍，
产出一份「国内实测可用」的配置。

为什么需要它
------------
仓库那条流水线跑在境外机房，它只能证明「节点活着、能跑流量」，证明不了「从国内连得上」。
2026-09-23 实测：同一份订阅（602 个节点），境外 runner 侧 18% 的服务器 TCP 可达，
而国内客户端里大部分显示超时 —— 因为「你 → 节点」这一跳只有你自己的网络能量到。
想让客户端里"看到的确实都能用"，就得在本机测一遍。免费池节点随时在换，
所以这份清单**短是正常的**（几十个甚至十几个都够用），它换来的是"每条都真能用"。

它做三件事
----------
1. 取候选：默认拉 release 里的 `best.yaml`（已由流水线筛过一遍"全球活着"的），
   `--full` 则改用 10 个源合并出来的完整池子（慢很多，但可能捞出流水线没留的节点）。
2. 用本机 mihomo 实测：延迟轮 + 真下 512KB 测速轮，目标与流水线一致
   （gstatic 204 / Google CDN），并用 `--dns-doh` 配上和客户端一样的 DoH，避免被污染解析坑。
3. 产出 `_cn_filter/best-cn.yaml`（完整配置，Verge 里「导入本地配置」即可用）
   + `nodes-cn.yaml`（纯节点清单）+ 一份运行报告。

用法
----
    python tools/local_cn_filter.py                     # 默认：候选=release 的 best.yaml
    python tools/local_cn_filter.py --full               # 候选=完整池子（本机跑，约十几分钟）
    python tools/local_cn_filter.py --limit 300          # 只测前 300 个（冒烟）
    python tools/local_cn_filter.py --publish            # 额外发布成 release 资产 best-cn.yaml
                                                          # （需要环境变量 GITHUB_TOKEN）

常用可调项：`--latency-timeout 8000`（国内链路慢，实测能用的节点握手也要 3~8 秒）、
`--max-latency 10000`、`--min-speed-kbps 100`、`--core <mihomo路径>`。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DEFAULT = os.path.normpath(os.path.join(HERE, '..'))
OUT_DIR = '_cn_filter'
RELEASE = 'https://github.com/haolive/changfeng/releases/download/best'
MIRRORS = ('https://github.boki.moe/', 'https://seep.eu.org/')
UA = 'clash.meta/v1.19.31'
DOH = ('https://doh.pub/dns-query', 'https://dns.alidns.com/dns-query')


def log(msg):
    print(msg, flush=True)


def fetch(urls, out_path, timeout=90):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for url in urls:
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': UA})
                with op.open(req, timeout=timeout) as r:
                    data = r.read()
                os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
                with open(out_path, 'wb') as fh:
                    fh.write(data)
                log('  下载成功（%d 字节）：%s' % (len(data), url[:70]))
                return True
            except Exception as e:  # noqa: BLE001
                last = e
                log('  %s 失败：%s' % (url[:60], e))
    log('  全部失败：%r' % (last,))
    return False


def find_core(explicit):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    cands = [
        os.path.expandvars(r'%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\verge-mihomo.exe'),
        r'D:\ProgramFiles\Portable\科学\Clash.Verge\verge-mihomo.exe',
        os.path.join(HERE, 'mihomo'),
        os.path.join(HERE, '..', 'mihomo'),
        shutil.which('mihomo') or '',
        shutil.which('verge-mihomo') or '',
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def find_config(repo, name):
    """配置在哪儿：本机习惯是放在仓库目录的**上一级**（仓库里那份是给流水线用的远端真源）。

    两个位置都找，找不到就把两个候选都报出来（免得像之前那样传到内核里才 FileNotFoundError）。
    """
    cands = [os.path.join(repo, name), os.path.join(os.path.dirname(repo), name)]
    for c in cands:
        if os.path.exists(c):
            return c
    raise SystemExit('找不到配置文件 %r，试过：\n  %s' % (name, '\n  '.join(cands)))


def main():
    ap = argparse.ArgumentParser(description='本机（国内）实测筛节点')
    ap.add_argument('--repo', default=REPO_DEFAULT, help='本地仓库目录（默认脚本上级目录）')
    ap.add_argument('--full', action='store_true', help='候选换成 10 个源合并的完整池子（慢）')
    ap.add_argument('--config', default='多订阅合并配置.yaml', help='配置模板（产出完整配置用）')
    ap.add_argument('--core', help='mihomo 可执行文件（默认自动找 Clash Verge 的）')
    ap.add_argument('--latency-timeout', type=int, default=8000, help='单节点延迟超时（毫秒，国内建议 8~10 秒）')
    ap.add_argument('--max-latency', type=int, default=10000, help='延迟超过就淘汰（毫秒）')
    ap.add_argument('--min-speed-kbps', type=float, default=100.0, help='下载速度下限（KB/s）')
    ap.add_argument('--speed-bytes', type=int, default=512 * 1024, help='测速下载字节数')
    ap.add_argument('--speed-limit', type=int, default=300, help='最多给多少个节点做下载测速')
    ap.add_argument('--max-nodes', type=int, default=300, help='结果里最多留多少个节点')
    ap.add_argument('--concurrency', type=int, default=24, help='并发数（本机别开太大，会把自家带宽占满）')
    ap.add_argument('--limit', type=int, help='只测前 N 个（冒烟）')
    ap.add_argument('--publish', action='store_true', help='把结果发布成 release 资产 best-cn.yaml（需 GITHUB_TOKEN）')
    a = ap.parse_args()

    repo = os.path.abspath(a.repo)
    cfg = find_config(repo, a.config)
    tester = os.path.join(repo, 'tools', 'test_nodes.py')
    fetcher = os.path.join(repo, 'tools', 'fetch_node_pool.py')
    if not os.path.exists(tester):
        log('找不到 %s（--repo 指到仓库目录上）' % tester)
        return 1
    core = find_core(a.core)
    if not core:
        log('找不到 mihomo 内核，用 --core 指定路径（Clash Verge 数据目录里有 verge-mihomo.exe）')
        return 1
    log('内核：%s\n配置：%s' % (core, cfg))

    out_dir = os.path.join(repo, OUT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    pool = os.path.join(out_dir, 'pool.yaml')

    if a.full:
        log('\n[1/2] 合并 10 个源（用客户端缓存，能离线就离线）…')
        cache = os.path.expandvars(r'%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\providers')
        cmd = [sys.executable, fetcher, '--config', cfg, '--out', pool,
               '--stats', os.path.join(out_dir, 'pool-stats.json')]
        if os.path.isdir(cache):
            cmd += ['--cache', cache]
        if subprocess.call(cmd) != 0:
            return 1
    else:
        log('\n[1/2] 取候选：release 里的 best.yaml（流水线已筛过一遍"全球活着"的）…')
        urls = [m + RELEASE + '/best.yaml' for m in MIRRORS] + [RELEASE + '/best.yaml']
        raw = os.path.join(out_dir, 'best-from-release.yaml')
        if not fetch(urls, raw):
            log('  拉不到候选，可以改用 --full（走本机合并）')
            return 1
        # 只留 proxies 段，交给 test_nodes.py 当池子用
        import yaml  # noqa: PLC0415
        nodes = (yaml.safe_load(open(raw, encoding='utf-8')) or {}).get('proxies') or []
        with open(pool, 'w', encoding='utf-8', newline='\n') as fh:
            yaml.safe_dump({'proxies': nodes}, fh, allow_unicode=True, sort_keys=False, width=4096)
        log('  候选 %d 个节点' % len(nodes))

    log('\n[2/2] 用本机网络实测（延迟 + 真下 %dKB）…' % (a.speed_bytes // 1024))
    cmd = [sys.executable, tester,
           '--pool', pool, '--config', cfg, '--core', core,
           '--out', os.path.join(out_dir, 'best-cn.yaml'),
           '--nodes-out', os.path.join(out_dir, 'nodes-cn.yaml'),
           '--stats', os.path.join(out_dir, 'filter-stats.json'),
           '--core-log', os.path.join(out_dir, 'core.log'),
           '--latency-timeout', str(a.latency_timeout),
           '--max-latency', str(a.max_latency),
           '--speed-bytes', str(a.speed_bytes),
           '--speed-timeout', '20',
           '--min-speed-kbps', str(a.min_speed_kbps),
           '--speed-limit', str(a.speed_limit),
           '--max-nodes', str(a.max_nodes),
           '--concurrency', str(a.concurrency),
           '--min-keep', '1']
    for d in DOH:
        cmd += ['--dns-doh', d]
    # 测速兜底：Google CDN 会对部分机房 IP 返 403（那不是节点的问题），换 CF 和国内镜像再试
    for fb in ('https://speed.cloudflare.com/__down?bytes={bytes}',
               'https://mirrors.aliyun.com/ubuntu/ls-lR.gz'):
        cmd += ['--speed-url-fallback', fb]
    if a.limit:
        cmd += ['--limit', str(a.limit)]
    rc = subprocess.call(cmd)

    stats_path = os.path.join(out_dir, 'filter-stats.json')
    kept = 0
    if os.path.exists(stats_path):
        with open(stats_path, encoding='utf-8') as fh:
            st = json.load(fh)
        kept = st.get('kept', 0)
        log('\n结果：候选 %d → 延迟合格 %d → 测速合格 %d → 最终 %d'
            % (st.get('pool', 0), st.get('latency', {}).get('passed', 0),
               st.get('speed', {}).get('passed', 0), kept))
    if rc != 0 and not kept:
        log('这次一个可用节点都没筛出来（免费池常态，等下一轮或换 --full 再试）')
        return 1

    log('\n产物：\n  %s   ← Verge 里「导入本地配置」\n  %s' %
        (os.path.join(out_dir, 'best-cn.yaml'), os.path.join(out_dir, 'nodes-cn.yaml')))

    if a.publish:
        token = os.environ.get('GITHUB_TOKEN', '')
        if not token or not kept:
            log('没发布：需要 GITHUB_TOKEN 环境变量，且至少有一个可用节点')
            return 0
        log('\n发布到 release tag best-cn …')
        return publish(token, os.path.join(out_dir, 'nodes-cn.yaml'), kept)
    return 0


def publish(token, nodes_file, kept):
    """把本机筛出来的清单发成 release 资产（只发节点清单，不含规则）。"""
    api = 'https://api.github.com'
    repo = 'haolive/changfeng'
    tag, asset, title = 'best-cn', 'nodes-cn.yaml', '本机实测（国内可用）节点'
    headers = {'Authorization': 'Bearer ' + token, 'User-Agent': 'local-cn-filter',
               'Accept': 'application/vnd.github+json'}

    def call(method, path, body=None, raw=False):
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
        req = urllib.request.Request(api + path, data=data, headers=headers, method=method)
        if data:
            req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = r.read()
                return r.status, (payload if raw else json.loads(payload or b'{}'))
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', 'replace')

    st, rel = call('GET', '/repos/%s/releases/tags/%s' % (repo, tag))
    notes = '由本机（国内网络）实测筛出：%d 个节点。生成于 %s。' % (
        kept, time.strftime('%Y-%m-%d %H:%M'))
    if st == 404:
        st, rel = call('POST', '/repos/%s/releases' % repo,
                       {'tag_name': tag, 'name': title, 'body': notes})
        if st not in (200, 201):
            log('  创建 release 失败：%s' % rel)
            return 1
    upload = rel.get('upload_url', '').split('{')[0]
    if not upload:
        log('  拿不到上传地址')
        return 1
    with open(nodes_file, 'rb') as fh:
        content = fh.read()
    req = urllib.request.Request(upload + '?name=' + asset, data=content, method='POST',
                                 headers={'Authorization': 'Bearer ' + token,
                                          'User-Agent': 'local-cn-filter',
                                          'Content-Type': 'text/yaml'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            log('  发布成功：%s（%d 字节）' % (r.status, len(content)))
    except Exception as e:  # noqa: BLE001
        log('  上传失败：%r' % (e,))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())