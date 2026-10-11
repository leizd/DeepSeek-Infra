package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func agentOutcomeFixture(t *testing.T) (*Control, AgentNodeOutcomeRequest, ed25519.PrivateKey) {
	t.Helper()
	control, request := agentNodeDispatchFixture(t)
	admitted, err := control.BeginAgentNodeDispatch(request)
	if err != nil {
		t.Fatal(err)
	}
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	control.agentOutcomeSignerKey = base64.RawURLEncoding.EncodeToString(public)
	intent := agentOutcomeIntent(admitted.Intent)
	receipt := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey,
		Evidence: &agentv1.AgentNodeOutcomeEvidence{SchemaVersion: 1, Fence: intent.Fence, OperationId: intent.OperationId,
			RunId: intent.RunId, NodeId: intent.NodeId, Phase: intent.Phase, Request: intent.Request, PlanDigest: intent.PlanDigest,
			MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch, WriterFencingToken: admitted.Admission.Lease.WriterFencingToken,
			CreatedAt:     time.Unix(intent.RecordedAt, 0).UTC().Format("2006-01-02T15:04:05Z"),
			ConfirmedAt:   time.Unix(intent.RecordedAt+1, 0).UTC().Format("2006-01-02T15:04:05Z"),
			OutcomeSha256: strings.Repeat("a", 64), OutcomeLength: 30,
			PublicEvent: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 20},
			FleetId:     control.fleetID, Environment: control.environment}}
	agentOutcomeSign(t, receipt, private)
	return control, AgentNodeOutcomeRequest{OperationID: "node-outcome-consume", Actor: "operator:agent-test",
		ActionID: request.ActionID, Epoch: request.Epoch, RunID: request.RunID, NodeID: request.NodeID, Receipt: receipt}, private
}

func agentOutcomeSign(t *testing.T, receipt *agentv1.AgentNodeOutcomeReceipt, private ed25519.PrivateKey) {
	t.Helper()
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(private, append([]byte(agentproof.NodeOutcomeDomain), encoded...))
}

func TestAgentNodeOutcomeDurableReplayPreservesPhaseAndLocks(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	lease, _, _ := control.GetActionLease(input.ActionID)
	resources, _ := control.GetResourceLeases(input.ActionID)
	run, _, _ := control.AgentRunMetadata(input.RunID)
	before := countControlEvents(t, control)
	encoded, _ := proto.MarshalOptions{Deterministic: true}.Marshal(input.Receipt)
	digest := sha256.Sum256(encoded)
	first, err := control.RecordAgentNodeOutcome(input)
	if err != nil || first.AlreadyRecorded || first.ReceiptSHA256 != hex.EncodeToString(digest[:]) ||
		first.Mutation.Domain != "action" || first.Mutation.State != "EXECUTING" || countControlEvents(t, control) != before+1 {
		t.Fatalf("durable receipt consumption failed: %+v %v", first, err)
	}
	for _, operation := range []string{input.OperationID, "same-receipt-new-operation"} {
		retry := input
		retry.OperationID = operation
		result, err := control.RecordAgentNodeOutcome(retry)
		if err != nil || !result.AlreadyRecorded || result.ReceiptSHA256 != first.ReceiptSHA256 || countControlEvents(t, control) != before+1 {
			t.Fatalf("replay wrote another proof/history event: %+v %v", result, err)
		}
	}
	input.Receipt.Evidence.PublicEvent.Length++
	_, intent, receipt, found, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || receipt == nil || receipt.Evidence.PublicEvent.Length != 20 || intent.State != "INTENT_RECORDED" {
		t.Fatalf("caller mutated durable evidence or intent: %v", err)
	}
	receipt.Signature[0] ^= 1
	_, _, nextReceipt, _, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
	if err != nil || bytes.Equal(receipt.Signature, nextReceipt.Signature) {
		t.Fatalf("query leaked a mutable alias: %v", err)
	}
	afterLease, _, _ := control.GetActionLease(input.ActionID)
	afterResources, _ := control.GetResourceLeases(input.ActionID)
	afterRun, _, _ := control.AgentRunMetadata(input.RunID)
	firstBytes, _ := json.Marshal([]any{lease, resources, run})
	secondBytes, _ := json.Marshal([]any{afterLease, afterResources, afterRun})
	if !bytes.Equal(firstBytes, secondBytes) {
		t.Fatal("receipt consumption changed lease, resources or public run projection")
	}
}

