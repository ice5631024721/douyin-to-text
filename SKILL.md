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

| 优先级 | 来源 | 做法 |
|---|---|---|
| 1 | **内嵌字幕轨** | `ffprobe` 探轨 → `ffmpeg -map 0:<idx> -c:s srt` 抽成 SRT → **复用原时间轴**，零 ASR 成本，比转写更准 |
| 2 | **同目录外挂字幕** | `<视频基名>.srt` / `.ass` / `.vtt` 直接读 |
| 3 | **ASR 转写** | `ffmpeg -vn -ac 1 -ar 16000 -c:a flac` 抽音轨 → `bl speech recognize` 异步 filetrans（句级 `begin_time/end_time` ＋ 词级 `words[]`）→ 按字幕规范切 cue |

拿到原文后翻译：**默认云端 `qwen-mt-flash`**（`bl text chat`），可 `--backend local` 切本地 llama.cpp / mlx-lm 的 OpenAI 兼容服务。

```bash
python ~/.dsh/skills/douyin-to-text/scripts/video_to_srt.py "<视频>" \
  --out <输出目录> [--source auto|embedded|sidecar|asr] [--source-lang en] [--target-lang zh] \
  [--sub-index N] [--chat-model qwen-mt-flash] [--backend cloud|local] \
  [--local-server http://127.0.0.1:8080] [--batch 20] [--workers 4] [--asr-json <已有.json>]
```

产出：`<视频基名>.srt`（双语）、`.source.srt`（复用的原文）、`.<lang>.json`（译文缓存，按原文指纹校验，**重切不重付**）。

实测（一部 43.8 分钟 1080p MKV，内嵌 `eng/SDH` 字幕轨 766 条 → 清洗后 765 条）：

| 阶段 | 耗时 | 关键点 |
|---|---|---|
| 探测＋抽内嵌字幕 | **2.8 s（热）/ 16–23 s（冷盘）** | 带 `-probesize 20M -analyzeduration 20M`；主要成本是读文件：MKV 字幕包散布全片，ffmpeg 得读完 3 GB 才抽得全 |
| 翻译 765 条 | **54 s** | qwen-mt-flash，20 条/批 × 4 并发，编号标记协议 |
| 合计 | **57 s** | 全程无 ASR 调用，成本 ≈0.03 元 |

质检：765/765 条有中文、0 重叠、0 乱序、无 >15 s 空隙；时间轴 `00:00:02 → 00:43:45` 与片长 2628.032 s 对齐。

**两个省时机（第二次跑同一部片时）**：
- **源字幕复用**：抽内嵌字幕要把整片读一遍（2.79 GB 外置机械盘实测 **22 s**）。产出目录里已有 `<基名>.source.srt` 且比视频新时自动复用，**跳过整片读盘**（探测 0.5 s）；`--refresh-source` 强制重抽。
  复用带守卫：`<基名>.source.json` 记录条数与是否 `--limit` 截断过，**截断产物永不复用**（否则会静默只出前 N 条字幕）。
- **同步校验 `--verify-sync auto|on|off`**：用 ffsubsync（音频 VAD + FFT）验证字幕是否真的对得上音轨，偏了就自动校正。`auto`（默认）只在**外挂字幕**时触发——内嵌轨本身就是视频的一部分，不需要验。
  双证实测：正对照（内嵌轨）报 `offset 0.000 / scale 1.0000`；反证（人为挪 +3.5 s）报 `offset -3.500` 并成功校正。成本约 13 s。

**关于"换 mkvextract 会不会更快"——实测否决**：本机冷盘全文件顺序读 2.79 GB 要 **22.3 s（134 MB/s）**，而页缓存热时 ffmpeg 抽完整条字幕轨只要 **0–1 s**。抽取耗时**全部是外置机械盘的 I/O**，不是工具效率；MKV 字幕块散布全片（每约 3.4 s 一条），任何正确的抽取器都得扫过绝大部分簇，换 MKVToolNix 也一样。**真正的省法只有一条：别重复读——用上面的源字幕复用。**

**四个实测反例（都踩过，别再走）**：
- **JSON 数组协议 → 模型拆句 → 二分永不收敛**：给它 20 条，它偶尔回 23/25 条（把一句拆成两条），拆到 5 条还回 8 条，白烧 49 次请求、400 条花了 142 秒。**改用编号标记协议**（每行前缀 `[[n]]`，按标记归位，拆句也能合并回同一条）后：同样 400 条只用 32 秒，全片 765 条 54 秒。
- **批大小 40 → 二分放大**：20 是实测最优点；40 只会让拆句更多、二分更多。
- **逐行翻译 → 429**：限额 **60 次/分钟 + 3.5 万 token/分钟**，765 次单行请求必被限流。脚本内置限速器（默认 50 次/分钟）。
- **"末尾补空"对齐 → 整批错位**：条数不符时补空会把译文按错位映射出去（中文里混英文）。改为**二分 + 成批补译**，绝不补空。

