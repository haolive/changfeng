#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把上游 provider（节点清单）清洗成 mihomo 能完整解析的文件。

为什么需要这一步
----------------
mihomo 解析 proxy-provider 时，遇到**第一个**字段非法的节点就整体报错返回：
    "initial proxy provider s8-ai-crawler-v2 error: proxy 2895 error: invalid REALITY public key"
不是跳过这一个节点，而是整个 provider 解析中止 —— 效果是该订阅源 **0 个节点生效**
（providers/<名>.yaml 缓存也不会落地，因为写盘在解析成功之后）。
上游是机器每小时重新生成的文件，坏节点会不断换编号复现，所以这里按**字段**清洗，
而不是按节点名排除（按名字排除会随上游重编号失效，且失效时是静默的）。

清洗规则
--------
1) 丢弃 reality-opts.public-key 无法按 base64url 解出 32 字节的节点
2) 丢弃 reality-opts.short-id 非十六进制、或解码后超过 8 字节的节点
3) 丢弃缺 name / type / server / port 的节点
4) 同名节点只保留第一个（mihomo 会对重名报 duplicate）
5) 修复 YAML 解析器差异：Python(PyYAML) 判定为字符串、而 Go(mihomo 用的 yaml.v3)
   会判定成数字/布尔的标量，强制加引号。
   典型受害者：`short-id: 062898e8` —— Go 按科学计数法读成 6.2898e12，
   落到 hex.Decode 里就是 "invalid REALITY short ID"。加引号即恢复正常。

用法
----
  python tools/sanitize_provider.py --url <上游URL> --out dist/s8.yaml [--stats dist/stats.json]
  python tools/sanitize_provider.py --in  <本地文件> --out <输出文件>
  python tools/sanitize_provider.py --url <上游URL> --out out.yaml --core-check <mihomo可执行文件>
  python tools/sanitize_provider.py --selftest

`--core-check` 会起一个**临时** mihomo 实例（隔离 home 目录 + 文件型 provider + 空闲端口），
确认清洗后的文件真能被内核加载出节点；这是唯一能覆盖"我没想到的非法字段"的验证方式
（注意：`mihomo -t` 不校验 provider 内容，不能用它代替）。
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit('需要 PyYAML：pip install pyyaml')

UA = 'clash.meta/v1.19.31'          # 与 mihomo 自身默认 UA 一致（机场可能按 UA 分流）
HEADER = ('# 由 tools/sanitize_provider.py 自动生成，请勿手工编辑\n'
          '# 上游: {src}\n'
          '# 生成时间: {ts}\n'
          '# 节点数: {kept}（原始 {total}，剔除 {dropped}）\n')

# ---------------------------------------------------------------- YAML 类型判定

GO_BOOL = {'true', 'True', 'TRUE', 'false', 'False', 'FALSE',
           'yes', 'Yes', 'YES', 'no', 'No', 'NO',
           'on', 'On', 'ON', 'off', 'Off', 'OFF', 'y', 'Y', 'n', 'N'}
GO_NULL = {'', '~', 'null', 'Null', 'NULL'}
GO_FLOAT_SPECIAL = {'.nan', '.NaN', '.NAN', '.inf', '.Inf', '.INF',
                    '+.inf', '+.Inf', '+.INF', '-.inf', '-.Inf', '-.INF'}
# 与 mihomo 用的 yaml.v3 里 yamlStyleFloat 保持一致
YAML_STYLE_FLOAT = re.compile(r'^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$')


def _go_int(plain):
    """模拟 Go strconv.ParseInt(s, 0, 64)（前导 0 = 八进制），失败返回 None。"""
    s = plain
    if s[:1] in ('+', '-'):
        s = s[1:]
    if not s:
        return None
    try:
        if s[:2] in ('0x', '0X'):
            return int(s[2:], 16)
        if s[:2] in ('0b', '0B'):
            return int(s[2:], 2)
        if s[:2] in ('0o', '0O'):
            return int(s[2:], 8)
        if len(s) > 1 and s[0] == '0':
            return int(s, 8)
        return int(s, 10)
    except ValueError:
        return None


