package agent

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"strings"
	"testing"
)

// These assertions pin the Go oracle the gateway admission decision and the
// Python reference implementation both mirror. They intentionally exercise
// every refusal branch: a silently admitted effect is the failure this file
// exists to prevent.

func successorInput() SuccessorInput {
	return SuccessorInput{
		IdentityValid:     true,
		Lease:             "live",
		Epoch:             "match",
		Effect:            "CONFIRMED",
		ResetAdmitted:     true,
		ResetFenceMatches: true,
		OperationID:       "agent-node-original",
		ActionID:          "run-1",
		ExecutionEpoch:    3,
		NodeID:            "coder",
		ResetIndex:        7,
	}
}

func TestSuccessorGenerationAdmitsOnlyASettledSameFenceReset(t *testing.T) {
	input := successorInput()
	decision := SuccessorGeneration(input)
	if !decision.Admitted || decision.Dispatch != "successor" || decision.Replayed {
		t.Fatalf("legal reset refused: %+v", decision)
	}
	if decision.RetainedOperationID != input.OperationID {
		t.Fatal("successor replaced the original operation id")
	}
	want := SuccessorOperationID(input.OperationID, input.ActionID, input.ExecutionEpoch, input.NodeID, input.ResetIndex)
	if decision.SuccessorOperationID != want {
		t.Fatalf("successor id mismatch: %q != %q", decision.SuccessorOperationID, want)
	}
	if !strings.HasPrefix(want, "agent-successor-") || len(want) != len("agent-successor-")+64 {
		t.Fatalf("successor id shape is wrong: %q", want)
	}

	replay := input
	replay.ExistingSuccessor = want
	if again := SuccessorGeneration(replay); !again.Admitted || !again.Replayed {
		t.Fatalf("identical successor did not replay: %+v", again)
	}

	for name, mutate := range map[string]func(*SuccessorInput){
		"identity":          func(in *SuccessorInput) { in.IdentityValid = false },
		"lease":             func(in *SuccessorInput) { in.Lease = "expired" },
		"stale-epoch":       func(in *SuccessorInput) { in.Epoch = "stale" },
		"mismatch-epoch":    func(in *SuccessorInput) { in.Epoch = "mismatch" },
		"awaiting-tools":    func(in *SuccessorInput) { in.Effect = "AWAITING_TOOLS" },
		"unknown-effect":    func(in *SuccessorInput) { in.Effect = "EFFECT_UNKNOWN" },
		"unsettled-reset":   func(in *SuccessorInput) { in.ResetAdmitted = false },
		"foreign-fence":     func(in *SuccessorInput) { in.ResetFenceMatches = false },
		"empty-operation":   func(in *SuccessorInput) { in.OperationID = "" },
		"newline-operation": func(in *SuccessorInput) { in.OperationID = "bad\nid" },
		"long-node":         func(in *SuccessorInput) { in.NodeID = strings.Repeat("a", 65) },
		"zero-epoch":        func(in *SuccessorInput) { in.ExecutionEpoch = 0 },
		"negative-index":    func(in *SuccessorInput) { in.ResetIndex = -1 },
		"foreign-successor": func(in *SuccessorInput) { in.ExistingSuccessor = "agent-successor-" + strings.Repeat("f", 64) },
	} {
		t.Run(name, func(t *testing.T) {
			changed := successorInput()
			mutate(&changed)
			refused := SuccessorGeneration(changed)
			if refused.Admitted || refused.SuccessorOperationID != "" {
				t.Fatalf("illegal reset admitted: %+v", refused)
			}
			if refused.Code == "" || refused.Message == "" || refused.Dispatch != "none" {
				t.Fatalf("refusal is not typed: %+v", refused)
			}
			if refused.RetainedOperationID != changed.OperationID && refused.RetainedOperationID != "" {
				t.Fatal("refusal dropped the original operation id")
			}
		})
	}
}

