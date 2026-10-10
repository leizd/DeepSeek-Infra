package store

import (
	"encoding/json"
	"errors"
	"math"
	"strings"
	"sync"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

func agentExecutionControl(t *testing.T, promoteAction bool) *Control {
	t.Helper()
	control := freshAgentEventControl(t)
	t.Cleanup(func() { _ = control.Close() })
	if promoteAction {
		dual := dualEvaluate(t, control, "action")
		transition := CutoverTransition{Domain: "action", To: CutoverGoAuthoritative,
			ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
			TransferID: "fresh-agent-execution", Authority: emptyPythonInventoryCheckpoint(t)}
		var err error
		transition.Promotion, err = SignPromotionArtifact(PromotionArtifactForTransition(transition, dual, control.now(), "fleet-a", "production"), promotionTestPrivate)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.TransitionCutover(transition); err != nil {
			t.Fatal(err)
		}
	}
	events := []agent.RunEvent{{Type: "run_status", Status: "created"}, {Type: "run_status", Status: "planning"},
		{Type: "agent_plan", Plan: []agent.RunPlanNode{{ID: "coder", Task: "write"}, {ID: "critic", Task: "review", DependsOn: []string{"coder"}}}},
		{Type: "run_status", Status: "running"}}
	for index, event := range events {
		input := eventMutation(t, control, int64(index), event)
		if index != 0 {
			input.Create = nil
		}
		if _, err := control.AppendAgentRunEvent(input); err != nil {
			t.Fatal(err)
		}
	}
	return control
}

func agentExecutionRequest(t *testing.T, control *Control) AgentRunExecutionRequest {
	t.Helper()
	cutover, err := control.GetCutover("agent_run")
	if err != nil {
		t.Fatal(err)
	}
	return AgentRunExecutionRequest{OperationID: "agent-execution-claim", Actor: "operator:agent-test", RunID: "run_events",
		Phase: "tasks", ExpectedIndex: 4, Owner: "rust-agent-worker", LeaseSeconds: 60,
		Fence: &internalprotocol.ActionFence{ActionId: "run_events", ExecutionEpoch: uint64(cutover.Epoch)}}
}

func TestAgentRunExecutionClaimPersistsDistinctFenceReservationsAndReplay(t *testing.T) {
	control := agentExecutionControl(t, true)
	request := agentExecutionRequest(t, control)
	result, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	claim := result.Admission.Lease
	if claim.ActionID == request.RunID || claim.Epoch != 1 || claim.Owner != request.Owner || len(claim.ClaimToken) != 64 ||
		claim.WriterFencingToken != control.Writer().FencingToken || result.Admission.Record.State != "CLAIMED" {
		t.Fatalf("unbound execution claim: %+v", result)
	}
	if len(result.Admission.ResourceKeys) != 1 || result.Admission.ResourceKeys[0] != "agent-run:run_events" {
		t.Fatalf("missing mandatory run reservation: %+v", result.Admission.ResourceKeys)
	}
	run, found, err := control.AgentRunMetadata(request.RunID)
	if err != nil || !found || run.NextIndex != 4 || run.Nodes()["coder"].Attempts != 0 {
		t.Fatalf("claim fabricated metadata/provider progress: %+v %v", run, err)
	}
	count := countControlEvents(t, control)
	replayed, err := control.ClaimAgentRunExecution(request)
	if err != nil || replayed.Mutation.Status != OperatorMutationAlreadyApplied ||
		replayed.Admission.Lease != claim || countControlEvents(t, control) != count {
		t.Fatalf("lost-response replay made another execution: %+v %v", replayed, err)
	}
	changed := request
	changed.Owner = "another-worker"
	if _, err := control.ClaimAgentRunExecution(changed); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("operation rebound to another worker: %v", err)
	}
	stored, exists, err := control.GetActionLease(claim.ActionID)
	if err != nil || !exists || stored != claim {
		t.Fatalf("durable lease: %+v %v", stored, err)
	}
}

