#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「多订阅合并配置.yaml」里的 proxy-providers 合成一个节点池（合并 + 清洗 + 去重）。

为什么需要这一步
----------------
客户端（Clash Verge）是靠 proxy-providers 在**本机**拉这些源的；源都是免费池，
常年混着大量已经死掉的节点。客户端的健康检查只能"发现"某个节点不通，
没法把它从列表里删掉 —— 列表越长，客户端要测的就越多，你在界面上看到的废节点也越多。

所以把「合并 → 清洗 → 测速」搬到 GitHub Actions 上跑，产出只有活节点的订阅。
本脚本负责与网络无关的前半段：

    1) 从配置里读出每个源的 url / exclude-filter / additional-prefix / override
    2) 逐个拉取（套了 GitHub 镜像前缀的 URL 会先还原成直连地址 —— runner 上直连更快更稳）
    3) 复用 tools/sanitize_provider.py 的按字段清洗（非法 REALITY / YAML 类型陷阱 / 指纹）
    4) 按 exclude-filter 过滤、按 additional-prefix 加前缀（**与客户端里看到的名字完全一致**）
    5) 跨源去掉完全相同的节点（同一个落地节点被多个源收录是常态）
    6) 输出 {proxies: [...]} 给 tools/test_nodes.py 去测速

关于前缀：2026-09-23 用 mihomo v1.19.31 实测，`additional-prefix: "S3 |"` 的结果是
`S3 |🇯🇵 日本 | JPN 5` —— 直接拼接、**不额外插空格**。这里照抄，保证改名前后
客户端的 store-selected（记住手动选过的节点）不会因为名字变了而失效。

用法
----
  python tools/fetch_node_pool.py --config 多订阅合并配置.yaml --out dist/pool.yaml
  python tools/fetch_node_pool.py --config 多订阅合并配置.yaml --out dist/pool.yaml \
      --cache "%APPDATA%/io.github.clash-verge-rev.clash-verge-rev/providers"   # 用客户端缓存离线跑
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit('需要 PyYAML：pip install pyyaml')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sanitize_provider as sp  # noqa: E402  （复用按字段清洗，别再抄一份）

UA = sp.UA

# 配置里给上游套的 GitHub 镜像前缀。runner 在境外，直连 github 反而更快更稳，
# 所以先尝试还原后的直连地址，失败再退回原地址（顺序见 url_candidates）。
MIRROR_PREFIXES = (
    'https://github.boki.moe/',
    'https://seep.eu.org/',
)

# 会被视作"同一个节点"的字段（凭据也算 —— 这一步只去重**完全相同**的条目，不丢信息）
IDENT_FIELDS = (
    'type', 'server', 'port', 'uuid', 'password', 'cipher', 'auth', 'auth-str', 'psk',
    'private-key', 'public-key', 'sni', 'servername', 'network', 'flow', 'security',
    'ws-opts', 'grpc-opts', 'reality-opts', 'plugin-opts', 'obfs', 'obfs-password',
    'up', 'down', 'protocol', 'protocol-param', 'alterId',
)

# 这些类型才认 skip-cert-verify；ss/ssr 之类没有 TLS 握手，别给它们加字段
TLS_TYPES = {'vmess', 'vless', 'trojan', 'hysteria', 'hysteria2', 'tuic', 'anytls', 'http'}


def load_sources(config_path):
    """从配置里读 proxy-providers，返回按原顺序的源列表。"""
    with open(config_path, 'r', encoding='utf-8') as fh:
        cfg = yaml.safe_load(fh) or {}
    sources = []
    for name, p in (cfg.get('proxy-providers') or {}).items():
        if not isinstance(p, dict):
            continue
        ov = p.get('override') or {}
        sources.append({
            'name': name,
            'url': str(p.get('url') or ''),
            'path': str(p.get('path') or ''),
            'prefix': str(ov.get('additional-prefix') or ''),
            'skip_cert': bool(ov.get('skip-cert-verify')),
            'exclude': str(p.get('exclude-filter') or ''),
        })
    return sources