func TestSuccessorOperationIDNeverCollidesWithTheOperationItReplaces(t *testing.T) {
	// The minted id must differ from the operation it replaces, otherwise a
	// successor would silently reuse the settled effect. The prefix guarantees
	// this, so assert the invariant instead of a branch that cannot be reached.
	original := successorInput().OperationID
	for _, actionID := range []string{"run-1", "run-2"} {
		for _, epoch := range []uint64{1, 3, 99} {
			minted := SuccessorOperationID(original, actionID, epoch, "coder", 7)
			if minted == original {
				t.Fatalf("successor collided with the original: %q", minted)
			}
			if !strings.HasPrefix(minted, "agent-successor-") {
				t.Fatalf("successor is not namespaced: %q", minted)
			}
		}
	}
	// Distinct reset material must not mint the same successor.
	a := SuccessorOperationID(original, "run-1", 3, "coder", 7)
	b := SuccessorOperationID(original, "run-1", 3, "coder", 8)
	c := SuccessorOperationID(original, "run-1", 4, "coder", 7)
	if a == b || a == c || b == c {
		t.Fatal("distinct reset material minted a shared successor")
	}
}

// The golden vectors are the cross-language contract.
//
// Go, Rust and the Python oracle each hash the same material independently. A
// change to the separator, the field order or the hex casing in any one of them
// would silently produce ids the other two never mint, so the value itself is
// pinned here and in `rust/crates/deepseek-policy/tests/agent_runtime_successor.rs`.
func TestSuccessorOperationIDMatchesTheCrossLanguageGoldenVectors(t *testing.T) {
	old := "agent-node-" + strings.Repeat("ab", 32)
	vectors := []struct {
		label string
		got   string
		want  string
	}{
		{
			"baseline",
			SuccessorOperationID(old, "action-1", 7, "coder", 4),
			"agent-successor-1c2e2a7fa327557053b894374bbd0972955fd5194ea704ee4766e872bdc6941e",
		},
		{
			"later-epoch",
			SuccessorOperationID(old, "action-1", 8, "coder", 4),
			"agent-successor-6f9a8ec251f4282b5f6a392b53e4f4d84da5ff1abcef26bc3760b4c75d37c365",
		},
		{
			"other-node",
			SuccessorOperationID(old, "action-1", 7, "critic", 4),
			"agent-successor-e354a150e0826ae98e8ac89427e773d164bcf57cb79761846decc54d45a63fe7",
		},
	}
	for _, row := range vectors {
		if row.got != row.want {
			t.Fatalf("%s drifted from the shared material: got %q, want %q", row.label, row.got, row.want)
		}
	}
}

func TestSuccessorBindingKeepsTheOriginalBesideTheMintedID(t *testing.T) {
	input := successorInput()
	bound := SuccessorBinding(input)
	if !bound.Persisted || bound.ReplacedOperationID {
		t.Fatalf("binding replaced the original operation: %+v", bound)
	}
	want := SuccessorOperationID(input.OperationID, input.ActionID, input.ExecutionEpoch, input.NodeID, input.ResetIndex)
	if bound.OperationID != input.OperationID || bound.SuccessorOperationID != want || bound.ResetIndex != input.ResetIndex {
		t.Fatalf("binding is not beside the original: %+v", bound)
	}

	refused := successorInput()
	refused.Effect = "EFFECT_UNKNOWN"
	refused.ExistingSuccessor = "agent-successor-" + strings.Repeat("a", 64)
	held := SuccessorBinding(refused)
	if held.Persisted {
		t.Fatal("refused generation persisted a successor")
	}
	if held.OperationID != refused.OperationID {
		t.Fatal("refusal replaced the stored operation id")
	}
	if held.SuccessorOperationID != refused.ExistingSuccessor {
		t.Fatal("refusal discarded an already stored successor")
	}
}

