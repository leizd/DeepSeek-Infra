//go:build integration

package worker

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/action"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

// This is an offline qualification signer, never a production key. The test
// exercises real Go control ownership, Rust TLS and provider bytes; production
// Rust key custody and whole-fleet admission are separate unfinished gates.
func TestRustWorkerPromotedControlWritesAndRecoversRealProviders(t *testing.T) {
	binaryPath := os.Getenv("DEEPSEEK_TEST_RUST_WORKER_BINARY")
	endpoints := strings.Split(os.Getenv("DEEPSEEK_NATIVE_S3_ENDPOINTS"), ",")
	bucket := os.Getenv("DEEPSEEK_NATIVE_S3_BUCKET")
	if binaryPath == "" || len(endpoints) != 3 || bucket == "" || os.Getenv("AWS_ACCESS_KEY_ID") == "" || os.Getenv("AWS_SECRET_ACCESS_KEY") == "" {
		t.Fatal("default worker binary and three isolated real providers are required")
	}
	for index, endpoint := range endpoints {
		t.Run(strconv.Itoa(index), func(t *testing.T) {
			control, promotionKey, checkpoint := isolatedLiveControl(t)
			defer control.Close()
			seed := bytes.Repeat([]byte{42}, ed25519.SeedSize)
			signer := ed25519.NewKeyFromSeed(seed)
			public := base64.RawURLEncoding.EncodeToString(signer.Public().(ed25519.PublicKey))
			writerFence := control.Writer().FencingToken
			prefix := "go-control-" + randomHex64(t)[:16]
			payload := []byte("Go durable admission reaches the default Rust worker and real S3")
			digest := sha256.Sum256(payload)
			identity := sha256.New()
			_, _ = identity.Write([]byte("deepseek-infra:s3-target-v1\x00"))
			for _, field := range []string{strings.TrimRight(endpoint, "/"), "us-east-1", bucket, prefix} {
				_ = binary.Write(identity, binary.BigEndian, uint64(len(field)))
				_, _ = identity.Write([]byte(field))
			}
			request := &actionv1.StorageMutationRequest{
				Fence:       &commonv1.ActionFence{ActionId: "go-provider-action", ExecutionEpoch: 1},
				OperationId: randomHex64(t), RequestId: randomHex64(t), Nonce: randomHex64(t),
				MutationType: "PUT_CHUNK", Provider: "s3", TargetIdentity: hex.EncodeToString(identity.Sum(nil)),
				Bucket: bucket, Prefix: prefix, ObjectKey: "native-object", PayloadDigest: hex.EncodeToString(digest[:]),
				ExpectedLength: uint64(len(payload)), Payload: payload, SchemaVersion: 1,
				Precondition: &actionv1.StoragePrecondition{ConditionType: actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_CREATE_ONLY},
			}
			root := t.TempDir()
			material := generateTLSMaterial(t, root, "deepseek-worker.test")
			reservation, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			target := reservation.Addr().String()
			if err := reservation.Close(); err != nil {
				t.Fatal(err)
			}
			expires := time.Now().UTC().Add(10 * time.Minute).Truncate(time.Second)
			cfg := TLSDialConfig{Target: target, TrustRootFile: material.caFile, ServerName: "deepseek-worker.test", BearerToken: tlsTestSecret, ExpiresAt: expires}
			start := func() func() {
				cmd := exec.Command(binaryPath)
				hideTestWorker(cmd)
				for _, name := range []string{"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"} {
					if value, found := os.LookupEnv(name); found {
						cmd.Env = append(cmd.Env, name+"="+value)
					}
				}
				cmd.Env = append(cmd.Env,
					"DEEPSEEK_WORKER_LISTEN="+target, "DEEPSEEK_WORKER_STATE_ROOT="+root,
					"DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY="+public,
					"DEEPSEEK_WORKER_AUTHORITY_FLEET_ID=fleet-a", "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT=test",
					"DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN="+strconv.FormatInt(writerFence, 10),
					EnvWorkerTLSCertFile+"="+material.certFile, EnvWorkerTLSKeyFile+"="+material.keyFile,
					EnvWorkerServiceBearer+"="+tlsTestSecret, EnvWorkerServiceBearerExpires+"="+expires.Format("2006-01-02T15:04:05Z"),
					EnvWorkerServiceName+"=go-control-plane", EnvWorkerServiceRole+"=controller",
					"DEEPSEEK_WORKER_S3_ENDPOINT="+endpoint, "DEEPSEEK_WORKER_S3_BUCKET="+bucket,
					"DEEPSEEK_WORKER_S3_PREFIX="+prefix, "DEEPSEEK_WORKER_S3_REGION=us-east-1",
					"DEEPSEEK_WORKER_S3_ACCESS_KEY="+os.Getenv("AWS_ACCESS_KEY_ID"),
					"DEEPSEEK_WORKER_S3_SECRET_KEY="+os.Getenv("AWS_SECRET_ACCESS_KEY"),
					"DEEPSEEK_WORKER_S3_ALLOW_HTTP_LOOPBACK=true")
				var stderr bytes.Buffer
				cmd.Stderr = &stderr
				cmd.WaitDelay = 2 * time.Second
				if err := cmd.Start(); err != nil {
					t.Fatal(err)
				}
				exited := make(chan error, 1)
				go func() { exited <- cmd.Wait() }()
				stopped := false
				stop := func() {
					if !stopped {
						stopped = true
						_ = cmd.Process.Kill()
						<-exited
					}
				}
				t.Cleanup(stop)
				deadline := time.Now().Add(10 * time.Second)
				for {
					connection, err := net.DialTimeout("tcp", target, 100*time.Millisecond)
					if err == nil {
						_ = connection.Close()
						return stop
					}
					select {
					case err := <-exited:
						stopped = true
						t.Fatalf("worker exited before readiness: %v; %s", err, stderr.String())
					default:
					}
					if time.Now().After(deadline) {
						t.Fatal("worker readiness timed out")
					}
					time.Sleep(25 * time.Millisecond)
				}
			}
			stop := start()
			client, err := DialTLS(cfg)
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			coordinator := action.NewCoordinator(control, client, action.WithAuthoritative(true))
			if _, err := coordinator.ExecuteStorageAction(ctx, request.Fence.ActionId, request); !errors.Is(err, store.ErrCutoverNotAuthorized) {
				t.Fatalf("unpromoted control reached dispatch: %v", err)
			}
			if status, _, _, _ := readIsolatedS3(t, endpoint, bucket, prefix, request.ObjectKey); status != http.StatusNotFound {
				t.Fatalf("refused dispatch created an object: %d", status)
			}
			promoteLiveAction(t, control, promotionKey, checkpoint)
			if err := control.Put(store.Record{Domain: "action", ID: request.Fence.ActionId, Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
				t.Fatal(err)
			}
			unsigned := liveUnsigned(t, request.Fence, writerFence)
			unsigned["schema"], unsigned["operation"], unsigned["payload"] = store.AuthorityRequestSchema, "install-epoch", map[string]any{}
			_, raw, err := store.SignAuthorityRequest(unsigned, signer, public)
			if err != nil {
				t.Fatal(err)
			}
			if err := client.InstallAuthoritativeEpoch(ctx, request.Fence, raw); err != nil {
				t.Fatalf("Go signed epoch installation: %v", err)
			}
			unsigned = liveUnsigned(t, request.Fence, writerFence)
			unsigned["schema"], unsigned["operation"] = store.StorageOperationGrantSchema, store.StorageOperationGrantPut
			unsigned["operationId"], unsigned["requestId"], unsigned["nonce"] = request.OperationId, request.RequestId, request.Nonce
			unsigned["payload"] = map[string]any{"mutationType": request.MutationType, "provider": request.Provider, "targetIdentity": request.TargetIdentity,
				"bucket": bucket, "prefix": prefix, "objectKey": request.ObjectKey, "objectDigest": request.PayloadDigest, "expectedLength": int64(len(payload)),
				"conditionType": "CREATE_ONLY", "expectedEtag": "", "claimRevision": int64(2)}
			_, request.CanonicalAuthorization, err = store.SignStorageOperationGrant(unsigned, signer, public)
			if err != nil {
				t.Fatal(err)
			}
			response, err := coordinator.ExecuteStorageAction(ctx, request.Fence.ActionId, request)
			if err != nil {
				t.Fatalf("promoted Go control to real Rust/provider: %v", err)
			}
			record, exists, err := control.Get("action", request.Fence.ActionId)
			if err != nil || !exists || record.State != "SUCCEEDED" {
				t.Fatalf("control did not settle: exists=%v state=%s err=%v", exists, record.State, err)
			}
			dispatch, bound, err := control.GetStorageDispatch(request.Fence.ActionId, 1)
			if err != nil || !bound || dispatch.Intent.OperationID != request.OperationId || dispatch.Intent.TargetIdentity != request.TargetIdentity {
				t.Fatalf("dispatch identity was not durable: bound=%v err=%v", bound, err)
			}
			status, body, etag, version := readIsolatedS3(t, endpoint, bucket, prefix, request.ObjectKey)
			if status != http.StatusOK || !bytes.Equal(body, payload) || strings.Trim(etag, "\"") != strings.Trim(response.Etag, "\"") {
				t.Fatalf("actual provider bytes/etag mismatch: status=%d", status)
			}
			if strconv.Itoa(index) == os.Getenv("DEEPSEEK_TEST_VERSIONED_PROVIDER_INDEX") && version == "" {
				t.Fatal("versioned provider returned no version")
			}
			stop()
			_ = client.Close()
			start()
			client, err = DialTLS(cfg)
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()
			queried, err := client.QueryStorageEffect(ctx, request.Fence, request.OperationId, "")
			if err != nil || !proto.Equal(response, queried) {
				t.Fatalf("receipt changed after forced restart: %v", err)
			}
			replayed, err := client.ExecuteStorageMutation(ctx, request, "")
			if err != nil || !proto.Equal(response, replayed) {
				t.Fatalf("completed replay changed receipt: %v", err)
			}
			status, body, replayETag, replayVersion := readIsolatedS3(t, endpoint, bucket, prefix, request.ObjectKey)
			if status != http.StatusOK || !bytes.Equal(body, payload) || replayETag != etag || replayVersion != version {
				t.Fatal("completed replay changed the actual provider object")
			}
			t.Logf("real Go control/provider %d: durable promotion, signed epoch/grant, TLS, SUCCEEDED, forced restart, unchanged object version", index)
		})
	}
}

func liveUnsigned(t *testing.T, fence *commonv1.ActionFence, fencing int64) map[string]any {
	t.Helper()
	now := time.Now().UTC().Truncate(time.Second)
	return map[string]any{"schemaVersion": 1, "domain": "action", "actionId": fence.ActionId, "executionEpoch": fence.ExecutionEpoch,
		"fencingToken": fencing, "revision": int64(1), "requestId": randomHex64(t), "nonce": randomHex64(t),
		"issuedAt": now.Format("2006-01-02T15:04:05Z"), "expiresAt": now.Add(5 * time.Minute).Format("2006-01-02T15:04:05Z"),
		"runtime": store.RuntimeGo, "mode": store.ModeShadow, "fleetId": "fleet-a", "environment": "test", "role": "control-plane"}
}

func isolatedLiveControl(t *testing.T) (*store.Control, ed25519.PrivateKey, *store.AuthorityCheckpoint) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "native-provider-qualification", AuthorizeCutover: true,
		PromotionSignerPublicKey: base64.RawURLEncoding.EncodeToString(public), FleetID: "fleet-a", Environment: "test"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	checkpoint := &store.AuthorityCheckpoint{Schema: store.ControlAuthoritySchema, AuthorityGeneration: 1, CreatedAt: time.Now().UTC().Format("2006-01-02T15:04:05Z"),
		ControlSchemaVersion: 7, Policies: []any{}, Targets: []any{}, ReceiptMutationGenerations: map[string]int64{}, PromotionEpochs: map[string]int64{}, DrainGenerations: map[string]int64{}, PlacementGenerations: map[string]int64{}}
	checkpoint.PayloadDigest, err = store.ComputePayloadDigest(checkpoint)
	if err != nil {
		t.Fatal(err)
	}
	checkpoint.Digest, err = store.ComputeCheckpointDigest(checkpoint)
	if err != nil {
		t.Fatal(err)
	}
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim authority: advanced=%v err=%v", advanced, err)
	}
	return control, private, checkpoint
}

