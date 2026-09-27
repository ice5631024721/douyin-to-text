#!/usr/bin/env python3
"""视频 → 任意语言对的双语 ASS 字幕，时间轴与视频一一对应。

任意源语言 → 任意语言对（默认 en,zh = 英上中下）：语言对里排第一的那行在最上，
用醒目的 Upper 样式（更大、加粗、纯白），其余行用 Lower（稍小、暖白）。
源语言若在语言对里，该行直接复用原文，不翻译、不花钱。

复用优先（不重复造轮子）：能拿到现成字幕就不转写——
  1. 内嵌字幕轨（MKV/MP4 的 srt/ass 文本轨，含 SDH）→ ffprobe 探轨 + ffmpeg 抽取，零 ASR 成本
  2. 同目录外挂字幕（<视频基名>.srt / .ass / .vtt）→ 直接复用
  3. 都没有才转写（ASR）→ ffmpeg 抽 16k 单声道 + bl 异步 filetrans 拿句级/词级时间戳
再翻译：默认云端 bl text chat（qwen-mt-flash），--backend local 可切本地 llama.cpp / mlx-lm
的 OpenAI 兼容服务。

为什么出 ASS 而不是 SRT：SubRip 格式**没有任何样式位**，字号/颜色/加粗/描边都无处安放，
"上面的字幕更醒目"这类要求只能由 ASS 承载（每行一个样式，正文里用 \\r 按行切换）。

工具链：ffmpeg / ffprobe（**走 PATH 优先，再按平台兜底**：macOS 常见 /opt/homebrew、Linux /usr/bin、
Windows C:\\ffmpeg\\bin）、bailian CLI（bl，ASR 与云端翻译；Windows 上是 bl.cmd，过 cmd /c 执行）、
可选 llama-server（本地翻译）。
注意：qwen-mt-turbo 与 gummy-* 于 2026-10-10 下线，默认翻译模型取 qwen-mt-flash。

跑法：
  python omnisub.py <video> [--out <dir>] [--source auto|embedded|sidecar|asr]
      [--source-lang auto|en|zh|ja|ko|fr|…] [--subtitles en,zh] [--sub-index N]
      [--asr-model ...] [--chat-model qwen-mt-flash] [--backend cloud|local] [--local-server URL]
      [--asr-json <已有.json>] [--no-translate] [--batch 20] [--workers 4]
      [--refresh-source] [--verify-sync auto|on|off] [--limit N] [--no-log]
      [--cache-dir <中间产物目录>]
  （--target-lang 已废弃，等价于 --subtitles <源语言>,<目标语言>，仅为兼容保留）

产出：**视频目录只多出一个 <视频基名>.ass**（双语，或 --no-translate 时的单语）——
  中间产物（<基名>.source.srt / .source.json / .asr.json / .<lang>.json）默认写进平台缓存目录
  （macOS ~/Library/Caches/omnisub/、Linux $XDG_CACHE_HOME/omnisub/、
  Windows %LOCALAPPDATA%\\omnisub\\Cache），可用 --cache-dir 改；这样既能"重切不重付"，
  又不往片库里堆文件。
与视频同名同目录即被 mpv / IINA / VLC / MPC-HC / PotPlayer / Infuse 自动加载。
媒体库（Plex / Jellyfin / Emby）对外挂 ASS 支持不一，必要时由用户自行改扩展名或重新封装。
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

def _tool_candidates(name: str) -> list[str]:
    """按平台给出可执行文件的兜底路径（先看 PATH，找不到才看这里）。"""
    home = Path.home()
    out: list[str] = []
    if sys.platform == "darwin":
        out += [f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]
    elif os.name == "nt":
        out += [rf"C:\ffmpeg\bin\{name}.exe", rf"C:\Program Files\ffmpeg\bin\{name}.exe"]
    else:
        out += [f"/usr/bin/{name}", f"/usr/local/bin/{name}", f"/snap/bin/{name}"]
    out += [str(home / ".local/bin" / name), str(home / "bin" / name)]
    return out


def find_tool(name: str, aliases: tuple[str, ...] = ()) -> str:
    """定位外部可执行文件：**优先 PATH**（macOS/Linux/Windows 通用），再按平台兜底。

    aliases 用于 Windows 的 shim 后缀（npm 装出来的是 `bl.cmd` 而不是 `bl`）。
    都没有就原样返回名字，让子进程抛出"找不到命令"，比在这里编一个假路径清楚。
    """
    for candidate in (name, *aliases):
        found = shutil.which(candidate)
        if found:
            return found
    for cand in _tool_candidates(name):
        if Path(cand).exists():
            return cand
    return name


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
ASR_MODEL_DEFAULT = "qwen-audio-3.1-asr-flash-filetrans"
# 实测（2026-09-27，765 条字幕）：flash 与 max 译文质量相当，flash 快约一倍
# qwen-mt-turbo 与 gummy-* 于 2026-10-10 下线；qwen-mt-flash 同价同档且在售（见 ASR-API.md）
CHAT_MODEL_DEFAULT = "qwen-mt-flash"
LOCAL_SERVER_DEFAULT = "http://127.0.0.1:8080"
DELIM = "\n|||\n"


def local_prompt(src_lang: str, tgt_lang: str) -> str:
    """本地后端的 Hy-MT2 分隔符模板。方向必须跟着语言对走，不能写死中文。"""
    return (f"请将以下{lang_name(src_lang)}文本准确翻译为{lang_name(tgt_lang)}。"
            "你必须在译文中保留等量的分隔符 ||| ，绝对不可遗漏、转义或翻译该符号，"
            "并注意分隔符的位置。\n\n")



TEXT_SUB_CODECS = {"srt", "subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
SIDECAR_EXTS = (".srt", ".ass", ".ssa", ".vtt")
MAX_LINE_CHARS = 42          # 仅 ASR 路线需要重切时用
MAX_CUE_MS = 7000
MIN_CUE_MS = 800
# 与下一句紧贴时优先让位给下一句；让不出 MIN_READABLE_MS 就退回 MIN_CUE_MS
# （宁可 0.5s 极短重叠，也不出 0.2s 闪现——实测 E08 全片 684 条里只触发 1 次）
MIN_READABLE_MS = 400
MIN_GAP_MS = 40
SOURCE_SUFFIX = ".source"     # 原文缓存：<基名>.source.srt / .source.json（自家中间产物）
MONO_SUFFIX = ".mono"         # 只出原文时的单语产物：<基名>.mono.ass
REQUESTS_PER_MIN = 50   # qwen-mt-flash 限额：60 次/分钟 + 3.5 万 token/分钟；留安全余量
TRANSLATE_BATCH = 20   # 实测 40 条/批会频繁触发"模型合并短句→条数不符→二分"，20 条最稳最快
TRANSLATE_TIMEOUT = 180   # bl --timeout：实测 8 并发下偶发 ETIMEDOUT，给足时间
TRANSLATE_ATTEMPTS = 3    # 单批最多重试次数（失败后二分）
ASR_TIMEOUT = 600

# ---------------- 语言：任意源语言 → 任意语言对的双语字幕 ----------------
# 交付契约：--subtitles 给的语言对**按顺序**决定行序，第一行在上、用醒目样式（见 ASS_STYLES）。
# 源语言若出现在语言对里，该行直接用原文（不翻译、不花钱）；不在则整对都翻译。
LANGS_DEFAULT = ("en", "zh")
LANG_NAMES = {
    "zh": "简体中文", "en": "英文", "ja": "日文", "ko": "韩文", "fr": "法文",
    "de": "德文", "es": "西班牙文", "ru": "俄文", "pt": "葡萄牙文", "it": "意大利文",
    "ar": "阿拉伯文", "th": "泰文", "vi": "越南文", "id": "印尼文", "tr": "土耳其文",
    "hi": "印地文", "nl": "荷兰文", "pl": "波兰文", "sv": "瑞典文", "ms": "马来文",
}
# ffprobe 的 ISO639-2/B、bl 的 ISO639-1、带地区码的标签都要能认
LANG_ALIASES = {
    "zh-cn": "zh", "zh-hans": "zh", "chs": "zh",
    "chi": "zh", "zho": "zh", "cmn": "zh",
    "eng": "en", "en-us": "en", "en-gb": "en",
    "jpn": "ja", "jp": "ja", "ja-jp": "ja",
    "kor": "ko", "ko-kr": "ko",
    "fre": "fr", "fra": "fr", "deu": "de", "ger": "de", "spa": "es", "rus": "ru",
    "por": "pt", "ita": "it", "ara": "ar", "tha": "th", "vie": "vi", "ind": "id",
    "tur": "tr", "hin": "hi", "nld": "nl", "dut": "nl", "pol": "pl", "swe": "sv",
    "msa": "ms", "may": "ms",
}
# 繁体中文单独成一个码：旧版把 zh-TW/zh-Hant 一律并成 zh，于是用户要繁体、拿到简体，
# 全程没有任何提示。归成 zh-Hant 后它就是一个独立目标，提示词写"繁體中文"。
LANG_HANT = {"zh-tw", "zh-hk", "zh-mo", "zh-hant", "zh-hant-tw", "cht"}
LANG_NAMES["zh-Hant"] = "繁體中文"
# 有**独占文字区**的语言：能用 Unicode 区块直接判定，不需要语言模型。
# ko 必须排在 ja/zh 之前（谚文与汉字互不相交，但 ja 的判据要用到汉字，顺序不能乱）。
SCRIPT_LANGS = {
    "ko": re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]"),   # 谚文
    "ja": re.compile(r"[\u3040-\u309f\u30a0-\u30ff]"),                   # 平假名/片假名
    "ru": re.compile(r"[\u0400-\u04ff]"),                                 # 西里尔
    "ar": re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]"),   # 阿拉伯
    "he": re.compile(r"[\u0590-\u05ff]"),                                 # 希伯来
    "el": re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]"),                  # 希腊
    "th": re.compile(r"[\u0e00-\u0e7f]"),                                 # 泰文
    "hi": re.compile(r"[\u0900-\u097f]"),                                 # 天城文
    "zh": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]"),                  # 汉字
}
# 拉丁字母语言共用字母区：区块分不出英/法/德/西，只能用英文虚词密度筛一轮（见 detect_lang）
LATIN_LANGS = {"en", "fr", "de", "es", "pt", "it", "nl", "pl", "sv", "tr", "vi", "id", "ms"}
EN_STOPWORDS = re.compile(
    r"\b(?:the|and|you|your|that|this|it|is|are|was|were|be|to|of|in|on|at|for|with|from|"
    r"have|has|had|do|does|did|not|but|they|them|we|us|he|she|his|her|our|their|there|here|"
    r"what|when|where|why|who|how|will|would|can|could|should|just|like|about|out|up|down|"
    r"all|one|know|get|got|go|going|want|need|see|think|say|said|right|okay|yeah|good|no|yes)\b",
    re.I)


def normalize_lang(code: str | None) -> str:
    """3 字母 / 带地区码 / 大小写不一的语言标签 → 统一的语言码。

    只对一个语言保留区分度：繁体中文（zh-TW / zh-HK / zh-Hant / cht）归成 `zh-Hant`，
    而不是并进 `zh`（=简体）——否则用户点名要繁体却拿到简体，且全程无声。
    """
    c = (code or "").strip().lower().replace("_", "-")
    if not c or c == "und":            # und = ffprobe 的"未定义"，按未知处理
        return ""
    if c in LANG_HANT:
        return "zh-Hant"
    return LANG_ALIASES.get(c, c.split("-")[0])


def lang_name(code: str) -> str:
    """给 MT 提示词用的语言名。认不出的语言直接回落到语言码本身（模型多数也能懂）。"""
    c = normalize_lang(code)
    if not c:
        return "原文"          # 源语言没判出来时的兜底措辞（不能写"英文"，那是在撒谎）
    return LANG_NAMES.get(c, code)


def parse_langs(spec: str) -> tuple[str, ...]:
    """`en,zh` / `en zh` → ('en','zh')。校验非空、无重复。"""
    out = [normalize_lang(x) for x in re.split(r"[,\s]+", spec or "") if x.strip()]
    out = [c for c in out if c]
    if not out:
        raise SystemExit(f"语言对解析为空：{spec!r}")
    dup = {c for c in out if out.count(c) > 1}
    if dup:
        raise SystemExit(f"语言对里有重复语言：{'、'.join(sorted(dup))}（{spec!r}）")
    return tuple(out)


def looks_like_lang(text: str, lang: str) -> bool:
    """粗判"这段文本是不是已经是该语言"。

    只用于两件事：① 决定某个语言是否可以直接复用原文；② 判断译文是否漏译。

    判据分两层：目标语言有**独占文字区**（韩/日/俄/阿/希/泰/天城文/汉字）就看该文字是否出现；
    拉丁字母语言（英/法/德/西…）则要求"有拉丁字母且没有其他文字区"。
    旧版只认 zh/en，于是 `--subtitles en,ja` 这类非中英目标下**每条译文都被判成漏译**：
    白跑两轮补译，真漏译还修不好（补译结果被同一条判据丢弃）。
    """
    t = text or ""
    base = normalize_lang(lang).split("-")[0]
    if base in SCRIPT_LANGS:
        if base == "ja":
            # 日文必然混假名：只有汉字时更可能是中文，不认作日文
            return bool(SCRIPT_LANGS["ja"].search(t))
        if base == "zh":
            return bool(SCRIPT_LANGS["zh"].search(t)) and not SCRIPT_LANGS["ja"].search(t)
        return bool(SCRIPT_LANGS[base].search(t))
    if base in LATIN_LANGS:
        return bool(re.search(r"[A-Za-z]", t)) and not any(p.search(t) for p in SCRIPT_LANGS.values())
    return False


# "这条不是空/纯符号"的判据：任意文字或数字都算有内容（韩/俄/阿/泰/纯假名日文全算）。
# 旧版写死 [0-9A-Za-z\u4e00-\u9fff]，把**非拉丁非汉字**的字幕整片当符号丢掉 ——
# 韩语 ASR 会直接"没有切出任何字幕条"退出，任意语言的支持在三条来源路径上全部失效。
HAS_CONTENT_CHAR = re.compile(r"[^\W_]", re.UNICODE)
# 只要"字"、不含数字：用于判断"这句值不值得翻译/补译"（纯号码行不必补译）
HAS_WORD_CHAR = re.compile(r"[^\W\d_]", re.UNICODE)


def detect_lang(texts: list[str]) -> tuple[str, bool]:
    """源语言兜底判定 → (语言码, 是否可信)。只在 --source-lang auto 且轨道没标签时用。

    **可信** = 由独占文字区判定（韩/日/俄/阿/希/泰/天城文/汉字），区块不会骗人。
    拉丁字母区无法由区块区分英/法/德/西（共用字母），只能用英文虚词密度筛一轮：
      · 像英文 → ("en", True)
      · 明显不像 → ("", False) —— 语言对里**每种语言都会被真正翻译**。返回空串是刻意的：
        旧版把一切非中日文本都判成 en，于是法文视频的"英文行"直接就是法文原文（实测证伪了
        "任意语言都能出中英"这句话），而且没有任何提示。
    样本太短（<30 词）时判不出外语，按英文放行——少花一次翻译钱，且短样本影响面极小。
    """
    sample = " ".join(t for t in texts if t)[:4000]
    if not sample:
        return "", False
    for code in ("ko", "ja", "ru", "ar", "he", "el", "th", "hi"):
        if SCRIPT_LANGS[code].search(sample):
            return code, True
    if SCRIPT_LANGS["zh"].search(sample):
        return "zh", True
    words = re.findall(r"[A-Za-z][A-Za-z']*", sample)
    if len(words) < 30:
        return "en", True
    density = len(EN_STOPWORDS.findall(sample)) / len(words)
    return ("en", True) if density >= 0.12 else ("", False)


def find_bl() -> str:
    """定位 bailian CLI（跨平台，复用 find_tool 的"PATH 优先 + 平台兜底"）。

    Windows 上 npm -g 装出来的是 `bl.cmd`：CreateProcess 不能直接执行 .cmd，
    必须过 `cmd /c`（见 `_bl_argv`）。
    """
    exe = find_tool("bl", ("bl.cmd", "bl.bat", "bl.exe"))
    if exe != "bl":
        return exe
    home = Path.home()
    appdata = os.environ.get("APPDATA")
    node_roots = [home / ".local/share/fnm/node-versions"]
    candidates: list[Path] = []
    if os.name == "nt" and appdata:
        candidates += [Path(appdata) / "npm" / "bl.cmd", Path(appdata) / "npm" / "bl"]
        node_roots.append(Path(appdata) / "fnm" / "node-versions")
    candidates.append(home / ".local/bin/bl")
    for root in node_roots:
        if root.exists():
            candidates += sorted(root.glob("*/installation/bin/bl"))
    for cand in candidates:
        if Path(cand).exists():
            return str(cand)
    raise SystemExit("找不到 bl（bailian-cli）：npm install -g bailian-cli（见 ASR-API.md）")


def _bl_argv(exe: str) -> list[str]:
    """Windows 的 .cmd/.bat shim 要先过 cmd /c，POSIX 下原样返回。"""
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe]
    return [exe]


def tool_env() -> dict:
    """给子进程用的 PATH：前置 ffmpeg/bl 所在目录与常见全局 bin。

    DSH 里 bash 子进程的 PATH 可能只有 /usr/bin:/bin:/usr/sbin:/sbin，
    而 ffsubsync 这类工具内部是按名字调 `ffmpeg` 的 —— 不前置就会静默失败。
    跨平台三点：分隔符用 os.pathsep（Windows 是 `;`）、Windows 补 %APPDATA%\\npm、
    目录**一律从工具实际所在位置推导**（不按版本号排序猜 fnm 目录：本机装过
    v24.13.0/v24.15.0/v26.7.0，挑"最新"会把 bl 的 `#!/usr/bin/env node` 换成另一个
    node 运行时；bl 所在目录同时也放着配套的 node，跟它走才对）。
    """
    env = os.environ.copy()
    parts: list[str] = []
    ffmpeg_dir = os.path.dirname(FFMPEG)
    if ffmpeg_dir:
        parts.append(ffmpeg_dir)
    parts.append(str(Path.home() / ".local/bin"))
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        parts.append(str(Path(appdata) / "npm"))
    try:
        parts.append(str(Path(find_bl()).parent))     # bl 与配套 node 同目录
    except SystemExit:
        pass
    env["PATH"] = os.pathsep.join(parts + [env.get("PATH", "")])
    return env


def default_cache_root() -> Path:
    """中间产物的默认缓存根目录（按平台惯例）。

    为什么不全写在视频目录：那些 `.source.srt` / `.source.json` / `.asr.json` / `.zh.json`
    都是中间产物，用户真正要的只有 `<基名>.ass` 一个文件（2026-09-27 明确要求）。
    放在这里既能"重切不重付"，又不污染片库目录。
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "omnisub" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/omnisub"
    return Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")) / "omnisub"


