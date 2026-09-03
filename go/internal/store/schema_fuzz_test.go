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

func FuzzValidRecordID(f *testing.F) {
	f.Add("act-1")
	f.Add(`..\writer`)
	f.Fuzz(func(t *testing.T, id string) {
		_ = ValidRecordID(id)
	})
}
