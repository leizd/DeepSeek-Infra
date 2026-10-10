package a2a

import (
	"encoding/json"
	"errors"
	"os"
	"testing"
	"time"
)

func TestMessageAdmissionMatchesPythonOracle(t *testing.T) {
	raw, err := os.ReadFile("../../../rust/crates/deepseek-gateway/tests/fixtures/a2a_message_oracle.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Message json.RawMessage `json:"message"`
		Text    string          `json:"text"`
	}
	if err = json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	s := openTestStore(t, t.TempDir(), time.Now)
	for _, tc := range cases {
		_, err := s.Submit("reasoner", "", tc.Message)
		if tc.Text == "" && !errors.Is(err, ErrInvalidTask) || tc.Text != "" && err != nil {
			t.Fatalf("message=%s expected text=%q error=%v", tc.Message, tc.Text, err)
		}
	}
}
