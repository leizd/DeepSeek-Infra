"""Offline fixture capture from original pure functions, without importing runtime stores."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "deepseek_infra/infra/agent_runtime/multi_agent.py"
TOOLS = ROOT / "deepseek_infra/infra/tool_runtime/tool_policy.py"
OUTPUT = ROOT / "rust/crates/deepseek-policy/tests/fixtures/multi_agent_chat.json"
constants = {"AGENT_PROFILES", "THINKING_CAPABLE_MODEL", "MULTI_AGENT_PER_AGENT_SEARCH_LIMIT", "PLANNER_SYSTEM",
             "WORKER_OUTPUT_TEMPLATE", "CRITIC_VERDICT_INSTRUCTION", "SYNTHESIZER_SYSTEM", "EMPTY_SYNTHESIS_FALLBACK",
             "_SECTION_ALIASES", "_ATX_HEADER_RE", "_BOLD_HEADER_RE", "_LABEL_HEADER_RE", "REVISION_TARGETS"}
functions = {"agent_model_for", "model_supports_thinking", "agent_tools_for", "_section_key_for_title", "_header_section_key",
             "parse_structured_agent_output", "displayable_agent_content", "parse_critic_verdict", "build_prior_context",
             "_agent_payload_for", "_build_agent_result", "search_source_note", "_format_agent_for_synthesis",
             "synthesis_messages", "agent_base_payload", "agent_messages", "extract_json_object", "failed_agent_output",
             "cache_usage_summary", "agent_cache_for_diagnostics"}
source = REFERENCE.read_text(encoding="utf-8")
tree = ast.parse(source)
nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
for node in tree.body:
    names = {target.id for target in node.targets if isinstance(target, ast.Name)} if isinstance(node, ast.Assign) else (
        {node.target.id} if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) else set())
    if names & constants or isinstance(node, ast.FunctionDef) and node.name in functions:
        nodes.append(node)
tool_tree = ast.parse(TOOLS.read_text(encoding="utf-8"))
profiles = next(node.value for node in tool_tree.body if isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name) and node.target.id == "CAPABILITY_PROFILES")
capabilities = {ast.literal_eval(key): ast.literal_eval(value) for key, value in zip(profiles.keys, profiles.values)
                if ast.literal_eval(key) != "full"}
tool_function = next(node for node in tool_tree.body if isinstance(node, ast.FunctionDef) and node.name == "capability_tools")
client_source = (ROOT / "deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
usage_function = next(node for node in ast.parse(client_source).body if isinstance(node, ast.FunctionDef) and node.name == "usage_int")
namespace: dict[str, Any] = {"json": json, "re": re, "Any": Any, "CAPABILITY_PROFILES": capabilities,
                             "AGENT_MODELS": {}, "DEFAULT_MODEL": "deepseek-v4-pro"}
exec(compile(ast.fix_missing_locations(ast.Module(body=[*nodes, tool_function, usage_function], type_ignores=[])), str(REFERENCE), "exec"), namespace)
rows = []
def add(op: str, expected: Any, **inputs: Any) -> None:
    rows.append({"op": op, **inputs, "expected": expected})

for raw in ["", "plain text", "[]", '{"agents":[]}', '```json\n{"agents":[{"id":"coder"}]}\n```',
            'prefix {"agents": [{"id":"critic"}]} suffix', '{} {}', '{"agents": [}', '{"x":"}"}']:
    add("plan", namespace["extract_json_object"](raw), content=raw)
texts = ["", "  普通摘要  ", "## 摘要\n短结论\n## 关键事实\n事实\n## 风险/不确定\n风险\n## 完整分析\n详细分析",
         "无头部前言\n# SUMMARY\nS\n###### facts\nE\n**RISKS**：\nR\n完整分析：\nD", "## 摘要\nA\n## 摘要\nB",
         "**我的结论如下**\n正文", "摘要：\nA\r\n关键事实:\r\nB\r风险：\rC", "摘要：\n## 无关标题\n保留正文",
         "## 摘要\n## 关键事实", "前文\u2028摘要：\u2028后文\u0085风险：\u0085风险\u2029完整分析：\u2029详细",
         "**summary**: \nA\n**facts**:\nB", "\u001c## 摘要\u001f\n\u001cbody\u001f"]
for raw in texts:
    add("structured", namespace["parse_structured_agent_output"](raw), content=raw)
prior_sets = [[], [{"id":"coder","task":"T","summary":"S","evidence":"E","risks":"R","full_output":"private detail"}],
              [{"name":"","id":"researcher","content":"","full_output":"  full fallback  "}],
              [{"task":"T","content":"  plain content  "}], [{"id":"critic","full_output":" "}],
              [{"id":"reasoner","failed":True,"summary":"No usable result","risks":"network unavailable"}]]
for prior in prior_sets:
    add("prior", namespace["build_prior_context"](prior), outputs=prior)
payloads = [{"apiKey":"offline-fixture-key","model":"deepseek-v4-flash","messages":[{"role":"user","content":"Q"}],
             "searchEnabled":True,"systemPrompt":"  original system  ","thinkingEnabled":False,"temperature":0.5,
             "traceId":"trace-fixture","agentMode":True,"toolsEnabled":True,"cascadeEnabled":True},
            {"messages":[None, "skip", {"role":"user","content":"Q"}],"searchEnabled":1,"systemPrompt":"\u001c原指令\u001f",
             "contextSummary":"summary","contextSummaryGeneration":3,"contextSummaryMessageCount":12,
             "contextCompressionDeltaCount":2,"memoryEnabled":False,"memoryScope":"conversation"}]
for payload in payloads:
    add("base", namespace["agent_base_payload"](payload), payload=payload)
    for role in ["researcher", "coder", "reasoner", "critic"]:
        for model in ["deepseek-v4-pro", "deepseek-v4-flash"]:
            namespace["AGENT_MODELS"] = {role:model}
            for prior in [prior_sets[0], prior_sets[2]]:
                expected = namespace["_agent_payload_for"](payload, agent_id=role, task="测试子任务", prior_outputs=prior)
                add("worker", expected, payload=payload, role=role, model=model, task="测试子任务", outputs=prior)
    for prior in prior_sets:
        expected = namespace["agent_base_payload"](payload)
        expected.update(model="deepseek-v4-flash", toolsEnabled=False, searchEnabled=False,
                        thinkingEnabled=payload.get("thinkingEnabled"), systemPrompt=namespace["SYNTHESIZER_SYSTEM"],
                        messages=namespace["synthesis_messages"](payload, "用户问题", prior))
        add("synthesis", expected, payload=payload, model="deepseek-v4-flash", query="用户问题", outputs=prior)
search = {"results":[None, {"url":" https://example.org/one ","title":" 来源一 "},
                     {"url":"https://example.org/one","title":"Duplicate"}, {"url":"https://example.org/two"},
                     *[{"url":f"https://example.org/{n}","title":f"Source {n}"} for n in range(6)]]}
for role in ["researcher", "coder"]:
    for raw in [texts[0], texts[2], "无结构文本"]:
        add("output", namespace["_build_agent_result"](role, "T", raw, search, {"total_tokens":4}),
            role=role, task="T", content=raw, search=search, usage={"total_tokens":4})
for output in [None, {}, {"summary":"修订建议：CODER"}, {"risks":"说明\u2028修订建议：无\u2028coder"},
               {"summary":"修订建议：critic coder","failed":True}, {"full_output":"修订建议：无\n修订建议：coder"},
               {"content":"修订建议：researcher reasoner"}, {"evidence":"修订建议：reasoner"}]:
    add("critic", namespace["parse_critic_verdict"](output), output=output)
for role in ["researcher", "coder", "reasoner", "critic"]:
    add("failed", namespace["failed_agent_output"](role, "T", RuntimeError("upstream unavailable")), role=role, task="T", error="upstream unavailable")
for usage in [None, {}, {"prompt_cache_hit_tokens":9,"prompt_cache_miss_tokens":3},
              {"prompt_cache_hit_tokens":"bad","promptCacheHitTokens":"3","prompt_cache_miss_tokens":7},
              {"prompt_cache_hit_tokens":-3,"prompt_cache_miss_tokens":4},
              {"promptCacheHitTokens":"27","promptCacheMissTokens":"73"}]:
    add("cache", namespace["cache_usage_summary"](usage), usage=usage)
    outputs = [{"id":"coder","usage":usage}, {"id":"critic","usage":{"prompt_cache_hit_tokens":5,"prompt_cache_miss_tokens":2}}]
    add("agent_cache", namespace["agent_cache_for_diagnostics"](outputs, usage), outputs=outputs, usage=usage)
fixture = {"referenceSha256":hashlib.sha256(REFERENCE.read_bytes()).hexdigest(),
           "toolPolicySha256":hashlib.sha256(TOOLS.read_bytes()).hexdigest(),
           "clientSha256":hashlib.sha256(client_source.encode()).hexdigest(), "cases":rows}
OUTPUT.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"cases":len(rows), "fixtureSha256":hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}))
