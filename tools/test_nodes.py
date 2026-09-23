#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对节点池逐个测速，产出"只留活节点"的订阅（best.yaml / nodes.yaml）。

怎么测
------
起一个**临时** mihomo 实例（隔离的 home 目录 + 空闲端口 + 节点名改成 n0000 这样的别名，
避免名字里的 emoji/中文/竖线在 API 路径和 YAML 里出幺蛾子），分两轮：

  1) 延迟轮：对每个节点调 `/proxies/<别名>/delay`（等价于客户端里的健康检查），
     超时或报错的直接淘汰；这一轮把 5000+ 个节点砍到几百个。
  2) 测速轮：给活下来的节点各开一个 `listeners` 入站（`proxy:` 字段绑定到该节点，
     见 mihomo 的 "入站监听器" 文档），再用本机 HTTP 代理的方式从 speed.cloudflare.com
     真下 512KB —— 只有"能握手但传输废掉"的节点会在这轮现形（免费池里这类很多：
     握手 200，一下载就龟速或断流）。

为什么不用 `/proxies/<名>/delay` 传大文件当测速：那个接口量的是**首字节**耗时，不是带宽。

产出
----
  * best.yaml  —— 完整配置：把「多订阅合并配置.yaml」里的 proxy-providers 段
                  换成测速后的 inline `proxies`，其余（DNS/策略组/分流规则/广告规则集）原样保留。
                  客户端里直接当订阅加进去就行，等于"你原来那份配置，但废节点被清掉了"。
  * nodes.yaml —— 只有 proxies 的清单，想自己组装配置的话把它当 proxy-provider 用。

用法
----
  python tools/test_nodes.py --pool dist/pool.yaml --config 多订阅合并配置.yaml \
      --core ./mihomo --out dist/best.yaml --nodes-out dist/nodes.yaml --stats dist/filter-stats.json
  # 本机冒烟：--limit 200 只测前 200 个节点
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit('需要 PyYAML：pip install pyyaml')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sanitize_provider as sp  # noqa: E402  （借它的 Go 友好 dumper：写出去的 YAML 不能把引号弄丢）


def dns_block(servers, ipv6=True):
    """给测试配置写 dns 段。

    默认**不启用 DNS**（mihomo 直接用系统解析器）—— 在境外 runner 上没问题。
    但在国内本机跑的时候，系统 DNS 对 Google/Cloudflare 这类域名可能给出被污染的结果，
    于是 mihomo 会把错的 IP 交给节点去连 → 好节点也被判死。
    所以本机跑建议用 `--dns-doh https://doh.pub/dns-query --dns-doh https://dns.alidns.com/dns-query`
    （和客户端配置一致），这样测试目标和流水线完全一样，只有**测速点**不同。
    """
    if not servers:
        return {'enable': False}
    return {'enable': True, 'ipv6': ipv6, 'enhanced-mode': 'normal',
            'nameserver': list(servers),
            'default-nameserver': ['223.5.5.5', '119.29.29.29']}


def min_config(proxies, listeners=None, dns=None):
    """测速用最小配置：不要 geo 规则，起得快。"""
    cfg = {
        'log-level': 'warning',
        'mode': 'rule',
        'port': 0, 'socks-port': 0, 'mixed-port': free_port(),
        'redir-port': 0, 'tproxy-port': 0,
        'external-controller': '127.0.0.1:%d' % free_port(),
        'secret': 'node-test',
        'unified-delay': True,        # 与客户端配置一致，阈值才有可比性
        'tcp-concurrent': True,
        'find-process-mode': 'off',
        'dns': dns or {'enable': False},
        'proxies': proxies,
        'rules': ['MATCH,DIRECT'],
    }
    if listeners:
        cfg['listeners'] = listeners
    return cfg


def write_config(path, cfg):
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        sp.dump_go_safe(cfg, fh)
    return path


def ensure_dir(path):
    if not path:
        return
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)


def core_test(core, cfg_path, workdir, timeout=180):
    """跑 `mihomo -t`：只校验配置能不能解析（不监听端口、不联网）。"""
    try:
        p = subprocess.run([core, '-t', '-f', cfg_path, '-d', workdir],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, text=True, errors='replace')
        return p.returncode, p.stdout or ''
    except subprocess.TimeoutExpired:
        return -1, 'mihomo -t 超时'


def prune_bad_nodes(core, nodes, workdir, max_drop=50):
    """内核预检自愈：把"能让整个配置解析失败"的节点剔掉。

    mihomo 解析 **inline proxies** 时，遇到字段缺失/非法的节点不是跳过它，而是整体报错
    （例如 `Parse config error: proxy 2: '' has unset fields: cipher`），
    照这样跑下去整个池子都进不了内核 —— 而 tools/sanitize_provider.py 的规则
    （name/type/server/port + REALITY 字段）覆盖不到"缺必填字段"这一类。
    所以这里用 `mihomo -t` 预检，按内核报的下标逐个剔除，直到配置能过为止。

    返回 (被剔除的 [(名字, 内核报的原因)], 无法定位时的原始报错)。
    """
    dropped = []
    for _ in range(max_drop + 1):
        cfg_path = write_config(os.path.join(workdir, 'precheck.yaml'), min_config(nodes))
        rc, out = core_test(core, cfg_path, workdir)
        if rc == 0:
            return dropped, ''
        hits = re.findall(r'proxy (\d+)(?:\s+error)?:\s*([^\n"]*)', out)
        if not hits:
            return dropped, out.strip()[-500:]
        idx, reason = int(hits[-1][0]), hits[-1][1].strip()
        if idx >= len(nodes):
            return dropped, out.strip()[-500:]
        name = str(nodes[idx].get('name', '?'))
        dropped.append((name, reason[:100]))
        print('  预检剔除 #%d %s —— %s' % (idx, name[:40], reason[:80]))
        del nodes[idx]
    return dropped, '预检剔除次数超过上限 %d' % max_drop


