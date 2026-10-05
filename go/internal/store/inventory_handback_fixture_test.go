package store

import (
	"os"
	"path/filepath"
	"testing"
)

// handbackFixturePath is the checked-in, Go-produced handback document the
// Python source verifier consumes. Regenerate it with
// DEEPSEEK_UPDATE_HANDBACK_FIXTURE=1 go test ./internal/store -run HandbackFixture
func handbackFixturePath(domain string) string {
	return "testdata/python_" + domain + "_inventory_handback_v1.json"
}

// handbackFixtureTransfer is the transfer ID the checked-in Python source
// fixture was fenced for, per domain.
var handbackFixtureTransfer = map[string]string{"policy": "fixture-policy", "target": "fixture-target"}

func TestHandbackFixtureMatchesTheCrossLanguageContract(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			path, raw := copiedPythonSourceFixture(t, domain)
			control, err := OpenControl(OpenOptions{
				Path: t.TempDir(), Owner: "fixture-handback", Now: func() int64 { return 1000 },
				AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic,
				FleetID: "fleet-a", Environment: "production",
			})
			if err != nil {
				t.Fatal(err)
			}
			defer control.Close()
			checkpoint := realPythonInventoryCheckpoint(t)
			if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
				t.Fatalf("claim: %v %v", advanced, err)
			}
			dualEvaluate(t, control, domain)
			attestation, err := AttestPythonInventorySource(path, raw)
			if err != nil {
				t.Fatal(err)
			}
			result, err := control.ImportAttestedPythonInventory(raw, attestation)
			if err != nil || result.TransferID != handbackFixtureTransfer[domain] {
				t.Fatalf("import: %+v %v", result, err)
			}
			handback, err := control.RollbackPythonInventory(domain, result.TransferID)
			if err != nil {
				t.Fatal(err)
			}
			document, err := CanonicalInventoryHandback(handback)
			if err != nil {
				t.Fatal(err)
			}
			document = append(document, '\n')
			fixture := filepath.FromSlash(handbackFixturePath(domain))
			if os.Getenv("DEEPSEEK_UPDATE_HANDBACK_FIXTURE") == "1" {
				if err := os.WriteFile(fixture, document, 0o600); err != nil {
					t.Fatal(err)
				}
			}
			checkedIn, err := os.ReadFile(fixture)
			if err != nil {
				t.Fatalf("checked-in handback fixture: %v", err)
			}
			if string(checkedIn) != string(document) {
				t.Fatalf("checked-in handback fixture drifted from the Go document:\n%s\n%s", checkedIn, document)
			}
		})
	}
}
