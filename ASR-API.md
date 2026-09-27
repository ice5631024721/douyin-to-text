# ASR API：qwen-audio-3.1-asr-flash-filetrans

## Key

```bash
KEY=$(grep -E "^OPENAI_API_KEY=" ~/.agentmemory/.env | cut -d= -f2)
```

## 首选：bailian CLI（接受本地文件，实测路径）

未装则先装：`export PATH="$HOME/.local/share/fnm/node-versions/v24.15.0/installation/bin:$PATH" && npm install -g bailian-cli`

```bash
export PATH="$HOME/.local/share/fnm/node-versions/v24.15.0/installation/bin:$PATH"
bl speech recognize --url /tmp/dy_audio.m4a \
  --model qwen-audio-3.1-asr-flash-filetrans --output json
```

结果在返回 JSON 的文本字段（transcript/text），失败时 JSON 含 code/message。

## 备选：原生异步 API

`filetrans` 走异步任务（需公网可访问的 file_urls；本地文件此路不通，仅当音频已有公网 URL 时用）：

```bash
curl -s -X POST https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen-audio-3.1-asr-flash-filetrans","input":{"file_urls":["<PUBLIC_URL>"]}}'
# 轮询：GET https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}
```

## 兜底：OpenAI 兼容同步端点

CLI 与异步 API 均不可用时尝试（模型名不带 filetrans）：

```bash
curl -s -X POST https://dashscope.aliyuncs.com/compatible-mode/v1/audio/transcriptions \
  -H "Authorization: Bearer $KEY" -F model=qwen3-asr-flash -F file=@/tmp/dy_audio.m4a
```

注意：此路曾对 `qwen3-asr-flash` 返回 404（模型路由可能仅开放部分端点）——失败就回到 CLI 路径，不要反复重试。

## 计费事实

- filetrans 输入 0.8 元/百万 tokens（三条 ASR lane 最便宜）
- 免费额度：filetrans 36,000 秒；qwen-audio-3.1 系列每模型另计 100 万 token（至 2026-12-21）
- 实测量级：≈0.0012–0.013 元/分钟音频

## 实测约束（2026-09-26 验证）

- `bl speech recognize` **同步模式上限 300 秒音频**，超出报 `audio duration over service process (300s)`——长音频按 ≤280 秒/片切分，各片**并行**转写后按序拼接
- **bl 输出流反直觉：转写正文在 stdout，`[Model:...]` banner 在 stderr**。收正文用 `2>/dev/null` 直接拿 stdout；`--output json` 在当前版本不可靠，按纯文本收
- 本机 ffmpeg 断链（x264 dylib 缺失），音视频转换一律用 macOS 原生 `afconvert`

## 整片带时间戳转写（字幕用，2026-09-27 实测）

要时间轴就别走同步模式：**异步模型**（`*-filetrans` / `fun-asr` / `paraformer-*`）接受整片音频，并可用 `--out` 落 JSON。

```bash
bl speech recognize --url /tmp/e02_16k.flac \
  --model qwen-audio-3.1-asr-flash-filetrans --language en \
  --out /tmp/e02_asr.json --output json --api-key "$KEY"
```

- 实测：43.8 分钟音频（16 kHz 单声道 FLAC，48 MB）**62 秒**返回，**无 300 秒限制**
- JSON 结构：`transcripts[0].sentences[]`，每句含 `begin_time`/`end_time`（毫秒）、`text`、`sentence_id`，以及**词级** `words[]`（`begin_time`/`end_time`/`text`/`punctuation`/`confidence`）——字幕切分与对齐用这一层
- 覆盖率自检：末句 `end_time` 应接近容器时长（实测 2626.8s vs 2628s），并确认相邻句之间没有 >15s 的空隙
- 解音轨用 PyAV：`~/.local/bin/uv run --with av --with numpy python scripts/video_to_srt.py …`；`afconvert` **读不了 MKV**（AVFoundation 不支持 Matroska）

## 翻译：bl text chat（字幕双语化用）

```bash
bl text chat --model qwen3.8-flash --messages-file /tmp/msg.json \
  --api-key "$KEY" --output json --quiet
```

- `--messages-file` 收 JSON messages 数组（`-` 可走 stdin）；系统提示要求"逐行翻译、顺序与条数同输入、只输出 JSON 数组"
- **输出形态不固定**：实测直接返回模型正文的 JSON 数组（`["译文1","译文2"]`），也可能包成 `{choices:[{message:{content}}]}`——解析要三种都吃（见 `scripts/video_to_srt.py` 的 `translate()`）
- 批大小 40 行为宜（实测 40 条/批比 20 条/批更划算）；模型用 `qwen3.8-flash`，`qwen3.8-max` 约慢一倍；返回条数与输入不符时脚本告警并逐行兜底，避免整批丢字幕
