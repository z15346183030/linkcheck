# 灵感来源：[lycheeverse/lychee](https://github.com/lycheeverse/lychee)

- 星标：4,058
- 许可证：Apache-2.0（仓库同时带 `LICENSE-MIT` 和 `LICENSE-APACHE`，双许可；API 返回 Apache-2.0）
- 语言：Rust
- 它承诺解决：检查文档和网站里的链接，找出坏掉的那些。

## 它做对了什么

**1. 抽取和检查彻底分开。** `collector.rs` 只负责「从输入里捞出链接」，`checker/` 只负责「把一个链接变成状态」。抽取层按输入类型分派：Markdown 走 `pulldown-cmark` 的事件流（41KB 的 `extract/markdown.rs`），HTML 走 `html5gum`，CSS 里的 `url()` 单独有 14KB 的实现，XML 和 wikilink 各有自己的抽取器。这个分层让「支持一种新格式」变成加一个抽取器，不碰检查逻辑。

**2. 状态是一等公民，不是一个布尔值。** `types/status.rs` 里的 `Status` 枚举有十个变体：`Ok` / `Error(ErrorKind)` / `RequestError` / `Timeout` / `UnknownStatusCode` / `UnknownMailStatus` / `Excluded` / `Unsupported` / `Cached`。也就是说，作者一开始就知道「失败」不止一种，并且在类型层面把它们分开了。

**3. HEAD 先探，失败再换 GET。** `checker/website.rs` 的注释写得很直白：有些服务器处理不好某些方法，所以不能只发一种请求就下结论。这一点很多链接检查器没做，于是把「服务器不喜欢 HEAD」报成死链。

**4. 主机池 + 限速 + 重试是独立模块。** `ratelimit/`、`retry.rs`、`types/redirect_history.rs` 各管一件事。重定向链被完整记录下来，所以报告里能写「跟了 10 次跳转」。

**5. 输出格式是一组可替换的 formatter。** compact / detailed / json / markdown / raw 五个，配合 `--format`。把「给人看」和「给机器看」当成两种消费方式，而不是硬编码一种。

## 它在哪里让人痛苦

**痛点一：403 不是死链，但工具把它当死链。**