def bl_env() -> dict:
    """bl 专用环境（等价于 tool_env，保留名字是因为调用点多）。"""
    return tool_env()


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
    fields = re.split(r"[:]", text.strip())
    if len(fields) == 2:                    # WebVTT 允许省略小时位：MM:SS,mmm
        h, m, rest = "0", fields[0], fields[1]
    elif len(fields) == 3:
        h, m, rest = fields
    else:
        raise ValueError(f"无法解析时间戳：{text!r}")
    s, _, ms = rest.replace(".", ",").partition(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int((ms or "0").ljust(3, "0")[:3])


# ---------------- 1a. 内嵌字幕轨（ffprobe 探轨 + ffmpeg 抽取）----------------
def ffprobe_json(video: Path) -> dict:
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-probesize", "20M", "-analyzeduration", "20M", "-show_entries",
         "stream=index,codec_type,codec_name,width,height:stream_tags=language,title:format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True, env=tool_env())
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
        want = normalize_lang(prefer_lang)
        if want:
            # 两边都归一：轨标签常是 ISO639-2（chi/zho/eng），而用户写的是 zh/en
            track = next((t for t in pool if normalize_lang(t["lang"]) == want), None)
        track = track or pool[0]
    if not track["text_based"]:
        raise SystemExit(f"字幕轨 idx={track['index']} 是图形字幕（{track['codec']}），需要先 OCR")
    with tempfile.TemporaryDirectory(prefix="omnisub-sub-") as tmp:
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
def looks_bilingual(path: Path) -> bool:
    """判断一份字幕是不是"我们自己产出的双语成品"（含大量中文）。

    必须防住：成品 <视频基名>.ass/.srt 就落在视频目录，正是 sidecar 的首个命中路径；
    不防就会把成品当原文回读（再把中文翻一遍）、或覆盖掉真正的英文外挂字幕。
    """
    try:
        cues = read_ass(path) if path.suffix.lower() in (".ass", ".ssa") else read_srt(path)
    except (OSError, ValueError):
        return False
    if not cues:
        return False
    zh = sum(1 for c in cues if re.search(r"[\u4e00-\u9fff]", c["text"]))
    return zh / len(cues) > 0.3


def has_own_mark(path: Path) -> bool:
    """读文件头认"这是我们自己产出的成品"。

    成品名就是 <视频基名>，与"用户放的外挂字幕"同名同扩展名，靠文件名分不出来，
    所以在 ASS 头部写产出标记（Title/注释行）当指纹。只读头 2KB，避免整片读盘。
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return ASS_MARK in fh.read(2048)
    except OSError:
        return False


def _is_own_artifact(cand: Path, video: Path) -> bool:
    """`<基名>.source.srt` / `<基名>.mono.ass` 是本技能自己写出的中间产物，不是"别人的外挂字幕"。

    旧版本把它们写在视频目录里（现在默认落缓存目录，但片库里可能还留着历史文件），
    且是纯英文 → 能绕过 looks_bilingual 守卫，被 sidecar 兜底 glob（`<基名>*`）
    当成外挂字幕读回来。后果实测：修好切分器后重跑，--refresh-source 与 --asr-json 全被
    架空，成品仍是旧的无标点文本 —— 必须显式排除。

    成品本身（<基名>.ass/.srt，名字与视频同名）靠产出标记识别，见 has_own_mark()。
    """
    if cand.suffix.lower() not in SIDECAR_EXTS:
        return False
    if cand.stem in (f"{video.stem}{SOURCE_SUFFIX}", f"{video.stem}{MONO_SUFFIX}"):
        return True
    return has_own_mark(cand)


def sidecar_path(video: Path) -> Path | None:
    for ext in SIDECAR_EXTS:
        exact = video.with_suffix(ext)
        if exact.exists():
            if looks_bilingual(exact):
                print(f"[src] 跳过 {exact.name}：它看起来是双语成品（不是原文）", flush=True)
                continue
            return exact
    stem = video.stem
    for cand in sorted(video.parent.glob(f"{stem}*")):
        if cand.suffix.lower() in SIDECAR_EXTS and cand != video:
            if _is_own_artifact(cand, video):
                print(f"[src] 跳过 {cand.name}：本技能自己的中间产物（原文缓存），不是外挂字幕", flush=True)
                continue
            if looks_bilingual(cand):        # 兜底路径同样要防"吃自己的产出"
                print(f"[src] 跳过 {cand.name}：它看起来是双语成品（不是原文）", flush=True)
                continue
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


# ---------------- 1d. 同步校验（ffsubsync）----------------
def find_ffsubsync() -> list[str] | None:
    """优先用 PATH 里的 ffsubsync；没有就用 uv 临时环境跑（本机 uv 在 ~/.local/bin）。"""
    exe = shutil.which("ffsubsync")
    if exe:
        return [exe]
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    if Path(uv).exists():
        return [uv, "run", "--quiet", "--with", "ffsubsync", "ffsubsync"]
    return None


def verify_sync(video: Path, source_srt: Path, work_dir: Path) -> dict | None:
    """用音频 VAD + FFT 对齐校验字幕是否真的对得上视频。

    实测（本机，43.8 分钟 1080p）：正对照（内嵌字幕）报 offset 0.000 / scale 1.000；
    反证（人为把字幕挪 +3.5s）报 offset -3.500 —— 说明这道闸门真能抓偏移。
    偏了就用 ffsubsync 自己的输出替换原文（不自己算偏移，避免符号/拉伸算错）。
    """
    cmd = find_ffsubsync()
    if cmd is None:
        print("[sync] 未找到 ffsubsync（PATH 无、uv 也缺），跳过校验", file=sys.stderr)
        return None
    fixed = work_dir / "synced.srt"
    proc = subprocess.run(cmd + [str(video), "-i", str(source_srt), "-o", str(fixed), "--gss"],
                          capture_output=True, text=True, env=tool_env())
    m_off = re.search(r"offset seconds:\s*(-?[\d.]+)", proc.stdout + proc.stderr)
    m_scale = re.search(r"framerate scale factor:\s*([\d.]+)", proc.stdout + proc.stderr)
    if not m_off or not fixed.exists():
        detail = (proc.stderr or proc.stdout or "")[-300:].replace("\n", " ")
        print(f"[sync] 校验未完成（exit={proc.returncode}）：{detail}", file=sys.stderr)
        return None
    offset = float(m_off.group(1))
    scale = float(m_scale.group(1)) if m_scale else 1.0
    info = {"offset": offset, "scale": scale, "fixed": False}
    if abs(offset) > 0.3 or abs(scale - 1.0) > 0.002:
        shutil.copy2(fixed, source_srt)
        info["fixed"] = True
        print(f"[sync] ⚠️ 字幕与音轨不同步（offset {offset:+.3f}s、scale {scale:.4f}）→ 已用 ffsubsync 校正", flush=True)
    else:
        print(f"[sync] ✅ 同步正常（offset {offset:+.3f}s、scale {scale:.4f}）", flush=True)
    try:
        fixed.unlink()          # 临时产物，别在输出目录里留 synced.srt
    except OSError:
        pass
    return info


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
    """lang 传 auto/空 → 不给语言提示，由模型自行判定（任意语言场景的默认行为）。

    `--language` 在 bl 里是**语言提示**而非必填项；给的提示错了反而会伤识别率，
    所以 auto 时宁可不给，让用户按需显式指定。
    """
    hint = "" if (lang or "").strip().lower() in ("", "auto") else normalize_lang(lang)
    argv = ["speech", "recognize", "--url", str(audio), "--model", model,
            "--out", str(out_json), "--output", "json",
            "--api-key", key, "--timeout", str(ASR_TIMEOUT)]
    if hint:
        argv += ["--language", hint]
    print(f"[asr] {model} ← {audio.name}"
          + (f"（语言提示 {hint}）" if hint else "（不给语言提示，自动判定）"), flush=True)
    proc = subprocess.run(
        _bl_argv(find_bl()) + argv,
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


def _canonical_text(text: str) -> str:
    """句级 text 才是可靠文本（标点、空格、词形都对），压缩空白后返回。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def _align_words(words: list[dict], canonical: str) -> list[list[int]] | None:
    """把词对齐到句级文本，返回 [[begin_ms, end_ms, c0, c1], ...]，c0/c1 是 canonical 的字符区间。

    为什么要对齐（E08 实测，旧版两个坑都踩了）：ASR 的 words[] 两个方向都不可靠——
      拆词（须无空格相接）："gl"+"enn"、"pr"+"ou"+"der"、"9"+"0"、"er"+"ie"
      缺空格（须有空格）  ："in"+"90"、"restaurant"+"You"、"day"+"90"、"And"+"I"
    本地规则分不出这两种（旧版直接 join → "in90" 共 28 条；只补空格 → "gl enn" 更多）。
    只有句级 text 知道空格在哪：**显示文本一律取自 canonical**，words[] 只贡献时间戳——
    在 canonical 的"字母数字投影"上顺序匹配每个词，上面两种怪癖都能自然对齐。
    任一词匹配不上、区间不单调、或覆盖不足 90% 时返回 None（调用方退回比例切分）。
    """
    if not canonical or not words:
        return None
    proj = [(i, ch.lower()) for i, ch in enumerate(canonical) if ch.isalnum()]
    if not proj:
        return None
    out: list[list[int]] = []
    cursor = 0
    for word in words:
        token = [ch.lower() for ch in (word.get("text") or "") if ch.isalnum()]
        if not token:                        # 纯标点词（"'" / ","）→ 并进前一个词的区间
            if out:
                end = out[-1][3]
                while end < len(canonical) and not canonical[end].isalnum() and canonical[end] != " ":
                    end += 1
                out[-1][3] = end
            continue
        idx, hit, first = cursor, 0, None
        while idx < len(proj) and hit < len(token):
            if proj[idx][1] == token[hit]:
                if first is None:
                    first = proj[idx][0]
                hit += 1
            idx += 1
        if hit < len(token) or first is None or (out and first < out[-1][3]):
            return None
        out.append([int(word.get("begin_time") or 0), int(word.get("end_time") or 0),
                    first, proj[idx - 1][0] + 1])
        cursor = idx
    if not out:
        return None
    seen = bytearray(len(canonical))
    for span in out:
        for i in range(max(0, span[2]), min(len(canonical), span[3])):
            seen[i] = 1
    if sum(seen[i] for i, _ in proj) < 0.9 * len(proj):
        return None
    return out


def _text_windows(text: str, max_chars: int) -> list[tuple[int, int]]:
    """把整句切成 ≤max_chars 的字符窗口：先按标点小句打包，超长小句再按空格均衡硬切。

    旧版按字符数贪心硬切，句尾常剩 1–3 个词的孤儿条（"out."、"that out."），
    碎片单独送 MT 会被脑补成别的意思；按小句打包后切点基本落在标点上。
    """
    atoms: list[tuple[int, int]] = []
    for m in re.finditer(r"[^,;:.!?…]*[,;:.!?…]+|[^,;:.!?…]+$", text):
        seg = m.group()
        lead = len(seg) - len(seg.lstrip())
        trail = len(seg) - len(seg.rstrip())
        if m.end() - trail > m.start() + lead:
            atoms.append((m.start() + lead, m.end() - trail))
    if not atoms:
        atoms = [(0, len(text))]

    packed: list[tuple[int, int]] = []
    cur0 = cur1 = -1
    for a0, a1 in atoms:
        if cur0 < 0:
            cur0, cur1 = a0, a1
        elif a1 - cur0 <= max_chars:
            cur1 = a1
        else:
            packed.append((cur0, cur1)); cur0, cur1 = a0, a1
    if cur0 >= 0:
        packed.append((cur0, cur1))

    out: list[tuple[int, int]] = []
    for c0, c1 in packed:
        seg = text[c0:c1]
        if len(seg) <= max_chars:
            out.append((c0, c1))
            continue
        n = (len(seg) + max_chars - 1) // max_chars      # 单个小句仍超长 → 均衡硬切
        start = 0
        for i in range(1, n):
            ideal = len(seg) * i // n
            lo, hi = start + 1, len(seg) - 1
            # 在整个小句里找离 ideal 最近的空格（不在 ideal 附近截断，否则会切进词里）
            cuts = [p for p in range(lo, hi + 1) if seg[p] == " "]
            cut = min(cuts, key=lambda p: (abs(p - ideal), p)) if cuts else min(max(ideal, lo), hi)
            out.append((c0 + start, c0 + cut)); start = cut
        out.append((c0 + start, c1))
    return [(a, b) for a, b in out if text[a:b].strip()]


def _split_long(sent: dict, max_ms: int, max_chars: int) -> list[dict]:
    """把一条 ASR 句切成若干条字幕：文本取自句级 text，时间来自对齐后的词。

    长度预算（max_chars，≈两行）与时长预算（max_ms）双约束；切点优先落在标点上。
    """
    words = [w for w in sent["words"]
             if (w.get("text") or "").strip() or (w.get("punctuation") or "").strip()]
    canonical = _canonical_text(sent.get("text"))
    if not canonical:
        return []
    span_ms = max(1, sent["end"] - sent["begin"])
    spans = _align_words(words, canonical)
    if spans is None:
        # 回退：词层缺失或对不上 → 只按文本切，时间在句内按**字符数比例**摊
        # （旧版按条数均摊且不看时长预算，慢语速/长停顿会切出 20 s 的长条）
        n_time = max(1, (span_ms + max_ms - 1) // max_ms)
        eff = max(20, len(canonical) // n_time)
        chunks = [(canonical[a:b].strip(), b - a)
                  for a, b in _text_windows(canonical, min(max_chars, eff))]
        chunks = [(t, w) for t, w in chunks if t]
        if not chunks:
            return []
        total_chars = sum(w for _, w in chunks) or 1
        out, acc = [], 0
        for text, width in chunks:
            begin = sent["begin"] + span_ms * acc // total_chars
            acc += width
            out.append({"begin": begin, "end": sent["begin"] + span_ms * acc // total_chars,
                        "text": text, "words": []})
        return out

    pieces: list[dict] = []
    for c0, c1 in _text_windows(canonical, max_chars):
        inner = [s for s in spans if s[2] < c1 and s[3] > c0]
        if not inner:
            continue
        begin, end = inner[0][0], inner[-1][1]
        if end - begin <= max_ms:                    # 时长也在预算内 → 一条
            pieces.append({"begin": begin, "end": end, "text": canonical[c0:c1].strip()})
            continue
        # 慢语速/长停顿：窗口时长超标 → 用词再按时长均分
        groups: list[list[list[int]]] = [[inner[0]]]
        for span in inner[1:]:
            # 切点必须落在"空格之后"的词首：ASR 会把 don't 拆成 don/'/t，
            # 允许在 t 处切就会得到 "I don'" ‖ "t know what to say."
            at_word_start = span[2] == 0 or canonical[span[2] - 1] == " "
            if span[1] - groups[-1][0][0] > max_ms and at_word_start:
                groups.append([span])
            else:
                groups[-1].append(span)
        bounds = [c0] + [g[0][2] for g in groups[1:]] + [c1]
        for i, group in enumerate(groups):
            text = canonical[bounds[i]:bounds[i + 1]].strip()
            if text:
                pieces.append({"begin": group[0][0], "end": group[-1][1], "text": text})
    return pieces


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
        if not text or not HAS_CONTENT_CHAR.search(text):
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
            cap = nxt["begin"] - MIN_GAP_MS
            # 优先不重叠：只要还留得住 MIN_READABLE_MS 的可读时长，就压到下一句开始之前。
            # 旧版写 max(begin+MIN_CUE_MS, cap)，下一句紧贴时会反推出 120–360ms 重叠
            #（E08 实测 3 处：条 46/340/510）。
            cue["end"] = cap if cap >= cue["begin"] + MIN_READABLE_MS else cue["begin"] + MIN_CUE_MS
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
def _translate_local(batch: list[str], server: str, timeout: int,
                     src_lang: str, tgt_lang: str) -> list[str] | None:
    """本地后端：llama.cpp / mlx-lm 的 OpenAI 兼容 /v1/chat/completions（Hy-MT2 官方分隔符模板）。"""
    prompt = local_prompt(src_lang, tgt_lang) + DELIM.join(batch)
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


def _cloud_payload(batch: list[str], chat_model: str, system: str,
                   src_lang: str, tgt_lang: str) -> list[dict]:
    """构造请求体。qwen-mt-* 只吃 user/assistant（带 system 会 400）。

    翻译方向由 src_lang/tgt_lang 决定 —— 旧版把"把英文翻译成简体中文"写死在提示词里，
    于是 --target-lang en 完全失效（拿中文当输入也照样要求译成中文），
    实测产出是"中文原样重复两遍"的假双语。方向必须进提示词，且要与语言对一致。
    """
    if not chat_model.startswith("qwen-mt"):
        return [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"lines": batch}, ensure_ascii=False)}]
    src, tgt = lang_name(src_lang), lang_name(tgt_lang)
    if len(batch) == 1:
        return [{"role": "user", "content": f"把下面这句{src}翻译成{tgt}，只输出译文：\n" + batch[0]}]
    # 编号标记协议：模型偶尔把一句拆成两条（实测 20 条回 23/25 条），二分永远不收敛；
    # 带 [[n]] 标记就能把拆出来的片段按标记归位，一次请求拿全，不用反复二分。
    marked = "\n".join(f"[[{i + 1}]] {x}" for i, x in enumerate(batch))
    return [{"role": "user", "content":
             f"把下面每一行{src}翻译成{tgt}。必须原样保留每行开头的编号标记 [[n]]，"
             "一个标记对应一条译文，不要合并或拆分编号：\n" + marked}]


