# douyin-to-text

把视频/图片转成文字与字幕 —— 一个面向 AI Agent 的 **Agent Skill**（Claude Skill / DSH Skill 格式）。

覆盖三条分支：

| 输入 | 处理路径 |
| --- | --- |
| 抖音链接 · 有声视频（`video/…`） | 分享页取无水印直链 → 下载 → `afconvert` 转 16k 单声道 wav → 分段**并行** ASR |
| 抖音链接 · 单图 / 轮播图（`note/…`） | 逐张下载图片 → 视觉读图，**图中文字一并转出** |
| **本地视频文件（MKV/MP4…）** | **复用优先**出双语字幕：内嵌字幕轨 → 同目录外挂字幕 → 都没有才 ASR；再批量并行翻译，输出「原文 + 译文」SRT |

## 本地视频 → 双语字幕（复用优先）

**能拿到现成字幕就绝不转写**——人工字幕的时间轴比 ASR 更准，而且省掉整片转写成本：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 内嵌字幕轨 | MKV/MP4 里的 srt/ass/文本轨（含 SDH）→ 直接复用原时间轴 |
| 2 | 同目录外挂字幕 | `<视频基名>.srt` / `.ass` / `.vtt`（含带语言后缀的同名文件） |
| 3 | ASR 转写 | 前两者都没有才走：`ffmpeg` 解 16 kHz 单声道 FLAC → `bl speech recognize` 异步 filetrans（句级 `begin_time/end_time` ＋词级 `words[]`）→ 按字幕规范切 cue |

拿到原文后统一用 `bl text chat`（默认 `qwen-mt-flash`）批量**并行**翻译：**20 条/批 × 4 并发**，走编号标记协议（每行前缀 `[[n]]`，模型拆句也能按标记归位）；条数不符递归二分、漏条成批补译，配 50 次/分钟限速器（官方限额 60）。写出「原文行 + 译文行」的双语 SRT。

```bash
~/.local/bin/python3 \
  scripts/video_to_srt.py "<视频>" --out <输出目录> \
  [--source auto|embedded|sidecar|asr] [--source-lang en] [--target-lang zh] \
  [--sub-index N] [--no-translate] [--asr-json <已有.json>]
```

产出：`<视频基名>.srt`（双语）、`<视频基名>.source.srt`（复用的原文，便于复查/重译）、ASR 路线的 `<视频基名>.asr.json`。
**重切不重付**：`--asr-json` 跳过解音轨与转写，只重切/重译。

实测（43.8 分钟 1080p MKV，内含 `eng/SDH` 文本轨）：766 条 → 清洗后 765 条，时间轴 `00:00:02 → 00:43:45` 与视频长度一致；全程零 ASR 调用，只付翻译。

### 装进 selfvideo

selfvideo（mpv 垫底、未关 `config`）默认 `sub-auto=exact`：**SRT 命名成与视频同名、放同目录即自动加载**；另可放进应用字幕缓存目录与已下载字幕并列。

## 为什么不用 yt-dlp

yt-dlp 的 Douyin 提取器当前被 **a_bogus 签名风控**挡死（2026-09 实测：master 分支 + 登录 cookie 均返回 403）。本 Skill 走经过验证的**分享页 SSR 路线**：`www.iesdouyin.com/share/<kind>/<id>/` 的页面里内嵌 `window._ROUTER_DATA`，其中 `item_list[0]` 含文案、播放直链与图片列表。

## 快速开始

```bash
# 1. 安装 skill（放到你的 skills 目录）
cp -r douyin-to-text ~/.dsh/skills/        # 或 ~/.claude/skills/

# 2. 准备登录态 cookie（分享页 SSR 需要登录态才内嵌视频数据）
#    从浏览器 DevTools 里复制请求的 -b 串，写入单行 k=v; k=v 格式：
printf '%s' 'ttwid=...; passport_csrf_token=...' > ~/.dsh/douyin-cookies.txt
chmod 600 ~/.dsh/douyin-cookies.txt

# 3. 把链接（或本地视频）交给 Agent，或按 SKILL.md 的步骤手动执行
```

