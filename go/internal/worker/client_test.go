package worker

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
)

type fakeWorkerRPC struct {
	request        *actionv1.AdmitCommandRequest
	response       *actionv1.AdmitCommandResponse
	err            error
	queryRequest   *actionv1.QueryEffectRequest
	queryResponse  *actionv1.EffectResult
	queryErr       error
	installRequest *actionv1.InstallAuthoritativeEpochRequest
	installResp    *actionv1.InstallAuthoritativeEpochResponse
	installErr     error

	storageRequest  *actionv1.StorageMutationRequest
	storageResponse *actionv1.StorageMutationResponse
	storageErr      error

	storageQueryReq  *actionv1.QueryStorageEffectRequest
	storageQueryResp *actionv1.StorageMutationResponse
	storageQueryErr  error
}

func (fake *fakeWorkerRPC) AdmitCommand(_ context.Context, request *actionv1.AdmitCommandRequest, _ ...grpc.CallOption) (*actionv1.AdmitCommandResponse, error) {
	fake.request = request
	return fake.response, fake.err
}

func (fake *fakeWorkerRPC) QueryEffect(_ context.Context, request *actionv1.QueryEffectRequest, _ ...grpc.CallOption) (*actionv1.EffectResult, error) {
	fake.queryRequest = request
	return fake.queryResponse, fake.queryErr
}

func (fake *fakeWorkerRPC) InstallAuthoritativeEpoch(_ context.Context, request *actionv1.InstallAuthoritativeEpochRequest, _ ...grpc.CallOption) (*actionv1.InstallAuthoritativeEpochResponse, error) {
	fake.installRequest = request
	return fake.installResp, fake.installErr
}

func (fake *fakeWorkerRPC) ExecuteStorageMutation(_ context.Context, request *actionv1.StorageMutationRequest, _ ...grpc.CallOption) (*actionv1.StorageMutationResponse, error) {
	fake.storageRequest = request
	return fake.storageResponse, fake.storageErr
}

func (fake *fakeWorkerRPC) QueryStorageEffect(_ context.Context, request *actionv1.QueryStorageEffectRequest, _ ...grpc.CallOption) (*actionv1.StorageMutationResponse, error) {
	fake.storageQueryReq = request
	return fake.storageQueryResp, fake.storageQueryErr
}

func TestAdmitNeverForwardsCallerControlledLiveEpoch(t *testing.T) {
	rpc := &fakeWorkerRPC{response: &actionv1.AdmitCommandResponse{
		Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED,
		State:  commonv1.EffectState_EFFECT_STATE_UNKNOWN,
	}}
	client := New(rpc)
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 7}
	if err := client.Admit(context.Background(), actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, fence); err != nil {
		t.Fatal(err)
	}
	if rpc.request == nil || rpc.request.LiveEpoch != 0 || rpc.request.Fence != fence {
		t.Fatalf("untrusted authority forwarded: %+v", rpc.request)
	}
}

func TestAdmitMapsKnownRejectionAndFailsClosedOnMalformedResponse(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 7}
	rpc := &fakeWorkerRPC{response: &actionv1.AdmitCommandResponse{
		Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED,
		State:  commonv1.EffectState_EFFECT_STATE_UNKNOWN,
		Error:  &commonv1.ErrorDetail{Code: "FENCE_MISMATCH"},
	}}
	client := New(rpc)
	if err := client.Admit(context.Background(), actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, fence); err != internalprotocol.ErrFenceMismatch {
		t.Fatalf("rejection: %v", err)
	}

	for _, response := range []*actionv1.AdmitCommandResponse{
		nil,
		{},
		{Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED, State: commonv1.EffectState_EFFECT_STATE_APPLIED},
		{Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN, Error: &commonv1.ErrorDetail{Code: "FENCE_MISMATCH"}},
		{Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN},
		{Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED, State: commonv1.EffectState_EFFECT_STATE_APPLIED, Error: &commonv1.ErrorDetail{Code: "FENCE_MISMATCH"}},
		{Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN, Error: &commonv1.ErrorDetail{Code: "MADE_UP"}},
	} {
		rpc.response = response
		if err := client.Admit(context.Background(), actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, fence); err != ErrInvalidWorkerResponse {
			t.Fatalf("response %+v: %v", response, err)
		}
	}
}