def _ask_cloud(batch: list[str], key: str, chat_model: str, system: str,
               timeout: int = TRANSLATE_TIMEOUT,
               src_lang: str = "en", tgt_lang: str = "zh") -> list[str] | None:
    """一次云端请求。返回 None = 失败或条数不符（交由上层二分），绝不猜测对齐关系。"""
    payload = _cloud_payload(batch, chat_model, system, src_lang, tgt_lang)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        msg_file = fh.name
    try:
        raw = ""
        for attempt in range(1, TRANSLATE_ATTEMPTS + 1):
            proc = subprocess.run(
                _bl_argv(find_bl()) + ["text", "chat", "--model", chat_model,
                                       "--messages-file", msg_file, "--api-key", key,
                                       "--output", "json", "--quiet", "--timeout", str(timeout)],
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
            # 记录真实原因：旧日志一律写"只回 0 条"，看着像请求失败，其实是模型合并了短句、
            # 少回一个标记（E08 实测约 1/4 的批发生，二分后全部补齐）
            missing = [i + 1 for i in range(len(batch)) if i + 1 not in slots]
            print(f"[mt] 标记不全（{len(batch)} 条缺 {len(missing)} 个：{missing[:5]}）→ 二分", file=sys.stderr)
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
                     local_server: str = LOCAL_SERVER_DEFAULT,
                     src_lang: str = "en", tgt_lang: str = "zh") -> list[str]:
    """一批 → 译文列表，长度恒等于输入。

    条数不符就二分（递归到单行），**绝不末尾补空**——补空等于把整批译文按错位映射出去。
    """
    if not batch:
        return []
    if _LIMITER is not None and backend != "local":
        _LIMITER.wait()
    out = (_translate_local(batch, local_server, timeout, src_lang, tgt_lang) if backend == "local"
           else _ask_cloud(batch, key, chat_model, system, timeout, src_lang, tgt_lang))
    if out is not None and len(out) == len(batch):
        return out
    if len(batch) == 1:
        print(f"[mt] 单行仍失败：{batch[0][:40]!r}", file=sys.stderr)
        return [out[0] if out else ""]
    mid = len(batch) // 2
    print(f"[mt] {len(batch)} 条只回 {len(out) if out else 0} 条 → 二分", file=sys.stderr)
    kw = dict(key=key, chat_model=chat_model, system=system, timeout=timeout, backend=backend,
              local_server=local_server, src_lang=src_lang, tgt_lang=tgt_lang)
    return _translate_batch(batch[:mid], **kw) + _translate_batch(batch[mid:], **kw)