def go_resolve(raw):
    """粗略模拟 yaml.v3 对标量的类型判定，返回 str/int/float/bool/null/timestamp。"""
    if raw in GO_NULL:
        return 'null'
    if raw in GO_BOOL:
        return 'bool'
    if raw in GO_FLOAT_SPECIAL:
        return 'float'
    if raw and raw[0] in '-+.0123456789':
        plain = raw.replace('_', '')
        if _go_int(plain) is not None:
            return 'int'
        if YAML_STYLE_FLOAT.match(plain):
            return 'float'
        if re.match(r'^[-+]?\d{4}-\d{1,2}-\d{1,2}', plain):
            return 'timestamp'
    return 'str'


# ---------------------------------------------------------------- 节点校验

def b64url_len(text):
    if not text:
        return 0
    try:
        return len(base64.b64decode(str(text) + '=' * (-len(str(text)) % 4), altchars=b'-_'))
    except Exception:
        return -1


def short_id_ok(text):
    s = str(text)
    if len(s) % 2 or not re.fullmatch(r'[0-9a-fA-F]*', s):
        return False
    return len(s) // 2 <= 8          # tls.RealityMaxShortIDLen = 8


def scalar(node):
    """取 ScalarNode 的文本（非标量返回 None）。"""
    return node.value if isinstance(node, yaml.ScalarNode) else None


def map_get(mapping_node, key):
    if not isinstance(mapping_node, yaml.MappingNode):
        return None
    for k, v in mapping_node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def validate_node(node):
    """返回 (是否保留, 丢弃原因)。只看会导致 mihomo 报错的硬性字段。"""
    if not isinstance(node, yaml.MappingNode):
        return False, '不是映射'
    name = scalar(map_get(node, 'name'))
    ntype = scalar(map_get(node, 'type'))
    server = scalar(map_get(node, 'server'))
    port = scalar(map_get(node, 'port'))
    if not name or not ntype or not server:
        return False, '缺 name/type/server'
    if not port:
        return False, '缺 port'
    ro = map_get(node, 'reality-opts')
    if isinstance(ro, yaml.MappingNode):
        pk = scalar(map_get(ro, 'public-key'))
        if pk is not None and b64url_len(pk) != 32:
            return False, 'reality public-key 非法(%r)' % pk[:24]
        sid = scalar(map_get(ro, 'short-id'))
        if sid and not short_id_ok(sid):
            return False, 'reality short-id 非法(%r)' % sid[:24]
    return True, ''


def fix_styles(node, repairs, path=''):
    """递归给"Go 会当数字、Python 当字符串"的标量强制加引号。"""
    if isinstance(node, yaml.ScalarNode):
        # style is None 表示上游写的是裸标量；已经带引号的不用动
        if node.tag == 'tag:yaml.org,2002:str' and node.style is None and go_resolve(node.value) != 'str':
            node.style = '"'
            repairs.append('%s=%r -> 加引号' % (path or '(scalar)', node.value[:40]))
        return
    if isinstance(node, yaml.SequenceNode):
        for i, child in enumerate(node.value):
            fix_styles(child, repairs, '%s[%d]' % (path, i))
    elif isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            key = k.value if isinstance(k, yaml.ScalarNode) else '?'
            fix_styles(v, repairs, ('%s.%s' % (path, key)) if path else key)


# ---------------------------------------------------------------- 主流程

