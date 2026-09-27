#!/usr/bin/env python3
"""omnisub 语言方向与 ASS 样式的确定性自检（零 LLM、零网络、零计费）。

为什么要有这条闸门：本项目的核心契约是"任意源语言 → 任意语言对的双语字幕"，
而 2026-09-27 之前的实现把翻译方向**写死**在提示词里（"把下面每一行英文翻译成简体中文"），
--target-lang 根本不进请求 → 中文源配 --target-lang en 的产出是"中文原样重复两遍"的假双语，
退出码 0、日志正常、没有任何报错。这种缺陷只有"直接检查请求体与产物"才拦得住，
端到端跑一遍是看不出来的（要真花钱调 MT 才能看出译文不对）。

覆盖：
  1. 方向进请求体：en→zh 与旧版**逐字节一致**（回归保护），zh→en 方向正确（缺陷修复）
  2. 漏译判据跟着目标语言走：正证（合格英译不得被判漏译，零请求）+ 反证（中文回给 en 必须被抓）
  3. ASS 产物：两行一个事件、\\r 按行切样式、上行更醒目（更大/加粗/纯白）、下行暖白、
     花括号与反斜杠转义、PlayRes 与字号按画面高度缩放
  4. 语言解析：别名归一、语言对解析、源语言三级回退、启发式判定（含日文不被误判成中文）

用法：PYTHONPATH= python3 evals/fixtures/scripts/selftest-langs.py [被测脚本路径]
退出码 0 = 全部通过。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

if sys.version_info < (3, 10):
    raise SystemExit(f"需要 Python 3.10+（当前 {sys.version.split()[0]}）；被测脚本本身也用 3.10 API。")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCRIPT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "scripts/omnisub.py"

PASS, FAIL = 0, 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  ✅ {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  ❌ {msg}")


def check(cond: bool, good: str, evil: str) -> None:
    ok(good) if cond else bad(evil)


# ASS V4+ 的 Style 字段序（**不含样式名**）：
#   0 Fontname  1 Fontsize  2 PrimaryColour  3 SecondaryColour  4 OutlineColour  5 BackColour
#   6 Bold  7 Italic  8 Underline  9 StrikeOut  10 ScaleX  11 ScaleY  12 Spacing  13 Angle
#   14 BorderStyle  15 Outline  16 Shadow  17 Alignment  18 MarginL  19 MarginR  20 MarginV
#   21 Encoding
F_FONT, F_SIZE, F_PRIMARY, F_BOLD, F_OUTLINE, F_ALIGN, F_MARGINV = 0, 1, 2, 6, 15, 17, 20


def style_fields(text: str) -> dict[str, list[str]]:
    """Style 行 → {样式名: 字段列表}，字段列表按上面的格式序。"""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("Style: "):
            name, _, rest = line[len("Style: "):].partition(",")
            out[name.strip()] = rest.split(",")
    return out


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("omnisub_under_test", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载被测脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["omnisub_under_test"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if not SCRIPT.exists():
        print(f"找不到被测脚本：{SCRIPT}")
        return 2
    m = load_module(SCRIPT)
    print(f"被测脚本：{SCRIPT}")

    # ---- 1. 翻译方向必须进请求体 ----
    print("== 1. 方向进请求体（写死方向的老缺陷）==")
    single = m._cloud_payload(["Hello there."], "qwen-mt-flash", "s", "en", "zh")
    check(single[0]["content"] == "把下面这句英文翻译成简体中文，只输出译文：\nHello there.",
          "en→zh 单行提示词与旧版逐字节一致",
          f"en→zh 单行提示词被改动：{single[0]['content']!r}")

    multi = m._cloud_payload(["a", "b"], "qwen-mt-flash", "s", "en", "zh")
    check(multi[0]["content"].startswith("把下面每一行英文翻译成简体中文。")
          and "[[1]] a" in multi[0]["content"] and "[[2]] b" in multi[0]["content"],
          "en→zh 批量提示词与编号标记协议与旧版一致",
          f"en→zh 批量提示词异常：{multi[0]['content']!r}")

    zh2en = m._cloud_payload(["你好"], "qwen-mt-flash", "s", "zh", "en")
    check(zh2en[0]["content"] == "把下面这句简体中文翻译成英文，只输出译文：\n你好",
          "zh→en 方向正确进请求（旧版这里是写死的英→中）",
          f"zh→en 方向没进请求：{zh2en[0]['content']!r}")

    ja2zh = m._cloud_payload(["こんにちは", "ありがとう"], "qwen-mt-flash", "s", "ja", "zh")
    check("日文" in ja2zh[0]["content"] and "简体中文" in ja2zh[0]["content"],
          "任意语言对：ja→zh 提示词带上日文源与中文目标",
          f"ja→zh 提示词异常：{ja2zh[0]['content']!r}")

    sysmsg = m.system_for("zh", "en")
    check("简体中文" in sysmsg and "英文" in sysmsg,
          "system 提示词方向跟着语言对走",
          f"system 提示词方向不对：{sysmsg!r}")

    sysmsg_legacy = m.system_for("en", "zh")
    check(sysmsg_legacy == ("You are a professional subtitle translator. Translate each 英文 line "
                            "into 简体中文. Keep the same order and count. "
                            "Output ONLY a JSON array of strings."),
          "en→zh 的 system 提示词与旧版逐字节一致",
          f"en→zh system 提示词被改动：{sysmsg_legacy!r}")

    # ---- 2. 漏译判据必须跟着目标语言走（正反双证）----
    print("== 2. 漏译判据方向（正证 + 反证）==")
    calls: list = []
    real_batch = m._translate_batch

    def spy_batch(batch, *a, **kw):
        calls.append(list(batch))
        return [""] * len(batch)

    try:
        m._translate_batch = spy_batch
        zh_src = ["你好世界。", "这是一句中文。"]
        en_ok = ["Hello world.", "This is a Chinese sentence."]
        out = m.repair_missing(zh_src, en_ok, "k", "qwen-mt-flash", "s", 1, "cloud", "http://x",
                               src_lang="zh", tgt_lang="en")
        check(out == en_ok and not calls,
              "正证：合格的英文译文在 en 目标下不被判漏译（0 次补译请求）",
              f"正证失败：合格的英文译文被判漏译，发起了 {len(calls)} 次补译（旧版汉字判据的行为）")

        m._translate_batch = lambda batch, *a, **kw: ["Hello", "Thank you"][:len(batch)]
        calls.clear()
        zh_bad = ["你好", "谢谢"]          # 目标是 en，却回了中文 → 必须被抓
        out2 = m.repair_missing(zh_bad, list(zh_bad), "k", "qwen-mt-flash", "s", 1, "cloud",
                                "http://x", src_lang="zh", tgt_lang="en")
        check(out2 == ["Hello", "Thank you"],
              "反证：中文回给 en 目标被判漏译并成功补译",
              f"反证失败：真漏译没被抓住，结果 {out2!r}")

        m._translate_batch = spy_batch
        calls.clear()
        en_src = ["Hello world."]
        zh_ok = ["你好世界。"]
        out3 = m.repair_missing(en_src, zh_ok, "k", "qwen-mt-flash", "s", 1, "cloud", "http://x",
                                src_lang="en", tgt_lang="zh")
        check(out3 == zh_ok and not calls,
              "回归：en→zh 的合格中文译文仍不被判漏译（0 次补译请求）",
              f"en→zh 出现误判，发起 {len(calls)} 次补译")

        m._translate_batch = spy_batch
        calls.clear()
        m.repair_missing(["♪♪♪"], [""], "k", "qwen-mt-flash", "s", 1, "cloud", "http://x",
                         src_lang="en", tgt_lang="zh")
        check(not calls,
              "纯符号行（♪♪♪）不算可翻译行，不触发补译",
              f"纯符号行被当成漏译，发起 {len(calls)} 次补译")
    finally:
        m._translate_batch = real_batch

    # ---- 3. ASS 产物与样式 ----
    print("== 3. ASS 产物与样式（上行更醒目的落地）==")
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.ass"
        cues = [
            {"begin": 1000, "end": 3000, "text": "中文一"},
            {"begin": 3180, "end": 5000, "text": "中文二"},
        ]
        rows = [["Hello one", "中文一"], ["Second {brace}\\slash", "中文二"]]
        m.write_ass(cues, out_path, rows, width=1920, height=1080)
        text = out_path.read_text(encoding="utf-8")

        check("; Generated by omnisub" in text and "Title: omnisub" in text,
              "成品带产出标记（否则会被当外挂字幕读回来）",
              "缺少产出标记")
        check("PlayResX: 1920" in text and "PlayResY: 1080" in text,
              "PlayRes 取自画面尺寸", "PlayRes 不对")
        check(text.count("Dialogue:") == 2, "两条 cue → 两个 Dialogue 事件",
              f"Dialogue 事件数异常：{text.count('Dialogue:')}")
        check("0:00:01.00,0:00:03.00" in text, "时间戳为 ASS 厘秒格式",
              "时间戳格式不对")
        check("{\\rUpper}Hello one\\N{\\rLower}中文一" in text,
              "两行同一事件、\\N 换行、\\r 按行切样式（上行 Upper）",
              "行内样式切换结构不对")
        check("\\{brace\\}" in text and "\\\\slash" in text,
              "花括号与反斜杠都被转义（否则被当覆盖标签解析）",
              f"转义不对：{text.splitlines()[-1]!r}")

        styles = style_fields(text)
        up, low = styles.get("Upper", []), styles.get("Lower", [])
        check(len(up) > 20 and len(low) > 20, "两个样式都已定义", f"样式缺失或字段不全：{styles}")
        check(up[F_FONT] == "PingFang SC" and low[F_FONT] == "PingFang SC",
              "字体名已写入（缺字体时 libass 按系统默认回落）",
              f"字体名不对：{up[F_FONT] if up else '?'}")
        check(up[F_SIZE] == "54" and low[F_SIZE] == "47",
              "字号按画面高缩放：Upper 54 / Lower 47（1080p）",
              f"字号不对：{up[F_SIZE]}/{low[F_SIZE]}")
        check(up[F_PRIMARY] == "&H00FFFFFF" and low[F_PRIMARY] == "&H00E6EDF0",
              "上行纯白 &H00FFFFFF、下行暖白 &H00E6EDF0（方案 A）",
              f"配色不对：{up[F_PRIMARY]}/{low[F_PRIMARY]}")
        check(up[F_BOLD] == "-1" and low[F_BOLD] == "0",
              "上行加粗、下行常规（上行的醒目度差异）",
              f"字重不对：{up[F_BOLD]}/{low[F_BOLD]}")
        check(up[F_OUTLINE] == "3" and low[F_OUTLINE] == "2",
              "上行描边更粗（3 vs 2），进一步拉开醒目度",
              f"描边不对：{up[F_OUTLINE]}/{low[F_OUTLINE]}")
        check(up[F_ALIGN] == "2" and low[F_ALIGN] == "2" and up[F_MARGINV] == low[F_MARGINV],
              "两行同对齐同 MarginV（单事件堆叠才不会散）",
              f"对齐/边距不一致：{up[F_ALIGN]}/{low[F_ALIGN]} {up[F_MARGINV]}/{low[F_MARGINV]}")

        # 2160p：字号与描边按比例翻倍
        big = Path(tmp) / "big.ass"
        m.write_ass(cues, big, rows, width=3840, height=2160)
        big_styles = style_fields(big.read_text(encoding="utf-8"))
        b_up, b_low = big_styles["Upper"], big_styles["Lower"]
        check(b_up[F_SIZE] == "108" and b_low[F_SIZE] == "94" and b_up[F_OUTLINE] == "6",
              "2160p 下字号/描边按画面高自动缩放（108/94、描边 6）",
              f"2160p 缩放不对：{b_up[F_SIZE]}/{b_low[F_SIZE]}/{b_up[F_OUTLINE]}")

        # 三行（--subtitles ja,en,zh）：第三行回落到 Lower 样式
        m.write_ass(cues, Path(tmp) / "tri.ass", [["A", "B", "C"], ["D", "E", "F"]],
                    width=1920, height=1080)
        tri = (Path(tmp) / "tri.ass").read_text(encoding="utf-8")
        check("{\\rUpper}A\\N{\\rLower}B\\N{\\rLower}C" in tri,
              "三行语言对：首行 Upper、其余 Lower（支持 N 行）",
              "多行样式回落不对")

    # ---- 4. 语言解析与源语言回退 ----
    print("== 4. 语言解析与源语言回退 ==")
    check(m.parse_langs("en,zh") == ("en", "zh"), "parse_langs('en,zh')", "parse_langs 逗号解析失败")
    check(m.parse_langs("ja en zh") == ("ja", "en", "zh"), "parse_langs 支持空格分隔与三行",
          "parse_langs 空格解析失败")
    for spec in ("", "en,en"):
        try:
            m.parse_langs(spec)
            bad(f"parse_langs({spec!r}) 应当报错")
        except SystemExit:
            ok(f"parse_langs({spec!r}) 正确拒绝")
    check(m.normalize_lang("eng") == "en" and m.normalize_lang("zh-CN") == "zh"
          and m.normalize_lang("chi") == "zh" and m.normalize_lang("und") == "",
          "语言别名归一（eng/zh-CN/chi → 2 字母；und → 未知）",
          "语言别名归一失败")
    check(m.detect_lang(["你好，世界。"]) == ("zh", True), "启发式判定：中文（汉字区块，可信）",
          f"中文判定失败：{m.detect_lang(['你好，世界。'])!r}")
    check(m.detect_lang(["こんにちは、世界。"]) == ("ja", True),
          "启发式判定：含汉字的日文由假名区块判定为 ja，不被误判成中文源",
          "日文被误判成中文源")
    check(m.resolve_source_lang("zh", "eng", ["Hello"]) == ("zh", "显式 --source-lang"),
          "源语言回退第 1 级：显式参数优先于轨道标签",
          "显式参数没有优先")
    check(m.resolve_source_lang("auto", "eng", ["你好"]) == ("en", "字幕轨语言标签 eng"),
          "源语言回退第 2 级：轨道语言标签",
          "轨道标签级别不对")
    code, basis = m.resolve_source_lang("auto", "", ["你好，世界。"])
    check(code == "zh" and "启发式" in basis, "源语言回退第 3 级：文本启发式",
          f"启发式回退不对：{(code, basis)!r}")

    # ---- 5. 任意语言：非拉丁文字不得被丢弃（CR 致命项回归）----
    # 旧版 normalize_cues 的判据写死 [0-9A-Za-z\u4e00-\u9fff]，把"非拉丁非汉字"的字幕
    # 整片当纯符号丢掉：韩语 ASR 直接"没有切出任何字幕条"退出，任意语言在三源路径上全废。
    print("== 5. 任意语言：非拉丁文字不得被丢弃 ==")
    keep = {
        "中文": "就算是下地狱。", "英文": "Hello there.",
        "韩文": "안녕하세요, 반갑습니다.", "纯假名日文": "こんにちは、ありがとう。",
        "俄文": "Привет, как дела?", "阿拉伯文": "مرحبا بالعالم",
        "泰文": "สวัสดีครับ", "希腊文": "Καλημέρα", "天城文": "नमस्ते दुनिया",
        "希伯来文": "שלום עולם",
    }
    for label, text in keep.items():
        kept = m.normalize_cues([{"begin": 0, "end": 2000, "text": text}])
        check(len(kept) == 1, f"保留{label}字幕",
              f"{label}被当成纯符号丢掉了（整片会 0 条字幕 → 任意语言不成立）")
    for label, text in {"纯符号行": "♪♪♪", "空白行": "   "}.items():
        check(not m.normalize_cues([{"begin": 0, "end": 2000, "text": text}]),
              f"仍然丢掉{label}（放开非拉丁文字不等于把噪声也留下）",
              f"{label}被留下了")

    check(m.detect_lang(["안녕하세요 반갑습니다"] * 5) == ("ko", True),
          "源语言判定：韩文由谚文区块直接判定", "韩文没被判出来")
    check(m.detect_lang(["Привет, как дела? Спасибо большое."] * 5) == ("ru", True),
          "源语言判定：俄文由西里尔区块判定", "俄文没被判出来")
    fr = ["Bonjour, comment allez-vous aujourd'hui ? Je vais très bien, merci beaucoup."] * 4
    check(m.detect_lang(fr) == ("", False),
          "源语言判定：法文判不出具体语种 → 返回未知（每种语言都真翻译），"
          "而不是谎报 en 把法文原文填进英文行",
          f"法文被误报成英文：{m.detect_lang(fr)!r}")
    en = ["I think that you should go to the store and get some of the things we need for the party."] * 4
    check(m.detect_lang(en) == ("en", True),
          "源语言判定：英文由虚词密度确认（旗舰路径不会因此多花一次翻译钱）",
          f"英文判定失败：{m.detect_lang(en)!r}")

    check(all(m.looks_like_lang(t, lg) for lg, t in (
        ("ko", "안녕하세요"), ("ru", "Привет"), ("ja", "こんにちは"),
        ("ar", "مرحبا"), ("th", "สวัสดี"), ("zh", "你好"), ("en", "Hello"))),
          "漏译判据覆盖非中英目标（旧版只认 zh/en → ja/ko 目标每条都判漏译、白烧两轮）",
          "非中英目标的漏译判据仍不成立")
    check(not m.looks_like_lang("Hello 世界", "en")
          and not m.looks_like_lang("hello 안녕하세요", "en")
          and not m.looks_like_lang("Hello Привет", "en"),
          "漏译判据：混入其它文字区的文本不算英文译文"
          "（旧断言用的串不含拉丁字母，走的是恒真路径——等于假绿）",
          "混入其它文字区的文本被当成了英文")
    check(all(m.looks_like_lang(t, lg) for lg, t in (
        ("ta", "தமிழ் மொழி"), ("km", "ភាសាខ្មែរ"), ("my", "မြန်မာဘာသာ"),
        ("bn", "বাংলা ভাষা"), ("si", "සිංහල"), ("bo", "བོད་ཡིག"))),
          "漏译判据覆盖长尾文字区（泰米尔/高棉/缅甸/孟加拉/僧伽罗/藏文）"
          "——没有它们时每条译文都被判漏译，白跑两轮且真漏译修不好",
          "长尾文字区的漏译判据不成立")
    check(m.detect_lang(["Bonjour, comment allez-vous ?"])[0] == "",
          "短样本（<30 词）的法文也不再被当成英文（靠非英语信号词兜底）",
          f"短法文样本仍被判成英文：{m.detect_lang(['Bonjour, comment allez-vous ?'])!r}")
    check(not m.NON_EN_MARKERS.search("I think that you should go to the store") and
          m.detect_lang(["Hello, how are you today my friend?"]) == ("en", True),
          "短样本兜底不误伤真英文（信号词只收不与英语撞车的词）",
          "真英文短样本被误判")
    check(m.parse_langs("en,zh-TW") == ("en", "zh-Hant") and m.lang_name("zh-TW") == "繁體中文",
          "繁体中文不再被静默并成简体（zh-TW / zh-Hant → zh-Hant / 繁體中文）",
          f"繁体被并成简体：{m.parse_langs('en,zh-TW')!r}")

    try:
        m._translate_batch = spy_batch
        calls.clear()
        ja_src = ["你好，世界。", "谢谢你。"]
        ja_ok = ["こんにちは、世界。", "ありがとうございます。"]
        out = m.repair_missing(ja_src, ja_ok, "k", "qwen-mt-flash", "s", 1, "cloud", "http://x",
                               src_lang="zh", tgt_lang="ja")
        check(out == ja_ok and not calls,
              "正证：合格的日文译文在 ja 目标下不被判漏译（旧版这里是全判漏译）",
              f"ja 目标误判，发起 {len(calls)} 次补译")
    finally:
        m._translate_batch = real_batch

    # ---- 6. 外挂字幕判定：自家成品不得被读回来，标记不得误伤真外挂 ----
    # 复审抓到的 N1：精确同名路径只查了 looks_bilingual（靠"含大量汉字"），
    # 于是语言对不含中文时（--subtitles en,ko）会把**自己刚交付的 .ass** 当外挂原文读回来，
    # 重跑变成自我翻译。与历史上 .source.srt 劫持同类，只是换了入口。
    print("== 6. 外挂字幕判定（自家成品 vs 真外挂）==")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        video = d / "sample.mkv"
        video.write_bytes(b"\x00")          # 只需同名占位：判定只看文件名与同级目录
        own = d / "sample.ass"
        m.write_ass([{"begin": 0, "end": 1000, "text": "x"}], own, [["Hello", "你好"]])
        check(m.has_own_mark(own) is True, "自家成品的产出标记可被识别", "成品标记识别失败")
        check(m.sidecar_path(video) is None,
              "自家成品不被当成外挂字幕（旧版在精确同名路径漏了这道守卫）",
              f"自家成品被当作外挂字幕：{m.sidecar_path(video)}")
        own.unlink()
        side = d / "sample.srt"
        side.write_text("1\n00:00:00,000 --> 00:00:01,000\nthis tool is named omnisub\n",
                        encoding="utf-8")
        check(m.has_own_mark(side) is False,
              "产出标记只对 ASS 生效（SRT 不可能带我们的标记）", "SRT 被误判为自家产物")
        check(m.sidecar_path(video) == side,
              "真外挂字幕即使台词里出现 omnisub 也被正常采纳（标记按整行匹配，不搜子串）",
              f"真外挂被误判成自家产物：{m.sidecar_path(video)}")

    print()
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (TypeError, AttributeError, KeyError) as exc:
        # 旧版把翻译方向写死在 _cloud_payload 里、函数签名里没有语言参数，
        # 跑到第一条断言就会抛 TypeError。这里把它翻译成人话，而不是甩一段栈。
        print(f"\n  ❌ 被测脚本缺少新的语言接口：{type(exc).__name__}: {exc}")
        print("     （预期形态：旧版本没有 src_lang/tgt_lang，翻译方向写死在提示词里）")
        sys.exit(1)
