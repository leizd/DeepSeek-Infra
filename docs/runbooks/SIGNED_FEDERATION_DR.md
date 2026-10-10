# Signed Federation 与跨 Fleet DR 运维手册

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

适用版本：v4.8.0 候选实现。

本手册覆盖 Fleet federation root、在线 signer、Peer Trust、Receiver ingress、
transfer reconcile 与 production federated DR drill。4.8.0 的本地实现和真实
MinIO 验证已完成；正式 release PASS 仍要求最终 PR head/merge SHA 的 CI producer、
三个 exact proof artifacts 与全局 Evidence Assembly 全部成功。

## 不可破坏的边界

- 每个 Fleet 拥有独立 ROOT、Control Authority、federation root、在线 signer、
  SQLite journals、HTTP 进程和 storage principal。Federation 不共享 Authority log，
  不运行 Raft，也不形成 multi-primary。
- Federation Ed25519 root 与在线 signer 只签 federation control/evidence documents。
  它们不能复用 Age identity 或 Control Authority 私有状态。
- 离线 root bundle 不得复制到 Federation 节点。节点只读取 public Fleet identity
  与加密 online signer bundle。
- Receiver 的长期 S3/MinIO credentials 只存在于 Receiver 进程。Sender 只获得
  Receiver 签发的短期、单用途、scope-bound ingress grant。
- Federation 只传输既有 randomized-Age ciphertext 与 `object-set-v1`。Receiver
  仍通过 production storage path 生成 Receipt v4 和 Commit v4。
- Federated durability 与 local durability 独立。Remote copy 不得减少
  `minCommittedCopies` 或 `minFailureDomains`，也不得触发 primary promotion、
  policy mutation、replica pruning 或 delete。
- Age private identity 永不出现在 federation document、HTTP payload、配置文件或
  proof 中。`RECOVERY_CAPABLE` 节点只从独立安全渠道预配置 recovery identity。

## 1. 身份与 signer 预配置

在隔离的 operator 环境调用
`deepseek_infra.infra.workspace.federation_identity`：

1. 使用 `create_fleet_root(...)` 创建不可覆盖的加密离线 root bundle。
2. 使用 `export_public_fleet_identity(...)` 导出可交换的 `fleet-identity-v1`。
3. 使用 `issue_online_signer(...)` 生成短期 online signer bundle。证书必须有单调
   `sequence`、明确 `notBefore` / `expiresAt` 和最小所需 purposes。
4. 将离线 root bundle 移回离线保管；只把 public identity 和加密 signer bundle
   部署到节点。

根和 signer passphrase 至少 16 bytes。不要把 passphrase 写入命令行、仓库、节点
JSON、日志或 Evidence。节点启动时从
`DEEPSEEK_FEDERATION_SIGNER_PASSPHRASE` 读取 signer passphrase，加载后立即从环境
删除并 zeroize 内存缓冲区。

Root fingerprint 必须通过 federation 之外的 operator channel 交换并人工核对。
首次网络连接、readiness payload 或远端自报值都不能成为信任来源。

## 2. 建立 Peer Trust

每侧 operator 都使用本地 `PeerTrustRegistry` 执行完整状态机：

```text
PENDING -> VERIFIED -> ACTIVE -> SUSPENDED -> REVOKED
```

1. `pin_peer(...)` 必须同时提供 exact `expected_root_fingerprint` 和 operator-known
   `provider / region / jurisdiction / siteClass` metadata。
2. `verify_peer(...)` 比较完整 public identity；不匹配即失败。
3. `activate_peer(...)` 只能从 `VERIFIED` 进入 `ACTIVE`。
4. `accept_online_signer(...)` 只接受 pinned root 签发、当前有效且 sequence 单调的
   signer certificate。

`expected_root_fingerprint=None` 会返回 `FEDERATION_PEER_ROOT_PIN_REQUIRED`；同一
Fleet ID 对应不同 root 会返回 `FEDERATION_FLEET_IDENTITY_COLLISION`。不要添加 TOFU
兜底。Peer 自报的 failure domain 必须与 registry 中完整 pinned metadata 相等。

## 3. Custody 模式

- `COLD_CUSTODY`：可保存 ciphertext、验证 Receipt/Commit 并返回 signed custody
  proof；不能声称可恢复、RTO 或 plaintext access。
- `RECOVERY_CAPABLE`：除 custody 外可运行 production restore drill。配置时必须
  同时绑定本地 credential provider/reference 和对应 Age recipient；实际 Age private
  identity 仍由本地安全 provider 或启动 stdin 提供。

不要通过修改一个已有 capability 绕过 revision fence。模式或 recovery binding
变化必须使用当前 `expected_revision`；冲突应重新读取后由 operator 决策。

## 4. 节点配置与启动

节点配置 schema 是 `federation-node-config-v1`。每个 Fleet 必须使用独立路径：

