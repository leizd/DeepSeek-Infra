package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net"
	"os"
	"time"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type agentExecutionAuthorizer interface {
	AuthorizeAgentExecution(context.Context, *agentv1.AgentRunExecutionClaim, string, string, worker.ControlSignerBinding) ([]byte, error)
}

type agentExecutionRefresher interface {
	RefreshAgentExecution(context.Context, *agentv1.AgentRunExecutionClaim, string, string, worker.ControlSignerBinding) ([]byte, error)
}

type agentExecutionSignerSession struct {
	client  agentExecutionAuthorizer
	binding worker.ControlSignerBinding
	close   func() error
}

// Only deployment-pinned public identity and authenticated TLS configuration
// enter Go. Neither custody paths nor a caller-supplied signer are accepted.
func configuredAgentExecutionSigner(control *store.Control) (*agentExecutionSignerSession, error) {
	cfg, err := worker.TLSDialConfigFromEnv()
	if err != nil {
		return nil, err
	}
	host, _, err := net.SplitHostPort(cfg.Target)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		return nil, worker.ErrWorkerTLSConfigInvalid
	}
	binding := worker.ControlSignerBinding{
		PublicKey:   os.Getenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY"),
		FleetID:     os.Getenv("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID"),
		Environment: os.Getenv("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT"),
	}
	fleet, environment := control.AgentExecutionRealm()
	if _, err := store.SignerKeyIDForPublicKey(binding.PublicKey); err != nil || fleet == "" || environment == "" ||
		binding.FleetID != fleet || binding.Environment != environment {
		return nil, worker.ErrControlSignerResponse
	}
	client, err := worker.DialTLS(cfg)
	if err != nil {
		return nil, err
	}
	return &agentExecutionSignerSession{client: client, binding: binding, close: client.Close}, nil
}

func agentSigningIntent(claim *agentv1.AgentRunExecutionClaim) (store.AgentExecutionGrantIntent, error) {
	if claim == nil || claim.Fence == nil || claim.Request == nil || claim.ReconciliationRequired ||
		(claim.State != "CLAIMED" && claim.State != "EXECUTING") {
		return store.AgentExecutionGrantIntent{}, store.ErrAgentExecutionGrant
	}
	intent := store.AgentExecutionGrantIntent{ActionID: claim.Fence.ActionId, ExecutionEpoch: claim.Fence.ExecutionEpoch,
		RunID: claim.RunId, Phase: executionPhaseName(claim.Phase), RequestSHA256: claim.Request.Sha256,
		RequestLength: claim.Request.Length, PlanDigest: claim.PlanDigest, MetadataIndex: claim.MetadataIndex,
		MetadataEpoch: claim.MetadataEpoch, Owner: claim.Owner, ClaimToken: claim.ClaimToken,
		ClaimRevision: claim.ClaimRevision, WriterFencingToken: claim.WriterFencingToken}
	return intent, store.ValidateAgentExecutionGrantIntent(intent)
}

// Dates and transient state are excluded. The identity stays the same across
// lease renewal and an EXECUTING replay, but changes with any admitted scope.
func agentSigningIdentifiers(intent store.AgentExecutionGrantIntent) (string, string) {
	raw, _ := json.Marshal(intent)
	hash := func(domain string) string {
		sum := sha256.Sum256(append([]byte(domain+"\x00"), raw...))
		return hex.EncodeToString(sum[:])
	}
	return hash("agent-authorize-request-v1"), hash("agent-authorize-nonce-v1")
}

func agentSigningError(err error) error {
	switch status.Code(err) {
	case codes.Canceled, codes.DeadlineExceeded:
		return status.Error(status.Code(err), "AGENT_EXECUTION_SIGNING_INTERRUPTED")
	case codes.FailedPrecondition, codes.InvalidArgument:
		return status.Error(codes.FailedPrecondition, "AGENT_EXECUTION_SIGNING_DENIED")
	case codes.PermissionDenied, codes.Unauthenticated:
		return status.Error(codes.PermissionDenied, "AGENT_EXECUTION_SIGNER_UNAUTHORIZED")
	default:
		return status.Error(codes.Unavailable, "AGENT_EXECUTION_SIGNER_UNAVAILABLE")
	}
}

