# omnisub 评测套件

用 skill-up 对 `omnisub` 这个 Skill 做回归评测。

## 设计取舍

六条用例**全部不联网、不下载、不写宿主日志**，只验 Skill 的契约面：

| case | 验什么 |
| --- | --- |
| `triage-video-share-page` | 解析 `_ROUTER_DATA`、识别 video 分支、`playwm`→`play` 直链替换 |
| `cookie-invalid-guard` | `item_list` 为空时的守卫：如实报告 cookie 失效并停下，禁止编造 |
| `summary-output-format` | 「汇总输出」五字段模板是否齐全 |
| `video-embedded-subtitle-reuse` | 本地视频有内嵌字幕轨时**必须复用**（零 ASR）、按「同名同目录」交付、**不得**写播放器私有目录 |
| `video-source-artifact-guard` | `--refresh-source` / `--asr-json` **必须真生效**：产出要来自 ASR JSON，不能被自家 `<基名>.source.srt` 劫持 |
| `video-bilingual-ass-contract` | 任意语言入口的离线半边：中文 ASR → 源语言判为 `zh`、默认语言对 `en,zh`、产物是带 `Upper`/`Lower` 双样式与产出标记的 **ASS**、不产 SRT、不往片库堆中间产物 |

后三条跑真脚本（`ffmpeg` 抽轨/切分，零网络、零计费），夹具是 2 段合成 MKV（3 KB / 4 KB，
见下）＋ `context.files` 内联的合成 ASR JSON。它们会写工作区里的 `.ass`/`.json`，
所以 prompt 都要求 `--no-log`（不追加宿主机 `~/.dsh/omnisub-timing.log`）。

**为什么不放真实抓取链路**：Douyin 风控 + cookie 时效 + ASR 计费会让它变成 flaky case，
不适合当门禁。真实链路走人工冒烟。

**夹具是合成的**：`fixtures/repos/**/share.html` 里的作者、文案、ID、直链全部是占位值
（域名统一 `example.invalid`），**不含任何真实抖音用户数据**；两段 MKV 是 `ffmpeg` 用
`lavfi` 现场合成的纯色视频＋静音轨（字幕轨是手写的占位英文），不含任何他人作品内容。
真实分享页 HTML 会带作者昵称 / uid / sec_uid / 带签名的 CDN 直链，属于第三方 PII，不适合入库。

**字幕/转写类夹具为什么不落成文件**：仓库 `.gitignore` 有意禁止 `*.srt` / `*.asr.json` 入库
（防他人作品的受版权内容），所以 `video-source-artifact-guard` 需要的那几份文本用
`context.files` 内联在用例 YAML 里——规则不削弱，夹具照样可复现。

**闸门有效性（双证）**：`video-source-artifact-guard` 在修复前的脚本上跑会 FAIL
（直接复现：同一命令下旧代码 `ASRJSON=NO STALE=STALE`——原文被自家 `.source.srt` 劫持；
新代码 `ASRJSON=OK STALE=CLEAN`）。判 FAIL 的依据是**产物**（文本来源 + `.source.json` 的
origin），不是退出码。另有一条防"绕闸"：用例要求走默认 auto 路由，judge 会检查 transcript
里没有 `--source asr`——旧代码下 agent 自己补这个参数就能让结果正确但没测到目标路径（实测过一次假绿）。

另外这条用例最初版本还踩过一个坑：夹具两句话被合并成 1 条 cue，触发了 `check_timeline`
单条 cue 的 `ValueError: max() iterable argument is empty`（旧代码固有缺陷），
试跑 agent 自己打补丁绕过后才"通过"。现已修掉该崩溃，并把夹具改成两句拉开 1s、
合计 85 字（合并不上），保证切成 2 条；直接双证：旧代码 `ValueError` / 新代码正常。

**注意 `environment.type: none` 不隔离**：被测 agent 直接在宿主机上跑。
历史上 `cookie-invalid-guard` 的 agent 真的执行了 Skill 步骤 1 的 `rm -f /tmp/dy_*`，
所以该用例的 prompt 已显式禁止执行任何 shell 命令。
本地视频那两条会在工作区里跑真脚本；若要更强隔离，改成 `environment.type: docker`。

## 确定性自检（不依赖 agent，也绕不开）

agent 级用例有个天然弱点：**被测路径可以被 agent 绕过去**。实测两次——旧代码下 agent 自己补
`--source asr`、或把陈旧的 `sample.source.srt` 挪走，都能拿到"正确"结果，用例不再在测目标路径；
还有一次直接超时（ERROR 而非 FAIL）。所以另固化两份**零 LLM 闸门**：

```bash
bash evals/fixtures/scripts/selftest-omnisub.sh      # 端到端契约，20 项
python3 evals/fixtures/scripts/selftest-langs.py     # 语言方向与 ASS 样式，35 项
```

**`selftest-omnisub.sh`（20 项，约 15 秒）**：① `--refresh-source --asr-json` 必须让成品来自
ASR JSON；② 同目录重跑、只给 `--asr-json`，显式参数仍要说了算；③ 只有 1 条 cue 时对齐自检不得崩；
④ **默认缓存下视频目录只许多出一个 `.ass`**（中间产物必须落到缓存目录，场景内用临时 `HOME`
验默认落点）；⑤ 源语言判定与 `--subtitles` 语言对解析；⑥ 已有双语成品时 `--no-translate`
不覆盖它（改写 `.mono.ass`）；⑦ 调用 `selftest-langs.py`。

**双证（同一命令、同一夹具，只换被测脚本）**：新版 `20 通过 / 0 失败`（exit 0）；
HEAD 版 `10 通过 / 10 失败`（exit 1——产物名还是 `.srt`、且缺语言接口）。

**`selftest-langs.py`（35 项，零网络）为什么必须单独存在**：这一版最严重的缺陷
（把翻译方向写死在提示词里、`--target-lang` 根本不进请求）**端到端是看不出来的**——
退出码 0、日志正常、耗时正常，只是产出变成"中文原样重复两遍"的假双语。要拦住它只能直接查请求体：
该闸门断言 `_cloud_payload` 的提示词文本（en→zh **逐字节**与旧版一致 + zh→en 方向正确）、
漏译判据的方向性（正证：合格英译零补译请求；反证：中文回给 en 必须被抓）、以及 ASS 的样式字段
（字号/配色/字重/描边/转义/多语言行回落）。**双证**：新版 `35 通过 / 0 失败`（exit 0）；
HEAD 版 exit 1（`_cloud_payload() takes 3 positional arguments but 5 were given`，闸门把它翻译成人话再报）。
这条是回归门禁的首选，skill-up 那 6 条用来验 agent 层的契约。

## 跑法

Skill 若位于 DSH workspace **之外**，插件工具 `skill_up_run` 因路径限制跑不了，只能用 CLI。
```bash
cd <skill 根目录>
skill-up validate evals/eval.yaml
skill-up run evals/eval.yaml --output-dir evals/.skill-up-workspace
```

引擎是 **deepseek-harness**（走 ACP）。凭据走 `~/.skill-up/credentials.yaml`，密钥不落 YAML——
`eval.yaml` 里的 `${api_key}` 由 skill-up 凭据链解析。

单跑一条：加 `--include-case-name <case-id>`。
稳定性采样：`--iteration 3`。

`eval.yaml` 里的 `DSH_BIN` / `command` / `dsh_runner.py` 是**路径占位符**，
首次使用请按本机实际安装位置覆盖（可用环境变量 `SKILLUP_DSH_BIN` / `SKILLUP_PYTHON` / `SKILLUP_DIR`）。
