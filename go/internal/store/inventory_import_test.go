package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func inventoryFixture(t *testing.T, domain string) (*AuthorityCheckpoint, []byte) {
	t.Helper()
	cp := frozenCheckpoint(t, 0)
	cp.Policies = []any{map[string]any{"policyId": "p-1", "policyRevision": 2, "enabled": false}}
	cp.Targets = []any{map[string]any{"targetId": "t-1", "kind": "s3", "topologyGeneration": 1}}
	cp.PromotionEpochs = map[string]int64{"p-1": 0}
	cp.DrainGenerations = map[string]int64{"p-1": 0}
	cp.PlacementGenerations = map[string]int64{"p-1": 0}
	cp.ReceiptMutationGenerations = map[string]int64{}
	resealCheckpoint(t, cp)
	var rows []any
	if domain == "policy" {
		rows = []any{map[string]any{
			"policy_id": "p-1", "revision": 2, "payload_json": `{"enabled":false,"policyId":"p-1","policyRevision":2}`,
			"topology_generation": 0, "promotion_epoch": 0, "drain_generation": 0,
			"placement_generation": 0, "updated_at": "2026-09-29T00:00:00Z",
		}}
	} else {
		rows = []any{map[string]any{
			"target_id": "t-1", "generation": 1,
			"payload_json": `{"kind":"s3","targetId":"t-1","topologyGeneration":1}`,
			"updated_at":   "2026-09-29T00:00:00Z",
		}}
	}
	sourceDigest, err := hashCanonicalJSON(rows)
	if err != nil {
		t.Fatal(err)
	}
	manifest := map[string]any{
		"schema": PythonInventoryExportSchema, "domain": domain, "transferId": "isolated-transfer",
		"sourceSchemaVersion": 8, "authorityGeneration": cp.AuthorityGeneration,
		"authorityDigest": cp.Digest, "sourceDigest": sourceDigest, "rows": rows,
		"legacyProjection": map[string]any{"fileCount": 0, "digest": nil},
	}
	manifestDigest, err := hashCanonicalJSON(manifest)
	if err != nil {
		t.Fatal(err)
	}
	manifest["manifestDigest"] = manifestDigest
	raw, err := pythonCanonicalJSON(manifest)
	if err != nil {
		t.Fatal(err)
	}
	return cp, append(raw, '\n')
}

func TestImportPythonInventoryIntoFreshDualEvaluateDomain(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			control := openAuthority(t)
			defer control.Close()
			cp, raw := inventoryFixture(t, domain)
			if _, advanced, err := control.ClaimControlAuthority(cp); err != nil || !advanced {
				t.Fatalf("claim: advanced=%v err=%v", advanced, err)
			}
			dualEvaluate(t, control, domain)
			result, err := control.ImportPythonInventory(raw)
			if err != nil || result.Imported != 1 || result.Domain != domain {
				t.Fatalf("import result %+v: %v", result, err)
			}
			id, revision := "p-1", int64(2)
			if domain == "target" {
				id, revision = "t-1", 1
			}
			record, exists, err := control.Get(domain, id)
			if err != nil || !exists || record.Revision != revision || record.State != "ACTIVE" {
				t.Fatalf("stored record %+v exists=%v err=%v", record, exists, err)
			}
			var payload map[string]any
			if err := json.Unmarshal(record.Payload, &payload); err != nil {
				t.Fatal(err)
			}
			field := "policyRevision"
			if domain == "target" {
				field = "topologyGeneration"
			}
			if payload[field] != float64(revision) {
				t.Fatalf("source revision/generation lost: %+v", payload)
			}
			if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrInventoryImportConflict) {
				t.Fatalf("second import must refuse existing history: %v", err)
			}
		})
	}
}

func TestImportPythonInventoryRefusesUntrustedAndStaleInput(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	cp, raw := inventoryFixture(t, "policy")
	if _, advanced, err := control.ClaimControlAuthority(cp); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatalf("shadow import: %v", err)
	}
	dualEvaluate(t, control, "policy")
	tampered := bytes.Replace(raw, []byte("isolated-transfer"), []byte("isolated-transfex"), 1)
	if _, err := control.ImportPythonInventory(tampered); !errors.Is(err, ErrInventoryImportInvalid) {
		t.Fatalf("tampered digest: %v", err)
	}
	duplicate := bytes.Replace(raw, []byte(`"domain":"policy"`), []byte(`"domain":"policy","domain":"policy"`), 1)
	if _, err := control.ImportPythonInventory(duplicate); !errors.Is(err, ErrInventoryImportInvalid) {
		t.Fatalf("duplicate JSON key: %v", err)
	}
	other := openAuthority(t)
	defer other.Close()
	dualEvaluate(t, other, "policy")
	if _, err := other.ImportPythonInventory(raw); !errors.Is(err, ErrCutoverAuthorityStale) {
		t.Fatalf("no installed checkpoint: %v", err)
	}
	if _, exists, err := control.Get("policy", "p-1"); err != nil || exists {
		t.Fatalf("refused input wrote record: exists=%v err=%v", exists, err)
	}
}