func TestAdmitMapsEveryFrozenWorkerRejection(t *testing.T) {
	cases := map[string]error{
		"EMPTY_ACTION_ID":              internalprotocol.ErrEmptyActionID,
		"ZERO_EXECUTION_EPOCH":         internalprotocol.ErrZeroEpoch,
		"STALE_EXECUTION_EPOCH":        internalprotocol.ErrStaleEpoch,
		"FENCE_MISMATCH":               internalprotocol.ErrFenceMismatch,
		"EFFECT_UNKNOWN":               internalprotocol.ErrUnknownEffect,
		"STORAGE_NOT_AUTHORITATIVE":    internalprotocol.ErrStorageNotAuthoritative,
		"TRANSFER_NOT_AUTHORITATIVE":   internalprotocol.ErrTransferNotAuthoritative,
		"FEDERATION_NOT_AUTHORITATIVE": internalprotocol.ErrFederationNotAuthoritative,
		"PROOF_NOT_AUTHORITATIVE":      internalprotocol.ErrProofNotAuthoritative,
	}
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 7}
	for code, want := range cases {
		rpc := &fakeWorkerRPC{response: &actionv1.AdmitCommandResponse{
			Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED,
			State:  commonv1.EffectState_EFFECT_STATE_UNKNOWN,
			Error:  &commonv1.ErrorDetail{Code: code},
		}}
		if err := New(rpc).Admit(context.Background(), actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, fence); err != want {
			t.Fatalf("%s: %v", code, err)
		}
	}
}

func TestAdmitRejectsInvalidFenceBeforeRPCAndPreservesTransportFailure(t *testing.T) {
	rpc := &fakeWorkerRPC{err: context.DeadlineExceeded}
	client := New(rpc)
	if err := client.Admit(context.Background(), actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, &commonv1.ActionFence{}); err != internalprotocol.ErrEmptyActionID {
		t.Fatalf("invalid fence: %v", err)
	}
	if rpc.request != nil {
		t.Fatal("invalid fence reached RPC")
	}
	if err := client.Admit(context.Background(), actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 1}); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("transport error: %v", err)
	}
	valid := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 1}
	if err := client.Admit(context.Background(), actionv1.CommandKind_COMMAND_KIND_UNSPECIFIED, valid); err != internalprotocol.ErrUnknownEffect {
		t.Fatalf("unknown kind: %v", err)
	}
	var nilClient *Client
	if err := nilClient.Admit(context.Background(), actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, valid); err != ErrInvalidWorkerResponse {
		t.Fatalf("nil client: %v", err)
	}
}

func TestQueryEffectPreservesUnknownAndFrozenRejection(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 7}
	for _, test := range []struct {
		code string
		want error
	}{
		{code: "EFFECT_UNKNOWN", want: internalprotocol.ErrUnknownEffect},
		{code: "PROOF_NOT_AUTHORITATIVE", want: internalprotocol.ErrProofNotAuthoritative},
	} {
		rpc := &fakeWorkerRPC{queryResponse: &actionv1.EffectResult{
			Fence: &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 7},
			State: commonv1.EffectState_EFFECT_STATE_UNKNOWN,
			Error: &commonv1.ErrorDetail{Code: test.code},
		}}
		state, err := New(rpc).QueryEffect(context.Background(), fence)
		if state != commonv1.EffectState_EFFECT_STATE_UNKNOWN || err != test.want {
			t.Fatalf("%s: %v %v", test.code, state, err)
		}
		if rpc.queryRequest == nil || rpc.queryRequest.Fence != fence {
			t.Fatalf("query fence not forwarded: %+v", rpc.queryRequest)
		}
	}
}