func TestAgentRunExecutionRequiresBothPromotedDomainsAndCurrentRun(t *testing.T) {
	control := agentExecutionControl(t, false)
	request := agentExecutionRequest(t, control)
	count := countControlEvents(t, control)
	if _, err := control.ClaimAgentRunExecution(request); !errors.Is(err, ErrCutoverNotAuthorized) || countControlEvents(t, control) != count {
		t.Fatalf("metadata ownership authorized effects: %v", err)
	}
	control = agentExecutionControl(t, true)
	request = agentExecutionRequest(t, control)
	cases := []AgentRunExecutionRequest{request, request, request, request}
	cases[0].ExpectedIndex--
	cases[1].Phase = "plan"
	cases[2].Fence = &internalprotocol.ActionFence{ActionId: "another-run", ExecutionEpoch: request.Fence.ExecutionEpoch}
	cases[3].Fence = &internalprotocol.ActionFence{ActionId: request.RunID, ExecutionEpoch: request.Fence.ExecutionEpoch + 1}
	for _, denied := range cases {
		before := countControlEvents(t, control)
		if _, err := control.ClaimAgentRunExecution(denied); err == nil || countControlEvents(t, control) != before {
			t.Fatalf("invalid claim wrote state: %+v %v", denied, err)
		}
	}
	cancel := eventMutation(t, control, 4, agent.RunEvent{Type: "run_status", Status: "cancelled"})
	cancel.Create = nil
	if _, err := control.AppendAgentRunEvent(cancel); err != nil {
		t.Fatal(err)
	}
	request.ExpectedIndex = 5
	if _, err := control.ClaimAgentRunExecution(request); err == nil {
		t.Fatal("cancelled run admitted effects")
	}
}

func TestAgentRunExecutionFaultsRollbackActionLeaseAndReceiptTogether(t *testing.T) {
	for _, stage := range []string{"after_budget_check", "after_first_resource", "after_action_lease_insert", "after_control_event_write", "at_commit"} {
		t.Run(stage, func(t *testing.T) {
			control := agentExecutionControl(t, true)
			request := agentExecutionRequest(t, control)
			before := countControlEvents(t, control)
			control.admissionFaultStage = stage
			if _, err := control.ClaimAgentRunExecution(request); !errors.Is(err, errInjectedAdmissionFault) {
				t.Fatalf("fault not reached: %v", err)
			}
			if countControlEvents(t, control) != before {
				t.Fatal("partial action history survived rollback")
			}
			if _, exists, err := control.ReadOperatorMutation(request.OperationID); err != nil || exists {
				t.Fatalf("partial operator receipt: %v %v", exists, err)
			}
			control.admissionFaultStage = ""
			if _, err := control.ClaimAgentRunExecution(request); err != nil {
				t.Fatalf("rollback left a conflicting reservation: %v", err)
			}
		})
	}
}

func TestAgentRunExecutionConcurrentClaimsAndNextRoundCannotDuplicate(t *testing.T) {
	control := agentExecutionControl(t, true)
	request := agentExecutionRequest(t, control)
	results := make(chan error, 2)
	var workers sync.WaitGroup
	for _, operation := range []string{"first-claim", "competing-claim"} {
		workers.Add(1)
		go func(operation string) {
			defer workers.Done()
			input := request
			input.OperationID = operation
			_, err := control.ClaimAgentRunExecution(input)
			results <- err
		}(operation)
	}
	workers.Wait()
	close(results)
	passed := 0
	for err := range results {
		if err == nil {
			passed++
		} else if !errors.Is(err, ErrActionLeaseActive) {
			t.Fatalf("unexpected competing claim: %v", err)
		}
	}
	if passed != 1 {
		t.Fatalf("duplicate admission: %d", passed)
	}
	changed := eventMutation(t, control, 4, agent.RunEvent{Type: "agent_plan", Plan: []agent.RunPlanNode{{ID: "coder", Task: "changed"}}})
	changed.Create = nil
	if _, err := control.AppendAgentRunEvent(changed); err != nil {
		t.Fatal(err)
	}
	request.ExpectedIndex, request.OperationID = 5, "next-round"
	if _, err := control.ClaimAgentRunExecution(request); !errors.Is(err, ErrResourceConflict) {
		t.Fatalf("plan change bypassed unresolved run reservation: %v", err)
	}
}