def sanitize(text, src_label):
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        raise SystemExit('上游内容不是 YAML 映射，无法处理')
    seq = map_get(root, 'proxies')
    if not isinstance(seq, yaml.SequenceNode):
        raise SystemExit('上游内容没有 proxies 列表')
    total = len(seq.value)
    kept, dropped, seen = [], {}, set()
    for node in seq.value:
        ok, why = validate_node(node)
        if ok:
            name = scalar(map_get(node, 'name'))
            if name in seen:
                ok, why = False, '重名'
            else:
                seen.add(name)
        if ok:
            kept.append(node)
        else:
            key = why.split('(')[0]
            dropped.setdefault(key, []).append(why)
    seq.value = kept

    repairs = []
    for node in kept:
        nm = scalar(map_get(node, 'name')) or '?'
        fix_styles(node, repairs, nm)

    # 只保留 proxies 一个键（上游的 dns/rules/proxy-groups 对 provider 无意义）
    key_node = yaml.ScalarNode('tag:yaml.org,2002:str', 'proxies')
    out_root = yaml.MappingNode('tag:yaml.org,2002:map', [(key_node, seq)])

    buf = io.StringIO()
    buf.write(HEADER.format(src=src_label, ts=time.strftime('%Y-%m-%d %H:%M:%S'),
                            kept=len(kept), total=total, dropped=total - len(kept)))
    yaml.serialize(out_root, buf)

    # "坏节点签名"：只由"剔除了哪些原因 + 修复了哪些"决定，不看数量、不看时间。
    # 供 tools/keepalive.py 判断"上游的坏节点集合是否变了"（重名不算 —— 那只是常规噪音，
    # mihomo 自己会跳过，把它算进去会让签名每小时都变，白刷提交历史）。
    sig_src = '\n'.join(sorted('%s|%s' % (k, x) for k, v in dropped.items() if k != '重名' for x in v)) \
              + '\n--\n' + '\n'.join(sorted(repairs))
    bad_signature = hashlib.sha1(sig_src.encode('utf-8')).hexdigest()[:16]

    return buf.getvalue(), {
        'source': src_label,
        'total': total,
        'kept': len(kept),
        'dropped_count': total - len(kept),
        'dropped': {k: v[:10] for k, v in dropped.items()},
        'dropped_reasons': {k: len(v) for k, v in dropped.items()},
        'quoted_scalars': repairs[:40],
        'quoted_count': len(repairs),
        'bad_signature': bad_signature,
    }