func TestQueryEffectRejectsUnboundOrMalformedOutcome(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 7}
	for _, response := range []*actionv1.EffectResult{
		nil,
		{},
		{Fence: &commonv1.ActionFence{ActionId: "other", ExecutionEpoch: 7}, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN, Error: &commonv1.ErrorDetail{Code: "EFFECT_UNKNOWN"}},
		{Fence: &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 8}, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN, Error: &commonv1.ErrorDetail{Code: "EFFECT_UNKNOWN"}},
		{Fence: fence, State: commonv1.EffectState_EFFECT_STATE_UNSPECIFIED, Error: &commonv1.ErrorDetail{Code: "EFFECT_UNKNOWN"}},
		{Fence: fence, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN},
		{Fence: fence, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN, Error: &commonv1.ErrorDetail{Code: "FENCE_MISMATCH"}},
		{Fence: fence, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN, Error: &commonv1.ErrorDetail{Code: "MADE_UP"}},
		{Fence: fence, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN, EffectId: "ambiguous", Error: &commonv1.ErrorDetail{Code: "EFFECT_UNKNOWN"}},
		{Fence: fence, State: commonv1.EffectState_EFFECT_STATE_APPLIED, EffectId: "unverified", ReceiptDigest: "sha256:receipt", CommitDigest: "sha256:commit", ProofDigest: "sha256:proof"},
		{Fence: fence, State: commonv1.EffectState_EFFECT_STATE_NOT_APPLIED},
	} {
		rpc := &fakeWorkerRPC{queryResponse: response}
		state, err := New(rpc).QueryEffect(context.Background(), fence)
		if state != commonv1.EffectState_EFFECT_STATE_UNKNOWN || err != ErrInvalidWorkerResponse {
			t.Fatalf("response %+v: %v %v", response, state, err)
		}
	}
}

func TestQueryEffectValidatesFenceAndPreservesTransportFailure(t *testing.T) {
	rpc := &fakeWorkerRPC{queryErr: context.DeadlineExceeded}
	client := New(rpc)
	state, err := client.QueryEffect(context.Background(), &commonv1.ActionFence{})
	if state != commonv1.EffectState_EFFECT_STATE_UNKNOWN || err != internalprotocol.ErrEmptyActionID || rpc.queryRequest != nil {
		t.Fatalf("invalid fence: %v %v %+v", state, err, rpc.queryRequest)
	}
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 1}
	state, err = client.QueryEffect(context.Background(), fence)
	if state != commonv1.EffectState_EFFECT_STATE_UNKNOWN || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("transport failure: %v %v", state, err)
	}
	var nilClient *Client
	state, err = nilClient.QueryEffect(context.Background(), fence)
	if state != commonv1.EffectState_EFFECT_STATE_UNKNOWN || err != ErrInvalidWorkerResponse {
		t.Fatalf("nil client: %v %v", state, err)
	}
}

func TestPlaintextTargetMustBeLoopbackIPLiteral(t *testing.T) {
	for _, target := range []string{"127.0.0.1:50052", "[::1]:50052"} {
		if err := ValidatePlaintextTarget(target); err != nil {
			t.Fatalf("%s: %v", target, err)
		}
	}
	for _, target := range []string{"", "localhost:50052", "0.0.0.0:50052", "192.0.2.1:50052", "127.0.0.1:0", "127.0.0.1:nope", "127.0.0.1:65536"} {
		if err := ValidatePlaintextTarget(target); err != ErrUnsafeWorkerTarget {
			t.Fatalf("%s: %v", target, err)
		}
	}
}

func TestDialPlaintextLoopbackCreatesOnlyLoopbackClientAndCloseIsSafe(t *testing.T) {
	if _, err := DialPlaintextLoopback("example.com:50052"); err != ErrUnsafeWorkerTarget {
		t.Fatalf("unsafe dial: %v", err)
	}
	client, err := DialPlaintextLoopback("127.0.0.1:1")
	if err != nil {
		t.Fatal(err)
	}
	if client.rpc == nil || client.connection == nil {
		t.Fatalf("client %+v", client)
	}
	if err := client.Close(); err != nil {
		t.Fatal(err)
	}
	if err := New(&fakeWorkerRPC{}).Close(); err != nil {
		t.Fatal(err)
	}
	var nilClient *Client
	if err := nilClient.Close(); err != nil {
		t.Fatal(err)
	}
}

func frozenAuthorityRequest(t *testing.T) ([]byte, *commonv1.ActionFence) {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve worker test source")
	}
	path := filepath.Join(filepath.Dir(source), "..", "..", "..", "compat", "native-runtime", "v7", "control", "authority_request_vector.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		CanonicalRequest string `json:"canonical_request"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	canonical := []byte(fixture.CanonicalRequest)
	fence, err := store.FenceFromAuthorityRequest(canonical)
	if err != nil {
		t.Fatal(err)
	}
	return canonical, fence
}

func TestInstallAuthoritativeEpochAcceptsMatchingSignedDocument(t *testing.T) {
	canonical, fence := frozenAuthorityRequest(t)
	rpc := &fakeWorkerRPC{installResp: &actionv1.InstallAuthoritativeEpochResponse{
		Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED,
		Fence:  fence,
	}}
	if err := New(rpc).InstallAuthoritativeEpoch(context.Background(), fence, canonical); err != nil {
		t.Fatal(err)
	}
	if rpc.installRequest == nil || rpc.installRequest.Fence != fence || string(rpc.installRequest.CanonicalRequest) != string(canonical) {
		t.Fatalf("install request: %+v", rpc.installRequest)
	}
}

