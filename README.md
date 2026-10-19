# LLM Prefix Cache Eval

面向模型与数据集组合的 Agent 请求采集和 Prefix Cache 命中率评估工具。

当前执行适配已针对 **GLM-5.3 + mini-SWE-agent 2.4.6 + DeepSWE/Pier** 验证；并非任意模型、任意数据集的一键适配器。统计入口可分析任何符合本项目 Usage CSV 字段约定的数据。更换执行模型需要同步修改适配、计量规则并重新验证。

仓库仅包含功能代码、测试、示例配置和依赖信息。不包含 API Key、历史评测结果、模型输入输出、原始日志、断点、数据集副本或第三方源码。

## 功能

- 请求前落盘完整输入，响应后保存完整输出、原始 Usage 和规范化 Token 字段。
- 按 `cached_tokens / prompt_tokens` 计算真实 API 缓存命中率；缺失数据不填零。
- 关联逻辑调用与实际请求，避免重试或多份日志重复累计。
- 保持 mini 原生 Prompt、工具解析和执行循环，宿主调用模型，隔离容器执行命令。
- 官方独立 Verifier 评分、逐任务归档、源码 Hash 和归档完整性核对。
- 积分/步数/时间/Context 保护、额度耗尽停止、干净预算暂停后的显式续跑。
- 自定义命中率分箱、请求占比、算术平均、中位数与 Token 加权指标。

## 安装

使用 Linux 或 WSL2，Python 3.12，以及可正常执行 `docker info` 的 Docker。任务容器不能访问网络或 Docker Socket；Key 留在宿主机。镜像可能较大，下载前应确认本地空间和网络。

```bash
git clone https://github.com/Joha0927/llm-prefix-cache-eval.git
cd llm-prefix-cache-eval
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock -e '.[test]'
```

`requirements.lock` 固定传递依赖，并将原开发环境的本地 editable 路径替换为上游固定 Git Commit。版本信息见 `configs/upstream_versions.json`。安装需要访问 PyPI 和 GitHub；不会自动调用模型。

第三方仓库不随本项目提交。准备固定 DeepSWE 快照：

```bash
git -c core.autocrlf=false clone --no-checkout https://github.com/datacurve-ai/deep-swe.git third_party/deep-swe
git -C third_party/deep-swe -c core.autocrlf=false checkout --detach 0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea
```

不要使用带 CRLF 的 shell 脚本执行 Linux 任务，不要修改 Benchmark 测试或参考答案。其他数据集需要符合 Pier 任务目录协议，并适配当前 `/app` Git 工作目录约定。

## 凭据

```bash
mkdir -p secrets
cp .env.example secrets/glm.env
chmod 600 secrets/glm.env
# 在本地编辑 secrets/glm.env，填写 Key；不要将 Key 放入命令参数。
```

当前使用 OpenAI Chat Completions 协议，端点为 `https://open.bigmodel.cn/api/coding/paas/v4`。自行确认账号、接口及使用场景的服务条款和额度。程序不伪装客户端，不自动切换账号或按量端点。

## 预览与运行

默认只预览任务，不读取 Key，也不调用模型：

```bash
python scripts/run_eval.py --task abs-module-cache-flags
python scripts/run_eval.py --all
```

实际运行需要显式 `--execute` 和已确认可用的积分预算。预算取窗口和每周剩余额度中较小值，不能把“5 小时额度”理解为运行时长：

```bash
python scripts/run_eval.py --task abs-module-cache-flags \
  --experiment-id example --available-points 2000 --execute
```

按清单顺序运行整个任务目录：

```bash
python scripts/run_eval.py --tasks-root third_party/deep-swe/tasks --all \
  --experiment-id dataset-run --available-points 2000 --execute
```

默认每任务 1500 估算积分、启动前额外预留 100、累计 400 步、3 小时活跃执行时间、响应最多 16384 输出 Tokens。观察到输入 Context 超过 400K 后停止；它不是精确服务端前置限制。金额/积分估计不是账单，单个在途请求可能突破阈值。积分系数针对 GLM-5.3 固定，周一至周五 UTC+8 的 14:00–18:00 为高峰，其余按半价估算；供应商规则变化后需更新验证。

同一实验使用锁文件防止并发。失败或预算暂停会停止序列，已有文件不覆盖，不在额度恢复后自动继续。重新启动会生成新的 Run ID，**没有自动跳过旧任务**；通过 `--task` 明确选择尚未执行的任务，避免把 `--all` 当作自动恢复。

## 输出与统计