def fetch(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise SystemExit('拉取上游失败：%r' % (last,))


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def core_check(core, provider_text, min_nodes=1, timeout=90):
    """起临时实例，确认清洗后的文件能被内核解析出 >= min_nodes 个节点。"""
    home = tempfile.mkdtemp(prefix='sanitize-check-')
    try:
        with open(os.path.join(home, 'provider.yaml'), 'w', encoding='utf-8') as fh:
            fh.write(provider_text)
        port = free_port()
        cfg = {
            'mode': 'rule', 'log-level': 'info',
            'mixed-port': free_port(),
            'external-controller': '127.0.0.1:%d' % port,
            'secret': 'sanitize-check',
            'proxy-providers': {'chk': {'type': 'file', 'path': './provider.yaml',
                                        'health-check': {'enable': False}}},
            'proxy-groups': [{'name': 'chk', 'type': 'select', 'use': ['chk']}],
            # 只用 MATCH，避免依赖 geosite/geoip 数据文件
            'rules': ['MATCH,chk'],
        }
        cfg_path = os.path.join(home, 'config.yaml')
        with open(cfg_path, 'w', encoding='utf-8') as fh:
            yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
        log_path = os.path.join(home, 'run.log')
        with open(log_path, 'w', encoding='utf-8') as fh:
            proc = subprocess.Popen([core, '-f', cfg_path, '-d', home], stdout=fh, stderr=subprocess.STDOUT)
        count, err, t0 = 0, None, time.time()
        while time.time() - t0 < timeout:
            time.sleep(2)
            txt = ''
            try:
                txt = open(log_path, 'r', encoding='utf-8', errors='replace').read()
            except Exception:       # noqa: BLE001
                pass
            m = re.search(r'initial proxy provider chk error: ([^\n"]+)', txt)
            if m:
                err = m.group(1)
                break
            try:
                req = urllib.request.Request('http://127.0.0.1:%d/providers/proxies' % port,
                                             headers={'Authorization': 'Bearer sanitize-check'})
                with urllib.request.urlopen(req, timeout=5) as r:
                    j = json.loads(r.read().decode('utf-8'))
                provs = j.get('providers')
                seq = list(provs.values()) if isinstance(provs, dict) else (provs or [])
                for x in seq:
                    if isinstance(x, dict) and x.get('name') == 'chk':
                        count = len(x.get('proxies') or [])
                if count:
                    break
            except Exception:       # noqa: BLE001
                pass
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except Exception:           # noqa: BLE001
            proc.kill()
        if err:
            return False, '内核拒绝：%s' % err
        if count < min_nodes:
            return False, '内核只加载出 %d 个节点' % count
        return True, '内核加载 %d 个节点' % count
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------- 自检

SELFTEST_SRC = """
proxies:
  - {name: good-1, type: ss, server: 192.0.2.1, port: 8388, cipher: aes-128-gcm, password: ok}
  - name: bad-reality
    type: vless
    server: 192.0.2.2
    port: 443
    uuid: 3e7cede4-721a-4807-b0a2-5fe6586af907
    reality-opts: {public-key: enabled}
  - name: trap-sid
    type: vless
    server: 192.0.2.3
    port: 443
    uuid: 57beeeb0-03fc-4fc9-94da-7604488a7134
    reality-opts:
      public-key: CGR1XzsRlvVmgeAiqLiA9SwEnxgtkbmANavMhmn6IVc
      short-id: 062898e8
  - {name: trap-pw, type: ss, server: 192.0.2.4, port: 8388, cipher: aes-128-gcm, password: 08}
  - {name: dup, type: ss, server: 192.0.2.5, port: 8388, cipher: aes-128-gcm, password: x}
  - {name: dup, type: ss, server: 192.0.2.6, port: 8388, cipher: aes-128-gcm, password: y}
  - {name: no-port, type: ss, server: 192.0.2.7, cipher: aes-128-gcm, password: z}
"""


def selftest():
    out, stats = sanitize(SELFTEST_SRC, 'selftest')
    fails = []
    if stats['kept'] != 4:
        fails.append('kept=%d 预期 4' % stats['kept'])
    for expect in ['short-id: "062898e8"', 'password: "08"']:
        if expect not in out:
            fails.append('输出里没有 %r' % expect)
    if out.count('name: dup') != 1:
        fails.append('重名节点应只留 1 个，实际 %d 个' % out.count('name: dup'))
    for banned in ['bad-reality', 'no-port']:
        if banned in out:
            fails.append('输出里不该有 %r' % banned)
    if not any('public-key' in k for k in stats['dropped']):
        fails.append('没记录 public-key 丢弃原因：%s' % stats['dropped'])
    print('selftest 输出：')
    print(out)
    print('stats:', json.dumps(stats, ensure_ascii=False, indent=2))
    if fails:
        print('SELFTEST FAILED:', '; '.join(fails))
        return 1
    print('SELFTEST OK')
    return 0


def main():
    ap = argparse.ArgumentParser(description='清洗 mihomo proxy-provider 文件')
    ap.add_argument('--url', help='上游 URL')
    ap.add_argument('--in', dest='infile', help='本地输入文件（替代 --url）')
    ap.add_argument('--out', help='输出文件')
    ap.add_argument('--stats', help='统计写入的 json 文件')
    ap.add_argument('--core-check', dest='core', help='用指定 mihomo 可执行文件做端到端自检')
    ap.add_argument('--min-nodes', type=int, default=1, help='自检要求的最小节点数')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.out or not (args.url or args.infile):
        ap.error('需要 --url 或 --in，以及 --out')

    text = open(args.infile, 'r', encoding='utf-8', errors='replace').read() if args.infile else fetch(args.url)
    label = args.infile or args.url
    out_text, stats = sanitize(text, label)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(out_text)
    print('原始 %d -> 保留 %d（剔除 %d，修引号 %d）' % (
        stats['total'], stats['kept'], stats['dropped_count'], stats['quoted_count']))
    for k, v in stats['dropped'].items():
        print('  剔除[%s] x%d 例：%s' % (k, len(v), v[:3]))
    for q in stats['quoted_scalars'][:10]:
        print('  修引号：%s' % q)

    ok = True
    if args.core:
        ok, msg = core_check(args.core, out_text, args.min_nodes)
        stats['core_check'] = {'ok': ok, 'msg': msg}
        print('内核自检：%s —— %s' % ('通过' if ok else '失败', msg))
    if args.stats:
        with open(args.stats, 'w', encoding='utf-8') as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=2)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
