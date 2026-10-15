package store

import (
	"database/sql"
	"math"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

// AgentRunRuntimeUpdate contains immutable Rust artifact references, never
// request bodies or provider credentials. ExpectedRevision and ExpectedRequest
// bind a preparation to the exact prior request; the existing event cursor CAS
// also prevents a concurrent confirmation, edit or cancellation from winning.
type AgentRunRuntimeUpdate struct {
	ExpectedRevision uint64            `json:"expectedRevision"`
	ExpectedRequest  agent.ArtifactRef `json:"expectedRequest"`
	Request          agent.ArtifactRef `json:"request"`
}

func applyAgentRuntimeUpdateTx(tx *sql.Tx, run *agent.RunMetadata, mutation AgentRunEventMutation) error {
	update := mutation.RuntimeUpdate
	if update == nil {
		return nil
	}
	// Recovery has a distinct atomic request/context admission. This legacy
	// awaiting-plan swap must never downgrade or rewrite a schema3 scope.
	if run.Recovery != nil || run.SchemaVersion >= 3 {
		return ErrIllegalTransition
	}
	current := run.RuntimeVersion()
	if update.ExpectedRevision == 0 || current >= math.MaxInt64 ||
		!validAgentArtifact(update.ExpectedRequest) || !validAgentArtifact(update.Request) {
		return ErrAgentRunMetadataInvalid
	}
	if update.ExpectedRevision != current || update.ExpectedRequest != run.Request {
		return ErrRevisionConflict
	}
	if run.Status != "awaiting_plan" || run.Cancelled || len(run.Plan) == 0 ||
		mutation.Event.Type != "run_status" || mutation.Event.Status != "running" {
		return ErrIllegalTransition
	}
	var reserved bool
	if err := tx.QueryRow(`SELECT EXISTS(SELECT 1 FROM action_resource_leases WHERE resource_key=?)`,
		"agent-run:"+run.RunID).Scan(&reserved); err != nil {
		return err
	}
	// There is deliberately no deadline predicate. A lost or expired controller
	// does not prove that its external effects were unapplied or settled.
	if reserved {
		return ErrAgentRunExecutionRequired
	}
	run.Request = update.Request
	// Older Go readers reject schema2 rather than decoding this field away and
	// later persisting a v1 record with a silently reset runtime revision.
	run.SchemaVersion = 2
	run.RuntimeRevision = current + 1
	return nil
}
