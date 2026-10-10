package worker

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

type fakeControlSigner func(*actionv1.SignControlRequest) (*actionv1.SignControlResponse, error)

func TestControlSignerRejectsDuplicateCredentialsBeforeIssuance(t *testing.T) {
	epoch, binding, _ := controlSigningFixture()
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY", binding.PublicKey)
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", binding.FleetID)
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT", binding.Environment)
	client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(*actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
		t.Fatal("duplicate credentials reached signing RPC")
		return nil, nil
	})}
	ctx := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer conflicting-offline-fixture")
	if _, err := client.SignControl(ctx, epoch, binding); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("duplicate signing credentials: %v", err)
	}
	if _, err := client.AuthorizeStorage(ctx, controlSigningGrant(epoch).StorageIntent, 2, 1); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("duplicate storage credentials: %v", err)
	}
}

func TestControlSignerRejectsValidGrantForMalformedCommand(t *testing.T) {
	epoch, binding, private := controlSigningFixture()
	valid := controlSigningGrant(epoch)
	response := fixtureSigningResponse(t, valid, binding, private, nil)
	for _, shape := range []string{"missing-intent", "missing-condition", "unknown-condition"} {
		t.Run(shape, func(t *testing.T) {
			input := proto.Clone(valid).(*actionv1.SignControlRequest)
			switch shape {
			case "missing-intent":
				input.StorageIntent = nil
			case "missing-condition":
				input.StorageIntent.Precondition = nil
			case "unknown-condition":
				input.StorageIntent.Precondition.ConditionType = actionv1.StorageConditionType(99)
			}
			client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(*actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
				return response, nil
			})}
			if _, err := client.SignControl(context.Background(), input, binding); !errors.Is(err, ErrControlSignerResponse) {
				t.Fatalf("accepted valid signature for malformed command: %v", err)
			}
		})
	}
}

func (fake fakeControlSigner) SignControl(_ context.Context, input *actionv1.SignControlRequest, _ ...grpc.CallOption) (*actionv1.SignControlResponse, error) {
	return fake(input)
}

func controlSigningFixture() (*actionv1.SignControlRequest, ControlSignerBinding, ed25519.PrivateKey) {
	private := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{42}, ed25519.SeedSize)) // offline unit fixture only
	binding := ControlSignerBinding{PublicKey: base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey)), FleetID: "fleet-a", Environment: "test"}
	input := &actionv1.SignControlRequest{Purpose: actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_INSTALL_EPOCH, Fence: &commonv1.ActionFence{ActionId: "native-action", ExecutionEpoch: 1}, RequestId: strings.Repeat("a", 64), Nonce: strings.Repeat("b", 64), Revision: 2, FencingToken: 1, FleetId: "fleet-a", Environment: "test"}
	return input, binding, private
}
func controlSigningGrant(input *actionv1.SignControlRequest) *actionv1.SignControlRequest {
	grant := proto.Clone(input).(*actionv1.SignControlRequest)
	grant.Purpose = actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_STORAGE_PUT
	grant.StorageIntent = &actionv1.StorageMutationRequest{Fence: proto.Clone(grant.Fence).(*commonv1.ActionFence), OperationId: strings.Repeat("c", 64), RequestId: grant.RequestId, Nonce: grant.Nonce, MutationType: "PUT_CHUNK", Provider: "s3", TargetIdentity: strings.Repeat("d", 64), Bucket: "native-fixture", Prefix: "native", ObjectKey: "chunk", PayloadDigest: strings.Repeat("e", 64), ExpectedLength: 1, Precondition: &actionv1.StoragePrecondition{ConditionType: actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_CREATE_ONLY}, SchemaVersion: 1}
	return grant
}
func fixtureSigningResponse(t *testing.T, input *actionv1.SignControlRequest, binding ControlSignerBinding, private ed25519.PrivateKey, alter func(map[string]any)) *actionv1.SignControlResponse {
	t.Helper()
	now := time.Now().UTC().Truncate(time.Second)
	issued, expires := input.IssuedAt, input.ExpiresAt
	if issued == "" {
		issued = now.Format("2006-01-02T15:04:05Z")
	}
	if expires == "" {
		expires = now.Add(5 * time.Minute).Format("2006-01-02T15:04:05Z")
	}
	unsigned := map[string]any{"schema": store.AuthorityRequestSchema, "schemaVersion": 1, "domain": "action", "operation": "install-epoch", "runtime": "go", "mode": "shadow", "role": "control-plane", "fleetId": input.FleetId, "environment": input.Environment, "actionId": input.Fence.ActionId, "executionEpoch": input.Fence.ExecutionEpoch, "fencingToken": int64(input.FencingToken), "revision": input.Revision, "requestId": input.RequestId, "nonce": input.Nonce, "issuedAt": issued, "expiresAt": expires, "payload": map[string]any{}}
	if intent := input.StorageIntent; intent != nil {
		condition := "CREATE_ONLY"
		if intent.Precondition.ConditionType == actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_IF_MATCH {
			condition = "IF_MATCH"
		}
		unsigned["schema"] = store.StorageOperationGrantSchema
		unsigned["operation"] = store.StorageOperationGrantPut
		unsigned["operationId"] = intent.OperationId
		unsigned["payload"] = map[string]any{"mutationType": intent.MutationType, "provider": intent.Provider, "targetIdentity": intent.TargetIdentity, "bucket": intent.Bucket, "prefix": intent.Prefix, "objectKey": intent.ObjectKey, "objectDigest": intent.PayloadDigest, "expectedLength": int64(intent.ExpectedLength), "conditionType": condition, "expectedEtag": intent.Precondition.ExpectedEtag, "claimRevision": input.Revision}
	}
	if alter != nil {
		alter(unsigned)
	}
	var raw []byte
	var err error
	if input.StorageIntent == nil {
		_, raw, err = store.SignAuthorityRequest(unsigned, private, binding.PublicKey)
	} else {
		_, raw, err = store.SignStorageOperationGrant(unsigned, private, binding.PublicKey)
	}
	if err != nil {
		t.Fatal(err)
	}
	keyID, _ := store.SignerKeyIDForPublicKey(binding.PublicKey)
	return &actionv1.SignControlResponse{Fence: proto.Clone(input.Fence).(*commonv1.ActionFence), CanonicalDocument: raw, SignerPublicKey: binding.PublicKey, SignerKeyId: keyID}
}

