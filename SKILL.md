---
name: omnisub
description: 视频/图片转文字与字幕入口。抖音链接（v.douyin.com 或 www.douyin.com）要求提取内容/转文字/识别图片文字时使用；本地视频文件（含 MKV）要求转写、出字幕、要时间轴、要双语/中英字幕、或指定语言字幕时同样使用。覆盖三条分支：抖音有声视频→ASR 转写（qwen-audio-3.1-asr-flash）；单图或轮播图→下载图片后视觉读图；本地视频文件→**任意源语言**转**任意语言对**的双语带样式 ASS 字幕（默认 en,zh；译文在上、原文沉底：中文片英上中下、英文片中上英下）。
---

# omnisub：视频 / 图片 → 文字与字幕

三条输入分支共用一个入口；**抖音链接 → 文字**走下面这套验证过的分享页路线。

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

## 本地视频文件 → 任意语言对的双语 ASS 字幕（时间轴与视频一一对应）

给一个本地视频（MKV/MP4 都行）产出双语字幕。**任意源语言 → 任意语言对**，默认 `en,zh`。
**复用优先：能拿到现成字幕就绝不转写。**

| 优先级 | 来源 | 做法 |
|---|---|---|
| 1 | **内嵌字幕轨** | `ffprobe` 探轨 → `ffmpeg -map 0:<idx> -c:s srt` 抽成 SRT → **复用原时间轴**，零 ASR 成本，比转写更准 |
| 2 | **同目录外挂字幕** | `<视频基名>.srt` / `.ass` / `.vtt` 直接读 |
| 3 | **ASR 转写** | `ffmpeg -vn -ac 1 -ar 16000 -c:a flac` 抽音轨 → `bl speech recognize` 异步 filetrans（句级 `begin_time/end_time` ＋ 词级 `words[]`）→ 按字幕规范切 cue |

拿到原文后翻译：**默认云端 `qwen-mt-flash`**（`bl text chat`），可 `--backend local` 切本地 llama.cpp / mlx-lm 的 OpenAI 兼容服务。

### 语言对与行序（这条分支的核心契约）

`--subtitles <语言,语言[,语言…]>` 决定**出几行**。**行序不变量：译文在上（醒目）、原文沉底**——中文片出英上中下、英文片出中上英下；`--subtitles` 的顺序决定译文行之间的次序（源不在语言对里时即整体行序）。
在上的那行用醒目的 Upper 样式。源语言若在语言对里，该行**直接复用原文**，不翻译、不花钱。

| 源语言 | 默认 `--subtitles en,zh` 的行为 |
|---|---|
| 英文 | 上行=中文译文（大而粗），下行=英文原文（中上英下） |
| 中文 | 上行=英文译文（大而粗），下行=中文原文（英上中下） |
| 日/韩/法… | 上行=→英文译文，下行=→中文译文 |

- **行序不变量：译文在上且醒目、原文沉底**。中文片出英上中下、英文片出中上英下（截图参照）；
  源不在语言对里时全是译文、按 `--subtitles` 顺序。用户只说"出双语字幕"时不需要额外交代。
- **繁体中文是独立目标**：`zh-TW` / `zh-Hant` / `cht` 归一成 `zh-Hant`（提示词写"繁體中文"），
  不再像旧版那样静默并成 `zh`（简体）——用户点名要繁体却拿到简体且毫无提示，是实测踩过的坑。
- 源语言三级回退：`--source-lang` 显式值 → 字幕轨语言标签 → 文本启发式。
  **非中英源语言建议显式传** `--source-lang ja` 之类：ASR 语言提示与翻译提示词都会更准
  （`bl speech recognize` 的返回里**没有**识别到的语言，只能靠这三步）。
- `--source-lang auto`（默认）对 ASR **不给** `--language`，交给模型自行判定；给错提示反而伤识别率。
- **automatic 判定的能力边界（实测口径）**：
  · 有**独占文字区**的语言（韩/日/俄/阿/希/泰/天城文/汉字）由 Unicode 区块直接判定，可靠；
  · **拉丁字母语言之间无法用区块区分**（英/法/德/西共用字母）。自动模式下会用英文虚词密度筛一轮：
    像英文→按 `en` 处理；**明显不像→源语言记为"未知"**，此时语言对里**每种语言都会真翻译**。
    这是刻意的：旧版把一切非中日文本都判成 `en`，于是法文视频的"英文行"直接就是法文原文（实测证伪）。
    真要做到提示词里也写对源语言名，就显式传 `--source-lang fr`。