class Core:
    """临时 mihomo 实例：起进程 + 调 API（用完必须 stop）。"""

    def __init__(self, core, proxies, listeners, log_path, dns=None, start_timeout=60):
        self.core, self.log_path = core, log_path
        self.home = tempfile.mkdtemp(prefix='node-test-')
        cfg = min_config(proxies, listeners, dns)
        self.ctrl = int(cfg['external-controller'].rsplit(':', 1)[1])
        self.secret = cfg['secret']
        self.cfg_path = write_config(os.path.join(self.home, 'config.yaml'), cfg)
        self._logfh = open(log_path, 'a', encoding='utf-8')
        self.proc = subprocess.Popen([core, '-f', self.cfg_path, '-d', self.home],
                                     stdout=self._logfh, stderr=subprocess.STDOUT)
        self.ready = False
        t0 = time.time()
        while time.time() - t0 < start_timeout:
            time.sleep(0.5)
            if self.proc.poll() is not None:
                break
            st, _ = self.api('/version', timeout=3)
            if st == 200:
                self.ready = True
                break

    def api(self, path, timeout=30):
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.ctrl, path),
                                     headers={'Authorization': 'Bearer ' + self.secret})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', 'replace')
        except Exception as e:  # noqa: BLE001
            return 0, repr(e)

    def delay(self, alias, url, timeout_ms, retries=2):
        """返回 (延迟ms, 失败原因)。

        `st == 0` 表示**连本地内核的 HTTP 都没连上**（本机防火墙/杀软/端口紧张时会被 RST），
        这跟节点好坏无关，重试几次再说 —— 否则本机跑出来的"可用率"会被这种抖动严重低估
        （实测一次 5872 节点的本机运行里，有 815 条是这种本地 RST）。
        """
        last = ''
        for i in range(retries + 1):
            q = urllib.parse.urlencode({'url': url, 'timeout': timeout_ms})
            st, body = self.api('/proxies/%s/delay?%s' % (urllib.parse.quote(alias, safe=''), q),
                                timeout=timeout_ms / 1000.0 + 20)
            if st == 200:
                try:
                    j = json.loads(body)
                    if isinstance(j, dict) and 'delay' in j:
                        return int(j['delay']), ''
                except ValueError:
                    pass
            try:
                msg = json.loads(body).get('message', body)
            except ValueError:
                msg = body
            msg = re.sub(r'\s+', ' ', str(msg))[:60]
            if 'timeout' in msg.lower():
                msg = '超时'
            elif 'error occurred in the delay test' in msg:
                msg = '连接失败'
            msg = msg or ('HTTP %s' % st)
            if st != 0:
                return None, msg        # 内核给了明确答复（超时/连接失败）→ 是节点的问题
            last = msg
            time.sleep(0.3 * (i + 1))
        return None, '本地内核 API 连不上（%s）' % last

    def log_tail(self, lines=25):
        try:
            with open(self.log_path, 'r', encoding='utf-8', errors='replace') as fh:
                return ''.join(fh.readlines()[-lines:])
        except Exception:  # noqa: BLE001
            return ''

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.communicate(timeout=15)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self._logfh.close()
        shutil.rmtree(self.home, ignore_errors=True)


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def is_free(port):
    try:
        s = socket.socket()
        s.bind(('127.0.0.1', port))
        s.close()
        return True
    except OSError:
        return False


def has_ipv6(timeout=3):
    """测试机有没有 IPv6 出口。GitHub runner 默认没有，这时候 IPv6 字面量节点没法测。"""
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(('2606:4700:4700::1111', 443))    # Cloudflare 的 IPv6 地址，不需要 DNS
        s.close()
        return True
    except OSError:
        return False


def is_ipv6_literal(server):
    """server 字段是不是 IPv6 字面量地址。

    别用 `':' in server` 糊弄：免费池里有 `server: 用户@主机:443?参数` 这种垃圾节点
    （冒号是有的），那样会被当成 IPv6 节点"跳过测速、原样保留"—— 等于把垃圾放进了产物。
    """
    s = str(server or '').strip().strip('[]')
    try:
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv6Address)
    except ValueError:
        return False


def is_domain_server(server):
    """server 是不是域名（而不是裸 IP）—— 域名型的多是 CDN 前置，国内可达率明显更高。"""
    s = str(server or '').strip()
    if not s or ':' in s:
        return False
    try:
        ipaddress.ip_address(s)
        return False          # 能当 IP 解析 → 不是域名
    except ValueError:
        return True


