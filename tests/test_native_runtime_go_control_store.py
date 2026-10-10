from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.native_runtime_contract import load_ownership


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "release" / "native_runtime_go_control_store_v1.json"


def test_go_control_tables_cover_go_owned_stores() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    ownership = load_ownership()
    assert catalog["runtime"] == "go"
    assert catalog["mode"] == "shadow"
    assert catalog["unique_writer"]["table"] == "control_writer"
    assert catalog["database"] == "sqlite"
    assert catalog["filename"] == "go-control/control.sqlite3"
    assert catalog["sqlite"]["journal_mode"] == "WAL"
    assert catalog["sqlite"]["synchronous"] == "FULL"
    assert catalog["sqlite"]["transaction_lock"] == "IMMEDIATE"
    assert catalog["sqlite"]["maximum_payload_bytes"] == 1 << 20
    assert catalog["sqlite"]["event_journal"] == "control_events"
    assert catalog["sqlite"]["event_mutation"] == "insert-only"
    assert catalog["sqlite"]["secret_material"].startswith("reject")
    assert catalog["sqlite"]["unknown_user_objects"] == "reject-before-write-connection"
    assert catalog["sqlite"]["identity_open"] == "immutable-read-only"
    assert catalog["cutover"]["table"] == "control_cutover"
    assert catalog["cutover"]["events"] == "control_cutover_events"
    assert catalog["cutover"]["default_state"] == "shadow"
    assert catalog["cutover"]["first_candidate_domain"] == "policy"
    assert catalog["cutover"]["go_authoritative"] == "authorized-by-control-authority-claim"
    assert catalog["cutover"]["de_promotion"].startswith("requires no authority")
    assert catalog["cutover"]["epoch_advance"] == "only inside the authorizing transaction"
    # A promotion is gated on every one of these; dropping one would make an
    # unauthorized ownership change reachable.
    preconditions = " ".join(catalog["cutover"]["promotion_preconditions"])
    assert "capability" in preconditions
    assert "control-authority-v1 checkpoint is installed" in preconditions
    assert "live tip (generation and digest)" in preconditions
    assert "fencing token expectations match" in preconditions
    authority = catalog["authority"]
    assert authority["schema"] == "control-authority-v1"
    assert authority["head"]["table"] == "control_authority_head"
    assert authority["checkpoints"]["table"] == "control_authority_checkpoints"
    assert authority["checkpoints"]["mutation"] == "insert-only"
    assert authority["cutover_authorizations"]["table"] == "control_cutover_authorizations"
    assert authority["cutover_authorizations"]["mutation"] == "insert-only"
    assert authority["deployment_gate"].startswith("store.OpenOptions.AuthorizeCutover")
    assert authority["open_gaps"], "unqualified authority must declare its open gaps"
    # Production authority is durable and never self-asserted, and the frozen
    # pre-cutover intent must not be reinterpreted as production authorization.
    production = catalog["production_authority"]
    assert production["source"].startswith("the durable control_cutover record")
    assert production["governed_domain"] == "action"
    assert production["self_assertion"].startswith("rejected")
    assert len(production["gated_entry_points"]) == 4
    assert production["non_authoritative_path"].startswith("unchanged")
    assert production["refusal"].startswith("CUTOVER_NOT_AUTHORIZED")
    operations = catalog["operations"]
    # The v1 channel still cannot authorize a production mutation, and the v2
    # revision is what does. Both halves must be stated or the catalog would
    # overstate one of them.
    assert "shadow-compare" in operations["production_apply_reason"]
    assert "cannot authorize a production mutation" in operations["production_apply_reason"]
    assert operations["production_apply"] == "implemented-through-v2-apply-mutation"
    assert operations["production_apply_scope"].startswith("reachable over the authenticated internal plane")
    assert "/internal/mutation/apply" in operations["production_apply_scope"]
    assert "Python, Go and Rust" in operations["production_apply_scope"]
    assert "control-mutation-request-v2" in operations["production_apply_contract_revision"]
    assert "one transaction" in operations["production_apply_atomicity"]
    assert len(operations["production_apply_refusals"]) == 6
    assert "different control domain" in operations["production_apply_refusals"][-1]
    assert operations["result_status"].startswith("PROPOSED for a v1 proposal")
    # The Go sources the catalog describes must actually contain the gate.
    reconciler = (ROOT / "go/internal/action/reconciler.go").read_text(encoding="utf-8")
    assert "assertProductionAuthority" in reconciler
    assert "IsGoAuthoritative(authorityDomain)" in reconciler
    assert 'authorityDomain = "action"' in reconciler
    assert "func (store *Control) IsGoAuthoritative(domain string) (bool, error)" in (
        ROOT / "go/internal/store/cutover.go"
    ).read_text(encoding="utf-8")
    apply_source = (ROOT / "go/internal/store/operation.go").read_text(encoding="utf-8")
    assert "func (store *Control) ApplyMutation(" in apply_source
    assert "MutationApplied" in apply_source
    assert "ErrMutationRequestDomainFenced" in apply_source
    status_schema = (ROOT / "go/internal/store/operation_status_schema.go").read_text(encoding="utf-8")
    assert "CHECK(result_status IN ('PROPOSED', 'APPLIED'))" in status_schema
    assert "verifyControlOperationStatusSchemaTx" in status_schema
    assert catalog["sqlite"]["operation_journal"] == "control_operations"
    assert catalog["sqlite"]["operation_mutation"] == "insert-only"
    assert catalog["operations"]["table"] == "control_operations"
    assert catalog["operations"]["mutation"] == "insert-only"
    assert catalog["operations"]["result_status"].startswith("PROPOSED")
    assert catalog["operations"]["production_apply"].startswith("implemented-through")
    assert catalog["operations"]["first_candidate_domain"] == "policy"
    assert catalog["migrations"][-1]["version"] == 15
    assert [item["version"] for item in catalog["migrations"]] == list(range(1, 16))
    # The operator channel is a second journal, not a widened status on the signed one.
    # Both halves of that claim are pinned: the signed CHECK must stay narrow, and the
    # operator table must exist in Go with its authorization stated.
    operator = catalog["operator_mutation"]
    assert operator["table"] == "control_operator_mutations"
    assert operator["mutation"] == "insert-only"
    assert "CHECK-constrained" in operator["why_a_separate_table"]
    assert "AuthorizeCutover" in operator["authorization"]
    assert "go_authoritative" in operator["authorization"]
    assert "inside the write transaction" in operator["epoch_source"]
    assert "one transaction" in operator["atomicity"]
    assert "ALREADY_APPLIED" in operator["already_applied"]
    assert "MUTATION_REQUEST_REPLAY_CONFLICT" in operator["already_applied"]
    assert len(operator["refusals"]) == 6
    assert "fenced domain" in operator["refusals"][2]
    assert "shared record rule" in operator["refusals"][4]
    assert "would make the domain unwritable" in operator["transport_rules_not_applied"]
    assert operator["open_gap"].startswith("no public route")
    operator_source = (ROOT / operator["declared_in"]).read_text(encoding="utf-8")
    assert "func (store *Control) ApplyOperatorMutation(" in operator_source
    # The operator channel applies the record engine's shared secret rule and *not* the
    # signed channel's transport rules, and the reason has to stay in the source: the
    # mutation primitive set refuses floats and `rejectMutationBodySecretKeys` flags the
    # bare fragment `credential`, both of which every stored policy carries.
    assert "rejectControlSecretMaterial" in operator_source
    assert "rejectMutationBodySecretKeys" in operator_source
    assert "validateMutationRecordPayload" in operator_source
    assert "credentialRef" in operator_source
    assert "ErrMutationRequestDomainFenced" in operator_source
    operator_schema = (ROOT / "go/internal/store/operator_mutation_schema.go").read_text(encoding="utf-8")
    assert operator["table"] in operator_schema
    assert "migrateToV15Tx" in operator_schema
    assert "verifyOperatorMutationSchemaTx" in operator_schema
    assert "OPERATOR_MUTATION_IMMUTABLE" in operator_schema
    assert "SchemaV15" in (ROOT / "go/internal/store/schema.go").read_text(encoding="utf-8")
    assert "migrateToV15Tx" in (ROOT / "go/internal/store/control.go").read_text(encoding="utf-8")
    # The public write routes are declared with the same care as the channel they use:
    # the delete refusal names its reason, and the create-revision narrowing is recorded
    # rather than left as an undocumented divergence from the oracle.
    routes = catalog["policy_write_routes"]
    assert routes["create"].startswith("POST /api/workspace/backup-policies")
    assert routes["update"].startswith("PATCH /api/workspace/backup-policies/")
    assert routes["delete"].startswith("DELETE /api/workspace/backup-policies/")
    assert "GO_POLICY_DELETE_UNSUPPORTED" in routes["delete"]
    assert "orphaned control events" in routes["delete_reason"]
    assert "lazily" in routes["target_bindings"]
    assert "must start at policyRevision 1" in routes["create_revision_narrowing"]
    routes_source = (ROOT / routes["declared_in"]).read_text(encoding="utf-8")
    assert "func backupPoliciesCreate(" in routes_source
    assert "func backupPoliciesUpdate(" in routes_source
    assert "func backupPoliciesDelete(" in routes_source
    assert "ApplyOperatorMutation(" in routes_source
    assert "policy.NormalizePolicy(" in routes_source
    for field in ("name", "enabled", "schedule", "protection", "replication", "incremental"):
        assert f'"{field}"' in routes_source, f"patch field missing: {field}"
    # The policy state table is the store's, and its shape is what makes a disabled
    # create and an in-place rewrite legal.
    schema_source = (ROOT / "go/internal/store/schema.go").read_text(encoding="utf-8")
    assert '"":         {"ACTIVE", "DISABLED"}' in schema_source
    assert '"ACTIVE":   {"ACTIVE", "DISABLED", TombstoneState}' in schema_source
    assert 'TombstoneState = "DELETED"' in schema_source
    promotion = catalog["promotion_artifact"]
    assert promotion["schema"] == "control-domain-promotion-v1"
    assert promotion["table"] == "control_promotion_artifacts"
    assert "deployment-pinned" in promotion["signer"]
    assert "in one transaction" in promotion["mutation"]
    assert "cannot be silently upgraded" in promotion["history"]
    inventory = catalog["inventory_import"]
    assert inventory["table"] == "control_inventory_imports"
    assert "one transaction" in inventory["mutation"]
    assert "unverified direct imports cannot promote" in inventory["source_attestation"]
    assert "first signed promotion rereads both source and projection" in inventory["first_promotion_reattestation"]
    assert "signed artifact" in inventory["signed_promotion"]
    health = inventory["target_health"]
    assert health["export_schema"] == "python-control-inventory-export-v2"
    assert health["binding_schema"] == "python-backup-target-health-v1"
    assert health["tables"] == ["backup_target_health", "control_target_health_imports"]
    assert "same Go transaction" in health["mutation"]
    assert "TARGET_HEALTH_NOT_TRANSFERRED" in health["public_read"]
    health_source = (ROOT / "go/internal/store/target_health_schema.go").read_text(encoding="utf-8")
    for table in health["tables"]:
        assert f'"{table}"' in health_source
    # The fence must freeze the state the transfer binds, not only the exported
    # rows, and both runtimes must derive the exact same objects from it.
    linked = inventory["linked_fence"]
    assert len(linked["tables"]) == 6
    assert linked["objects"].startswith("18 triggers")
    assert "PythonWriterMechanicallyDeniedError" in linked["python_denial"]
    assert "byte for byte" in linked["verification"]
    assert "last fence is lifted" in linked["lift"]
    source = (ROOT / "go/internal/store/inventory_source.go").read_text(encoding="utf-8")
    assert "linkedFenceTriggerSQL" in source
    assert "linkedFenceObjects()" in source
    assert "native_control_fence_" in source
    for table in linked["tables"]:
        assert f'{{table: "{table}"' in source, f"linked fence table missing from Go: {table}"
    handoff = (ROOT / "scripts/native_control_handoff.py").read_text(encoding="utf-8")
    assert "linked_fence_objects" in handoff
    for table in linked["tables"]:
        assert f'"{table}":' in handoff, f"linked fence table missing from Python: {table}"
    # The reverse transfer must be declared, gated and reachable in Go, and its
    # Python verifier must consume exactly the Go document bytes.
    handback = catalog["inventory_handback"]
    assert handback["schema"] == "control-inventory-handback-v1"
    assert handback["table"] == "control_inventory_handbacks"
    assert "same transaction" in handback["mutation"]
    assert handback["python_receipt"].startswith("python-control-inventory-handback-receipt-v1")
    assert len(handback["preconditions"]) == 5
    handback_source = (ROOT / "go/internal/store/inventory_handback.go").read_text(encoding="utf-8")
    assert handback["schema"] in handback_source
    assert "control_inventory_handbacks" in (
        ROOT / "go/internal/store/inventory_handback_schema.go"
    ).read_text(encoding="utf-8")
    handoff_source = (ROOT / "scripts/native_control_handoff.py").read_text(encoding="utf-8")
    assert handback["schema"] in handoff_source
    assert "python-control-inventory-handback-receipt-v1" in handoff_source
    assert "--rollback" in handoff_source
    covered: set[str] = set()
    for table in catalog["tables"]:
        covered.update(table["ownership_ids"])
    covered.update(catalog["writer_plan"].keys())
    # The Go control plane owns more than the control store: a service-local
    # database counts as a declared store too, as long as the catalog names it.
    for store in catalog["companion_stores"]:
        covered.update(store["ownership_ids"])
    go_control = {
        item["id"]
        for item in ownership["domains"]
        if item.get("durable_store") == "go_control"
    }
    assert go_control <= covered


