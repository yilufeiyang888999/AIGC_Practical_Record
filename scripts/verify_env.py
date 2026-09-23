# verify_env.py
import torch, sys

print("PyTorch:", torch.__version__, "| CUDA runtime:", torch.version.cuda)
print("CUDA 可用:", torch.cuda.is_available())
if not torch.cuda.is_available():
    sys.exit(1)

cap = torch.cuda.get_device_capability(0)
arch = torch.cuda.get_arch_list()
tag = f"sm_{cap[0]}{cap[1]}"

print("显卡:", torch.cuda.get_device_name(0))
print("算力:", cap, f"({tag})")
print("内核架构列表:", arch)

ok = tag in arch
print("校验:", "通过" if ok else "失败 —— 该 PyTorch 构建不含本机内核，必须更换 wheel")
print("显存总量:", round(torch.cuda.get_device_properties(0).total_memory/1024**3, 1), "GB")

# 真正跑一次 kernel，别只看 is_available
x = torch.randn(1024, 1024, device="cuda", dtype=torch.float16)
y = x @ x
torch.cuda.synchronize()
print("矩阵运算: 通过", tuple(y.shape))

sys.exit(0 if ok else 1)