func TestAgentNodeOutcomeRejectsForgedAndReboundReceiptsWithoutPartialWrites(t *testing.T) {
	control, good, private := agentOutcomeFixture(t)
	before := countControlEvents(t, control)
	cases := []struct {
		name string
		edit func(*AgentNodeOutcomeRequest)
	}{
		{"no-receipt", func(v *AgentNodeOutcomeRequest) { v.Receipt = nil }},
		{"no-action", func(v *AgentNodeOutcomeRequest) { v.ActionID = "" }},
		{"no-run", func(v *AgentNodeOutcomeRequest) { v.RunID = "" }},
		{"zero-epoch", func(v *AgentNodeOutcomeRequest) { v.Epoch = 0 }},
		{"future-epoch", func(v *AgentNodeOutcomeRequest) { v.Epoch++ }},
		{"wrong-run", func(v *AgentNodeOutcomeRequest) { v.RunID = "other-run" }},
		{"missing-node", func(v *AgentNodeOutcomeRequest) { v.NodeID = "critic" }},
		{"empty-node", func(v *AgentNodeOutcomeRequest) { v.NodeID = "" }},
		{"bad-signature", func(v *AgentNodeOutcomeRequest) { v.Receipt.Signature[0] ^= 1 }},
		{"wrong-writer", func(v *AgentNodeOutcomeRequest) {
			v.Receipt.Evidence.WriterFencingToken++
			agentOutcomeSign(t, v.Receipt, private)
		}},
		{"signed-foreign-run", func(v *AgentNodeOutcomeRequest) {
			v.Receipt.Evidence.RunId = "other-run"
			agentOutcomeSign(t, v.Receipt, private)
		}},
		{"signed-stale-metadata", func(v *AgentNodeOutcomeRequest) {
			v.Receipt.Evidence.MetadataEpoch++
			agentOutcomeSign(t, v.Receipt, private)
		}},
		{"unknown-fields", func(v *AgentNodeOutcomeRequest) { v.Receipt.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1}) }},
		{"no-operation", func(v *AgentNodeOutcomeRequest) { v.OperationID = "" }},
		{"no-actor", func(v *AgentNodeOutcomeRequest) { v.Actor = "" }},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			input := good
			input.Receipt = proto.Clone(good.Receipt).(*agentv1.AgentNodeOutcomeReceipt)
			test.edit(&input)
			if _, err := control.RecordAgentNodeOutcome(input); err == nil {
				t.Fatal("forged result was persisted")
			}
			if countControlEvents(t, control) != before {
				t.Fatal("refused proof wrote partial history")
			}
			if _, _, receipt, _, err := control.AgentNodeOutcomeSnapshot(good.ActionID, good.NodeID); err != nil || receipt != nil {
				t.Fatalf("refused proof retained evidence: %v", err)
			}
		})
	}
	key := control.agentOutcomeSignerKey
	control.agentOutcomeSignerKey = ""
	if _, err := control.RecordAgentNodeOutcome(good); !errors.Is(err, agentproof.ErrAgentNodeOutcomeInvalid) {
		t.Fatalf("missing deployment pin accepted a caller's key: %v", err)
	}
	control.agentOutcomeSignerKey = key
	for _, domain := range []string{"agent_run", "action"} {
		cutover, _ := control.GetCutover(domain)
		if _, err := control.db.Exec("UPDATE control_cutover SET state=? WHERE domain=?", CutoverShadow, domain); err != nil {
			t.Fatal(err)
		}
		if _, err := control.RecordAgentNodeOutcome(good); !errors.Is(err, ErrCutoverNotAuthorized) {
			t.Fatalf("lost %s authority: %v", domain, err)
		}
		if _, err := control.db.Exec("UPDATE control_cutover SET state=? WHERE domain=?", cutover.State, domain); err != nil {
			t.Fatal(err)
		}
	}
}

func TestAgentNodeOutcomeConcurrentConsumersHaveOneDurableProof(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	before := countControlEvents(t, control)
	var tasks sync.WaitGroup
	results := make(chan AgentNodeOutcomeResult, 8)
	failures := make(chan error, 8)
	for index := 0; index < 8; index++ {
		tasks.Add(1)
		go func(index int) {
			defer tasks.Done()
			request := input
			request.OperationID = fmt.Sprintf("outcome-concurrent-%d", index)
			result, err := control.RecordAgentNodeOutcome(request)
			results <- result
			failures <- err
		}(index)
	}
	tasks.Wait()
	close(results)
	close(failures)
	for err := range failures {
		if err != nil {
			t.Fatal(err)
		}
	}
	first := 0
	for result := range results {
		if !result.AlreadyRecorded {
			first++
		}
	}
	if first != 1 || countControlEvents(t, control) != before+1 {
		t.Fatalf("first receipts = %d", first)
	}
}

