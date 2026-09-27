#!/usr/bin/env bash
# omnisub.py 的确定性自检（零 LLM、零网络、零计费，约 15 秒）。
#
# 为什么单独存在：skill-up 的用例是"agent 级"契约测试，agent 可能绕开被测路径
# （实测旧代码下 agent 自己补 --source asr、或把陈旧文件挪走，就能拿到正确结果），
# 也可能超时。这几条实测缺陷必须有一条**走产出路径、无法绕开**的闸门来守。
#
# 覆盖：显式参数的权威性（--refresh-source/--asr-json 不被自家产物架空）、单条 cue 不崩、
#       片库只多出一个 .ass、源语言判定与语言对解析、单语输出不覆盖双语成品。
# 翻译方向与 ASS 样式由同目录的 selftest-langs.py 覆盖（那部分必须直接查请求体与样式字段：
# 端到端跑一遍看不出来 —— 旧版把方向写死在提示词里，退出码照样是 0、日志照样正常）。
#
# 用法：
#   bash evals/fixtures/scripts/selftest-omnisub.sh [被测脚本路径]
#   PYTHON=/abs/path/python3 bash evals/fixtures/scripts/selftest-omnisub.sh
# 退出码 0 = 全部通过；非 0 = 有用例失败（会打印 ❌ 行）。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
SCRIPT=${1:-$ROOT/scripts/omnisub.py}
PY=${PYTHON:-python3}
MKV=$ROOT/evals/fixtures/repos/video-guard/sample.mkv
MKV_EMB=$ROOT/evals/fixtures/repos/video-embedded/sample.mkv

[ -f "$SCRIPT" ] || { echo "找不到被测脚本：$SCRIPT"; exit 2; }
[ -f "$MKV" ] || { echo "找不到夹具视频：$MKV"; exit 2; }

# 解释器版本闸门：本机默认 python3 是 3.9（/usr/bin/python3），缺 Path.write_text(newline=)
# 等 3.10 API，会产出一片"假红"——真故障会被当成环境噪声。宁可在这里明确报错。
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "需要 Python 3.10+，当前 $PY 是 $("$PY" -V 2>&1)。"
  echo "请用 PYTHON=/abs/path/python3.12 bash $0 指定 3.10+ 解释器。"
  exit 2
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0

mkws() {   # $1=目录：视频 + 陈旧的自家产物 + 合成 ASR 结果
  local d=$1
  rm -rf "$d"; mkdir -p "$d"
  cp "$MKV" "$d/sample.mkv"
  cat > "$d/sample.source.srt" <<'SRT'
1
00:00:00,500 --> 00:00:02,000
STALE_SOURCE_MARKER must not survive a refresh.

2
00:00:03,000 --> 00:00:04,600
STALE_SOURCE_MARKER second stale line.
SRT
  echo '{"cues": 2, "limited": false, "origin": "内嵌字幕轨 idx=2 srt/eng"}' > "$d/sample.source.json"
  cat > "$d/sample.asr.json" <<'JSON'
{"transcripts": [{"channel_id": 0, "content_duration_in_milliseconds": 7000, "sentences": [
 {"begin_time": 500, "end_time": 2000, "sentence_id": 1, "text": "FROM_ASR_JSON_ALPHA the quick brown fox."},
 {"begin_time": 3000, "end_time": 4600, "sentence_id": 2, "text": "FROM_ASR_JSON_BETA jumps over the lazy dog."}]}]}
JSON
  touch "$d"/sample.*
}

ok()  { pass=$((pass+1)); echo "  ✅ $1"; }
bad() { fail=$((fail+1)); echo "  ❌ $1"; }

check_guard() {   # $1=目录 $2=场景名：成品必须来自 ASR JSON，中间产物必须落在缓存目录
  local d=$1 tag=$2 origin
  grep -q FROM_ASR_JSON_ALPHA "$d/sample.ass" 2>/dev/null && ok "$tag：成品来自 ASR JSON" || bad "$tag：成品不是 ASR JSON"
  grep -q STALE_SOURCE_MARKER "$d/sample.ass" 2>/dev/null && bad "$tag：被自家 .source.srt 劫持（STALE 进了成品）" || ok "$tag：未被自家产物劫持"
  origin=$(cd "$d" && $PY -c 'import json;print(json.load(open(".cache/sample.source.json")).get("origin",""))' 2>/dev/null)
  case "$origin" in
    "ASR 转写"*) ok "$tag：缓存里的 origin=$origin" ;;
    *)           bad "$tag：缓存里的 origin=$origin（期望 ASR 转写；没落到 --cache-dir 也算失败）" ;;
  esac
}

echo "== 场景 1：--refresh-source --asr-json --no-translate --no-log --cache-dir .cache =="
mkws "$TMP/s1"
(cd "$TMP/s1" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json sample.asr.json --no-translate --no-log --cache-dir .cache >/dev/null 2>&1) \
  && ok "场景 1：退出码 0" || bad "场景 1：非 0 退出"
check_guard "$TMP/s1" "场景 1"

echo "== 场景 2：同目录重跑，只给 --asr-json（不加 --refresh-source）—— 显式参数必须仍然说了算 =="
(cd "$TMP/s1" && $PY "$SCRIPT" sample.mkv --asr-json sample.asr.json --no-translate --no-log --cache-dir .cache >/dev/null 2>&1) \
  && ok "场景 2：退出码 0" || bad "场景 2：非 0 退出"
check_guard "$TMP/s1" "场景 2"

