"""随机值与哈希。

方案 12：会话令牌、主人 secret、访客码在数据库里只存哈希。

---

**`hash_token` 的适用范围是一条硬约束，不是建议。**

它用的是 SHA-256，只适用于**高熵**随机值。本项目里符合条件的有两类：

- 会话令牌：32 字节随机，取值空间 2^256；
- 主人 secret：32 字节随机，取值空间 2^256。

这两者的哈希无法被离线穷举，快哈希是正确选择——用慢哈希只会让每个请求
都多花几十毫秒，换不到任何安全收益。

**访客码绝不能用它。** 6 位数字只有 10^6 种可能，SHA-256 的彩虹表几秒钟
就能穷举完，等于明文存储。M9 实现访客码时必须使用带随机盐的慢哈希
（`hashlib.scrypt` 或 argon2），不得复用本模块的 `hash_token`。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid

# 会话令牌与主人 secret 的随机字节数。方案 3.1 要求"至少 32 字节"。
TOKEN_ENTROPY_BYTES = 32


def new_id() -> str:
    """内部标识符（房间、设备、会话、区域……）。

    明确一点：**它不是凭证**，不需要密码学强度，所以用 uuid4 而不是
    随机令牌。凭证一律走 `new_token()`。
    """
    return uuid.uuid4().hex


def new_token(nbytes: int = TOKEN_ENTROPY_BYTES) -> str:
    """生成高熵凭证。URL 安全，可直接放进链接和 Cookie。"""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> bytes:
    """凭证的存储形式。只可用于高熵随机值，见模块说明。"""
    return hashlib.sha256(token.encode("utf-8")).digest()


def token_matches(token: str, stored_hash: bytes) -> bool:
    """恒定时间比较。

    当前所有查询都是"用哈希查哈希"，比较由 SQLite 完成，用不到本函数；
    它留给需要把哈希取出来在应用层比对的场景（例如 M9 的访客码尝试计数），
    避免到时候有人顺手写 `==`。
    """
    return hmac.compare_digest(hash_token(token), stored_hash)
