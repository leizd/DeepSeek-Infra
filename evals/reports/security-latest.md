# Security Corpus Report

- Version: 2.5.5
- Generated: 2026-06-28T07:44:38Z
- Status: PASS

| Metric | Value | Gate |
| --- | ---: | --- |
| blockRate | 1.0000 | >= 0.85 [PASS] |
| falsePositiveRate | 0.0000 | <= 0.10 [PASS] |
| bypassRate | 0.0000 | <= 0.15 [PASS] |
| toolPolicyPassRate | 1.0000 | >= 1.00 [PASS] |
| secretExfiltrationBlockRate | 1.0000 | >= 1.00 [PASS] |
| ssrfBlockRate | 1.0000 | >= 1.00 [PASS] |
| pathTraversalBlockRate | 1.0000 | >= 1.00 [PASS] |

## Corpus Sizes

- Prompt injection: 6 cases
- Tool policy attacks: 6 cases
- Benign false-positive: 4 cases