func promoteLiveAction(t *testing.T, control *store.Control, private ed25519.PrivateKey, authority *store.AuthorityCheckpoint) {
	t.Helper()
	for _, state := range []store.CutoverState{store.CutoverDualEvaluate, store.CutoverGoAuthoritative} {
		current, err := control.GetCutover("action")
		if err != nil {
			t.Fatal(err)
		}
		request := store.CutoverTransition{Domain: "action", To: state, ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch,
			FencingToken: current.FencingToken, TransferID: "actual-provider-" + string(state), Authority: authority}
		if store.IsDomainGoAuthoritative(state) {
			artifact := store.PromotionArtifactForTransition(request, current, time.Now().Unix(), "fleet-a", "test")
			request.Promotion, err = store.SignPromotionArtifact(artifact, private)
			if err != nil {
				t.Fatal(err)
			}
		}
		if _, err := control.TransitionCutover(request); err != nil {
			t.Fatal(err)
		}
	}
}

// Test-only SigV4 GET verifies provider bytes independently of the worker journal.
func readIsolatedS3(t *testing.T, endpoint, bucket, prefix, key string) (int, []byte, string, string) {
	t.Helper()
	u, err := url.Parse(endpoint)
	if err != nil || u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.Path != "" || u.User != nil {
		t.Fatal("only explicit isolated loopback providers are allowed")
	}
	u.Path = "/" + bucket + "/" + prefix + "/" + key
	now := time.Now().UTC()
	date := now.Format("20060102")
	stamp := now.Format("20060102T150405Z")
	empty := sha256.Sum256(nil)
	bodyDigest := hex.EncodeToString(empty[:])
	headers := "host:" + u.Host + "\nx-amz-content-sha256:" + bodyDigest + "\nx-amz-date:" + stamp + "\n"
	canonical := "GET\n" + u.EscapedPath() + "\n\n" + headers + "\nhost;x-amz-content-sha256;x-amz-date\n" + bodyDigest
	canonicalDigest := sha256.Sum256([]byte(canonical))
	scope := date + "/us-east-1/s3/aws4_request"
	message := "AWS4-HMAC-SHA256\n" + stamp + "\n" + scope + "\n" + hex.EncodeToString(canonicalDigest[:])
	mac := func(key []byte, value string) []byte {
		h := hmac.New(sha256.New, key)
		_, _ = h.Write([]byte(value))
		return h.Sum(nil)
	}
	signingKey := mac(mac(mac(mac([]byte("AWS4"+os.Getenv("AWS_SECRET_ACCESS_KEY")), date), "us-east-1"), "s3"), "aws4_request")
	request, err := http.NewRequest(http.MethodGet, u.String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("x-amz-content-sha256", bodyDigest)
	request.Header.Set("x-amz-date", stamp)
	request.Header.Set("Authorization", "AWS4-HMAC-SHA256 Credential="+os.Getenv("AWS_ACCESS_KEY_ID")+"/"+scope+", SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="+hex.EncodeToString(mac(signingKey, message)))
	client := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{Proxy: nil}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	defer client.CloseIdleConnections()
	response, err := client.Do(request)
	if err != nil {
		t.Fatal("isolated provider GET failed")
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1024*1024))
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, body, response.Header.Get("ETag"), response.Header.Get("x-amz-version-id")
}