func TestAgentRunExecutionGenericWritesCannotCreateOrRewriteIntent(t *testing.T) {
	control := agentExecutionControl(t, true)
	request := agentExecutionRequest(t, control)
	result, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	for _, method := range []func(Record) error{control.Put, control.PutShadow} {
		for _, payload := range []string{string(result.Admission.Record.Payload), `{}`} {
			record := result.Admission.Record
			record.Revision++
			record.Payload = []byte(payload)
			if err := method(record); err == nil {
				t.Fatal("generic writer rewrote a native execution intent")
			}
		}
	}
	newRecord := result.Admission.Record
	newRecord.ID, newRecord.Revision, newRecord.State = "forged-native-execution", 1, "PENDING"
	if err := control.Put(newRecord); !errors.Is(err, ErrAgentRunExecutionRequired) {
		t.Fatalf("generic writer created an execution intent: %v", err)
	}
	if strings.Contains(string(result.Admission.Record.Payload), "claimToken") {
		t.Fatal("claim credential leaked into action payload")
	}
}

func TestAgentRunExecutionRenewalStopsOnCancelOrPlanChange(t *testing.T) {
	for _, event := range []agent.RunEvent{{Type: "run_status", Status: "cancelled"},
		{Type: "agent_plan", Plan: []agent.RunPlanNode{{ID: "coder", Task: "a different task"}}}} {
		t.Run(event.Type, func(t *testing.T) {
			control := agentExecutionControl(t, true)
			request := agentExecutionRequest(t, control)
			result, err := control.ClaimAgentRunExecution(request)
			if err != nil {
				t.Fatal(err)
			}
			claim := result.Admission.Lease
			renewal := ActionLeaseRenewal{ActionID: claim.ActionID, Epoch: claim.Epoch, Owner: claim.Owner, ClaimToken: claim.ClaimToken, LeaseSeconds: 60}
			if _, err := control.RenewActionLease(renewal); err != nil {
				t.Fatalf("live run cannot renew: %v", err)
			}
			input := eventMutation(t, control, 4, event)
			input.Create = nil
			if _, err := control.AppendAgentRunEvent(input); err != nil {
				t.Fatal(err)
			}
			if _, err := control.RenewActionLease(renewal); err == nil {
				t.Fatal("stale run scope kept an execution lease alive")
			}
			if _, err := control.ClaimAgentRunExecution(request); err == nil {
				t.Fatal("lost-response replay reauthorized stale run scope")
			}
			locks, err := control.GetResourceLeases(claim.ActionID)
			if err != nil || len(locks) != 1 {
				t.Fatalf("uncertain old execution lost its reservation: %+v %v", locks, err)
			}
		})
	}
}

func TestAgentRunExecutionReopenAndTakeoverRequireReconciliation(t *testing.T) {
	control := agentExecutionControl(t, true)
	request := agentExecutionRequest(t, control)
	first, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	old := first.Admission.Lease
	options := OpenOptions{Path: control.path, Owner: "agent-control-successor", Now: func() int64 { return 1061 },
		AuthorizeCutover: true, PromotionSignerPublicKey: control.promotionSignerKey, FleetID: control.fleetID, Environment: control.environment}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = successor.Close() })
	if persisted, found, err := successor.GetActionLease(old.ActionID); err != nil || !found || persisted != old {
		t.Fatalf("reopen lost the original claim: %+v %v", persisted, err)
	}
	if _, err := successor.ClaimAgentRunExecution(request); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("old operator retry survived writer takeover: %v", err)
	}
	request.OperationID, request.Owner = "agent-execution-takeover", "successor-rust-worker"
	next, err := successor.ClaimAgentRunExecution(request)
	if err != nil || next.Admission.Record.State != "RECONCILING" || next.Admission.Lease.Epoch != old.Epoch+1 ||
		next.Admission.Lease.ClaimToken == old.ClaimToken || next.Admission.Lease.ActionID != old.ActionID {
		t.Fatalf("takeover granted another fresh dispatch: %+v %v", next, err)
	}
	if _, err := successor.RenewActionLease(ActionLeaseRenewal{ActionID: old.ActionID, Epoch: old.Epoch, Owner: old.Owner, ClaimToken: old.ClaimToken}); err == nil {
		t.Fatal("stale worker retained authority")
	}
	cancel := eventMutation(t, successor, 4, agent.RunEvent{Type: "run_status", Status: "cancelled"})
	cancel.Create = nil
	if _, err := successor.AppendAgentRunEvent(cancel); err != nil {
		t.Fatal(err)
	}
	claim := next.Admission.Lease
	if _, err := successor.RenewActionLease(ActionLeaseRenewal{ActionID: claim.ActionID, Epoch: claim.Epoch, Owner: claim.Owner, ClaimToken: claim.ClaimToken}); err != nil {
		t.Fatalf("read-only reconciliation cannot keep its lease: %v", err)
	}
}

