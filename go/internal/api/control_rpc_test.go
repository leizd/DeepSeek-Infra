package api

import (
	"context"
	"net"
	"net/http"
	"reflect"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func controlRPCClient(t *testing.T, control *store.Control, bearer string) controlv1.ControlPlaneClient {
	return controlRPCClientWithHealth(t, control, bearer, ShadowHealth())
}

func controlRPCClientWithHealth(t *testing.T, control *store.Control, bearer string, health ControlPlaneHealth) controlv1.ControlPlaneClient {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	rpc := NewControlRPC(control, bearer, health)
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetUnencryptedHTTP2(true)
	server := &http.Server{Handler: rpc, Protocols: protocols}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close(); rpc.Stop(); server.Close(); <-done })
	return controlv1.NewControlPlaneClient(connection)
}

func TestControlRPCHealthReportsOnlyTheConfiguredProcessAuthority(t *testing.T) {
	for _, test := range []struct {
		name      string
		health    ControlPlaneHealth
		mode      controlv1.RuntimeMode
		authority string
	}{
		{"empty defaults to shadow", ControlPlaneHealth{}, controlv1.RuntimeMode_RUNTIME_MODE_SHADOW_READONLY, "python"},
		{"configured shadow", HealthFor(config.Config{Mode: config.ModeShadow}), controlv1.RuntimeMode_RUNTIME_MODE_SHADOW_READONLY, "python"},
		{"configured authoritative", HealthFor(config.Config{Mode: config.ModeAuthoritative}), controlv1.RuntimeMode_RUNTIME_MODE_AUTHORITATIVE, "go"},
	} {
		t.Run(test.name, func(t *testing.T) {
			client := controlRPCClientWithHealth(t, nil, testInternalBearer, test.health)
			response, err := client.Health(controlRPCContext(t, testInternalBearer), &controlv1.HealthRequest{})
			if err != nil || !response.Ok || response.Mode != test.mode || response.MutationAuthority != test.authority {
				t.Fatalf("health authority: %v %v", response, err)
			}
			// Process authority never supplies missing per-domain policy state.
			if _, err := client.GetBackupPolicyRecipients(controlRPCContext(t, testInternalBearer), &controlv1.BackupPolicyRecipientsRequest{}); status.Code(err) != codes.Unavailable {
				t.Fatalf("health invented an authoritative policy store: %v", err)
			}
		})
	}
}

func TestControlRPCRejectsDuplicateCredentialsAndOversizedMessages(t *testing.T) {
	client := controlRPCClient(t, nil, testInternalBearer)
	ctx := metadata.AppendToOutgoingContext(controlRPCContext(t, testInternalBearer), "authorization", "Bearer "+testInternalBearer)
	if _, err := client.Health(ctx, &controlv1.HealthRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("duplicate authorization accepted: %v", err)
	}
	ctx = controlRPCContext(t, testInternalBearer)
	if _, err := client.ShadowEvaluate(ctx, &controlv1.ShadowEvaluateRequest{CanonicalInput: make([]byte, (1<<20)+1)}); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("oversized request reached evaluation: %v", err)
	}
	if response, err := client.Health(ctx, &controlv1.HealthRequest{}); err != nil || !response.Ok {
		t.Fatalf("rejected request damaged the control listener: %v %v", response, err)
	}
}

func controlRPCContext(t *testing.T, bearer string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	if bearer != "" {
		ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+bearer)
	}
	return ctx
}

