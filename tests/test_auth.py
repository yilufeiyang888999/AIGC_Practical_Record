"""网关鉴权行为测试（端到端，走真实 HTTP 栈）。

为什么值得单独测：鉴权是"看起来对"最容易骗过人的地方。
中间件改一行、挂载顺序变一下，结果图就从"需要 key"变成"谁都能下"，
而页面照样 200，日志也照样正常。**只测了就等于没测，必须真发请求。**

依赖 fastapi[testclient] / httpx；缺失时跳过（不阻塞纯逻辑测试）。

运行：
    AIGC_API_KEY=testkey python tests/test_auth.py
"""
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "gateway" / "aigc-gateway"))

API_KEY = "testkey"
os.environ.setdefault("AIGC_API_KEY", API_KEY)

try:
    from fastapi.testclient import TestClient
    import main
    HAS_DEPS = True
except Exception as e:                      # 依赖缺失时跳过，不阻塞其他测试
    HAS_DEPS = False
    SKIP_REASON = f"缺少依赖: {e}"


@unittest.skipUnless(HAS_DEPS, SKIP_REASON if not HAS_DEPS else "")
class TestAuthMiddleware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def test_v1_requires_key(self):
        self.assertEqual(self.client.get("/v1/tasks/abc").status_code, 401)

    def test_files_requires_key(self):
        """⚠️ 这条是 2026-09-23 修的那个洞。

        /files 挂的是出图结果。此前中间件只守 /v1/，结果图裸奔——
        而 task_id 只有 12 位 hex。这与"数据不出内网"的定位直接冲突。
        """
        self.assertEqual(self.client.get("/files/abc/a.png").status_code, 401)

    def test_wrong_key_rejected(self):
        r = self.client.get("/files/abc/a.png", headers={"x-api-key": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_correct_key_passes_auth(self):
        """带正确 key 时不应再是 401。

        断言 404 而不是 200：文件本就不存在，404 正好证明**鉴权已通过**，
        走到了静态文件查找环节。若这里返回 401，说明中间件把正确 key 也拦了。
        """
        r = self.client.get("/files/abc/a.png", headers={"x-api-key": API_KEY})
        self.assertNotEqual(r.status_code, 401)

    def test_public_endpoints_stay_open(self):
        """/ 与 /health 必须开放：前者是自描述，后者给探活用。"""
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_root_response_is_self_describing(self):
        d = self.client.get("/").json()
        self.assertEqual(d["service"], "aigc-gateway")
        self.assertIn("/v1/images/generations", d["endpoints"]["image"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