// AuthorizeExecution is the production control path from durable admission to
// Rust custody. It never settles an effect or authorizes fresh reconciliation
// dispatch. Rust independently renews the same Go claim before issuing bytes.
func (service *agentRunService) AuthorizeExecution(ctx context.Context, input *agentv1.ClaimAgentRunExecutionRequest) (*agentv1.AuthorizeAgentRunExecutionResponse, error) {
	if err := service.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || executionPhaseName(input.Phase) == "" ||
		len(input.ProtoReflect().GetUnknown()) != 0 ||
		(input.MetadataFence != nil && len(input.MetadataFence.ProtoReflect().GetUnknown()) != 0) {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_EXECUTION_INVALID")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	input = proto.Clone(input).(*agentv1.ClaimAgentRunExecutionRequest)
	factory := service.executionSigner
	if factory == nil {
		factory = configuredAgentExecutionSigner
	}
	signer, err := factory(service.control)
	if err != nil || signer == nil || signer.client == nil {
		return nil, status.Error(codes.Unavailable, "AGENT_EXECUTION_SIGNER_UNAVAILABLE")
	}
	if signer.close != nil {
		defer signer.close()
	}
	admission, err := service.ClaimExecution(ctx, input)
	if err != nil {
		return nil, err
	}
	intent, err := agentSigningIntent(admission.Claim)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, "AGENT_EXECUTION_RECONCILIATION_REQUIRED")
	}
	requestID, nonce := agentSigningIdentifiers(intent)
	raw, err := signer.client.AuthorizeAgentExecution(ctx, proto.Clone(admission.Claim).(*agentv1.AgentRunExecutionClaim), requestID, nonce, signer.binding)
	if err != nil {
		return nil, agentSigningError(err)
	}
	raw = append([]byte(nil), raw...)
	// Re-read live authority after the remote call. A cancelled/expired/rebound
	// run cannot receive a successful authorization response, even if a grant
	// was issued before that concurrent state change.
	renewed, err := service.RenewExecution(ctx, &agentv1.RenewAgentRunExecutionRequest{Fence: admission.Claim.Fence,
		RunId: admission.Claim.RunId, Owner: admission.Claim.Owner, ClaimToken: admission.Claim.ClaimToken, LeaseSeconds: 60})
	if err != nil {
		return nil, err
	}
	liveIntent, err := agentSigningIntent(renewed.Claim)
	if err != nil || liveIntent != intent {
		return nil, status.Error(codes.FailedPrecondition, "AGENT_EXECUTION_BINDING_MISMATCH")
	}
	return verifiedAgentAuthorization(signer, intent, requestID, nonce, raw, renewed.Claim, admission.AlreadyApplied)
}