def endpoint_key(node):
    """落地端点身份（不含凭据）：同一台机器 + 同一个伪装参数 = 同一个节点。"""
    return json.dumps({k: node.get(k) for k in
                       ('type', 'server', 'port', 'sni', 'servername', 'network',
                        'ws-opts', 'grpc-opts', 'reality-opts')}, sort_keys=True, ensure_ascii=False)


def download_speed(port, urls, want_bytes, timeout):
    """经本地 HTTP 代理（listener）下载，返回 (拿到字节, 秒, 用到的 url, 错误文本)。

    多个 url 是"兜底"：某个 CDN 从节点出口不可达时（免费池里很常见），换一个再试；
    只要读到过数据就不再换 —— 那属于"太慢"，不是"不通"。
    """
    op = urllib.request.build_opener(urllib.request.ProxyHandler(
        {'http': 'http://127.0.0.1:%d' % port, 'https': 'http://127.0.0.1:%d' % port}))
    last_err = ''
    for url in urls:
        t0 = time.monotonic()
        got = 0
        try:
            with op.open(url, timeout=timeout) as r:
                while got < want_bytes:
                    chunk = r.read(min(65536, want_bytes - got))
                    if not chunk:
                        break
                    got += len(chunk)
                    if time.monotonic() - t0 > timeout:
                        break
            if got:
                return got, time.monotonic() - t0, url, ''
        except Exception as e:  # noqa: BLE001
            last_err = '%s: %s' % (type(e).__name__, re.sub(r'\s+', ' ', str(e))[:60])
    return 0, 0.0, urls[0], last_err or '没读到数据'


def speed_fail_kinds(speed_fail):
    """把测速失败明细归成"几类"，好看日志（同一个原因只算一类）。"""
    kinds = {}
    for kind, detail in speed_fail.values():
        key = kind if kind == '速度不足' else '%s(%s)' % (kind, detail[:48])
        kinds[key] = kinds.get(key, 0) + 1
    return kinds


def build_output_config(template_path, nodes, header):
    """把模板配置里的 proxy-providers 换成 inline proxies。"""
    with open(template_path, 'r', encoding='utf-8') as fh:
        cfg = yaml.safe_load(fh) or {}
    out = {}
    inserted = False
    for k, v in cfg.items():
        if k == 'proxy-providers':
            out['proxies'] = nodes
            inserted = True
            continue
        out[k] = v
    if not inserted:
        out['proxies'] = nodes
    return header + sp.dump_go_safe(out)


