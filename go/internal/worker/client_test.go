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