func executionInput() SuccessorExecutionInput {
	input := successorInput()
	return SuccessorExecutionInput{
		IdentityValid:     input.IdentityValid,
		Lease:             input.Lease,
		Epoch:             input.Epoch,
		Effect:            input.Effect,
		ResetAdmitted:     input.ResetAdmitted,
		ResetFenceMatches: input.ResetFenceMatches,
		OperationID:       input.OperationID,
		ActionID:          input.ActionID,
		ExecutionEpoch:    input.ExecutionEpoch,
		NodeID:            input.NodeID,
		ResetIndex:        input.ResetIndex,
		StoredSuccessor:   SuccessorOperationID(input.OperationID, input.ActionID, input.ExecutionEpoch, input.NodeID, input.ResetIndex),
		StoredOperationID: input.OperationID,
		AuthorityIssued:   true,
		AuthorityEpoch:    5,
		PresentedEpoch:    5,
	}
}

// executionRefusalCode pins the exact code for each refusal branch. A branch
// that starts returning a different code is a contract change: a controller
// branches on these strings, so "some refusal" is not enough.
var executionRefusalCode = map[string]string{
	"worker-promoted":  "FENCE_MISMATCH",
	"identity":         "INVALID_IDENTITY",
	"lease":            "LEASE_NOT_HELD",
	"stale":            "STALE_EXECUTION_EPOCH",
	"presented-older":  "STALE_EXECUTION_EPOCH",
	"mismatch":         "FENCE_MISMATCH",
	"no-authority":     "FENCE_MISMATCH",
	"presented-newer":  "FENCE_MISMATCH",
	"awaiting-tools":   "AGENT_DRIVER_TOOLS_PENDING",
	"unknown-effect":   "EFFECT_UNKNOWN",
	"unsettled-reset":  "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
	"foreign-fence":    "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
	"not-issued":       "FENCE_MISMATCH",
	"empty-operation":  "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
	"negative-index":   "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
	"foreign-original": "AGENT_RUNTIME_GENERATION_CONFLICT",
	"same-epoch":       "AGENT_RUNTIME_SUCCESSOR_HELD",
	"no-successor":     "AGENT_RUNTIME_SUCCESSOR_HELD",
	"successor-is-old": "AGENT_RUNTIME_SUCCESSOR_HELD",
}

func TestSuccessorExecutionRequiresAnIssuedNewerEpoch(t *testing.T) {
	input := executionInput()
	decision := SuccessorExecution(input)
	if !decision.Admitted || !decision.Execute || decision.Promoted {
		t.Fatalf("legal successor execution refused: %+v", decision)
	}
	if decision.OperationID != input.StoredSuccessor || decision.ExecutionEpoch != input.AuthorityEpoch {
		t.Fatalf("execution did not bind the stored successor: %+v", decision)
	}
	if decision.RetainedOperationID != input.OperationID || decision.RetainedEpoch != input.ExecutionEpoch {
		t.Fatal("execution lost the retained original")
	}

	for name, mutate := range map[string]func(*SuccessorExecutionInput){
		"worker-promoted":  func(in *SuccessorExecutionInput) { in.WorkerPromoted = true },
		"identity":         func(in *SuccessorExecutionInput) { in.IdentityValid = false },
		"lease":            func(in *SuccessorExecutionInput) { in.Lease = "expired" },
		"stale":            func(in *SuccessorExecutionInput) { in.Epoch = "stale" },
		"presented-older":  func(in *SuccessorExecutionInput) { in.PresentedEpoch = 4 },
		"mismatch":         func(in *SuccessorExecutionInput) { in.Epoch = "mismatch" },
		"no-authority":     func(in *SuccessorExecutionInput) { in.AuthorityEpoch = 0; in.PresentedEpoch = 0 },
		"presented-newer":  func(in *SuccessorExecutionInput) { in.PresentedEpoch = 6 },
		"awaiting-tools":   func(in *SuccessorExecutionInput) { in.Effect = "AWAITING_TOOLS" },
		"unknown-effect":   func(in *SuccessorExecutionInput) { in.Effect = "EFFECT_UNKNOWN" },
		"unsettled-reset":  func(in *SuccessorExecutionInput) { in.ResetAdmitted = false },
		"foreign-fence":    func(in *SuccessorExecutionInput) { in.ResetFenceMatches = false },
		"not-issued":       func(in *SuccessorExecutionInput) { in.AuthorityIssued = false },
		"empty-operation":  func(in *SuccessorExecutionInput) { in.OperationID = "" },
		"negative-index":   func(in *SuccessorExecutionInput) { in.ResetIndex = -1 },
		"foreign-original": func(in *SuccessorExecutionInput) { in.StoredOperationID = "someone-else" },
		"same-epoch": func(in *SuccessorExecutionInput) {
			in.AuthorityEpoch = in.ExecutionEpoch
			in.PresentedEpoch = in.ExecutionEpoch
		},
		"no-successor":     func(in *SuccessorExecutionInput) { in.StoredSuccessor = "" },
		"successor-is-old": func(in *SuccessorExecutionInput) { in.StoredSuccessor = in.OperationID },
	} {
		t.Run(name, func(t *testing.T) {
			changed := executionInput()
			mutate(&changed)
			refused := SuccessorExecution(changed)
			if refused.Admitted || refused.Execute || refused.OperationID != "" {
				t.Fatalf("illegal execution admitted: %+v", refused)
			}
			if refused.Code == "" || refused.Message == "" {
				t.Fatalf("refusal is not typed: %+v", refused)
			}
			if want, pinned := executionRefusalCode[name]; pinned && refused.Code != want {
				t.Fatalf("%s: got code %q, want %q", name, refused.Code, want)
			}
		})
	}
}

