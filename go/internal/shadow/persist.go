package shadow

import (
	"encoding/json"
	"errors"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func Persist(control *store.Control, snapshot map[string]any, decision map[string]any) error {
	if control == nil {
		return nil
	}
	if err := persistActions(control, snapshot, decision); err != nil {
		return err
	}
	if err := persistWave(control, decision); err != nil {
		return err
	}
	if err := persistRisks(control, decision); err != nil {
		return err
	}
	if err := persistPeers(control, decision); err != nil {
		return err
	}
	if err := persistAgents(control, snapshot); err != nil {
		return err
	}
	return persistInventory(control, snapshot)
}

func persistActions(control *store.Control, snapshot map[string]any, decision map[string]any) error {
	epochs := map[string]uint64{}
	types := map[string]string{}
	for _, raw := range protocol.AsList(snapshot["actions"]) {
		action := protocol.AsMap(raw)
		id := protocol.AsString(action["actionId"])
		if id == "" {
			continue
		}
		epochs[id] = uint64(protocol.AsInt(action["executionEpoch"]))
		types[id] = protocol.AsString(action["type"])
	}
	scheduler := protocol.AsMap(decision["scheduler"])
	for _, raw := range protocol.AsList(scheduler["admissions"]) {
		admission := protocol.AsMap(raw)
		if protocol.AsString(admission["decision"]) != "ADMIT" {
			continue
		}
		id := protocol.AsString(admission["actionId"])
		epoch := epochs[id]
		if id == "" || epoch == 0 {
			continue
		}
		if err := remember(control, "action", id, "PENDING", epoch, admission); err != nil {
			return err
		}
		if err := dispatchAdmitted(control, "action", id, epoch, types[id]); err != nil {
			return err
		}
	}
	return nil
}

func commandKindFromType(actionType string) (internalprotocol.CommandKind, bool) {
	switch strings.ToUpper(actionType) {
	case "CREATE_REPAIR_JOB":
		return internalprotocol.CommandExecuteRepair, true
	case "CREATE_REBALANCE_JOB":
		return internalprotocol.CommandExecuteRebalance, true
	case "CREATE_BACKUP_JOB", "EXECUTE_BACKUP":
		return internalprotocol.CommandExecuteBackup, true
	case "CREATE_RESTORE_JOB", "EXECUTE_RESTORE":
		return internalprotocol.CommandExecuteRestore, true
	case "EXECUTE_FEDERATED_TRANSFER":
		return internalprotocol.CommandExecuteFederatedTransfer, true
	default:
		return internalprotocol.CommandUnspecified, false
	}
}

func dispatchAdmitted(control *store.Control, domain, id string, epoch uint64, actionType string) error {
	kind, ok := commandKindFromType(actionType)
	if !ok {
		return nil
	}
	fence := &internalprotocol.ActionFence{ActionId: id, ExecutionEpoch: epoch}
	if err := internalprotocol.ValidateFence(fence); err != nil {
		return err
	}
	record, exists, err := control.Get(domain, id)
	if err != nil {
		return err
	}
	if !exists {
		return internalprotocol.ErrFenceMismatch
	}
	err = internalprotocol.PlanNative(kind, fence, record.ExecutionEpoch)
	if err == nil || nativeNotAuthoritative(err) {
		return nil
	}
	return err
}

func nativeNotAuthoritative(err error) bool {
	return errors.Is(err, internalprotocol.ErrStorageNotAuthoritative) ||
		errors.Is(err, internalprotocol.ErrTransferNotAuthoritative) ||
		errors.Is(err, internalprotocol.ErrFederationNotAuthoritative) ||
		errors.Is(err, internalprotocol.ErrProofNotAuthoritative)
}

func persistWave(control *store.Control, decision map[string]any) error {
	wave := protocol.AsMap(decision["wave"])
	if protocol.AsString(wave["decision"]) != "ADMIT" {
		return nil
	}
	scheduleID := protocol.AsString(wave["scheduleId"])
	if scheduleID == "" {
		return nil
	}
	if err := remember(control, "wave", scheduleID, "PLANNED", 1, wave); err != nil {
		return err
	}
	return remember(control, "scheduler_run", scheduleID, "queued", 1, wave)
}

func persistRisks(control *store.Control, decision map[string]any) error {
	risk := protocol.AsMap(decision["risk"])
	for _, raw := range protocol.AsList(risk["risks"]) {
		item := protocol.AsMap(raw)
		severity := protocol.AsString(item["severity"])
		target := protocol.AsString(item["target"])
		if target == "" || severity == "" || severity == "healthy" {
			continue
		}
		if err := remember(control, "risk", target, "OPEN", 0, item); err != nil {
			return err
		}
	}
	return nil
}

func persistAgents(control *store.Control, snapshot map[string]any) error {
	result := agent.Evaluate(snapshot)
	for _, raw := range protocol.AsList(result["admissions"]) {
		admission := protocol.AsMap(raw)
		if protocol.AsString(admission["decision"]) != "ADMIT" {
			continue
		}
		id := protocol.AsString(admission["runId"])
		if id == "" {
			continue
		}
		if err := remember(control, "agent_run", id, "created", 0, admission); err != nil {
			return err
		}
	}
	return nil
}

func persistInventory(control *store.Control, snapshot map[string]any) error {
	type spec struct {
		key, idField, domain, state string
		epoch                       uint64
	}
	for _, item := range []spec{
		{"policies", "policyId", "policy", "ACTIVE", 0},
		{"targets", "targetId", "target", "ACTIVE", 0},
		{"grants", "grantId", "grant", "ACTIVE", 0},
		{"sessions", "sessionId", "session", "PENDING", 0},
		{"forecasts", "forecastId", "forecast", "ACTIVE", 0},
		{"transfers", "transferId", "transfer", "PROPOSED", 1},
	} {
		for _, raw := range protocol.AsList(snapshot[item.key]) {
			row := protocol.AsMap(raw)
			id := protocol.AsString(row[item.idField])
			if id == "" {
				continue
			}
			epoch := item.epoch
			if item.domain == "transfer" {
				if parsed := uint64(protocol.AsInt(row["executionEpoch"])); parsed != 0 {
					epoch = parsed
				}
			}
			if err := remember(control, item.domain, id, item.state, epoch, row); err != nil {
				return err
			}
			if item.domain == "transfer" {
				if err := dispatchAdmitted(control, "transfer", id, epoch, "EXECUTE_FEDERATED_TRANSFER"); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

func persistPeers(control *store.Control, decision map[string]any) error {
	federation := protocol.AsMap(decision["federation"])
	for _, raw := range protocol.AsList(federation["transitions"]) {
		item := protocol.AsMap(raw)
		if protocol.AsString(item["decision"]) != "ALLOW" {
			continue
		}
		peer := protocol.AsString(item["peerFleetId"])
		from := protocol.AsString(item["from"])
		to := protocol.AsString(item["to"])
		if peer == "" || from == "" || to == "" {
			continue
		}
		if err := remember(control, "peer", peer, from, 0, item); err != nil && err != store.ErrIllegalTransition {
			return err
		}
		if from != to {
			if err := advance(control, "peer", peer, to, item); err != nil && err != store.ErrIllegalTransition {
				return err
			}
		}
	}
	return nil
}

func remember(control *store.Control, domain, id, state string, epoch uint64, payload any) error {
	_, ok, err := control.Get(domain, id)
	if err != nil || ok {
		return err
	}
	raw, _ := json.Marshal(payload)
	return control.Put(store.Record{Domain: domain, ID: id, Revision: 1, ExecutionEpoch: epoch, State: state, Payload: raw})
}

func advance(control *store.Control, domain, id, state string, payload any) error {
	existing, ok, err := control.Get(domain, id)
	if err != nil || !ok {
		return err
	}
	raw, _ := json.Marshal(payload)
	existing.Revision++
	existing.State = state
	existing.Payload = raw
	return control.Put(existing)
}