func TestControlSignerVerifiesExactPublicBindingAndStorageIntent(t *testing.T) {
	epoch, binding, private := controlSigningFixture()
	for _, input := range []*actionv1.SignControlRequest{epoch, controlSigningGrant(epoch)} {
		client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(input *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
			return fixtureSigningResponse(t, input, binding, private, nil), nil
		})}
		if _, err := client.SignControl(context.Background(), input, binding); err != nil {
			t.Fatal(err)
		}
	}
	unicodeGrant := controlSigningGrant(epoch)
	unicodeGrant.StorageIntent.ObjectKey = "对象<>&\u2028\u2029\\u2028"
	unicodeClient := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(input *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
		return fixtureSigningResponse(t, input, binding, private, nil), nil
	})}
	if _, err := unicodeClient.SignControl(context.Background(), unicodeGrant, binding); err != nil {
		t.Fatal(err)
	}
	input := controlSigningGrant(epoch)
	input.StorageIntent.Precondition.ConditionType = actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_IF_MATCH
	input.StorageIntent.Precondition.ExpectedEtag = "\"existing-etag\""
	client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(input *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
		return fixtureSigningResponse(t, input, binding, private, nil), nil
	})}
	if _, err := client.SignControl(context.Background(), input, binding); err != nil {
		t.Fatal(err)
	}
	epoch.IssuedAt = time.Now().UTC().Format("2006-01-02T15:04:05Z")
	epoch.ExpiresAt = time.Now().UTC().Add(time.Minute).Format("2006-01-02T15:04:05Z")
	if _, err := client.SignControl(context.Background(), epoch, binding); err != nil {
		t.Fatal(err)
	}
}

func TestControlSignerRejectsMalformedResponsesAndValidOtherSignatures(t *testing.T) {
	epoch, binding, private := controlSigningFixture()
	for _, field := range []string{"actionId", "executionEpoch", "revision", "requestId", "nonce", "issuedAt", "expiresAt"} {
		t.Run(field, func(t *testing.T) {
			input := proto.Clone(epoch).(*actionv1.SignControlRequest)
			input.IssuedAt = time.Now().UTC().Format("2006-01-02T15:04:05Z")
			input.ExpiresAt = time.Now().UTC().Add(time.Minute).Format("2006-01-02T15:04:05Z")
			client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(call *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
				return fixtureSigningResponse(t, call, binding, private, func(doc map[string]any) {
					switch field {
					case "executionEpoch", "revision":
						doc[field] = int64(3)
					case "actionId":
						doc[field] = "other-action"
					case "requestId", "nonce":
						doc[field] = strings.Repeat("f", 64)
					case "issuedAt":
						doc[field] = time.Now().UTC().Add(-time.Second).Format("2006-01-02T15:04:05Z")
					case "expiresAt":
						doc[field] = time.Now().UTC().Add(2 * time.Minute).Format("2006-01-02T15:04:05Z")
					}
				}), nil
			})}
			if _, err := client.SignControl(context.Background(), input, binding); !errors.Is(err, ErrControlSignerResponse) {
				t.Fatalf("accepted different %s: %v", field, err)
			}
		})
	}
	for change := 0; change < 8; change++ {
		client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(input *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
			response := fixtureSigningResponse(t, input, binding, private, nil)
			switch change {
			case 0:
				return nil, nil
			case 1:
				response.Fence = nil
			case 2:
				response.SignerPublicKey = "other"
			case 3:
				response.SignerKeyId = "other"
			case 4:
				response.CanonicalDocument = nil
			case 5:
				response.CanonicalDocument = []byte("{}")
			case 6:
				response.ProtoReflect().SetUnknown([]byte{0x78, 1})
			case 7:
				return nil, context.DeadlineExceeded
			}
			return response, nil
		})}
		if _, err := client.SignControl(context.Background(), epoch, binding); err == nil {
			t.Fatalf("accepted response case %d", change)
		}
	}
	grant := controlSigningGrant(epoch)
	client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(input *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
		response := fixtureSigningResponse(t, input, binding, private, func(doc map[string]any) { doc["payload"].(map[string]any)["objectKey"] = "other" })
		return response, nil
	})}
	if _, err := client.SignControl(context.Background(), grant, binding); err == nil {
		t.Fatal("accepted different storage placement")
	}
}

