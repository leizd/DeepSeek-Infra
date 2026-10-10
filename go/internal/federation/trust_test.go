package federation

import "testing"

func TestEvaluatePeerTransitions(t *testing.T) {
	meta := map[string]any{"provider": "minio", "region": "us", "jurisdiction": "us", "siteClass": "standard"}
	got := Evaluate(map[string]any{
		"localFleetId": "fleet-a",
		"federationTransitions": []any{
			map[string]any{"peerFleetId": "fleet-a", "from": "PENDING", "to": "VERIFIED", "metadata": meta},
			map[string]any{"peerFleetId": "fleet-b", "from": "PENDING", "to": "VERIFIED"},
			map[string]any{"peerFleetId": "fleet-c", "from": "PENDING", "to": "ACTIVE", "metadata": meta},
			map[string]any{"peerFleetId": "fleet-d", "from": "PENDING", "to": "VERIFIED", "metadata": meta},
			map[string]any{"peerFleetId": "fleet-e", "from": "REVOKED", "to": "ACTIVE", "metadata": meta},
			map[string]any{"peerFleetId": "fleet-f", "from": "PENDING", "to": "PENDING", "metadata": meta},
			map[string]any{"peerFleetId": "fleet-g", "from": "ACTIVE", "to": "PENDING", "metadata": meta},
		},
	})
	rows := got["transitions"].([]any)
	if len(rows) != 7 {
		t.Fatalf("rows %d", len(rows))
	}
	codes := make([]string, 0, len(rows))
	for _, raw := range rows {
		codes = append(codes, raw.(map[string]any)["code"].(string))
	}
	want := []string{
		"FEDERATION_PEER_SAME_FLEET",
		"FEDERATION_PEER_METADATA_INVALID",
		"FEDERATION_PEER_NOT_VERIFIED",
		"OK",
		"FEDERATION_PEER_REVOKED",
		"IDEMPOTENT",
		"FEDERATION_PEER_STATE_TRANSITION_INVALID",
	}
	for i, code := range want {
		if codes[i] != code {
			t.Fatalf("code[%d]=%s want %s (%v)", i, codes[i], code, codes)
		}
	}
}
