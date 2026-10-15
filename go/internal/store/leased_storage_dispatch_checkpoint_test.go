package store

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestLeasedDispatchCheckpointAcceptsOnlyBoundedFramedJSON(t *testing.T) {
	want := leasedDispatchCheckpoint{Lease: ActionLease{Epoch: 1}}
	want.Dispatch.Intent = dispatchIntent()
	body, err := json.Marshal(want)
	if err != nil {
		t.Fatal(err)
	}
	framed := leasedDispatchCheckpointPrefix + string(body) + "\n"
	for _, input := range []string{framed, "=== RUN child\n{\"unrelated\":true}\n" + framed} {
		got, err := readLeasedDispatchCheckpoint(strings.NewReader(input))
		if err != nil || got.Lease.Epoch != 1 || got.Dispatch.Intent.OperationID != want.Dispatch.Intent.OperationID {
			t.Fatalf("lost framed checkpoint: %+v %v", got, err)
		}
	}
	for _, input := range []string{
		string(body) + "\n",
		leasedDispatchCheckpointPrefix + "broken\n",
		leasedDispatchCheckpointPrefix + string(body) + " {}\n",
		leasedDispatchCheckpointPrefix + "{\"unexpected\":true}\n",
		strings.Repeat("x", 64*1024+1),
		strings.Repeat("diagnostic\n", 7000) + framed,
	} {
		if _, err := readLeasedDispatchCheckpoint(strings.NewReader(input)); err == nil {
			t.Fatal("accepted missing, malformed, oversized or trailing checkpoint")
		}
	}
}

func TestLeasedDispatchCheckpointReportsTheActualChildFailure(t *testing.T) {
	input := "--- FAIL: child (2.19s)\n    child.go:38: original writer lease expired\nFAIL\n"
	_, err := readLeasedDispatchCheckpoint(strings.NewReader(input))
	if err == nil || !strings.Contains(err.Error(), "original writer lease expired") || !strings.Contains(err.Error(), "without a framed durable checkpoint") {
		t.Fatalf("child failure was hidden by transport parsing: %v", err)
	}
	_, err = readLeasedDispatchCheckpoint(strings.NewReader(strings.Repeat("old noise\n", 600) + "last child failure\n"))
	if err == nil || !strings.Contains(err.Error(), "last child failure") || len(err.Error()) > 2300 {
		t.Fatalf("diagnostics are unbounded or lost the final failure: %v", err)
	}
}
