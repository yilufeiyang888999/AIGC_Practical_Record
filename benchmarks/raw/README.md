# 原始基准数据（benchmarks/raw）

> 本目录是 [../实测数据.md](../实测数据.md) 中**每一个数字的来源**。
> 共 **25 轮**：图像 18 轮 / 340 样本，LLM 7 轮 / 63 条请求记录。
> 每轮同时留 `.json`（含逐条明细）和 `.csv`（扁平表，便于 Excel/BI 直接打开）。

## 数据格式

`bench_*.json` — 图像基准：

```
{
  "summary": { run_id, host, comfy_host, device, checkpoint, resolution,
               steps, cfg, wait_mode, total, success, failed, success_rate,
               batch_elapsed_s, avg_s, median_s, stdev_s, min_s, max_s,
               jitter_s, p50_s, p90_s, p95_s, p99_s, pct_confidence,
               throughput_per_min, vram_peak_gb },
  "records": [ { index, seed, steps, cfg, width, height, prompt, status,
                 elapsed, vram_peak_gb, attempts, error }, ... ]
}
```

`llm_bench_*.json` — LLM 基准：

```
{
  "summary": { run_id, host, llm_host, model, max_tokens, repeat,
               concurrency: { wall_s, task_totals_s, ratio_max_min, verdict },
               cases: { short|medium|long: { n, prompt_tokens, ttft_cold_s,
                        ttft_median_s, prefill_tps_median, decode_tps_median,
                        total_median_s, tok_per_s_median } } },
  "records": [ { case, rep, note, cold, status, ttft_s, ttft_content_s,
                 total_s, decode_s, completion_tokens, prompt_tokens,
                 tok_per_s, prefill_tps_srv, decode_tps_srv, cache_n }, ... ]
}
```

`device` 字段同时编码了 GPU 型号与显存分配器（`: cudaMallocAsync` / `: native`），
`wait_mode` 区分 WebSocket 精测与轮询——这两列决定了数据能否作为基线（见实测数据 §3.2）。

---

## 图像基准 18 轮（340 样本）

| 文件（`bench_*.json`） | GPU | 分配器 | 测量 | 分辨率 | n | 中位 | P95 | 张/分 | 角色 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `20260909_172357` | 5060 | cudaMallocAsync | polling | 512×512 | 10 | 6.03 | 6.05 | 9.91 | ❌ 作废（分配器 + 量具双错） |
| `20260909_172519` | P100 | native | polling | 512×512 | 10 | 8.18 | 10.19 | 7.08 | ❌ 作废（n=10 样本不足） |
| `20260910_112150` | 5060 | cudaMallocAsync | polling | 512×512 | 20 | 6.03 | 24.94 | 8.57 | ❌ 作废（P95 24.94s = 批量卡死） |
| `20260910_114915` | 5060 | native | polling | 512×512 | 20 | 2.01 | 4.11 | 25.65 | ⚠️ 量具偏差（P95 虚高 76%） |
| `20260910_115047` | P100 | native | polling | 512×512 | 20 | 8.17 | 10.22 | 7.02 | ⚠️ 量具偏差（制造了"10.2s 毛刺"假象） |
| `20260910_141820` | 5060 | native | **WS** | 512×512 | 20 | 1.95 | 4.27 | 25.57 | WS 首轮（P95 未收敛，被 170905 取代） |
| `20260910_163114` | P100 | cudaMallocAsync | **WS** | 512×512 | 20 | 7.79 | 8.88 | 7.46 | ✅ A/B 对照（证伪"跨架构共性"） |
| **`20260910_164732`** | **P100** | **native** | **WS** | 512×512 | 20 | **7.90** | **8.92** | **7.49** | ✅ **P100 基线** |
| `20260910_170415` | 5060 | cudaMallocAsync | **WS** | 512×512 | 20 | 4.54 | 5.95 | 10.72 | ✅ A/B 对照（+2.6s 的真实开销） |
| **`20260910_170905`** | **5060** | **native** | **WS** | 512×512 | 20 | **1.94** | **2.34** | **27.86** | ✅ **5060 基线** |
| `20260914_122639` | P100 | native | **WS** | 512×512 | 20 | 7.83 | 9.97 | 7.02 | 温度墙闭环轮（配合同步 nvidia-smi 采样） |
| `20260914_145946` | P100（裸机 8288） | native | **WS** | 512×512 | 20 | 7.46 | 8.79 | 7.43 | ✅ 容器化对照：**裸机侧** |
| `20260914_150759` | P100（容器 8289） | native | **WS** | 512×512 | 20 | 7.89 | 9.06 | 7.50 | ✅ 容器化对照：**容器侧**（净开销 0.7%） |
| **`20260917_171304`** | **P100** | **native** | **WS** | **768×512** | 20 | **17.73** | **19.24** | **3.63** | ✅ **LoRA 批量基线**（降速 ~30%） |
| `20260921_155208` | P100 | native | polling | 768×512 | 20 | 22.44 | 22.62 | 2.82 | 散热复测 / PL=170W（量具偏差，仅趋势参考） |
| `20260921_160655` | P100 | native | **WS** | 768×512 | 20 | 21.12 | 23.07 | 2.90 | ✅ 散热复测 / PL=170W（比基线慢 19%） |
| `20260922_113513` | P100 | native | **WS** | 768×512 | 20 | 16.42 | 17.64 | 3.56 | ✅ 导风罩改造后（空调环境，回落到 16.4s） |
| `20260922_115646` | P100 | native | **WS** | 768×512 | 20 | 16.29 | 16.86 | 3.74 | ✅ 导风罩改造后（同上，复现性好） |

