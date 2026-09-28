package store

import (
	"errors"
	"strings"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

func dualEvaluate(t *testing.T, control *Control, domain string) CutoverRecord {
	t.Helper()
	current, err := control.GetCutover(domain)
	if err != nil {
		t.Fatal(err)
	}
	dual, err := control.TransitionCutover(CutoverTransition{
		Domain:           domain,
		To:               CutoverDualEvaluate,
		ExpectedRevision: current.Revision,
		ExpectedEpoch:    current.Epoch,
		FencingToken:     current.FencingToken,
		TransferID:       domain + "-dual",
	})
	if err != nil {
		t.Fatal(err)
	}
	return dual
}

// The default deployment must stay mechanically unable to promote a control
// domain even when a caller presents a well-formed authority checkpoint.
func TestCutoverPromotionRequiresAnAuthorizedDeployment(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	dual := dualEvaluate(t, control, "policy")
	if _, err := control.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision,
		ExpectedEpoch:    dual.Epoch,
		FencingToken:     dual.FencingToken,
		TransferID:       "policy-promote",
		Authority:        frozenCheckpoint(t, 0),
	}); err != ErrCutoverNotAuthorized {
		t.Fatalf("shadow deployment promoted a domain: %v", err)
	}
	got, err := control.GetCutover("policy")
	if err != nil || got != dual {
		t.Fatalf("refused promotion changed state: %+v %v", got, err)
	}
	if count := authorizationRowCount(t, control); count != 0 {
		t.Fatalf("refused promotion journaled %d authorizations", count)
	}
}

func TestCutoverPromotionRequiresAPresentedAndInstalledAuthority(t *testing.T) {
	t.Run("no claim presented", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, advanced, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err != nil || !advanced {
			t.Fatalf("claim: advanced=%v %v", advanced, err)
		}
		dual := dualEvaluate(t, control, "policy")
		if _, err := control.TransitionCutover(CutoverTransition{
			Domain:           "policy",
			To:               CutoverGoAuthoritative,
			ExpectedRevision: dual.Revision,
			ExpectedEpoch:    dual.Epoch,
			FencingToken:     dual.FencingToken,
			TransferID:       "policy-promote",
		}); err != ErrCutoverNotAuthorized {
			t.Fatalf("promotion without a claim: %v", err)
		}
	})
	t.Run("no claim installed", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		dual := dualEvaluate(t, control, "policy")
		if _, err := control.TransitionCutover(CutoverTransition{
			Domain:           "policy",
			To:               CutoverGoAuthoritative,
			ExpectedRevision: dual.Revision,
			ExpectedEpoch:    dual.Epoch,
			FencingToken:     dual.FencingToken,
			TransferID:       "policy-promote",
			Authority:        frozenCheckpoint(t, 0),
		}); !errors.Is(err, ErrCutoverAuthorityStale) {
			t.Fatalf("promotion with an uninstalled authority: %v", err)
		}
		got, err := control.GetCutover("policy")
		if err != nil || got != dual {
			t.Fatalf("refused promotion changed state: %+v %v", got, err)
		}
		if count := authorizationRowCount(t, control); count != 0 {
			t.Fatalf("refused promotion journaled %d authorizations", count)
		}
	})
}

// The legal path must genuinely succeed through every authoritative state, and
// only there may the epoch and fencing token advance.
func TestCutoverPromotionSucceedsThroughEveryAuthoritativeState(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	authority := frozenCheckpoint(t, 0)
	if _, advanced, err := control.ClaimControlAuthority(authority); err != nil || !advanced {
		t.Fatalf("claim: advanced=%v %v", advanced, err)
	}
	dual := dualEvaluate(t, control, "policy")
	promoted, err := control.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision,
		ExpectedEpoch:    dual.Epoch,
		FencingToken:     dual.FencingToken,
		TransferID:       "policy-promote",
		Authority:        authority,
	})
	if err != nil {
		t.Fatalf("authorized promotion: %v", err)
	}
	if promoted.State != CutoverGoAuthoritative || promoted.Owner != RuntimeGo || promoted.PreviousOwner != OwnerPython ||
		promoted.Revision != dual.Revision+1 || promoted.Epoch != dual.Epoch+1 ||
		promoted.FencingToken != dual.FencingToken+1 || promoted.TransferID != "policy-promote" {
		t.Fatalf("promotion record: %+v", promoted)
	}
	var generation int64
	var digest, fromState, toState string
	if err := control.db.QueryRow(
		`SELECT authority_generation, authority_digest, from_state, to_state
		 FROM control_cutover_authorizations WHERE domain='policy' AND transfer_id='policy-promote'`,
	).Scan(&generation, &digest, &fromState, &toState); err != nil {
		t.Fatal(err)
	}
	if generation != 1 || digest != authority.Digest || fromState != string(CutoverDualEvaluate) || toState != string(CutoverGoAuthoritative) {
		t.Fatalf("authorization journal: gen=%d digest=%s %s->%s", generation, digest, fromState, toState)
	}

	shadowed, err := control.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverPythonShadow,
		ExpectedRevision: promoted.Revision,
		ExpectedEpoch:    promoted.Epoch,
		FencingToken:     promoted.FencingToken,
		TransferID:       "policy-python-shadow",
		Authority:        authority,
	})
	if err != nil {
		t.Fatalf("python_shadow promotion: %v", err)
	}
	disabled, err := control.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverPythonDisabled,
		ExpectedRevision: shadowed.Revision,
		ExpectedEpoch:    shadowed.Epoch,
		FencingToken:     shadowed.FencingToken,
		TransferID:       "policy-python-disabled",
		Authority:        authority,
	})
	if err != nil {
		t.Fatalf("python_disabled promotion: %v", err)
	}
	if disabled.State != CutoverPythonDisabled || disabled.Owner != RuntimeGo || disabled.Epoch != promoted.Epoch+2 {
		t.Fatalf("python_disabled record: %+v", disabled)
	}
	if count := authorizationRowCount(t, control); count != 3 {
		t.Fatalf("authorization journal rows = %d, want 3", count)
	}
	// The authority itself did not advance: promoting a domain is not a claim.
	if head, exists, err := control.ControlAuthorityHead(); err != nil || !exists || head.Generation != 1 {
		t.Fatalf("promotion moved the authority head: %+v exists=%v %v", head, exists, err)
	}
}

