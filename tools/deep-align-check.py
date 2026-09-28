#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deep-align-check —— 双语 ASS 的**地面真值**对齐审计（人工审计工具，不进交付路径）。

为什么需要它（2026-09-27 实测教训）：`omnisub.py` 的 `[sync]` 闸门是**零成本结构判据**（看句末
标点），能抓住"整体错开一条"这类错位，但它看不见"标点模式恰好一致"的内容错位。当时为了证明
一份成品确实错位，我做了 373 条逐条独立复译——**被 50 次/分钟的限速拖了 7 分钟**，而且第一版
抽样用了等距步长，把"连续错位段"打散成了散点，差点误判成"正常改写"。

本工具的判据与当时的定案方法一致，并修正了那个抽样缺陷：
  · **一条 cue 一次请求**：单条请求结构上不可能被模型合并/重新编号，参照不可污染；
  · **取连续窗口**（默认 4 段 × 10 条）而不是等距抽样：错位的特征是**连续段**，等距抽样会把它打散；
  · 每条比三种假设：成品译文 ≈ 同条新译（对齐）/ ≈ 上一条新译（中文整体提前）/ ≈ 下一条（滞后）。

用法（视频或成品 ass 都行；--limit 之类的小样不要用本工具，窗口要落在完整成品上）：

    python3 tools/deep-align-check.py "<视频或 .ass>" [--windows 4] [--per-window 10] \
        [--source-lang en] [--source-srt <基名>.source.srt]

退出码：0 = 采样窗口内全部对齐；1 = 检出疑似错位（打印窗口与三种相似度）。
成本：默认 40 次单条翻译请求 ≈ 40–60 s（限速 50 次/分钟）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# 自举：DSH 里裸 `python3` 是系统 3.9，而 omnisub（本工具 import 它复用请求路径）要 3.10+。
# 先在本机找一个 3.10+ 解释器把自己重跑一遍，否则直跑会撞在 omnisub 的版本检查上。
if sys.version_info < (3, 10):
    _can = [os.path.expanduser("~/.local/bin/python3"),
            *sorted(str(p) for p in
                    Path(os.path.expanduser("~/.local/share/uv/python")).glob("*/bin/python3")),
            "/opt/homebrew/bin/python3.13", "/opt/homebrew/bin/python3.12"]
    _me = os.path.abspath(__file__)
    for _py in _can:
        if os.access(_py, os.X_OK):
            try:
                os.execv(_py, [_py, _me, *sys.argv[1:]])
            except OSError:
                continue
    raise SystemExit("deep-align-check 需要 Python 3.10+（DSH 里裸 python3 是 3.9）")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import omnisub  # noqa: E402  （复用它的 bl 发现、限速器与云端单条请求路径，别在这里重写一遍）


def parse_ass(path: Path) -> list[tuple[float, str]]:
    """取每个事件的**第一行**（译文在上、原文沉底 → 第一行是译文）与起始秒。"""
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue: "):
            continue
        q = line[len("Dialogue: "):].split(",", 9)
        h, m, s = q[1].split(":")
        begin = int(h) * 3600 + int(m) * 60 + float(s)
        up = re.sub(r"^\{[^}]*\}", "", q[9].split(r"\N")[0]).strip()
        out.append((begin, up))
    return out


def parse_srt(path: Path) -> list[str]:
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip())
    return [" ".join(b.strip().splitlines()[2:]) for b in blocks if len(b.strip().splitlines()) >= 3]


def norm(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text or "")


def bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def dice(a: str, b: str) -> float:
    x, y = bigrams(norm(a)), bigrams(norm(b))
    return 2 * len(x & y) / (len(x) + len(y)) if x and y else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="双语 ASS 的地面真值对齐审计")
    ap.add_argument("target", type=Path, help="视频文件或成品 .ass")
    ap.add_argument("--source-srt", type=Path, default=None, help="原文（默认取缓存里的 <基名>.source.srt）")
    ap.add_argument("--source-lang", default="en")
    ap.add_argument("--target-lang", default="zh")
    ap.add_argument("--windows", type=int, default=4, help="连续窗口数（默认 4）")
    ap.add_argument("--per-window", type=int, default=10, help="每窗口条数（默认 10）")
    ap.add_argument("--cache-dir", type=Path, default=None, help="成品对应的缓存目录（找 source.srt 用）")
    args = ap.parse_args()

    target = args.target.expanduser().resolve()
    if target.suffix.lower() == ".ass":
        ass, video = target, None
    else:
        video, ass = target, target.with_suffix(".ass")
    if not ass.exists():
        raise SystemExit(f"找不到成品：{ass}")

    cues = parse_ass(ass)
    if args.source_srt:
        src = parse_srt(args.source_srt.expanduser().resolve())
    else:
        if args.cache_dir:
            roots = [args.cache_dir.expanduser().resolve()]
        elif video:
            roots = [omnisub.default_cache_root() / f"{ass.stem}-{hashlib.sha1(str(video).encode()).hexdigest()[:8]}"]
        else:
            # 给的是 .ass：按 <基名>-<哈希> 前缀在默认缓存根里找（同一部片可能有多份，取含 source.srt 的）
            roots = sorted(p for p in omnisub.default_cache_root().glob(f"{ass.stem}-*") if p.is_dir())
        found = next((r / f"{ass.stem}.source.srt" for r in roots
                      if (r / f"{ass.stem}.source.srt").exists()), None)
        if not found:
            raise SystemExit("找不到 .source.srt：用 --source-srt 或 --cache-dir 指定")
        src = parse_srt(found)
        print(f"[src] 原文 {found}")
    if len(src) != len(cues):
        raise SystemExit(f"原文与成品的条数不一致（{len(src)} vs {len(cues)}）→ 拿错文件了")

    n = len(cues)
    step = max(1, n // args.windows)
    spans = []
    for w in range(args.windows):
        start = min(n - 1, w * step + max(0, step // 3))
        spans.append(list(range(start, min(n, start + args.per_window))))
    picked = [i for s in spans for i in s]
    print(f"[plan] {args.windows} 个连续窗口 × {args.per_window} 条 = {len(picked)} 次单条翻译"
          f"（≈{len(picked) * 1.2:.0f}s，限速 50 次/分钟）")

    key = omnisub.api_key(None)
    omnisub._LIMITER = omnisub._RateLimiter(omnisub.REQUESTS_PER_MIN)   # type: ignore[attr-defined]
    # 参照缓存：审计要反复跑（改前/改后对照），同一句文本只译一次
    cpath = (args.cache_dir.expanduser().resolve() if args.cache_dir else omnisub.default_cache_root()) \
        / f"deep-refs.{args.source_lang}-{args.target_lang}.json"
    memo: dict[str, str] = {}
    if cpath.exists():
        try:
            memo = json.loads(cpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            memo = {}
    refs: dict[int, str] = {}
    for k, i in enumerate(picked, 1):
        hit = memo.get(src[i])
        if hit is None:
            out = omnisub._ask_cloud([src[i]], key, omnisub.CHAT_MODEL_DEFAULT,
                                     omnisub.system_for(args.source_lang, args.target_lang),
                                     omnisub.TRANSLATE_TIMEOUT, args.source_lang, args.target_lang)
            hit = (out[0] or "").strip() if out else ""
            if hit:
                memo[src[i]] = hit
        if hit:
            refs[i] = hit
        if k % 10 == 0 or k == len(picked):
            print(f"   {k}/{len(picked)}", flush=True)
    try:
        cpath.write_text(json.dumps(memo, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    bad_total = 0
    for wi, span in enumerate(spans, 1):
        verdicts = []
        for i in span:
            keep = cues[i][1]
            cur = dice(keep, refs.get(i, ""))
            prev = dice(keep, refs.get(i - 1, "")) if i else 0.0
            nxt = dice(keep, refs.get(i + 1, "")) if i + 1 < n else 0.0
            if cur >= 0.30 or max(prev, nxt) <= 0.30:
                verdicts.append(("ok", cur, prev, nxt))
            else:
                # 成品#N ≈ 下一条的参照 → 中文把下一句提前显示了 → "提前(lead)"；反之"滞后(lag)"
                verdicts.append(("lead" if nxt > prev else "lag", cur, prev, nxt))
        bad = [(j, v) for j, v in enumerate(verdicts) if v[0] != "ok"]
        bad_total += len(bad)
        tag = "✅" if not bad else "❌"
        print(f"[窗口 {wi}] cue#{span[0]}–#{span[-1]}（{cues[span[0]][0]:.0f}s 起）{tag} "
              f"对齐 {len(span) - len(bad)}/{len(span)}")
        for j, (v, cur, prev, nxt) in bad:
            i = span[j]
            print(f"    ✗ cue#{i} @{cues[i][0]:.0f}s {v}（同条 {cur:.2f} / 上一条 {prev:.2f} / 下一条 {nxt:.2f}）")
            print(f"        成品 {cues[i][1][:46]}")
            print(f"        英文 {src[i][:56]}")
            print(f"        新译 {refs.get(i, '')[:46]}")
    print(f"\n[结论] {'✅ 采样窗口内全部对齐' if not bad_total else f'❌ 检出 {bad_total} 条疑似错位'}"
          f"（窗口内连续错位 = 整体错位的特征；散落的低分有两类正常情况：\n"
          f"   ① 不同措辞的正常改写；② **同句跨两条 cue** —— 一个翻译单元被切回两条 cue 时，\n"
          f"      若译文语序与源文不同（如中文把 won't mate 放到句尾），任何**连续**切分都对不上\n"
          f"      两条源文，属跨语言语序差异、不是实现缺陷。2026-09-28 实测：E04#218 属 ①、\n"
          f"      E01#40 属 ②（试过\"更宽窗口找句末标点\"的兜底并回退，对 ② 无效且会压短 cue）")
    return 0 if not bad_total else 1


if __name__ == "__main__":
    raise SystemExit(main())