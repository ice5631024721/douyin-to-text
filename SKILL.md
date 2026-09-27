---
name: douyin-to-text
description: 视频/图片转文字与字幕入口。抖音链接（v.douyin.com 或 www.douyin.com）要求提取内容/转文字/识别图片文字时使用；本地视频文件（含 MKV）要求转写、出字幕、要时间轴或双语字幕时同样使用。覆盖三条分支：抖音有声视频→ASR 转写（qwen-audio-3.1-asr-flash）；单图或轮播图→下载图片后视觉读图；本地视频文件→带句级/词级时间戳的双语字幕 SRT。
---

# 抖音链接 → 文字

给一个抖音链接，产出完整内容文本。**yt-dlp 的 Douyin 提取器当前被 a_bogus 签名风控挡死（2026-09 实测连 master + 登录 cookie 均 403），禁用 yt-dlp 取抖音**，走下面验证过的分享页路线。

cookie 依赖：分享页 SSR 需要登录态 cookie 才内嵌视频数据。cookie 存于 `~/.dsh/douyin-cookies.txt`（单行 `k=v; k=v` 格式）；失效（item_list 为空）时向用户要一份新的 DevTools curl 里的 `-b` 串覆盖该文件。

## 步骤

### 1. Triage + 取直链（一次完成）

**先清场**：`rm -f /tmp/dy_share.html /tmp/dy_video.mp4 /tmp/dy_audio.wav /tmp/dy_part*.wav /tmp/dy_txt_*.txt /tmp/dy_img_* /tmp/dy_transcript.txt`——/tmp 跨运行残留会让图片帖误复用上条视频的下载/转码产物（2026-09-26 实测踩坑）。

```bash
EFF=$(curl -sIL "<链接>" -o /dev/null -w '%{url_effective}')
KIND=$(echo "$EFF" | grep -oE '(video|note)/[0-9]+' | cut -d/ -f1)   # note = 图片/轮播帖
ID=$(echo "$EFF" | grep -oE '[0-9]{10,}' | head -1)
curl -s -A "<iPhone Safari UA>" -b "$(cat ~/.dsh/douyin-cookies.txt)" \
  "https://www.iesdouyin.com/share/$KIND/$ID/" -o /tmp/dy_share.html
```

解析 `_ROUTER_DATA`（`loaderData['video_(id)/page']['videoInfoRes']['item_list'][0]`）：
- `desc` = 文案；`video.play_addr.url_list[0]` 把 `playwm` 替换为 `play` = 无水印直链 → **视频分支**
- `images` 非空 → **图片/轮播分支**（每项 `url_list[0]`）
- item_list 为空 = cookie 失效，按上面约定换 cookie，禁止继续猜。

### 2a. 视频分支：下载 → 转 wav → 分段 ASR

```bash
curl -sL -A "<iPhone Safari UA>" -e "https://www.iesdouyin.com/" -o /tmp/dy_video.mp4 "<play直链>"
afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/dy_video.mp4 /tmp/dy_audio.wav   # macOS 原生，绕开坏 ffmpeg
```

守卫：mp4 < 100KB = 直链过期（302 落到错误页），回步骤 1 重取直链，禁止对坏文件继续转码。

ASR 同步模式上限 300 秒：用 python `wave` 按 **280 秒**切片为 /tmp/dy_part%02d.wav（贴上限切，调用数最少）。**并行转写**（主导阶段，并发提速约 3 倍）：每片 `bl ... > /tmp/dy_txt_%02d.txt 2>/dev/null &` 后台启动后 `wait`，按文件名序拼接；空文本片顺序重试一次。**bl 的正文在 stdout、banner 在 stderr**——直接收 stdout，别 sed 删行。

完成标准：拿到非空转写；纯 BGM 无人声的空文本也是合法结果，如实报告。

### 2b. 图片/轮播分支：下载 → 视觉读图

curl 逐张下载 `images[i].url_list[0]` 到 /tmp/dy_img_N.jpeg，用 read_image 逐张读出**图中全部文字**并描述画面；多张独立文件可同批并行读。完成标准：每张图有文字转写或"无文字"的明确记录。

### 3. 汇总输出

```
【文案】<desc>
【类型】视频(<时长>) | 单图 | 轮播(<n>张)
【正文】<ASR 文本或图片文字>
【备注】<BGM 无人声 / 某图无文字 / cookie 失效等>
【耗时】triage {t_triage}s · 下载 {t_dl}s · 转码 {t_conv}s · ASR {t_asr}s（{n}片）· 总计 {t_total}s
```

## 本地视频文件 → 双语字幕（SRT，时间轴与视频一一对应）

