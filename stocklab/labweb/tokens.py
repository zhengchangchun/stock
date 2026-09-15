"""表单 `_token`：HMAC(服务端密钥, 会话 id + 日期)。

## 为什么 HTTP Basic Auth 下仍然必须有这一层

Basic Auth 的凭据是**浏览器自动带上**的 —— 只要用户登录过，一个
`<form action="http://127.0.0.1:8791/lab/trades" method="post">` 放在任意
第三方页面上，浏览器就会带着凭据把请求发出去。也就是说**认证完全挡不住 CSRF**。
（nano 的 nginx 反代把服务暴露在 `:80`，这条路径是真实可达的。）

所以每个表单都带一个服务端签发的 `_token`：
- 它由**服务端密钥**导出，攻击者猜不出来；
- 校验失败 → **403 且数据库一个字节都不动**（有测试钉住）；
- 密钥在**进程启动时随机生成**（`secrets.token_bytes(32)`），不落盘、不进日志、
  不出现在 `/health` 里。

## 为什么日期进签名

签名里带日期，token 不会永久有效。允许**今天与昨天**两天：
页面在 23:59:59 渲染、用户在 00:00:01 提交是完全正常的操作，
一刀切到「只有今天」会把这类提交判成攻击 —— 而用户看到的会是
「403 无权限」，一个让人以为系统坏了的错误。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import date, timedelta

#: token 长度（hex 字符）。HMAC-SHA256 取前 32 位足够，且表单短。
TOKEN_CHARS = 32

#: 接受的天数窗口（今天 + 昨天）。
ACCEPT_DAYS = 2


def new_secret() -> bytes:
    """服务端密钥。**每次启动随机**，不落盘（落盘就多一个需要保密的文件）。"""
    return secrets.token_bytes(32)


def new_form_id() -> str:
    """一次渲染对应一个 `_form_id`，当幂等键用（见 `app.py` 的双提交处理）。"""
    return secrets.token_hex(8)


class TokenSigner:
    """签发 / 校验表单 token。无状态：token 本身就是签名。"""

    def __init__(self, secret: bytes, *, session: str | None = None) -> None:
        if not secret:
            raise ValueError("密钥不能为空")
        self._secret = bytes(secret)
        # 会话 id 也随机：同一密钥在不同进程/不同重启后签发的 token 互不通用
        self.session = session or secrets.token_hex(8)

    def mint(self, day: str) -> str:
        msg = f"{self.session}|{day}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:TOKEN_CHARS]

    def accepted(self, *, today: str | None = None) -> list[str]:
        """当前可接受的 token 集合（今天 + 昨天）。"""
        end = date.fromisoformat(today) if today else date.today()
        return [self.mint((end - timedelta(days=k)).isoformat())
                for k in range(ACCEPT_DAYS)]

    def verify(self, token: str | None, *, today: str | None = None) -> bool:
        """常数时间比较。缺失 / 空 / 错误一律 `False`（不区分原因，不给探测信号）。"""
        if not token:
            return False
        return any(hmac.compare_digest(token, good)
                   for good in self.accepted(today=today))

    @property
    def secret(self) -> bytes:
        """给测试用；**禁止**把它渲染进任何响应（`/health` 有测试钉住不泄露）。"""
        return self._secret


__all__ = ["ACCEPT_DAYS", "TOKEN_CHARS", "TokenSigner", "new_form_id", "new_secret"]