func TestCutoverPromotionRefusesAStaleOrForeignAuthority(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	first := frozenCheckpoint(t, 0)
	second := frozenCheckpoint(t, 1)
	if _, advanced, err := control.ClaimControlAuthority(first); err != nil || !advanced {
		t.Fatalf("claim genesis: advanced=%v %v", advanced, err)
	}
	if _, advanced, err := control.ClaimControlAuthority(second); err != nil || !advanced {
		t.Fatalf("claim second: advanced=%v %v", advanced, err)
	}
	dual := dualEvaluate(t, control, "policy")

	fork := *second
	fork.CreatedAt = "2026-09-03T00:00:10Z"
	resealCheckpoint(t, &fork)

	tampered := *second
	tampered.PayloadDigest = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

	tests := []struct {
		name      string
		authority *AuthorityCheckpoint
	}{
		{name: "stale generation", authority: first},
		{name: "fork at the same generation", authority: &fork},
		{name: "tampered payload digest", authority: &tampered},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := control.TransitionCutover(CutoverTransition{
				Domain:           "policy",
				To:               CutoverGoAuthoritative,
				ExpectedRevision: dual.Revision,
				ExpectedEpoch:    dual.Epoch,
				FencingToken:     dual.FencingToken,
				TransferID:       "policy-promote",
				Authority:        test.authority,
			}); !errors.Is(err, ErrCutoverAuthorityStale) {
				t.Fatalf("promotion error = %v, want %v", err, ErrCutoverAuthorityStale)
			}
			got, err := control.GetCutover("policy")
			if err != nil || got != dual {
				t.Fatalf("refused promotion changed state: %+v %v", got, err)
			}
			if count := authorizationRowCount(t, control); count != 0 {
				t.Fatalf("refused promotion journaled %d authorizations", count)
			}
		})
	}
}

// Ownership must always be recoverable: de-promotion never needs authority.
func TestCutoverDePromotionNeedsNoAuthority(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	authority := frozenCheckpoint(t, 0)
	if _, _, err := control.ClaimControlAuthority(authority); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "policy")
	promoted, err := control.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: "policy-promote", Authority: authority,
	})
	if err != nil {
		t.Fatal(err)
	}
	rolled, err := control.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverShadow,
		ExpectedRevision: promoted.Revision, ExpectedEpoch: promoted.Epoch, FencingToken: promoted.FencingToken,
		TransferID: "policy-rollback",
	})
	if err != nil {
		t.Fatalf("de-promotion without authority: %v", err)
	}
	if rolled.State != CutoverShadow || rolled.Owner != OwnerPython || rolled.FencingToken != promoted.FencingToken+1 {
		t.Fatalf("rollback record: %+v", rolled)
	}
	if count := authorizationRowCount(t, control); count != 1 {
		t.Fatalf("de-promotion journaled an authorization: %d", count)
	}
}

