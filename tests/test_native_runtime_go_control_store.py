from __future__ import annotations

import json
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
    assert catalog["migrations"][-1]["version"] == 9
    assert [item["version"] for item in catalog["migrations"]] == list(range(1, 10))
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
    assert "AuthorizeCutover: cfg.ControlAuthority" in lifecycle
    assert "cfg.InternalAPIBearer" in lifecycle
    config_source = (ROOT / "go/internal/config/config.go").read_text(encoding="utf-8")
    assert "DEEPSEEKD_INTERNAL_BEARER" in config_source
    assert "DEEPSEEKD_CONTROL_AUTHORITY" in config_source
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
