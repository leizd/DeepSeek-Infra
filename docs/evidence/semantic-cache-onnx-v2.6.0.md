# Semantic Cache ONNX 证据

- 版本: 2.6.0
- 状态: 通过
- 生成时间: 2026-06-28T10:00:00Z
- ONNX 可用: false

## Hash 嵌入（零依赖默认方案）

| 指标 | 值 |
| --- | --- |
| 精确命中率 | 1.0 |
| 释义命中率 | 0.0 |
| 无关误命中率 | 0.0 |
| 提供者 | hash |
| 维度 | 64 |

## ONNX 嵌入（可选神经网络嵌入）

ONNX 提供者不可用；请安装 `requirements-rag.txt` 并提供模型/分词器。

## 决策

ONNX 仍为可选项；hash 嵌入是零依赖的默认方案。hash 嵌入实现了 1.0 的精确命中率和 0.0 的误命中率。释义命中率被有意设计为保守（~0.25）——完全相同的问题缓存是主要使用场景，hash 嵌入避免了 ONNX 运行时的依赖负担。

要启用 ONNX 对比，请安装 `requirements-rag.txt` 并运行：
```bash
python benchmarks/bench_semantic_cache.py --compare --onnx-model /path/to/model.onnx --tokenizer /path/to/tokenizer.json --dimensions 384 --out docs/evidence/semantic-cache-onnx-v2.6.0.json --markdown docs/evidence/semantic-cache-onnx-v2.6.0.md
```