def url_candidates(url):
    """直连地址优先，其次原地址。"""
    cands = []
    for pre in MIRROR_PREFIXES:
        if url.startswith(pre):
            cands.append(url[len(pre):])
    if url not in cands:
        cands.append(url)
    return cands


def fetch_text(url, retries=3, timeout=90):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError('拉取失败：%r' % (last,))


def compile_exclude(pattern):
    """exclude-filter 支持用反引号分隔多条正则（与 mihomo 一致）。"""
    regs = []
    for part in str(pattern or '').split('`'):
        part = part.strip()
        if not part:
            continue
        try:
            regs.append(re.compile(part))
        except re.error as e:
            print('  ！exclude-filter 不是合法正则，已忽略：%r（%s）' % (part, e))
    return regs


def excluded(name, regs):
    return any(r.search(name) for r in regs)


def ident_key(node):
    picked = {}
    for f in IDENT_FIELDS:
        if f in node:
            picked[f] = node[f]
    picked['type'] = node.get('type')
    picked['server'] = node.get('server')
    picked['port'] = node.get('port')
    return json.dumps(picked, sort_keys=True, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description='合并多订阅配置里的 proxy-providers 成一个节点池')
    ap.add_argument('--config', default='多订阅合并配置.yaml', help='多订阅合并配置.yaml 的路径')
    ap.add_argument('--out', required=True, help='输出节点池文件（{proxies: [...]}）')
    ap.add_argument('--stats', help='统计写入的 json 文件')
    ap.add_argument('--cache', help='离线/自测：在该目录里按 provider 的 path 文件名找缓存，命中就不联网')
    ap.add_argument('--min-nodes', type=int, default=1, help='少于这么多节点则判定失败（默认 1）')
    a = ap.parse_args()

    sources = load_sources(a.config)
    if not sources:
        print('配置里没有 proxy-providers，无法合并')
        return 1

    merged, per_source, dropped = [], [], {}
    failed = []
    for i, s in enumerate(sources, 1):
        tag = s['name']
        print('[%d/%d] %s' % (i, len(sources), tag))
        rec = {'name': tag, 'url': s['url'], 'error': None, 'upstream': 0, 'kept': 0,
               'excluded': 0, 'dropped_by_sanitize': 0, 'bad_signature': ''}
        text, label = None, s['url']
        cache_file = os.path.join(a.cache, os.path.basename(s['path'])) if (a.cache and s['path']) else None
        if cache_file and os.path.exists(cache_file):
            text = open(cache_file, 'r', encoding='utf-8', errors='replace').read()
            label = cache_file
            print('   用缓存 %s' % os.path.basename(cache_file))
        elif not s['url']:
            rec['error'] = '配置里没有 url'
        else:
            for cand in url_candidates(s['url']):
                try:
                    text = fetch_text(cand)
                    label = cand
                    break
                except Exception as e:  # noqa: BLE001
                    print('   %s 拉取失败：%s' % (cand, e))
            if text is None:
                rec['error'] = '拉取失败'
        if text is None:
            failed.append(tag)
            per_source.append(rec)
            dropped['拉取失败'] = dropped.get('拉取失败', 0) + 1
            continue

        try:
            out_text, st = sp.sanitize(text, label)
        except (SystemExit, Exception) as e:  # noqa: BLE001  sanitize 用 SystemExit 表达"内容不对"
            rec['error'] = '内容不是 Clash 节点清单（%s）' % e
            failed.append(tag)
            per_source.append(rec)
            dropped['源内容异常'] = dropped.get('源内容异常', 0) + 1
            continue

        nodes = (yaml.safe_load(out_text) or {}).get('proxies') or []
        rec['upstream'] = st['total']
        rec['dropped_by_sanitize'] = st['dropped_count']
        rec['bad_signature'] = st.get('bad_signature', '')
        regs = compile_exclude(s['exclude'])
        kept = []
        for n in nodes:
            name = str(n.get('name') or '')
            if excluded(name, regs):
                rec['excluded'] += 1
                continue
            n['name'] = s['prefix'] + name if s['prefix'] else name
            if s['skip_cert'] and 'skip-cert-verify' not in n and str(n.get('type')) in TLS_TYPES:
                n['skip-cert-verify'] = True
            kept.append(n)
        rec['kept'] = len(kept)
        print('   上游 %d → 清洗后 %d → 过滤 %d → 保留 %d%s' % (
            rec['upstream'], len(nodes), rec['excluded'], rec['kept'],
            '，清洗剔除 %d' % rec['dropped_by_sanitize'] if rec['dropped_by_sanitize'] else ''))
        merged.extend(kept)
        per_source.append(rec)

    # 跨源去重：完全相同的条目只留一个（前缀已按源区分，名字不会撞）
    seen, uniq, dup = set(), [], 0
    for n in merged:
        k = ident_key(n)
        if k in seen:
            dup += 1
            continue
        seen.add(k)
        uniq.append(n)
    if dup:
        dropped['重复节点'] = dup
    for k, v in [('exclude-filter', sum(r['excluded'] for r in per_source)),
                 ('清洗剔除', sum(r['dropped_by_sanitize'] for r in per_source))]:
        if v:
            dropped[k] = v

    v6 = sum(1 for n in uniq if ':' in str(n.get('server', '')))
    by_source = {}
    for n in uniq:
        src = str(n.get('name', '')).split(' |')[0] if '|' in str(n.get('name', '')) else '其它'
        by_source[src] = by_source.get(src, 0) + 1

    header = ('# 由 tools/fetch_node_pool.py 自动生成（多订阅合并配置.yaml 的 proxy-providers 合并清洗产物）\n'
              '# 生成时间: {ts}\n'
              '# 节点数: {kept}（合并 {merged}，去重 {dup}）；其中 IPv6 字面量地址 {v6} 个\n'
              '# 各源: {by_source}\n'
              '# 下一步: tools/test_nodes.py 会对这份池子逐个测速，只有活下来的节点才会进订阅\n')
    text = header.format(ts=time.strftime('%Y-%m-%d %H:%M:%S'), kept=len(uniq), merged=len(merged),
                         dup=dup, v6=v6,
                         by_source=', '.join('%s=%d' % kv for kv in sorted(by_source.items())))
    d = os.path.dirname(os.path.abspath(a.out))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(a.out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
        # 用 Go 友好的 dumper：PyYAML 读进来再写出去会把清洗层加的引号弄丢
        # （'062898e8' 这种值裸着写出去，mihomo 会按科学计数法读）
        sp.dump_go_safe({'proxies': uniq}, fh)

    print('\n合并结果：%d 个源 → %d 个节点（去重 %d）；IPv6 字面量 %d 个' % (
        len(sources) - len(failed), len(uniq), dup, v6))
    print('各源：%s' % ', '.join('%s=%d' % kv for kv in sorted(by_source.items())))
    if failed:
        print('拉取/解析失败的源：%s' % ', '.join(failed))

    stats = {
        'stage': 'fetch',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'config': a.config,
        'sources': per_source,
        'source_count': len(sources),
        'failed_sources': failed,
        'merged': len(merged),
        'kept': len(uniq),
        'duplicates': dup,
        'ipv6_literals': v6,
        'by_source': by_source,
        'dropped_reasons': dropped,
        # 给 tools/keepalive.py 用：只有"源挂了/恢复了"或"上游夹带的坏节点变了"才变
        'bad_signature': hashlib.sha1(('\n'.join(
            sorted('%s|%s' % (r['name'], r['bad_signature'] or ('FAIL' if r['error'] else ''))
                   for r in per_source))).encode('utf-8')).hexdigest()[:16],
    }
    if a.stats:
        with open(a.stats, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=2)
            fh.write('\n')

    if len(uniq) < a.min_nodes:
        print('节点数 %d 少于下限 %d，判定失败' % (len(uniq), a.min_nodes))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())