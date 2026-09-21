"""单文件看板（P14）+ 回环守卫。

- `summary`：把**既有接口**拼成一份稳定 JSON（不产生新口径）
- `html`：把摘要渲染成**单文件** HTML（零外部依赖、相对 URL、inline SVG）
- `server`：**只剩回环守卫**（`assert_loopback`）—— 2026-09-21 两个网页服务合并后，
  这里不再有 HTTP 服务（见 `docs/decisions/2026-09-21-ADR-018-单一Web服务.md`）；
  唯一的网页服务是 `lab serve`（端口 8791）。

服务不由本项目常驻（ADR-001 D-05：项目内不实现守护进程）。`lab serve` 由人/claude
起一次做验收，常驻交给 nanobot 调度侧。
"""
