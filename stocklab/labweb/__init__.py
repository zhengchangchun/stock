"""P15 · 持仓管理 Web 应用（浏览器里查看 + 管理真实持仓）。

## 为什么不扩 `stocklab/dashboard/`（取舍记录）

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
| `data.py`   | 取数：把既有函数拼成各页面要的 dict（**不产生新口径**） |
| `render.py` | 服务端渲染 HTML（内嵌 CSS + inline SVG；无 CDN / 无外部字体 / 无 JS 框架） |
| `app.py`    | HTTP 路由表 + 写路径（PRG）+ 回环守卫 |
"""

from __future__ import annotations

SERVICE = "stocklab-labweb"
VERSION = "1.0"

__all__ = ["SERVICE", "VERSION"]
