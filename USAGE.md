# llmctl 使用手册

**语言：** 中文 | [English](USAGE_EN.md)

## 安装后的第一组命令

```bash
sudo llmctl status
sudo llmctl health
sudo llmctl smoke --full
sudo llmctl key show
sudo llmctl admin show
sudo llmctl admin set-password
```

服务器从 UTC 改为中国标准时间：

```bash
sudo llmctl timezone set Asia/Shanghai
```

Web 管理界面默认地址为 `http://服务器IP:8000/ui`，管理员用户名 `admin`，初始通用密码 `llm-admin`。密码会写在 root-only 的 `/etc/llm-cluster/secrets.env`，也可用 `sudo llmctl admin show` 查看。首次登录后请修改。

## SSH 断开或安装窗口看起来不动

systemd 接管后，SSH 会话断开不会终止 Worker。重新登录后运行：

```bash
systemctl status llm-cluster.service --no-pager -l
sudo llmctl startup watch
sudo llmctl status
sudo llmctl health
journalctl -u llm-cluster.service -f
```

`startup watch` 每 10 秒汇总一次 `healthy/loading/pending/failed`、每个 Worker 状态和每张卡显存。Worker 加载大模型时，`GPU-Util` 可能接近 0%，但显存会逐步增长。看单个 Worker：

```bash
sudo llmctl logs worker 0 -f
nvidia-smi
```

若总控显示 `active (exited)` 或 `Finished llm-cluster.service`，这是正常的 `oneshot + RemainAfterExit` 状态；真正的推理进程位于 `llm-worker@N.service`。

## Worker 管理

```bash
sudo llmctl start all
sudo llmctl start 0,2,4
sudo llmctl stop all
sudo llmctl stop 1,3
sudo llmctl restart all
sudo llmctl restart 0,1
sudo llmctl shutdown
```

`start/stop/restart` 默认不改变下次开机列表；`shutdown` 并发停止 Router、数据库和全部 Worker，也不改变下次开机列表。持久管理：

```bash
sudo llmctl enable 0,1,2,3
sudo llmctl disable 6,7
sudo llmctl activate 4,5
sudo llmctl deactivate 4,5
sudo llmctl scale 6
sudo llmctl autostart status
sudo llmctl autostart enable
sudo llmctl autostart disable
```

- `enable/disable`：只改开机列表。
- `activate/deactivate`：同时改开机列表、运行状态和 LiteLLM 后端。
- `scale N`：持久设置为前 N 个实例。
- `all` 对 `start/restart` 表示持久激活列表；`stop all` 会停止所有可能实例。

并行启动数量：

```bash
sudo llmctl tune set startup-parallelism 8
sudo llmctl restart all
```

8 个并行加载启动更快，但 CPU、系统内存和磁盘读取峰值更高。启动期间每个 Worker 分别加载一份模型；这不是 GPU 间权重共享。

启动命令按批次并发发起，并持续输出整个批次的聚合进度，不会再逐个 Worker 静默等待。直接执行 `systemctl start llm-cluster.service` 时，systemd 客户端本身不会转发服务日志；请另开窗口或 SSH 重连后运行 `sudo llmctl startup watch`。

## LiteLLM 路由

```bash
sudo llmctl router status
sudo llmctl router restart
sudo llmctl database status
```

默认 `least-busy` 根据每个后端未完成请求数选择 Worker，并结合 `max_parallel_requests=max-num-seqs` 限流。它不会直接读取 GPU 显存、KV Cache 或图片大小，因此不是完美的“按工作量”调度。大请求仍可能造成负载不均；需要用业务压测观察，再考虑请求分类、不同模型别名或独立长上下文池。

## OpenAI 兼容 API

先获取参数：

```bash
sudo llmctl key show
```

文本请求：

```bash
curl http://服务器IP:8000/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"安装摘要中的模型名",
    "messages":[{"role":"user","content":"你好"}],
    "max_tokens":256
  }'
```

支持请求级关闭思考的模型可加入：

```json
{
  "reasoning_effort": "none",
  "chat_template_kwargs": {"enable_thinking": false}
}
```

不支持思考开关的模型不要发送这些字段。查看当前能力：

```bash
sudo llmctl status
```