func TestAgentRunExecutionReplayCommitsTheAdvertisedWriterDeadline(t *testing.T) {
	control := agentExecutionControl(t, true)
	clock := int64(1000)
	control.now = func() int64 { return clock }
	request := agentExecutionRequest(t, control)
	first, err := control.ClaimAgentRunExecution(request)
	if err != nil || first.WriterLeaseUntil != control.Writer().LeaseUntil {
		t.Fatalf("claim advertised an uncommitted writer deadline: %+v %v", first, err)
	}
	clock = 1010
	replay, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	var storedDeadline int64
	if err := control.db.QueryRow("SELECT lease_until FROM control_writer WHERE singleton=1").Scan(&storedDeadline); err != nil {
		t.Fatal(err)
	}
	if replay.WriterLeaseUntil <= first.WriterLeaseUntil || replay.WriterLeaseUntil != storedDeadline ||
		replay.WriterLeaseUntil != control.Writer().LeaseUntil || replay.Admission.Lease != first.Admission.Lease {
		t.Fatalf("lost-response retry advertised a rolled-back writer renewal: %+v stored=%d", replay, storedDeadline)
	}
}

func TestAgentRunExecutionRejectsLossyIdentityNormalization(t *testing.T) {
	for _, field := range []string{"operation", "actor", "owner", "run"} {
		t.Run(field, func(t *testing.T) {
			control := agentExecutionControl(t, true)
			request := agentExecutionRequest(t, control)
			invalid := "identity-" + string([]byte{0xff})
			switch field {
			case "operation":
				request.OperationID = invalid
			case "actor":
				request.Actor = invalid
			case "owner":
				request.Owner = invalid
			case "run":
				request.RunID = invalid
			}
			before := countControlEvents(t, control)
			if _, err := control.ClaimAgentRunExecution(request); !errors.Is(err, ErrAgentRunExecutionRequired) {
				t.Fatalf("invalid UTF-8 %s identity was rewritten and admitted: %v", field, err)
			}
			if countControlEvents(t, control) != before {
				t.Fatal("rejected identity created durable work")
			}
		})
	}
}

func TestAgentRunExecutionRejectsInvalidScopeWithoutDurableWork(t *testing.T) {
	control := agentExecutionControl(t, true)
	request := agentExecutionRequest(t, control)
	for name, mutate := range map[string]func(*AgentRunExecutionRequest){
		"missing_operation": func(r *AgentRunExecutionRequest) { r.OperationID = "" },
		"nul_actor":         func(r *AgentRunExecutionRequest) { r.Actor = "actor\x00hidden" },
		"oversized_owner":   func(r *AgentRunExecutionRequest) { r.Owner = strings.Repeat("x", 257) },
		"missing_owner":     func(r *AgentRunExecutionRequest) { r.Owner = "   " },
		"path_run":          func(r *AgentRunExecutionRequest) { r.RunID = "../another" },
		"unknown_phase":     func(r *AgentRunExecutionRequest) { r.Phase = "tool" },
		"negative_lease":    func(r *AgentRunExecutionRequest) { r.LeaseSeconds = -1 },
		"oversized_lease":   func(r *AgentRunExecutionRequest) { r.LeaseSeconds = 301 },
		"overflow_cursor":   func(r *AgentRunExecutionRequest) { r.ExpectedIndex = math.MaxInt64 },
	} {
		t.Run(name, func(t *testing.T) {
			input := request
			mutate(&input)
			before := countControlEvents(t, control)
			if _, err := control.ClaimAgentRunExecution(input); !errors.Is(err, ErrAgentRunExecutionRequired) || countControlEvents(t, control) != before {
				t.Fatalf("invalid scope wrote durable state: %v", err)
			}
		})
	}
	request.RunID, request.Fence.ActionId = "missing-run", "missing-run"
	if _, err := control.ClaimAgentRunExecution(request); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("missing metadata was treated as ready: %v", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ClaimAgentRunExecution(request); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("closed writer admitted execution: %v", err)
	}
}

