# Qwen3.8 Flash Next 在 8×RTX PRO 6000D 上部署

本项目为 Qwen3.8 Flash Next 提供专用 vLLM 预览运行时、TP2、PLE CPU offload、专家并行、MTP、KV Cache 精度和静态 YaRN 的完整配置链路。

> 重要：Qwen3.8 Flash Next 和对应 vLLM 支持仍是预览状态。一键入口会直接部署四个同构 TP2 实例，但只在逐实例真实生成通过后上线；失败自动恢复，成功后也可显式回滚。需要更保守的分阶段验收时，仍可使用下方通用表单先做单组金丝雀。

## 推荐的一键部署

升级到 LLMCtl 3.6.7 后，进入管理后台“模型部署”，使用首屏的“Qwen3.8 Flash Next 一键部署”：

1. 页面自动读取八张 GPU 的实际拓扑并显示四个 TP2 分组。
2. 推荐参数已经填好；“高级设置”默认折叠，一般无需修改。
3. 点击“开始自动部署并上线”。后台会从 ModelScope 下载固定 revision，依次备份、部署，并在逐实例真实图文生成通过后把 `gdn-inside` 切换到 Qwen。
4. 下载或预检失败不会修改当前服务；修改 Worker 后失败会自动恢复。
5. 成功后页面显示“恢复到部署前状态”，可一键恢复原模型、Worker 和路由。

下面的命令行和通用表单仅用于需要手工控制的高级场景。

## 本机推荐结果

针对 8 张 84GB RTX PRO 6000D、512GB 主内存、无 NVLink、PCIe 4.0，当前推荐四个完全一致的实例：

| 项目 | 统一配置 | 原因 |
| --- | --- | --- |
| 模型 | ModelScope `RadixArk/Qwen3.8-Flash-Next-NVFP4` | 约 135GB；NVFP4 路由专家 + FP8 PLE，减少国内下载和主机存储压力 |
| revision | `a6cc3dfc4d4d4617b6ede29f53e751215510e681` | 与 HF RadixArk `7b719225...` 的关键清单和抽样权重哈希一致 |
| vLLM | `vllm/vllm-openai:qwen38-flash-next` | 普通 vLLM 0.22.1 不支持该架构 |
| 拓扑 | 4×TP2，并开启 EP | 两卡通信可控；避免 TP8 在 PCIe 上放大 AllReduce 与专家通信 |
| 上下文 | 原生 `262144`，YaRN=`1` | 不牺牲常用短上下文质量；满足不少于 256K 的要求 |
| 调度序列 | 每实例 `8` | 支持 4–8 路普通长度并发；不表示 8 路都能同时占满 262K |
| PLE | CPU offload 开启 | 51B N-gram 表放入主内存，显著释放显存给 KV Cache |
| KV Cache | `auto`，当前为 BF16 | 当前专用 QSA 后端的稳定路径 |
| MTP | 初始 `0`；验收后 A/B `2` | 先建立可解释基线，再判断投机解码是否真正降低业务延迟 |
| 前缀缓存 | 初始关闭 | 当前预览分支存在混合 GDN/QSA 前缀缓存稳定性报告 |
| 显存比例 | `0.90` | 在 84GB 卡上保留 CUDA Graph、GDN/QSA 与多模态工作区余量 |
| 图片输入 | 每请求最多 `4` 张，可调 `1–16` | checkpoint 已包含视觉编码器；图片数、分辨率和文本共同占用 262K 上下文与处理预算 |
| 批处理 Token | `8192` | 兼顾 chunked prefill、并发和 GDN 状态要求 |
| 启动并行度 | `4` | 四个 TP2 实例同时加载；允许 PLE 和 Linux 页缓存使用全部可用主内存，不预留固定空闲量 |

四组 GPU 不应只按编号猜测。先运行：

```bash
nvidia-smi topo -m
nvidia-smi topo -p2p r
nvidia-smi topo -p2p w
numactl -H
```

