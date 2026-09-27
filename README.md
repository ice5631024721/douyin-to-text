# omnisub

把视频/图片转成文字与字幕 —— 一个面向 AI Agent 的 **Agent Skill**（Claude Skill / DSH Skill 格式）。

覆盖三条分支：

| 输入 | 处理路径 |
| --- | --- |
| 抖音链接 · 有声视频（`video/…`） | 分享页取无水印直链 → 下载 → `afconvert` 转 16k 单声道 wav → 分段**并行** ASR |
| 抖音链接 · 单图 / 轮播图（`note/…`） | 逐张下载图片 → 视觉读图，**图中文字一并转出** |
| **本地视频文件（MKV/MP4…）** | **复用优先**出**双语 ASS 字幕**：内嵌字幕轨 → 同目录外挂字幕 → 都没有才 ASR；再批量并行翻译 |

## 本地视频 → 任意语言对的双语 ASS 字幕

**任意源语言 → 任意语言对**（`--subtitles`，默认 `en,zh`）。语言对里**排第一的那行在最上**，
用醒目的 `Upper` 样式；其余行用 `Lower`。源语言若在语言对里，该行**直接复用原文**，不翻译、不花钱。

| 源语言 | `--subtitles en,zh`（默认）的实际行为 |
| --- | --- |
| 英文 | 上行=英文字幕原文，下行=中文译文（**英上中下**） |
| 中文 | 上行=中文→英文译文，下行=中文字幕原文（**英上中下**） |
| 日文/韩文/俄文/阿拉伯文/泰文… | 上行=→英文译文，下行=→中文译文（原文不显示） |
| 法文/德文/西班牙文…（拉丁字母语系） | 自动模式判不出具体语种时记作"未知"，**两种语言都真翻译**；提示词里要写对源语言名就显式传 `--source-lang fr` |

> 源语言判定：有独占文字区的语言（韩/日/俄/阿/希/泰/天城文/汉字）由 Unicode 区块直接判定；
> 拉丁字母语系之间区块分不出来，自动模式用英文虚词密度筛一轮，明显不像英文就记作"未知"
> （宁可多翻一种，也不把法文原文填进英文行）。繁体中文是独立目标：`zh-TW` / `zh-Hant` → `zh-Hant`（繁體中文）。

**能拿到现成字幕就绝不转写**——人工字幕的时间轴比 ASR 更准，而且省掉整片转写成本：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 内嵌字幕轨 | MKV/MP4 里的 srt/ass/文本轨（含 SDH）→ 直接复用原时间轴 |
| 2 | 同目录外挂字幕 | `<视频基名>.srt` / `.ass` / `.vtt` |
| 3 | ASR 转写 | 前两者都没有才走：`ffmpeg` 解 16 kHz 单声道 FLAC → `bl speech recognize` 异步 filetrans（句级 `begin_time/end_time` ＋词级 `words[]`）→ 按字幕规范切 cue |

拿到原文后统一用 `bl text chat`（默认 `qwen-mt-flash`）批量**并行**翻译：**20 条/批 × 4 并发**，
走编号标记协议（每行前缀 `[[n]]`，模型拆句也能按标记归位）；条数不符递归二分、漏条成批补译，
配 50 次/分钟限速器（官方限额 60）。**方向由语言对决定**，进请求体（不是写死在提示词里）。

```bash
python3 \
  scripts/omnisub.py "<视频>" --out <输出目录> \
  [--source auto|embedded|sidecar|asr] [--source-lang auto|en|zh|ja|ko|fr|…] \
  [--subtitles en,zh] [--sub-index N] [--no-translate] [--asr-json <已有.json>] \
  [--no-log] [--cache-dir <目录>]
```

**产出只有一个文件**：视频同目录的 `<视频基名>.ass`（双语；`--no-translate` 时是单语）。
中间产物——`<基名>.source.srt`（原文）、`.source.json`（元数据）、`.asr.json`（转写结果）、
`.<lang>.json`（按语言分的译文缓存）——默认写进**平台缓存目录**
（macOS `~/Library/Caches/omnisub/<基名>-<路径哈希>/`，Linux `$XDG_CACHE_HOME/omnisub/`，
Windows `%LOCALAPPDATA%\omnisub\Cache`），`--cache-dir` 可改。既保住"**重切不重付**"，又不往片库里堆文件。

`--asr-json` 跳过解音轨与转写，只重切/重译——它是"我就要用这份转写"的显式指令，
会强制走 ASR 分支，压过缓存复用、内嵌轨与外挂字幕。

### 样式：为什么是 ASS 而不是 SRT

**SubRip（SRT）格式没有任何样式位**——只有序号、时间轴和纯文本，字号、颜色、加粗、描边都无处安放。
"上面的字幕更醒目"这种要求只能由 **ASS/SSA** 承载。往 SRT 里塞 `<font color>` 是播放器私有行为、
支持参差不齐，不能当交付标准。所以本工具**只出 ASS**。

