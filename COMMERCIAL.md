# 商业授权 / Commercial Licensing

Copyright (C) 2026 yangzhuo

本项目采用 **双许可（dual licensing）**：开源使用走 AGPL-3.0，商业使用需另行购买授权。

## 一、开源使用：AGPL-3.0

个人学习、研究、自用，以及任何**愿意遵守 AGPL-3.0 全部条款**的使用，都**免费**。

AGPL-3.0 是强 copyleft 协议，关键义务两条：

1. **分发即开源**：你把本项目（或其修改版）分发出去，必须一并提供完整对应源码，且同样以 AGPL-3.0 授权。
2. **网络服务也算分发**（第 13 条，AGPL 相对 GPL 的核心差异）：你修改本项目后**把它作为网络服务提供给他人使用**
   （SaaS、网页/接口服务、在线工具等），也必须向这些使用者提供完整对应源码。

完整条款见 [`LICENSE`](LICENSE)（AGPL-3.0 官方原文，未作任何改动）。

## 二、商业使用：需购买授权

**以下情形必须取得商业授权**：

- 将本项目（或其修改版）**集成进闭源产品**并对外销售或提供；
- 将本项目作为**网络服务对外运营**，而**不愿**按 AGPL-3.0 第 13 条公开自己的完整源码；
- 在**企业内部作为商业流程的一环**使用，且不愿承担 AGPL-3.0 的开源义务；
- 需要**免除 AGPL-3.0 义务**、需要**商业条款/质保/技术支持**，或需要**定制开发**的任何情形。

商业授权按项目/用途/规模**单独议定**（可含闭源使用许可、技术支持、定制开发）。

**联系方式**：GitHub [@ice5631024721](https://github.com/ice5631024721)（商业授权请通过主页所示邮箱联系，附上用途与规模说明）。

## 三、边界说明（避免误解）

| 事项 | 说明 |
| --- | --- |
| **调用第三方 API 的费用** | 本项目只是一个**客户端工具**：ASR 与翻译走你自己申请的百炼（DashScope）额度，**费用与配额由你自己的账号承担**，与本项目的授权无关。 |
| **你处理的内容** | 你用本工具生成的字幕/文字稿属于**你处理的内容**，本项目不对其主张任何权利，也不做版权判断——请自行确认使用范围。 |
| **第三方依赖** | `ffmpeg` / `ffprobe`、`bl`（bailian CLI）等为独立程序，各自遵循其自身许可（ffmpeg 为 LGPL/GPL，视构建而定）。本项目通过命令行调用它们，不链接、不修改、不打包分发。 |
| **本文件性质** | 本文件是对双许可模式的**说明**，不构成法律意见，也不替代 [`LICENSE`](LICENSE) 中的正式条款。若两者冲突，以 AGPL-3.0 原文为准；商业授权以双方签署的协议为准。 |

---

## Commercial Use Requires a Paid License

This project is **dual-licensed**: free under AGPL-3.0 for open-source use, **paid** for commercial use.

Commercial use — including embedding it in a closed-source product, or operating it as a network service
without releasing your complete source under AGPL-3.0 §13 — requires a separate commercial license.
Contact: GitHub [@ice5631024721](https://github.com/ice5631024721).
