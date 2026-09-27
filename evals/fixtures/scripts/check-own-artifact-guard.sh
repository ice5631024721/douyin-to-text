#!/usr/bin/env bash
# 判定：交付物（sample.ass）的原文必须来自 --asr-json，而不是目录里那份自家中间产物
# sample.source.srt；且中间产物必须落在 --cache-dir 指定的缓存目录里，不留在视频目录。
# 工作目录 = 用例工作区根目录（skill-up 约定）；退出码 0 = PASS。
set -uo pipefail
f=sample.ass
[ -f "$f" ] || { echo "FAIL: 交付物 $f 不存在"; exit 1; }

grep -q "FROM_ASR_JSON_ALPHA" "$f"      || { echo "FAIL: $f 里没有 ASR JSON 的内容"; exit 1; }
! grep -q "STALE_SOURCE_MARKER" "$f"    || { echo "FAIL: 自家中间产物被当成外挂字幕复用（STALE_SOURCE_MARKER 进了成品）"; exit 1; }

meta=.cache/sample.source.json
if [ -f "$meta" ]; then
  origin=$(python3 -c 'import json;print(json.load(open(".cache/sample.source.json")).get("origin",""))' 2>/dev/null || echo "")
  case "$origin" in
    "ASR 转写"*) ;;
    *) echo "FAIL: .cache/sample.source.json 的 origin=$origin（期望 ASR 转写），说明走了缓存/外挂而不是 ASR 分支"; exit 1 ;;
  esac
else
  echo "FAIL: 没找到缓存里的 .cache/sample.source.json —— 中间产物没落进 --cache-dir"; exit 1
fi

if [ -n "${EVAL_TRANSCRIPT_PATH:-}" ] && [ -r "${EVAL_TRANSCRIPT_PATH}" ]; then
  if grep -q -- "--source[= ]asr" "${EVAL_TRANSCRIPT_PATH}"; then
    echo "FAIL: 运行里显式用了 --source asr，绕过了默认 auto 路由 —— 本用例要测的正是 auto 路由，等于没测到"
    exit 1
  fi
fi

echo "PASS: 成品来自 --asr-json、未被自家产物劫持、走默认 auto 路由、中间产物落在缓存目录"
