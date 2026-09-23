#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保活 + 审计：只在"上游坏节点签名变了"或"超过 N 天没提交过"时才更新统计文件。

为什么需要它
------------
1) **保活**：GitHub 对公开仓库的定时任务有个坑 —— 连续 60 天没有任何提交活动，
   `schedule` 会被自动停用。而本 workflow 只更新 release 资产、不产生 commit，
   正好属于"会被停用"的状态，所以需要偶尔提交一次。
2) **审计**：上游哪天开始夹带非法节点、哪天又干净了，看这个文件的变化就知道，
   不用去翻 Actions 日志。

两道门槛都满足才更新，所以正常情况下一周最多提交一两次，不会刷满提交历史。

用法
----
  python tools/keepalive.py --stats dist/stats.json --store stats/s8-stats.json [--max-age-days 7] [--force]

第一行输出 `update` 或 `skip`，workflow 靠它决定要不要 git commit。
"""

import argparse
import json
import os
import sys
import time


def read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser(description='按需更新 s8 清洗统计（保活 + 审计）')
    ap.add_argument('--stats', required=True, help='sanitize_provider.py --stats 产出的 json')
    ap.add_argument('--store', required=True, help='要提交进仓库的统计文件路径')
    ap.add_argument('--max-age-days', type=float, default=7, help='超过这么多天没提交就强制更新一次（保活）')
    ap.add_argument('--force', action='store_true', help='无条件写入（测试用）')
    a = ap.parse_args()

    stats = read_json(a.stats)
    if not stats:
        print('skip')      # 没有 stats 就不折腾，让 workflow 保持原样
        print('# 读不到 %s，跳过' % a.stats)
        return 0

    old = read_json(a.store) or {}
    sig = str(stats.get('bad_signature', ''))
    sig_changed = bool(sig) and sig != str(old.get('bad_signature', ''))
    age_days = 999
    if old.get('updated_at'):
        try:
            age_days = (time.time() - time.mktime(time.strptime(old['updated_at'], '%Y-%m-%dT%H:%M:%SZ'))
                        ) / 86400.0
        except Exception:  # noqa: BLE001
            pass

    if not (a.force or sig_changed or age_days >= a.max_age_days):
        print('skip')
        print('# 坏节点签名没变（%s），距上次提交 %d 天，未到 %g 天保活线' % (sig, int(age_days), a.max_age_days))
        return 0

    reason = '强制写入' if a.force else ('坏节点签名变化 %s -> %s' % (old.get('bad_signature', '(无)'), sig)
                                     if sig_changed else '距上次提交 %d 天，保活一次' % int(age_days))
    note = ('签名变化 = 上游夹带的非法节点集合变了（新出现了坏节点，或旧的消失了）。'
            '重名不计入签名（那只是常规噪音）。')
    record = {
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'update_reason': reason,
        'bad_signature': sig,
        'dropped_reasons': stats.get('dropped_reasons', {}),
        'dropped_examples': stats.get('dropped', {}),
        'quoted_scalars': stats.get('quoted_scalars', [])[:10],
        'quoted_count': stats.get('quoted_count', 0),
        'upstream_total': stats.get('total'),
        'kept': stats.get('kept'),
        'note': note,
    }
    d = os.path.dirname(os.path.abspath(a.store))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(a.store, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
        fh.write('\n')
    print('update')
    print('# 更新原因：%s' % reason)
    print('# 上游 %s 个节点 → 保留 %s，剔除 %s，修复 %s' % (
        record['upstream_total'], record['kept'], stats.get('dropped_count'), record['quoted_count']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
