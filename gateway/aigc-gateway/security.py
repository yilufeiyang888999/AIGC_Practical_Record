"""Swagger UI Authorize 按钮用的安全方案声明。

⚠️ 只实例化 APIKeyHeader 不会出现在 OpenAPI 里——必须被路由 Depends()
引用，FastAPI 才会生成安全方案（Authorize 按钮才有）。
实际鉴权校验在 main.py 的中间件，这里纯粹是"声明给 Swagger 看"。
auto_error=False：缺失时返回 None 而不是 403，放行给中间件统一处理。
"""
from fastapi.security import APIKeyHeader

api_key_scheme = APIKeyHeader(name="x-api-key", auto_error=False)