工具调用遵循 OpenAI `tools`/`tool_choice` 格式。模型只会返回工具调用意图；业务程序仍必须执行函数，并把 `tool` 消息返回模型。脚本只对识别出的模型协议开启 vLLM tool parser。

## 图片与 OCR

只有 `llmctl status` 显示支持图片时才能使用：

```bash
sudo llmctl ocr /path/to/image.png '逐字识别全部内容'
```

默认每请求上限 8 张，完整验收包含单请求 6 张：

```bash
sudo llmctl smoke --full
sudo llmctl tune set max-images 8
sudo llmctl restart all
```

API 使用 OpenAI `image_url` 内容块。默认推荐 `data:image/...;base64,...`；服务没有放行任意外部媒体域名。

## 参数调整

```bash
sudo llmctl tune show
sudo llmctl tune set max-model-len 32768
sudo llmctl tune set max-num-seqs 7
sudo llmctl tune set max-num-batched-tokens 8192
sudo llmctl tune set gpu-memory-utilization 0.92
sudo llmctl tune set routing-strategy least-busy
```

修改显存或 Worker 参数后需 `restart all`。提高 `max-model-len`、`max-num-seqs` 或图片上限会增加峰值显存；不要只因为空闲时还有几 GiB 就盲目打满，CUDA Graph、临时张量、图片编码和请求长度波动都需要余量。

## 性能验收

```bash
sudo llmctl bench --concurrency 25 --requests 50 --max-tokens 512
```

输出包括聚合 token/s、单请求有效 token/s p50/p95 和请求耗时。对“25 平均并发、40 token/s”这类目标，至少分别测试短文本、长 prompt、思考、工具调用和 6 图请求。256K 是单请求最大窗口，不等于 25 个请求都能同时使用 256K。

## 模型搜索与重配

```bash
sudo llmctl models hardware
sudo llmctl models search Qwen --source all --task auto
sudo llmctl models inspect modelscope Qwen/Qwen3-8B
sudo llmctl models current
```

切换到不同模型：回到项目目录，重新运行安装器。

```bash
sudo bash install-llm-cluster.sh --force-reconfigure
```

这样会重新检查权重、架构、TP、上下文和能力解析器。直接 `llmctl download` 只允许重新核验/补齐当前模型，不允许绕过规划切换模型。

## 镜像维护、代理和离线包

脚本不会自动更新。显式维护：

```bash
sudo llmctl proxy set 10.1.0.6 7890 http
sudo llmctl update --vllm-image vllm/vllm-openai:v0.22.1
sudo llmctl proxy clear
```

导出/导入：

```bash
sudo llmctl offline export /data/offline/llm-bundle
sudo llmctl offline import /data/offline/llm-bundle
```

## 日志

```bash
sudo llmctl logs worker 0 -f
sudo llmctl logs router -f
sudo llmctl logs database -f
journalctl -u llm-cluster.service -n 300 --no-pager
```

常见判断：

- `curl: (7) ... 810N` 在 Worker 加载阶段只是健康检查尚未通过，不代表已失败。
- Worker 显存停止增长且日志长期无变化：查看是否在编译 CUDA Graph、CPU 内存/磁盘是否饱和，以及服务是否仍 active。
- Worker 退出：日志末尾通常会给出 OOM、架构、量化内核或模型代码错误。
- LiteLLM 不健康但 Worker 健康：检查 `llm-router` 和 `llm-database` 日志。

## 卸载

默认保留模型与 LiteLLM 数据库：

```bash
sudo llmctl uninstall
```

卸载会并发停止 Router、数据库和全部 Worker，并每 5 秒显示仍活动的 systemd 单元、容器和 GPU 显存。正常停止总等待上限为 180 秒；超时后只对 `llm-*` 命名的本集群单元/容器执行一次有界强制停止，仍失败则保留配置并明确退出，不会边运行边删除。删除包含大量小文件的编译缓存、以及显式 `--purge-model` 的模型目录时也会持续输出心跳，不会再长时间静默。

可选永久删除：

```bash
sudo llmctl uninstall --purge-model --purge-images --purge-database
```

`--purge-model` 只有在模型目录存在安装器标记时才允许执行；`--purge-database` 会永久删除 Web UI 数据、虚拟 key 和管理记录。
