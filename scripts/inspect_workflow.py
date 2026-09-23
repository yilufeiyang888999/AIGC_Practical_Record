"""检查 API 格式工作流的结构，确认导出正确。"""
import json, sys
from pathlib import Path

WF = Path(r"D:\AIGC\Workflows\base_workflow_api.json")

if not WF.exists():
    sys.exit(f"❌ 文件不存在: {WF}")

wf = json.loads(WF.read_text(encoding="utf-8"))

# 判断格式
if "nodes" in wf or "links" in wf:
    sys.exit("❌ 这是 UI 格式（有 nodes/links），不是 API 格式。请用 Export (API) 重新导出")

print(f"✅ API 格式，共 {len(wf)} 个节点\n")
print(f"顶层 keys: {sorted(wf.keys(), key=int)}\n")

# 逐个节点展开
for nid in sorted(wf.keys(), key=int):
    node = wf[nid]
    title = node.get("_meta", {}).get("title", "")
    title_str = f'  [标题: {title}]' if title else "  [无自定义标题]"
    print(f"节点 {nid}: {node['class_type']}{title_str}")
    for k, v in node["inputs"].items():
        if isinstance(v, list):
            print(f"    {k:16s} ← 连线自 节点{v[0]} 的输出[{v[1]}]")
        else:
            shown = str(v)
            if len(shown) > 60:
                shown = shown[:57] + "..."
            print(f"    {k:16s} = {shown}")
    print()

# 检查自定义标题
print("=" * 50)
titles = {n.get("_meta", {}).get("title"): nid for nid, n in wf.items()
          if n.get("_meta", {}).get("title")}
expected = ["POSITIVE_PROMPT", "NEGATIVE_PROMPT", "SAMPLER", "LATENT"]
for t in expected:
    if t in titles:
        print(f"✅ {t:18s} → 节点 {titles[t]}")
    else:
        print(f"⚠️  {t:18s} → 未找到（步骤 4 的标题没改，或改完没重新导出）")