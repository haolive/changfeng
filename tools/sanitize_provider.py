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
6) 给"必须有 uTLS 但指纹不被 mihomo 支持"的节点补/改 `client-fingerprint: chrome`：
   带 reality-opts 或 flow 的节点如果指纹是空 / `none` / `unsafe`（Xray 写法）之类，
   mihomo 会退化成原生 TLS，然后报 `wrong clientFingerprint:...` 或
   `vision: not a valid supported TLS connection`，该节点拨号直接失败。

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


# ---------------------------------------------------------------- 写出去的 YAML 也要防同一个坑

class GoSafeDumper(yaml.SafeDumper):
    """safe_dump 时给"Go 会当数字/布尔/null"的字符串强制加引号。

    为什么要单独一个 Dumper：fix_styles() 只修**从上游读进来的那棵树**；而流水线里
    节点会被 PyYAML 读成普通 dict 再重新落盘（合并、改名前缀、写测速配置、写产物），
    这一读一写就会把引号弄丢 —— Python 觉得 '062898e8' 是字符串所以裸着写，
    Go 的 yaml.v3 却按科学计数法读成 6.2898e12，于是：
        invalid REALITY short ID / 密码对不上 / 端口变成别的东西
    用这个 Dumper，写出去的文件和上游清洗过的版本一样安全。
    """


def _go_safe_str(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data,
                                   style='"' if go_resolve(data) != 'str' else None)


GoSafeDumper.add_representer(str, _go_safe_str)


def dump_go_safe(data, stream=None):
    """safe_dump 的 Go 友好版（其余参数按本项目习惯固定）。"""
    return yaml.dump(data, stream, Dumper=GoSafeDumper, allow_unicode=True,
                     sort_keys=False, width=4096, default_flow_style=False)


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


# mihomo component/tls/utls.go 的 GetFingerprint() 认识的值；"none" 和空值会被静默忽略
# （= 不走 uTLS），其它值会打 `wrong clientFingerprint:<值>` 警告。
KNOWN_FINGERPRINTS = {'chrome', 'firefox', 'safari', 'ios', 'android', 'edge', '360', 'qq',
                      'random', 'randomized'}


def fingerprint_ok(value):
    """mihomo 能不能对这个 client-fingerprint 用 uTLS。"""
    return str(value or '').strip().lower() in KNOWN_FINGERPRINTS


def needs_utls(node):
    """该节点是否必须有 uTLS 才能工作：

    * 带 reality-opts → mihomo 直接报 "REALITY is based on uTLS, please set a client-fingerprint"；
    * 带 flow（xtls-rprx-vision）→ 拿不到合法 TLS 连接时报
      "vision: not a valid supported TLS connection"。
    两种情况都会让这个节点拨号失败（只死它自己，不像非法字段那样废掉整个 provider）。
    """
    if map_get(node, 'reality-opts') is not None:
        return True
    return bool(str(scalar(map_get(node, 'flow')) or '').strip())


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