每个 TP2 组优先选择同一 PCIe switch，其次同一 Root Complex、同一 NUMA 节点，最后才跨 CPU socket。若 `0+1、2+3、4+5、6+7` 不是最短路径，应在部署计划中显式调整实例的 `gpu_devices`，不要继续使用相邻编号假设。

## 创建原生 262K 部署

全新安装或高级手工部署可使用固定 revision：

```bash
sudo bash install-llm-cluster.sh \
  --yes \
  --model-source modelscope \
  --model-id RadixArk/Qwen3.8-Flash-Next-NVFP4 \
  --model-revision a6cc3dfc4d4d4617b6ede29f53e751215510e681 \
  --tp-size 2 \
  --max-model-len 262144 \
  --max-num-seqs 8 \
  --active-instances 1 \
  --startup-parallelism 4 \
  --gpu-memory-utilization 0.90 \
  --max-num-batched-tokens 8192 \
  --ple-cpu-offload enabled \
  --expert-parallel enabled \
  --prefix-caching disabled \
  --flashinfer-autotune disabled \
  --mtp-speculative-tokens 0 \
  --kv-cache-dtype auto \
  --yarn-factor 1
```

已有集群应在管理后台打开“模型部署”，点击“Qwen3.8 NVFP4 预设”，第一次只选择拓扑最优的一组两张 GPU。金丝雀通过后编辑同一部署，再选择全部八张 GPU 并核对四个 TP2 分组。四组运行参数保持一致。页面会先生成只读计划；只有确认后才下载、备份、重载 Worker 和发布路由。

不要在第一次加载时同时开启 MTP、前缀缓存、NVFP4 KV 或 1M YaRN。多个优化一起变化会让失败无法归因。

视觉能力不需要再安装第二个模型。该 checkpoint 的 `vision_config` 和视觉权重会与语言模型一起下载、加载；Worker 通过 `--limit-mm-per-prompt` 强制执行每请求图片数。图片经视觉处理器转换成 Token，所以“最多 4 张”不是容量保证：高分辨率图片、长文本、较大输出和并发请求仍需共同落在 262K 上下文与显存预算内。LLMCtl 上线前发送最多两张内嵌小图验证真实图文链路，`llmctl smoke --full` 再按配置验证多图输入。

## 验收并尝试 MTP2

先保留 `MTP_SPECULATIVE_TOKENS=0`，至少完成：

```bash
sudo llmctl status all
sudo llmctl health
sudo llmctl smoke --full
sudo llmctl optimize
```

业务压测按 `C1/C2/C4/C8` 采集 TTFT、ITL/TPOT、端到端延迟、单实例与整机输出 Token/s、KV Cache 峰值、排队、抢占、GPU 利用率、PCIe 流量和主内存。长上下文必须包含 256K 附近的有界检索样例，不能只证明服务能启动。

基线稳定后，把同一部署的 `MTP 草稿 Token` 改为 `2`，保持其他字段不变并重复相同负载。只有以下条件同时满足才保留 MTP2：

- 目标业务的 TTFT/TPOT 或总吞吐有可重复提升；
- 接受率足以覆盖额外草稿计算；
- C1–C8、长对话、视觉和工具调用无质量或稳定性回退；
- GDN 状态显存、KV 抢占和尾延迟仍在预算内。

MTP3 不是默认的“更高等级”。草稿越多，接受时一次可前进更多 Token，但被拒绝的计算和每序列状态显存也更多。对 4–8 并发实例，MTP2 通常是更合理的第一档。

全新安装的单实例金丝雀验收后再扩到四个同构实例：

```bash
sudo llmctl scale 4
sudo llmctl smoke --full
```

## 1M 上下文与 NVFP4 QSA KV

本次生产目标不启用 1M。代码支持显式的全池静态 YaRN 2×/4×，但不会把四个实例拆成不同上下文池。当前 84GB TP2 预算可容纳约一条完整 1M 请求/实例，不能同时证明 4–8 路 1M；而 Static YaRN 4× 会作用于所有短请求。若未来重新评估，必须让全部实例统一为：

```text
MAX_MODEL_LEN=1000000
YARN_FACTOR=4
```

