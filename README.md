# douyin-to-text

给一个抖音链接，产出完整内容文本 —— 一个面向 AI Agent 的 **Agent Skill**（Claude Skill / DSH Skill 格式）。

覆盖两条分支：

| 链接类型 | 处理路径 |
| --- | --- |
| 有声视频（`video/…`） | 分享页取无水印直链 → 下载 → `afconvert` 转 16k 单声道 wav → 分段**并行** ASR |
| 单图 / 轮播图（`note/…`） | 逐张下载图片 → 视觉读图，**图中文字一并转出** |

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

# 3. 把链接交给 Agent，或按 SKILL.md 的步骤手动执行
```

ASR 依赖与备选通道见 [`ASR-API.md`](ASR-API.md)。

## 目录结构

```
SKILL.md                 # Skill 本体：triage 规则、两条分支、输出模板、计时约定
ASR-API.md               # ASR 通道说明：bailian CLI / 原生异步 API / OpenAI 兼容端点
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

这个 Skill 会接触登录态 cookie 与付费 ASR 额度，因此有几条**刻意为之**的约束：

| 做法 | 原因 |
| --- | --- |
| **cookie 不落仓库**，只从 `~/.dsh/douyin-cookies.txt` 读取（`chmod 600`），且必须由用户手动提供 | 会话 cookie 等同账号凭据；写进 skill 目录会被 `git add .` 带走 |
| **API key 不落任何文件**，从既有 env 派生（`grep -E '^OPENAI_API_KEY=' ~/.agentmemory/.env \| cut -d= -f2`） | 避免在 skill 里新增一份密钥副本 |
| **评测夹具全部合成**：`share.html` 里的作者、文案、ID、直链都是占位值（`example.invalid`），不含任何真实抖音用户数据 | 真实分享页 HTML 含作者昵称/uid/sec_uid/带签名 CDN 直链，属于第三方 PII，不适合公开 |
| **评测用例不触碰真实链路** | 抖音风控 + cookie 时效 + ASR 计费会让它变成 flaky case，不适合当 CI 门禁；真实链路走人工冒烟 |
| **`.gitignore` 兜底**：cookie / 运行日志 / ASR 产物 / 评测 workspace 一律忽略 | 防止运行一次就把凭据或他人内容提交上去 |

**已知仍需注意的两点**（使用者自查）：

1. 下载的媒体落在 `/tmp`，属于**临时目录但不自动清理**；步骤 1 开头有 `rm -f /tmp/dy_*` 清场，避免跨运行残留被误复用。若你的机器上有其他程序使用同名前缀，请改路径。
2. 本 Skill 会把提取到的正文原样返回，**不做版权判断**。用它处理他人作品时，请自行确认使用范围。

## 环境要求

- macOS（依赖系统原生 `afconvert` 做音视频转换；Linux 请自行替换为 `ffmpeg`）
- 一个可用的 ASR 通道（见 `ASR-API.md`；默认 `qwen-audio-3.1-asr-flash-filetrans`）
- `curl`、`python3`

## License

MIT
