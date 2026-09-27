#!/usr/bin/env bash
# video_to_srt.py 的确定性自检（零 LLM、零网络、零计费，约 10 秒）。
#
# 为什么单独存在：skill-up 的用例是"agent 级"契约测试，agent 可能绕开被测路径
# （实测旧代码下 agent 自己补 --source asr、或把陈旧文件挪走，就能拿到正确结果），
# 也可能超时。这三条实测缺陷必须有一条**走产出路径、无法绕开**的闸门来守。
#
# 用法：
#   bash evals/fixtures/scripts/selftest-video-to-srt.sh [被测脚本路径]
#   PYTHON=/abs/path/python3 bash evals/fixtures/scripts/selftest-video-to-srt.sh
# 退出码 0 = 全部通过；非 0 = 有用例失败（会打印 FAIL 行）。
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
SCRIPT=${1:-$ROOT/scripts/video_to_srt.py}
PY=${PYTHON:-python3}
MKV=$ROOT/evals/fixtures/repos/video-guard/sample.mkv

[ -f "$SCRIPT" ] || { echo "找不到被测脚本：$SCRIPT"; exit 2; }
[ -f "$MKV" ] || { echo "找不到夹具视频：$MKV"; exit 2; }

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
  echo '{"cues": 2, "limited": false, "origin": "内嵌字幕轨 idx=2 srt/eng", "wrap_source": false}' > "$d/sample.source.json"
  cat > "$d/sample.asr.json" <<'JSON'
{"transcripts": [{"channel_id": 0, "content_duration_in_milliseconds": 7000, "sentences": [
 {"begin_time": 500, "end_time": 2000, "sentence_id": 1, "text": "FROM_ASR_JSON_ALPHA the quick brown fox."},
 {"begin_time": 3000, "end_time": 4600, "sentence_id": 2, "text": "FROM_ASR_JSON_BETA jumps over the lazy dog."}]}]}
JSON
  touch "$d"/sample.*
}

ok()   { pass=$((pass+1)); echo "  ✅ $1"; }
bad()  { fail=$((fail+1)); echo "  ❌ $1"; }

check_guard() {   # $1=目录 $2=场景名：原文必须来自 ASR JSON，且没被自家产物污染
  local d=$1 tag=$2
  if grep -q FROM_ASR_JSON_ALPHA "$d/sample.source.srt" 2>/dev/null; then ok "$tag：原文来自 ASR JSON"; else bad "$tag：原文不是 ASR JSON"; fi
  if grep -q STALE_SOURCE_MARKER "$d/sample.source.srt" 2>/dev/null; then bad "$tag：被自家 .source.srt 劫持（STALE 残留）"; else ok "$tag：未被自家产物劫持"; fi
  local origin
  origin=$(cd "$d" && $PY -c 'import json;print(json.load(open("sample.source.json")).get("origin",""))' 2>/dev/null)
  case "$origin" in
    "ASR 转写"*) ok "$tag：source.json origin=$origin" ;;
    *)           bad "$tag：source.json origin=$origin（期望 ASR 转写）" ;;
  esac
}

echo "== 场景 1：--refresh-source --asr-json --no-translate --no-log =="
mkws "$TMP/s1"
(cd "$TMP/s1" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json sample.asr.json --no-translate --no-log >/dev/null 2>&1) \
  && ok "场景 1：退出码 0" || bad "场景 1：非 0 退出"
check_guard "$TMP/s1" "场景 1"

echo "== 场景 2：同目录重跑，只给 --asr-json（不加 --refresh-source）—— 显式参数必须仍然说了算 =="
(cd "$TMP/s1" && $PY "$SCRIPT" sample.mkv --asr-json sample.asr.json --no-translate --no-log >/dev/null 2>&1) \
  && ok "场景 2：退出码 0" || bad "场景 2：非 0 退出"
check_guard "$TMP/s1" "场景 2"

echo "== 场景 3：只有 1 条 cue —— 对齐自检不得崩（旧版 max() on empty → ValueError）=="
mkws "$TMP/s3"
cat > "$TMP/s3/one.json" <<'JSON'
{"transcripts": [{"content_duration_in_milliseconds": 7000, "sentences": [
 {"begin_time": 500, "end_time": 2000, "sentence_id": 1, "text": "SINGLE_CUE_ONLY."}]}]}
JSON
out=$(cd "$TMP/s3" && $PY "$SCRIPT" sample.mkv --refresh-source --asr-json one.json --no-translate --no-log 2>&1)
if [ $? -eq 0 ] && ! grep -q Traceback <<<"$out"; then ok "场景 3：单条 cue 正常退出"; else bad "场景 3：崩溃或被 Traceback 打断"; fi
grep -q SINGLE_CUE_ONLY "$TMP/s3/sample.source.srt" 2>/dev/null && ok "场景 3：产出 1 条字幕" || bad "场景 3：没产出字幕"

echo
echo "结果：$pass 通过 / $fail 失败"
[ "$fail" -eq 0 ] || exit 1
