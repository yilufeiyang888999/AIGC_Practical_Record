"""网关与客户端单元测试（stdlib unittest，不引入 pytest 依赖）。

为什么只测这几处：
  出图链路依赖真实的 ComfyUI，单测没有意义；真正值得锁住的是**纯逻辑**——
  鉴权比较、工作流参数注入、模板不被污染。这三处一旦出错，
  表现为"看起来跑通了但结果是错的"，是最难线上发现的一类 bug。

运行：
    python -m unittest discover -s tests -v
或：
    python tests/test_gateway.py
"""
import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))                             # comfy_client 共享包在仓库根
sys.path.insert(0, str(REPO_ROOT / "gateway" / "aigc-gateway"))  # security/config 在网关目录

import comfy_client as cc          # noqa: E402
from security import key_matches   # noqa: E402

TEMPLATE_PATH = REPO_ROOT / "gateway" / "aigc-gateway" / "workflows" / "lora_workflow_api.json"


def load_template() -> dict:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


class TestApiKeyComparison(unittest.TestCase):
    """密钥比较：正确性与恒定时间语义。"""

    def test_matching_key(self):
        self.assertTrue(key_matches("secret", "secret"))

    def test_mismatching_key(self):
        self.assertFalse(key_matches("secret", "Secre1"))

    def test_none_is_rejected(self):
        """None 必须等价于空串，不能因为短路而意外通过。"""
        self.assertFalse(key_matches(None, "secret"))
        self.assertFalse(key_matches(None, ""))

    def test_empty_expected_key_rejects_everything(self):
        # 未配置密钥时（config.API_KEY 为空），任何值都不该"匹配"
        self.assertFalse(key_matches("anything", ""))

    def test_prefix_of_correct_key_is_not_accepted(self):
        self.assertFalse(key_matches("sec", "secret"))


class TestBuildPrompt(unittest.TestCase):
    """工作流参数注入。"""

    def setUp(self):
        self.tpl = load_template()

    def test_template_not_mutated(self):
        """deepcopy 的意义就在这儿：模板是进程级缓存，被污染会波及所有后续任务。"""
        before = copy.deepcopy(self.tpl)
        cc.build_prompt(self.tpl, positive="a cat", seed=1, steps=10)
        self.assertEqual(self.tpl, before)

    def test_basic_params_applied(self):
        wf = cc.build_prompt(self.tpl, positive="a cat", negative="blurry",
                             seed=42, steps=15, cfg=6.5, width=640, height=448)
        self.assertEqual(wf[cc.find_by_title(wf, "POSITIVE_PROMPT")]["inputs"]["text"], "a cat")
        self.assertEqual(wf[cc.find_by_title(wf, "NEGATIVE_PROMPT")]["inputs"]["text"], "blurry")
        sampler = wf[cc.find_by_title(wf, "SAMPLER")]["inputs"]
        self.assertEqual(sampler["seed"], 42)
        self.assertEqual(sampler["steps"], 15)
        self.assertEqual(sampler["cfg"], 6.5)
        latent = wf[cc.find_by_title(wf, "LATENT")]["inputs"]
        self.assertEqual((latent["width"], latent["height"]), (640, 448))

    def test_none_params_keep_template_values(self):
        """传 None = 不覆盖。这是向后兼容的契约，不能改成"用默认值覆盖"。"""
        wf = cc.build_prompt(self.tpl, positive="x")
        self.assertEqual(
            wf[cc.find_by_class(wf, "CheckpointLoaderSimple")]["inputs"]["ckpt_name"],
            self.tpl[cc.find_by_class(self.tpl, "CheckpointLoaderSimple")]["inputs"]["ckpt_name"],
        )

    def test_checkpoint_override(self):
        wf = cc.build_prompt(self.tpl, ckpt_name="sd_xl_base_1.0.safetensors")
        self.assertEqual(
            wf[cc.find_by_class(wf, "CheckpointLoaderSimple")]["inputs"]["ckpt_name"],
            "sd_xl_base_1.0.safetensors")

    def test_lora_override_name_and_strength(self):
        wf = cc.build_prompt(self.tpl, lora_name="myshiba_v4.safetensors", lora_strength=0.8)
        n = wf[cc.find_by_class(wf, "LoraLoader")]["inputs"]
        self.assertEqual(n["lora_name"], "myshiba_v4.safetensors")
        # model 与 clip 两个强度必须一起改，只改一个会导致 LoRA 表现不一致
        self.assertEqual(n["strength_model"], 0.8)
        self.assertEqual(n["strength_clip"], 0.8)

    def test_lora_strength_only(self):
        wf = cc.build_prompt(self.tpl, lora_strength=1.2)
        n = wf[cc.find_by_class(wf, "LoraLoader")]["inputs"]
        self.assertEqual(n["strength_model"], 1.2)
        self.assertEqual(
            n["lora_name"],
            self.tpl[cc.find_by_class(self.tpl, "LoraLoader")]["inputs"]["lora_name"])

    def test_lora_override_without_lora_node_raises(self):
        """模板没有 LoRA 节点却要求改 LoRA —— 必须报错，静默忽略会让人以为生效了。"""
        wf = load_template()
        lora_id = cc.find_by_class(wf, "LoraLoader")
        del wf[lora_id]
        with self.assertRaises(ValueError):
            cc.build_prompt(wf, lora_name="whatever.safetensors")

    def test_filename_prefix_applied(self):
        wf = cc.build_prompt(self.tpl, filename_prefix="gateway/abc123")
        self.assertEqual(
            wf[cc.find_by_class(wf, "SaveImage")]["inputs"]["filename_prefix"],
            "gateway/abc123")