> **⚠️ 诚实标注**：A10（Ampere）两轮对照数据**已随 ModelScope 实例释放丢失**，无原始文件。
> 结论（cudaMallocAsync 与 native 中位均为 2.01s、20/20、显存 2.31GB）见实测数据 §3.1。
> 这是本仓库**唯一一组不可查证的实测数据**。

---

## LLM 基准 7 轮（63 条请求 + 7 次并发判定）

| 文件（`llm_bench_*.json`） | 模型 | 变量 | prefill（short/med/long） | decode（short/med/long） | 角色 |
| --- | --- | --- | --- | --- | --- |
| `20260918_111847` | ornith-9B-Q8_0 | ubatch **32** | 85.9 / 162.4 / 161.6 | 28.8 / 28.8 / 28.7 | ✅ A/B 前轮（prefill 卡在 162） |
| **`20260918_155227`** | **ornith-9B-Q8_0** | **ubatch 256** | 86.1 / **513.2** / **505.4** | **28.7 / 28.7 / 28.6** | ✅ **LLM 基线（定稿）** |
| `20260918_160239` | Qwen3-14B-Q4_K_M | ubatch 256 | 21.5 / 21.2 / 19.3 | 22.5 / 22.2 / **19.6** | ✅ 模型对比轮（Q4 反慢 22%） |
| `20260920_104721` | ornith-9B-Q8_0 | ubatch 256 | 85.7 / 499.6 / 483.9 | 28.6 / 27.0 / **26.5** | 复测轮 ⚠️ 见下方存疑 |
| `20260920_105135` | ornith-9B-Q8_0 | ubatch 256 | 85.6 / 505.5 / 492.8 | 28.8 / 28.7 / **27.1** | 复测轮 ⚠️ 见下方存疑 |
| `20260920_105447` | ornith-9B-Q8_0 | ubatch 256 | 85.4 / 505.0 / 472.4 | 28.8 / 28.1 / **26.2** | 复测轮 ⚠️ 见下方存疑 |
| `20260921_151702` | ornith-9B-Q8_0 | ubatch 256 | 86.0 / 513.4 / 504.9 | 28.8 / 28.8 / 28.7 | ✅ 基线复现（与 09-18 完全吻合） |

> **⚠️ 诚实标注（存疑项，未结论）**：09-20 三轮的 **long 档 decode 落在 26.2~27.1 tok/s**，
> 明显低于基线 28.7；short/medium 档同期正常。当时**未记录并发上下文**（是否有 ComfyUI 同卡
> 出图、是否有其他请求占用），因此**不对其成因下结论**，仅如实归档。
> 09-21 复现轮 long 档回到 28.7，说明这不是配置漂移。
> 若要定因，需重跑并同步记录 `/system_stats` 与同卡进程列表。

---

## 复现方式

```bash
# 图像基准（以 5060 基线为例）
export COMFY_HOST=http://127.0.0.1:8188
python scripts/comfy_batch_gen.py --n 20 --ws --width 512 --height 512 --steps 20

# LLM 基准
export LLM_HOST=http://127.0.0.1:8000
python scripts/llm_bench.py --repeat 3 --max-tokens 256

# 一键汇总本目录全部轮次（读 summary 字段，输出上表）
python scripts/summarize_runs.py benchmarks/raw
```

各轮次的**完整环境上下文**（驱动 / PyTorch / 分配器 / 温度条件）见
[../实测数据.md §1 测试环境](../实测数据.md)，环境变化已在实测数据 §"数据版本演进"逐轮标注。