### 样式：只出 ASS，因为 SRT 放不下样式

**SubRip（SRT）格式没有任何样式位**——只有序号、时间轴、纯文本，字号/颜色/加粗/描边全都无处安放。
"上面的字幕更醒目"这类要求只能由 ASS/SSA 承载；往 SRT 里塞 `<font color>` 是播放器私有行为，支持参差不齐。
所以**交付物固定是 `<视频基名>.ass`，不要再额外产出一份 SRT**。

| 样式 | 用在哪 | 主色 | 字号 | 字重 | 描边 |
|---|---|---|---|---|---|
| `Upper` | 译文行（在上，醒目） | 纯白 `#FFFFFF` | 6.5% 画面高 | **加粗** | 3/1080 |
| `Lower` | 原文行（在下） | 暖白 `#F0EDE6` | 4.0% 画面高 | 常规 | 2/1080 |

两行写进**同一个 Dialogue 事件**，`\N` 换行 + `{\rUpper}`/`{\rLower}` 按行切样式——
两行天然是一个整体，底部定位交给 libass，换字号或换分辨率都不会散。
字号与描边**按画面高度等比缩放**（`PlayRes` 取自视频流的真实 width/height），1080p 与 2160p 都合身。
成品头部写有 `Title: omnisub` 产出标记，用来把自己产出的字幕从"外挂字幕"候选里排除。

```bash
python ~/.dsh/skills/omnisub/scripts/omnisub.py "<视频>" \
  --out <输出目录> [--source auto|embedded|sidecar|asr] \
  [--source-lang auto|en|zh|ja|ko|fr|…] [--subtitles en,zh] \
  [--sub-index N] [--chat-model qwen-mt-flash] [--backend cloud|local] \
  [--local-server http://127.0.0.1:8080] [--batch 20] [--workers 4] [--asr-json <已有.json>] \
  [--refresh-source] [--verify-sync auto|on|off] [--limit N] [--no-log] [--cache-dir <目录>]
```

**产出只有一个文件**：视频同目录的 `<视频基名>.ass`（双语；`--no-translate` 时是单语，且若已有双语成品则改写 `<基名>.mono.ass`）。
中间产物（`<基名>.source.srt` 原文、`.source.json` 元数据、`.asr.json` 转写结果、`.<语言>.json` 译文缓存）
默认写进**平台缓存目录**——macOS `~/Library/Caches/omnisub/`、Linux `$XDG_CACHE_HOME/omnisub/`、
Windows `%LOCALAPPDATA%\omnisub\Cache`，同一视频按 `<基名>-<路径哈希>` 分子目录。
**不要把中间产物写在视频旁边**（2026-09-27 用户明确要求：片库里只该多出一个 ass）。需要指定位置就 `--cache-dir`，
评测/CI 通常传工作区内的目录以保持密闭。

实测（一部 43.8 分钟 1080p MKV，内嵌 `eng/SDH` 字幕轨 766 条 → 清洗后 765 条）：

| 阶段 | 耗时 | 关键点 |
|---|---|---|
| 探测＋抽内嵌字幕 | **2.8 s（热）/ 16–23 s（冷盘）** | 带 `-probesize 20M -analyzeduration 20M`；主要成本是读文件：MKV 字幕包散布全片，ffmpeg 得读完 3 GB 才抽得全 |
| 翻译 765 条 | **54 s** | qwen-mt-flash，20 条/批 × 4 并发，编号标记协议 |
| 合计 | **57 s** | 全程无 ASR 调用，成本 ≈0.03 元 |

质检：765/765 条有中文、0 重叠、0 乱序、无 >15 s 空隙；时间轴 `00:00:02 → 00:43:45` 与片长 2628.032 s 对齐。

**两个省时机（第二次跑同一部片时）**：
- **源字幕复用**：抽内嵌字幕要把整片读一遍（2.79 GB 外置机械盘实测 **22 s**）。缓存目录里已有 `<基名>.source.srt` 且比视频新时自动复用，**跳过整片读盘**（探测 0.5 s）；`--refresh-source` 强制重抽。
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

**默认只做一件事**：把 `<视频基名>.ass` 写到视频同目录（脚本 `--out` 默认就是视频目录）。同名同目录是**跨平台通行**的自动加载约定，**不要**去写任何播放器的私有缓存/库目录——播放器不止一种，替用户选播放器是越界（2026-09-27 被明确要求回退：曾把成品拷进 selfvideo 缓存，已删）。同理，**不要往视频目录里堆中间产物**（`.source.srt`/`.asr.json`/`.<语言>.json` 都进缓存目录）：片库里只该多出一个 ass。