func TestImportPythonInventoryAcceptsRealPythonSourceFixtures(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			checkpointBytes, err := os.ReadFile(filepath.Join("testdata", "python_inventory_checkpoint_v1.json"))
			if err != nil {
				t.Fatal(err)
			}
			var checkpoint AuthorityCheckpoint
			if err := json.Unmarshal(checkpointBytes, &checkpoint); err != nil {
				t.Fatal(err)
			}
			manifest, err := os.ReadFile(filepath.Join("testdata", "python_"+domain+"_inventory_export_v1.json"))
			if err != nil {
				t.Fatal(err)
			}
			control := openAuthority(t)
			defer control.Close()
			if _, advanced, err := control.ClaimControlAuthority(&checkpoint); err != nil || !advanced {
				t.Fatalf("claim Python checkpoint: %v %v", advanced, err)
			}
			dualEvaluate(t, control, domain)
			result, err := control.ImportPythonInventory(manifest)
			if err != nil || result.Imported != 1 {
				t.Fatalf("import Python SQLite export: %+v %v", result, err)
			}
		})
	}
}

func resealInventoryFixture(t *testing.T, raw []byte, mutate func(map[string]any)) []byte {
	t.Helper()
	var document map[string]any
	if err := decodeSingleJSON(raw, &document); err != nil {
		t.Fatal(err)
	}
	mutate(document)
	delete(document, "sourceDigest")
	sourceDigest, err := hashCanonicalJSON(document["rows"])
	if err != nil {
		t.Fatal(err)
	}
	document["sourceDigest"] = sourceDigest
	delete(document, "manifestDigest")
	manifestDigest, err := hashCanonicalJSON(document)
	if err != nil {
		t.Fatal(err)
	}
	document["manifestDigest"] = manifestDigest
	encoded, err := pythonCanonicalJSON(document)
	if err != nil {
		t.Fatal(err)
	}
	return append(encoded, '\n')
}

func TestImportPythonInventoryRejectsRehashedSourceAndMetadataForgery(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, raw := inventoryFixture(t, "policy")
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	for name, mutate := range map[string]func(map[string]any){
		"payload changed after export": func(doc map[string]any) {
			row := doc["rows"].([]any)[0].(map[string]any)
			row["payload_json"] = `{"enabled":true,"policyId":"p-1","policyRevision":2}`
		},
		"source policy generation forged": func(doc map[string]any) {
			row := doc["rows"].([]any)[0].(map[string]any)
			row["promotion_epoch"] = 4
		},
		"source revision forged": func(doc map[string]any) {
			row := doc["rows"].([]any)[0].(map[string]any)
			row["revision"] = 3
		},
		"source identity forged": func(doc map[string]any) {
			row := doc["rows"].([]any)[0].(map[string]any)
			row["policy_id"] = "p-2"
		},
		"extra source row": func(doc map[string]any) {
			row := doc["rows"].([]any)[0].(map[string]any)
			doc["rows"] = []any{row, row}
		},
	} {
		t.Run(name, func(t *testing.T) {
			forged := resealInventoryFixture(t, raw, mutate)
			if _, err := control.ImportPythonInventory(forged); !errors.Is(err, ErrInventoryImportInvalid) {
				t.Fatalf("rehashed forged source accepted: %v", err)
			}
		})
	}
	if _, exists, err := control.Get("policy", "p-1"); err != nil || exists {
		t.Fatalf("forged imports wrote a record: %v %v", exists, err)
	}
}

func TestImportPythonInventoryRejectsUnmigratedTargetReceiptGenerations(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, raw := inventoryFixture(t, "target")
	checkpoint.ReceiptMutationGenerations["t-1"] = 2
	resealCheckpoint(t, checkpoint)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "target")
	raw = resealInventoryFixture(t, raw, func(doc map[string]any) {
		doc["authorityDigest"] = checkpoint.Digest
	})
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrInventoryImportInvalid) {
		t.Fatalf("unmigrated receipt generation accepted: %v", err)
	}
}