def missing_indices(lines: list[str], translations: list[str], tgt_lang: str) -> list[int]:
    """哪些行没译成目标语言（漏条 / 原样回吐）——补译与补译后复核共用同一判据。

    判据必须**跟着目标语言走**：旧版写死"译文里没有汉字就是漏译"，在 zh→en 方向下
    每条正确的英文译文都不含汉字 → 全片被判漏译，白跑两轮补译，而补译结果又因同一条
    汉字判据被丢弃。纯符号行（♪♪♪）不算可翻译行，避免无谓补译。
    """
    return [i for i, t in enumerate(translations)
            if HAS_WORD_CHAR.search(lines[i] or "") and not looks_like_lang(t or "", tgt_lang)]


def repair_missing(lines: list[str], translations: list[str], key: str, chat_model: str,
                   system: str, timeout: int, backend: str, local_server: str,
                   src_lang: str = "en", tgt_lang: str = "zh", rounds: int = 2) -> list[str]:
    """成批补译（每批 10 条，最多 rounds 轮），不逐行。

    漏译判据必须**跟着目标语言走**：旧版写死"译文里没有汉字就是漏译"，在 zh→en 方向下
    每条正确的英文译文都不含汉字 → 全片被判为漏译，白跑两轮补译（数百次请求），
    而补译结果又因同一条汉字判据被丢弃 —— 纯烧钱且永不收敛。
    """
    for rnd in range(1, rounds + 1):
        bad = missing_indices(lines, translations, tgt_lang)
        if not bad:
            return translations
        print(f"[mt] 第 {rnd} 轮补译：{len(bad)} 条", flush=True)
        for start in range(0, len(bad), 10):
            idx = bad[start:start + 10]
            fixed = _translate_batch([lines[i] for i in idx], key, chat_model, system,
                                     timeout, backend, local_server, src_lang, tgt_lang)
            for i, t in zip(idx, fixed):
                if looks_like_lang(t or "", tgt_lang):
                    translations[i] = t
    return translations


