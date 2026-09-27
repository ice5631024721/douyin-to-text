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
- 本机 `ffmpeg`/`ffprobe` 已于 2026-09-27 用 `brew reinstall ffmpeg` 修好（均 9.0.2，`/opt/homebrew/bin`）；音视频一律走它们，**不用 PyAV**（`afconvert` 读不了 MKV）

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
- 解音轨/抽字幕用 `ffmpeg`：`~/.local/bin/python3 scripts/video_to_srt.py …`（`ffprobe` 探轨、`ffmpeg -map 0:<idx> -c:s srt` 抽字幕、`-vn -ac 1 -ar 16000 -c:a flac` 抽音轨）

## 翻译：bl text chat（字幕双语化用）

```bash
bl text chat --model qwen-mt-flash --messages-file /tmp/msg.json \
  --api-key "$KEY" --output json --quiet
```

- `--messages-file` 收 JSON messages 数组（`-` 可走 stdin）；系统提示要求"逐行翻译、顺序与条数同输入、只输出 JSON 数组"
- **输出形态不固定**：实测直接返回模型正文的 JSON 数组（`["译文1","译文2"]`），也可能包成 `{choices:[{message:{content}}]}`——解析要三种都吃（见 `scripts/video_to_srt.py` 的 `translate()`）
- **批大小 20 最稳**：实测 40 条/批会让模型把短句并进相邻行、条数不符触发大量二分（400 条跑了 142 秒、49 次二分），20 条/批同样内容只要 32 秒。
- 协议用**编号标记**（每行前缀 `[[n]]`，模型拆句也能按标记归位）；条数不符**递归二分**、漏条**成批补译**，绝不「末尾补空」（那会让整批译文错位）。
- 配 4 并发 + **50 次/分钟限速器**（官方限额 60 次/分钟 + 3.5 万 token/分钟；逐行翻译必被 429，退避比限速更慢）；**务必带 `--timeout 180`**。

## 模型选型与费用（2026-09-27 实测，价格取自百炼模型目录）

### 纯文本翻译（字幕翻译用）

| 模型 | 输入 | 输出 | 20 条实测 | 备注 |
|---|---|---|---|---|
| **qwen-mt-flash**（默认） | 0.7 元/百万 token | 1.95 | **2 s** | 与 turbo 同价同档，在售 |
| qwen-mt-lite | 0.6 | 1.6 | 1 s | 输出会套 ```json 围栏、说话人标签保留英文 |
| qwen-mt-plus | 1.8 | 5.4 | 2 s | 质量档 |
| qwen-mt-uni | 文本 65 / 文档 20 / 图片 32 / 音频 400 元/百万 | 同左 | — | 多模态统一翻译 |
| ~~qwen-mt-turbo~~ | 0.7 | 1.95 | 1 s | **2026-10-10 下线** |

**一集 43.8 分钟剧集（765 条字幕）≈ 0.03 元**（输入约 1.2 万 token、输出约 1 万 token）。

### 语音类

| 模型 | 能力 | 价格 | 用法 |
|---|---|---|---|
| **qwen-audio-3.1-asr-flash-filetrans**（默认 ASR） | 识别，**带句级+词级时间戳** | 0.8 / 2.7 元/百万 token | 整片异步，实测 43.8 分钟音频 62 秒返回；一集 ≈0.08 元 |
| qwen-audio-3.0-asr | 识别 | 0.00022 元/秒 | 无时间戳需求时更省 |
| qwen3.8-omni-flash | 全模态理解＋**可直接英音→中文** | 0.8 / 2.7 元/百万 token | `bl omni --audio x.wav --text-only --message "翻译成中文"`；**无时间戳，不能做字幕轴** |
| ~~gummy-chat-v1 / gummy-realtime-v1~~ | 语音识别及翻译 | 0.00015 元/秒 | **2026-10-10 下线** |
| qwen3-livetranslate-flash 系列 | 直播/实时翻译 | 音频 10～40 元/百万 token | 实时/流式接口，`bl speech recognize` 调不通 |

### 怎么查某个模型是否要下线

```bash
bl model list --model <model-id> --output json | python3 -c "import json,sys;m=(json.load(sys.stdin).get('items') or [{}])[0];print(m.get('upcomingOfflineAt') or '在售', m.get('announceUrl',''))"
```
目录里带 `--include-deprecated` 可看已下线模型。

### 已知下线批次（2026-07-10 公告，2026-10-10 生效）

`qwen-mt-turbo`、`gummy-chat-v1`、`gummy-realtime-v1` → 替代：文本用 **qwen-mt-flash**，语音直出用 **qwen3.8-omni-flash**。
（公告页 https://www.aliyun.com/notice/118434 正文为 JS 渲染，清单以上面的目录字段为准。）

### 本地翻译后端（离线/隐私场景，可选）

```bash
# llama.cpp（官方 GGUF 量化版，Apple Metal）
llama-server -m <自备的 Hy-MT2-7B GGUF> --port 8080 -ngl 99   # 模型需自行下载（本机评测后已删除）
python scripts/video_to_srt.py <video> --backend local --local-server http://127.0.0.1:8080
```
本地用 Hy-MT2 官方的「分隔符」提示模板（`|||` 分段），脚本按分隔符切回。实测 Apple M4：Q4_K_M 19.95 tok/s、4.06 GB；MLX 8bit 12.0 tok/s、8.26 GB；质量与云端基本持平（中立裁判 6:6/6:7），但慢 8–25 倍。