echo "== 场景 3：只有 1 条 cue —— 对齐自检不得崩（旧版 max() on empty → ValueError）=="
mkws "$TMP/s3"
cat > "$TMP/s3/one.json" <<'JSON'
{"transcripts": [{"content_duration_in_milliseconds": 7000, "sentences": [
 {"begin_time": 500, "end_time": 2000, "sentence_id": 1, "text": "SINGLE_CUE_ONLY."}]}]}
JSON
out=$(cd "$TMP/s3" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json one.json --no-translate --no-log --cache-dir .cache 2>&1)
rc=$?
if [ "$rc" -eq 0 ] && ! grep -q Traceback <<<"$out"; then ok "场景 3：单条 cue 正常退出"; else bad "场景 3：崩溃或被 Traceback 打断（exit=$rc）"; fi
grep -q SINGLE_CUE_ONLY "$TMP/s3/sample.ass" 2>/dev/null && ok "场景 3：产出 1 条字幕" || bad "场景 3：没产出字幕"

echo "== 场景 4：默认缓存时，视频目录只许多出一个 .ass（中间产物不许留在片库）=="
d=$TMP/s4; rm -rf "$d"; mkdir -p "$d/home" "$d/video"; cp "$MKV_EMB" "$d/video/sample.mkv"
(cd "$d/video" && HOME="$d/home" $PY "$SCRIPT" sample.mkv --no-translate --no-log >/dev/null 2>&1) \
  && ok "场景 4：退出码 0" || bad "场景 4：非 0 退出"
leaked=$(cd "$d/video" && ls -A | grep -v -E '^(sample\.mkv|sample\.ass)$' || true)
[ -z "$leaked" ] && ok "场景 4：视频目录只有 原视频 + sample.ass" || bad "场景 4：视频目录多了：$(echo "$leaked" | tr '\n' ' ')"
cached=$(cd "$d/home" && find . -name "sample.source.srt" | head -1)
[ -n "$cached" ] && ok "场景 4：中间产物落在默认缓存目录（$cached）" || bad "场景 4：默认缓存目录里没有中间产物"

echo "== 场景 5：源语言判定与语言对（任意语言的入口）=="
d=$TMP/s5; rm -rf "$d"; mkdir -p "$d"; cp "$MKV" "$d/sample.mkv"
cat > "$d/cn.json" <<'JSON'
{"transcripts": [{"content_duration_in_milliseconds": 7000, "sentences": [
 {"begin_time": 500, "end_time": 2000, "sentence_id": 1, "text": "就算是下地狱，上司也会比我们更深一层。"},
 {"begin_time": 3000, "end_time": 4600, "sentence_id": 2, "text": "有他们在前面，会不会走得快一点？"}]}]}
JSON
out=$(cd "$d" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json cn.json --no-translate --no-log --cache-dir .cache 2>&1)
grep -q "源语言 zh" <<<"$out" && ok "场景 5：中文转写被判定为 zh 源（文本启发式）" \
  || bad "场景 5：源语言判定不是 zh —— $(grep -o '\[lang\].*' <<<"$out" | head -1)"
grep -q "输出 en,zh" <<<"$out" && ok "场景 5：默认语言对是 en,zh（英上中下）" \
  || bad "场景 5：默认语言对不是 en,zh —— $(grep -o '\[lang\].*' <<<"$out" | head -1)"
out=$(cd "$d" && $PY "$SCRIPT" sample.mkv --asr-json cn.json --no-translate --no-log --cache-dir .cache --subtitles zh,ja 2>&1)
grep -q "输出 zh,ja" <<<"$out" && ok "场景 5：--subtitles zh,ja 覆盖默认语言对与行序" \
  || bad "场景 5：--subtitles 没生效 —— $(grep -o '\[lang\].*' <<<"$out" | head -1)"
out=$(cd "$d" && $PY "$SCRIPT" sample.mkv --asr-json cn.json --no-translate --no-log --cache-dir .cache --subtitles en,en 2>&1)
grep -q "重复语言" <<<"$out" && ok "场景 5：--subtitles 里的重复语言被拒绝" \
  || bad "场景 5：重复语言没被拒绝"

echo "== 场景 6：已有双语成品时，--no-translate 不覆盖（改写 .mono.ass）=="
d=$TMP/s6; rm -rf "$d"; mkdir -p "$d"; cp "$MKV" "$d/sample.mkv"
cat > "$d/sample.ass" <<'ASS'
[Script Info]
; Generated by omnisub
Title: omnisub
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Upper,PingFang SC,54,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,40,40,32,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.50,0:00:02.00,Upper,,0,0,0,,{\rUpper}KEEP_ME
ASS
(cd "$d" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json "$TMP/s3/one.json" --no-translate --no-log --cache-dir .cache >/dev/null 2>&1)
grep -q KEEP_ME "$d/sample.ass" 2>/dev/null && ok "场景 6：双语成品未被单语输出覆盖" || bad "场景 6：双语成品被覆盖了"
[ -f "$d/sample.mono.ass" ] && ok "场景 6：单语输出写到 sample.mono.ass" || bad "场景 6：没有产出 .mono.ass"

echo "== 场景 7：翻译方向与 ASS 样式（同一闸门的 Python 部分）=="
if [ -f "$HERE/selftest-langs.py" ]; then
  if out=$($PY "$HERE/selftest-langs.py" "$SCRIPT" 2>&1); then
    ok "场景 7：$(tail -1 <<<"$out")"
  else
    bad "场景 7：语言/样式自检失败 —— $(grep '❌' <<<"$out" | head -3 | tr '\n' ' ')"
  fi
else
  bad "场景 7：找不到 selftest-langs.py"
fi

echo
echo "结果：$pass 通过 / $fail 失败"
[ "$fail" -eq 0 ] || exit 1