def system_for(src_lang: str, tgt_lang: str) -> str:
    """翻译系统提示词（translate 与 repair_missing 必须用同一份，否则补译风格会漂）。

    只有非 qwen-mt 模型会真正收到它 —— qwen-mt-* 不吃 system 角色，方向靠 _cloud_payload
    的用户消息承载。
    """
    return (f"You are a professional subtitle translator. Translate each {lang_name(src_lang)} line "
            f"into {lang_name(tgt_lang)}. Keep the same order and count. "
            f"Output ONLY a JSON array of strings.")


def translate(lines: list[str], key: str, chat_model: str, src_lang: str, tgt_lang: str,
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

    system = system_for(src_lang, tgt_lang)
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
                                   backend, local_server, src_lang, tgt_lang): i for i in todo}
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
def write_source_srt(cues: list[dict], path: Path) -> None:
    """写原文缓存 `<基名>.source.srt` —— 中间产物，不是交付物。

    交付物是 .ass（见 write_ass）。这份 SRT 只服务两件事：源字幕复用（省掉整片读盘）
    与 ffsubsync 同步校验。因此**不做换行包装**：ASS 侧由 libass 按画面宽度自动折行，
    这里预先硬折只会把多余的换行带进成品。
    """
    blocks = [
        f"{i}\n{ts(cue['begin'])} --> {ts(cue['end'])}\n{cue['text'].strip()}"
        for i, cue in enumerate(cues, 1)
    ]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8", newline="\n")
    print(f"[srt] {len(cues)} 条 → {path}", flush=True)


# ---------------- 3b. 写 ASS（唯一交付物）----------------
# 为什么不能再出 SRT：SubRip 只有序号/时间轴/纯文本，**格式本身没有任何样式位**，
# 字号、颜色、加粗、描边都无处安放；往 SRT 里塞 <font color> 是播放器私有行为、
# 支持参差不齐，不能当交付标准。要"上面的字幕更醒目"就只能出 ASS。
ASS_MARK = "omnisub"                # 产出标记：把自己产出的成品从"外挂字幕"候选里排除
ASS_FONT_DEFAULT = "PingFang SC"    # 中文首选；缺字体时 libass 按系统默认回落
# 「上面的字幕更醒目」的落地（方案 A 高对比白系）：
#   Upper = 第一行（在上）：纯白 #FFFFFF、字号更大、加粗、描边更粗 → 醒目
#   Lower = 其余行（在下）：暖白 #F0EDE6（BGR 即 E6EDF0）、字号略小、常规字重 → 退到背景
# 语言对里谁在上由 --subtitles 的顺序决定，样式只认"位置"，不认具体语言。
# (样式名, 主色 &HAABBGGRR, 字号/画面高, 粗体开关, 描边/画面高)
ASS_STYLES = (
    ("Upper", "&H00FFFFFF", 0.0500, -1, 0.0028),
    ("Lower", "&H00E6EDF0", 0.0435, 0, 0.0022),
)


