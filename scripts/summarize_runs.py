"""
汇总 benchmarks/raw/ 下全部基准轮次，输出 Markdown 表格。

存在的理由：
  README 承诺"每个数字都有原始数据可查"。可查证的前提是**能一键重算**——
   reviewer 不必信任文档里的表格，跑一遍这个脚本就能自己得到同一组数。
   这是把"我说是"变成"你可以自己验"的最小实现。

用法：
    python scripts/summarize_runs.py [目录]        # 默认 benchmarks/raw
    python scripts/summarize_runs.py --csv          # 输出 CSV 而非 Markdown
"""
import argparse
import csv
import glob
import json
import os
import sys


def load(path: str) -> dict:
    """读一轮基准的 summary。坏文件不静默跳过——静默跳过会让造假变得容易。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)["summary"]


def gpu_of(device: str) -> str:
    if "5060" in device:
        return "5060"
    if "P100" in device:
        return "P100"
    if "A10" in device:
        return "A10"
    return device.split(":", 2)[-1].strip()[:16] or "?"


def alloc_of(device: str) -> str:
    return "cudaMallocAsync" if "cudaMallocAsync" in device else "native"


def summarize_image(s: dict) -> dict:
    return {
        "轮次": s.get("run_id", "?"),
        "GPU": gpu_of(s.get("device", "")),
        "分配器": alloc_of(s.get("device", "")),
        "测量": s.get("wait_mode", "polling"),
        "分辨率": s.get("resolution", "?"),
        "n": s.get("total"),
        "中位": s.get("median_s"),
        "P95": s.get("p95_s"),
        "张/分": s.get("throughput_per_min"),
        "成功": f"{s.get('success')}/{s.get('total')}",
        "显存峰值G": s.get("vram_peak_gb"),
    }


def summarize_llm(s: dict) -> dict:
    row = {"轮次": s.get("run_id", "?"), "模型": s.get("model", "?")}
    for case in ("short", "medium", "long"):
        c = s.get("cases", {}).get(case, {})
        row[f"{case}_prefill"] = c.get("prefill_tps_median")
        row[f"{case}_decode"] = c.get("decode_tps_median")
        row[f"{case}_ttft_cold"] = c.get("ttft_cold_s")
    row["并发判定"] = s.get("concurrency", {}).get("verdict", "?")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="汇总基准轮次")
    ap.add_argument("directory", nargs="?", default="benchmarks/raw")
    ap.add_argument("--csv", action="store_true", help="输出 CSV 而非 Markdown")
    args = ap.parse_args()

    d = args.directory
    if not os.path.isdir(d):
        print(f"目录不存在: {d}", file=sys.stderr)
        return 1

    img_files = sorted(glob.glob(os.path.join(d, "bench_*.json")))
    llm_files = sorted(glob.glob(os.path.join(d, "llm_bench_*.json")))
    if not img_files and not llm_files:
        print(f"{d} 下没有 bench_*.json / llm_bench_*.json", file=sys.stderr)
        return 1

    img_rows = []
    for f in img_files:
        try:
            img_rows.append(summarize_image(load(f)))
        except Exception as e:      # 坏文件要看见，不能默默少一行
            print(f"⚠️ 跳过 {os.path.basename(f)}: {e}", file=sys.stderr)

    llm_rows = []
    for f in llm_files:
        try:
            llm_rows.append(summarize_llm(load(f)))
        except Exception as e:
            print(f"⚠️ 跳过 {os.path.basename(f)}: {e}", file=sys.stderr)

    total_samples = sum(r["n"] for r in img_rows if isinstance(r["n"], int))

    if args.csv:
        w = csv.DictWriter(sys.stdout, fieldnames=list(img_rows[0].keys()))
        w.writeheader()
        w.writerows(img_rows)
        return 0

    def md(rows: list) -> str:
        if not rows:
            return "（无）"
        head = "| " + " | ".join(rows[0].keys()) + " |"
        sep = "|" + "|".join(["---"] * len(rows[0])) + "|"
        body = ["| " + " | ".join("" if v is None else str(v) for v in r.values()) + " |"
                for r in rows]
        return "\n".join([head, sep] + body)

    print(f"## 图像基准（{len(img_rows)} 轮 / {total_samples} 样本）\n")
    print(md(img_rows))
    print(f"\n## LLM 基准（{len(llm_files)} 轮 / {sum(3 * 3 for _ in llm_rows)} 条请求）\n")
    print(md(llm_rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
