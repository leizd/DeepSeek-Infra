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
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
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

// Rust provisions and holds the worker signing key; Go receives public metadata
// and signatures only. Promotion uses an isolated offline administrative fixture.
// This remains provider qualification, not a production or whole-fleet cutover.
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
			public, bundleFile, passwordFile := provisionNativeControlSigner(t, binaryPath, root)
			t.Setenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY", public)
			t.Setenv("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", "fleet-a")
			t.Setenv("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT", "test")
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
					"DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE="+bundleFile,
					"DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE="+passwordFile,
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
			// Exercise the renewable action/resource claim through the same Rust
			// custody path, without a caller-supplied grant or Go private key.
			leased := proto.Clone(request).(*actionv1.StorageMutationRequest)
			leased.Fence = &commonv1.ActionFence{ActionId: "go-provider-leased-action", ExecutionEpoch: 1}
			leased.OperationId, leased.RequestId, leased.Nonce = randomHex64(t), randomHex64(t), randomHex64(t)
			leased.ObjectKey = "native-leased-对象<>&\u2028"
			if err = control.Put(store.Record{Domain: "action", ID: leased.Fence.ActionId, Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
				t.Fatal(err)
			}
			claim, err := control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: leased.Fence.ActionId, LeaseSeconds: 60, ResourceKeys: []string{"native-provider-target"}})
			if err != nil {
				t.Fatalf("native action admission: %v", err)
			}
			if _, err = coordinator.ExecuteClaimedStorageAction(ctx, claim.Lease, leased); err != nil {
				t.Fatalf("leased native custody dispatch: %v", err)
			}
			leasedRecord, found, err := control.Get("action", leased.Fence.ActionId)
			if err != nil || !found || leasedRecord.State != "VERIFYING" {
				t.Fatalf("leased action verification state: %v", err)
			}
			leasedStatus, leasedBody, _, _ := readIsolatedS3(t, endpoint, bucket, prefix, leased.ObjectKey)
			if leasedStatus != http.StatusOK || !bytes.Equal(leasedBody, payload) {
				t.Fatalf("leased custody provider read mismatch: status=%d body=%q", leasedStatus, leasedBody)
			}
			stop()
			_ = client.Close()
			start()
			client, err = DialTLS(cfg)
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()
			request, err = client.AuthorizeStorage(ctx, request, 2, writerFence)
			if err != nil {
				t.Fatalf("Rust custody replay after forced restart: %v", err)
			}
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
			t.Logf("real Go control/provider %d: durable promotion, Rust key custody and signed epoch/grant, TLS, SUCCEEDED, forced restart, unchanged object version", index)
		})
	}
}

func provisionNativeControlSigner(t *testing.T, workerBinary, root string) (string, string, string) {
	t.Helper()
	bundleFile := filepath.Join(root, "control-key.encrypted.json")
	passwordFile := filepath.Join(root, "control-key.credential")
	if err := os.WriteFile(passwordFile, []byte("isolated-native-control-passphrase-32bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	name := "deepseek-control-signer-init"
	if strings.HasSuffix(workerBinary, ".exe") {
		name += ".exe"
	}
	cmd := exec.Command(filepath.Join(filepath.Dir(workerBinary), name))
	hideTestWorker(cmd)
	for _, key := range []string{"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"} {
		if value, ok := os.LookupEnv(key); ok {
			cmd.Env = append(cmd.Env, key+"="+value)
		}
	}
	cmd.Env = append(cmd.Env, "DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE="+bundleFile, "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE="+passwordFile, "DEEPSEEK_WORKER_AUTHORITY_FLEET_ID=fleet-a", "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT=test")
	raw, err := cmd.Output()
	if err != nil {
		t.Fatal("native custody provisioning failed")
	}
	var binding map[string]string
	if err = json.Unmarshal(raw, &binding); err != nil || binding["signerPublicKey"] == "" || binding["fleetId"] != "fleet-a" || binding["environment"] != "test" {
		t.Fatal("native custody public metadata invalid")
	}
	return binding["signerPublicKey"], bundleFile, passwordFile
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
	u.RawPath = isolatedS3CanonicalPath(u.Path)
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

// S3 SigV4 permits only unreserved bytes and object-key slashes; URL.EscapedPath
// alone also leaves reserved '&' and '+' bytes unescaped.
// https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html
func isolatedS3CanonicalPath(path string) string {
	const hexDigits = "0123456789ABCDEF"
	var result strings.Builder
	for i := 0; i < len(path); i++ {
		b := path[i]
		if b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z' || b >= '0' && b <= '9' || strings.ContainsRune("-._~/", rune(b)) {
			result.WriteByte(b)
		} else {
			result.WriteByte('%')
			result.WriteByte(hexDigits[b>>4])
			result.WriteByte(hexDigits[b&15])
		}
	}
	return result.String()
}

func TestIsolatedS3CanonicalPath(t *testing.T) {
	path := "/bucket/prefix/对象<>&\u2028 +%?#/A-Z_a.~"
	want := "/bucket/prefix/%E5%AF%B9%E8%B1%A1%3C%3E%26%E2%80%A8%20%2B%25%3F%23/A-Z_a.~"
	if got := isolatedS3CanonicalPath(path); got != want {
		t.Fatalf("S3 canonical path: %q", got)
	}
	u := &url.URL{Scheme: "http", Host: "127.0.0.1", Path: path, RawPath: want}
	if u.EscapedPath() != want {
		t.Fatal("canonical path did not survive URL encoding")
	}
}