| 场景 | 命名 / 位置 | 说明 |
|---|---|---|
| 通用播放器（mpv / IINA / VLC / MPC-HC / PotPlayer / Infuse） | 与视频**同名同目录** `<视频基名>.ass` | 脚本默认产出，零配置自动加载；样式由 libass 渲染 |
| 媒体库（Plex / Jellyfin / Emby） | 视各自支持度而定 | **外挂 ASS 在媒体库里支持度不一**（Plex 可能转码/烧录，Jellyfin 较完整）。用户确需 SRT 时由用户自行转换（会丢样式） |
| 手动加载 | 任意路径 | 播放器 `--sub-file=`、拖入窗口、或字幕菜单选文件 |

- 跨平台性来自文件本身：**UTF-8 无 BOM、LF 换行**的 ASS，主流桌面播放器通用。
- 用户**点名**要装进某个播放器时，才按那个播放器的文档装（路径随时会变，不要写死在技能里）。
- 用户点名要 SRT（例如媒体库只吃 SRT）时，**如实告知 SRT 会丢掉全部样式**（字号/配色/加粗/描边），再按其选择交付。

### 翻译后端选型（2026-09-27 实测，Apple M4/32GB，20 条真实字幕）

| 后端 | 模型 | 20 条耗时 | decode | 内存 | 一集(765条) | 质量（中立裁判，位置互换） |
|---|---|---|---|---|---|---|
| **云端（默认）** | `qwen-mt-flash` | **1.8 s** | — | — | **≈70 s / ≈0.03 元** | 略优：更口语化、会本地化人名 |
| 本地 | Hy-MT2-7B GGUF Q4_K_M（llama.cpp） | 13.2 s | 19.95 tok/s | 4.06 GB | ≈5.7–8.4 分 | 与云端基本持平（6:6、6:7），术语更准（`(RETCHES)`→「干呕声」） |
| 本地 | Hy-MT2-7B MLX 8bit | 22.5 s | 12.0 tok/s | 8.26 GB | ≈14 分 | 大致同级（结论受 20 条小样本/裁判差异影响） |

**结论：生产用云端 `qwen-mt-flash`；离线/隐私场景用本地 llama.cpp Q4**（本地服务起法：`llama-server -m <gguf> --port 8080 -ngl 99`，再 `--backend local`）。

### 这条分支的坑（实测）

