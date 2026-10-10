# A2A External Peer Evidence

- Version: 4.0.0
- Generated: 2026-07-15T11:37:55Z
- Status: PASS
- Peer: A2A Interop Peer
- Peer Type: `independent-process`
- Protocol: `0.3.0`
- URL: `http://127.0.0.1:50176`
- Endpoint: `http://127.0.0.1:50176/a2a/agents/interop-peer`

| Check | Status |
| --- | --- |
| agentCard | PASS |
| messageSend | PASS |
| messageStream | PASS |
| tasksGet | PASS |
| tasksCancel | PASS |
| tasksList | PASS |
| artifactChunks | PASS |
| sseFinalEvent | PASS |

## Steps

| Step | Status | Detail |
| --- | --- | --- |
| a2a.agent_card | pass | name=A2A Interop Peer protocol=0.3.0 |
| a2a.message_send | pass | task=task_e74fdde2a7e3206a5ee6f799 state=working |
| a2a.tasks_get | pass | task=task_e74fdde2a7e3206a5ee6f799 |
| a2a.message_stream | pass | events=5 final=completed |
| a2a.artifact_chunks | pass | chunks=2 indices=[0, 1] |
| a2a.sse_final_event | pass | final=completed |
| a2a.tasks_list | pass | 2 tasks listed |
| a2a.tasks_cancel | pass | task=task_455870197c2772e93d3810eb state=canceling |
