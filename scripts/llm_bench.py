"""
LLM 推理性能实测（llama.cpp OpenAI 兼容 API，流式）
用法：
    set LLM_HOST=http://192.168.1.100:8000     # llama-server（0.0.0.0 暴露，局域网直连）
    python llm_bench.py
产出：
    logs/llm_bench_<run_id>.json    结构化指标（benchmarks 数据源）
    logs/llm_bench_<run_id>.csv     同上

测什么：
    1. TTFT（首 token 延迟）= 提交 → 第一个内容 token 到达
    2. 解码速度（tok/s）   = 完成 token 数 / 解码时长
    3. 提示词长度对 TTFT 的影响（短/中/长三档）
    4. 并发行为：--parallel 1 时第二个请求排队多久
注意：
    - 每个用例跑 REPEAT 次取中位数；首轮含 KV cache 冷启动，单独标注
    - token 数优先取服务端 usage 字段，取不到退化为流式 chunk 计数（近似）
"""
import json, csv, time, statistics, os, socket, threading
from datetime import datetime
from pathlib import Path

import requests

LLM_HOST = os.getenv("LLM_HOST", "http://127.0.0.1:8000").rstrip("/")
BASE_DIR = Path(__file__).resolve().parent
REPEAT   = 3                 # 每用例重复次数
MAX_TOKENS = 256             # 统一输出长度，保证 tok/s 可横向比较
TIMEOUT  = 300               # P100 + 大模型，给足

# ---------- 测试用例：短 / 中 / 长提示词 ----------
# 中文按 ~1 token/字估算，实际以服务端 usage.prompt_tokens 为准
_FILLER = "人工智能正在改变软件工程的交付方式，从需求分析到运维监控的每个环节都出现了新的工具链。"
CASES = [
    {"name": "short",  "prompt": "用三句话介绍杭州的历史与美食。",
     "note": "典型短对话"},
    {"name": "medium", "prompt": _FILLER * 15 + "\n请总结上文要点。",
     "note": "~500 token 提示词"},
    {"name": "long",   "prompt": _FILLER * 60 + "\n请总结上文要点，并逐条展开分析。",
     "note": "~2400 token 提示词，考察 prefill 压力"},
]


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def get_model_id(session) -> str:
    try:
        d = session.get(f"{LLM_HOST}/v1/models", timeout=(5, 10)).json()
        return d["data"][0]["id"]
    except Exception:
        return "unknown"


def chat_stream(session, prompt: str, max_tokens: int = MAX_TOKENS) -> dict:
    """流式请求，返回 TTFT / 解码速率 / token 数。失败抛异常。"""
    payload = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.time()
    ttft = None                # 首个任意 token（含 reasoning）
    t_content = None           # 首个正文 token（思考型模型会明显晚于 ttft）
    chunks = 0
    usage = {}
    timings = {}               # llama-server 在末尾 chunk 返回的服务端权威计时
    raw_head = []              # 诊断用：保留前几个原始 chunk
    with session.post(f"{LLM_HOST}/v1/chat/completions", json=payload,
                      stream=True, timeout=(10, TIMEOUT)) as r:
        r.raise_for_status()
        # ⚠️ 必须按字节切行再手动解码：decode_unicode 在响应头无 charset 时会按
        # Latin-1 解码，中文 UTF-8 序列中的 0x85（NEL）会被 splitlines 当成换行，
        # 把 JSON 行从中间切碎（本项目实测踩中）
        for line in r.iter_lines():
            if not line or not line.startswith(b"data:"):
                continue
            data = line[5:].strip().decode("utf-8", "replace")
            if data == "[DONE]":
                break
            if len(raw_head) < 3:
                raw_head.append(data[:400])
            obj = json.loads(data)
            if obj.get("error"):
                raise RuntimeError(f"服务端返回错误: {obj['error']}")
            if obj.get("usage"):
                usage = obj["usage"]
            if obj.get("timings"):
                timings = obj["timings"]
            for ch in obj.get("choices", []):
                delta = ch.get("delta") or {}
                # 思考型模型（Qwen3 系等）先输出 reasoning_content，两种都算有效 token
                if delta.get("content") or delta.get("reasoning_content"):
                    if ttft is None:
                        ttft = time.time() - t0
                    if delta.get("content") and t_content is None:
                        t_content = time.time() - t0
                    chunks += 1
    total = time.time() - t0
    if ttft is None:
        raise RuntimeError("流式响应无任何内容 token，原始返回开头: " + " | ".join(raw_head))

    completion_tokens = usage.get("completion_tokens") or timings.get("predicted_n") or chunks
    prompt_tokens = usage.get("prompt_tokens") or timings.get("prompt_n")
    decode_s = total - ttft

    def _r2(x):
        return round(x, 2) if isinstance(x, (int, float)) else None

    return {
        "ttft_s": round(ttft, 3),
        "ttft_content_s": round(t_content, 3) if t_content is not None else None,
        "total_s": round(total, 2),
        "decode_s": round(decode_s, 2),
        "completion_tokens": completion_tokens,
        "prompt_tokens": prompt_tokens,
        "tok_per_s": _r2(completion_tokens / decode_s) if decode_s > 0 else None,
        # 服务端权威计时（llama-server timings）：prefill / decode 分离，比客户端掐表准
        "prefill_tps_srv": _r2(timings.get("prompt_per_second")),
        "decode_tps_srv": _r2(timings.get("predicted_per_second")),
        "cache_n": timings.get("cache_n"),   # >0 表示命中了前缀 KV 缓存（重复请求注意）
    }


