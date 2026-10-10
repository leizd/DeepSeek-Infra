package protocol

import (
	"bytes"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func TestGeneratedActionFenceHasStableWireBytes(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 7}
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(fence)
	if err != nil {
		t.Fatalf("marshal generated fence: %v", err)
	}
	want := []byte("\x0a\x05act-1\x10\x07")
	if !bytes.Equal(encoded, want) {
		t.Fatalf("wire bytes = %x, want %x", encoded, want)
	}
	decoded := &commonv1.ActionFence{}
	if err := proto.Unmarshal(encoded, decoded); err != nil {
		t.Fatalf("unmarshal generated fence: %v", err)
	}
	if !proto.Equal(decoded, fence) {
		t.Fatalf("round trip = %v, want %v", decoded, fence)
	}
	if got := string(commonv1.File_common_v1_common_proto.Package()); got != "deepseek.common.v1" {
		t.Fatalf("descriptor package = %q", got)
	}
}

func TestGeneratedWorkerServiceContractIsLinked(t *testing.T) {
	var client actionv1.WorkerClient
	if client != nil {
		t.Fatal("zero WorkerClient must be nil")
	}
}