- **翻译方向曾被写死在提示词里（本项目最严重的一次缺陷，2026-09-27）**：`_cloud_payload()` 对 `qwen-mt-*` 直接拼死字符串「把下面每一行**英文**翻译成**简体中文**」，`--target-lang` 根本没进请求。后果：中文源配"要英文"的产出是**中文原样重复两遍的假双语**——退出码 0、日志正常、耗时正常，端到端完全看不出来，只有直接查请求体才发现。修法：方向由 `src_lang`/`tgt_lang` 参数决定，且**必须与语言对一致**；同时用 `selftest-langs.py` 把"en→zh 提示词逐字节一致 + zh→en 方向正确"钉成闸门。
- **漏译判据不能假设目标语言是中文**：`repair_missing()` 旧版判"漏译"的条件是"译文里没有汉字"，在 zh→en 方向下**每条正确的英文译文都不含汉字** → 全片被判漏译，白跑两轮补译（数百次请求），而补译结果又因同一条汉字判据被丢弃。现改为跟着 `tgt_lang` 走（`looks_like_lang`），并加了"纯符号行不算可翻译行"的护栏（`♪♪♪` 不该触发补译）。
- **`--no-translate` 的防覆盖守卫不能只靠"含汉字"**：旧版用 `looks_bilingual()`（汉字符占比 >30%）判断"已有双语成品"，于是语言对不含中文时（如 `--subtitles en,ja`）成品会被静默覆盖。现在以文件头里的**产出标记**（`Title: omnisub`）为主判据。
- **译文缓存必须校验源语言**：缓存文件名只有目标语言（`<基名>.<语言>.json`），同一份文本从 zh→en 与 en→zh 的译文完全不同。旧缓存的元数据里没有 `src_lang`，若按"缺省即相同"放行就会跨方向串用；现在**要求该字段显式相等**，缺失一律视为不匹配、重新翻译。
- **`ffprobe` 不请求 width/height 就拿不到真实画面尺寸**：`ffprobe_json()` 的 `-show_entries` 没带 `width,height` 时，`video_size()` 会一直回落到 1920x1080（实测 3840x1634 的宽银幕片也报 1080）。PlayRes 与字号/描边都按它换算，必须请求真实尺寸。另：本机 ffmpeg **没有编译 libass**（`ffmpeg -filters` 里没有 `subtitles` 滤镜），所以**不能用 ffmpeg 预渲染 ASS 来验证样式**，要验证得走真实播放器。
- **"任意语言"曾经是假的：非拉丁非汉字字幕被整片丢弃（2026-09-27 CR 抓到的致命项）**：`normalize_cues()` 的"这条不是空的"判据写死 `[0-9A-Za-z\u4e00-\u9fff]`，于是**韩/俄/阿/泰/希腊/天城文/纯假名日文**全被当成纯符号条丢掉——韩语 ASR 会直接 `没有切出任何字幕条`、退出码 1，内嵌轨与外挂字幕两条路同样全废。修法：判据换成与文字无关的 `[^\W_]`（任意文字或数字都算有内容），而"是不是纯符号行"（♪♪♪）仍照旧拦掉。**判据必须用 Unicode 属性，不要写死字母表。**
- **漏译判据不能只认中英**：`looks_like_lang()` 旧版只认 zh/en，于是 `--subtitles en,ja` 这类目标下**每条译文都被判成漏译**——固定白烧两轮补译，真漏译还修不好（补译结果被同一条判据丢弃），stderr 还谎报"仍有 N 条不像X"。现按目标语言的**独占文字区**判定（韩→谚文、日→假名、俄→西里尔…；拉丁语系→有拉丁字母且无其它文字区）。
- **`ffmpeg`/`ffprobe` 必须是好的**：本机曾断链（ffprobe 指向已删 Cellar、ffmpeg 缺 libx264），2026-09-27 `brew reinstall ffmpeg` 修好，二者均为 9.0.2（`/opt/homebrew/bin`）。**一律用 ffmpeg/ffprobe，不用 PyAV**。
- **子进程一律带 PATH（`tool_env()`）**：DSH 里 bash 子进程的 PATH 可能只有 `/usr/bin:/bin:/usr/sbin:/sbin`。ffsubsync 这类工具**内部按名字调 `ffmpeg`**，不前置 `/opt/homebrew/bin` 就会 `exit=1` 静默空转（实测：同步校验变成"校验未完成，保持原字幕"）。脚本已统一走 `tool_env()`（前置 ffmpeg/uv/node/bl 目录），失败时还会打印真实 stderr。
- **`bl` 是 npm shim（`#!/usr/bin/env node`）**：PATH 里没有 node 时它以 exit=127 **静默空返回**（症状：整批翻译返回 0 条却无报错）。脚本用 `bl_env()` 把 bl 所在目录前置进子进程 PATH。
- **同步 ASR 上限 300 秒**；要整片一次过且要时间戳，用异步模型（`*-filetrans`/`fun-asr`/`paraformer-*`）配 `--out <json>`。`bl speech` **没有翻译子命令**。
- **`bl text chat --output json` 可能直接输出模型正文（JSON 数组）**；`qwen-mt-*` 系列**不接受 system 角色**（报 `Role must be in [user, assistant]`）——脚本对三种返回形态都做了兼容。
- **`bl text chat` 默认超时偏短**：并发下会 `ETIMEDOUT`，必须 `--timeout 180`；单批失败重试 3 次，仍失败二分（末尾补空会让整批译错位）。
- **模型下线**：`qwen-mt-turbo`、`gummy-chat-v1`、`gummy-realtime-v1` 于 **2026-10-10** 下线（目录字段 `upcomingOfflineAt`，公告 [aliyun.com/notice/118434](https://www.aliyun.com/notice/118434)）；替代见 `ASR-API.md`。
- **ASR 的 `words[]` 两个方向都不可靠，字幕文本必须取句级 `text`**（2026-09-27 实测 E08 全片 823 句）：标点**不在** `words[].text` 里而单独放 `punctuation`（实测 1323 个），只拼 text 会产出"整片 631/631 条零标点"的字幕；词本身还会**拆开**（`gl`+`enn`、`pr`+`ou`+`der`、`9`+`0`、`er`+`ie`，须无空格相接）或**缺前导空格**（`in`+`90`、`restaurant`+`You`、`And`+`I`，须有空格）——本地规则分不出这两种（直接 join 得 28 条 `in90`，只补空格得更多 `gl enn`）。现在 `_split_long` 把词对齐到句级 text 的"字母数字投影"，**显示文本一律取自 text、words 只供时间**；对齐失败才退回按字符比例切。标点一并恢复了"按小句断句"的能力——旧版丢标点后断句判断永不触发，长句按 84 字硬切出半句（`…i couldn`、`dollar business in`），半句各自送 MT 就被脑补出原文没有的意思。**标点的上限就是句级 text 给的上限**：ASR 本身没打标点的句子（实测 684 条里 46 条）无从补；`words[].punctuation` 不参与显示文本。
- **自家产物会被当成"外挂字幕"读回来，且能架空显式参数**（三层，2026-09-27 各踩一次）：① `<基名>.source.srt`（纯英文）能绕过 `looks_bilingual` 守卫，被 sidecar 兜底 glob `<基名>*` 命中；② 上一次 `--no-translate` 留下的**单语**成品同样会被当成外挂字幕；③ 换成 ASS 之后又冒出新入口——`sidecar_path()` 的**精确同名**路径只查了 `looks_bilingual()`（靠"含大量汉字"），于是语言对不含中文时（如 `--subtitles en,ko`）会把**自己刚交付的 `<基名>.ass`**当外挂原文读回来，重跑变成自我翻译。三者的后果一样：`--asr-json`/`--refresh-source` 被静默架空、重跑仍出旧文本。修法：精确同名与 glob 兜底**两条路径都要过** `_is_own_artifact()`（`.source`/`.mono` 名字 + ASS 头部的产出标记，标记按整行匹配且只对 .ass/.ssa 生效，免得真外挂字幕的台词里出现该词就被误伤）；**`--asr-json` 直接强制 `--source asr`**（显式指定转写结果时，缓存、内嵌轨、外挂字幕一律让位）。判据：跑完先看日志 `[src]` 那行写的是什么来源，别只看退出码 0。
- **单条字幕曾让自检崩**：`check_timeline` 的 `max(gaps)` 在只有 1 条 cue 时抛 `ValueError: max() iterable argument is empty`（实测：短夹具只有一句台词就必崩，试跑 agent 靠自己打补丁绕过）。已改成空列表安全取值。
- **`--no-log`**：评测/CI 跑时加上，不追加 `~/.dsh/omnisub-timing.log`（默认会追加，属宿主状态污染）。
- **中间产物的落脚点**：默认 `~/Library/Caches/omnisub/<基名>-<路径哈希>/`（Linux/Windows 见上），`--cache-dir` 可改。写在视频旁边会让片库每部片多出 4 个文件（2026-09-27 用户明确要求改掉）；评测里统一 `--cache-dir .cache` 保持密闭。
- **平台支持只承诺实测过的**：macOS 是验证过的路径（含整集 44 分钟 MKV）；Linux 走同一 ffmpeg/ffprobe 链路但未实测；Windows 的工具发现（PATH 优先 + `bl.cmd` 经 `cmd /c` + `os.pathsep`）已按约定写好，**本机无 Windows，未实测**。任何平台排查的第一步都是确认 `ffprobe`/`bl` **能被直接执行**，而不是只 `which` 得到。
- **`[mt] N 条只回 0 条 → 二分` 的真相是"标记不全"**：那是模型把短句并进相邻行、少回一个 `[[n]]`（实测约 1/4 的批，二分后全部补齐，成品零漏译），不是请求失败。日志现在直接写明缺几个标记。
- macOS 无 `timeout`；整片转写/翻译放后台作业跑。

## 计时

步骤 1 开头 `SECONDS=0`；每阶段完成即记录并归零（bash 的 `SECONDS` 赋值即重置，整秒粒度够用，不引外部依赖）：`t_triage`（步骤 1）、`t_dl`/`t_conv`（2a 下载/转码）、`t_asr`（2a 并行 ASR 批的墙钟时间）、`t_imgs`（2b 读图）。完成标准：耗时行同时出现在输出与日志，缺一即未 done。

汇总后追加一行 TSV 到 `~/.dsh/omnisub-timing.log`（制表符分隔：日期、视频 ID、各阶段秒数、总计、正文字数），供跨次对比：哪段占主导、cookie 失效是否拖慢 triage。