def concurrency_probe(session) -> dict:
    """两个相同请求并发提交，测第二个的排队代价（--parallel 1 时应约等于第一个的全程耗时）。"""
    results = [None, None]

    def worker(i):
        try:
            results[i] = chat_stream(session, "用一句话说明量子计算的原理。", max_tokens=128)
        except Exception as e:
            results[i] = {"error": str(e)[:200]}

    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0

    ok = [r for r in results if r and "error" not in r]
    if len(ok) < 2:
        return {"error": "并发探测失败", "detail": results}
    totals = [r["total_s"] for r in ok]
    ratio = max(totals) / min(totals) if min(totals) > 0 else 0
    # 串行（--parallel 1）：后到的请求耗时 = 排队 + 执行 ≈ 2× 单任务 → max/min ≈ 2
    # 真并行：两者都变慢但几乎同时完成 → max/min ≈ 1
    # ⚠️ 不能用 wall vs sum(totals) 判定：串行时后到者的 total 已含排队，sum 双重计数
    if ratio >= 1.5:
        verdict = "串行排队"
    elif ratio <= 1.3:
        verdict = "有并行"
    else:
        verdict = "不确定"
    return {
        "wall_s": round(wall, 2),
        "task_totals_s": totals,
        "ratio_max_min": round(ratio, 2),
        "verdict": verdict,
    }


def main():
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    with requests.Session() as s:
        s.headers["Content-Type"] = "application/json"
        model = get_model_id(s)
        log(f"目标：{LLM_HOST} | 模型：{model}")

        # 健康检查
        try:
            s.get(f"{LLM_HOST}/health", timeout=(5, 10)).raise_for_status()
        except Exception as e:
            log(f"❌ /health 不可达：{e}")
            raise SystemExit(1)

        records = []
        for case in CASES:
            for rep in range(1, REPEAT + 1):
                rec = {"case": case["name"], "rep": rep, "note": case["note"],
                       "cold": rep == 1, "status": "pending"}
                try:
                    rec.update(chat_stream(s, case["prompt"]))
                    rec["status"] = "ok"
                    cache_mark = f" [cache_n={rec['cache_n']}]" if rec.get("cache_n") else ""
                    log(f"[{case['name']} #{rep}] TTFT {rec['ttft_s']}s | "
                        f"prefill {rec['prefill_tps_srv']} tok/s | "
                        f"decode {rec['decode_tps_srv']} tok/s | "
                        f"prompt={rec['prompt_tokens']} completion={rec['completion_tokens']}{cache_mark}")
                except Exception as e:
                    rec.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
                    log(f"[{case['name']} #{rep}] ❌ {rec['error'][:120]}")
                records.append(rec)
                time.sleep(1)

        log("并发探测（2 请求同时提交）...")
        conc = concurrency_probe(s)
        log(f"并发结论：{conc.get('verdict', conc)}")

    # ---------- 汇总：按用例取中位数（cold 首轮单独列出）----------
    summary = {"run_id": run_id, "host": socket.gethostname(),
               "llm_host": LLM_HOST, "model": model,
               "max_tokens": MAX_TOKENS, "repeat": REPEAT,
               "concurrency": conc, "cases": {}}
    for case in CASES:
        ok = [r for r in records if r["case"] == case["name"] and r["status"] == "ok"]
        warm = [r for r in ok if not r["cold"]]
        pool = warm or ok
        if not pool:
            summary["cases"][case["name"]] = {"status": "all_failed"}
            continue
        entry = {
            "n": len(ok),
            "prompt_tokens": ok[0]["prompt_tokens"],
            "ttft_median_s": round(statistics.median(r["ttft_s"] for r in pool), 3),
            "ttft_cold_s": next((r["ttft_s"] for r in ok if r["cold"]), None),
            "tok_per_s_median": round(statistics.median(r["tok_per_s"] for r in pool), 2),
            "total_median_s": round(statistics.median(r["total_s"] for r in pool), 2),
            "prefill_tps_median": round(statistics.median(
                r["prefill_tps_srv"] for r in pool if r["prefill_tps_srv"]), 2),
            "decode_tps_median": round(statistics.median(
                r["decode_tps_srv"] for r in pool if r["decode_tps_srv"]), 2),
        }
        summary["cases"][case["name"]] = entry

    (log_dir / f"llm_bench_{run_id}.json").write_text(
        json.dumps({"summary": summary, "records": records},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    cols = ["case", "rep", "cold", "status", "ttft_s", "ttft_content_s", "total_s",
            "decode_s", "tok_per_s", "prefill_tps_srv", "decode_tps_srv",
            "prompt_tokens", "completion_tokens", "cache_n", "error"]
    with (log_dir / f"llm_bench_{run_id}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)

    print("\n" + "=" * 60)
    print(f"  {model}")
    print("=" * 60)
    for name, c in summary["cases"].items():
        if "status" in c:
            print(f"  {name:8s} 全部失败")
            continue
        print(f"  {name:8s} prompt={c['prompt_tokens'] or '?':>6} tok | "
              f"TTFT 中位 {c['ttft_median_s']}s（冷 {c['ttft_cold_s']}s）| "
              f"prefill {c['prefill_tps_median']} | decode {c['decode_tps_median']} tok/s")
    print(f"  并发：{conc.get('verdict', '失败')} | wall {conc.get('wall_s')}s")
    print("=" * 60)
    print(f"  指标: logs/llm_bench_{run_id}.json / .csv")


if __name__ == "__main__":
    main()