- `publicIdentityPath`、`signerBundlePath`；
- `peerRegistryPath`、`transferJournalPath`、`receiverDbPath`、
  `durabilityDbPath`、`custodyDbPath`、`nodeStateDbPath`；
- `stagingDir`、`remoteTargetId`、operator-pinned `failureDomainMetadata`；
- readiness、`maxIngressBytes`、`ownerInstanceId` 和 custody mode。

生产进程由 service manager 注入两个环境变量：

- `DEEPSEEK_FEDERATION_OPERATOR_TOKEN`：至少 16 characters；
- `DEEPSEEK_FEDERATION_SIGNER_PASSPHRASE`：在线 signer bundle passphrase。

启动示例：

```powershell
python -m deepseek_infra.federation_app `
  --config D:\fleet-b\federation-node.json `
  --host 127.0.0.1 `
  --port 8448
```

非 loopback bind 必须显式添加 `--allow-non-loopback`。生产网络同时必须配置
`--ssl-certfile` 与 `--ssl-keyfile`，并由网络 ACL 只允许 pinned peers。Operator
routes 还强制请求来自 loopback，并要求 `X-Federation-Operator-Token`；不要将该
token 发送给 peer。

Recovery-capable 节点额外使用 `--recovery-identity-stdin`，从 stdin 读取一次 Age
identity。不要通过环境变量、文件路径参数或 HTTP 发送 Age private identity。

每个进程只注入自己的 storage principal。启动后先用 `/federation/v1/health`
核对 `fleetId`、root/state identity 与新 PID，再允许流量。

### HTTP surfaces

Peer surface 只接受签名/受 grant 约束的 federation traffic：

- `POST /federation/v1/peer/readiness`
- `POST /federation/v1/peer/challenges/respond`
- `POST /federation/v1/peer/ingress-grants`
- `POST /federation/v1/peer/transfers/{transferId}/declaration`
- `PUT /federation/v1/peer/transfers/{transferId}/components/{componentDigest}`
- `GET /federation/v1/peer/transfers/{transferId}`
- `POST /federation/v1/peer/transfers/{transferId}/commit`

Operator surface 只供本机 orchestrator 使用，强制 loopback 与 operator token：

- challenge issue/verify 与 readiness verify；
- transfer propose、ingress grant verify 与 remote-verifying transition；
- replica attestation verify；
- DR drill 与 DR attestation verify。

不要把 operator route 暴露为 peer API，也不要把 HTTP 200 当作 semantic success；
调用方必须检查返回文档、状态与 stable error code。

## 5. 正常 transfer 顺序

```text
pin + verify + activate peer
-> accept root-certified online signer
-> challenge / response
-> verify signed full readiness payload
-> derive immutable transferId
-> Receiver signs scoped ingress grant
-> declare object-set-v1
-> stream existing Age ciphertext components
-> Receiver Receipt v4 + Commit v4
-> verify signed replica attestation
-> record FEDERATED_COMMITTED
```

`transferId` 由 source Fleet、destination Fleet、backup ID 与 object-set digest 的
domain-separated canonical tuple 推导。相同 ID 与相同 binding 是 resume；相同 ID
与不同内容必须返回 `FEDERATION_TRANSFER_IDENTITY_CONFLICT`。

Ingress grant 的 source/destination、transfer、policy、backup、object-set digest、
prefix、max bytes、nonce 和有效期必须全部匹配。上传不得逃逸 prefix 或超过累计
byte ceiling。Grant 不能授权另一 transfer，也不能在重试时重置已消费容量。

## 6. 分区、未知结果与 Receiver 崩溃

最危险的状态是 Receiver 已 durable commit、Sender 尚未收到响应。此时禁止 blind
replay：

1. 对原 `transferId` 执行 `GET /federation/v1/peer/transfers/{transferId}`。
2. 返回 `COMMITTED` 时，验证 signed replica attestation、peer trust、signer chain、
   Receipt/Commit/object-set binding 和 pinned failure-domain metadata，然后本地记录。
3. 返回 `RESUME` 时，读取 Receiver durable journal，只发送缺失 component，并复用
   原 transfer/grant/write identity。
4. 返回 not found 或 identity conflict 时停止；不要生成替代 transfer ID 掩盖错误。

Receiver 进程死亡后，使用同一个 Fleet root、public identity、peer registry、transfer
journal、receiver DB、staging directory、target 和 storage principal 启动新 PID。
不要清理 staging 或 journal。健康检查通过后先 reconcile 原 transfer，再恢复写入。
Commit 重试必须返回同一个 effect digest，不能生成第二个 remote commit。

## 7. Online signer 轮换

1. 在离线 root 环境签发更高 certificate `sequence` 的新 online signer，并设置短期
   validity window 与最小 purposes。
2. 两侧先验证 pinned root certificate，并通过 `accept_online_signer(...)` 接受新
   signer；sequence 回退或同 key/different certificate 必须失败。
3. 原子部署新加密 bundle，更新节点配置引用并受控重启节点。
4. 用新 signer 完成 challenge/response 和 signed readiness 验证。
5. 确认新流量只使用新 signer 后，调用 `revoke_online_signer(...)` 撤销旧 signer，
   记录 actor、reason 和精确 effective time。
6. 保留旧 certificate、revocation event 与历史 proof。历史验证使用 signing time；
   current authorization 始终拒绝已撤销 signer。

不要覆盖旧 signer bundle，也不要复用 sequence。Root 轮换不是在线 signer 轮换：
root 疑似泄露时必须执行 Peer revocation 和显式重新建联，不能在同一 Fleet ID 下静默
替换 root。

## 8. Incident handling

| 事件 | 立即动作 | 恢复条件 |
| --- | --- | --- |
| Online signer 疑似泄露 | 立即撤销该 signer 并停止新 grant/transfer，保全 journal 与 proofs；若 root 仍可信则签发更高 sequence signer | 新 signer 被 pinned root 验证、接受并通过 challenge/readiness；若已将 Peer 置为 `SUSPENDED`，则不能原地重新激活，必须走 terminal revocation 与显式重新建联 |
| Federation root 疑似泄露 | `REVOKED` peer，隔离节点和 storage principal，禁止重绑同 Fleet ID | 新 root/必要时新 Fleet ID 通过独立渠道重新 pin；旧 trust 永不自动恢复 |
| Ingress grant 泄露 | 在网络/服务层停止 Receiver ingress，按 grant/transfer 查询已写 bytes并等待 grant expiry；只有准备终止该 trust 时才 suspend peer | 原 transfer journal 对账完成，确认无 prefix/byte escape；已 suspend 的 Peer 不得原地恢复 |
| Attestation tamper | 不记录 `FEDERATED_COMMITTED`，保存原 bytes 和验证错误 | 从原 transfer ID 重新查询 Receiver，获得可验证的 exact attestation |
| Receiver crash/partition | 保留 DB、journal、staging 与 target；新 PID reconcile | 同 transfer ID 返回 COMMITTED 或 RESUME，且 commit effect 唯一 |
| Age recovery identity 疑似泄露 | 隔离 recovery-capable 节点，撤销本地 secret provider binding，不通过 federation 轮换 | 新 recipient 通过独立安全渠道部署并通过新的 recovery capability revision |

Peer `REVOKED` 是 terminal。不要把 incident recovery 实现成自动 reactivation。

## 9. Federated DR drill

只有 `RECOVERY_CAPABLE` Receiver 可运行 drill：

1. 从 durable federated replica、Receipt v4 和 Commit v4 解析冻结 restore inputs。
2. 使用 production restore path 写入 isolated workspace。
3. 校验 transfer ID、backup ID、object-set digest、remote Receipt/Commit、restore ID、
   source revision 和 workspace digest。
4. 记录 started/completed time 与实际 RTO。
5. 无论成功或失败都执行 cleanup；只有 `cleanupCompleted=true` 且 workspace binding
   语义验证通过，signed `federated-dr-drill-attestation-v1` 才可满足 Evidence。

`success=true`、HTTP 200 或 pytest exit code 都不是 DR correctness proof。

## 10. Evidence 验证

最终 CI producer 必须上传以下 exact `evidence-proof-v2` artifacts：

- `docs/evidence/federation-trust-proof-v4.8.0.json`
- `docs/evidence/federated-replica-proof-v4.8.0.json`
- `docs/evidence/federated-dr-proof-v4.8.0.json`

下载 artifact 后逐个重新验证：

```powershell
python scripts/validate_evidence_proof.py `
  --proof docs/evidence/federation-trust-proof-v4.8.0.json `
  --scenario real-two-fleet-four-minio-signed-federation-trust

python scripts/validate_evidence_proof.py `
  --proof docs/evidence/federated-replica-proof-v4.8.0.json `
  --scenario real-two-fleet-four-minio-signed-federation-replica

python scripts/validate_evidence_proof.py `
  --proof docs/evidence/federated-dr-proof-v4.8.0.json `
  --scenario real-two-fleet-four-minio-signed-federation-dr
```

再用 `Get-FileHash -Algorithm SHA256` 和 `(Get-Item ...).Length` 对照 producer report
中的 SHA-256 与 byte size，并确认 Evidence Assembly 重新读取了同一组 bytes。Local
real-MinIO proof 只能作为开发证据，不能替代 exact PR-head/merge artifact。

## 明确不做

Automatic cross-Fleet primary promotion、shared/multi-primary Authority、Raft/global
consensus、cross-Fleet policy mutation/delete、automatic local replica pruning、remote
copy 替代 local durability、Age private identity 传输、TOFU、LLM trust/routing，以及
Receipt v5、Commit v5、`object-set-v2` 或 `control-authority-v2`。
