package worker

import (
	"context"
	"errors"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc"
)

type fakeWorkerRPC struct {
	request       *actionv1.AdmitCommandRequest
	response      *actionv1.AdmitCommandResponse
	err           error
	queryRequest  *actionv1.QueryEffectRequest
	queryResponse *actionv1.EffectResult
	queryErr      error
}

func (fake *fakeWorkerRPC) AdmitCommand(_ context.Context, request *actionv1.AdmitCommandRequest, _ ...grpc.CallOption) (*actionv1.AdmitCommandResponse, error) {
	fake.request = request
	return fake.response, fake.err
}

func (fake *fakeWorkerRPC) QueryEffect(_ context.Context, request *actionv1.QueryEffectRequest, _ ...grpc.CallOption) (*actionv1.EffectResult, error) {
	fake.queryRequest = request
	return fake.queryResponse, fake.queryErr
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
