#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长风分享频道 → 固定订阅链接（镜像转发）。

背景
----
频道 https://t.me/s/changfengchannel 每条消息都带三个订阅链接：

    Clash/Mihomo（推荐）：https://nodebuf.com/files/public/<id>/download
    SingBox：              https://nodebuf.com/files/public/<id>/download
    Base64 通用订阅：      https://nodebuf.com/files/public/<id>/download

上游的 <id> **每条消息都换**（早 8 点 / 晚 8 点各一条），所以链接没法收藏。
本脚本把它变成一个固定地址：

    抓频道 → 解析最新一条消息 → 下载其 Clash/Mihomo 内容 → 原样存成 release 资产

客户端只认这一个地址，链接变了由本任务在背后替换：

    https://github.com/haolive/changfeng/releases/download/changfeng/clash.yaml

设计要点
--------
* **内容零改动**：上游给什么字节就存什么字节，不做转换、不加壳（客户端要的就是原样）。
* **回退**：最新一条的链接取不到 / 返回 HTML / 结构不合法，就沿 ``?before=`` 往前翻
  到上一条可用消息；实在没有就报错退出，**绝不把坏内容覆盖到固定地址上**。
* **幂等**：内容哈希和上次一致时输出 ``CHANGED=no``，workflow 直接跳过上传，
  避免每小时无意义地重建 release 资产。
* **零第三方依赖**：只用标准库；装了 PyYAML 才额外做一次 YAML 结构校验（可选）。

用法
----
    python sync_changfeng.py --out dist
    python sync_changfeng.py --out dist --proxy http://127.0.0.1:7890
    python sync_changfeng.py --out dist --prev dist/prev/latest.json
    python sync_changfeng.py --selftest
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import html as html_mod
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # 可选依赖：装了才做结构级校验，没装不影响使用
    import yaml  # type: ignore
except Exception:  # noqa: BLE001
    yaml = None

__all__ = [
    "Fetcher",
    "parse_channel_html",
    "pick_latest",
    "next_before_hint",
    "looks_like_clash",
    "looks_like_singbox",
    "looks_like_base64",
    "build_fixed_urls",
    "collect",
    "main",
]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_CHANNEL_URL = "https://t.me/s/changfengchannel"

#: 内部格式名 → release 资产名。资产名就是固定地址的一部分，改名等于换地址，慎改。
ASSETS = {
    "clash": "clash.yaml",
    "singbox": "singbox.json",
    "base64": "base64.txt",
    "links": "links.txt",
    "latest": "latest.json",
}

#: 从上游抓的三种格式。links.txt / latest.json 是本脚本自己造的元信息，不上游抓。
UPSTREAM_KINDS = ("clash", "singbox", "base64")
#: 主格式：整条流水线以它为准（它取不到就整体报错，不发布半成品）。
PRIMARY_KIND = "clash"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 三种链接的中文标签 → 内部格式名。同一格式给多个写法，
#: 频道哪天改个标点也不至于解析失败。
LABEL_PATTERNS: dict[str, tuple[str, ...]] = {
    "clash": (r"clash\s*/\s*mihomo", r"mihomo\s*/\s*clash", r"clash", r"mihomo"),
    "singbox": (r"sing\s*-?\s*box",),
    "base64": (r"base\s*-?\s*64", r"通用订阅", r"v2ray\s*ray", r"v2ray"),
}

CST = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Fetcher:
    """带重试 / 代理 / 超时的极简 HTTP 客户端（只用标准库）。"""

    def __init__(
        self,
        proxy: str | None = None,
        timeout: int = 30,
        retries: int = 3,
        backoff: float = 2.0,
        insecure: bool = False,
    ) -> None:
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
        ]
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)
        self._opener.addheaders = [
            ("User-Agent", USER_AGENT),
            ("Accept", "*/*"),
            ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
        ]
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = backoff

    def get(self, url: str) -> bytes:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with self._opener.open(url, timeout=self.timeout) as resp:
                    return resp.read()
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last = exc
                if attempt < self.retries:
                    wait = self.backoff * attempt
                    print(f"  ! 第 {attempt} 次取 {url} 失败：{exc}，{wait:.0f}s 后重试")
                    time.sleep(wait)
        raise RuntimeError(f"取 {url} 失败（重试 {self.retries} 次）：{last}")

    def get_text(self, url: str, encoding: str = "utf-8") -> str:
        return self.get(url).decode(encoding, "replace")


