# A2A Third-Party Peer Evidence

- Version: 2.5.6
- Generated: 2026-06-28T07:38:13Z
- Status: PASS
- Peer: A2A Third-Party Demo Peer
- Peer Type: `third-party`
- Protocol: `0.3.0`
- URL: `http://127.0.0.1:49920`
- Endpoint: `http://127.0.0.1:49920/a2a/agents/third-party-demo-peer`

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
| a2a.agent_card | pass | name=A2A Third-Party Demo Peer protocol=0.3.0 |
| a2a.message_send | pass | task=task_77b73ceb6a73cb17563c617f state=working |
| a2a.tasks_get | pass | task=task_77b73ceb6a73cb17563c617f |
| a2a.message_stream | pass | events=5 final=completed |
| a2a.artifact_chunks | pass | chunks=2 indices=[0, 1] |
| a2a.sse_final_event | pass | final=completed |
| a2a.tasks_list | pass | 2 tasks listed |
| a2a.tasks_cancel | pass | task=task_fec7e7fae147f5577f2a267f state=canceling |
