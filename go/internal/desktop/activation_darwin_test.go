//go:build darwin && !ios

package desktop

import "testing"

func TestNativeMacActivationPolicyRequiresObservedRegularState(t *testing.T) {
	for _, test := range []struct {
		name                    string
		before, after           int64
		switchResult, wantError bool
		wantCalls               int
	}{
		{"already-regular", 0, 0, false, false, 0},
		{"successful-switch", 2, 0, true, false, 1},
		{"observed-regular-with-false-return", 2, 0, false, false, 1},
		{"true-return-without-regular-state", 2, 2, true, true, 1},
		{"denied-switch", 2, 2, false, true, 1},
		{"unknown-state", -1, -1, false, true, 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			state, calls := test.before, 0
			err := ensureMacRegularActivationPolicy(func() int64 { return state }, func() bool { calls++; state = test.after; return test.switchResult })
			if (err != nil) != test.wantError || calls != test.wantCalls {
				t.Fatal("activation state was not enforced", err, calls)
			}
		})
	}
}
