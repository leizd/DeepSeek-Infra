package federation

import "github.com/leizd/DeepSeek-Infra/go/pkg/protocol"

var allowed = map[[2]string]struct{}{
	{"PENDING", "VERIFIED"}:  {},
	{"VERIFIED", "ACTIVE"}:   {},
	{"ACTIVE", "SUSPENDED"}:  {},
	{"PENDING", "REVOKED"}:   {},
	{"VERIFIED", "REVOKED"}:  {},
	{"ACTIVE", "REVOKED"}:    {},
	{"SUSPENDED", "REVOKED"}: {},
}

var metadataFields = []string{"provider", "region", "jurisdiction", "siteClass"}

func Evaluate(snapshot map[string]any) map[string]any {
	localFleet := protocol.AsString(snapshot["localFleetId"])
	results := make([]any, 0)
	for _, raw := range protocol.AsList(snapshot["federationTransitions"]) {
		item := protocol.AsMap(raw)
		peer := protocol.AsString(item["peerFleetId"])
		from := protocol.AsString(item["from"])
		to := protocol.AsString(item["to"])
		if peer == "" || peer == localFleet {
			results = append(results, row(peer, from, to, "REJECT", "FEDERATION_PEER_SAME_FLEET"))
			continue
		}
		metadata := protocol.AsMap(item["metadata"])
		missing := false
		for _, field := range metadataFields {
			if protocol.AsString(metadata[field]) == "" {
				missing = true
				break
			}
		}
		if missing {
			results = append(results, row(peer, from, to, "REJECT", "FEDERATION_PEER_METADATA_INVALID"))
			continue
		}
		if from == "REVOKED" && to != "REVOKED" {
			results = append(results, row(peer, from, to, "REJECT", "FEDERATION_PEER_REVOKED"))
			continue
		}
		if from == to {
			results = append(results, row(peer, from, to, "ALLOW", "IDEMPOTENT"))
			continue
		}
		if to == "ACTIVE" && from == "PENDING" {
			results = append(results, row(peer, from, to, "REJECT", "FEDERATION_PEER_NOT_VERIFIED"))
			continue
		}
		if _, ok := allowed[[2]string{from, to}]; !ok {
			results = append(results, row(peer, from, to, "REJECT", "FEDERATION_PEER_STATE_TRANSITION_INVALID"))
			continue
		}
		results = append(results, row(peer, from, to, "ALLOW", "OK"))
	}
	return map[string]any{"transitions": results}
}

func row(peer, from, to, decision, code string) map[string]any {
	return map[string]any{
		"peerFleetId": peer,
		"from":        from,
		"to":          to,
		"decision":    decision,
		"code":        code,
	}
}