# --------------------------------------------------------------------------
# 频道解析
# --------------------------------------------------------------------------

_MSG_SPLIT = re.compile(
    r'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
)
_POST_ID = re.compile(r'data-post="[^"/]+/(\d+)"')
_BEFORE_HINT = re.compile(r'js-messages_more_wrap.*?data-before="(\d+)"', re.S)
_URL_IN_TEXT = re.compile(r'https?://[^\s"\'<>()\[\]{}，。；：、]+')
_UPDATE_DATE = re.compile(r"更新日期[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2})")


def _block_to_text(block: str) -> str:
    """把一条消息的 HTML 压成纯文本，但保留「标签在左、链接在右」的相对顺序。

    标签之间隔着 ``<br/>`` 和 ``<a ...>``；全删掉会让 "Clash/Mihomo（推荐）："
    和后面的 https://… 粘成一坨，统一换成空格最省事也最稳。
    """
    text = re.sub(r"<br\s*/?>", "\n", block, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _iter_urls(text: str, pos: int = 0):
    """按出现顺序产出 URL（去掉尾随标点，那属于消息文案不属于地址）。"""
    for m in _URL_IN_TEXT.finditer(text, pos):
        yield m.group(0).rstrip(".,;:!?'\"")


def _urls_in_order(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in _iter_urls(text):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _pick_by_label(text: str, kind: str) -> str | None:
    """在文本里找「标签 → 紧随其后的第一个 URL」。"""
    for pattern in LABEL_PATTERNS[kind]:
        m = re.search(pattern, text, flags=re.I)
        if not m:
            continue
        for url in _iter_urls(text, m.end()):
            # 消息底部还有一组 inline 按钮，它们的 href 与正文一致，重复取也无妨
            if "://" in url:
                return url
    return None


def parse_channel_html(page_html: str) -> list[dict]:
    """解析频道页面，返回按消息 id 升序排列的消息列表。

    每项形如::

        {post_id, text, urls: {clash|singbox|base64: url}, all_urls: [...], update_date}
    """
    messages: list[dict] = []
    for block in _MSG_SPLIT.split(page_html)[1:]:  # [0] 是页头
        pid_m = _POST_ID.search(block)
        if not pid_m:
            continue
        text = _block_to_text(block)
        urls_in_order = _urls_in_order(text)

        picked: dict[str, str | None] = {k: _pick_by_label(text, k) for k in UPSTREAM_KINDS}
        # 标签一个都没命中时退回「按顺序取第 1/2/3 个」。频道当前格式固定是
        # Clash → SingBox → Base64，够用；真拿不准就不猜，宁可少一种格式。
        if all(v is None for v in picked.values()) and len(urls_in_order) >= 3:
            picked = dict(zip(UPSTREAM_KINDS, urls_in_order[:3]))

        dm = _UPDATE_DATE.search(text)
        messages.append(
            {
                "post_id": int(pid_m.group(1)),
                "text": text,
                "urls": picked,
                "all_urls": urls_in_order,
                "update_date": dm.group(1) if dm else None,
            }
        )
    messages.sort(key=lambda m: m["post_id"])
    return messages


def next_before_hint(page_html: str) -> int | None:
    """页面顶部的「更早消息」游标；用于翻到上一页。"""
    m = _BEFORE_HINT.search(page_html)
    return int(m.group(1)) if m else None


def pick_latest(messages: list[dict], kind: str = PRIMARY_KIND) -> dict | None:
    """取最新一条带有所需格式链接的消息（``messages`` 需按 id 升序）。"""
    for msg in reversed(messages):
        if msg["urls"].get(kind):
            return msg
    return None


# --------------------------------------------------------------------------
# 内容校验
# --------------------------------------------------------------------------

_BAD_PREFIX = (b"<!doctype", b"<html", b"<?xml", b"<script")


def _bad_html(data: bytes) -> bool:
    head = data[:400].lstrip().lower()
    return head.startswith(_BAD_PREFIX) or b"<title>" in head


def looks_like_clash(data: bytes) -> tuple[bool, str]:
    """Clash/Mihomo 配置特征：有非空 proxies 段、且不是 HTML。"""
    if len(data) < 300:
        return False, f"太短（{len(data)} 字节）"
    if _bad_html(data):
        return False, "返回的是 HTML 页面而不是配置"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False, "不是合法 UTF-8"
    if text.lstrip().startswith("{"):
        try:  # 偶尔上游给的是 JSON 形态的 mihomo 配置
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            return False, f"JSON 解析失败：{exc}"
        if not obj.get("proxies"):
            return False, "JSON 里没有 proxies"
        return True, f"JSON 配置，{len(obj['proxies'])} 个节点"
    if yaml is not None:
        try:
            obj = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001
            return False, f"YAML 解析失败：{exc}"
        if not isinstance(obj, dict):
            return False, "YAML 顶层不是映射"
        proxies = obj.get("proxies")
        if not isinstance(proxies, list) or not proxies:
            return False, "YAML 里没有非空 proxies"
        return True, f"YAML，{len(proxies)} 个节点"
    if "proxies:" in text:
        return True, "含 proxies:（未装 PyYAML，仅文本特征校验）"
    return False, "既不是 JSON 也没有 proxies 段"


def looks_like_singbox(data: bytes) -> tuple[bool, str]:
    if len(data) < 200:
        return False, f"太短（{len(data)} 字节）"
    if _bad_html(data):
        return False, "返回的是 HTML 页面而不是配置"
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"JSON 解析失败：{exc}"
    if not isinstance(obj, dict):
        return False, "顶层不是对象"
    if not obj.get("outbounds"):
        return False, "没有 outbounds 段"
    return True, f"JSON，{len(obj['outbounds'])} 个 outbound"


def looks_like_base64(data: bytes) -> tuple[bool, str]:
    if len(data) < 100:
        return False, f"太短（{len(data)} 字节）"
    if _bad_html(data):
        return False, "返回的是 HTML 页面而不是订阅内容"
    compact = re.sub(rb"\s+", b"", bytes(data).strip())
    if compact.startswith(b"data:"):
        compact = compact.split(b",", 1)[-1]
    compact = compact.rstrip(b"=")
    try:
        decoded = base64.b64decode(compact + b"=" * (-len(compact) % 4), validate=False)
    except (binascii.Error, ValueError) as exc:
        return False, f"base64 解码失败：{exc}"
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return False, "base64 解出来不是 UTF-8"
    if "://" not in text:
        return False, "base64 解出来没有协议链接"
    return True, f"base64，{text.count('://')} 条协议链接"


VALIDATORS = {
    "clash": looks_like_clash,
    "singbox": looks_like_singbox,
    "base64": looks_like_base64,
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(CST).replace(microsecond=0).isoformat()


def build_fixed_urls(repo: str, tag: str) -> dict[str, str]:
    base = f"https://github.com/{repo}/releases/download/{tag}"
    return {name: f"{base}/{asset}" for name, asset in ASSETS.items()}


def keepalive_due(state_path: Path, max_age_days: int, verbose: bool = True) -> bool:
    """该不该提交一次（保活）。

    公开仓库连续 60 天没有提交活动，GitHub 会自动停用所有 schedule。
    正常情况下每次内容变化都会产生一次提交；但万一上游长期不更新（比如频道停了），
    就得靠一个低频的兜底提交把 schedule 保住。这里只在距上次提交超过
    ``max_age_days`` 时才说要提交，避免每小时刷一次提交历史。
    """
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(state["committed_at"])
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        if verbose:
            print("[保活] 没有可用的历史记录，本次提交")
        return True
    age_days = (datetime.now(CST) - last).days
    if verbose:
        print(f"[保活] 上次提交 {state.get('committed_at')}，距今 {age_days} 天"
              f"（阈值 {max_age_days} 天）")
    return age_days >= max_age_days


def write_state(state_path: Path, latest: dict) -> None:
    """写入保活用的状态文件（只在 workflow 决定要提交时才调用）。"""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "committed_at": now_iso(),
        "post_id": latest["source"]["post_id"],
        "update_date": latest["source"]["update_date"],
        "clash_sha256": latest["content_sha256"][PRIMARY_KIND],
        "fixed_url": latest["fixed_urls"]["clash"],
    }
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _gather_candidates(
    fetcher: Fetcher, channel_url: str, max_pages: int, verbose: bool
) -> list[dict]:
    """翻页收集消息，按消息 id **降序**（新的在前）返回。"""
    by_id: dict[int, dict] = {}
    before: int | None = None
    seen_before: set[int] = set()

    for page in range(1, max_pages + 1):
        url = channel_url + (f"?before={before}" if before else "")
        if verbose:
            print(f"[抓取] 第 {page} 页：{url}")
        page_html = fetcher.get_text(url)
        msgs = parse_channel_html(page_html)
        if verbose:
            span = f"{msgs[0]['post_id']}~{msgs[-1]['post_id']}" if msgs else "空"
            print(f"  解析到 {len(msgs)} 条消息，id {span}")
        for m in msgs:
            by_id[m["post_id"]] = m
        nxt = next_before_hint(page_html)
        if not nxt or nxt in seen_before:
            break
        seen_before.add(nxt)
        before = nxt

    return [by_id[k] for k in sorted(by_id, reverse=True)]


def collect(
    fetcher: Fetcher,
    channel_url: str,
    max_pages: int = 3,
    kinds: tuple[str, ...] = UPSTREAM_KINDS,
    verbose: bool = True,
) -> tuple[dict, dict[str, bytes], dict]:
    """抓频道 → 选最新可用消息 → 下载各格式内容。

    返回 ``(message, {kind: bytes}, meta)``。主格式取不到就抛异常，
    绝不用旧内容或不完整内容去顶替固定地址。
    """
    candidates = _gather_candidates(fetcher, channel_url, max_pages, verbose)
    if not candidates:
        raise RuntimeError(f"翻 {max_pages} 页都没解析出任何消息：{channel_url}")

    def try_kind(url: str, kind: str) -> tuple[bytes, str] | None:
        try:
            data = fetcher.get(url)
        except RuntimeError as exc:
            if verbose:
                print(f"  ! {kind} 下载失败：{exc}")
            return None
        ok, why = VALIDATORS[kind](data)
        if not ok:
            if verbose:
                print(f"  ! {kind} 内容校验未通过（{why}）：{url}")
            return None
        return data, why

    # ---- 主格式：从最新往回试，第一个能用的就是它 ----
    chosen: tuple[dict, bytes, str, str] | None = None
    for msg in candidates:
        url = msg["urls"].get(PRIMARY_KIND)
        if not url:
            continue
        got = try_kind(url, PRIMARY_KIND)
        if got:
            chosen = (msg, got[0], got[1], url)
            if verbose:
                print(f"[选中] 消息 https://t.me/{msg['post_id']}"
                      f"（上游更新日期 {msg.get('update_date') or '未知'}）")
                print(f"        {PRIMARY_KIND}: {got[1]}，{len(got[0])} 字节  <- {url}")
            break
    if chosen is None:
        raise RuntimeError(
            f"最近 {len(candidates)} 条消息的 Clash/Mihomo 链接都取不到或内容不合法，"
            "本次不发布（保持固定地址上的旧内容不动）"
        )

    msg, data, why, used_url = chosen
    payloads: dict[str, bytes] = {PRIMARY_KIND: data}
    notes: dict[str, str] = {PRIMARY_KIND: why}
    used_urls: dict[str, str | None] = {k: None for k in UPSTREAM_KINDS}
    used_urls[PRIMARY_KIND] = used_url

    # ---- 其余格式：先在同一条消息里找，再回退到更早的消息 ----
    for kind in kinds:
        if kind in payloads:
            continue
        for cand in candidates:
            url = cand["urls"].get(kind)
            if not url:
                continue
            got = try_kind(url, kind)
            if got:
                payloads[kind], notes[kind], used_urls[kind] = got[0], got[1], url
                if verbose:
                    where = "同一条消息" if cand is msg else f"回退到消息 {cand['post_id']}"
                    print(f"        {kind}: {got[1]}，{len(got[0])} 字节（{where}） <- {url}")
                break
        else:
            if verbose:
                print(f"[跳过] {kind}：候选消息里都没找到可用链接")

    meta = {
        "channel": channel_url,
        "post_id": msg["post_id"],
        "message_url": f"https://t.me/{msg['post_id']}",
        "update_date": msg.get("update_date"),
        "upstream": used_urls,
        "notes": notes,
    }
    return msg, payloads, meta


def render_links_txt(fixed: dict[str, str], meta: dict) -> str:
    lines = [
        "# 长风分享频道 —— 固定订阅地址",
        "",
        "频道里的原始链接每条消息都会变；这里是转发后的固定地址。",
        "由 GitHub Actions 每小时同步一次，上游更新后客户端无需改地址。",
        "",
        "【Clash / Mihomo】（推荐）",
        fixed["clash"],
        "",
        "【SingBox】",
        fixed["singbox"],
        "",
        "【Base64 通用订阅】（v2rayN / Hiddify / Shadowrocket 等）",
        fixed["base64"],
        "",
        "——",
        f"频道       : {meta['channel']}",
        f"当前源消息 : {meta['message_url']}",
        f"上游更新   : {meta.get('update_date') or '未知'}",
        f"本次同步   : {now_iso()}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把长风分享频道的滚动订阅链接转成固定链接（内容原样镜像）"
    )
    ap.add_argument("--channel-url", default=DEFAULT_CHANNEL_URL, help="频道预览页地址")
    ap.add_argument("--repo", default="haolive/changfeng", help="仓库 slug（写进 links.txt）")
    ap.add_argument("--tag", default="changfeng", help="放资产的 release tag")
    ap.add_argument("--out", default="dist", help="产物输出目录")
    ap.add_argument("--prev", default=None,
                    help="上一次的 latest.json；给了就比对哈希，内容没变时输出 CHANGED=no")
    ap.add_argument("--state-file", default=None,
                    help="保活状态文件路径；给了就顺带算 KEEPALIVE=yes/no")
    ap.add_argument("--keepalive-max-age-days", type=int, default=7,
                    help="距上次提交超过多少天算需要保活提交（默认 7）")
    ap.add_argument("--max-pages", type=int, default=3, help="最多往前翻几页找可用消息")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--proxy", default=os.environ.get("CF_PROXY") or None,
                    help="HTTP 代理，如 http://127.0.0.1:7890（也可用 CF_PROXY 环境变量）")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 校验（排障用）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="跑内置自测后退出")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    verbose = not args.quiet
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    fetcher = Fetcher(
        proxy=args.proxy,
        timeout=args.timeout,
        retries=args.retries,
        insecure=args.insecure,
    )

    _msg, payloads, meta = collect(
        fetcher, args.channel_url, max_pages=args.max_pages, verbose=verbose
    )
    fixed = build_fixed_urls(args.repo, args.tag)

    # ---- 写产物（字节与上游完全一致，不做任何转换）----
    written: list[str] = []
    for kind, data in payloads.items():
        (out_dir / ASSETS[kind]).write_bytes(data)
        written.append(ASSETS[kind])
        if verbose:
            print(f"[写出] {out_dir / ASSETS[kind]}（{len(data)} 字节）")

    (out_dir / ASSETS["links"]).write_text(
        render_links_txt(fixed, meta), encoding="utf-8"
    )
    written.append(ASSETS["links"])

    prev_hash = None
    prev_post = None
    if args.prev and Path(args.prev).exists():
        try:
            prev = json.loads(Path(args.prev).read_text(encoding="utf-8"))
            prev_hash = (prev.get("content_sha256") or {}).get(PRIMARY_KIND)
            prev_post = prev.get("source", {}).get("post_id", prev.get("post_id"))
        except (json.JSONDecodeError, OSError, AttributeError) as exc:
            if verbose:
                print(f"[提示] 旧 latest.json 读不了（{exc}），按「有变化」处理")

    changed = sha256(payloads[PRIMARY_KIND]) != prev_hash

    latest = {
        "fixed_urls": fixed,
        "source": {
            "channel": meta["channel"],
            "post_id": meta["post_id"],
            "message_url": meta["message_url"],
            "update_date": meta["update_date"],
        },
        "upstream": meta["upstream"],
        "content_sha256": {k: sha256(v) for k, v in payloads.items()},
        "content_notes": meta["notes"],
        "fetched_at": now_iso(),
        "previous_post_id": prev_post,
    }
    (out_dir / ASSETS["latest"]).write_text(
        json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    written.append(ASSETS["latest"])

    (out_dir / ".changed").write_text("yes" if changed else "no", encoding="utf-8")

    # ---- 保活：公开仓库 60 天无提交会被自动停用 schedule，这里低频兜底 ----
    keepalive = False
    state_file: Path | None = None
    if args.state_file:
        state_file = Path(args.state_file)
        keepalive = keepalive_due(state_file, args.keepalive_max_age_days, verbose)
        if keepalive:
            # 只有确定要提交时才落盘，免得 workflow 最后放弃提交、文件却已改写
            write_state(state_file, latest)
        (out_dir / ".keepalive").write_text("yes" if keepalive else "no", encoding="utf-8")

    if verbose:
        print()
        print(f"Clash 内容指纹 : {sha256(payloads[PRIMARY_KIND])[:16]}…")
        print(f"上次源消息     : {prev_post if prev_post is not None else '（无记录）'}")
        print(f"本次源消息     : {meta['post_id']}")
        print(f"CHANGED={'yes' if changed else 'no'}")
        if state_file is not None:
            print(f"KEEPALIVE={'yes' if keepalive else 'no'}")
        print()
        print("固定订阅地址（客户端填这个，永不变）：")
        print(f"  {fixed['clash']}")
        print(f"  {fixed['singbox']}")
        print(f"  {fixed['base64']}")
    print("ASSETS=" + ",".join(written))
    print(f"CHANGED={'yes' if changed else 'no'}")
    if state_file is not None:
        print(f"KEEPALIVE={'yes' if keepalive else 'no'}")
    return 0


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------

_SAMPLE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message text_not_supported_wrap js-widget_message"
     data-post="changfengchannel/99999" data-view="x">
  <div class="tgme_widget_message_text js-message_text" dir="auto">
    <i class="emoji"><b>&#128257;</b></i> 更新日期：2026-10-04 08:00<br/>
    节点数量：6 条<br/>
    <b>&#128187;</b> 订阅链接：<br/><br/>Clash/Mihomo（推荐）：<br/>
    <a href="https://nodebuf.com/files/public/AAA111/download">https://nodebuf.com/files/public/AAA111/download</a><br/>
    SingBox：<br/>
    <a href="https://nodebuf.com/files/public/BBB222/download">https://nodebuf.com/files/public/BBB222/download</a><br/>
    Base64 通用订阅：<br/>
    <a href="https://nodebuf.com/files/public/CCC333/download">https://nodebuf.com/files/public/CCC333/download</a><br/>
  </div>
  <a href="https://nodebuf.com/files/public/AAA111/download">Clash/Mihomo</a>
</div></div>
"""


def _selftest() -> int:
    ok = True

    def check(label: str, got, want) -> None:
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  {label}: {got!r}" + ("" if good else f" != {want!r}"))

    print("[自测] 频道 HTML 解析")
    msgs = parse_channel_html(_SAMPLE)
    check("消息数", len(msgs), 1)
    m = msgs[0]
    check("post_id", m["post_id"], 99999)
    check("clash 链接", m["urls"]["clash"], "https://nodebuf.com/files/public/AAA111/download")
    check("singbox 链接", m["urls"]["singbox"], "https://nodebuf.com/files/public/BBB222/download")
    check("base64 链接", m["urls"]["base64"], "https://nodebuf.com/files/public/CCC333/download")
    check("更新日期", m["update_date"], "2026-10-04 08:00")
    check("pick_latest", (pick_latest(msgs) or {}).get("post_id"), 99999)

    print("[自测] 内容校验")
    check("HTML 被拒", looks_like_clash(b"<!DOCTYPE html><html><body>404</body></html>")[0], False)
    check("空内容被拒", looks_like_clash(b"")[0], False)
    check("太短的 clash 被拒", looks_like_clash(b"proxies: []\n")[0], False)
    # 校验有最小长度门槛（真实订阅都是几 KB），所以这里的样本也按真实体量造
    b64_sub = base64.b64encode(
        "\n".join(f"ss://YWVzLTI1Ni1nY206cGFzc0AxLjIuMy40OjQ0Mw#node{i}" for i in range(30)).encode()
    )
    check("base64 无协议被拒", looks_like_base64(base64.b64encode(b"hello world hello" * 20))[0], False)
    check("base64 正例", looks_like_base64(b64_sub)[0], True)
    check("singbox 无 outbounds 被拒", looks_like_singbox(b'{"inbounds":[]}' * 60)[0], False)
    singbox = json.dumps(
        {"log": {"level": "info"}, "outbounds": [{"type": "direct"}, {"type": "block"}]}
    ).encode()
    singbox += b" " * 300  # 补到超过最小长度门槛
    check("singbox 正例", looks_like_singbox(singbox)[0], True)
    if yaml is not None:
        good = ("proxies:\n" + "".join(
            f"  - {{name: n{i}, type: ss, server: a.example, port: 443}}\n" for i in range(6)
        )).encode("utf-8")
        check("clash 正例", looks_like_clash(good)[0], True)
        check("clash 无 proxies 被拒", looks_like_clash(b"port: 7890\nmode: rule\n" + b"#" * 400)[0], False)
    else:
        print("  SKIP  未安装 PyYAML，跳过 YAML 结构校验（装上 PyYAML 会更严）")

    print("[自测] 固定地址生成")
    check("固定地址", build_fixed_urls("haolive/changfeng", "changfeng")["clash"],
          "https://github.com/haolive/changfeng/releases/download/changfeng/clash.yaml")

    print("[自测] 保活判定")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "last-sync.json"
        check("无历史记录 → 需要提交", keepalive_due(sp, 7, verbose=False), True)
        write_state(sp, {
            "source": {"post_id": 1, "update_date": "2026-10-04 08:00"},
            "content_sha256": {PRIMARY_KIND: "deadbeef"},
            "fixed_urls": build_fixed_urls("o/r", "changfeng"),
        })
        check("刚提交过 → 不需要", keepalive_due(sp, 7, verbose=False), False)
        stale = json.loads(sp.read_text(encoding="utf-8"))
        stale["committed_at"] = "2026-01-01T00:00:00+08:00"
        sp.write_text(json.dumps(stale), encoding="utf-8")
        check("很久没提交 → 需要", keepalive_due(sp, 7, verbose=False), True)

    print()
    print("自测结果：" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())