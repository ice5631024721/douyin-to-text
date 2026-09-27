#!/usr/bin/env python3
"""视频 → 双语字幕（SRT），时间轴与视频一一对应。

**复用优先（不重复造轮子）**：能拿到现成字幕就不转写——
  1. 内嵌字幕轨（MKV/MP4 的 srt/ass 文本轨，含 SDH）→ 直接复用原时间轴，零 ASR 成本
  2. 同目录外挂字幕（<视频基名>.srt / .ass / .vtt / 同名带语言后缀）→ 直接复用
  3. 都没有才转写（ASR）——用 PyAV 解音轨 + bl 异步 filetrans 拿句级/词级时间戳
再用 bl text chat 把原文批量译成目标语言，输出「原文行 + 译文行」的双语 SRT。

为什么是这套组合（全部本机已装，不依赖坏掉的系统 ffmpeg）：
  * 解音轨/解字幕：PyAV（uv 提供，wheel 自带 FFmpeg 库）——本机 ffmpeg/ffprobe 断链
    （x264 dylib 缺失、ffmpeg@7 无 bin），macOS 原生 afconvert 又读不了 MKV。
  * 转写：bl speech recognize --model qwen-audio-3.1-asr-flash-filetrans（异步）
  * 翻译：bl text chat（同一 CLI、同一把 key，OpenAI 兼容端点）

跑法：
  ~/.local/bin/uv run --with av --with numpy python video_to_srt.py <video> \
      [--out <dir>] [--source auto|embedded|sidecar|asr] [--target-lang zh] \
      [--sub-index N] [--sub-lang eng] [--asr-model ...] [--chat-model qwen3.8-flash] \
      [--no-translate] [--keep-audio] [--asr-json <已有.json>]

产出：
  <out>/<视频基名>.srt        双语；--no-translate 时只有原文
  <out>/<视频基名>.source.srt 复用的原字幕（内嵌提取或外挂复制），便于复查/重译
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ASR_MODEL_DEFAULT = "qwen-audio-3.1-asr-flash-filetrans"
# 实测（2026-09-27，765 条字幕）：flash 与 max 译文质量相当，flash 快约一倍
CHAT_MODEL_DEFAULT = "qwen3.8-flash"
TEXT_SUB_CODECS = {"srt", "subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
SIDECAR_EXTS = (".srt", ".ass", ".ssa", ".vtt")
MAX_LINE_CHARS = 42          # 仅 ASR 路线需要重切时用
MAX_CUE_MS = 7000
MIN_CUE_MS = 800
MIN_GAP_MS = 40
TRANSLATE_BATCH = 40


def find_bl() -> str:
    found = shutil.which("bl")
    if found:
        return found
    candidates = [Path.home() / ".local/bin/bl", Path("/opt/homebrew/bin/bl"), Path("/usr/local/bin/bl")]
    fnm_root = Path.home() / ".local/share/fnm/node-versions"
    if fnm_root.exists():
        candidates += sorted(fnm_root.glob("*/installation/bin/bl"))
    for cand in candidates:
        if Path(cand).exists():
            return str(cand)
    raise SystemExit("找不到 bl（bailian-cli）：npm install -g bailian-cli（见 ASR-API.md）")


def bl_env() -> dict:
    """bl 是 npm shim（#!/usr/bin/env node）：node 不在 PATH 上时它会以 exit=127 静默空返回。

    实测坑（2026-09-27）：DSH/GUI 会话的 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，
    直接调 bl 会 20 个批次全部拿到空 stdout，看起来像"模型没返回译文"。
    这里把 bl 所在目录（fnm/global 布局里 node 与它同级）前置进 PATH。
    """
    env = os.environ.copy()
    bl_dir = str(Path(find_bl()).parent)
    parts = [bl_dir]
    if not shutil.which("node", path=env.get("PATH", "")):
        parts.append(bl_dir)  # node 通常与 bl 同目录
        for extra in (Path.home() / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
            if Path(extra).exists():
                parts.append(str(extra))
    env["PATH"] = os.pathsep.join(parts + [env.get("PATH", "")])
    return env


def api_key(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("DASHSCOPE_API_KEY"):
        return os.environ["DASHSCOPE_API_KEY"]
    env_path = Path.home() / ".agentmemory/.env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("找不到 API key：传 --api-key 或设 DASHSCOPE_API_KEY（见 ASR-API.md）")


# ---------------- 时间工具 ----------------
def ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_ts(text: str) -> int:
    h, m, rest = re.split(r"[:]", text.strip())
    s, _, ms = rest.replace(".", ",").partition(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int((ms or "0").ljust(3, "0")[:3])


# ---------------- 1a. 内嵌字幕轨 ----------------
def embedded_tracks(video: Path) -> list[dict]:
    import av

    ctx = av.open(str(video))
    tracks = []
    for stream in ctx.streams:
        if stream.type != "subtitle":
            continue
        meta = dict(stream.metadata or {})
        tracks.append({
            "index": stream.index,
            "codec": stream.codec_context.name,
            "lang": (meta.get("language") or "").lower(),
            "title": meta.get("title") or "",
            "text_based": stream.codec_context.name.lower() in TEXT_SUB_CODECS,
        })
    return tracks


def extract_embedded(video: Path, index: int | None, prefer_lang: str | None) -> tuple[list[dict], dict]:
    import av

    ctx = av.open(str(video))
    streams = [s for s in ctx.streams if s.type == "subtitle"]
    if not streams:
        raise SystemExit("视频里没有字幕轨")
    stream = None
    if index is not None:
        stream = next((s for s in streams if s.index == index), None)
        if stream is None:
            raise SystemExit(f"没有 idx={index} 的字幕轨")
    else:
        text_streams = [s for s in streams if s.codec_context.name.lower() in TEXT_SUB_CODECS]
        pool = text_streams or streams
        if prefer_lang:
            stream = next((s for s in pool
                           if (dict(s.metadata or {}).get("language") or "").lower().startswith(prefer_lang)), None)
        stream = stream or pool[0]
    info = {"index": stream.index, "codec": stream.codec_context.name, **dict(stream.metadata or {})}
    tb = stream.time_base
    raw_cues: list[dict] = []
    for packet in ctx.demux(stream):
        if packet.size == 0:
            continue
        try:
            data = bytes(packet)
        except Exception:
            data = packet.to_bytes()
        text = data.decode("utf-8", "replace")
        text = re.sub(r"\{[^}]*\}", "", text)                      # ASS 样式块
        text = re.sub(r"<[^>]+>", "", text)                        # HTML 标签
        text = re.sub(r"\\([Nnh])", "\n", text).strip()             # ASS 硬换行
        if not text:
            continue
        begin = int(packet.pts * tb * 1000) if packet.pts is not None else None
        duration = int(packet.duration * tb * 1000) if packet.duration else None
        if begin is None:
            continue
        raw_cues.append({"begin": begin, "end": begin + duration if duration else None, "text": text})
    cues = normalize_cues(raw_cues)
    return cues, info


# ---------------- 1b. 外挂字幕 ----------------
def sidecar_path(video: Path) -> Path | None:
    for ext in SIDECAR_EXTS:
        exact = video.with_suffix(ext)
        if exact.exists():
            return exact
    stem = video.stem
    for cand in sorted(video.parent.glob(f"{stem}*")):
        if cand.suffix.lower() in SIDECAR_EXTS and cand != video:
            return cand
    return None


def read_srt(path: Path) -> list[dict]:
    cues = []
    text = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        idx = 0
        if re.fullmatch(r"\d+", lines[0].strip()):
            idx = 1
        if idx >= len(lines) or "-->" not in lines[idx]:
            continue
        left, _, right = lines[idx].partition("-->")
        body = "\n".join(lines[idx + 1:]).strip()
        if body:
            cues.append({"begin": parse_ts(left), "end": parse_ts(right.split()[0]), "text": body})
    return normalize_cues(cues)


def read_ass(path: Path) -> list[dict]:
    cues = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        begin, end, body = parts[1], parts[2], parts[9]
        body = re.sub(r"\{[^}]*\}", "", body)
        body = re.sub(r"\\[Nn]", "\n", body).strip()
        if body:
            cues.append({"begin": parse_ts(begin.replace(".", ",")), "end": parse_ts(end.replace(".", ",")), "text": body})
    return normalize_cues(cues)


# ---------------- 1c. ASR 路线 ----------------
def extract_audio(video: Path, audio: Path) -> float:
    import av

    ctx = av.open(str(video))
    stream = next((s for s in ctx.streams if s.type == "audio"), None)
    if stream is None:
        raise SystemExit(f"{video} 里没有音频流")
    duration = float(ctx.duration) / av.time_base if ctx.duration else 0.0
    print(f"[audio] {stream.codec_context.name} {stream.rate}Hz {stream.channels}ch dur={duration:.0f}s", flush=True)
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
    out = av.open(str(audio), "w")
    ostream = out.add_stream("flac", rate=16000)
    ostream.layout = "mono"
    total = 0
    for frame in ctx.decode(stream):
        for resampled in resampler.resample(frame):
            for packet in ostream.encode(resampled):
                out.mux(packet)
            total += resampled.samples
    for resampled in resampler.resample(None):
        for packet in ostream.encode(resampled):
            out.mux(packet)
        total += resampled.samples
    for packet in ostream.encode(None):
        out.mux(packet)
    out.close()
    seconds = total / 16000
    print(f"[audio] {seconds:.1f}s → {audio.name} ({audio.stat().st_size/1e6:.1f} MB)", flush=True)
    return seconds


def transcribe(audio: Path, out_json: Path, key: str, model: str, lang: str) -> dict:
    print(f"[asr] {model} ← {audio.name}", flush=True)
    proc = subprocess.run(
        [find_bl(), "speech", "recognize", "--url", str(audio), "--model", model,
         "--language", lang, "--out", str(out_json), "--output", "json", "--api-key", key],
        capture_output=True, text=True, env=bl_env(),
    )
    if proc.returncode != 0 or not out_json.exists():
        sys.stderr.write(proc.stdout[-2000:] + proc.stderr[-2000:])
        raise SystemExit(f"bl speech recognize 失败（exit={proc.returncode}）")
    return json.loads(out_json.read_text(encoding="utf-8"))


def sentences_of(data: dict) -> list[dict]:
    out = []
    for sent in (data.get("transcripts") or [{}])[0].get("sentences") or []:
        text = (sent.get("text") or "").strip()
        if not text:
            continue
        out.append({"begin": int(sent.get("begin_time") or 0), "end": int(sent.get("end_time") or 0),
                    "text": text, "words": sent.get("words") or []})
    out.sort(key=lambda s: (s["begin"], s["end"]))
    return out


def _split_long(sent: dict, max_ms: int, max_chars: int) -> list[dict]:
    words = [w for w in sent["words"] if (w.get("text") or "").strip()]
    if not words:
        text = sent["text"]
        if len(text) <= max_chars and sent["end"] - sent["begin"] <= max_ms:
            return [sent]
        chunks, cur = [], ""
        for piece in re.split(r"(?<=[,;:.!?])\s+", text):
            if cur and len(cur) + 1 + len(piece) > max_chars:
                chunks.append(cur); cur = piece
            else:
                cur = f"{cur} {piece}".strip()
        if cur:
            chunks.append(cur)
        total = max(1, len(chunks))
        span = max(1, sent["end"] - sent["begin"])
        return [{"begin": sent["begin"] + span * i // total, "end": sent["begin"] + span * (i + 1) // total,
                 "text": c, "words": []} for i, c in enumerate(chunks)]

    pieces, cur = [], []
    for word in words:
        cur.append(word)
        joined = "".join((w.get("text") or "") for w in cur).strip()
        ends_clause = bool(re.search(r"[,;:.!?]$", joined))
        too_long = len(joined) > max_chars or (word["end_time"] - cur[0]["begin_time"]) > max_ms
        if too_long or (ends_clause and len(joined) > max_chars * 0.6):
            pieces.append(cur); cur = []
    if cur:
        pieces.append(cur)
    out = []
    for group in pieces:
        text = "".join((w.get("text") or "") for w in group).strip()
        out.append({"begin": int(group[0]["begin_time"]), "end": int(group[-1]["end_time"]), "text": text})
    return out


def cues_from_asr(sentences: list[dict]) -> list[dict]:
    pieces: list[dict] = []
    for sent in sentences:
        if not sent["text"]:
            continue
        if sent["end"] <= sent["begin"]:
            sent["end"] = sent["begin"] + 1000
        for piece in _split_long(sent, MAX_CUE_MS, MAX_LINE_CHARS * 2):
            if piece["text"]:
                pieces.append(piece)
    merged: list[dict] = []
    for piece in pieces:
        if merged:
            prev = merged[-1]
            joined_len = len(prev["text"]) + 1 + len(piece["text"])
            if (joined_len <= MAX_LINE_CHARS * 2 and piece["end"] - prev["begin"] <= MAX_CUE_MS
                    and piece["begin"] - prev["end"] < 700):
                prev["text"] = f"{prev['text']} {piece['text']}".strip()
                prev["end"] = piece["end"]
                continue
        merged.append(dict(piece))
    return normalize_cues(merged)


# ---------------- 共用：清洗时间轴 ----------------
def normalize_cues(cues: list[dict]) -> list[dict]:
    """丢掉空/纯符号条，补最短时长，保证不重叠、留最小间隔。"""
    clean = []
    for cue in sorted(cues, key=lambda c: c["begin"]):
        text = cue.get("text", "").strip()
        if not text or not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text):
            continue
        if cue.get("end") is None or cue["end"] <= cue["begin"]:
            cue["end"] = cue["begin"] + 1500
        cue["text"] = text
        clean.append(cue)
    for i, cue in enumerate(clean):
        if cue["end"] - cue["begin"] < MIN_CUE_MS:
            cue["end"] = cue["begin"] + MIN_CUE_MS
        nxt = clean[i + 1] if i + 1 < len(clean) else None
        if nxt and cue["end"] > nxt["begin"] - MIN_GAP_MS:
            cue["end"] = max(cue["begin"] + MIN_CUE_MS, nxt["begin"] - MIN_GAP_MS)
    return clean


# ---------------- 2. 翻译 ----------------
def _translate_batch(batch: list[str], key: str, chat_model: str, system: str) -> list[str]:
    payload = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({"lines": batch}, ensure_ascii=False)},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        msg_file = handle.name
    try:
        proc = subprocess.run(
            [find_bl(), "text", "chat", "--model", chat_model, "--messages-file", msg_file,
             "--api-key", key, "--output", "json", "--quiet"],
            capture_output=True, text=True, env=bl_env(),
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout[-800:] + proc.stderr[-800:])
            raise SystemExit(f"bl text chat 失败（exit={proc.returncode}）——"
                             f"常见原因：node 不在 PATH（见 bl_env 注释）或 key/额度问题")
        raw = proc.stdout
        translated: list[str] = []
        content = raw
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                # bl text chat --output json 在部分版本里直接输出模型正文（JSON 数组）
                translated = [str(x) for x in parsed]
            elif isinstance(parsed, dict):
                node = parsed
                choices = node.get("choices")
                if isinstance(choices, list) and choices:
                    node = choices[0].get("message") or {}
                candidate = node.get("content") if isinstance(node, dict) else None
                content = candidate if isinstance(candidate, str) else json.dumps(node, ensure_ascii=False)
            else:
                content = str(parsed)
        except json.JSONDecodeError:
            pass
        if not translated:
            match = re.search(r"\[.*\]", str(content), re.S)
            translated = json.loads(match.group(0)) if match else []
        if len(translated) != len(batch):
            print(f"[mt] 警告：一批 {len(batch)} 条只返回 {len(translated)} 条，按序兜底", file=sys.stderr)
            translated = (list(translated) + [""] * len(batch))[:len(batch)]
        return [str(x).strip() for x in translated]
    finally:
        os.unlink(msg_file)


def translate(lines: list[str], key: str, chat_model: str, target: str,
              workers: int = 8) -> list[str]:
    """批量翻译；批间并行（网络等待为主），输出严格保持输入顺序。"""
    from concurrent.futures import ThreadPoolExecutor

    system = (
        f"You are a professional subtitle translator. Translate each English subtitle line into "
        f"natural, colloquial {target} (Simplified Chinese). Rules: translate line by line, keeping the "
        f"SAME order and the SAME count as the input; never merge or split lines; keep ALL-CAPS speaker "
        f"labels (e.g. 'GLENN STEARNS:') and put the Chinese translation after the label on the same line; "
        f"keep bracketed sound descriptions such as [door slams] and (laughs) translated inside the same "
        f"kind of brackets; keep ♪ music markers; use established Chinese renderings for people and place "
        f"names and keep them consistent across lines; keep the style spoken and concise for on-screen "
        f'subtitles; output ONLY a JSON array of strings, e.g. ["译文1","译文2"].'
    )
    batches = [lines[i:i + TRANSLATE_BATCH] for i in range(0, len(lines), TRANSLATE_BATCH)]
    results: list[list[str] | None] = [None] * len(batches)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_translate_batch, batch, key, chat_model, system): idx
                   for idx, batch in enumerate(batches)}
        for future in futures:
            pass  # 保持 futures 存活；结果在下面按序收集
        for future, idx in sorted(((f, i) for f, i in futures.items()), key=lambda x: x[1]):
            results[idx] = future.result()
            done += 1
            print(f"[mt] {done}/{len(batches)} 批（{min(done * TRANSLATE_BATCH, len(lines))}/{len(lines)} 条）",
                  flush=True)
    flat: list[str] = []
    for chunk in results:
        flat.extend(chunk or [])
    return flat[:len(lines)]


# ---------------- 3. 写 SRT ----------------
def wrap(text: str, limit: int = MAX_LINE_CHARS) -> str:
    out_lines: list[str] = []
    for para in text.split("\n"):
        if len(para) <= limit:
            out_lines.append(para)
            continue
        words, cur = para.split(), ""
        for word in words:
            if cur and len(cur) + 1 + len(word) > limit:
                out_lines.append(cur); cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            out_lines.append(cur)
    if len(out_lines) > 2:
        out_lines = [out_lines[0], " ".join(out_lines[1:])]
    return "\n".join(out_lines)


def write_srt(cues: list[dict], path: Path, translations: list[str] | None = None, wrap_source: bool = True) -> None:
    blocks = []
    for i, cue in enumerate(cues, 1):
        source = wrap(cue["text"]) if wrap_source else cue["text"].strip()
        lines = [source]
        if translations:
            lines.append(translations[i - 1])
        blocks.append(f"{i}\n{ts(cue['begin'])} --> {ts(cue['end'])}\n" + "\n".join(lines))
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    print(f"[srt] {len(cues)} 条 → {path}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="视频 → 双语字幕（复用优先：内嵌字幕 > 外挂字幕 > ASR）")
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="输出目录（默认与视频同目录）")
    ap.add_argument("--source", choices=["auto", "embedded", "sidecar", "asr"], default="auto")
    ap.add_argument("--source-lang", default="en", help="ASR 语言提示 / 内嵌轨语言偏好")
    ap.add_argument("--target-lang", default="zh")
    ap.add_argument("--sub-index", type=int, default=None, help="指定内嵌字幕轨 index")
    ap.add_argument("--asr-model", default=ASR_MODEL_DEFAULT)
    ap.add_argument("--chat-model", default=CHAT_MODEL_DEFAULT)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--asr-json", type=Path, default=None, help="复用已有 ASR 结果（跳过解音轨与转写）")
    ap.add_argument("--no-translate", action="store_true", help="只出原文 SRT")
    ap.add_argument("--keep-audio", action="store_true")
    args = ap.parse_args()

    video: Path = args.video.expanduser().resolve()
    if not video.exists():
        raise SystemExit(f"找不到视频：{video}")
    out_dir = (args.out or video.parent).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = video.stem

    cues: list[dict] | None = None
    origin = ""
    if args.source in ("auto", "embedded"):
        tracks = embedded_tracks(video)
        text_tracks = [t for t in tracks if t["text_based"]]
        if tracks:
            print(f"[probe] 字幕轨 {len(tracks)} 条：" + "; ".join(
                f"idx={t['index']} {t['codec']}/{t['lang'] or '?'} {t['title']}" for t in tracks), flush=True)
        if text_tracks:
            try:
                cues, info = extract_embedded(video, args.sub_index, args.source_lang)
                if cues:
                    origin = f"内嵌字幕轨 idx={info['index']} {info['codec']}/{info.get('language','?')}"
            except SystemExit:
                if args.source == "embedded":
                    raise
    if cues is None and args.source in ("auto", "sidecar"):
        found = sidecar_path(video)
        if found:
            cues = read_ass(found) if found.suffix.lower() in (".ass", ".ssa") else read_srt(found)
            if cues:
                origin = f"外挂字幕 {found.name}"
    if cues is None and args.source == "auto":
        print("[probe] 没有可用字幕 → 走 ASR 转写", flush=True)
    if cues is None:
        if args.source in ("embedded", "sidecar"):
            raise SystemExit(f"--source {args.source} 但没找到可用字幕")
        key = api_key(args.api_key)
        with tempfile.TemporaryDirectory(prefix="v2srt-") as tmp:
            if args.asr_json:
                asr_json = args.asr_json.expanduser().resolve()
                if not asr_json.exists():
                    raise SystemExit(f"找不到 --asr-json：{asr_json}")
                print(f"[asr] 复用 {asr_json.name}", flush=True)
            else:
                audio = Path(tmp) / f"{stem}.flac"
                asr_json = out_dir / f"{stem}.asr.json"
                extract_audio(video, audio)
                transcribe(audio, asr_json, key, args.asr_model, args.source_lang)
                if args.keep_audio:
                    shutil.copy2(audio, out_dir / audio.name)
        cues = cues_from_asr(sentences_of(json.loads(asr_json.read_text(encoding="utf-8"))))
        origin = "ASR 转写"

    if not cues:
        raise SystemExit("没有切出任何字幕条")
    print(f"[src] {origin}：{len(cues)} 条，{ts(cues[0]['begin'])} → {ts(cues[-1]['end'])}", flush=True)

    source_srt = out_dir / f"{stem}.source.srt"
    write_srt(cues, source_srt, wrap_source=False)

    if args.no_translate:
        final = out_dir / f"{stem}.srt"
        write_srt(cues, final, wrap_source=False)
        print(f"[done] {final}")
        return

    key = api_key(args.api_key)
    translations = translate([c["text"] for c in cues], key, args.chat_model, args.target_lang)
    final = out_dir / f"{stem}.srt"
    write_srt(cues, final, translations, wrap_source=origin.startswith("ASR"))
    print(f"[done] {final}")


if __name__ == "__main__":
    main()