func TestAgentRunExecutionCommitClockRefusalRollsBackEveryReservation(t *testing.T) {
	for name, deadline := range map[string]int64{"backward": 999, "writer_expired": 1061, "action_expired": 1002} {
		t.Run(name, func(t *testing.T) {
			control := agentExecutionControl(t, true)
			request := agentExecutionRequest(t, control)
			request.LeaseSeconds = 1
			before := countControlEvents(t, control)
			calls := 0
			control.now = func() int64 {
				calls++
				if calls <= 2 {
					return 1000
				}
				return deadline
			}
			_, err := control.ClaimAgentRunExecution(request)
			if !errors.Is(err, ErrWriterFenceHeld) && !errors.Is(err, ErrActionLeaseExpired) {
				t.Fatalf("expired transaction returned a usable claim: %v", err)
			}
			control.now = func() int64 { return 1000 }
			if countControlEvents(t, control) != before {
				t.Fatal("refused commit left action progress")
			}
			for _, table := range []string{"action_journal", "action_leases", "action_resource_leases", "control_operator_mutations"} {
				var count int
				query := "SELECT COUNT(*) FROM " + table
				if table == "control_operator_mutations" {
					query += " WHERE operation_id='agent-execution-claim'"
				}
				if err := control.db.QueryRow(query).Scan(&count); err != nil || count != 0 {
					t.Fatalf("%s survived refused execution commit: %d %v", table, count, err)
				}
			}
		})
	}
}

func TestAgentRunExecutionReplayRefusesExpiredOrDamagedClaim(t *testing.T) {
	for _, scenario := range []string{"expired", "removed_resource", "changed_request", "missing_action"} {
		t.Run(scenario, func(t *testing.T) {
			control := agentExecutionControl(t, true)
			request := agentExecutionRequest(t, control)
			if scenario == "expired" {
				request.LeaseSeconds = 10
			}
			result, err := control.ClaimAgentRunExecution(request)
			if err != nil {
				t.Fatal(err)
			}
			switch scenario {
			case "expired":
				control.now = func() int64 { return result.Admission.Lease.LeaseUntil }
			case "removed_resource":
				_, err = control.db.Exec("DELETE FROM action_resource_leases WHERE action_id=?", result.Admission.Record.ID)
			case "changed_request":
				request.ExpectedIndex++
			case "missing_action":
				_, err = control.db.Exec("DELETE FROM action_journal WHERE id=?", result.Admission.Record.ID)
			}
			if err != nil {
				t.Fatal(err)
			}
			before := countControlEvents(t, control)
			_, replayErr := control.ClaimAgentRunExecution(request)
			if replayErr == nil || countControlEvents(t, control) != before {
				t.Fatalf("damaged or expired replay returned another execution: %v", replayErr)
			}
			if scenario == "expired" && !errors.Is(replayErr, ErrActionLeaseExpired) {
				t.Fatalf("action expiry was hidden by another boundary: %v", replayErr)
			}
		})
	}
}

