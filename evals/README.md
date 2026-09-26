# douyin-to-text 评测套件

用 skill-up 对 `douyin-to-text` 这个 Skill 做回归评测。

## 设计取舍

三条用例**全部不联网、不下载、不写日志**，只验 Skill 的契约面：

| case | 验什么 |
| --- | --- |
| `triage-video-share-page` | 解析 `_ROUTER_DATA`、识别 video 分支、`playwm`→`play` 直链替换 |
| `cookie-invalid-guard` | `item_list` 为空时的守卫：如实报告 cookie 失效并停下，禁止编造 |
| `summary-output-format` | 「汇总输出」五字段模板是否齐全 |

**为什么不放真实抓取链路**：Douyin 风控 + cookie 时效 + ASR 计费会让它变成 flaky case，
不适合当门禁。真实链路走人工冒烟。

**夹具是合成的**：`fixtures/repos/**/share.html` 里的作者、文案、ID、直链全部是占位值
（域名统一 `example.invalid`），**不含任何真实抖音用户数据**。真实分享页 HTML 会带作者
昵称 / uid / sec_uid / 带签名的 CDN 直链，属于第三方 PII，不适合入库。

**注意 `environment.type: none` 不隔离**：被测 agent 直接在宿主机上跑。
历史上 `cookie-invalid-guard` 的 agent 真的执行了 Skill 步骤 1 的 `rm -f /tmp/dy_*`，
所以该用例的 prompt 已显式禁止执行任何 shell 命令。
若要跑会动文件系统的用例，改成 `environment.type: docker`。

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