func TestInstallAuthoritativeEpochRejectsLocalMismatchesBeforeRPC(t *testing.T) {
	canonical, fence := frozenAuthorityRequest(t)
	rpc := &fakeWorkerRPC{}
	client := New(rpc)
	if err := client.InstallAuthoritativeEpoch(context.Background(), &commonv1.ActionFence{}, canonical); err != internalprotocol.ErrEmptyActionID {
		t.Fatalf("invalid fence: %v", err)
	}
	if err := client.InstallAuthoritativeEpoch(context.Background(), fence, nil); err != store.ErrAuthorityRequestInvalid {
		t.Fatalf("empty document: %v", err)
	}
	if err := client.InstallAuthoritativeEpoch(context.Background(), &commonv1.ActionFence{ActionId: "other", ExecutionEpoch: 4}, canonical); err != internalprotocol.ErrFenceMismatch {
		t.Fatalf("mismatched envelope: %v", err)
	}
	if rpc.installRequest != nil {
		t.Fatal("invalid install reached RPC")
	}
	oversized := make([]byte, store.MaxAuthorityRequestBytes+1)
	if err := client.InstallAuthoritativeEpoch(context.Background(), fence, oversized); err != store.ErrAuthorityRequestTooLarge {
		t.Fatalf("oversized: %v", err)
	}
}

func TestInstallAuthoritativeEpochMapsRejectionAndFailsClosed(t *testing.T) {
	canonical, fence := frozenAuthorityRequest(t)
	rpc := &fakeWorkerRPC{installResp: &actionv1.InstallAuthoritativeEpochResponse{
		Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED,
		Error:  &commonv1.ErrorDetail{Code: "AUTHORITY_REQUEST_SIGNER_MISMATCH"},
	}}
	if err := New(rpc).InstallAuthoritativeEpoch(context.Background(), fence, canonical); err != store.ErrAuthorityRequestSignerMismatch {
		t.Fatalf("signer: %v", err)
	}
	for _, response := range []*actionv1.InstallAuthoritativeEpochResponse{
		nil,
		{},
		{Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED},
		{Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED, Fence: fence, Error: &commonv1.ErrorDetail{Code: "AUTHORITY_REQUEST_REPLAY"}},
		{Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED, Fence: fence, Error: &commonv1.ErrorDetail{Code: "AUTHORITY_REQUEST_REPLAY"}},
		{Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED, Error: &commonv1.ErrorDetail{Code: "MADE_UP"}},
	} {
		rpc.installResp = response
		if err := New(rpc).InstallAuthoritativeEpoch(context.Background(), fence, canonical); err != ErrInvalidWorkerResponse {
			t.Fatalf("response %+v: %v", response, err)
		}
	}
	rpc.installResp = nil
	rpc.installErr = context.DeadlineExceeded
	if err := New(rpc).InstallAuthoritativeEpoch(context.Background(), fence, canonical); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("transport: %v", err)
	}
	var nilClient *Client
	if err := nilClient.InstallAuthoritativeEpoch(context.Background(), fence, canonical); err != ErrInvalidWorkerResponse {
		t.Fatalf("nil client: %v", err)
	}
}

