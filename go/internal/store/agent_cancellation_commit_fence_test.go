package store

import (
	"errors"
	"testing"
)

func TestCancellationFinalCommitAndReplayStillRequireWriterLease(t *testing.T) {
	for _, replay := range []bool{false, true} {
		name := "new-commit"
		if replay {
			name = "replay"
		}
		t.Run(name, func(t *testing.T) {
			control, input := cancellationSettlementFixture(t, true)
			if replay {
				if _, err := control.SettleAgentCancellation(input); err != nil {
					t.Fatal(err)
				}
			}
			clock, start := control.now, control.now()
			before, calls := countControlEvents(t, control), 0
			limit := 3
			if replay {
				limit = 2
			}
			control.now = func() int64 {
				calls++
				if calls >= limit {
					return start + control.leaseSeconds
				}
				return start
			}
			_, err := control.SettleAgentCancellation(input)
			control.now = clock
			if !errors.Is(err, ErrWriterFenceHeld) || countControlEvents(t, control) != before {
				t.Fatal("final writer expiry published cancellation", err)
			}
			if _, err := control.SettleAgentCancellation(input); err != nil {
				t.Fatal("final writer expiry poisoned retry", err)
			}
		})
	}
}

func TestCancellationWriterRevocationInsideTransactionRollsBackCustody(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	before := countControlEvents(t, control)
	if _, err := control.db.Exec(`CREATE TEMP TRIGGER cancellation_writer_revocation AFTER INSERT ON control_operator_mutations BEGIN UPDATE control_writer SET owner_instance_id='revoked-offline-writer' WHERE singleton=1; END`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.SettleAgentCancellation(input); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatal("revoked writer committed cancellation", err)
	}
	if _, err := control.db.Exec("DROP TRIGGER cancellation_writer_revocation"); err != nil {
		t.Fatal(err)
	}
	resources, err := control.GetResourceLeases(input.ActionID)
	if err != nil || len(resources) != 1 || countControlEvents(t, control) != before {
		t.Fatal("writer revocation discarded custody", err)
	}
	if _, err := control.SettleAgentCancellation(input); err != nil {
		t.Fatal("revocation poisoned valid retry", err)
	}
}

func TestCancellationBrokenActionAuthorityReadCannotPublishTerminal(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	before := countControlEvents(t, control)
	if _, err := control.db.Exec(`CREATE TEMP VIEW control_cutover AS SELECT domain,CASE WHEN domain='action' THEN NULL ELSE state END AS state,revision,epoch,fencing_token,previous_owner,owner,transfer_id,writer_fencing_token,updated_at FROM main.control_cutover`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.SettleAgentCancellation(input); err == nil {
		t.Fatal("broken action authority read accepted")
	}
	if _, err := control.db.Exec("DROP VIEW temp.control_cutover"); err != nil {
		t.Fatal(err)
	}
	if countControlEvents(t, control) != before {
		t.Fatal("broken authority read published terminal")
	}
	if _, err := control.SettleAgentCancellation(input); err != nil {
		t.Fatal("authority read fault poisoned retry", err)
	}
}
