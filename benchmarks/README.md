# Benchmarks

适用版本：v2.5.8。

四个可复跑基准，全部支持 `--json` 机器可读输出。**离线两项**不需要服务和 API Key，可以直接跑（CI / 评审同样适用）；**在线两项**打的是真实上游模型，会消耗 token。

| 脚本 | 测什么 | 前置条件 |
| --- | --- | --- |
| `bench_rag_retrieval.py` | 检索延迟 avg/P50/P95 + Recall@K / MRR（与 eval 同口径） | 无（离线） |
| `bench_semantic_cache.py` | 语义缓存 store / lookup 延迟、精确命中率、改写命中率、误命中率；支持 `--provider hash\|onnx` 对照与 `--compare` 产出结构化 evidence | 无（hash 离线）；ONNX 需 `requirements-rag.txt` + 本地模型/tokenizer |
| `bench_chat_latency.py` | 流式 TTFT、总延迟、token 用量、语义缓存命中分布 | 本地服务 + DeepSeek Key |
| `bench_agent_dag.py` | 多 Agent DAG 端到端延迟、每 Agent 耗时、token / 成本 | 本地服务 + DeepSeek Key |

```bash
python benchmarks/bench_rag_retrieval.py
python benchmarks/bench_semantic_cache.py --provider hash
python benchmarks/bench_semantic_cache.py --compare --out docs/evidence/semantic-cache-onnx-v2.5.8.json --markdown docs/evidence/semantic-cache-onnx-v2.5.8.md
# ONNX model 可用时自动计入 comparison：
python benchmarks/bench_semantic_cache.py --compare --onnx-model /models/bge-micro.onnx --tokenizer /models/tokenizer.json --dimensions 384
# 先启动服务（python app.py），再：
python benchmarks/bench_chat_latency.py --n 3
python benchmarks/bench_agent_dag.py
```

实测样例数字与解读见 README「Benchmarks」一节。基准只测本仓库可控的部分：离线项数字逐次可复现（检索基准钉 `PYTHONHASHSEED`）；在线项受上游负载影响，应报告分位数而不是单次值。Hash embedding 是零依赖默认档（exact hit rate 1.0，false hit rate 0.0，改写命中 0.0 是阈值 0.95 下的预期行为）；ONNX 作为可选增强，通过 `--compare` 生成结构化 evidence 后再决定是否切默认。语义缓存 evidence 见 [docs/evidence/semantic-cache-onnx-v2.5.8.json](../docs/evidence/semantic-cache-onnx-v2.5.8.json)。
