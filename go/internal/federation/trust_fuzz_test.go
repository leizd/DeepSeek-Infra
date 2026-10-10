package federation

import (
	"testing"
)

func FuzzFederationEvaluate(f *testing.F) {
	f.Add("fleet-b", "PENDING", "VERIFIED", "minio", "us-east-1", "us", "region", "fleet-a")
	f.Add("fleet-a", "PENDING", "VERIFIED", "minio", "us-east-1", "us", "region", "fleet-a")
	f.Add("fleet-b", "PENDING", "ACTIVE", "minio", "us-east-1", "us", "region", "fleet-a")
	f.Add("fleet-b", "VERIFIED", "ACTIVE", "", "", "", "", "fleet-a")
	f.Add("fleet-c", "REVOKED", "ACTIVE", "s3", "eu-west-1", "eu", "datacenter", "fleet-a")
	f.Add("", "", "", "", "", "", "", "")

	f.Fuzz(func(t *testing.T, peerID, from, to, provider, region, jurisdiction, siteClass, localFleet string) {
		snapshot := map[string]any{
			"localFleetId": localFleet,
			"federationTransitions": []any{
				map[string]any{
					"peerFleetId": peerID,
					"from":        from,
					"to":          to,
					"metadata": map[string]any{
						"provider":     provider,
						"region":       region,
						"jurisdiction": jurisdiction,
						"siteClass":    siteClass,
					},
				},
			},
		}

		res := Evaluate(snapshot)
		if res == nil {
			t.Fatal("Evaluate returned nil")
		}
		transitions, ok := res["transitions"].([]any)
		if !ok || len(transitions) != 1 {
			t.Fatalf("expected 1 transition, got %d", len(transitions))
		}
		row, ok := transitions[0].(map[string]any)
		if !ok {
			t.Fatal("transition row not map")
		}
		dec, _ := row["decision"].(string)
		code, _ := row["code"].(string)
		if dec != "ALLOW" && dec != "REJECT" {
			t.Fatalf("invalid decision: %s", dec)
		}

		// TOFU Invariant: PENDING to ACTIVE can never be ALLOWed
		if from == "PENDING" && to == "ACTIVE" && dec == "ALLOW" {
			t.Fatal("TOFU violation: allowed direct transition from PENDING to ACTIVE")
		}

		// Same fleet invariant: peer cannot be local fleet
		if (peerID == "" || peerID == localFleet) && dec == "ALLOW" {
			t.Fatal("allowed transition with empty peer or same as local fleet")
		}

		// Revoked terminal invariant: REVOKED cannot transition to anything else
		if from == "REVOKED" && to != "REVOKED" && dec == "ALLOW" {
			t.Fatalf("allowed transition out of terminal REVOKED to %s", to)
		}

		if dec == "REJECT" && code == "" {
			t.Fatal("rejected transition must have non-empty reason code")
		}
	})
}