func TestInstallAuthoritativeEpochMapsFrozenAuthorityCodes(t *testing.T) {
	canonical, fence := frozenAuthorityRequest(t)
	cases := map[string]error{
		"AUTHORITY_REQUEST_INVALID":                 store.ErrAuthorityRequestInvalid,
		"AUTHORITY_REQUEST_TOO_LARGE":               store.ErrAuthorityRequestTooLarge,
		"AUTHORITY_REQUEST_SCHEMA_INVALID":          store.ErrAuthorityRequestSchemaInvalid,
		"AUTHORITY_REQUEST_FIELDS_INVALID":          store.ErrAuthorityRequestFieldsInvalid,
		"AUTHORITY_REQUEST_CANONICAL_MISMATCH":      store.ErrAuthorityRequestCanonicalMismatch,
		"AUTHORITY_REQUEST_DIGEST_MISMATCH":         store.ErrAuthorityRequestDigestMismatch,
		"AUTHORITY_REQUEST_PAYLOAD_DIGEST_MISMATCH": store.ErrAuthorityRequestPayloadDigestMismatch,
		"AUTHORITY_REQUEST_SIGNATURE_INVALID":       store.ErrAuthorityRequestSignatureInvalid,
		"AUTHORITY_REQUEST_EXPIRED":                 store.ErrAuthorityRequestExpired,
		"AUTHORITY_REQUEST_FUTURE_SKEW":             store.ErrAuthorityRequestFutureSkew,
		"AUTHORITY_REQUEST_REPLAY":                  store.ErrAuthorityRequestReplay,
		"AUTHORITY_REQUEST_NONCE_REUSE":             store.ErrAuthorityRequestNonceReuse,
		"AUTHORITY_REQUEST_DOMAIN_MISMATCH":         store.ErrAuthorityRequestDomainMismatch,
		"AUTHORITY_REQUEST_FLEET_MISMATCH":          store.ErrAuthorityRequestFleetMismatch,
		"AUTHORITY_REQUEST_ENVIRONMENT_MISMATCH":    store.ErrAuthorityRequestEnvironmentMismatch,
		"AUTHORITY_REQUEST_ROLE_MISMATCH":           store.ErrAuthorityRequestRoleMismatch,
		"AUTHORITY_REQUEST_RUNTIME_MISMATCH":        store.ErrAuthorityRequestRuntimeMismatch,
		"AUTHORITY_REQUEST_MODE_MISMATCH":           store.ErrAuthorityRequestModeMismatch,
		"AUTHORITY_REQUEST_OPERATION_INVALID":       store.ErrAuthorityRequestOperationInvalid,
		"AUTHORITY_REQUEST_STALE_FENCING_TOKEN":     store.ErrAuthorityRequestStaleFencingToken,
		"AUTHORITY_REQUEST_SIGNER_MISMATCH":         store.ErrAuthorityRequestSignerMismatch,
		"AUTHORITY_REQUEST_SECRET_DETECTED":         store.ErrAuthorityRequestSecretDetected,
		"STALE_EXECUTION_EPOCH":                     internalprotocol.ErrStaleEpoch,
		"FENCE_MISMATCH":                            internalprotocol.ErrFenceMismatch,
		"EMPTY_ACTION_ID":                           internalprotocol.ErrEmptyActionID,
		"ZERO_EXECUTION_EPOCH":                      internalprotocol.ErrZeroEpoch,
	}
	for code, want := range cases {
		rpc := &fakeWorkerRPC{installResp: &actionv1.InstallAuthoritativeEpochResponse{
			Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED,
			Error:  &commonv1.ErrorDetail{Code: code},
		}}
		if err := New(rpc).InstallAuthoritativeEpoch(context.Background(), fence, canonical); err != want {
			t.Fatalf("%s: %v", code, err)
		}
	}
}

func TestExecuteStorageMutationValidation(t *testing.T) {
	client := New(&fakeWorkerRPC{})
	// Nil client
	var nilClient *Client
	if _, err := nilClient.ExecuteStorageMutation(context.Background(), nil, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("nil client: %v", err)
	}

	// Nil request or nil fence
	if _, err := client.ExecuteStorageMutation(context.Background(), nil, ""); err != internalprotocol.ErrEmptyActionID {
		t.Fatalf("nil request: %v", err)
	}
	if _, err := client.ExecuteStorageMutation(context.Background(), &actionv1.StorageMutationRequest{}, ""); err != internalprotocol.ErrEmptyActionID {
		t.Fatalf("nil fence: %v", err)
	}

	// Invalid fence
	if _, err := client.ExecuteStorageMutation(context.Background(), &actionv1.StorageMutationRequest{
		Fence: &commonv1.ActionFence{ActionId: "", ExecutionEpoch: 1},
	}, ""); err != internalprotocol.ErrEmptyActionID {
		t.Fatalf("empty action id: %v", err)
	}
	if _, err := client.ExecuteStorageMutation(context.Background(), &actionv1.StorageMutationRequest{
		Fence: &commonv1.ActionFence{ActionId: "a1", ExecutionEpoch: 0},
	}, ""); err != internalprotocol.ErrZeroEpoch {
		t.Fatalf("zero epoch: %v", err)
	}

	// Empty operation ID
	if _, err := client.ExecuteStorageMutation(context.Background(), &actionv1.StorageMutationRequest{
		Fence:       &commonv1.ActionFence{ActionId: "a1", ExecutionEpoch: 1},
		OperationId: "",
	}, ""); err != store.ErrAuthorityRequestOperationInvalid {
		t.Fatalf("empty op id: %v", err)
	}
}