Worker 会自动注入 `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1` 和官方 `rope_parameters` 覆盖。上线前必须重新做短上下文质量对照和 1M 检索验收。

`KV_CACHE_DTYPE=nvfp4` 也已进入配置契约，但当前 Qwen3.8 专用 QSA 后端只声明 `auto/bfloat16`。LLMCtl 会在下载大权重前读取目标镜像的 QSA 支持列表；不支持时失败关闭。即使未来可用，NVFP4 KV 主要是容量选项，不是无条件性能选项：SM120 的公开结果显示 decode 可能更快，但长提示 prefill 可能明显慢于 FP8/BF16 路径。

## 回退到 Ornith 或重新分配 TP

每次部署在停止 Worker 前都会保存注册表、全局配置、受影响 Worker 私有环境和接入层计划。模型权重不会因回退被删除。

部署失败会自动恢复。成功后可在后台任务卡点击“回滚到部署前”，或执行：

```bash
sudo llmctl model rollback DEPLOYMENT_JOB_ID
```

回退流程先恢复旧 Worker 配置，等待 Ornith 实际健康，再把公开路由切回，避免把流量提前送往仍在加载的旧模型。

重新部署 Ornith 1.5 时，创建或载入 Ornith 自己的部署对象，并按它的显存计划选择 TP1/TP2/TPn。Qwen 的 PLE、EP、MTP、KV Cache 和 YaRN 字段保存在 Qwen 部署的 Worker 私有环境中，不会作为 Ornith 的隐式默认值。提交前重点核对计划中的受影响 Worker、GPU 分组、TP 和回退快照。

## 常见失败

| 现象 | 处理 |
| --- | --- |
| 镜像不注册 `Qwen4ExpForConditionalGeneration` | 使用专用 `qwen38-flash-next` 镜像，不下载权重 |
| ModelScope 报 `/root/.modelscope` 只读 | 升级到 3.6.7；控制服务会把 SDK home、cache 和 `--cache-dir` 固定到模型盘私有目录 |
| 下载结束后控制服务持续大量写盘 | 升级到 3.6.7；命令输出持久化已限制为最后 100 行，避免进度条日志写放大 |
| PLE 启动时报缺少 `ngram_embedding.weight_scale` | 升级到 3.6.7；LLMCtl 会从当前基础镜像 ID 构建最小 FP8-PLE resolver 派生层，原镜像和权重保持不变 |
| 在 `starting` 阶段点击安全取消后仍等待 | 升级到 3.6.7；并行 Worker 健康等待现在每 3 秒检查取消标志并立即进入回滚 |
| 图片请求被拒绝为数量超限 | 在一键页面调整“每请求最大图片数”；推荐保持 4，不要只修改客户端 |
| NVFP4 QSA KV 能力校验失败 | 改回 `auto`；不要用普通 NVFP4 KV patch 冒充 QSA 支持 |
| PLE 进程无法交接 CUDA 句柄 | 确认容器具有最小的 `SYS_PTRACE` capability；LLMCtl 只在 PLE 开启时添加 |
| 四组并行加载时实际触发主内存 OOM | 任务会失败并回滚；若仍要降低峰值，再临时改回分组启动 |
| 共享前缀长对话触发 GPU 异常 | 保持前缀缓存关闭，升级到修复后的专用镜像后重新验收 |
| MTP 后吞吐没有提升或尾延迟变差 | 回到 MTP0；接受率不足时更多草稿只增加无效计算 |
| 需要恢复旧模型 | 使用成功任务的回滚入口；不要删除旧权重或手工覆盖 Worker 环境 |

## 依据

- [Qwen3.8 Flash Next 官方仓库](https://github.com/QwenLM/Qwen3.8-Flash-Next)
- [vLLM 官方 Qwen3.8 Flash Next recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next)
- [vLLM Qwen3.8 Flash Next 支持 PR](https://github.com/vllm-project/vllm/pull/53896)
- [SM120 NVFP4 KV Cache 性能跟踪](https://github.com/vllm-project/vllm/issues/50416)
- [Qwen3.8 前缀缓存稳定性报告](https://github.com/vllm-project/vllm/issues/54173)
