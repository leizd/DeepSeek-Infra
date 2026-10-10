package shadow

import (
	"encoding/json"
	"os"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

type ReplayCase struct {
	Name   string `json:"name"`
	Digest string `json:"digest"`
}

type ReplayReport struct {
	Kernel         string       `json:"kernel"`
	MutationDenied bool         `json:"mutationDenied"`
	Cases          []ReplayCase `json:"cases"`
	StoreDigest    string       `json:"storeDigest,omitempty"`
}

func Replay(control *store.Control, fixturePath string) (ReplayReport, error) {
	raw, err := os.ReadFile(fixturePath)
	if err != nil {
		return ReplayReport{}, err
	}
	var fixture struct {
		Kernel string `json:"kernel"`
		Cases  []struct {
			Name     string         `json:"name"`
			Snapshot map[string]any `json:"snapshot"`
			Expect   struct {
				Digest string `json:"digest"`
			} `json:"expect"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		return ReplayReport{}, err
	}
	report := ReplayReport{Kernel: fixture.Kernel, MutationDenied: true}
	for _, item := range fixture.Cases {
		decision, err := Evaluate(item.Snapshot)
		if err != nil {
			return ReplayReport{}, err
		}
		digest, _ := decision["digest"].(string)
		if item.Expect.Digest != "" && digest != item.Expect.Digest {
			return ReplayReport{}, errDigestMismatch(item.Name, digest, item.Expect.Digest)
		}
		if err := Persist(control, item.Snapshot, decision); err != nil {
			return ReplayReport{}, err
		}
		report.Cases = append(report.Cases, ReplayCase{Name: item.Name, Digest: digest})
	}
	if control != nil {
		snap, err := control.ExportSnapshot()
		if err != nil {
			return ReplayReport{}, err
		}
		report.StoreDigest = snap.Digest
	}
	return report, nil
}

type digestMismatch struct {
	name, got, want string
}

func errDigestMismatch(name, got, want string) error {
	return digestMismatch{name: name, got: got, want: want}
}

func (err digestMismatch) Error() string {
	return "pythonDecisionDigest mismatch for " + err.name + ": go=" + err.got + " python=" + err.want
}