def main():
    ap = argparse.ArgumentParser(description='节点池测速 + 产出过滤后的订阅')
    ap.add_argument('--pool', required=True, help='tools/fetch_node_pool.py 产出的节点池')
    ap.add_argument('--config', default='多订阅合并配置.yaml', help='配置模板（产出 best.yaml 用）')
    ap.add_argument('--core', required=True, help='mihomo 可执行文件')
    ap.add_argument('--out', help='输出完整配置（best.yaml）')
    ap.add_argument('--nodes-out', help='输出纯节点清单（nodes.yaml）')
    ap.add_argument('--stats', help='统计写入的 json')
    ap.add_argument('--pool-stats', help='合并第一个阶段的统计（给保活脚本用）')
    ap.add_argument('--notes', help='写一份发布说明（release notes）')
    ap.add_argument('--core-log', default='dist/core.log', help='临时内核实例的日志（排查用）')
    # 阈值
    ap.add_argument('--latency-url', default='https://www.gstatic.com/generate_204',
                    help='延迟测试地址（与客户端健康检查一致）')
    ap.add_argument('--latency-timeout', type=int, default=3000, help='单个节点的延迟测试超时（毫秒）')
    ap.add_argument('--max-latency', type=int, default=2000, help='延迟超过这个值就不要了（毫秒）')
    ap.add_argument('--speed-url', default='https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb',
                    help='测速下载地址（默认用 Google CDN 的大文件：延迟轮也是 Google 家族的地址，'
                         '能通 gstatic 的节点基本都能通它；{bytes} 会被替换成测试块大小）')
    ap.add_argument('--speed-url-fallback', action='append', default=None,
                    help='第一个地址不可达（一个字节都没读到）时换它再试。可重复指定；'
                         '传空字符串可关掉兜底（不传则默认用 Cloudflare 的测速端点）')
    ap.add_argument('--speed-bytes', type=int, default=512 * 1024, help='测速下载的字节数')
    ap.add_argument('--speed-timeout', type=float, default=10.0, help='测速下载的超时（秒）')
    ap.add_argument('--min-speed-kbps', type=float, default=100.0, help='低于这个速度就不要了（KB/s）')
    ap.add_argument('--speed-limit', type=int, default=800, help='最多给多少个（延迟最优的）节点做下载测速')
    ap.add_argument('--max-nodes', type=int, default=600, help='最终订阅里最多留多少个节点')
    ap.add_argument('--concurrency', type=int, default=64, help='并发数（延迟轮）')
    ap.add_argument('--min-keep', type=int, default=100, help='活下来的节点少于这个数就判失败（不发布）')
    ap.add_argument('--limit', type=int, help='只测前 N 个节点（本机冒烟用）')
    ap.add_argument('--dns-doh', action='append', default=None,
                    help='给测试用的内核配上 DoH DNS（可重复指定，如 --dns-doh https://doh.pub/dns-query）。'
                         '默认不启用 DNS（用系统解析器）：境外 runner 上没问题，但国内本机跑时系统 DNS 可能'
                         '给出被污染的解析结果，好节点会被冤枉，建议本机跑时配上和客户端一致的 DoH')
    ap.add_argument('--fast-out', help='额外产出"优先订阅"完整配置（只留经验上国内可达率高的类型）')
    ap.add_argument('--fast-nodes-out', help='额外产出"优先订阅"的纯节点清单')
    ap.add_argument('--fast-notes', help='写一份"优先订阅"的发布说明')
    ap.add_argument('--fast-types', default='http,anytls',
                    help='优先订阅保留哪些节点类型（逗号分隔）。依据：2026-09-24 实测 http 62%%、'
                         'anytls 67%% 国内可达，而 vmess 1.5%%、ss 1.6%%、vless 9.9%%')
    ap.add_argument('--fast-domains', action='store_true',
                    help='优先订阅里也保留"server 是域名"的节点（可达率约 35%%，能多留约 10%% 的可用节点）')
    ap.add_argument('--fast-max-nodes', type=int, help='优先订阅最多留多少个（默认同 --max-nodes）')
    ap.add_argument('--source-label', help='产物头部"源模板"显示的名字（默认取 --config 的文件名，'
                                           '不要写绝对路径 —— 产物会公开发布）')
    ap.add_argument('--test-location', default='GitHub runner（境外机房）',
                    help='产物头部"测速环境"的写法（本机跑的时候改成如实描述）')
    ap.add_argument('--ipv6-policy', choices=['keep', 'drop'], default='keep',
                    help='测试机没有 IPv6 时，IPv6 字面量节点怎么办：keep=原样保留（默认，不冤枉好节点）/ drop=删掉')
    a = ap.parse_args()

    t_start = time.time()
    with open(a.pool, 'r', encoding='utf-8') as fh:
        nodes = (yaml.safe_load(fh) or {}).get('proxies') or []
    if not nodes:
        print('节点池是空的：%s' % a.pool)
        return 1
    if a.limit:
        nodes = nodes[:a.limit]
    print('节点池 %d 个' % len(nodes))
    dns_cfg = dns_block(a.dns_doh)
    if a.dns_doh:
        print('测试内核使用 DoH DNS：%s' % ', '.join(a.dns_doh))

    for p in (a.core_log, a.out, a.nodes_out, a.stats, a.notes):
        ensure_dir(p)
    open(a.core_log, 'w', encoding='utf-8').close()     # 每次运行清空

    # ---- IPv6：测试机没有出口时，IPv6 字面量节点没法测 -------------------------
    v6_nodes = [n for n in nodes if is_ipv6_literal(n.get('server'))]
    testable = [n for n in nodes if not is_ipv6_literal(n.get('server'))]
    v6_kept, v6_dropped, test_host_has_v6 = [], [], None
    if v6_nodes:
        test_host_has_v6 = has_ipv6()
        if test_host_has_v6:
            print('测试机有 IPv6，%d 个 IPv6 节点正常参与测速' % len(v6_nodes))
            testable.extend(v6_nodes)
        elif a.ipv6_policy == 'keep':
            v6_kept = v6_nodes
            print('测试机没有 IPv6：%d 个 IPv6 字面量节点跳过测速、原样保留（--ipv6-policy drop 可以删掉）'
                  % len(v6_nodes))
        else:
            v6_dropped = v6_nodes
            print('测试机没有 IPv6：%d 个 IPv6 字面量节点按 --ipv6-policy drop 删掉' % len(v6_nodes))

    alias_map = {}       # 别名 -> 原始节点
    alias_nodes = []     # 给内核用的（名字换成别名）
    for i, n in enumerate(testable):
        alias = 'n%05d' % i
        alias_map[alias] = n
        tn = dict(n)
        tn['name'] = alias
        alias_nodes.append(tn)

    # ---- 预检：把"会让整个配置解析失败"的节点剔掉（否则一个都测不成） ------------
    prune_drops = []          # [(真实名, 内核报的原因)]
    if alias_nodes:
        workdir = tempfile.mkdtemp(prefix='node-precheck-')
        print('\n内核预检（mihomo -t，%d 个节点）' % len(alias_nodes))
        raw_drops, err = prune_bad_nodes(a.core, alias_nodes, workdir)
        shutil.rmtree(workdir, ignore_errors=True)
        if err:
            print('预检没能定位到具体节点，内核报错：\n%s' % err)
            return 1
        for alias, why in raw_drops:
            node = alias_map.pop(alias, None)
            prune_drops.append(((node or {}).get('name', alias), why))
        print('预检通过：%d 个节点可被内核解析%s' % (
            len(alias_nodes), '，剔除 %d 个字段非法的' % len(prune_drops) if prune_drops else ''))

    # ---- 第一轮：延迟 ---------------------------------------------------------
    latency = {}
    failed_reason = {}
    if alias_nodes:
        print('\n延迟轮：%d 个节点，超时 %dms，并发 %d' % (len(alias_nodes), a.latency_timeout, a.concurrency))
        core = Core(a.core, alias_nodes, None, a.core_log, dns=dns_cfg)
        if not core.ready:
            print('内核启动失败：\n%s' % core.log_tail())
            core.stop()
            return 1
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            futs = {ex.submit(core.delay, al, a.latency_url, a.latency_timeout): al for al in alias_map}
            done = 0
            for f in as_completed(futs):
                al = futs[f]
                delay, why = f.result()
                done += 1
                if done % 500 == 0:
                    print('  %d/%d，已用 %.0fs' % (done, len(futs), time.time() - t0))
                if delay is not None:
                    latency[al] = delay
                else:
                    failed_reason[al] = why
        core.stop()
        alive = len(latency)
        print('延迟轮结束：%d/%d 可用，用时 %.0fs' % (alive, len(alias_nodes), time.time() - t0))
        dtype = {}
        for why in failed_reason.values():
            k = (why.split(' ')[0] or '未知')[:40]
            dtype[k] = dtype.get(k, 0) + 1
        if dtype:
            print('  未通过原因：%s' % '；'.join('%s ×%d' % kv for kv in
                                              sorted(dtype.items(), key=lambda kv: -kv[1])[:6]))
        if alive == 0:
            print('一个都没活下来 —— 内核日志尾部：\n%s' % core.log_tail())
    else:
        alive = 0
        dtype = {}

    too_slow = [al for al, dl in latency.items() if dl > a.max_latency]
    ok_latency = {al: dl for al, dl in latency.items() if dl <= a.max_latency}
    print('延迟 ≤%dms 的有 %d 个（>%dms 淘汰 %d 个）' % (a.max_latency, len(ok_latency), a.max_latency, len(too_slow)))

    # ---- 第二轮：下载测速 -----------------------------------------------------
    speed_pool = sorted(ok_latency, key=lambda al: ok_latency[al])[:a.speed_limit]
    speed, speed_fail, urls = {}, {}, []
    if speed_pool:
        port_base = 21000
        listeners, ports = [], {}
        for al in speed_pool:
            while not is_free(port_base):
                port_base += 1
            ports[al] = port_base
            listeners.append({'name': 'l-' + al, 'type': 'http', 'port': port_base,
                              'listen': '127.0.0.1', 'proxy': al})
            port_base += 1
        print('\n测速轮：%d 个节点，每节点下 %dKB，限时 %.0fs，低于 %.0f KB/s 淘汰' % (
            len(speed_pool), a.speed_bytes // 1024, a.speed_timeout, a.min_speed_kbps))
        core = Core(a.core, [n for n in alias_nodes if n['name'] in ports], listeners, a.core_log, dns=dns_cfg)
        if not core.ready:
            print('内核启动失败：\n%s' % core.log_tail())
            core.stop()
            return 1
        url = a.speed_url.format(bytes=a.speed_bytes)
        fb = a.speed_url_fallback if a.speed_url_fallback is not None \
            else ['https://speed.cloudflare.com/__down?bytes={bytes}']
        urls = [url] + [u.format(bytes=a.speed_bytes) for u in fb if u.strip()]
        _port_open(ports[speed_pool[0]])            # 先等第一个 listener 就绪（带重试）
        not_listening = [al for al in speed_pool if not _port_open(ports[al], retries=1)]
        for al in not_listening:
            speed_fail[al] = ('listener 未就绪', '')
        todo = [al for al in speed_pool if al not in speed_fail]
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=max(8, a.concurrency // 2)) as ex:
            futs = {ex.submit(download_speed, ports[al], urls, a.speed_bytes, a.speed_timeout): al for al in todo}
            done = 0
            for f in as_completed(futs):
                al = futs[f]
                got, dt, used, err = f.result()
                done += 1
                if done % 100 == 0:
                    print('  %d/%d，已用 %.0fs' % (done, len(futs), time.time() - t0))
                kbps = got / dt / 1024.0 if dt > 0 else 0.0
                if got >= a.speed_bytes and kbps >= a.min_speed_kbps:
                    speed[al] = round(kbps, 1)
                elif got:
                    speed_fail[al] = ('速度不足', '%.0f KB/s' % kbps)
                else:
                    speed_fail[al] = ('下载失败', err)
        core.stop()
        print('测速轮结束：%d/%d 合格，用时 %.0fs' % (len(speed), len(speed_pool), time.time() - t0))
        if speed_fail:
            print('  未通过原因：%s' % '；'.join('%s ×%d' % kv for kv in
                                              sorted(speed_fail_kinds(speed_fail).items(), key=lambda kv: -kv[1])[:6]))

    # ---- 汇总与去重 -----------------------------------------------------------
    scored = sorted(speed, key=lambda al: (ok_latency[al], -speed[al]))
    untested_ok = [al for al in sorted(ok_latency, key=lambda x: ok_latency[x]) if al not in speed
                   and al not in speed_fail]        # 超过 --speed-limit 没排上测速的

    def pick(aliases, cap, pred=None):
        """按"延迟优先、端点去重"的顺序取前 cap 个（pred 用于只挑某一类）。"""
        out, seen = [], set()
        for al in aliases:
            node = alias_map[al]
            if pred and not pred(node):
                continue
            k = endpoint_key(node)
            if k in seen:
                continue
            seen.add(k)
            out.append(al)
            if len(out) >= cap:
                break
        return out

    candidates = scored + untested_ok
    final_aliases = pick(candidates, a.max_nodes)
    final_nodes = [alias_map[al] for al in final_aliases] + v6_kept
    n_speed_in = sum(1 for al in final_aliases if al in speed)
    print('\n最终 %d 个节点 = 测速合格 %d + 仅延迟合格（没排上测速）%d + IPv6 未测速 %d' % (
        len(final_nodes), n_speed_in, len(final_aliases) - n_speed_in, len(v6_kept)))
    print('  测速合格共 %d 个、仅延迟合格共 %d 个，--max-nodes=%d' % (
        len(speed), len(untested_ok), a.max_nodes))

    # ---- 优先订阅（fast）：只留"经验上从国内连得通"的类型 ----------------------
    # 依据见 README-节点测速过滤.md：2026-09-24 实测（602 个节点、从大陆本机逐个 TCP 探测），
    # type=http 62.0% / anytls 66.7% 可达，而 vmess 1.5% / ss 1.6% / vless 9.9%；
    # 只留高可达率类型 → 列表从 602 缩到 161，但保住了 83% 的可用节点。
    # ⚠ 这是**经验筛选**，不是"从国内实测过"（GitHub runner 在境外，量不到那一跳）。
    fast_types = {t.strip().lower() for t in str(a.fast_types or '').split(',') if t.strip()}
    fast_aliases, fast_nodes, fb = [], [], {}

    def fast_ok(node):
        if str(node.get('type') or '').lower() in fast_types:
            return True
        return a.fast_domains and is_domain_server(node.get('server'))

    if a.fast_out or a.fast_nodes_out:
        fast_aliases = pick(candidates, a.fast_max_nodes or a.max_nodes, fast_ok)
        fast_nodes = [alias_map[al] for al in fast_aliases] + [n for n in v6_kept if fast_ok(n)]
        print('优先订阅（--fast-types %s%s）：%d 个节点' % (
            a.fast_types, '，含域名型' if a.fast_domains else '',
            len(fast_nodes)))

    # ---- 产出 -----------------------------------------------------------------
    by_source = {}
    for n in final_nodes:
        nm = str(n.get('name', ''))
        src = nm.split(' |')[0] if ' |' in nm else '其它'
        by_source[src] = by_source.get(src, 0) + 1
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    nodes_header = ('# 由 tools/test_nodes.py 自动生成：多订阅节点池经延迟 + 下载测速后的存活节点\n'
                    '# 生成时间: {ts}\n'
                    '# 节点数: {kept}；筛选条件: 延迟 ≤{maxlat}ms（超时 {lto}ms）、下载 ≥{minsp} KB/s'
                    '（{bytes}KB 块，限时 {sto}s）\n'
                    '# 各源: {by}\n').format(ts=ts, kept=len(final_nodes), maxlat=a.max_latency,
                                             lto=a.latency_timeout, minsp=a.min_speed_kbps,
                                             bytes=a.speed_bytes // 1024, sto=int(a.speed_timeout),
                                             by=', '.join('%s=%d' % kv for kv in sorted(by_source.items())))
    if a.nodes_out:
        with open(a.nodes_out, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(nodes_header)
            sp.dump_go_safe({'proxies': final_nodes}, fh)
        print('写出 %s' % a.nodes_out)
    if a.out:
        best_header = ('# 由 tools/test_nodes.py 自动生成，请勿手工编辑（改动会被下次生成覆盖）。\n'
                       '# 源模板: {cfg}（proxy-providers 段已换成测速后的 inline proxies，其余原样保留）\n'
                       '# 生成时间: {ts}\n'
                       '# 节点数: {kept}（节点池 {pool} → 延迟合格 {lat} → 测速合格 {spd}）\n'
                       '# 筛选条件: 延迟 ≤{maxlat}ms（超时 {lto}ms）、下载 ≥{minsp} KB/s'
                       '（{bytes}KB 块，限时 {sto}s）、最多 {maxn} 个\n'
                       '# ⚠ 测速环境: {loc}。它只说明节点"活着且能跑流量"；\n'
                       '#   从你自己的网络连它是否同样快，取决于你自己的链路（客户端的健康检查会再筛一遍）。\n').format(
            cfg=a.source_label or os.path.basename(a.config), ts=ts, kept=len(final_nodes),
            pool=len(nodes), lat=len(ok_latency), spd=len(speed), maxlat=a.max_latency,
            lto=a.latency_timeout, minsp=a.min_speed_kbps, bytes=a.speed_bytes // 1024,
            sto=int(a.speed_timeout), maxn=a.max_nodes, loc=a.test_location)
        text = build_output_config(a.config, final_nodes, best_header)
        with open(a.out, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
        print('写出 %s' % a.out)

    # ---- 优先订阅的产物 -------------------------------------------------------
    fast_base = 'https://github.com/haolive/changfeng/releases/download/fast'
    if a.fast_nodes_out or a.fast_out:
        fb = {}
        for n in fast_nodes:
            nm = str(n.get('name', ''))
            src = nm.split(' |')[0] if ' |' in nm else '其它'
            fb[src] = fb.get(src, 0) + 1
        keep_ratio = (100.0 * len(fast_nodes) / len(final_nodes)) if final_nodes else 0
        if a.fast_nodes_out:
            with open(a.fast_nodes_out, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write('# 由 tools/test_nodes.py 自动生成：优先订阅（只留经验上国内可达率高的类型）\n'
                         '# 生成时间: {ts}\n'
                         '# 节点数: {n}；保留类型: {t}{d}\n'
                         '# 各源: {by}\n'.format(
                             ts=ts, n=len(fast_nodes), t=a.fast_types,
                             d='（含域名型）' if a.fast_domains else '',
                             by=', '.join('%s=%d' % kv for kv in sorted(fb.items()))))
                sp.dump_go_safe({'proxies': fast_nodes}, fh)
            print('写出 %s' % a.fast_nodes_out)
        if a.fast_out:
            fast_header = (
                '# 由 tools/test_nodes.py 自动生成，请勿手工编辑（改动会被下次生成覆盖）。\n'
                '# 这是「优先订阅」：在 best 的基础上，只保留**经验上从国内连得通**的节点类型\n'
                '#   （--fast-types {t}{d}），类型筛选依据与实测数据见仓库 README-节点测速过滤.md。\n'
                '# 源模板: {cfg}；生成时间: {ts}\n'
                '# 节点数: {n}（占 best 的 {ratio:.0f}%）\n'
                '# ⚠ 这是**经验筛选**，不是"从国内实测过"：GitHub runner 在境外，量不到「你 → 节点」那一跳。\n'
                '#   想 100% 确认，只能在你自己的机器上再筛一遍。\n').format(
                t=a.fast_types, d='，含域名型' if a.fast_domains else '',
                cfg=a.source_label or os.path.basename(a.config), ts=ts,
                n=len(fast_nodes), ratio=keep_ratio)
            with open(a.fast_out, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(build_output_config(a.config, fast_nodes, fast_header))
            print('写出 %s' % a.fast_out)
    if a.fast_notes:
        with open(a.fast_notes, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('# fast（优先订阅）\n\n')
            fh.write('- 生成时间：%s\n' % ts)
            fh.write('- 节点数：**%d**（占 best 的 %.0f%%：best %d 个 → 这里 %d 个）\n'
                     % (len(fast_nodes), keep_ratio, len(final_nodes), len(fast_nodes)))
            fh.write('- 保留的节点类型：`%s`%s\n' % (a.fast_types, '（含域名型）' if a.fast_domains else ''))
            fh.write('- 各源：%s\n' % ', '.join('%s=%d' % kv for kv in sorted(fb.items())))
            fh.write('\n**为什么只留这些类型**：2026-09-24 从大陆本机对 `best` 的 602 个节点逐个做\n'
                     'TCP 探测，按类型统计"国内能否连上"：\n\n'
                     '| 类型 | 样本 | 国内可达率 |\n|---|---|---|\n'
                     '| `http` | 158 | **62.0%** |\n'
                     '| `anytls` | 3 | 66.7% |\n'
                     '| `vless` | 111 | 9.9% |\n'
                     '| `vmess` | 67 | 1.5% |\n'
                     '| `ss` | 182 | 1.6% |\n\n'
                     '整体只有 19.9% 可达。所以按类型收窄后，**列表缩到 1/4，却保住了 83%% 的可用节点** ——\n'
                     '客户端里"一片超时"的观感会明显改善。\n')
            fh.write('\nClash Verge 里直接当订阅用：\n\n```\n%s/best.yaml\n```\n' % fast_base)
            fh.write('\n只想要节点清单（自己组装配置时当 proxy-provider 用）：\n\n```\n%s/nodes.yaml\n```\n'
                     % fast_base)
            fh.write('\n> ⚠ 这是**经验筛选**，不是"从国内实测过"：GitHub runner 在境外机房，\n'
                     '> 量不到「你 → 节点」那一跳（GFW 那一段只有你自己的网络能看见）。\n'
                     '> 想要 100% 确认，只能在你自己的机器上再筛一遍（见 README 里的 `tools/local_cn_filter.py`）。\n'
                     '> 本资产每小时自动更新，与 `best` 同步。\n')
        print('写出 %s' % a.fast_notes)

    # ---- 统计 ----------------------------------------------------------------
    pool_stats = {}
    if a.pool_stats and os.path.exists(a.pool_stats):
        try:
            pool_stats = json.load(open(a.pool_stats, 'r', encoding='utf-8'))
        except Exception:  # noqa: BLE001
            pool_stats = {}
    sf_kinds = speed_fail_kinds(speed_fail)
    drop_reasons = dict(pool_stats.get('dropped_reasons', {}))
    if failed_reason:
        drop_reasons['延迟测试失败'] = len(failed_reason)
    if too_slow:
        drop_reasons['延迟过高'] = len(too_slow)
    if sf_kinds:
        n_slow = sf_kinds.get('速度不足', 0)
        if n_slow:
            drop_reasons['速度不足'] = n_slow
        if sum(sf_kinds.values()) - n_slow:
            drop_reasons['下载失败'] = sum(sf_kinds.values()) - n_slow
    if v6_dropped:
        drop_reasons['IPv6 无出口'] = len(v6_dropped)
    if prune_drops:
        drop_reasons['字段非法（内核预检剔除）'] = len(prune_drops)
    stats = {
        'stage': 'filter',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'elapsed_seconds': round(time.time() - t_start, 1),
        'pool': len(nodes),
        'sources': pool_stats.get('sources', []),
        'source_count': pool_stats.get('source_count'),
        'failed_sources': pool_stats.get('failed_sources', []),
        'total': len(nodes),
        'kept': len(final_nodes),
        'by_source': by_source,
        'latency': {'tested': len(alias_nodes), 'url': a.latency_url, 'timeout_ms': a.latency_timeout,
                    'max_ms': a.max_latency, 'passed': len(ok_latency),
                    'failure_kinds': dtype},
        'speed': {'tested': len(speed_pool), 'url': a.speed_url, 'urls': urls, 'bytes': a.speed_bytes,
                  'min_kbps': a.min_speed_kbps, 'timeout_s': a.speed_timeout, 'limit': a.speed_limit,
                  'passed': len(speed), 'failure_kinds': sf_kinds},
        'ipv6': {'literals': len(v6_nodes), 'test_host_has_ipv6': test_host_has_v6,
                 'policy': a.ipv6_policy, 'kept_untested': len(v6_kept), 'dropped': len(v6_dropped)},
        'max_nodes': a.max_nodes,
        'fast': {'nodes': len(fast_nodes), 'types': a.fast_types, 'with_domains': a.fast_domains,
                 'keep_ratio': round((100.0 * len(fast_nodes) / len(final_nodes)) if final_nodes else 0, 1),
                 'by_source': fb if (a.fast_out or a.fast_nodes_out) else {}},
        'dedup_or_cap_dropped': len(scored) + len(untested_ok) - len(final_aliases),
        'dropped_count': len(nodes) - len(final_nodes),
        'dropped_reasons': drop_reasons,
        'dropped_examples': {'延迟测试失败': sorted(
            '%s %s' % (alias_map[al]['name'][:40], why) for al, why in failed_reason.items())[:10],
            '字段非法': ['%s —— %s' % (n[:40], why) for n, why in prune_drops[:10]]},
        'latency_best': [(alias_map[al]['name'][:48], ok_latency[al], speed.get(al))
                         for al in final_aliases[:10]],
        # keepalive 用：源挂了/恢复了、或上游坏节点集合变了才会变
        'bad_signature': pool_stats.get('bad_signature', ''),
    }
    if a.stats:
        with open(a.stats, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
    if a.notes:
        # 用纯 GitHub 地址：说明是公开可见的，镜像前缀让用户自己在客户端加（换镜像不用改仓库）
        base = 'https://github.com/haolive/changfeng/releases/download/best'
        with open(a.notes, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('# best（测速过滤后的订阅）\n\n')
            fh.write('- 生成时间：%s（用时 %.0fs）\n' % (ts, time.time() - t_start))
            fh.write('- 节点池 %d 个（%s 个源）→ 延迟合格 %d → 测速合格 %d → **最终 %d 个**\n' % (
                len(nodes), pool_stats.get('source_count', '?'), len(ok_latency), len(speed), len(final_nodes)))
            fh.write('- 筛选标准：延迟 ≤%dms（超时 %dms）、下载 ≥%d KB/s（%dKB 测试块，限时 %ds）\n' % (
                a.max_latency, a.latency_timeout, a.min_speed_kbps, a.speed_bytes // 1024, int(a.speed_timeout)))
            fh.write('- 各源存活：%s\n' % ', '.join('%s=%d' % kv for kv in sorted(by_source.items())))
            if pool_stats.get('failed_sources'):
                fh.write('- ⚠ 本次拉取失败的源：%s\n' % ', '.join(pool_stats['failed_sources']))
            fh.write('\nClash Verge 里直接当订阅用：\n\n```\n%s/best.yaml\n```\n' % base)
            fh.write('\n只想要节点清单（自己组装配置时当 proxy-provider 用）：\n\n```\n%s/nodes.yaml\n```\n' % base)
    print('\n用时 %.0fs' % (time.time() - t_start))
    if len(final_nodes) < a.min_keep:
        print('存活节点 %d 少于下限 %d，判定失败（release 里的上一版不会被覆盖）' % (len(final_nodes), a.min_keep))
        return 1
    return 0


def _port_open(port, timeout=0.5, retries=6):
    """listener 是否已经就绪（内核刚起来时可能还在 bind）。"""
    for _ in range(retries):
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout)
            s.close()
            return True
        except OSError:
            time.sleep(0.4)
    return False


if __name__ == '__main__':
    sys.exit(main())