func TestAgentNodeOutcomeImmutableConflictAndGenericWriteDenial(t *testing.T) {
	control, input, private := agentOutcomeFixture(t)
	if _, err := control.RecordAgentNodeOutcome(input); err != nil {
		t.Fatal(err)
	}
	before := countControlEvents(t, control)
	conflict := input
	conflict.OperationID = "conflicting-outcome"
	conflict.Receipt = proto.Clone(input.Receipt).(*agentv1.AgentNodeOutcomeReceipt)
	conflict.Receipt.Evidence.PublicEvent.Length++
	agentOutcomeSign(t, conflict.Receipt, private)
	if _, err := control.RecordAgentNodeOutcome(conflict); !errors.Is(err, ErrAgentNodeOutcomeConflict) {
		t.Fatalf("conflicting proof overwrote result: %v", err)
	}
	replay := input
	replay.Actor = "different-actor"
	if _, err := control.RecordAgentNodeOutcome(replay); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("operation was rebound: %v", err)
	}
	record, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	for _, replace := range []json.RawMessage{nil, json.RawMessage(`{}`), json.RawMessage(`{"coder":{"writerFencingToken":1,"receipt":"eA=="}}`)} {
		modified := record
		modified.Revision++
		var fields map[string]json.RawMessage
		_ = json.Unmarshal(record.Payload, &fields)
		if replace == nil {
			delete(fields, agentNodeOutcomeField)
		} else {
			fields[agentNodeOutcomeField] = replace
		}
		modified.Payload, _ = json.Marshal(fields)
		if err := control.Put(modified); err == nil {
			t.Fatal("generic write replaced protected outcome evidence")
		}
	}
	if countControlEvents(t, control) != before {
		t.Fatal("refused replacement wrote partial history")
	}
}

func TestAgentNodeOutcomeExpiredLeaseAndRestartRetainHistoricalProof(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	if _, err := control.db.Exec("UPDATE action_leases SET lease_until=? WHERE action_id=?", control.now()-1, input.ActionID); err != nil {
		t.Fatal(err)
	}
	if _, err := control.RecordAgentNodeOutcome(input); err != nil {
		t.Fatalf("expired lease erased historical proof: %v", err)
	}
	options := OpenOptions{Path: control.path, Owner: "outcome-successor", Now: control.now, AuthorizeCutover: true,
		PromotionSignerPublicKey: control.promotionSignerKey, AgentOutcomeSignerPublicKey: control.agentOutcomeSignerKey,
		FleetID: control.fleetID, Environment: control.environment}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	_, _, receipt, found, err := successor.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || !proto.Equal(receipt, input.Receipt) {
		t.Fatalf("restart changed historical proof: %v", err)
	}
	// A later terminal lease ledger cannot erase independently captured history.
	if _, err := successor.db.Exec("UPDATE action_leases SET terminal_state='SUCCEEDED' WHERE action_id=?", input.ActionID); err != nil {
		t.Fatal(err)
	}
	if _, _, receipt, _, err := successor.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID); err != nil || !proto.Equal(receipt, input.Receipt) {
		t.Fatalf("terminal lease erased proof: %v", err)
	}
}

func TestAgentNodeOutcomePayloadAndReadGuards(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	if _, _, receipt, found, err := control.AgentNodeOutcomeSnapshot("absent-action", input.NodeID); err != nil || found || receipt != nil {
		t.Fatalf("absence invented evidence: %v", err)
	}
	if _, _, receipt, found, err := control.AgentNodeOutcomeSnapshot(input.ActionID, "absent-node"); err != nil || found || receipt != nil {
		t.Fatalf("absent node invented evidence: %v", err)
	}
	for _, payload := range []string{`not-json`, `{"nativeAgentNodeOutcomes":null}`, `{"nativeAgentNodeOutcomes":[]}`, `{"nativeAgentNodeOutcomes":{"coder":{"unexpected":1}}}`} {
		if _, err := agentNodeOutcomeBytes(Record{Payload: json.RawMessage(payload)}); err == nil {
			t.Fatalf("corrupt outcome payload accepted: %s", payload)
		}
	}
	for _, payload := range []string{`not-json`, `{}`, `{"nativeAgentNodeOutcomes":null}`} {
		bad := Record{Payload: json.RawMessage(payload)}
		if sameAgentNodeOutcomes(bad, Record{Payload: json.RawMessage(`{"nativeAgentNodeOutcomes":{}}`)}) {
			t.Fatal("untrusted/missing marker compared equal")
		}
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, _, _, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("closed outcome snapshot: %v", err)
	}
}