def fix_fingerprints(nodes, repairs):
    """修 `client-fingerprint`：mihomo 不认的值一律改成 chrome，缺失的按需补上。

    上游会把 Xray 的 `fingerprint: unsafe`（含义是"不用 uTLS"）照抄成
    `client-fingerprint: unsafe`，或者干脆不写这个字段。对 mihomo 的影响分两种：

    * 节点**带 reality-opts 或 flow**：没有可用指纹时直接拨号失败
      （日志 `wrong clientFingerprint:unsafe` / `vision: not a valid supported TLS connection`）
      → 必须补上 `client-fingerprint: chrome`；
    * 节点**不需要 uTLS**：能连上，但每次握手都打一行 `wrong clientFingerprint:<值>` 警告
      → 把这种"写了但不认识的值"也改成 chrome，让日志安静（**缺失**的字段不动，
      否则会给成百上千个正常节点凭空加字段）。

    这类问题只死单个节点，不像非法字段那样废掉整个 provider；但既然能修就修。
    """
    fixed = 0
    for node in nodes:
        fp = map_get(node, 'client-fingerprint')
        old = scalar(fp)
        if fingerprint_ok(old):
            continue
        if fp is None and not needs_utls(node):
            continue
        if fp is not None:
            fp.value = 'chrome'
        else:
            node.value.append((yaml.ScalarNode('tag:yaml.org,2002:str', 'client-fingerprint'),
                               yaml.ScalarNode('tag:yaml.org,2002:str', 'chrome')))
        name = scalar(map_get(node, 'name')) or '?'
        repairs.append('%s.client-fingerprint=%r -> chrome（%s）' % (
            name, old, '否则该节点拨不通' if needs_utls(node) else '消除内核告警'))
        fixed += 1
    return fixed


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

    quote_repairs = []
    for node in kept:
        nm = scalar(map_get(node, 'name')) or '?'
        fix_styles(node, quote_repairs, nm)
    fp_repairs = []
    fp_fixed = fix_fingerprints(kept, fp_repairs)
    repairs = quote_repairs + fp_repairs

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
        'quoted_scalars': quote_repairs[:40],
        'quoted_count': len(quote_repairs),
        'fingerprints': fp_repairs[:40],
        'fingerprint_fixed_count': fp_fixed,
        'repairs': repairs[:40],
        'repair_count': len(repairs),
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
  - name: fp-unsafe-vision
    type: vless
    server: 192.0.2.8
    port: 443
    uuid: 5be7fb02-b6a5-450f-b041-3243b98e8420
    flow: xtls-rprx-vision
    client-fingerprint: unsafe
  - name: fp-missing-reality
    type: vless
    server: 192.0.2.9
    port: 443
    uuid: 5be7fb02-b6a5-450f-b041-3243b98e8420
    reality-opts:
      public-key: Uvj5H9pDJP0HX2bN7NN7sCQwVCrC5N2NbKf-yuy1ikE
  - {name: fp-unsafe-plain, type: ss, server: 192.0.2.10, port: 8388, cipher: aes-128-gcm, password: q, client-fingerprint: unsafe}
"""


def selftest():
    out, stats = sanitize(SELFTEST_SRC, 'selftest')
    fails = []
    if stats['kept'] != 7:
        fails.append('kept=%d 预期 7' % stats['kept'])
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
    # 指纹修复：3 个需要 uTLS 的（补上）+ 1 个写了 unsafe 的普通节点（改掉）
    if stats.get('fingerprint_fixed_count') != 4:
        fails.append('指纹修复数=%s 预期 4' % stats.get('fingerprint_fixed_count'))
    seg = out.split('name: fp-unsafe-vision')[1].split('- name:')[0]
    if 'client-fingerprint: chrome' not in seg:
        fails.append('fp-unsafe-vision 的指纹没被改成 chrome')
    seg2 = out.split('name: fp-missing-reality')[1].split('- name:')[0]
    if 'client-fingerprint: chrome' not in seg2:
        fails.append('fp-missing-reality 没补上 client-fingerprint: chrome')
    seg3 = out.split('name: fp-unsafe-plain')[1]
    if 'client-fingerprint: chrome' not in seg3:
        fails.append('写了未知指纹的普通节点也应改成 chrome（消除内核告警）')
    # dump_go_safe：PyYAML 读进来再写出去时也得保住引号（流水线里会往返好几轮）
    dumped = dump_go_safe({'proxies': [{'name': '1', 'type': 'ss', 'port': 8388,
                                        'password': '08', 'short-id': '062898e8'}]})
    for expect in ['password: "08"', 'short-id: "062898e8"', 'name: "1"']:
        if expect not in dumped:
            fails.append('dump_go_safe 没给陷阱标量加引号（缺 %r）：\n%s' % (expect, dumped))
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
    print('原始 %d -> 保留 %d（剔除 %d，修引号 %d，修指纹 %d）' % (
        stats['total'], stats['kept'], stats['dropped_count'],
        stats['quoted_count'], stats.get('fingerprint_fixed_count', 0)))
    for k, v in stats['dropped'].items():
        print('  剔除[%s] x%d 例：%s' % (k, len(v), v[:3]))
    for q in stats['quoted_scalars'][:10]:
        print('  修引号：%s' % q)
    for q in stats.get('fingerprints', [])[:10]:
        print('  修指纹：%s' % q)

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