func TestSuccessorExecutionHoldsWhileTheFenceIsUnchanged(t *testing.T) {
	input := executionInput()
	input.AuthorityEpoch = input.ExecutionEpoch
	input.PresentedEpoch = input.ExecutionEpoch
	decision := SuccessorExecution(input)
	if decision.Code != "AGENT_RUNTIME_SUCCESSOR_HELD" {
		t.Fatalf("same-fence successor was not held: %+v", decision)
	}
}

func TestDispatchAllowedMatchesTheSharedRefusalTable(t *testing.T) {
	allowed := []string{"CONFIRMED", "SETTLED_FAILURE", "NONE"}
	for _, effect := range allowed {
		if code, ok := DispatchAllowed("live", "match", effect); !ok || code != "" {
			t.Fatalf("%s should dispatch: %q %v", effect, code, ok)
		}
	}
	for _, row := range []struct{ lease, epoch, effect, code string }{
		{"expired", "match", "CONFIRMED", "LEASE_NOT_HELD"},
		{"live", "stale", "CONFIRMED", "STALE_EXECUTION_EPOCH"},
		{"live", "mismatch", "NONE", "FENCE_MISMATCH"},
		{"live", "match", "EFFECT_UNKNOWN", "EFFECT_UNKNOWN"},
		{"live", "match", "", "EFFECT_UNKNOWN"},
		{"live", "match", "AWAITING_TOOLS", "AGENT_DRIVER_TOOLS_PENDING"},
		{"live", "match", "DISPATCHING", "EFFECT_UNKNOWN"},
	} {
		code, ok := DispatchAllowed(row.lease, row.epoch, row.effect)
		if ok || code != row.code {
			t.Fatalf("%v: got %q %v, want %q", row, code, ok, row.code)
		}
	}
}

func ref(shas ...string) ArtifactRef {
	text := strings.Repeat("a", 64)
	if len(shas) > 0 {
		text = shas[0]
	}
	return ArtifactRef{SHA256: text, Length: 10}
}

