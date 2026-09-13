package worker

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/grpc/metadata"
)

func TestTLSCredentialLifetimeIsBounded(t *testing.T) {
	cfg := TLSDialConfig{Target: "127.0.0.1:1", TrustRootFile: "ca.pem", ServerName: "worker.test", BearerToken: tlsTestSecret, ExpiresAt: time.Now().Add(24 * time.Hour)}
	if err := validateTLSDialConfig(cfg); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("long-lived service credential accepted: %v", err)
	}
}

func TestTLSCredentialExpiryIsRecheckedForEveryRequest(t *testing.T) {
	creds := transportBearer{token: tlsTestSecret, expiresAt: time.Now().Add(time.Minute)}
	if _, err := creds.GetRequestMetadata(context.Background()); err != nil {
		t.Fatal(err)
	}
	creds.expiresAt = time.Now().Add(-time.Second)
	if md, err := creds.GetRequestMetadata(context.Background()); !errors.Is(err, ErrWorkerTLSConfigInvalid) || md != nil {
		t.Fatal("expired credential was returned for a later request")
	}
}

func TestTLSClientRejectsConflictingCredentialSources(t *testing.T) {
	client := &Client{tlsSecured: true, bearerAttached: true}
	for _, tc := range []struct {
		ctx   context.Context
		token string
	}{
		{context.Background(), tlsTestSecret},
		{metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer "+tlsTestSecret), ""},
	} {
		if _, err := client.outgoingContext(tc.ctx, tc.token); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
			t.Fatalf("ambiguous credentials accepted: %v", err)
		}
	}
}

func TestTLSConfigurationFormattingRedactsCredentials(t *testing.T) {
	for _, value := range []any{TLSDialConfig{BearerToken: tlsTestSecret}, transportBearer{token: tlsTestSecret}} {
		for _, format := range []string{"%v", "%+v", "%#v"} {
			if strings.Contains(fmt.Sprintf(format, value), tlsTestSecret) {
				t.Fatal("configuration formatting exposed credential")
			}
		}
	}
}

func TestPlaintextMetadataCredentialRejectedBeforeRPC(t *testing.T) {
	client, err := DialPlaintextLoopback("127.0.0.1:1")
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	for _, method := range []string{"admit", "query", "execute", "query-storage"} {
		t.Run(method, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer "+tlsTestSecret), time.Second)
			defer cancel()
			request := mutationRequest()
			var err error
			switch method {
			case "admit":
				err = client.Admit(ctx, actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, request.Fence)
			case "query":
				_, err = client.QueryEffect(ctx, request.Fence)
			case "execute":
				_, err = client.ExecuteStorageMutation(ctx, request, "")
			case "query-storage":
				_, err = client.QueryStorageEffect(ctx, request.Fence, request.OperationId, "")
			}
			if !errors.Is(err, ErrWorkerPlaintextCredential) {
				t.Fatalf("plaintext metadata guard not applied: %v", err)
			}
		})
	}
}