样式（方案 A 高对比白系，字号与描边按画面高度等比缩放，1080p / 2160p 都合身）：

| 样式 | 用在哪 | 主色 | 字号 | 字重 | 描边 |
| --- | --- | --- | --- | --- | --- |
| `Upper` | 语言对里**第一行**（在上，醒目） | 纯白 `#FFFFFF` | 5.0% 画面高 | **加粗** | 3/1080 |
| `Lower` | 其余行（在下） | 暖白 `#F0EDE6` | 4.35% 画面高 | 常规 | 2/1080 |

两行写进**同一个 Dialogue 事件**，用 `\N` 换行、`{\rUpper}` / `{\rLower}` 按行切样式：
这样两行天然是一个整体（居中堆叠、底部定位全交给 libass），换字号或换分辨率都不会散。

`--subtitles` 支持 **N 种语言**（如 `ja,en,zh` 出三行）：首行 `Upper`，其余全部 `Lower`。

### 交付：片库里只多出一个 ass，也不往播放器里装

**默认只做两件事**：① 把 `<视频基名>.ass` 写到视频同目录（脚本 `--out` 默认就是视频目录）；
② 中间产物一律进缓存目录。同名同目录是**跨平台通行**的自动加载约定，**不要**去写任何播放器的
私有缓存/库目录——播放器不止一种，替用户选播放器是越界。

| 场景 | 命名 / 位置 | 说明 |
| --- | --- | --- |
| 通用播放器（mpv / IINA / VLC / MPC-HC / PotPlayer / Infuse） | 与视频**同名同目录** `<视频基名>.ass` | 脚本默认产出，零配置自动加载；ASS 样式由 libass 渲染 |
| 媒体库（Plex / Jellyfin / Emby） | 视各自支持度而定 | **外挂 ASS 在媒体库里支持度不一**（Plex 可能转码/烧录，Jellyfin 较完整）。需要时自行转成 SRT（会丢样式）或封装进容器 |
| 手动加载 | 任意路径 | 播放器 `--sub-file=`、拖入窗口、或字幕菜单选文件 |

跨平台性来自文件本身：**UTF-8 无 BOM、LF 换行**的 ASS，主流桌面播放器通用。

## 为什么不用 yt-dlp

yt-dlp 的 Douyin 提取器当前被 **a_bogus 签名风控**挡死（2026-09 实测：master 分支 + 登录 cookie 均返回 403）。本 Skill 走经过验证的**分享页 SSR 路线**：`www.iesdouyin.com/share/<kind>/<id>/` 的页面里内嵌 `window._ROUTER_DATA`，其中 `item_list[0]` 含文案、播放直链与图片列表。

## 快速开始

```bash
# 1. 安装 skill（放到你的 skills 目录）
cp -r omnisub ~/.dsh/skills/        # 或 ~/.claude/skills/

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
COMMERCIAL.md            # 双许可说明：AGPL-3.0 开源免费 / 商业使用需付费授权
LICENSE                  # AGPL-3.0 官方原文（未作改动）
scripts/
  omnisub.py             # 本地视频 → 双语 ASS（复用优先，ffmpeg/ffprobe + bailian CLI）
evals/
  eval.yaml              # skill-up 评测套件（全部离线）
  cases/*.yaml           # 用例定义与断言
  fixtures/repos/        # 离线夹具：合成分享页 HTML、汇总输入、2 段合成 MKV
  fixtures/scripts/      # 不依赖 agent 的确定性闸门：selftest-omnisub.sh + selftest-langs.py
  README.md              # 评测设计与跑法
```

## 评测

用例**全部不联网、不下载、不写宿主日志**（会动工作区文件），验 Skill 的契约面：
分享页解析、cookie 失效守卫、汇总模板，以及本地视频字幕分支的**复用优先级**与**显式参数权威性**。

```bash
skill-up validate evals/eval.yaml
skill-up run evals/eval.yaml --output-dir evals/.skill-up-workspace
```

另有一份**不依赖 agent 的确定性闸门**（零网络零计费，约 15 秒），走产出路径、无法绕开：

```bash
# 显式指定 3.10+ 解释器：macOS 自带 /usr/bin/python3 是 3.9，会得到一片"假红"
PYTHON=/abs/path/python3 bash evals/fixtures/scripts/selftest-omnisub.sh
```

它覆盖两类缺陷：
- **端到端可观测的**（`selftest-omnisub.sh`，20 项）：显式参数不被自家产物架空、单条 cue 不崩、
  片库只多出一个 `.ass`、源语言判定与语言对解析、单语输出不覆盖双语成品。