> Ther are a lot of sites that use cloudflare as protection, checking sites that use cloudflare can result in 403, would be nice to have support for FlareSolverr to fix this situation.
>
> —— [#1439](https://github.com/lycheeverse/lychee/issues/1439)，2024-06-10 提出，至今 open

这条 issue 的解法是「加一个浏览器求解器去绕过 Cloudflare」——也就是装一个几十 MB 的浏览器自动化服务，只为了让一个链接检查器拿到 200。方向反了。403 的意思是「服务器不想跟这个客户端说话」，它关于的是客户端，不是页面。用户需要的不是绕过它，而是别把它算成错误。

**痛点二：一千个链接跑 15 分钟，而且没法中途收手。**

> Currently, I use lychee to validate external links in my blog. I have more than 600 Markdown pages and more than a thousand of links. It takes 15+ minutes to check them all. Sometimes it may take even longer. Since the checking process is rather non-deterministic, it won't hurt if it stops after the first five minutes.
>
> —— [#2076](https://github.com/lycheeverse/lychee/issues/2076)，2026-03-05 提出，至今 open

600 个页面、1000 个链接，15 分钟。作者要的东西很小：跑满 5 分钟就停，报告已经查到的，并说明还有多少没查。这个功能至今没有。

**痛点三：锚点检查在 GitHub 上直接误报。**

> ```
> [ERROR] https://github.com/googleapis/googleapis/blob/6a8c7914.../error_details.proto#L119 | Cannot find fragment
> ```
> This used to work until last month.
>
> —— [#1729](https://github.com/lycheeverse/lychee/issues/1729)，2025-06-13 提出，40 条评论，至今 open

`#L119` 是代码托管站用 JavaScript 画出来的行号锚点，HTML 里根本没有这个 `id`。工具在 HTML 里找不到它，就报「找不到锚点」。这条 issue 有 40 条评论、跨了几个月，因为「锚点在不在」这件事在不同站点上规则完全不同，而工具只有「在」和「不在」两个答案。

**痛点四：三个开关都叫「只查本地」，没有一个真的是。**

> As a new user, I tried to use `lychee` for checking links from local files only. No external requests was expected, I tried to troubleshoot why they were being made and thought `--offline` would apply to my inputs... I was mistaken and `--skip-missing` was the more appropriate option as it applied to inputs, whereas `--offline` does not.
>
> —— [#668](https://github.com/lycheeverse/lychee/issues/668)，2022-06-30 提出，至今 open

同一个需求还有一条 [#1646](https://github.com/lycheeverse/lychee/issues/1646)：「我只想查外链，怎么配？」——4 年过去，答案仍然是「组合几个开关试试」。`--offline` 管的是「从输入里抽出来的链接」，`--skip-missing` 管的是输入本身，`--scheme file` 又是另一个维度。三个概念，一个需求。

**痛点五：本地路径的解析有坑。**

> It seems that lychee skips links with absolute path or spaces, and it doesn't handle links with `~` as absolute links.
>
> —— [#1503](https://github.com/lycheeverse/lychee/issues/1503)，2024-09-21 提出，至今 open

带空格的路径、绝对路径、`~` 开头的路径，三种在 Markdown 里都合法，处理方式各不相同。

## 我们的取舍

**砍掉的：**

| 砍掉 | 为什么 |
|---|---|
| 远程输入（抓一个网页再爬它） | 文档检查器的输入是自己的仓库。支持远程输入会把工具变成爬虫，然后就要处理 robots.txt、深度限制、去重域名…… |
| 配置文件的 64 个键 | 一个键都不留。所有能改的东西都在命令行上，且都有能用的默认值。 |
| `--accept 200..=204,429` 这套区间语法 | 状态码到判决的映射是一个常量表，十秒能读完。要改判决就改表，不用学一门小语言。 |
| 邮箱地址检查（SMTP 探测） | 探测邮箱是否存在的可靠性极低，误报率极高，而且需要发信。`mailto:` 直接标 `skipped`。 |
| 自定义请求头、Basic Auth、Cookie、代理、PAC | 检查公开文档用不上。真需要登录才能看的页面，本来也不该放进链接检查。 |
| 5 种输出格式 | 两种：给人看的文本，给机器看的 JSON。 |
| 缓存文件（跨运行复用结果） | 单次运行内按 URL 去重，收益的绝大部分已经拿到了；跨运行缓存会引入「结果过期」这个新问题。 |
| HTML 渲染 | 不做。渲染意味着内置浏览器，也就是 [#1439](https://github.com/lycheeverse/lychee/issues/1439) 那条路。锚点查不出来就报 `unknown`。 |

**保留的：**

- 五种判决（`ok` / `dead` / `blocked` / `unknown` / `skipped`）——这是整个工具存在的理由。
- HEAD 先探、失败换 GET。
- 锚点检查（GitHub slug 规则 + `id`/`name` + `#L12` 行号）。
- 重定向跟随与环检测。
- 每主机并发限制。
- 每个坏链接一行，列出所有出现位置。

**加的（原版没有的）：**

- `--only local|external`：一个开关表达「只查本地」，一个开关表达「只查外链」。`--only local` 保证一个 socket 都不开。
- `--deadline`：跑满 N 秒就停，把没查的标成 `skipped` 并计数。这是 [#2076](https://github.com/lycheeverse/lychee/issues/2076) 要的东西。
- **根路径探针**：一个 404 只有在「主机还愿意正常响应它自己的根路径」时才算死链。crates.io 对所有非浏览器客户端把整站 HTML 返回 404，连 `/` 也是——不加这条规则，一个拼错的 crate 名会被报成死链。
- **读满上限就不下结论**：页面超过 512KB 时，前 512KB 里没有的锚点只报 `unknown`。这一条是被真实数据打出来的（见下）。
- 坏链接按「目标」聚合：同一个 URL 在 12 个文件里出现，报告一行，行内列出 12 个位置。

## 与原版的差异

| 维度 | lychee 0.24.2 | linkcheck 1.0.0 |
|---|---|---|
| 判决种类 | 成功 / 失败（+ 若干内部变体，对外仍是一个错误） | 五种，只有 `dead` 影响退出码 |
| 403 / 429 / 503 | 报错，退出码 2 | `blocked`，退出码 0 |
| 5xx / 超时 | 报错，退出码 2 | `unknown`，退出码 0 |
| 主机根路径 404 | 不检查 | 探一次，命中则把该主机的 404 降级为 `blocked` |
| 页面超过读取上限 | 不适用（全量读） | 锚点结论降级为 `unknown` |
| 锚点查不到时的答案 | 「找不到锚点」 | 能证明才算死链，否则 `unknown` |
| 命令行选项 | 72 个 | 8 个（+ `-h` / `--version`） |
| 配置文件 | `lychee.toml`，64 个键 | 无 |
| 输出格式 | 5 种 | 2 种（文本 / JSON） |
| 去重 | 同一 URL 多次出现会分别报告 | 一次检查，报告一行，列出所有位置 |
| 超时预算 | 无（有 `--max-retries`、`--retry-wait-time`） | `--deadline` |
| 实现 | Rust，8 个 crate，lib + bin | Python 单文件，标准库 |
| 依赖 | reqwest / tokio / pulldown-cmark / html5gum / regex … | 无 |
| 远程输入 | 支持 | 不支持（明确的取舍） |

## 实测对照

同一份标注语料（6 个 Markdown 文件、104 个链接、24 个不同目标，跑在本机 HTTP 服务上，每条路由的期望判决人工标注）：

| | linkcheck | lychee 0.24.2 |
|---|---|---|
| 报出的不同问题 | 12 行（6 dead + 3 blocked + 1 unknown + 2 skipped） | 10 个不同 URL，50 行 |
| 真死链 | 6/6 全部命中 | 6/6 全部命中 |
| 误报 | 0 | 4（403 / 429 / 503 / 500） |
| 精确率 | 6/6 = 100% | 6/10 = 60% |
| 耗时（timeout 5s，三次） | 4.8 / 4.9 / 5.0 s | 6.1 / 20.0 / 20.1 s |
| 退出码 | 1（有死链） | 2 |

## 许可证说明

lychee 采用 MIT / Apache-2.0 双许可，允许在保留版权声明的前提下复用代码片段。

本项目**没有复用它的任何代码**。实现语言不同（Rust → Python），数据结构不同，判决模型不同。拆解阶段读了 `types/status.rs`、`checker/website.rs`、`README.md` 和 issue 列表，用来理解它的架构选择和已知问题；写出来的每一行都是自己的。

crates.io 的 404 行为、nixos 手册 2.8MB 页面这两个真实案例，是在实测中撞出来的，不是从它的代码里读到的。
