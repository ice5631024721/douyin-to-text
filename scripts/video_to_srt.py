#!/usr/bin/env python3
"""视频 → 双语字幕（SRT），时间轴与视频一一对应。

复用优先（不重复造轮子）：能拿到现成字幕就不转写——
  1. 内嵌字幕轨（MKV/MP4 的 srt/ass 文本轨，含 SDH）→ ffprobe 探轨 + ffmpeg 抽取，零 ASR 成本
  2. 同目录外挂字幕（<视频基名>.srt / .ass / .vtt）→ 直接复用
  3. 都没有才转写（ASR）→ ffmpeg 抽 16k 单声道 + bl 异步 filetrans 拿句级/词级时间戳
再翻译：默认云端 bl text chat（qwen-mt-flash），--backend local 可切本地 llama.cpp / mlx-lm
的 OpenAI 兼容服务；输出「原文行 + 译文行」的双语 SRT。

工具链（全部本机已装）：ffmpeg / ffprobe（/opt/homebrew/bin，2026-09-27 用 brew 修好）、
bailian CLI（bl，ASR 与云端翻译）、可选 llama-server（本地翻译）。
注意：qwen-mt-turbo 与 gummy-* 于 2026-10-10 下线，默认翻译模型取 qwen-mt-flash。

跑法：
  python video_to_srt.py <video> [--out <dir>] [--source auto|embedded|sidecar|asr]
      [--source-lang en] [--target-lang zh] [--sub-index N] [--asr-model ...]
      [--chat-model qwen-mt-flash] [--backend cloud|local] [--local-server URL]
      [--asr-json <已有.json>] [--no-translate] [--batch 40] [--workers 5]

产出：<out>/<视频基名>.srt（双语）、.source.srt（复用的原文）、.<lang>.json（译文缓存，重切不重付）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import time
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
ASR_MODEL_DEFAULT = "qwen-audio-3.1-asr-flash-filetrans"
# 实测（2026-09-27，765 条字幕）：flash 与 max 译文质量相当，flash 快约一倍
# qwen-mt-turbo 与 gummy-* 于 2026-10-10 下线；qwen-mt-flash 同价同档且在售（见 ASR-API.md）
CHAT_MODEL_DEFAULT = "qwen-mt-flash"
LOCAL_SERVER_DEFAULT = "http://127.0.0.1:8080"
DELIM = "\n|||\n"
LOCAL_PROMPT = ("请将以下文本准确翻译为中文。你必须在译文中保留等量的分隔符 ||| ，"
                "绝对不可遗漏、转义或翻译该符号，并注意分隔符的位置。\n\n")
TEXT_SUB_CODECS = {"srt", "subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
SIDECAR_EXTS = (".srt", ".ass", ".ssa", ".vtt")
MAX_LINE_CHARS = 42          # 仅 ASR 路线需要重切时用
MAX_CUE_MS = 7000
MIN_CUE_MS = 800
MIN_GAP_MS = 40
REQUESTS_PER_MIN = 50   # qwen-mt-flash 限额：60 次/分钟 + 3.5 万 token/分钟；留安全余量
TRANSLATE_BATCH = 20   # 实测 40 条/批会频繁触发"模型合并短句→条数不符→二分"，20 条最稳最快
TRANSLATE_TIMEOUT = 180   # bl --timeout：实测 8 并发下偶发 ETIMEDOUT，给足时间
TRANSLATE_ATTEMPTS = 3    # 单批最多重试次数（失败后二分）
ASR_TIMEOUT = 600


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


# ---------------- 1a. 内嵌字幕轨（ffprobe 探轨 + ffmpeg 抽取）----------------
def ffprobe_json(video: Path) -> dict:
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-probesize", "20M", "-analyzeduration", "20M", "-show_entries",
         "stream=index,codec_type,codec_name:stream_tags=language,title:format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"ffprobe 失败：{proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout or "{}")


def embedded_tracks(video: Path) -> list[dict]:
    tracks = []
    for st in ffprobe_json(video).get("streams") or []:
        if st.get("codec_type") != "subtitle":
            continue
        tags = st.get("tags") or {}
        codec = (st.get("codec_name") or "").lower()
        tracks.append({"index": st.get("index"), "codec": codec,
                       "lang": (tags.get("language") or "").lower(),
                       "title": tags.get("title") or "",
                       "text_based": codec in TEXT_SUB_CODECS})
    return tracks


def extract_embedded(video: Path, index: int | None, prefer_lang: str | None) -> tuple[list[dict], dict]:
    tracks = embedded_tracks(video)
    if not tracks:
        raise SystemExit("视频里没有字幕轨")
    track = None
    if index is not None:
        track = next((t for t in tracks if t["index"] == index), None)
        if track is None:
            raise SystemExit(f"没有 idx={index} 的字幕轨")
    else:
        pool = [t for t in tracks if t["text_based"]] or tracks
        if prefer_lang:
            track = next((t for t in pool if t["lang"].startswith(prefer_lang)), None)
        track = track or pool[0]
    if not track["text_based"]:
        raise SystemExit(f"字幕轨 idx={track['index']} 是图形字幕（{track['codec']}），需要先 OCR")
    with tempfile.TemporaryDirectory(prefix="v2srt-sub-") as tmp:
        raw_srt = Path(tmp) / "sub.srt"
        proc = subprocess.run([FFMPEG, "-v", "error", "-y", "-probesize", "20M", "-analyzeduration", "20M",
                               "-i", str(video),
                               "-map", f"0:{track['index']}", "-c:s", "srt", str(raw_srt)],
                              capture_output=True, text=True)
        if proc.returncode != 0 or not raw_srt.exists():
            raise SystemExit(f"ffmpeg 抽字幕失败：{proc.stderr.strip()[:200]}")
        cues = read_srt(raw_srt)
    return cues, {"index": track["index"], "codec": track["codec"],
                  "language": track["lang"], "title": track["title"]}


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


# ---------------- 1c. ASR 路线（ffmpeg 抽音轨）----------------
def extract_audio(video: Path, audio: Path) -> float:
    info = ffprobe_json(video)
    duration = float((info.get("format") or {}).get("duration") or 0)
    stream = next((x for x in (info.get("streams") or []) if x.get("codec_type") == "audio"), None)
    if stream is None:
        raise SystemExit(f"{video} 里没有音频流")
    print(f"[audio] {stream.get('codec_name')} → 16kHz 单声道", flush=True)
    proc = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(video),
                           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(audio)],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not audio.exists():
        raise SystemExit(f"ffmpeg 抽音轨失败：{proc.stderr.strip()[:200]}")
    print(f"[audio] {duration:.0f}s → {audio.name} ({audio.stat().st_size/1e6:.1f} MB)", flush=True)
    return duration


def transcribe(audio: Path, out_json: Path, key: str, model: str, lang: str) -> dict:
    print(f"[asr] {model} ← {audio.name}", flush=True)
    proc = subprocess.run(
        [find_bl(), "speech", "recognize", "--url", str(audio), "--model", model,
         "--language", lang, "--out", str(out_json), "--output", "json",
         "--api-key", key, "--timeout", str(ASR_TIMEOUT)],
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
class _RateLimiter:
    """按"每分钟 N 次"节流。百炼 qwen-mt-flash 限 60 次/分钟，超了就是一串 429 退避（比限速更慢）。"""

    def __init__(self, rpm: int) -> None:
        self._interval = 60.0 / max(1, rpm)
        self._lock = threading.Lock()
        self._next_at = time.monotonic()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                time.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = now + self._interval


_LIMITER: "_RateLimiter | None" = None

# 目标：**少往返 + 不错位**。实测（qwen-mt-flash，20 条/批）单批约 1.8–2 s；
# 逐行翻译要 765 次往返且触发 429 限流，所以批量优先、错位才二分、补译也成批。
def _translate_local(batch: list[str], server: str, timeout: int) -> list[str] | None:
    """本地后端：llama.cpp / mlx-lm 的 OpenAI 兼容 /v1/chat/completions（Hy-MT2 官方分隔符模板）。"""
    prompt = LOCAL_PROMPT + DELIM.join(batch)
    payload = {"model": "local", "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.7, "top_p": 0.6, "top_k": 20,
               "repetition_penalty": 1.05, "max_tokens": 4096, "stream": False}
    req = urllib.request.Request(server.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"[mt] 本地服务失败：{type(exc).__name__} {str(exc)[:110]}", file=sys.stderr)
        return None
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    parts = [x.strip() for x in text.split("|||")]
    return parts if len(parts) == len(batch) else None


def _cloud_payload(batch: list[str], chat_model: str, system: str) -> list[dict]:
    """构造请求体。qwen-mt-* 只吃 user/assistant（带 system 会 400）。"""
    if not chat_model.startswith("qwen-mt"):
        return [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"lines": batch}, ensure_ascii=False)}]
    if len(batch) == 1:
        return [{"role": "user", "content": "把下面这句英文翻译成简体中文，只输出译文：\n" + batch[0]}]
    # 编号标记协议：模型偶尔把一句拆成两条（实测 20 条回 23/25 条），二分永远不收敛；
    # 带 [[n]] 标记就能把拆出来的片段按标记归位，一次请求拿全，不用反复二分。
    marked = "\n".join(f"[[{i + 1}]] {x}" for i, x in enumerate(batch))
    return [{"role": "user", "content":
             "把下面每一行英文翻译成简体中文。必须原样保留每行开头的编号标记 [[n]]，"
             "一个标记对应一条译文，不要合并或拆分编号：\n" + marked}]


def _ask_cloud(batch: list[str], key: str, chat_model: str, system: str,
               timeout: int = TRANSLATE_TIMEOUT) -> list[str] | None:
    """一次云端请求。返回 None = 失败或条数不符（交由上层二分），绝不猜测对齐关系。"""
    payload = _cloud_payload(batch, chat_model, system)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        msg_file = fh.name
    try:
        raw = ""
        for attempt in range(1, TRANSLATE_ATTEMPTS + 1):
            proc = subprocess.run(
                [find_bl(), "text", "chat", "--model", chat_model, "--messages-file", msg_file,
                 "--api-key", key, "--output", "json", "--quiet", "--timeout", str(timeout)],
                capture_output=True, text=True, env=bl_env())
            raw = proc.stdout
            if proc.returncode == 0 and raw.strip():
                break
            detail = (proc.stderr or raw)[-160:].replace("\n", " ")
            print(f"[mt] 第 {attempt}/{TRANSLATE_ATTEMPTS} 次失败：{detail}", file=sys.stderr)
            if attempt < TRANSLATE_ATTEMPTS:
                time.sleep(2 * attempt)          # 429 限流时退避
        else:
            return None
        if '"error"' in raw[:200] and '"code"' in raw[:600]:
            return None

        content: str | None = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
            return [x.strip() for x in parsed]
        if isinstance(parsed, dict):
            node = (parsed.get("choices") or [{}])[0].get("message", {}) if parsed.get("choices") else parsed
            content = node.get("content") if isinstance(node, dict) else None
            content = content if isinstance(content, str) else json.dumps(node, ensure_ascii=False)
        elif isinstance(parsed, str):
            content = parsed
        else:
            content = raw
        if len(batch) == 1:                        # 单行：直接取正文，不必是 JSON
            text = (content or "").strip()
            if text.startswith("[") :
                try:
                    arr = json.loads(text, strict=False)
                    return [str(arr[0]).strip()] if isinstance(arr, list) and arr else None
                except json.JSONDecodeError:
                    pass
            return [text] if text else None
        # 编号标记协议解析：文本按 [[n]] 切片，同编号的片段合并（模型拆句时仍能归位）
        marked_text = content or ""
        found = list(re.finditer(r"\[\[\s*(\d+)\s*\]\]", marked_text))
        if found and len(batch) > 1:
            slots: dict[int, list[str]] = {}
            for pos, m in enumerate(found):
                n = int(m.group(1))
                end = found[pos + 1].start() if pos + 1 < len(found) else len(marked_text)
                piece = marked_text[m.end():end].strip()
                if piece:
                    slots.setdefault(n, []).append(piece)
            if all(i + 1 in slots for i in range(len(batch))):
                return [" ".join(slots[i + 1]).strip() for i in range(len(batch))]
            return None
        match = re.search(r"\[.*\]", content or "", re.S)
        if not match:
            return None
        try:
            arr = json.loads(match.group(0), strict=False)   # 模型常在字符串里漏转义换行
        except json.JSONDecodeError:
            return None
        return [str(x).strip() for x in arr] if isinstance(arr, list) else None
    finally:
        os.unlink(msg_file)


def _translate_batch(batch: list[str], key: str, chat_model: str, system: str,
                     timeout: int = TRANSLATE_TIMEOUT, backend: str = "cloud",
                     local_server: str = LOCAL_SERVER_DEFAULT, depth: int = 0) -> list[str]:
    """一批 → 译文列表，长度恒等于输入。

    条数不符就二分（递归到单行），**绝不末尾补空**——补空等于把整批译文按错位映射出去。
    """
    if not batch:
        return []
    if _LIMITER is not None and backend != "local":
        _LIMITER.wait()
    out = (_translate_local(batch, local_server, timeout) if backend == "local"
           else _ask_cloud(batch, key, chat_model, system, timeout))
    if out is not None and len(out) == len(batch):
        return out
    if len(batch) == 1:
        print(f"[mt] 单行仍失败：{batch[0][:40]!r}", file=sys.stderr)
        return [out[0] if out else ""]
    mid = len(batch) // 2
    print(f"[mt] {len(batch)} 条只回 {len(out) if out else 0} 条 → 二分", file=sys.stderr)
    return (_translate_batch(batch[:mid], key, chat_model, system, timeout, backend, local_server, depth + 1)
            + _translate_batch(batch[mid:], key, chat_model, system, timeout, backend, local_server, depth + 1))


def repair_missing(lines: list[str], translations: list[str], key: str, chat_model: str,
                   target: str, timeout: int, backend: str, local_server: str,
                   rounds: int = 2) -> list[str]:
    """成批补译（每批 10 条，最多 rounds 轮），不逐行。"""
    for rnd in range(1, rounds + 1):
        bad = [i for i, t in enumerate(translations)
               if not re.search(r"[\u4e00-\u9fff]", t or "") and re.search(r"[A-Za-z]", lines[i])]
        if not bad:
            return translations
        print(f"[mt] 第 {rnd} 轮补译：{len(bad)} 条", flush=True)
        for start in range(0, len(bad), 10):
            idx = bad[start:start + 10]
            fixed = _translate_batch([lines[i] for i in idx], key, chat_model, target,
                                     timeout, backend, local_server)
            for i, t in zip(idx, fixed):
                if re.search(r"[\u4e00-\u9fff]", t or ""):
                    translations[i] = t
    return translations


def translate(lines: list[str], key: str, chat_model: str, target: str,
              workers: int = 4, batch_size: int = TRANSLATE_BATCH,
              timeout: int = TRANSLATE_TIMEOUT, partial_path: Path | None = None,
              fingerprint: str = "", backend: str = "cloud",
              local_server: str = LOCAL_SERVER_DEFAULT, rpm: int = REQUESTS_PER_MIN) -> list[str]:
    """批量翻译；批间并行，输出严格保持输入顺序。

    增量缓存：每完成一批就把该批结果写进 partial_path（按指纹+批大小校验），
    重跑时只补缺失批次——长片翻译中途失败不再从零开始。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    global _LIMITER
    _LIMITER = _RateLimiter(rpm)

    system = (f"You are a professional subtitle translator. Translate each English line into "
              f"{target}. Keep the same order and count. Output ONLY a JSON array of strings.")
    batches = [lines[i:i + batch_size] for i in range(0, len(lines), batch_size)]
    results: list[list[str] | None] = [None] * len(batches)
    lock = threading.Lock()

    def persist() -> None:
        if partial_path is None:
            return
        payload = {"fingerprint": fingerprint, "batch_size": batch_size, "count": len(lines),
                   "batches": {str(i): r for i, r in enumerate(results) if r is not None}}
        partial_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    if partial_path is not None and partial_path.exists():
        try:
            cached = json.loads(partial_path.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint and cached.get("batch_size") == batch_size:
                for key_idx, value in (cached.get("batches") or {}).items():
                    i = int(key_idx)
                    if 0 <= i < len(batches) and isinstance(value, list) and len(value) == len(batches[i]):
                        results[i] = value
                reused = sum(1 for r in results if r is not None)
                if reused:
                    print(f"[mt] 续跑：复用已完成 {reused}/{len(batches)} 批", flush=True)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    todo = [i for i, r in enumerate(results) if r is None]
    done = len(batches) - len(todo)
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_translate_batch, batches[i], key, chat_model, system, timeout,
                                   backend, local_server): i for i in todo}
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
                with lock:
                    done += 1
                    persist()
                print(f"[mt] {done}/{len(batches)} 批（{min(done * batch_size, len(lines))}/{len(lines)} 条）",
                      flush=True)

    translations: list[str] = []
    for r in results:
        translations.extend(r or [])
    return translations[:len(lines)]


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


