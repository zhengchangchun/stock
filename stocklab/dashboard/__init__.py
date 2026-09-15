"""单文件看板 + 本地只读服务（P14）。

- `summary`：把**既有接口**拼成一份稳定 JSON（不产生新口径）
- `html`：把摘要渲染成**单文件** HTML（零外部依赖、相对 URL、inline SVG）
- `server`：stdlib `ThreadingHTTPServer`，**只绑回环**、**只读**

服务不由本项目常驻（ADR-001 D-05：项目内不实现守护进程）。claude / 人手起一次做验收，
常驻交给 nanobot 调度侧。
"""