// A cutover decision must survive process restart, and the capability to promote
// further must stay a property of the deployment that opened the store.
func TestCutoverPromotionIsDurableAndDeploymentScoped(t *testing.T) {
	control := openAuthority(t)
	path := control.path
	authority := frozenCheckpoint(t, 0)
	if _, _, err := control.ClaimControlAuthority(authority); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "policy")
	if _, err := control.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: "policy-promote", Authority: authority,
	}); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}

	// A read-only-capability deployment can observe the promoted domain but not
	// promote it further.
	observer, err := OpenControl(OpenOptions{Path: path, Owner: "observer"})
	if err != nil {
		t.Fatal(err)
	}
	got, err := observer.GetCutover("policy")
	if err != nil || got.State != CutoverGoAuthoritative || got.Owner != RuntimeGo {
		t.Fatalf("promotion did not survive restart: %+v %v", got, err)
	}
	if head, exists, err := observer.ControlAuthorityHead(); err != nil || !exists || head.Generation != 1 {
		t.Fatalf("authority did not survive restart: %+v exists=%v %v", head, exists, err)
	}
	if _, err := observer.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverPythonShadow,
		ExpectedRevision: got.Revision, ExpectedEpoch: got.Epoch, FencingToken: got.FencingToken,
		TransferID: "observer-promote", Authority: authority,
	}); err != ErrCutoverNotAuthorized {
		t.Fatalf("observer promoted a domain: %v", err)
	}
	if err := observer.Close(); err != nil {
		t.Fatal(err)
	}

	successor, err := OpenControl(OpenOptions{Path: path, Owner: "successor", AuthorizeCutover: true})
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	if _, err := successor.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverPythonShadow,
		ExpectedRevision: got.Revision, ExpectedEpoch: got.Epoch, FencingToken: got.FencingToken,
		TransferID: "successor-promote", Authority: authority,
	}); err != nil {
		t.Fatalf("authorized successor could not continue the cutover: %v", err)
	}
	if count := authorizationRowCount(t, successor); count != 2 {
		t.Fatalf("authorization journal after restart: %d", count)
	}
}

// Presenting a larger epoch is not authority: the fencing CAS still refuses it.
func TestCutoverEpochCannotBeSelfPromoted(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	authority := frozenCheckpoint(t, 0)
	if _, _, err := control.ClaimControlAuthority(authority); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "policy")
	if _, err := control.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch + 1, FencingToken: dual.FencingToken,
		TransferID: "policy-promote", Authority: authority,
	}); err != internalprotocol.ErrStaleEpoch {
		t.Fatalf("epoch self-promotion: %v", err)
	}
	got, err := control.GetCutover("policy")
	if err != nil || got != dual {
		t.Fatalf("refused promotion changed state: %+v %v", got, err)
	}
	if count := authorizationRowCount(t, control); count != 0 {
		t.Fatalf("refused promotion journaled %d authorizations", count)
	}
}

// A rejected authorization journal write must roll the promotion back whole: no
// ownership change and no epoch advance survive a failed audit trail.
func TestCutoverPromotionRollsBackWhenTheAuthorizationJournalRejects(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	authority := frozenCheckpoint(t, 0)
	if _, _, err := control.ClaimControlAuthority(authority); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "policy")
	if _, err := control.db.Exec(`CREATE TRIGGER reject_cutover_authorization
		BEFORE INSERT ON control_cutover_authorizations
		BEGIN SELECT RAISE(ABORT, 'reject cutover authorization'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: "policy-promote", Authority: authority,
	}); err == nil || !strings.Contains(err.Error(), "reject cutover authorization") {
		t.Fatalf("authorization journal failure: %v", err)
	}
	got, err := control.GetCutover("policy")
	if err != nil || got != dual {
		t.Fatalf("failed audit trail left a promotion: %+v %v", got, err)
	}
	if count := authorizationRowCount(t, control); count != 0 {
		t.Fatalf("authorization rows = %d, want 0", count)
	}
}

// The signed mutation channel is a *pre-cutover* qualification mechanism. Its
// only frozen intent is `shadow-compare`, which describes a comparison
// expectation and carries no record body, so it cannot authorize a production
// mutation. Once the domain is Go's, acceptance must therefore refuse — and it
// must refuse before any durable write, or a caller could use a pre-cutover
// artifact to drive post-cutover state.
func TestAcceptMutationRefusesAfterCutoverWithoutWriting(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	authority := frozenCheckpoint(t, 0)
	if _, advanced, err := control.ClaimControlAuthority(authority); err != nil || !advanced {
		t.Fatalf("claim: advanced=%v %v", advanced, err)
	}
	dual := dualEvaluate(t, control, "policy")
	if _, err := control.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: "policy-promote", Authority: authority,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AcceptMutation(
		[]byte(`{"domain":"policy","operation":"propose-mutation"}`),
		MutationAuthority{},
	); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatalf("post-cutover acceptance: %v", err)
	}
	var operations, policies int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_operations").Scan(&operations); err != nil {
		t.Fatal(err)
	}
	if err := control.db.QueryRow("SELECT COUNT(*) FROM policies").Scan(&policies); err != nil {
		t.Fatal(err)
	}
	if operations != 0 || policies != 0 {
		t.Fatalf("refused acceptance wrote state: operations=%d policies=%d", operations, policies)
	}
}

func TestCutoverAuthorizationsAreImmutable(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	authority := frozenCheckpoint(t, 0)
	if _, _, err := control.ClaimControlAuthority(authority); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "policy")
	if _, err := control.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: "policy-promote", Authority: authority,
	}); err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		"UPDATE control_cutover_authorizations SET authority_generation=9",
		"DELETE FROM control_cutover_authorizations",
	} {
		if _, err := control.db.Exec(statement); err == nil {
			t.Fatalf("authorization journal was mutable: %s", statement)
		}
	}
	if count := authorizationRowCount(t, control); count != 1 {
		t.Fatalf("authorization journal rows = %d, want 1", count)
	}
}