T0 = time.time()
T = {"probe": 0.0, "audio": 0.0, "asr": 0.0, "mt": 0.0, "write": 0.0}


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
    ap.add_argument("--no-cache", action="store_true", help="忽略译文缓存，强制重新翻译")
    ap.add_argument("--batch", type=int, default=TRANSLATE_BATCH, help="每次翻译的条数（默认 20；实测 40 会大量触发二分反而更慢）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条字幕（小样验证用，0=全部）")
    ap.add_argument("--workers", type=int, default=4, help="翻译并发数（默认 4；受 60 次/分钟限流约束）")
    ap.add_argument("--rpm", type=int, default=REQUESTS_PER_MIN, help="每分钟最大请求数（默认 45，限流上限 60）")
    ap.add_argument("--backend", choices=["cloud", "local"], default="cloud",
                    help="翻译后端：cloud=bl text chat（默认 qwen-mt-flash）；local=本地 OpenAI 兼容服务")
    ap.add_argument("--local-server", default=LOCAL_SERVER_DEFAULT,
                    help="本地后端地址，如 llama-server --port 8080 或 mlx_lm.server")
    ap.add_argument("--timeout", type=int, default=TRANSLATE_TIMEOUT, help="单次 bl 调用超时秒数（默认 180）")
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
    _stage = time.time()
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
        T["audio"] = time.time() - _stage
        _stage = time.time()
        cues = cues_from_asr(sentences_of(json.loads(asr_json.read_text(encoding="utf-8"))))
        T["asr"] = time.time() - _stage
        origin = "ASR 转写"

    T["probe"] = time.time() - _stage
    if args.limit and args.limit > 0:
        cues = cues[:args.limit]
        print(f"[limit] 仅处理前 {len(cues)} 条", flush=True)
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
    lines = [c["text"] for c in cues]
    fingerprint = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]
    cache_path = out_dir / f"{stem}.{args.target_lang}.json"
    translations: list[str] | None = None
    if cache_path.exists() and not args.no_cache:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("count") == len(lines) and cached.get("fingerprint") == fingerprint \
                    and len(cached.get("translations") or []) == len(lines):
                translations = [str(x) for x in cached["translations"]]
                print(f"[mt] 复用译文缓存 {cache_path.name}（{len(translations)} 条）", flush=True)
        except (json.JSONDecodeError, KeyError, TypeError):
            translations = None
    _stage = time.time()
    if translations is None:
        translations = translate(lines, key, args.chat_model, args.target_lang,
                                 workers=args.workers, batch_size=args.batch, timeout=args.timeout,
                                 partial_path=cache_path, fingerprint=fingerprint,
                                 backend=args.backend, local_server=args.local_server, rpm=args.rpm)
        cache_path.write_text(json.dumps(
            {"fingerprint": fingerprint, "count": len(lines), "model": args.chat_model,
             "translations": translations}, ensure_ascii=False), encoding="utf-8")
        print(f"[mt] 译文已缓存 → {cache_path.name}（重切/重跑不再重复付费）", flush=True)
    # 补译校验：漏条/回原文的行成批补译（不逐行，避免 429 与慢）
    if any(not re.search(r"[\u4e00-\u9fff]", t or "") for t in translations):
        translations = repair_missing(lines, translations, key, args.chat_model, args.target_lang,
                                      args.timeout, args.backend, args.local_server)
        if cache_path is not None:
            cache_path.write_text(json.dumps(
                {"fingerprint": fingerprint, "count": len(lines), "model": args.chat_model,
                 "translations": translations}, ensure_ascii=False), encoding="utf-8")
    T["mt"] = time.time() - _stage
    _stage = time.time()
    final = out_dir / f"{stem}.srt"
    write_srt(cues, final, translations, wrap_source=origin.startswith("ASR"))
    T["write"] = time.time() - _stage

    total = time.time() - T0
    def _fmt(x: float) -> str:
        return f"{x:.1f}"
    print(f"【耗时】探测 {_fmt(T["probe"])}s · 抽音轨 {_fmt(T["audio"])}s · 转写 {_fmt(T["asr"])}s · "
          f"翻译 {_fmt(T["mt"])}s · 写盘 {_fmt(T["write"])}s · 总计 {_fmt(total)}s"
          f"（{len(cues)} 条，{origin}）", flush=True)
    try:  # 与抖音分支共用一份时间日志，便于跨次比较
        log = Path.home() / ".dsh/douyin-timing.log"
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\t".join([
                time.strftime("%Y-%m-%d %H:%M:%S"), f"local:{stem[:40]}",
                _fmt(T["probe"]), _fmt(T["audio"]), _fmt(T["asr"]), _fmt(T["mt"]),
                _fmt(T["write"]), _fmt(total), str(len(cues)), str(sum(len(c["text"]) for c in cues)),
            ]) + "\n")
    except OSError as exc:
        print(f"[timing] 写日志失败：{exc}", file=sys.stderr)
    print(f"[done] {final}")


if __name__ == "__main__":
    main()