ASR / 翻译通道与实测约束见 [`ASR-API.md`](ASR-API.md)。

## 目录结构

```
SKILL.md                 # Skill 本体：triage 规则、三条分支、输出模板、计时约定
ASR-API.md               # ASR 与翻译通道：bailian CLI / 原生异步 API / OpenAI 兼容端点
scripts/
  video_to_srt.py        # 本地视频 → 双语字幕（复用优先，ffmpeg/ffprobe + bailian CLI）
evals/
  eval.yaml              # skill-up 评测套件（3 条用例，全部离线）
  cases/*.yaml           # 用例定义与断言
  fixtures/repos/        # 离线夹具：合成分享页 HTML + 汇总输入
  README.md              # 评测设计与跑法
```

## 评测

三条用例**全部不联网、不下载、不写日志**，只验 Skill 的契约面：解析规则、守卫条件、输出模板。

```bash
skill-up validate evals/eval.yaml
skill-up run evals/eval.yaml --output-dir evals/.skill-up-workspace
```

> ⚠️ `environment.type: none` **不隔离** —— 被测 agent 直接跑在宿主机上。
> 会动文件系统的用例请改 `environment.type: docker`。详见 [`evals/README.md`](evals/README.md)。

## 安全设计

这个 Skill 会接触登录态 cookie 与付费 ASR / 翻译额度，因此有几条**刻意为之**的约束：

| 做法 | 原因 |
| --- | --- |
| **cookie 不落仓库**，只从 `~/.dsh/douyin-cookies.txt` 读取（`chmod 600`），且必须由用户手动提供 | 会话 cookie 等同账号凭据；写进 skill 目录会被 `git add .` 带走 |
| **API key 不落任何文件**，从既有 env 派生（`grep -E '^OPENAI_API_KEY=' ~/.agentmemory/.env \| cut -d= -f2`） | 避免在 skill 里新增一份密钥副本 |
| **不出货任何字幕/转写正文**：仓库只有工具与文档，生成的 `.srt` / `.asr.json` 一律不提交 | 影视/他人作品的字幕属于受版权保护的内容 |
| **评测夹具全部合成**：`share.html` 里的作者、文案、ID、直链都是占位值（`example.invalid`），不含任何真实抖音用户数据 | 真实分享页 HTML 含作者昵称/uid/sec_uid/带签名 CDN 直链，属于第三方 PII，不适合公开 |
| **评测用例不触碰真实链路** | 抖音风控 + cookie 时效 + ASR 计费会让它变成 flaky case，不适合当 CI 门禁；真实链路走人工冒烟 |
| **`.gitignore` 兜底**：cookie / 运行日志 / ASR 与字幕产物 / 评测 workspace 一律忽略 | 防止运行一次就把凭据或他人内容提交上去 |

**已知仍需注意的两点**（使用者自查）：

1. 下载的媒体落在 `/tmp`，属于**临时目录但不自动清理**；抖音分支步骤 1 开头有 `rm -f /tmp/dy_*` 清场，避免跨运行残留被误复用。若你的机器上有其他程序使用同名前缀，请改路径。
2. 本 Skill 会把提取到的正文原样返回，**不做版权判断**。用它处理他人作品时，请自行确认使用范围。

## 环境要求

- `python3` 3.10+（`scripts/video_to_srt.py` 只用标准库）＋ **`ffmpeg`/`ffprobe`**（探轨、抽字幕、抽音轨）＋ `bl`（bailian CLI，ASR 与翻译）
- `bailian` CLI（`bl`）＋ 一个可用的 ASR / 文本模型额度（见 `ASR-API.md`）
- 抖音分支在 macOS 上依赖系统原生 `afconvert` 做音视频转换（Linux 请替换为 `ffmpeg`）

> 踩坑提示：`bl` 是 npm shim（`#!/usr/bin/env node`）。**PATH 里没有 node 时它以 exit=127 静默空返回**，表现为"翻译批次全部返回 0 条却无报错"。`scripts/video_to_srt.py` 内置 `bl_env()` 把 `bl` 所在目录前置进子进程 PATH；自行调用 `bl` 时请一并处理。

## License

MIT