func TestImportPythonInventoryRejectsMalformedRowsBeforeWriting(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, raw := inventoryFixture(t, "policy")
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	for name, mutate := range map[string]func(map[string]any){
		"unknown manifest field":    func(doc map[string]any) { doc["unverified"] = true },
		"wrong schema":              func(doc map[string]any) { doc["schema"] = "other" },
		"wrong domain":              func(doc map[string]any) { doc["domain"] = "action" },
		"invalid transfer id":       func(doc map[string]any) { doc["transferId"] = "../other" },
		"unsupported source schema": func(doc map[string]any) { doc["sourceSchemaVersion"] = 9 },
		"projection files without a digest": func(doc map[string]any) {
			// A nil digest means the export bound no directory, so it cannot
			// claim files. The converse is legal: an empty directory binds its
			// own digest and still reports zero files.
			doc["legacyProjection"].(map[string]any)["fileCount"] = 2
		},
		"projection digest is not a digest": func(doc map[string]any) {
			projection := doc["legacyProjection"].(map[string]any)
			projection["digest"], projection["fileCount"] = "not-a-digest", 1
		},
		"projection file count negative": func(doc map[string]any) {
			doc["legacyProjection"].(map[string]any)["fileCount"] = -1
		},
		"missing source row id":  func(doc map[string]any) { delete(doc["rows"].([]any)[0].(map[string]any), "policy_id") },
		"invalid source payload": func(doc map[string]any) { doc["rows"].([]any)[0].(map[string]any)["payload_json"] = "{" },
		"source payload id differs": func(doc map[string]any) {
			doc["rows"].([]any)[0].(map[string]any)["payload_json"] = `{"enabled":false,"policyId":"other","policyRevision":2}`
		},
		"missing source payload":     func(doc map[string]any) { delete(doc["rows"].([]any)[0].(map[string]any), "payload_json") },
		"source revision is text":    func(doc map[string]any) { doc["rows"].([]any)[0].(map[string]any)["revision"] = "2" },
		"source updated at missing":  func(doc map[string]any) { doc["rows"].([]any)[0].(map[string]any)["updated_at"] = "" },
		"policy generation is text":  func(doc map[string]any) { doc["rows"].([]any)[0].(map[string]any)["topology_generation"] = "0" },
		"policy generation negative": func(doc map[string]any) { doc["rows"].([]any)[0].(map[string]any)["topology_generation"] = -1 },
		"oversized source payload": func(doc map[string]any) {
			doc["rows"].([]any)[0].(map[string]any)["payload_json"] = strings.Repeat("x", maximumPayloadBytes+1)
		},
		"source payload too deeply nested": func(doc map[string]any) {
			row := doc["rows"].([]any)[0].(map[string]any)
			row["payload_json"] = `{"policyId":"p-1","policyRevision":2,"deep":` + strings.Repeat("[", 130) + "0" + strings.Repeat("]", 130) + "}"
		},
	} {
		t.Run(name, func(t *testing.T) {
			forged := resealInventoryFixture(t, raw, mutate)
			if _, err := control.ImportPythonInventory(forged); !errors.Is(err, ErrInventoryImportInvalid) {
				t.Fatalf("malformed source row accepted: %v", err)
			}
		})
	}
	if _, err := control.ImportPythonInventory(raw[:len(raw)-1]); !errors.Is(err, ErrInventoryImportInvalid) {
		t.Fatalf("missing newline accepted: %v", err)
	}
	if _, err := control.ImportPythonInventory([]byte("{\n")); !errors.Is(err, ErrInventoryImportInvalid) {
		t.Fatalf("malformed document accepted: %v", err)
	}
	if _, exists, err := control.Get("policy", "p-1"); err != nil || exists {
		t.Fatalf("malformed rows wrote a record: exists=%v err=%v", exists, err)
	}
}

func TestImportPythonInventoryRejectsAlteredSourceDigestWithRehashedManifest(t *testing.T) {
	_, raw := inventoryFixture(t, "policy")
	var document map[string]any
	if err := decodeSingleJSON(raw, &document); err != nil {
		t.Fatal(err)
	}
	document["sourceDigest"] = strings.Repeat("0", 64)
	delete(document, "manifestDigest")
	digest, err := hashCanonicalJSON(document)
	if err != nil {
		t.Fatal(err)
	}
	document["manifestDigest"] = digest
	forged, err := pythonCanonicalJSON(document)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := parsePythonInventoryExport(append(forged, '\n')); !errors.Is(err, ErrInventoryImportInvalid) {
		t.Fatalf("source digest rehash accepted: %v", err)
	}
}

