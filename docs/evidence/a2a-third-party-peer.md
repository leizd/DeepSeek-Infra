# A2A 第三方对等节点证据

- 版本: 2.6.3
- 生成时间: 2026-06-30T02:29:38Z
- 状态: 通过
- 对等节点: A2A Interop Peer
- 对等节点类型: `third-party`
- 协议: `0.3.0`
- URL: `http://127.0.0.1:57749`
- 端点: `http://127.0.0.1:57749/a2a/agents/interop-peer`

| 检查项 | 状态 |
| --- | --- |
| agentCard | 通过 |
| messageSend | 通过 |
| messageStream | 通过 |
| tasksGet | 通过 |
| tasksCancel | 通过 |
| tasksList | 通过 |
| artifactChunks | 通过 |
| sseFinalEvent | 通过 |

## 步骤

| 步骤 | 状态 | 详情 |
| --- | --- | --- |
| a2a.agent_card | 通过 | name=A2A Interop Peer protocol=0.3.0 |
| a2a.message_send | 通过 | task=task_2cde3cf0769a8a7375db896f state=working |
| a2a.tasks_get | 通过 | task=task_2cde3cf0769a8a7375db896f |
| a2a.message_stream | 通过 | events=5 final=completed |
| a2a.artifact_chunks | 通过 | chunks=2 indices=[0, 1] |
| a2a.sse_final_event | 通过 | final=completed |
| a2a.tasks_list | 通过 | 列出了 2 个 task |
| a2a.tasks_cancel | 通过 | task=task_117a0753bdba385ea4051fc5 state=canceling |
