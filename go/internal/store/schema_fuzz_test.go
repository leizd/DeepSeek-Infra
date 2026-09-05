package store

import "testing"

func FuzzLegalTransition(f *testing.F) {
	f.Add("peer", "PENDING", "VERIFIED")
	f.Add("action", "PENDING", "CLAIMED")
	f.Add("wave", "PLANNED", "RUNNING")
	f.Fuzz(func(t *testing.T, domain, from, to string) {
		_ = LegalTransition(domain, from, to)
	})
}

func FuzzLegalCutoverTransition(f *testing.F) {
	f.Add("shadow", "dual_evaluate")
	f.Add("shadow", "go_authoritative")
	f.Add("dual_evaluate", "shadow")
	f.Fuzz(func(t *testing.T, from, to string) {
		_ = LegalCutoverTransition(CutoverState(from), CutoverState(to))
	})
}

func FuzzValidRecordID(f *testing.F) {
	f.Add("act-1")
	f.Add(`..\writer`)
	f.Fuzz(func(t *testing.T, id string) {
		_ = ValidRecordID(id)
	})
}