func TestAgentRunExecutionScopeLossDeniesGenericAdmissionAndRenewal(t *testing.T) {
	for _, scenario := range []string{"permission", "missing_run", "corrupt_run", "metadata_epoch", "action_owner"} {
		for _, method := range []string{"admit", "renew"} {
			t.Run(scenario+"/"+method, func(t *testing.T) {
				control := agentExecutionControl(t, true)
				request := agentExecutionRequest(t, control)
				request.LeaseSeconds = 10
				result, err := control.ClaimAgentRunExecution(request)
				if err != nil {
					t.Fatal(err)
				}
				claim := result.Admission.Lease
				switch scenario {
				case "permission":
					control.authorizeCutover = false
				case "missing_run":
					_, err = control.db.Exec("DELETE FROM agent_runs WHERE id=?", request.RunID)
				case "corrupt_run":
					_, err = control.db.Exec("UPDATE agent_runs SET record_digest=? WHERE id=?", strings.Repeat("f", 64), request.RunID)
				case "metadata_epoch":
					_, err = control.db.Exec("UPDATE control_cutover SET epoch=epoch+1 WHERE domain='agent_run'")
				case "action_owner":
					_, err = control.db.Exec("UPDATE control_cutover SET state=? WHERE domain='action'", CutoverShadow)
				}
				if err != nil {
					t.Fatal(err)
				}
				before := countControlEvents(t, control)
				if method == "admit" {
					control.now = func() int64 { return 1011 }
					_, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.ActionID, Owner: "successor"})
				} else {
					_, err = control.RenewActionLease(ActionLeaseRenewal{ActionID: claim.ActionID, Epoch: claim.Epoch,
						Owner: claim.Owner, ClaimToken: claim.ClaimToken})
				}
				if err == nil || countControlEvents(t, control) != before {
					t.Fatalf("generic %s ignored lost %s scope: %v", method, scenario, err)
				}
				resources, resourceErr := control.GetResourceLeases(claim.ActionID)
				if resourceErr != nil || len(resources) != 1 {
					t.Fatalf("scope loss discarded unresolved effect reservations: %+v %v", resources, resourceErr)
				}
			})
		}
	}
}

func TestAgentRunExecutionSQLiteWriteFailuresRollbackBeforeDispatch(t *testing.T) {
	for _, table := range []string{"action_journal", "control_events", "action_lease_events", "control_operator_mutations"} {
		t.Run(table, func(t *testing.T) {
			control := agentExecutionControl(t, true)
			request := agentExecutionRequest(t, control)
			before := countControlEvents(t, control)
			// A TEMP trigger injects a real SQLite write failure without modifying
			// the persisted schema or disabling any store integrity check.
			statement := "CREATE TEMP TRIGGER agent_execution_write_fault BEFORE INSERT ON " + table +
				" BEGIN SELECT RAISE(ABORT,'injected-agent-write-failure'); END"
			if _, err := control.db.Exec(statement); err != nil {
				t.Fatal(err)
			}
			if _, err := control.ClaimAgentRunExecution(request); err == nil || !strings.Contains(err.Error(), "injected-agent-write-failure") {
				t.Fatalf("SQLite failure did not refuse the claim: %v", err)
			}
			if countControlEvents(t, control) != before {
				t.Fatal("partial execution became durable before dispatch")
			}
			if _, err := control.db.Exec("DROP TRIGGER temp.agent_execution_write_fault"); err != nil {
				t.Fatal(err)
			}
			result, err := control.ClaimAgentRunExecution(request)
			if err != nil || result.Mutation.Status != OperatorMutationApplied || result.Admission.Lease.Epoch != 1 {
				t.Fatalf("rolled-back local claim left a replay or takeover: %+v %v", result, err)
			}
		})
	}
}

func TestAgentRunExecutionDefaultLeaseSnapshotsCallerAndReplays(t *testing.T) {
	control := agentExecutionControl(t, true)
	request := agentExecutionRequest(t, control)
	request.LeaseSeconds, request.Owner = 0, "  rust-worker\t"
	originalFence := request.Fence
	first, err := control.ClaimAgentRunExecution(request)
	if err != nil || first.Admission.Lease.LeaseUntil != 1060 || first.Admission.Lease.Owner != "rust-worker" {
		t.Fatalf("default bounded lease was not admitted: %+v %v", first, err)
	}
	if request.LeaseSeconds != 0 || request.Owner != "  rust-worker\t" || request.Fence != originalFence || originalFence.ActionId != request.RunID {
		t.Fatal("admission changed the caller's request")
	}
	replay, err := control.ClaimAgentRunExecution(request)
	if err != nil || replay.Mutation.Status != OperatorMutationAlreadyApplied || replay.Admission.Lease != first.Admission.Lease {
		t.Fatalf("default lease normalization changed replay identity: %+v %v", replay, err)
	}
}

