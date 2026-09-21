"""P15 · 持仓管理 Web 应用（浏览器里查看 + 管理真实持仓）。

## 为什么不扩 `stocklab/dashboard/`（取舍记录）

> ⚠️ **2026-09-21 更新**：两个网页服务已**合并**（ADR-018）。当时「只读服务 vs 可写应用」
> 的区分是为选落点而写的；现在看板不再有服务入口（只剩 `dashboard build` 的离线单文件），
> `lab serve` 是本机唯一的网页服务，端口 8791。下面第 1 条的「只读契约」已随服务一起删除，
> 第 2 条的「离线产物 vs 实时应用」仍然成立（产物归产物）。

`docs/plans/2026-09-15-p15-持仓管理Web应用.md` 允许两种落点。这里选**新建包**：

1. `dashboard` 的**只读**是一条被测钉住的契约（`server.py` 的 docstring、
   `tests/test_dashboard_server.py` 的 `test_post_is_method_not_allowed`）。
   往同一个 `route()` 里塞 POST 写入口，等于让「只读服务」这句话变成
   「大部分时候只读」—— 而安全属性一旦变成形容词就没了。
2. `dashboard` 还承担**离线单文件产物**（`dashboard build` → 一个 HTML 文件，
   可双击、可转发）。那是「快照」；本应用是「实时 + 可写」。两者的生命周期
   与失效模式都不同，混在一起会互相牵制。
3. 复用点在**函数级**：组合视图调 `portfolio.view`、风险调 `risk.panel`、
   写入调 `portfolio.ledger`、回环守卫调 `dashboard.server.assert_loopback`。
   所以「分成两个包」不产生第二份口径 —— 口径只跟函数走，不跟包走。

## 组成

| 模块 | 职责 |
|---|---|
| `tokens.py` | 表单 `_token`（HMAC，防 CSRF） |
| `data.py`   | 模块2 取数：把既有函数拼成各页面要的 dict（**不产生新口径**） |
| `render.py` | 模块2 渲染 HTML（内嵌 CSS + inline SVG；无 CDN / 无外部字体 / 无 JS 框架） |
| `cand_data.py` | **模块1** 取数：候选池三表 + `instruments` 取名（只读） |
| `cand_render.py` | **模块1** 渲染（复用模块2 的壳、CSS、折叠块） |
| `app.py`    | HTTP 路由表 + 写路径（PRG）+ 回环守卫 |

模块1 与模块2 共用一个服务器、一个壳、一套 token，但取数与渲染分成两组文件：
`render.py` 已 66KB，再把候选池塞进去就是什么都装的杂物间（边界按**职责**切）。
"""

from __future__ import annotations

SERVICE = "stocklab-labweb"
VERSION = "1.0"

__all__ = ["SERVICE", "VERSION"]