func TestImportPythonInventoryRefusesMissingDeploymentAuthorityAndDamagedStores(t *testing.T) {
	_, raw := inventoryFixture(t, "policy")
	t.Run("unconfigured authority", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		dualEvaluate(t, control, "policy")
		if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrCutoverNotAuthorized) {
			t.Fatalf("unauthorized import: %v", err)
		}
	})
	t.Run("unavailable database", func(t *testing.T) {
		control := openAuthority(t)
		_ = control.db.Close()
		if _, err := control.ImportPythonInventory(raw); err == nil {
			t.Fatal("unavailable database accepted import")
		}
	})
	t.Run("broken schema", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TRIGGER control_events_no_delete"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("broken schema accepted import: %v", err)
		}
	})
	t.Run("corrupt cutover row", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, err := control.db.Exec("UPDATE control_cutover SET transfer_id='..' WHERE domain='policy'"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt cutover accepted import: %v", err)
		}
	})
}

func TestImportPythonInventoryRefusesCheckpointCountAndGenerationMismatch(t *testing.T) {
	for name, change := range map[string]func(*AuthorityCheckpoint){
		"policy count":          func(cp *AuthorityCheckpoint) { cp.Policies = []any{} },
		"policy generation map": func(cp *AuthorityCheckpoint) { cp.PromotionEpochs = map[string]int64{} },
	} {
		t.Run(name, func(t *testing.T) {
			control := openAuthority(t)
			defer control.Close()
			checkpoint, raw := inventoryFixture(t, "policy")
			change(checkpoint)
			resealCheckpoint(t, checkpoint)
			if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
				t.Fatalf("claim: %v %v", advanced, err)
			}
			dualEvaluate(t, control, "policy")
			raw = resealInventoryFixture(t, raw, func(doc map[string]any) { doc["authorityDigest"] = checkpoint.Digest })
			if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrInventoryImportInvalid) {
				t.Fatalf("checkpoint mismatch accepted: %v", err)
			}
		})
	}
}

func TestImportPythonInventoryRefusesNonzeroUntrackedPolicyTopologyGeneration(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, raw := inventoryFixture(t, "policy")
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	raw = resealInventoryFixture(t, raw, func(doc map[string]any) {
		doc["rows"].([]any)[0].(map[string]any)["topology_generation"] = 3
	})
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrInventoryImportInvalid) {
		t.Fatalf("unowned generation accepted: %v", err)
	}
	if _, exists, err := control.Get("policy", "p-1"); err != nil || exists {
		t.Fatalf("refusal wrote policy: %v %v", exists, err)
	}
}

func TestImportPythonInventoryRejectsTamperedStoredCheckpointDocument(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, raw := inventoryFixture(t, "policy")
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	if _, err := control.db.Exec("DROP TRIGGER control_authority_checkpoints_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`UPDATE control_authority_checkpoints SET document='{}' WHERE authority_generation=1`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlAuthoritySchemaObjects["control_authority_checkpoints_no_update"]); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrCutoverAuthorityStale) {
		t.Fatalf("corrupt installed checkpoint accepted: %v", err)
	}
}

func TestImportPythonInventoryRefusesInstalledCheckpointForAnotherSourceSchema(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, raw := inventoryFixture(t, "policy")
	checkpoint.ControlSchemaVersion = 9
	resealCheckpoint(t, checkpoint)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	raw = resealInventoryFixture(t, raw, func(doc map[string]any) { doc["authorityDigest"] = checkpoint.Digest })
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrCutoverAuthorityStale) {
		t.Fatalf("wrong checkpoint schema accepted: %v", err)
	}
}

func TestImportPythonInventoryRefusesStaleWriterAndExpiredCommit(t *testing.T) {
	clock := int64(1000)
	control, err := OpenControl(OpenOptions{
		Path: t.TempDir(), Owner: "import-owner", AuthorizeCutover: true,
		Now: func() int64 { return clock },
	})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	checkpoint, raw := inventoryFixture(t, "policy")
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	control.token++
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("stale writer accepted: %v", err)
	}
	control.token--
	control.closed = true
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("closed writer accepted: %v", err)
	}
	control.closed = false
	control.now = func() int64 {
		clock++
		if clock == 1002 {
			return 1031
		}
		return 1000
	}
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expired commit accepted: %v", err)
	}
	control.now = func() int64 { return 1000 }
	if _, exists, err := control.Get("policy", "p-1"); err != nil || exists {
		t.Fatalf("expired commit wrote a record: exists=%v err=%v", exists, err)
	}
}