func TestAgentRunExecutionRefusesUnconvertedMetadata(t *testing.T) {
	control := agentExecutionControl(t, true)
	request := agentExecutionRequest(t, control)
	request.RunID, request.Fence.ActionId = "unconverted-run", "unconverted-run"
	if _, err := control.ApplyOperatorMutation(OperatorMutation{OperationID: "unconverted-record-fixture",
		ActionID: request.RunID, Actor: "operator:legacy-fixture", Record: Record{Domain: "agent_run", ID: request.RunID, Revision: 1,
			ExecutionEpoch: request.Fence.ExecutionEpoch, State: "created", Payload: json.RawMessage(`{}`)}}); err != nil {
		t.Fatal(err)
	}
	before := countControlEvents(t, control)
	if _, err := control.ClaimAgentRunExecution(request); !errors.Is(err, ErrAgentRunMetadataInvalid) || countControlEvents(t, control) != before {
		t.Fatalf("unconverted record became execution authority: %v", err)
	}
}

func TestAgentRunExecutionReplayJournalCannotRebindScopeOrEpoch(t *testing.T) {
	control := agentExecutionControl(t, true)
	request := agentExecutionRequest(t, control)
	claim, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	// Exercise the journal-consumer boundary under its real transaction lock.
	// Faults alter the loaded reply/request, never disable journal immutability.
	control.mu.Lock()
	defer control.mu.Unlock()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, scenario := range []string{"domain", "run", "action", "cursor", "phase", "owner", "epoch"} {
		t.Run(scenario, func(t *testing.T) {
			input, receipt := request, claim.Mutation
			expected := ErrCorruptRecord
			switch scenario {
			case "domain":
				receipt.Domain = "policy"
			case "run":
				receipt.RecordID = "another-run"
			case "action":
				receipt.ActionID = "another-action"
			case "cursor":
				input.ExpectedIndex++
			case "phase":
				input.Phase = "plan"
			case "owner":
				input.Owner = "another-worker"
				expected = ErrActionLeaseStale
			case "epoch":
				receipt.ExecutionEpoch++
				expected = ErrActionLeaseStale
			}
			if _, err := control.replayAgentExecutionTx(tx, input, receipt, 1000); !errors.Is(err, expected) {
				t.Fatalf("loaded replay %s rebound authority: %v", scenario, err)
			}
		})
	}
}

func TestAgentRunExecutionBindingRefusesMalformedPersistedScope(t *testing.T) {
	control := agentExecutionControl(t, true)
	result, err := control.ClaimAgentRunExecution(agentExecutionRequest(t, control))
	if err != nil {
		t.Fatal(err)
	}
	good := result.Admission.Record
	valid, err := AgentRunExecutionBindingFromRecord(good)
	if err != nil || valid.RunID != "run_events" || valid.Phase != "tasks" {
		t.Fatalf("durable execution binding could not be resolved: %+v %v", valid, err)
	}
	for name, mutate := range map[string]func(*Record){
		"invalid_json":   func(r *Record) { r.Payload = json.RawMessage(`{`) },
		"missing_marker": func(r *Record) { r.Payload = json.RawMessage(`{}`) },
		"wrong_domain":   func(r *Record) { r.Domain = "agent_run" },
		"wrong_action":   func(r *Record) { r.ID = "different-action" },
		"unknown_scope_field": func(r *Record) {
			r.Payload = json.RawMessage(strings.Replace(string(r.Payload), `"schemaVersion":1`, `"schemaVersion":1,"workerCanAdvanceEpoch":true`, 1))
		},
	} {
		t.Run(name, func(t *testing.T) {
			invalid := good
			mutate(&invalid)
			if _, err := AgentRunExecutionBindingFromRecord(invalid); !errors.Is(err, ErrAgentRunExecutionRequired) {
				t.Fatalf("malformed persisted scope admitted: %v", err)
			}
		})
	}
}
