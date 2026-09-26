---
name: douyin-to-text
description: 抖音链接转文字入口。用户给出抖音链接（v.douyin.com 或 www.douyin.com）要求提取内容/转文字/识别图片文字时使用。覆盖两条分支：有声视频→ASR 转写（qwen-audio-3.1-asr-flash）；单图或轮播图→下载图片后视觉读图（图中文字一并转出）。
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

## 计时

步骤 1 开头 `SECONDS=0`；每阶段完成即记录并归零（bash 的 `SECONDS` 赋值即重置，整秒粒度够用，不引外部依赖）：`t_triage`（步骤 1）、`t_dl`/`t_conv`（2a 下载/转码）、`t_asr`（2a 并行 ASR 批的墙钟时间）、`t_imgs`（2b 读图）。完成标准：耗时行同时出现在输出与日志，缺一即未 done。

汇总后追加一行 TSV 到 `~/.dsh/douyin-timing.log`（制表符分隔：日期、视频 ID、各阶段秒数、总计、正文字数），供跨次对比：哪段占主导、cookie 失效是否拖慢 triage。