func TestControlSignerRefusesInvalidLocalInputsAndPlaintext(t *testing.T) {
	epoch, binding, private := controlSigningFixture()
	valid := fakeControlSigner(func(input *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
		return fixtureSigningResponse(t, input, binding, private, nil), nil
	})
	client := &Client{tlsSecured: true, bearerAttached: true, signer: valid}
	for change := 0; change < 9; change++ {
		input := proto.Clone(epoch).(*actionv1.SignControlRequest)
		switch change {
		case 0:
			input = nil
		case 1:
			input.Fence = nil
		case 2:
			input.FleetId = "other"
		case 3:
			input.Environment = "other"
		case 4:
			input.FencingToken = ^uint64(0)
		case 5:
			input.ProtoReflect().SetUnknown([]byte{0x78, 1})
		case 6:
			input.Fence.ProtoReflect().SetUnknown([]byte{0x78, 1})
		case 7:
			input.Purpose = 0
		case 8:
			input.Fence.ExecutionEpoch = 0
		}
		if _, err := client.SignControl(context.Background(), input, binding); err == nil {
			t.Fatalf("accepted input %d", change)
		}
	}
	bad := binding
	bad.PublicKey = "bad"
	if _, err := client.SignControl(context.Background(), epoch, bad); err == nil {
		t.Fatal("accepted invalid pinned key")
	}
	for _, candidate := range []*Client{nil, {}, {tlsSecured: true}, {tlsSecured: true, bearerAttached: true}} {
		if _, err := candidate.SignControl(context.Background(), epoch, binding); err == nil {
			t.Fatal("accepted unavailable transport")
		}
	}
	if _, err := client.SignControl(nil, epoch, binding); err == nil {
		t.Fatal("accepted nil context")
	}
	if signerIntegerMatches(json.Number("1.5"), 1) || signerIntegerMatches(int64(1), 1) {
		t.Fatal("accepted inexact integer")
	}
}

func TestAuthorizeStorageUsesRustSignerAndPreservesCallerBytes(t *testing.T) {
	epoch, binding, private := controlSigningFixture()
	request := controlSigningGrant(epoch).StorageIntent
	request.Payload = []byte{42}
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY", binding.PublicKey)
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", binding.FleetID)
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT", binding.Environment)
	for change := 0; change < 5; change++ {
		rpc := &fakeWorkerRPC{installResp: &actionv1.InstallAuthoritativeEpochResponse{Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED, Fence: request.Fence}, response: &actionv1.AdmitCommandResponse{Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN}}
		if change == 1 || change == 2 {
			rpc.installResp = &actionv1.InstallAuthoritativeEpochResponse{Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED, Error: &commonv1.ErrorDetail{Code: "AUTHORITY_REQUEST_REPLAY"}}
		}
		if change == 2 {
			rpc.err = context.DeadlineExceeded
		}
		if change == 3 {
			rpc.installErr = context.DeadlineExceeded
		}
		client := &Client{tlsSecured: true, bearerAttached: true, rpc: rpc, signer: fakeControlSigner(func(input *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
			if change == 4 && input.Purpose == actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_STORAGE_PUT {
				return nil, context.DeadlineExceeded
			}
			if input.StorageIntent != nil && (len(input.StorageIntent.Payload) != 0 || len(input.StorageIntent.CanonicalAuthorization) != 0) {
				t.Fatal("signing RPC received payload/key material")
			}
			return fixtureSigningResponse(t, input, binding, private, nil), nil
		})}
		authorized, err := client.AuthorizeStorage(context.Background(), request, 2, 1)
		if change < 2 {
			if err != nil || len(authorized.CanonicalAuthorization) == 0 {
				t.Fatalf("authorize %d: %v", change, err)
			}
			if len(request.CanonicalAuthorization) != 0 || !bytes.Equal(authorized.Payload, request.Payload) {
				t.Fatal("caller request changed")
			}
		} else if err == nil {
			t.Fatalf("accepted failure case %d", change)
		}
	}
	client := &Client{}
	for _, input := range []*actionv1.StorageMutationRequest{nil, {}, {Fence: epoch.Fence, CanonicalAuthorization: []byte{1}}} {
		if _, err := client.AuthorizeStorage(context.Background(), input, 2, 1); err == nil {
			t.Fatal("accepted invalid unsigned command")
		}
	}
	if _, err := client.AuthorizeStorage(context.Background(), request, 0, 1); err == nil {
		t.Fatal("accepted zero revision")
	}
	if _, err := client.AuthorizeStorage(context.Background(), request, 2, 0); err == nil {
		t.Fatal("accepted zero writer")
	}
}