func TestExecuteStorageMutationConfirmed(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 2}
	rpc := &fakeWorkerRPC{
		storageResponse: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			OperationId: "op-1",
			State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
			Fence:       fence,
			Etag:        "\"etag-1\"",
			EffectId:    "act-1:2",
		},
	}
	client := New(rpc)
	resp, err := client.ExecuteStorageMutation(context.Background(), &actionv1.StorageMutationRequest{
		Fence:       fence,
		OperationId: "op-1",
	}, "test-token")
	if err != nil {
		t.Fatalf("confirmed: %v", err)
	}
	if resp.Etag != "\"etag-1\"" || resp.EffectId != "act-1:2" {
		t.Fatalf("unexpected resp: %+v", resp)
	}
}

func TestExecuteStorageMutationRejectionAndFailures(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 2}
	cases := map[string]error{
		"SERVICE_AUTHENTICATION_UNAVAILABLE": internalprotocol.ErrServiceAuthenticationUnavailable,
		"AUTHENTICATION_MISSING":             internalprotocol.ErrAuthenticationMissing,
		"AUTHENTICATION_INVALID":             internalprotocol.ErrAuthenticationInvalid,
		"PRECONDITION_REJECTED":              internalprotocol.ErrStoragePreconditionRejected,
		"REPLAY_REJECTED":                    internalprotocol.ErrStorageReplayRejected,
		"UNKNOWN_EFFECT_RETRY_BLOCKED":       internalprotocol.ErrStorageUnknownEffectRetryBlocked,
		"TARGET_MISMATCH":                    internalprotocol.ErrStorageTargetMismatch,
		"DIGEST_MISMATCH":                    internalprotocol.ErrStorageDigestMismatch,
		"WORKER_WITHOUT_AUTHORITY":           internalprotocol.ErrStorageWorkerWithoutAuthority,
		"STORAGE_TRANSPORT_UNAVAILABLE":      internalprotocol.ErrStorageTransportUnavailable,
		"STORAGE_TRANSPORT_ERROR":            internalprotocol.ErrStorageTransportError,
		"FENCE_MISMATCH":                     internalprotocol.ErrFenceMismatch,
	}

	for code, want := range cases {
		rpc := &fakeWorkerRPC{
			storageResponse: &actionv1.StorageMutationResponse{
				Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED,
				OperationId: "op-1",
				Fence:       fence,
				Error:       &commonv1.ErrorDetail{Code: code},
			},
		}
		client := New(rpc)
		_, err := client.ExecuteStorageMutation(context.Background(), &actionv1.StorageMutationRequest{
			Fence:       fence,
			OperationId: "op-1",
		}, "tok")
		if !errors.Is(err, want) {
			t.Fatalf("%s: got %v, want %v", code, err, want)
		}
	}
}

func TestQueryStorageEffectStates(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 2}

	// 1. Confirmed
	rpc := &fakeWorkerRPC{
		storageQueryResp: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			OperationId: "op-1",
			State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
			Fence:       fence,
			Etag:        "\"etag-1\"",
		},
	}
	client := New(rpc)
	resp, err := client.QueryStorageEffect(context.Background(), fence, "op-1", "tok")
	if err != nil || resp.Etag != "\"etag-1\"" {
		t.Fatalf("confirmed query: %v, %+v", err, resp)
	}

	// 2. EffectUnknown
	rpc.storageQueryResp = &actionv1.StorageMutationResponse{
		Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_EFFECT_UNKNOWN,
		OperationId: "op-1",
		State:       commonv1.EffectState_EFFECT_STATE_UNKNOWN,
		Fence:       fence,
	}
	_, err = client.QueryStorageEffect(context.Background(), fence, "op-1", "tok")
	if !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("unknown query: %v", err)
	}

	// 3. Reconciling
	rpc.storageQueryResp = &actionv1.StorageMutationResponse{
		Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_RECONCILING,
		OperationId: "op-1",
		State:       commonv1.EffectState_EFFECT_STATE_UNKNOWN,
		Fence:       fence,
	}
	_, err = client.QueryStorageEffect(context.Background(), fence, "op-1", "tok")
	if !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("reconciling query: %v", err)
	}

	// 4. Transport error
	rpc.storageQueryResp = nil
	rpc.storageQueryErr = errors.New("connection reset")
	_, err = client.QueryStorageEffect(context.Background(), fence, "op-1", "tok")
	if err == nil {
		t.Fatal("expected transport error")
	}
}

