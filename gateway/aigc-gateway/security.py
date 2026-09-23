"""Swagger UI Authorize 按钮用的安全方案声明 + 密钥校验。

⚠️ 只实例化 APIKeyHeader 不会出现在 OpenAPI 里——必须被路由 Depends()
引用，FastAPI 才会生成安全方案（Authorize 按钮才有）。
实际鉴权校验在 main.py 的中间件，这里提供**校验函数本身**。

2026-09-23 修正：密钥比较改用 hmac.compare_digest（恒定时间）。
`!=` 会在第一个不同字节处提前返回，比较耗时随"猜对的前缀长度"变化——
这是教科书级的时序侧信道。API Key 是长期凭证，值得这个防御。
"""
import hmac

from fastapi.security import APIKeyHeader

api_key_scheme = APIKeyHeader(name="x-api-key", auto_error=False)


def key_matches(provided: str | None, expected: str) -> bool:
    """恒定时间比较。provided 为 None 时按空串比较，不做短路。

    ⚠️ expected 为空串时**一律返回 False**。
    否则 `key_matches(None, "")` 会成立——空密钥 + 空输入互相"匹配"，
    这正是"我以为关了鉴权结果它认了空值"那类事故的入口。
    未配置密钥的放行逻辑在 main.py 中间件（`if config.API_KEY and ...`），
    不该由比较函数承担。
    """
    if not expected:
        return False
    return hmac.compare_digest(provided or "", expected)