func TestRetainedDriverToolResultsServesOnlyStoredResultsForTheSameWait(t *testing.T) {
	public := ref()
	if got := RetainedDriverToolResults("CONFIRMED", public, nil); got.Returned || got.Code != "" {
		t.Fatalf("a settled effect must not be served tools: %+v", got)
	}
	pending := RetainedDriverToolResults("AWAITING_TOOLS", ArtifactRef{}, nil)
	if pending.Returned || pending.Code != "AGENT_DRIVER_TOOLS_PENDING" {
		t.Fatalf("a malformed request marker must stay pending: %+v", pending)
	}

	request := RunEvent{Type: "agent_tool_request", Phase: "coder", Scope: retainedToolResultScope, Body: public}
	result := RunEvent{Type: "agent_tool_result", Phase: "coder", Scope: "call-1", Body: ref(strings.Repeat("b", 64))}
	served := RetainedDriverToolResults("AWAITING_TOOLS", public, []RunEvent{request, result, result})
	if !served.Returned || len(served.Events) != 1 {
		t.Fatalf("stored tool result was not served once: %+v", served)
	}

	// A marker naming different bytes does not authorize the request.
	foreign := request
	foreign.Body = ref(strings.Repeat("c", 64))
	if got := RetainedDriverToolResults("AWAITING_TOOLS", public, []RunEvent{foreign, result}); got.Returned {
		t.Fatal("a marker for other bytes served the wait")
	}
	// A marker without a phase cannot identify the waiting node.
	phaseless := request
	phaseless.Phase = ""
	if got := RetainedDriverToolResults("AWAITING_TOOLS", public, []RunEvent{phaseless, result}); got.Returned {
		t.Fatal("a phaseless marker served the wait")
	}
	// A result without a body reference is not usable custody.
	bodiless := result
	bodiless.Body = ArtifactRef{}
	if got := RetainedDriverToolResults("AWAITING_TOOLS", public, []RunEvent{request, bodiless}); got.Returned {
		t.Fatal("a bodiless result served the wait")
	}
	// The lowercase wire spelling is accepted too.
	lower := RetainedDriverToolResults("awaiting_tools", public, []RunEvent{request, result})
	if !lower.Returned {
		t.Fatal("lowercase awaiting state was refused")
	}
}