func TestExecuteStorageMutationMalformedResponses(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 2}
	req := &actionv1.StorageMutationRequest{Fence: fence, OperationId: "op-1"}

	// 1. Nil response from RPC
	rpc := &fakeWorkerRPC{storageResponse: nil}
	client := New(rpc)
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("expected ErrInvalidWorkerResponse on nil response, got: %v", err)
	}

	// 2. Transport error from RPC
	rpc.storageErr = errors.New("rpc failed")
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err == nil {
		t.Fatal("expected rpc transport error")
	}
	rpc.storageErr = nil

	// 3. Response fence mismatch: ActionId
	rpc.storageResponse = &actionv1.StorageMutationResponse{
		Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
		State:  commonv1.EffectState_EFFECT_STATE_APPLIED,
		Fence:  &commonv1.ActionFence{ActionId: "other", ExecutionEpoch: 2},
	}
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("fence action mismatch: %v", err)
	}

	// 4. Response fence mismatch: ExecutionEpoch
	rpc.storageResponse.Fence = &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 3}
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("fence epoch mismatch: %v", err)
	}

	// 5. Response operation ID mismatch
	rpc.storageResponse.Fence = fence
	rpc.storageResponse.OperationId = "wrong-op"
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("operation id mismatch: %v", err)
	}
	rpc.storageResponse.OperationId = "op-1"

	// 6. Confirmed with Error != nil
	rpc.storageResponse.Error = &commonv1.ErrorDetail{Code: "FENCE_MISMATCH"}
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("confirmed with error: %v", err)
	}
	rpc.storageResponse.Error = nil

	// 7. Confirmed with State != Applied
	rpc.storageResponse.State = commonv1.EffectState_EFFECT_STATE_UNKNOWN
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("confirmed with unknown state: %v", err)
	}

	// 8. Rejected with Error == nil
	rpc.storageResponse.Status = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED
	rpc.storageResponse.Error = nil
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("rejected with nil error: %v", err)
	}

	// 9. Failed with Error == nil
	rpc.storageResponse.Status = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED
	rpc.storageResponse.Error = nil
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("failed with nil error: %v", err)
	}

	// 10. Failed with known error
	rpc.storageResponse.Error = &commonv1.ErrorDetail{Code: "STORAGE_TRANSPORT_ERROR"}
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); !errors.Is(err, internalprotocol.ErrStorageTransportError) {
		t.Fatalf("failed with known error: %v", err)
	}

	// 11. Unknown status
	rpc.storageResponse.Status = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_UNSPECIFIED
	if _, err := client.ExecuteStorageMutation(context.Background(), req, ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("unknown status: %v", err)
	}
}