- **端到端看不出来的**（`selftest-langs.py`，62 项）：旧版把**翻译方向写死在提示词里**
  （"把下面每一行英文翻译成简体中文"），`--target-lang` 根本不进请求 —— 退出码 0、日志正常、
  产出是"中文原样重复两遍"的假双语。方向必须直接查请求体才能拦住，所以这条闸门断言
  `_cloud_payload` 的提示词文本、漏译判据的方向性（正证零请求 + 反证必须抓到），
  以及 ASS 的样式字段（字号/配色/字重/描边/转义）。

> ⚠️ `environment.type: none` **不隔离** —— 被测 agent 直接跑在宿主机上。
> 本地视频那几条用例会在工作区里跑真脚本（写 `.ass`/`.json`）；用例 prompt 已显式要求 `--no-log`，
> 不再写宿主机的时间日志。若要更强隔离请改 `environment.type: docker`。详见 [`evals/README.md`](evals/README.md)。

## 安全设计

这个 Skill 会接触登录态 cookie 与付费 ASR / 翻译额度，因此有几条**刻意为之**的约束：

| 做法 | 原因 |
| --- | --- |
| **cookie 不落仓库**，只从 `~/.dsh/douyin-cookies.txt` 读取（`chmod 600`），且必须由用户手动提供 | 会话 cookie 等同账号凭据；写进 skill 目录会被 `git add .` 带走 |
| **API key 不落任何文件**，从既有 env 派生（`grep -E '^OPENAI_API_KEY=' ~/.agentmemory/.env \| cut -d= -f2`） | 避免在 skill 里新增一份密钥副本 |
| **不出货任何字幕/转写正文**：仓库只有工具与文档，生成的 `.ass` / `.srt` / `.asr.json` 一律不提交 | 影视/他人作品的字幕属于受版权保护的内容 |
| **评测夹具全部合成**：`share.html` 里的作者、文案、ID、直链都是占位值（`example.invalid`），不含任何真实抖音用户数据 | 真实分享页 HTML 含作者昵称/uid/sec_uid/带签名 CDN 直链，属于第三方 PII，不适合公开 |
| **评测用例不触碰真实链路** | 抖音风控 + cookie 时效 + ASR 计费会让它变成 flaky case，不适合当 CI 门禁；真实链路走人工冒烟 |
| **`.gitignore` 兜底**：cookie / 运行日志 / ASR 与字幕产物 / 评测 workspace 一律忽略 | 防止运行一次就把凭据或他人内容提交上去 |

**已知仍需注意的两点**（使用者自查）：

1. 下载的媒体落在 `/tmp`，属于**临时目录但不自动清理**；抖音分支步骤 1 开头有 `rm -f /tmp/dy_*` 清场，避免跨运行残留被误复用。若你的机器上有其他程序使用同名前缀，请改路径。
2. 本 Skill 会把提取到的正文原样返回，**不做版权判断**。用它处理他人作品时，请自行确认使用范围。

## 环境要求与平台支持

- `python3` 3.10+（`scripts/omnisub.py` 只用标准库）
- **`ffmpeg` / `ffprobe`**：探轨、抽内嵌字幕、抽音轨。脚本按 **PATH 优先**解析，再按平台兜底
  （macOS `/opt/homebrew/bin`、Linux `/usr/bin`、Windows `C:\ffmpeg\bin`），不写死单一路径。
- **`bl`（bailian CLI）** ＋ 一个可用的 ASR / 文本模型额度（见 `ASR-API.md`）。
  Windows 上 npm 装出来的是 `bl.cmd`，脚本经 `cmd /c` 调用。

| 平台 | 本地视频 → 字幕 | 抖音链接分支 |
| --- | --- | --- |
| macOS | ✅ 实测（2026-09，含整集 44 分钟 MKV；中/英双向） | ✅ 实测（依赖系统原生 `afconvert`） |
| Linux | ✅ 同一 ffmpeg/ffprobe 链路，脚本无 macOS 专属调用 | ⚠️ 需自行把 `afconvert` 换成 `ffmpeg`，未实测 |
| Windows | ⚠️ 工具发现与 `.cmd` 调用已按官方约定写好，**本机无 Windows，未实测** | ⚠️ 同上，未实测 |

> 只承诺实测过的组合：macOS 是验证过的路径，Windows 分支是按约定写的 best-effort。
> 任何平台排查的第一步都是确认 `ffprobe` / `bl` **能被直接执行**（而不是只 `which` 得到）。

> 踩坑提示：`bl` 是 npm shim（`#!/usr/bin/env node`）。**PATH 里没有 node 时它以 exit=127 静默空返回**，表现为"翻译批次全部返回 0 条却无报错"。`scripts/omnisub.py` 内置 `bl_env()` 把 `bl` 所在目录前置进子进程 PATH；自行调用 `bl` 时请一并处理。

## License

**双许可**：开源使用 [AGPL-3.0](LICENSE)（免费，但分发/提供网络服务时须同样开源）；
**商业使用需购买授权**（闭源集成、SaaS 运营、企业内商业流程等）。

详见 [`COMMERCIAL.md`](COMMERCIAL.md)。