func TestControlRPCRequiresConfiguredBearerBeforeEveryRead(t *testing.T) {
	client := controlRPCClient(t, nil, testInternalBearer)
	for _, bearer := range []string{"", "incorrect", testInternalBearer} {
		ctx := controlRPCContext(t, bearer)
		health, err := client.Health(ctx, &controlv1.HealthRequest{})
		if bearer != testInternalBearer {
			if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("health auth: %v", err)
			}
		} else if err != nil || !health.Ok || health.MutationAuthority != "python" {
			t.Fatalf("health: %v %v", health, err)
		}
		_, err = client.GetBackupPolicyRecipients(ctx, &controlv1.BackupPolicyRecipientsRequest{})
		want := codes.Unauthenticated
		if bearer == testInternalBearer {
			want = codes.Unavailable
		}
		if status.Code(err) != want {
			t.Fatalf("recipient auth %q: %v", bearer, err)
		}
	}
	unconfigured := controlRPCClient(t, nil, "")
	if _, err := unconfigured.Health(controlRPCContext(t, testInternalBearer), &controlv1.HealthRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unconfigured auth: %v", err)
	}
}

func TestControlRPCRecipientSnapshotUsesDurableAuthorityAndRetainsEmptyGroups(t *testing.T) {
	control := applyStore(t)
	private, public := applySigner(t)
	server, httpClient := applyServer(t, control, applyOptions(public))
	applyPolicyRecord(t, server, httpClient, control, private, public, "a", "p-rpc-1", map[string]any{
		"enabled": true, "protection": map[string]any{"recipients": []any{"age1a", "age1b"}},
	})
	applyPolicyRecord(t, server, httpClient, control, private, public, "b", "p-rpc-2", map[string]any{
		"enabled": true, "protection": map[string]any{}, "encryption": map[string]any{"recipients": []any{"age1legacy"}},
	})
	applyPolicyRecord(t, server, httpClient, control, private, public, "c", "p-rpc-3", map[string]any{
		"enabled": 0, "protection": map[string]any{"recipients": []any{"age1disabled"}},
	})
	client := controlRPCClient(t, control, testInternalBearer)
	response, err := client.GetBackupPolicyRecipients(controlRPCContext(t, testInternalBearer), &controlv1.BackupPolicyRecipientsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if !response.Authoritative || response.PolicyCount != 3 || response.EnabledPolicyCount != 2 || len(response.EnabledRecipientGroups) != 2 ||
		!reflect.DeepEqual(response.Recipients, []string{"age1a", "age1b", "age1legacy", "age1disabled"}) || len(response.EnabledRecipientGroups[1].Recipients) != 0 {
		t.Fatalf("snapshot lost policy semantics: %+v", response)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetBackupPolicyRecipients(controlRPCContext(t, testInternalBearer), &controlv1.BackupPolicyRecipientsRequest{}); status.Code(err) != codes.DataLoss {
		t.Fatalf("closed source reported as empty: %v", err)
	}
}

func TestControlRPCRefusesShadowPolicyAndDoesNotPersistEvaluation(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "rpc-shadow"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	client := controlRPCClient(t, control, testInternalBearer)
	ctx := controlRPCContext(t, testInternalBearer)
	if _, err := client.GetBackupPolicyRecipients(ctx, &controlv1.BackupPolicyRecipientsRequest{}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("shadow source: %v", err)
	}
	for _, domain := range []string{"", "control", "scheduler", "risk", "wave", "federation"} {
		result, err := client.ShadowEvaluate(ctx, &controlv1.ShadowEvaluateRequest{Domain: domain, CanonicalInput: []byte("{}")})
		if err != nil || !result.MutationDenied || len(result.DecisionDigest) != 64 {
			t.Fatalf("evaluation %q: %v %v", domain, result, err)
		}
	}
	for _, request := range []*controlv1.ShadowEvaluateRequest{{CanonicalInput: []byte("[]")}, {CanonicalInput: []byte("null")}, {Domain: "mutate", CanonicalInput: []byte("{}")}} {
		if _, err := client.ShadowEvaluate(ctx, request); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid evaluation: %v", err)
		}
	}
	snapshot, err := control.ExportSnapshot()
	if err != nil || len(snapshot.Records) != 0 {
		t.Fatalf("read RPC changed control state: %v %v", snapshot.Records, err)
	}
}