给一个本地视频（MKV/MP4 都行）产出双语字幕。**复用优先：能拿到现成字幕就绝不转写。**

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | **内嵌字幕轨** | MKV/MP4 里的 srt/ass/文本轨（含 SDH）→ 直接复用**原时间轴**，零 ASR 成本，且人工字幕比转写更准 |
| 2 | **同目录外挂字幕** | `<视频基名>.srt` / `.ass` / `.vtt`（含带语言后缀的同名文件）→ 同上 |
| 3 | **ASR 转写** | 前两者都没有才走：PyAV 解 16 kHz 单声道 FLAC → `bl speech recognize` 异步 filetrans（句级 begin/end ＋词级时间戳）→ 按字幕规范切 cue |

拿到原文后统一用 `bl text chat`（默认 `qwen3.8-flash`；`--chat-model qwen3.8-max` 质量略高但约慢一倍）批量**并行**翻译，输出「原文行 ＋ 译文行」的双语 SRT。

```bash
~/.local/bin/uv run --with av --with numpy python \
  ~/.dsh/skills/douyin-to-text/scripts/video_to_srt.py "<视频>" \
  --out <输出目录> [--source auto|embedded|sidecar|asr] [--source-lang en] \
  [--target-lang zh] [--sub-index N] [--no-translate] [--asr-json <已有.json>]
```

产出：`<视频基名>.srt`（双语）、`<视频基名>.source.srt`（复用的原文，便于复查/重译）、ASR 路线的 `<视频基名>.asr.json`。**重切不重付**：`--asr-json` 跳过解音轨与转写，只重切/重译。

实测（Undercover.Billionaire S01E02，43.8 分钟 1080p MKV）：内嵌 `eng/SDH` 文本轨 766 条 → 清洗后 765 条，时间轴 00:00:02 → 00:43:45，与视频长度一致；全程零 ASR 调用，只付翻译。

### 装进 selfvideo

selfvideo 用 mpv 垫底、没关 `config`（`~/.config/mpv/` 为空），mpv 默认 `sub-auto=exact`：**SRT 命名成与视频同名、放同目录即自动加载**。

```bash
cp "<视频基名>.srt" "<视频所在目录>/<视频基名>.srt"                        # mpv 自动加载
cp "<视频基名>.srt" ~/Library/Caches/dev.selfvideo.player/selfvideo/subs/   # 应用字幕缓存
```

（selfvideo 字幕链路：provider → `~/Library/Caches/dev.selfvideo.player/selfvideo/subs/` → mpv `sub-add`，见 `src-tauri/src/subs/mod.rs` 的 `sub_load`；mpv 启动选项在 `src-tauri/src/lib.rs`。）

### 这条分支的坑（实测）

- **`bl` 是 npm shim（`#!/usr/bin/env node`）：PATH 里没有 node 时它以 exit=127 静默空返回**——症状是"20 个翻译批次全部返回 0 条"却毫无报错。脚本用 `bl_env()` 把 bl 所在目录前置进子进程 PATH；凡是调用 `bl` 的脚本都要做这件事（本机 GUI 会话 PATH 默认只有 `/usr/bin:/bin:/usr/sbin:/sbin`）。
- **ffmpeg/ffprobe 断链**：`ffprobe` 指向已删 Cellar 版本、`ffmpeg` 9.0.2 缺 `libx264.165.dylib`、`ffmpeg@7` 无 bin → 一律 PyAV（`uv run --with av`），别去修 brew。macOS 原生 `afconvert` **读不了 MKV**。
- **同步 ASR 上限 300 秒**；要整片一次过且要时间戳，用异步模型（`*-filetrans` / `fun-asr` / `paraformer-*`）配 `--out <json>`。
- **`bl text chat --output json` 可能直接输出模型正文（JSON 数组）而非包装对象**——解析要同时吃 list、`{choices:[{message:{content}}]}`、裸文本三种形态。
- **翻译批次并行**（默认 8 并发），结果按批索引回填，顺序不会乱；返回条数与输入不符的批次告警并按序兜底。
- SDH 底本含 `GLENN STEARNS:` 这类说话人标签与 `[door slams]`、`♪` 等注释——翻译提示词里已要求原样保留。
- macOS 无 `timeout`；整片转写/翻译放后台作业跑。

## 计时

步骤 1 开头 `SECONDS=0`；每阶段完成即记录并归零（bash 的 `SECONDS` 赋值即重置，整秒粒度够用，不引外部依赖）：`t_triage`（步骤 1）、`t_dl`/`t_conv`（2a 下载/转码）、`t_asr`（2a 并行 ASR 批的墙钟时间）、`t_imgs`（2b 读图）。完成标准：耗时行同时出现在输出与日志，缺一即未 done。

汇总后追加一行 TSV 到 `~/.dsh/douyin-timing.log`（制表符分隔：日期、视频 ID、各阶段秒数、总计、正文字数），供跨次对比：哪段占主导、cookie 失效是否拖慢 triage。