**小样验证**：改 prompt/协议时别拿整片试——`--limit 120` 只翻前 120 条，20 秒出结果；确认后再跑全片（译文有缓存，重复跑不重付）。

### 交付：只出字幕文件，**不要往播放器里装**

**默认只做一件事**：把 `<视频基名>.srt` 写到视频同目录（脚本 `--out` 默认就是视频目录）。同名同目录是**跨平台通行**的自动加载约定，**不要**去写任何播放器的私有缓存/库目录——播放器不止一种，替用户选播放器是越界（2026-09-27 被明确要求回退：曾把成品拷进 selfvideo 缓存，已删）。

| 场景 | 命名 / 位置 | 说明 |
|---|---|---|
| 通用播放器（mpv / IINA / VLC / MPC-HC / PotPlayer / Infuse） | 与视频**同名同目录** `<视频基名>.srt` | 脚本默认产出，零配置自动加载 |
| 媒体库（Plex / Jellyfin / Emby） | 同目录 + **语言后缀**：`<视频基名>.zh.srt`；双语用 `.zh-en.srt` | 扫描后作为外挂字幕轨。语言后缀要紧贴基名（别写成 `.zh .srt`）；只放 `<基名>.srt` 时可能被标成"未知语言" |
| 手动加载 | 任意路径 | 播放器 `--sub-file=`、拖入窗口、或字幕菜单选文件 |

- 跨平台性来自文件本身：**UTF-8 无 BOM、LF 换行**的 SRT，主流播放器与媒体服务器通用。
- 用户**点名**要装进某个播放器时，才按那个播放器的文档装（路径随时会变，不要写死在技能里）。
- 需要给媒体库用的语言后缀版时，让用户自己 `cp`（或按上面表格命名），**本技能不主动制造第二份**。

### 翻译后端选型（2026-09-27 实测，Apple M4/32GB，20 条真实字幕）

| 后端 | 模型 | 20 条耗时 | decode | 内存 | 一集(765条) | 质量（中立裁判，位置互换） |
|---|---|---|---|---|---|---|
| **云端（默认）** | `qwen-mt-flash` | **1.8 s** | — | — | **≈70 s / ≈0.03 元** | 略优：更口语化、会本地化人名 |
| 本地 | Hy-MT2-7B GGUF Q4_K_M（llama.cpp） | 13.2 s | 19.95 tok/s | 4.06 GB | ≈5.7–8.4 分 | 与云端基本持平（6:6、6:7），术语更准（`(RETCHES)`→「干呕声」） |
| 本地 | Hy-MT2-7B MLX 8bit | 22.5 s | 12.0 tok/s | 8.26 GB | ≈14 分 | 大致同级（结论受 20 条小样本/裁判差异影响） |

**结论：生产用云端 `qwen-mt-flash`；离线/隐私场景用本地 llama.cpp Q4**（本地服务起法：`llama-server -m <gguf> --port 8080 -ngl 99`，再 `--backend local`）。

### 这条分支的坑（实测）