func TestProjectJournalObservationNeverInventsAnUnappliedState(t *testing.T) {
	settled := ProjectJournalObservation("coder", true, "CONFIRMED", nil)
	if settled.Append || settled.Kind != "settled" || settled.Status != "CONFIRMED" || settled.Scope != "" {
		t.Fatalf("a settled read must not advance the cursor: %+v", settled)
	}
	if len(settled.SHA256) != 64 || settled.Length == 0 {
		t.Fatal("settled observation has no digest")
	}

	// The document digest is the cross-language contract: Rust, Go and the
	// Python oracle each build the same bytes and the artifact store hashes
	// them. A change to the template or the field order in one implementation
	// would produce a different event body under the same public phase, so the
	// digest and length are pinned here, in
	// rust/crates/deepseek-policy/tests/agent_runtime_successor.rs and in
	// tests/test_native_agent_runtime_admission.py.
	golden := []struct {
		label  string
		phase  string
		status string
		scope  string
		sha256 string
		length uint64
	}{
		{
			"state",
			"coder", "CONFIRMED", "",
			"bc5e0883cfe0f23deeaa8fa27c80a0160df5c3cf4dc63b0ef7d662d517c4419d", 79,
		},
		{
			"observation",
			"coder", "", "timeout",
			"3ecca5d30e026d59120c68cef043ba414cc6d5584b25c983d971cfffe7ae7efa", 80,
		},
	}
	for _, row := range golden {
		raw, ok := nodeEffectBytes(row.phase, row.status, row.scope)
		if !ok {
			t.Fatalf("%s document was refused", row.label)
		}
		digest := fmt.Sprintf("%x", sha256.Sum256(raw))
		if digest != row.sha256 || uint64(len(raw)) != row.length {
			t.Fatalf("%s document drifted: sha256=%s length=%d, want %s/%d",
				row.label, digest, len(raw), row.sha256, row.length)
		}
	}

	for name, row := range map[string]struct {
		found bool
		state string
		err   error
		kind  string
		scope string
	}{
		"missing":      {false, "", nil, "unreadable", "missing"},
		"deadline":     {true, "", context.DeadlineExceeded, "unreadable", "timeout"},
		"cancelled":    {true, "", context.Canceled, "unreadable", "timeout"},
		"message":      {true, "", errors.New("rpc timeout while reading"), "unreadable", "timeout"},
		"absent-log":   {true, "", errors.New("ABSENT_LOG"), "unreadable", "absent_log"},
		"not-applied":  {true, "NOT_APPLIED", nil, "unreadable", "not_applied"},
		"awaiting":     {true, "AWAITING_TOOLS", nil, "awaiting", ""},
		"unknown":      {true, "EFFECT_UNKNOWN", nil, "unknown", ""},
		"dispatching":  {true, "DISPATCHING", nil, "unknown", ""},
		"empty-state":  {true, "", nil, "unknown", ""},
		"unsafe-state": {true, "bad state", nil, "unreadable", "unavailable"},
	} {
		t.Run(name, func(t *testing.T) {
			observation := ProjectJournalObservation("coder", row.found, row.state, row.err)
			if !observation.Append {
				t.Fatal("an unresolved read must be observable")
			}
			if observation.Kind != row.kind {
				t.Fatalf("kind %q != %q", observation.Kind, row.kind)
			}
			if row.scope != "" && observation.Scope != row.scope {
				t.Fatalf("scope %q != %q", observation.Scope, row.scope)
			}
			if observation.Status == "NOT_APPLIED" {
				t.Fatal("a missing row must not be published as NOT_APPLIED")
			}
		})
	}

	// Non-canonical node ids are not projected onto the public stream.
	unreadable := ProjectJournalObservation("bad node", true, "CONFIRMED", nil)
	if unreadable.Kind != "unreadable" || unreadable.Scope != "unavailable" || unreadable.Phase != "" {
		t.Fatalf("non-canonical node id was projected: %+v", unreadable)
	}
	// A settled read with a capitalized state keeps the canonical status.
	for _, state := range []string{"confirmed", "settled_failure", "none"} {
		observation := ProjectJournalObservation("coder", true, state, nil)
		if observation.Append || observation.Kind != "settled" {
			t.Fatalf("%s was not treated as settled: %+v", state, observation)
		}
		if observation.Status == "" || observation.Status == state {
			t.Fatalf("%s was not canonicalized: %+v", state, observation)
		}
	}
	// The recursive unreadable document is stable when the node id is empty.
	if empty := ProjectJournalObservation("", true, "CONFIRMED", nil); empty.Kind != "unreadable" || empty.Scope != "unavailable" {
		t.Fatalf("empty node id produced %+v", empty)
	}
}

func TestJournalHelpersKeepPublicTokensSafe(t *testing.T) {
	if !safeJournalToken("coder.1:task-2") {
		t.Fatal("a legal token was refused")
	}
	for _, bad := range []string{strings.Repeat("a", 65), "bad token", "café", "quote\"", ""} {
		if bad == "" {
			continue
		}
		if safeJournalToken(bad) {
			t.Fatalf("%q was accepted as a public token", bad)
		}
	}
	if !journalObservationScope("timeout") || !journalObservationScope("not_applied") {
		t.Fatal("a known observation scope was refused")
	}
	if journalObservationScope("CONFIRMED") || journalObservationScope("") {
		t.Fatal("a state was accepted as an observation scope")
	}
	if got := applyJournalTemplate("%s/%s/%s", "a", "b", "c"); got != "a/b/c" {
		t.Fatalf("template substitution is wrong: %q", got)
	}
	if got := applyJournalTemplate("no slots", "a"); got != "no slots" {
		t.Fatalf("template without slots changed: %q", got)
	}
	if raw, ok := nodeEffectBytes("coder", "", "timeout"); !ok || !strings.Contains(string(raw), `"observation":"timeout"`) {
		t.Fatalf("observation document is wrong: %s", raw)
	}
	if raw, ok := nodeEffectBytes("coder", "CONFIRMED", ""); !ok || !strings.Contains(string(raw), `"state":"CONFIRMED"`) {
		t.Fatalf("state document is wrong: %s", raw)
	}
	if _, ok := nodeEffectBytes("bad node", "CONFIRMED", ""); ok {
		t.Fatal("an unsafe phase produced a document")
	}
	if !canonicalNodeID("coder") || canonicalNodeID("") || canonicalNodeID("bad node") {
		t.Fatal("canonical node id check is wrong")
	}
}