func verifiedAgentAuthorization(signer *agentExecutionSignerSession, intent store.AgentExecutionGrantIntent, requestID, nonce string, raw []byte, claim *agentv1.AgentRunExecutionClaim, alreadyApplied bool) (*agentv1.AuthorizeAgentRunExecutionResponse, error) {
	keyID, err := store.SignerKeyIDForPublicKey(signer.binding.PublicKey)
	if err != nil {
		return nil, status.Error(codes.DataLoss, "AGENT_EXECUTION_GRANT_INVALID")
	}
	document, err := store.VerifyAgentExecutionGrant(raw, store.AuthorityRequestContext{Now: time.Now().UTC(),
		SignerPublicKey: signer.binding.PublicKey, SignerKeyID: keyID, ExpectedDomain: "action",
		ExpectedOperation: store.AgentExecutionGrantOperation, ExpectedRuntime: "go", ExpectedMode: "authoritative",
		ExpectedRole: "control-plane", ExpectedFleetID: signer.binding.FleetID, ExpectedEnvironment: signer.binding.Environment,
		CurrentFencingToken: intent.WriterFencingToken, LiveEpoch: int64(intent.ExecutionEpoch), MaxFutureSkewSeconds: 30}, intent)
	if err != nil || document["requestId"] != requestID || document["nonce"] != nonce {
		return nil, status.Error(codes.DataLoss, "AGENT_EXECUTION_GRANT_INVALID")
	}
	return &agentv1.AuthorizeAgentRunExecutionResponse{AlreadyApplied: alreadyApplied, Claim: claim,
		CanonicalAuthorization: raw, SignerPublicKey: signer.binding.PublicKey, FleetId: signer.binding.FleetID,
		Environment: signer.binding.Environment}, nil
}

// RefreshAuthorization renews a live, already-admitted phase and issues a new
// grant under an explicit operation identity. Claiming and epoch installation
// remain separate; signing never rescues a stale/cancelled/unknown action.
func (service *agentRunService) RefreshAuthorization(ctx context.Context, input *agentv1.RefreshAgentRunAuthorizationRequest) (*agentv1.AuthorizeAgentRunExecutionResponse, error) {
	if err := service.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || !store.ValidRecordID(input.OperationId) ||
		input.Fence == nil || !store.ValidRecordID(input.Fence.ActionId) || input.Fence.ExecutionEpoch == 0 ||
		!store.ValidRecordID(input.RunId) || input.Owner == "" || input.ClaimToken == "" ||
		input.LeaseSeconds == 0 || input.LeaseSeconds > 300 ||
		len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_EXECUTION_INVALID")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	input = proto.Clone(input).(*agentv1.RefreshAgentRunAuthorizationRequest)
	factory := service.executionSigner
	if factory == nil {
		factory = configuredAgentExecutionSigner
	}
	signer, err := factory(service.control)
	if err != nil || signer == nil || signer.client == nil {
		return nil, status.Error(codes.Unavailable, "AGENT_EXECUTION_SIGNER_UNAVAILABLE")
	}
	if signer.close != nil {
		defer signer.close()
	}
	refresher, ok := signer.client.(agentExecutionRefresher)
	if !ok {
		return nil, status.Error(codes.Unavailable, "AGENT_EXECUTION_SIGNER_UNAVAILABLE")
	}
	renewal := &agentv1.RenewAgentRunExecutionRequest{Fence: input.Fence, RunId: input.RunId,
		Owner: input.Owner, ClaimToken: input.ClaimToken, LeaseSeconds: input.LeaseSeconds}
	live, err := service.RenewExecution(ctx, renewal)
	if err != nil {
		return nil, err
	}
	intent, err := agentSigningIntent(live.Claim)
	if err != nil {
		return nil, agentSigningError(err)
	}
	hash := func(domain string) string {
		sum := sha256.Sum256([]byte(domain + "\x00" + input.OperationId))
		return hex.EncodeToString(sum[:])
	}
	requestID, nonce := hash("agent-refresh-request-v1"), hash("agent-refresh-nonce-v1")
	raw, err := refresher.RefreshAgentExecution(ctx, live.Claim, requestID, nonce, signer.binding)
	if err != nil {
		return nil, agentSigningError(err)
	}
	raw = append([]byte(nil), raw...)
	renewed, err := service.RenewExecution(ctx, renewal)
	if err != nil {
		return nil, err
	}
	after, err := agentSigningIntent(renewed.Claim)
	if err != nil || after != intent {
		return nil, status.Error(codes.FailedPrecondition, "AGENT_EXECUTION_BINDING_MISMATCH")
	}
	return verifiedAgentAuthorization(signer, intent, requestID, nonce, raw, renewed.Claim, true)
}