- **`ffmpeg`/`ffprobe` 必须是好的**：本机曾断链（ffprobe 指向已删 Cellar、ffmpeg 缺 libx264），2026-09-27 `brew reinstall ffmpeg` 修好，二者均为 9.0.2（`/opt/homebrew/bin`）。**一律用 ffmpeg/ffprobe，不用 PyAV**。
- **子进程一律带 PATH（`tool_env()`）**：DSH 里 bash 子进程的 PATH 可能只有 `/usr/bin:/bin:/usr/sbin:/sbin`。ffsubsync 这类工具**内部按名字调 `ffmpeg`**，不前置 `/opt/homebrew/bin` 就会 `exit=1` 静默空转（实测：同步校验变成"校验未完成，保持原字幕"）。脚本已统一走 `tool_env()`（前置 ffmpeg/uv/node/bl 目录），失败时还会打印真实 stderr。
- **`bl` 是 npm shim（`#!/usr/bin/env node`）**：PATH 里没有 node 时它以 exit=127 **静默空返回**（症状：整批翻译返回 0 条却无报错）。脚本用 `bl_env()` 把 bl 所在目录前置进子进程 PATH。
- **同步 ASR 上限 300 秒**；要整片一次过且要时间戳，用异步模型（`*-filetrans`/`fun-asr`/`paraformer-*`）配 `--out <json>`。`bl speech` **没有翻译子命令**。
- **`bl text chat --output json` 可能直接输出模型正文（JSON 数组）**；`qwen-mt-*` 系列**不接受 system 角色**（报 `Role must be in [user, assistant]`）——脚本对三种返回形态都做了兼容。
- **`bl text chat` 默认超时偏短**：并发下会 `ETIMEDOUT`，必须 `--timeout 180`；单批失败重试 3 次，仍失败二分（末尾补空会让整批译错位）。
- **模型下线**：`qwen-mt-turbo`、`gummy-chat-v1`、`gummy-realtime-v1` 于 **2026-10-10** 下线（目录字段 `upcomingOfflineAt`，公告 [aliyun.com/notice/118434](https://www.aliyun.com/notice/118434)）；替代见 `ASR-API.md`。
- **ASR 的 `words[]` 两个方向都不可靠，字幕文本必须取句级 `text`**（2026-09-27 实测 E08 全片 823 句）：标点**不在** `words[].text` 里而单独放 `punctuation`（实测 1323 个），只拼 text 会产出"整片 631/631 条零标点"的字幕；词本身还会**拆开**（`gl`+`enn`、`pr`+`ou`+`der`、`9`+`0`、`er`+`ie`，须无空格相接）或**缺前导空格**（`in`+`90`、`restaurant`+`You`、`And`+`I`，须有空格）——本地规则分不出这两种（直接 join 得 28 条 `in90`，只补空格得更多 `gl enn`）。现在 `_split_long` 把词对齐到句级 text 的"字母数字投影"，**显示文本一律取自 text、words 只供时间**；对齐失败才退回按字符比例切。标点一并恢复了"按小句断句"的能力——旧版丢标点后断句判断永不触发，长句按 84 字硬切出半句（`…i couldn`、`dollar business in`），半句各自送 MT 就被脑补出原文没有的意思。**标点的上限就是句级 text 给的上限**：ASR 本身没打标点的句子（实测 684 条里 46 条）无从补；`words[].punctuation` 不参与显示文本。
- **自家产物会被当成"外挂字幕"读回来，且能架空显式参数**（两层，2026-09-27 各踩一次）：① `<基名>.source.srt`（纯英文）能绕过 `looks_bilingual` 守卫，被 sidecar 兜底 glob `<基名>*` 命中；② 上一次 `--no-translate` 留下的**单语** `<基名>.srt` 同样会被当成外挂字幕。两者的后果一样：`--asr-json`/`--refresh-source` 被静默架空、重跑仍出旧文本。修法两层：`sidecar_path()` 用 `_is_own_artifact()` 排除 `.source`/`.mono`；**`--asr-json` 直接强制 `--source asr`**（显式指定转写结果时，缓存、内嵌轨、外挂字幕一律让位）。判据：跑完先看日志 `[src]` 那行写的是什么来源，别只看退出码 0。
- **单条字幕曾让自检崩**：`check_timeline` 的 `max(gaps)` 在只有 1 条 cue 时抛 `ValueError: max() iterable argument is empty`（实测：短夹具只有一句台词就必崩，试跑 agent 靠自己打补丁绕过）。已改成空列表安全取值。
- **`--no-log`**：评测/CI 跑时加上，不追加 `~/.dsh/douyin-timing.log`（默认会追加，属宿主状态污染）。
- **平台支持只承诺实测过的**：macOS 是验证过的路径（含整集 44 分钟 MKV）；Linux 走同一 ffmpeg/ffprobe 链路但未实测；Windows 的工具发现（PATH 优先 + `bl.cmd` 经 `cmd /c` + `os.pathsep`）已按约定写好，**本机无 Windows，未实测**。任何平台排查的第一步都是确认 `ffprobe`/`bl` **能被直接执行**，而不是只 `which` 得到。
- **`[mt] N 条只回 0 条 → 二分` 的真相是"标记不全"**：那是模型把短句并进相邻行、少回一个 `[[n]]`（实测约 1/4 的批，二分后全部补齐，成品零漏译），不是请求失败。日志现在直接写明缺几个标记。
- macOS 无 `timeout`；整片转写/翻译放后台作业跑。

## 计时

步骤 1 开头 `SECONDS=0`；每阶段完成即记录并归零（bash 的 `SECONDS` 赋值即重置，整秒粒度够用，不引外部依赖）：`t_triage`（步骤 1）、`t_dl`/`t_conv`（2a 下载/转码）、`t_asr`（2a 并行 ASR 批的墙钟时间）、`t_imgs`（2b 读图）。完成标准：耗时行同时出现在输出与日志，缺一即未 done。

汇总后追加一行 TSV 到 `~/.dsh/douyin-timing.log`（制表符分隔：日期、视频 ID、各阶段秒数、总计、正文字数），供跨次对比：哪段占主导、cookie 失效是否拖慢 triage。