def test_companion_go_stores_are_declared_in_their_go_source() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    for store in catalog["companion_stores"]:
        source = (ROOT / store["declared_in"]).read_text(encoding="utf-8")
        for key in ("filename", "table", "schema", "writer_lock"):
            assert store[key] in source, f"{store['id']}: {key} not in {store['declared_in']}"
        assert store["journal_mode"] in source
        assert store["synchronous"] in source


def test_go_schema_matches_catalog_and_rejects_python_paths() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    schema = (ROOT / "go/internal/store/schema.go").read_text(encoding="utf-8")
    control = (ROOT / "go/internal/store/control.go").read_text(encoding="utf-8")
    for table in catalog["tables"]:
        assert f'"{table["name"]}"' in schema
        assert f'"{table["domain"]}"' in schema
    assert "ErrPythonStorePath" in schema
    assert "ErrWriterFenceHeld" in schema
    assert "DenyMutation" in control
    assert "C.CString" not in control
    assert '"database/sql"' in control
    assert '"modernc.org/sqlite"' in control
    assert "writeJSONAtomic" not in control
    assert "ControlDatabaseFilename" in control
    assert "control_events" in control
    assert "control_cutover" in control
    assert "control_operations" in control
    assert "SchemaV3" in schema
    assert "CUTOVER_NOT_AUTHORIZED" in schema
    assert catalog["cutover"]["table"] in control
    for part in catalog["forbidden_python_path_components"]:
        assert part in schema
    # The authority objects the catalog declares must exist in Go, and the
    # cutover capability must stay a deployment opt-in that is mechanically tied
    # to an authenticated control plane. This asserts the coupling, not the
    # absence of the capability: Go tests prove the default stays off and that
    # config refuses the combination.
    authority_source = (ROOT / "go/internal/store/authority_state_schema.go").read_text(encoding="utf-8")
    assert '"control_authority_head"' in authority_source
    assert '"control_authority_checkpoints"' in authority_source
    assert '"control_cutover_authorizations"' in authority_source
    lifecycle = (ROOT / "go/internal/lifecycle/lifecycle.go").read_text(encoding="utf-8")
    assert re.search(r"AuthorizeCutover:\s+cfg\.ControlAuthority", lifecycle)
    assert "cfg.InternalAPIBearer" in lifecycle
    config_source = (ROOT / "go/internal/config/config.go").read_text(encoding="utf-8")
    assert "DEEPSEEKD_INTERNAL_BEARER" in config_source
    assert "DEEPSEEKD_CONTROL_AUTHORITY" in config_source
    assert "DEEPSEEKD_PROMOTION_SIGNER_KEY" in config_source
    assert re.search(r"PromotionSignerPublicKey:\s+cfg\.PromotionSignerPublicKey", lifecycle)
    promotion_source = (ROOT / "go/internal/store/promotion.go").read_text(encoding="utf-8")
    assert "ed25519.Verify" in promotion_source
    assert "artifact.Domain != req.Domain" in promotion_source
    assert "artifact.ExecutionEpoch != current.Epoch" in promotion_source
    assert "artifact.AuthorityDigest != req.Authority.Digest" in promotion_source
    cutover_source = (ROOT / "go/internal/store/cutover.go").read_text(encoding="utf-8")
    assert "verifyPromotionArtifact(" in cutover_source
    assert "INSERT INTO control_promotion_artifacts" in cutover_source
    assert 'cfg.ControlAuthority && cfg.InternalAPIBearer == ""' in config_source, (
        "control authority must be refused without an authenticated control plane"
    )
    auth_source = (ROOT / "go/internal/api/auth.go").read_text(encoding="utf-8")
    assert "RequireInternalBearer" in auth_source
    assert "subtle.ConstantTimeCompare" in auth_source
    assert "IsLoopback" in auth_source
    shadow_routes = (ROOT / "go/internal/api/shadow.go").read_text(encoding="utf-8")
    for route in catalog["internal_api"]["routes"]:
        assert route in shadow_routes, f"internal route not mounted: {route}"
    assert "RequireInternalBearer(internal, options.Bearer)" in shadow_routes
    assert "InternalOptions{Bearer: bearer}" in shadow_routes
    claim_contract = catalog["internal_api"]["authority_claim"]
    assert "/internal/authority/claim" in catalog["internal_api"]["routes"]
    assert "/internal/authority/head" in catalog["internal_api"]["routes"]
    assert "advanced=false" in claim_contract["success"]
    assert "maxAuthorityCheckpointBytes" in shadow_routes
    assert "control.ClaimControlAuthority(checkpoint)" in shadow_routes
    assert "control.ControlAuthorityHead()" in shadow_routes
    # The apply transport must never take its signer from the request, and must
    # refuse when the deployment configured none.
    apply_contract = catalog["internal_api"]["mutation_apply"]
    assert "never from the request" in apply_contract["authority"]
    assert apply_contract["unconfigured_signer"] == "503 MUTATION_SIGNER_NOT_CONFIGURED"
    assert "MUTATION_SIGNER_NOT_CONFIGURED" in shadow_routes
    assert "store.MaxMutationRequestBytes" in shadow_routes
    config_source_for_signer = (ROOT / "go/internal/config/config.go").read_text(encoding="utf-8")
    assert "DEEPSEEKD_MUTATION_SIGNER_KEY" in config_source_for_signer
    assert "MutationSignerPublicKey" in config_source_for_signer


def test_python_shadow_export_report(tmp_path: Path) -> None:
    from scripts.control_plane_shadow import export_report

    out = tmp_path / "shadow-report.json"
    payload = export_report(out)
    assert payload["kernel"] == "control-shadow-decision-v1"
    assert payload["mutationDenied"] is True
    assert payload["cases"]
    assert all(item["digest"] for item in payload["cases"])
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["cases"] == payload["cases"]