def ass_time(ms: int) -> str:
    """ASS 时间戳：H:MM:SS.cc（厘秒）。"""
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, cs = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{cs // 10:02d}"


def ass_escape(text: str) -> str:
    """ASS 正文转义。

    花括号是覆盖标签的定界符（正文里的 `{` 会被当成标签头），反斜杠是转义符。
    顺序有讲究：**先转义反斜杠、再包花括号**，反过来的话自己加的那批反斜杠会被二次转义。
    """
    t = (text or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return "\\N".join(line.strip() for line in t.splitlines() if line.strip())


def write_ass(cues: list[dict], path: Path, lines_by_cue: list[list[str]],
              width: int = 1920, height: int = 1080) -> None:
    """写双语 ASS：每条的 N 行放进**同一个 Dialogue 事件**，用 \\N 换行、\\r 按行切样式。

    为什么不用"每行一个事件 + 各自 MarginV"：那要手算两行的行高差，字号或分辨率一变就错位，
    而本项目的字号是按画面高度比例算的，换片就得重算。单事件方案里两行天然是一个整体，
    居中堆叠与底部定位全交给 libass，换字号/换分辨率都不会散。
    行序 = lines_by_cue 里的顺序：第一条在最上、用 Upper 样式，其余用 Lower。
    """
    w = max(320, int(width or 1920))
    h = max(240, int(height or 1080))
    sizes = [max(12, round(h * ratio)) for _, _, ratio, _, _ in ASS_STYLES]
    outlines = [max(1, round(h * ratio)) for _, _, _, _, ratio in ASS_STYLES]
    margin_v = max(10, round(h * 0.030))

    lines: list[str] = [
        "[Script Info]",
        f"; Generated by {ASS_MARK}",
        f"Title: {ASS_MARK}",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    for (name, colour, _, bold, _), size, outline in zip(ASS_STYLES, sizes, outlines):
        # Alignment=2（底部居中）；两个样式用同一个 MarginV，单事件里才不会互相打架
        lines.append(f"Style: {name},{ASS_FONT_DEFAULT},{size},{colour},{colour},&H00000000,"
                     f"&H80000000,{bold},0,0,0,100,100,0,0,1,{outline},1,2,40,40,{margin_v},1")
    lines += ["", "[Events]",
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    rendered = 0
    width_rows = 0
    for i, cue in enumerate(cues):
        rows = [r for r in (lines_by_cue[i] if i < len(lines_by_cue) else []) if (r or "").strip()]
        if not rows:
            continue
        width_rows = max(width_rows, len(rows))
        body = "\\N".join(
            f"{{\\r{ASS_STYLES[min(j, len(ASS_STYLES) - 1)][0]}}}{ass_escape(text)}"
            for j, text in enumerate(rows))
        lines.append(f"Dialogue: 0,{ass_time(cue['begin'])},{ass_time(cue['end'])},"
                     f"{ASS_STYLES[0][0]},,0,0,0,,{body}")
        rendered += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"[ass] {rendered} 条 × 最多 {width_rows} 行 → {path}", flush=True)


T0 = time.time()
T = {"probe": 0.0, "audio": 0.0, "asr": 0.0, "mt": 0.0, "write": 0.0}


def check_timeline(cues: list[dict], video: Path, partial: bool = False) -> None:
    """对齐自检：覆盖率 / 大空隙 / 重叠。这是"字幕与视频一一对应"的机器判据。

    ASR 或外挂字幕都可能只覆盖片段，人工看不出来；这里直接给结论并落进日志。
    """
    try:
        duration = float((ffprobe_json(video).get("format") or {}).get("duration") or 0)
    except (SystemExit, ValueError, TypeError):
        return
    if duration <= 0:
        return
    if not cues:
        print("[check] 对齐自检：没有切出任何字幕条", flush=True)
        return
    last_end = cues[-1]["end"] / 1000.0
    tail_gap = duration - last_end
    gaps = [b["begin"] - a["end"] for a, b in zip(cues, cues[1:])]
    max_gap = max(gaps) if gaps else 0.0      # 只有 1 条字幕时 gaps 为空（旧版 max() 直接 ValueError）
    big = [g for g in gaps if g > 20000]
    overlaps = [(a, b) for a, b in zip(cues, cues[1:]) if a["end"] > b["begin"] + 1]
    cover = last_end / duration * 100
    flags = []
    if tail_gap > max(30, duration * 0.02) and not partial:   # --limit 小样不该刷覆盖率告警
        flags.append(f"末条到片尾还差 {tail_gap:.0f}s")
    elif last_end > duration * 1.02 and not partial:
        flags.append(f"字幕超出片尾 {last_end - duration:.0f}s（可能拿错集/版本不符）")
    if big and not partial:
        flags.append(f"{len(big)} 处 >20s 空隙")
    if overlaps:
        flags.append(f"{len(overlaps)} 处重叠")
    state = ("（小样，跳过覆盖率判定）" if partial and not flags
             else "⚠️ " + "；".join(flags) if flags else "✅ 覆盖率/空隙/重叠正常")
    print(f"[check] 对齐自检：覆盖到 {cover:.1f}%（末条 {last_end:.1f}s / 片长 {duration:.1f}s）、"
          f"最大空隙 {max_gap / 1000:.1f}s、重叠 {len(overlaps)} 处 → {state}", flush=True)


def report_timing(stem: str, cues: list[dict], origin: str, write_log: bool = True) -> None:
    """打印阶段耗时；默认追加到 ~/.dsh/omnisub-timing.log（评测/CI 用 --no-log 关掉，别污染宿主状态）。"""
    total = time.time() - T0

    def _fmt(x: float) -> str:
        return f"{x:.1f}"

    print(f"【耗时】探测 {_fmt(T['probe'])}s · 抽音轨 {_fmt(T['audio'])}s · 转写 {_fmt(T['asr'])}s · "
          f"翻译 {_fmt(T['mt'])}s · 写盘 {_fmt(T['write'])}s · 总计 {_fmt(total)}s"
          f"（{len(cues)} 条，{origin}）", flush=True)
    if not write_log:
        return
    try:
        log = Path.home() / ".dsh/omnisub-timing.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\t".join([
                time.strftime("%Y-%m-%d %H:%M:%S"), f"local:{stem[:40]}",
                _fmt(T["probe"]), _fmt(T["audio"]), _fmt(T["asr"]), _fmt(T["mt"]),
                _fmt(T["write"]), _fmt(total), str(len(cues)), str(sum(len(c["text"]) for c in cues)),
            ]) + "\n")
    except OSError as exc:
        print(f"[timing] 写日志失败：{exc}", file=sys.stderr)


def resolve_source_lang(explicit: str, track_lang: str, texts: list[str]) -> tuple[str, str]:
    """源语言三级回退：显式参数 → 字幕轨语言标签 → 文本启发式。返回 (语言码, 依据)。

    为什么不直接信 ASP/ASR 的返回：`bl speech recognize` 的 JSON 里**没有识别到的语言**
    （实测只有 file_url/properties/transcripts/usage 四个键），内嵌轨又常常不打语言标签
    （实测本机某片 4 条国语音轨全有 chi 标签，而另一部片的字幕轨没有），
    所以只能逐级回退，并把"依据"打出来让用户能判断该不该显式指定。
    """
    want = (explicit or "").strip().lower()
    if want and want != "auto":
        return normalize_lang(explicit), "显式 --source-lang"
    code = normalize_lang(track_lang)
    if code:
        return code, f"字幕轨语言标签 {track_lang}"
    code, confident = detect_lang(texts)
    if not code:
        return "", ("文本看着不是英文、又判不出具体语种 → 按未知处理：语言对里每种语言都会真翻译"
                    "（要更准就别用 auto，显式传 --source-lang <语种>）")
    return code, f"文本启发式自动判定 → {code}（要更准可显式传 --source-lang）"


def video_size(video: Path) -> tuple[int, int]:
    """画面尺寸 → ASS 的 PlayResX/Y（字号与描边都按它换算）。取不到就退回 1920x1080。"""
    try:
        data = ffprobe_json(video)
    except (SystemExit, ValueError, TypeError):
        return 1920, 1080
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and stream.get("width") and stream.get("height"):
            return int(stream["width"]), int(stream["height"])
    return 1920, 1080


def translate_for_target(lines: list[str], key: str, src_lang: str, tgt_lang: str,
                         chat_model: str, cache_path: Path, fingerprint: str, *,
                         workers: int, batch_size: int, timeout: int, backend: str,
                         local_server: str, rpm: int, no_cache: bool) -> list[str]:
    """一个目标语言的全部字幕行：缓存命中就用缓存，否则翻译 → 补译 → 落缓存。

    每个目标语言一份缓存（<基名>.<语言>.json），互不覆盖：同一部片既出 en 又出 zh 时，
    两边的翻译进度、计费与重跑都是独立的。
    """
    def dump(payload: list[str]) -> None:
        cache_path.write_text(json.dumps(
            {"fingerprint": fingerprint, "count": len(lines), "model": chat_model,
             "src_lang": src_lang, "target_lang": tgt_lang, "backend": backend,
             "translations": payload}, ensure_ascii=False), encoding="utf-8")

    translations: list[str] | None = None
    if cache_path.exists() and not no_cache:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            same_input = (cached.get("count") == len(lines)
                          and cached.get("fingerprint") == fingerprint
                          and len(cached.get("translations") or []) == len(lines))
            # src_lang 必须显式相等：旧缓存没有这个键，若按"缺省即相同"放行，
            # 同一份文本在 zh→en 与 en→zh 之间会互相串用（实测 E33 就撞上过这个坑）
            same_engine = (cached.get("model") == chat_model
                           and cached.get("backend", "cloud") == backend
                           and cached.get("src_lang") == src_lang)
            if same_input and same_engine:
                translations = [str(x) for x in cached["translations"]]
                print(f"[mt] {tgt_lang}：复用译文缓存 {cache_path.name}"
                      f"（{len(translations)} 条，{chat_model}）", flush=True)
            elif same_input:
                print(f"[mt] {tgt_lang}：缓存来自 {cached.get('model')}/"
                      f"{cached.get('backend') or 'cloud'}/{cached.get('src_lang') or '?'}，"
                      f"与本次 {chat_model}/{backend}/{src_lang} 不符 → 重新翻译", flush=True)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"[mt] {tgt_lang}：缓存损坏（{type(exc).__name__}）→ 忽略并重译：{cache_path.name}",
                  file=sys.stderr)

    if translations is None:
        translations = translate(lines, key, chat_model, src_lang, tgt_lang,
                                 workers=workers, batch_size=batch_size, timeout=timeout,
                                 partial_path=cache_path, fingerprint=fingerprint,
                                 backend=backend, local_server=local_server, rpm=rpm)
        dump(translations)
        print(f"[mt] {tgt_lang}：译文已缓存 → {cache_path.name}（重切/重跑不再重复付费）", flush=True)

    # 补译校验：漏条/回原文的行成批补译（不逐行，避免 429 与慢）
    if missing_indices(lines, translations, tgt_lang):
        before = list(translations)
        translations = repair_missing(lines, translations, key, chat_model,
                                      system_for(src_lang, tgt_lang), timeout, backend,
                                      local_server, src_lang, tgt_lang)
        if translations != before:
            dump(translations)
    left = missing_indices(lines, translations, tgt_lang)
    if left:
        print(f"[mt] {tgt_lang}：补译后仍有 {len(left)} 条不像 {lang_name(tgt_lang)}，已如实保留",
              file=sys.stderr)
    return translations


def main() -> None:
    ap = argparse.ArgumentParser(
        description="视频 → 任意语言对的双语 ASS 字幕（复用优先：内嵌字幕 > 外挂字幕 > ASR）")
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="输出目录（默认与视频同目录）")
    ap.add_argument("--cache-dir", type=Path, default=None,
                    help="中间产物目录（默认平台缓存目录，如 ~/Library/Caches/omnisub/；"
                         "视频目录只会多出 <基名>.ass 一个文件）")
    ap.add_argument("--source", choices=["auto", "embedded", "sidecar", "asr"], default="auto")
    ap.add_argument("--source-lang", default="auto",
                    help="源语言：auto=自动判定（显式参数 → 字幕轨语言标签 → 文本启发式）；"
                         "也可显式给 en/zh/ja/ko/fr…。非中英源语言建议显式指定，"
                         "ASR 语言提示与翻译方向提示词都会更准")
    ap.add_argument("--subtitles", default=None, metavar="LANG[,LANG...]",
                    help="要产出的字幕语言与行序（逗号分隔），**第一行在最上、用醒目样式**；"
                         "默认 en,zh（英上中下）。源语言若在列表中则该行直接复用原文、不翻译。"
                         "支持任意语言对与三行以上，如 ja,en,zh")
    ap.add_argument("--target-lang", default=None,
                    help="[已废弃] 单目标写法，等价于 --subtitles <源语言>,<目标语言>；请改用 --subtitles")
    ap.add_argument("--sub-index", type=int, default=None, help="指定内嵌字幕轨 index")
    ap.add_argument("--asr-model", default=ASR_MODEL_DEFAULT)
    ap.add_argument("--chat-model", default=CHAT_MODEL_DEFAULT)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--asr-json", type=Path, default=None, help="复用已有 ASR 结果（跳过解音轨与转写）")
    ap.add_argument("--no-translate", action="store_true", help="只出原文单语 ASS（不翻译）")
    ap.add_argument("--no-cache", action="store_true", help="忽略译文缓存，强制重新翻译")
    ap.add_argument("--batch", type=int, default=TRANSLATE_BATCH, help="每次翻译的条数（默认 20；实测 40 会大量触发二分反而更慢）")
    ap.add_argument("--refresh-source", action="store_true",
                    help="强制重新抽字幕（默认会复用比视频新的 .source.srt，省掉整片读盘）")
    ap.add_argument("--verify-sync", choices=["auto", "on", "off"], default="auto",
                    help="用 ffsubsync 校验字幕与音轨是否同步：auto=仅外挂字幕时校验（默认）、on=总是、off=不校验")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条字幕（小样验证用，0=全部）")
    ap.add_argument("--workers", type=int, default=4, help="翻译并发数（默认 4；受 60 次/分钟限流约束）")
    ap.add_argument("--rpm", type=int, default=REQUESTS_PER_MIN, help="每分钟最大请求数（默认 50，官方限额 60）")
    ap.add_argument("--backend", choices=["cloud", "local"], default="cloud",
                    help="翻译后端：cloud=bl text chat（默认 qwen-mt-flash）；local=本地 OpenAI 兼容服务")
    ap.add_argument("--local-server", default=LOCAL_SERVER_DEFAULT,
                    help="本地后端地址，如 llama-server --port 8080 或 mlx_lm.server")
    ap.add_argument("--timeout", type=int, default=TRANSLATE_TIMEOUT, help="单次 bl 调用超时秒数（默认 180）")
    ap.add_argument("--keep-audio", action="store_true")
    ap.add_argument("--no-log", action="store_true",
                    help="不追加 ~/.dsh/omnisub-timing.log（评测/CI 用，避免污染宿主状态）")
    args = ap.parse_args()

    video: Path = args.video.expanduser().resolve()
    if not video.exists():
        raise SystemExit(f"找不到视频：{video}")
    out_dir = (args.out or video.parent).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = video.stem

    cues: list[dict] | None = None
    origin = ""
    track_lang = ""                      # 内嵌轨的语言标签：源语言三级回退的第二级
    # 中间产物（.source.srt / .source.json / .asr.json / 译文缓存）默认落在**缓存目录**，
    # 不再堆在视频旁边——用户要的是"视频目录只多出一个 .ass"。
    # 同一视频的缓存放在 <缓存根>/<基名>-<路径哈希前 8 位>/ 下，换目录的同名视频不会互相串。
    cache_root = (args.cache_dir.expanduser().resolve() if args.cache_dir
                  else default_cache_root() / f"{stem}-{hashlib.sha1(str(video).encode()).hexdigest()[:8]}")
    cache_root.mkdir(parents=True, exist_ok=True)
    # --asr-json 是"我就要用这份转写结果"的显式指令：强制走 ASR 分支，
    # 压过缓存复用、内嵌轨与外挂字幕。否则它会被目录里的产物静默架空——
    # 实测踩过两次：一次是自家 <基名>.source.srt（绕过 looks_bilingual），
    # 一次是上一次 --no-translate 留下的单语成品。显式参数必须说了算。
    if args.asr_json is not None and args.source != "asr":
        print(f"[src] 指定 --asr-json → 强制走 ASR 分支（忽略 --source {args.source} 与目录里的中间产物）",
              flush=True)
        args.source = "asr"

    # 源字幕复用：抽内嵌字幕要把整片读一遍（2.79 GB 外置机械盘实测 22 s），
    # 上次抽过且比视频新就直接用；--refresh-source 强制重抽。
    cached_source = cache_root / f"{stem}{SOURCE_SUFFIX}.srt"
    source_meta = cache_root / f"{stem}{SOURCE_SUFFIX}.json"
    if (not args.refresh_source and args.source in ("auto", "embedded")
            and cached_source.exists() and cached_source.stat().st_mtime >= video.stat().st_mtime):
        meta = None
        try:
            meta = json.loads(source_meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = None
        cached_cues = read_srt(cached_source)
        # 守卫（失效关闭）：只有"完整抽取 + 元数据可读"才复用。
        # 元数据缺失/损坏时无从判断是否被 --limit 截断 —— 宁可贵 20 秒重抽，也不能静默少出字幕
        #（历史上 6739c59 那版在 --limit 下写截断的 source.srt 且不写 .source.json）。
        if not isinstance(meta, dict):
            print(f"[src] 缺少/损坏 {source_meta.name}，为安全起见不复用", flush=True)
        elif meta.get("limited"):
            print(f"[src] 已有的 {cached_source.name} 来自 --limit 运行，不复用", flush=True)
        elif not cached_cues:
            print(f"[src] {cached_source.name} 为空，不复用", flush=True)
        else:
            cues = cached_cues
            origin = "复用的 source.srt（跳过抽取）"
            print(f"[src] 复用 {cached_source.name}（{len(cues)} 条，比视频新，跳过整片读盘）", flush=True)
    _stage = time.time()
    if cues is None and args.source in ("auto", "embedded"):
        tracks = embedded_tracks(video)
        text_tracks = [t for t in tracks if t["text_based"]]
        if tracks:
            print(f"[probe] 字幕轨 {len(tracks)} 条：" + "; ".join(
                f"idx={t['index']} {t['codec']}/{t['lang'] or '?'} {t['title']}" for t in tracks), flush=True)
        if text_tracks:
            try:
                cues, info = extract_embedded(
                    video, args.sub_index,
                    None if args.source_lang.strip().lower() == "auto" else args.source_lang)
                if cues:
                    origin = f"内嵌字幕轨 idx={info['index']} {info['codec']}/{info.get('language','?')}"
                    track_lang = info.get("language") or ""
            except SystemExit as exc:
                if args.source == "embedded":
                    raise
                print(f"[src] 内嵌字幕不可用（{exc}）→ 继续尝试其他来源", file=sys.stderr)
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
        with tempfile.TemporaryDirectory(prefix="omnisub-") as tmp:
            if args.asr_json:
                asr_json = args.asr_json.expanduser().resolve()
                if not asr_json.exists():
                    raise SystemExit(f"找不到 --asr-json：{asr_json}")
                print(f"[asr] 复用 {asr_json.name}", flush=True)
            else:
                audio = Path(tmp) / f"{stem}.flac"
                asr_json = cache_root / f"{stem}.asr.json"
                _t0 = time.time()
                extract_audio(video, audio)
                T["audio"] = time.time() - _t0          # 真·抽音轨（ffmpeg 读盘）
                _t0 = time.time()
                transcribe(audio, asr_json, key, args.asr_model, args.source_lang)
                T["asr"] = time.time() - _t0            # 真·转写（上传+排队+识别）
                if args.keep_audio:
                    shutil.copy2(audio, cache_root / audio.name)   # --keep-audio 也进缓存目录，片库不落文件
        _stage = time.time()
        sentences = sentences_of(json.loads(asr_json.read_text(encoding="utf-8")))
        cues = cues_from_asr(sentences)
        T["asr"] += time.time() - _stage                # 切 cue 计入转写阶段
        origin = "ASR 转写"

    T["probe"] = time.time() - _stage
    if args.limit and args.limit > 0:
        cues = cues[:args.limit]
        print(f"[limit] 仅处理前 {len(cues)} 条", flush=True)
    if not cues:
        raise SystemExit("没有切出任何字幕条")
    check_timeline(cues, video, partial=bool(args.limit))
    print(f"[src] {origin}：{len(cues)} 条，{ts(cues[0]['begin'])} → {ts(cues[-1]['end'])}", flush=True)

    source_srt = cache_root / f"{stem}{SOURCE_SUFFIX}.srt"
    write_source_srt(cues, source_srt)
    # 元数据只留"复用守卫"真正要读的字段：cues（空则不复用）与 limited（截断产物永不复用）
    (cache_root / f"{stem}{SOURCE_SUFFIX}.json").write_text(json.dumps(
        {"cues": len(cues), "limited": bool(args.limit), "origin": origin},
        ensure_ascii=False), encoding="utf-8")

    # 同步校验：外挂/下载来的字幕可能与视频不同版本 —— 用音频对齐验一次（auto 时仅外挂字幕触发）
    if args.verify_sync == "on" or (args.verify_sync == "auto" and origin.startswith("外挂")):
        info = verify_sync(video, source_srt, cache_root)
        if info:
            cues = read_srt(source_srt)          # 以盘上的 source.srt 为准重建时间轴
            print(f"[sync] 时间轴已核对（offset {info['offset']:+.3f}s、scale {info['scale']:.4f}"
                  f"{'，已校正' if info['fixed'] else '，无需校正'}，{len(cues)} 条）", flush=True)

    # ---- 语言对：出几行、行序、以及哪些行需要翻译 ----
    pair = parse_langs(args.subtitles) if args.subtitles else None
    lines = [c["text"] for c in cues]
    src_lang, src_basis = resolve_source_lang(args.source_lang, track_lang, lines)
    if pair is None:
        if args.target_lang:
            # 旧参数兼容：老语义就是"原文行 + 目标语言行"
            pair = tuple(dict.fromkeys(
                p for p in (src_lang, normalize_lang(args.target_lang)) if p))
            print(f"[lang] --target-lang 已废弃 → 按 --subtitles {','.join(pair)} 处理", flush=True)
        else:
            pair = LANGS_DEFAULT
    print(f"[lang] 源语言 {src_lang or '未知'}（{src_basis}）→ 输出 {','.join(pair)}"
          f"（{len(pair)} 行，第一行在最上、用醒目样式）", flush=True)
    w, h = video_size(video)

    if args.no_translate:
        _stage = time.time()
        final = out_dir / f"{stem}.ass"
        # 只出原文时不要用单语文件覆盖已有的双语成品。
        # 判据以**产出标记**为主，而不是 looks_bilingual（靠"含大量汉字"）：
        # 语言对不含中文时（如 --subtitles en,ja）成品里一个汉字都没有，汉字判据会放行覆盖。
        # looks_bilingual 留着兜"别人放的双语外挂字幕"——那种没有我们的标记。
        if final.exists() and (has_own_mark(final) or looks_bilingual(final)):
            final = out_dir / f"{stem}{MONO_SUFFIX}.ass"
            print(f"[out] {stem}.ass 已是双语成品 → 本次单语输出写到 {final.name}", flush=True)
        write_ass(cues, final, [[line] for line in lines], width=w, height=h)
        T["write"] = time.time() - _stage
        report_timing(stem, cues, origin, write_log=not args.no_log)
        print(f"[done] {final}")
        return

    key = api_key(args.api_key)
    fingerprint = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]
    _stage = time.time()
    rows_by_lang: dict[str, list[str]] = {}
    for lang in pair:
        if lang == src_lang:
            rows_by_lang[lang] = list(lines)      # 源语言行直接用原文：不翻译、不花钱、无缓存
            print(f"[mt] {lang}：源语言 → 直接复用原文，不做翻译", flush=True)
            continue
        rows_by_lang[lang] = translate_for_target(
            lines, key, src_lang, lang, args.chat_model,
            cache_root / f"{stem}.{lang}.json", fingerprint,
            workers=args.workers, batch_size=args.batch, timeout=args.timeout,
            backend=args.backend, local_server=args.local_server, rpm=args.rpm,
            no_cache=args.no_cache)
    T["mt"] = time.time() - _stage

    _stage = time.time()
    final = out_dir / f"{stem}.ass"
    rows_by_cue = [[rows_by_lang[lang][i] for lang in pair] for i in range(len(cues))]
    write_ass(cues, final, rows_by_cue, width=w, height=h)
    T["write"] = time.time() - _stage

    report_timing(stem, cues, origin, write_log=not args.no_log)
    print(f"[done] {final}")


if sys.version_info < (3, 10):
    raise SystemExit(
        f"omnisub 需要 Python 3.10+（当前 {sys.version.split()[0]}）。\n"
        "macOS 自带的 /usr/bin/python3 是 3.9，缺 Path.write_text(newline=) 等 3.10 API；\n"
        "请改用 3.10+ 解释器（例如本机 uv 托管的 ~/.local/bin/python3）。")


if __name__ == "__main__":
    main()