```text
data/experiments/<experiment_id>/tasks/<task_id>/runs/<run_id>/
  usage.csv             每次实际请求的 Token、命中率、状态与估算积分
  requests.jsonl        完整模型输入输出和原始 Usage 的追加事件
  config.json           配置、任务文件 Hash 与源码 Hash
  status.json           执行与评分状态
  manifest.json         本次归档文件的 SHA256
  native/               Pier/mini 原生轨迹、Patch、官方评分
  source_snapshot/      本次适配器源码
```

`session_id` 是本实现的轨迹会话标识；`call_id` 是逻辑调用，`attempt_id` 是实际请求。只有已提交的 Git 修改进入官方评分。不要将 Provider、SDK 和原生轨迹三份 Usage 相加。

合并多条轨迹统计时，显式列出所需 CSV。相同 `attempt_id` 的一致副本只计一次，冲突副本报错：

```bash
python scripts/cache_distribution.py path/to/run-a/usage.csv path/to/run-b/usage.csv \
  --output reports/cache_distribution.json
python scripts/audit_run.py path/to/run-directory
```

默认端点为 `0, 0.1, ..., 0.9, 0.91, ..., 0.99, 0.992, 0.994, 0.996, 0.998, 1`。使用精确分数比较，左闭右开，末档含 1。可通过 `--edges` 提供其他端点。失败/无效 Usage 不填零，报告其排除数量与覆盖率；研究结论需考虑任务内相关性、冷暖缓存和长度分布。

## 断点恢复

干净预算/额度暂停会保留完整对话、累计步数/积分/活跃时间、项目 tar、Hash manifest 和已停止容器。仅在同一 Docker 主机、保留容器仍存在且 Hash 通过时显式恢复：

```bash
python scripts/run_eval.py --task TASK_ID --resume path/to/paused-run \
  --experiment-id continuation --points-limit 1500 --total-points-limit 3500 \
  --available-points 2000 --execute
```

`--points-limit` 控制本段；`--total-points-limit` 控制包含旧消耗的累计上限。不要清零旧消耗或把跨窗口续跑当作连续冷启动样本。服务端缓存可能过期。任意崩溃、工具执行途中、丢失容器和平台迁移不在已验证恢复范围内。

归档故障会保留原始失败，停止序列。只有确保原模型已完成提交、Commit 与二进制 Patch Hash 完全一致时，才可独立恢复官方评分；不得修改补丁后冒充原始结果。

## 测试与目录

```bash
python -m pytest -q
```

测试使用 Mock HTTP，不使用真实 Key，不调用付费模型。包括采集前后行为一致性、gzip/SSE、重试计数、缺失 Usage、取消、写盘错误、预算入口和分箱边界。

`src/glm_trace/` 是核心适配与采集；`scripts/` 是运行、审计和统计入口；`tests/` 是无模型回归；`configs/` 是公开的参考配置。`data/`、`reports/`、`secrets/`、缓存和第三方源码均被忽略。

此仓库发布版未包含本机历史实验专用的 Run ID、恢复脚本或实验记录。未为项目新增开源许可证；上游来源及必要版权声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。


## 评测收尾后的代码快照（2026-10-19）

本版同步已完成评测所用的采集/宿主适配、累计断点、私有目录工作区归档、截止保护与JSONL审计修复，并保留原可移植顺序CLI。`scripts/live_mirror.py`提供失败不丢失原任务监督的原子副本工具。机器专用并发协调器、一次性恢复入口和历史状态保留在本地实验档案，不作为跨机器可执行CLI发布。默认仍仅预览，不启动新模型请求。

`analysis/theoretical_prefix_cache/core.py`为纯离线分析内核，支持历史前缀LCP及Token/Block16/Block64粒度；不读取凭据或调用模型。使用该内核前安装同目录的`requirements.txt`。分词器与模板需调用方另行按固定版本取得，未随仓库分发；理论缓存不能替代Provider实测Usage。

```bash
pip install -r analysis/theoretical_prefix_cache/requirements.txt
PYTHONPATH=src:scripts:analysis/theoretical_prefix_cache python -m pytest -q tests analysis/theoretical_prefix_cache/test_core.py
```

显式无限积分仅为宿主适配的独立授权接口能力，现有通用CLI继续要求有限预算；不会自动继承本地历史恢复授权或跳过真实额度保护。环境截止与共享停止参数仅为可选宿主保护，不改变模型Prompt和工具协议。


### 精简后的运行边界

顺序入口复用同一`live_mirror`原子副本实现，临时副本复制失败仅记录诊断并继续监督原Pier；子进程退出后的最终复制及归档验收仍严格失败，不会降级为成功。相同副本目标只写一次，结果JSON只解析一次。模型请求、Usage计数、断点计数和官方评分逻辑未改。