func TestQueryStorageEffectMalformedResponses(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 2}

	// 1. Nil client
	var nilClient *Client
	if _, err := nilClient.QueryStorageEffect(context.Background(), fence, "op-1", ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("nil client: %v", err)
	}

	// 2. Nil fence / invalid fence
	client := New(&fakeWorkerRPC{})
	if _, err := client.QueryStorageEffect(context.Background(), nil, "op-1", ""); err != internalprotocol.ErrEmptyActionID {
		t.Fatalf("nil fence: %v", err)
	}
	if _, err := client.QueryStorageEffect(context.Background(), &commonv1.ActionFence{ActionId: "", ExecutionEpoch: 1}, "op-1", ""); err != internalprotocol.ErrEmptyActionID {
		t.Fatalf("empty action id: %v", err)
	}
	if _, err := client.QueryStorageEffect(context.Background(), &commonv1.ActionFence{ActionId: "a", ExecutionEpoch: 0}, "op-1", ""); err != internalprotocol.ErrZeroEpoch {
		t.Fatalf("zero epoch: %v", err)
	}

	// 3. Nil response from RPC
	rpc := &fakeWorkerRPC{storageQueryResp: nil}
	client = New(rpc)
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("nil query response: %v", err)
	}

	// 4. Response fence mismatch: ActionId
	rpc.storageQueryResp = &actionv1.StorageMutationResponse{
		Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
		OperationId: "op-1",
		State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
		Fence:       &commonv1.ActionFence{ActionId: "other", ExecutionEpoch: 2},
	}
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("fence action mismatch: %v", err)
	}

	// 5. Response fence mismatch: ExecutionEpoch
	rpc.storageQueryResp.Fence = &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 5}
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("fence epoch mismatch: %v", err)
	}

	// 6. Confirmed with Error != nil
	rpc.storageQueryResp.Fence = fence
	rpc.storageQueryResp.Error = &commonv1.ErrorDetail{Code: "FENCE_MISMATCH"}
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("confirmed with error: %v", err)
	}
	rpc.storageQueryResp.Error = nil

	// 7. Confirmed with State != Applied
	rpc.storageQueryResp.State = commonv1.EffectState_EFFECT_STATE_UNKNOWN
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("confirmed with unknown state: %v", err)
	}

	// 8. Rejected with Error == nil
	rpc.storageQueryResp.Status = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED
	rpc.storageQueryResp.Error = nil
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("rejected with nil error: %v", err)
	}

	// 9. Rejected with known error
	rpc.storageQueryResp.Error = &commonv1.ErrorDetail{Code: "REPLAY_REJECTED"}
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); !errors.Is(err, internalprotocol.ErrStorageReplayRejected) {
		t.Fatalf("rejected with replay: %v", err)
	}

	// 10. Failed with Error == nil
	rpc.storageQueryResp.Status = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED
	rpc.storageQueryResp.Error = nil
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("failed with nil error: %v", err)
	}

	// 11. Failed with known error
	rpc.storageQueryResp.Error = &commonv1.ErrorDetail{Code: "STORAGE_TRANSPORT_UNAVAILABLE"}
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); !errors.Is(err, internalprotocol.ErrStorageTransportUnavailable) {
		t.Fatalf("failed with transport unavailable: %v", err)
	}

	// 12. Unknown status
	rpc.storageQueryResp.Status = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_UNSPECIFIED
	if _, err := client.QueryStorageEffect(context.Background(), fence, "op-1", ""); err != ErrInvalidWorkerResponse {
		t.Fatalf("unspecified status: %v", err)
	}
}

func TestDialPlaintextLoopbackTargets(t *testing.T) {
	if _, err := DialPlaintextLoopback("192.168.1.1:8080"); err == nil {
		t.Fatal("expected error for non-loopback target")
	}
	client, err := DialPlaintextLoopback("127.0.0.1:54321")
	if err != nil {
		t.Fatalf("unexpected dial error: %v", err)
	}
	if client != nil {
		_ = client.Close()
	}
}

func TestWorkerClientEdgeCoverage(t *testing.T) {
	client := New(&fakeWorkerRPC{})

	// 1. InstallAuthoritativeEpoch with malformed JSON canonical request (line 101)
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 1}
	if err := client.InstallAuthoritativeEpoch(context.Background(), fence, []byte("{")); err == nil {
		t.Fatal("expected error on malformed JSON")
	}

	// 2. ExecuteStorageMutation returning STORAGE_MUTATION_STATUS_EFFECT_UNKNOWN (line 205)
	rpcUnknown := &fakeWorkerRPC{
		storageResponse: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_EFFECT_UNKNOWN,
			OperationId: "op-1",
			Fence:       fence,
		},
	}
	clientUnknown := New(rpcUnknown)
	_, err := clientUnknown.ExecuteStorageMutation(context.Background(), &actionv1.StorageMutationRequest{
		Fence:       fence,
		OperationId: "op-1",
	}, "")
	if !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("expected ErrUnknownEffect, got: %v", err)
	}

	// 3. knownRejection for OPERATION_INVALID (line 346)
	rpcOpInv := &fakeWorkerRPC{
		storageResponse: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED,
			Error:       &commonv1.ErrorDetail{Code: store.ErrAuthorityRequestOperationInvalid.Error()},
			Fence:       fence,
			OperationId: "op-1",
		},
	}
	clientOpInv := New(rpcOpInv)
	_, err = clientOpInv.ExecuteStorageMutation(context.Background(), &actionv1.StorageMutationRequest{
		Fence:       fence,
		OperationId: "op-1",
	}, "")
	if !errors.Is(err, store.ErrAuthorityRequestOperationInvalid) {
		t.Fatalf("expected ErrAuthorityRequestOperationInvalid, got: %v", err)
	}
}
