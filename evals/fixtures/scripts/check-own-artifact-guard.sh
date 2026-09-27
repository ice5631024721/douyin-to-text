#!/usr/bin/env bash
# 判定：原文必须来自 --asr-json，而不是目录里那份自家中间产物 <基名>.source.srt。
# 工作目录 = 用例工作区根目录（skill-up 约定）；退出码 0 = PASS。
# 判别不看"退出码"，看产物本身：
#   1) 文本来自 ASR JSON（正向）
#   2) 没有被陈旧的自家产物污染（反向）
#   3) .source.json 的 origin 记录的是 ASR 分支（而不是"复用的 source.srt"）
#   4) 走了默认 auto 路由 —— 显式加 --source asr 会绕过被测路径，等于没测到，判 FAIL
set -uo pipefail
f=sample.source.srt
[ -f "$f" ] || { echo "FAIL: $f 不存在"; exit 1; }

grep -q "FROM_ASR_JSON_ALPHA" "$f"      || { echo "FAIL: $f 里没有 ASR JSON 的内容"; exit 1; }
! grep -q "STALE_SOURCE_MARKER" "$f"    || { echo "FAIL: 自家中间产物被当成外挂字幕复用了（STALE_SOURCE_MARKER 仍在）"; exit 1; }

if [ -f sample.source.json ]; then
  origin=$(python3 -c 'import json,sys;print(json.load(open("sample.source.json")).get("origin",""))' 2>/dev/null || echo "")
  case "$origin" in
    "ASR 转写"*) ;;
    *) echo "FAIL: source.json 的 origin=$origin（期望 ASR 转写），说明走了缓存/外挂而不是 ASR 分支"; exit 1 ;;
  esac
fi

if [ -n "${EVAL_TRANSCRIPT_PATH:-}" ] && [ -r "${EVAL_TRANSCRIPT_PATH}" ]; then
  if grep -q -- "--source[= ]asr" "${EVAL_TRANSCRIPT_PATH}"; then
    echo "FAIL: 运行里显式用了 --source asr，绕过了默认 auto 路由 —— 本用例要测的正是 auto 路由，等于没测到"
    exit 1
  fi
fi

echo "PASS: 原文来自 --asr-json，未被自家 .source.srt 劫持，且走的是默认 auto 路由"
