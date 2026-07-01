# Security Smoke Checklist

适用版本：v2.6.7。

这页是 DeepSeek Infra 安全能力的**最小可复现命令集**：任何人克隆仓库后，无需 API Key、无需联网，都能在本地验证 Tool Policy、Context Taint 防火墙与 Prompt Injection 评测门禁是否工作。先跑通这套冒烟，再去看 [THREAT_MODEL.md](THREAT_MODEL.md) 的威胁分类与 [SECURITY.md](SECURITY.md) 的数据驻留口径。

## 1. 前置：安装依赖

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
```

全部离线。`pytest`、`ruff`、`mypy` 与 evals 都不需要外部服务或密钥。

## 2. Tool Policy 硬门禁（CI 必过项）

26 个固定攻防用例回放：SSRF / 路径越界 / 密钥外泄 / 敏感记忆写入 / capability 切片 / 确认门 / 注入清洗。**任何一个误判都会让 CI 失败**（`exit 1`）。

```bash
python evals/runners/run_tool_eval.py
```

预期：

```
=== Eval Report · tool-policy ===
Cases: 26
Tool Policy Pass Rate: 1.000
Prompt Injection Defense Pass: 1.000
```

期望 Pass Rate = `1.000`。低于此值说明策略回归，不可发布。

## 3. Prompt Injection 对抗评测 hard gate（v2.3.0）

30 个对抗样本（中文 / 英文 / Base64 / Markdown hidden instruction / 多轮诱导 / 良性样本）输出 `blockRate` / `falsePositiveRate` / `bypassRate`，并对照版本化阈值做门禁：

```bash
python evals/runners/run_injection_adversarial.py
```

阈值（v2.2.6 baseline）：

| 指标 | 阈值 | 说明 |
| --- | --- | --- |
| `blockRate` | `>= 0.85` | 攻击样本被防火墙命中 |
| `falsePositiveRate` | `<= 0.10` | 良性样本不被误伤 |
| `bypassRate` | `<= 0.15` | 等价于 `1 - blockRate` |

预期：

```
Soft Gate: PASS (all thresholds met)
  - blockRate: 1.000 >= 0.85 [PASS]
  - falsePositiveRate: 0.000 <= 0.1 [PASS]
  - bypassRate: 0.000 <= 0.15 [PASS]
```

- **hard gate（v2.3.0 起 CI 必过）**：加 `--strict` 让未达标返回 `exit 1` 阻断 PR；CI 已接入。本地不加 `--strict` 仍只 warning 便于迭代。

```bash
python evals/runners/run_injection_adversarial.py --strict --no-report
```

## 4. Context Taint / Tool Policy 单元测试

纯函数与策略引擎的回归测试（含 v2.2.6 新增的 deny `reason` / `suggestion` 字段断言与 per-category `scan_text` 矩阵）：

```bash
pytest tests/test_context_taint.py tests/test_tool_policy.py -q
```

## 5. v2.4 版本化安全语料库

`evals/golden/security/` 固化三份可回归语料：prompt injection、tool policy attacks 和 benign false-positive。`run_security_corpus.py` 会输出 security corpus report，并在 strict 模式下检查：

- `blockRate >= 0.85`
- `falsePositiveRate <= 0.10`
- `bypassRate <= 0.15`
- `toolPolicyPassRate == 1.00`
- `secretExfiltrationBlockRate == 1.00`
- `ssrfBlockRate == 1.00`
- `pathTraversalBlockRate == 1.00`

```bash
python evals/runners/run_security_corpus.py --strict --out evals/reports/security-latest.json --markdown evals/reports/security-latest.md
```

## 6. 运行时防火墙状态（需要本地服务）

如果已起了一个本地 server（`python launch.py --server`，默认 `127.0.0.1:8000`），可以核对 Context Taint 防火墙的实时配置：

```bash
# 需要 .env 里的 API_TOKEN（或 DEEPSEEK_API_TOKEN）
TOKEN=$(grep -E '^(API_TOKEN|DEEPSEEK_API_TOKEN)=' .env | head -1 | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/taint | python -m json.tool
```

预期返回 `contextTaint.enabled: true`、`escalateConfirm: true`、`sources` 列出信任层级、`sensitiveToolNames` 包含 `forget_memory`。

另一个活体验证入口是 `GET /api/tool-policy`，返回最近审计条目（含 deny `reason` / `suggestion` / `risk`）。

## 7. CI 安全扫描（可选，本地复现）

```bash
pip-audit -r requirements.txt -r requirements-dev.txt          # 依赖 CVE
bandit -r deepseek_infra --severity-level high -q               # 只看高危静态问题
detect-secrets scan --baseline .secrets.baseline               # 凭证扫描（务必带 --baseline）
```

`detect-secrets` **必须**带 `--baseline`：测试夹具里有故意写入的假密钥，baseline 已审阅放行。

## 验收口径

| 能力 | 验证命令 | 状态 |
| --- | --- | --- |
| Tool Policy 硬门禁 | `run_tool_eval.py` | `exit 0`，Pass Rate 1.000 |
| Injection soft gate | `run_injection_adversarial.py` | `Soft Gate: PASS` |
| Injection hard gate（可选） | `run_injection_adversarial.py --strict` | `exit 0` |
| Security corpus hard gate | `run_security_corpus.py --strict` | `exit 0`，全部 v2.4 metrics PASS |
| Context Taint 单元覆盖 | `pytest tests/test_context_taint.py` | 全绿 |
| Tool Policy 单元覆盖 | `pytest tests/test_tool_policy.py` | 全绿 |
| 防火墙运行时状态 | `GET /api/taint` | `enabled: true` |
