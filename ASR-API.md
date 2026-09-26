# ASR API：qwen-audio-3.1-asr-flash-filetrans

## Key

```bash
KEY=$(grep -E "^OPENAI_API_KEY=" ~/.agentmemory/.env | cut -d= -f2)
```

> 本仓库不含任何密钥。KEY 从你机器上既有的 env 文件派生，请自行确认该文件权限（`chmod 600`）。

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