func TestJournalQueryObservationClassifiesTransportFailures(t *testing.T) {
	for name, row := range map[string]struct {
		err   error
		scope string
	}{
		"nil":          {nil, ""},
		"deadline":     {context.DeadlineExceeded, "timeout"},
		"canceled":     {context.Canceled, "timeout"},
		"literal":      {errors.New("timeout"), "timeout"},
		"upper-text":   {errors.New("Deadline Exceeded"), "timeout"},
		"cancelled-uk": {errors.New("context cancelled"), "timeout"},
		"absent":       {errors.New("worker ABSENT_LOG"), "absent_log"},
		"absent-lower": {errors.New("absent_log in worker"), "absent_log"},
		"other":        {errors.New("permission denied"), "unavailable"},
	} {
		t.Run(name, func(t *testing.T) {
			scope, failed := journalQueryObservation(row.err)
			if row.scope == "" {
				if failed || scope != "" {
					t.Fatalf("nil error reported %q %v", scope, failed)
				}
				return
			}
			if !failed || scope != row.scope {
				t.Fatalf("got %q %v, want %q", scope, failed, row.scope)
			}
		})
	}
}

func TestDispatchAllowedRefusesAnUnknownEffectInsteadOfRetryingIt(t *testing.T) {
	// EFFECT_UNKNOWN is not a missing effect; retrying it would duplicate an
	// upstream side effect that may already have happened.
	for _, effect := range []string{"EFFECT_UNKNOWN", "UNKNOWN", "PROVIDER_5XX"} {
		if code, ok := DispatchAllowed("live", "match", effect); ok || code != "EFFECT_UNKNOWN" {
			t.Fatalf("%s was retried: %q %v", effect, code, ok)
		}
	}
}

func TestJournalObservationDocumentIsStableOnUnsafeTokens(t *testing.T) {
	// An unsafe token degrades to the canonical unreadable document rather than
	// publishing text the event stream could not re-parse.
	degraded := journalObservationDocument("bad node", "", "", true, "unknown")
	if degraded.Kind != "unreadable" || degraded.Scope != "unavailable" || !degraded.Append {
		t.Fatalf("unsafe token did not degrade: %+v", degraded)
	}
	// The degraded document is itself canonical, so re-deriving it is stable and
	// the recursive fallback cannot loop.
	again := journalObservationDocument("", "", "unavailable", true, "unreadable")
	if again.Kind != "unreadable" || again.Scope != "unavailable" {
		t.Fatalf("canonical unreadable input produced %+v", again)
	}
	// A status that is not a scope is published as a status, not a scope.
	stateful := journalObservationDocument("coder", "DISPATCHING", "", true, "unknown")
	if stateful.Status != "DISPATCHING" || stateful.Scope != "" || !stateful.Append {
		t.Fatalf("state document is wrong: %+v", stateful)
	}
	if len(stateful.SHA256) != 64 || stateful.Length == 0 {
		t.Fatal("state document has no digest")
	}
}

func TestDriverBodyRefRejectsNonCanonicalDigests(t *testing.T) {
	if !driverBodyRef(ref()) {
		t.Fatal("a canonical reference was refused")
	}
	for name, bad := range map[string]ArtifactRef{
		"short":     {SHA256: "abc", Length: 1},
		"empty":     {},
		"uppercase": {SHA256: strings.Repeat("A", 64), Length: 1},
		"non-hex":   {SHA256: strings.Repeat("z", 64), Length: 1},
		"zero-size": {SHA256: strings.Repeat("a", 64), Length: 0},
	} {
		if driverBodyRef(bad) {
			t.Fatalf("%s digest was accepted", name)
		}
	}
}