class TestWorkflowTemplate(unittest.TestCase):
    """工作流模板本身的约定（这些是脚本按标题定位节点的前提）。"""

    def setUp(self):
        self.tpl = load_template()

    def test_required_titles_exist(self):
        for title in ("POSITIVE_PROMPT", "NEGATIVE_PROMPT", "SAMPLER", "LATENT"):
            cc.find_by_title(self.tpl, title)   # 找不到会抛 KeyError

    def test_is_api_format_not_ui_format(self):
        # UI 格式带 nodes/links，喂给 /prompt 会被拒
        self.assertNotIn("nodes", self.tpl)
        self.assertNotIn("links", self.tpl)

    def test_default_lora_matches_benchmark_baseline(self):
        """默认 LoRA 必须与 benchmarks §2.4 的基线版本一致。

        曾出现工作流写 v2、benchmarks 基线是 v4 的不一致——
        这类"文档说 A、代码跑 B"的偏差，正是单测该锁住的。
        """
        lora = self.tpl[cc.find_by_class(self.tpl, "LoraLoader")]["inputs"]["lora_name"]
        self.assertTrue(lora.startswith("myshiba_v4"), f"默认 LoRA 应为 v4 基线，实际 {lora}")


class TestSingleSourceOfTruth(unittest.TestCase):
    """2026-09-23：两份 comfy_client.py 已合并为仓库根的共享包 comfy_client/。

    此前这里测的是"两份实现有没有漂移"——那只能**发现**问题，不能**阻止**问题。
    现在改测两件事：
      1. 不会再有人把实现复制回消费方目录（漂移的根源）
      2. 共享包在两种工作区下都能解析出正确的路径（合并带来的新风险）
    """

    def test_no_local_copies(self):
        """消费方目录里不能再出现 comfy_client.py。"""
        for rel in ("scripts/comfy_client.py", "gateway/aigc-gateway/comfy_client.py"):
            self.assertFalse((REPO_ROOT / rel).exists(),
                             f"{rel} 不应存在：实现已统一到仓库根的 comfy_client/ 包，"
                             f"复制一份回去就是漂移的开始")

    def test_package_lives_at_repo_root(self):
        self.assertTrue((REPO_ROOT / "comfy_client" / "__init__.py").exists())
        self.assertEqual(Path(cc.__file__).resolve().parent,
                         (REPO_ROOT / "comfy_client").resolve(),
                         "import 到的不是仓库内的包，检查 sys.path / 是否装了旧版本")

    def test_public_api_present(self):
        """网关与脚本依赖这些名字，改名/删除必须被拦住。"""
        for name in ("build_prompt", "submit", "wait_for", "fetch_images",
                     "find_by_title", "find_by_class", "load_workflow",
                     "make_session", "check_server", "list_checkpoints",
                     "configure", "get_workflow_path", "get_base_dir"):
            self.assertTrue(hasattr(cc, name), f"共享包缺少 {name}")

    def test_default_workflow_path_exists(self):
        """默认工作区（仓库根）的工作流不能是死路径。"""
        path = cc.get_workflow_path()
        self.assertTrue(path.exists(), f"默认工作流不存在: {path}")

    def test_gateway_workspace_resolves(self):
        """网关 configure 之后，三个目录都应落在网关自己目录下。

        这是合并共享包引入的新风险：两边工作区不同，配错了不会报错，
        只会静默读错工作流、把图写到错误的地方。
        """
        gw = REPO_ROOT / "gateway" / "aigc-gateway"
        snapshot = cc.get_settings()
        try:
            cc.configure(base_dir=gw,
                         workflow=str(gw / "workflows" / "lora_workflow_api.json"))
            self.assertEqual(cc.get_base_dir(), gw.resolve())
            self.assertTrue(cc.get_workflow_path().exists())
            self.assertEqual(cc.get_output_dir(), gw.resolve() / "output")
            self.assertEqual(cc.get_log_dir(), gw.resolve() / "logs")
        finally:
            cc.configure(base_dir=snapshot.base_dir, workflow=snapshot.workflow,
                         host=snapshot.host)


if __name__ == "__main__":
    unittest.main(verbosity=2)
