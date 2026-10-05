package store

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"strings"
	"testing"

	modernsqlite "modernc.org/sqlite"
)

var errInventoryReadInterrupted = errors.New("inventory database read interrupted")

// Fail the transport of a real SQLite result or commit, after the database has
// been populated normally. This models an I/O failure without changing the
// schema or bypassing the control store's normal validation and transaction.
type inventoryReadFault struct {
	query                                      string
	cursor, malformed, commit, armed, injected bool
	skip                                       int
	matched                                    int
}

type inventoryFaultConnector struct {
	driver.Connector
	fault *inventoryReadFault
}

func (connector inventoryFaultConnector) Connect(ctx context.Context) (driver.Conn, error) {
	conn, err := connector.Connector.Connect(ctx)
	if err != nil {
		return nil, err
	}
	return inventoryFaultConnection{Conn: conn, fault: connector.fault}, nil
}

type inventoryFaultConnection struct {
	driver.Conn
	fault *inventoryReadFault
}

func (conn inventoryFaultConnection) QueryContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Rows, error) {
	if strings.Contains(query, conn.fault.query) {
		conn.fault.matched++
	}
	if conn.fault.armed && strings.Contains(query, conn.fault.query) && !conn.fault.commit {
		if conn.fault.skip > 0 {
			conn.fault.skip--
			return conn.Conn.(driver.QueryerContext).QueryContext(ctx, query, args)
		}
		conn.fault.armed, conn.fault.injected = false, true
		if !conn.fault.cursor && !conn.fault.malformed {
			return nil, errInventoryReadInterrupted
		}
		rows, err := conn.Conn.(driver.QueryerContext).QueryContext(ctx, query, args)
		if err != nil {
			return nil, err
		}
		return inventoryFaultRows{Rows: rows, malformed: conn.fault.malformed}, nil
	}
	return conn.Conn.(driver.QueryerContext).QueryContext(ctx, query, args)
}

func TestAuthoritativeTargetLastReadMustSucceedAfterSchemaValidation(t *testing.T) {
	for _, query := range []string{"FROM control_cutover WHERE domain = ?", "SELECT manifest_digest,transfer_id,health_digest,row_count",
		"record_digest, writer_fencing_token, recorded_at, event_id"} {
		t.Run(query, func(t *testing.T) {
			control, checkpoint, dual, imported := importedHealthStore(t, false)
			defer control.Close()
			if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); err != nil {
				t.Fatal(err)
			}
			fault := inventoryReadFault{query: query}
			installInventoryReadFault(t, control, &fault)
			if _, _, err := control.ListAuthoritativeTargets(); err != nil || fault.matched == 0 {
				t.Fatalf("normal read did not reach its source: %v", err)
			}
			// Let all preceding validation reads succeed, then interrupt the last
			// authoritative read. An earlier integrity check is not a substitute.
			fault.skip, fault.armed = fault.matched-1, true
			if _, _, err := control.ListAuthoritativeTargets(); err == nil || !fault.injected {
				t.Fatalf("last read interruption was hidden: %v", err)
			}
			if _, health, err := control.ListAuthoritativeTargets(); err != nil || len(health) != 2 {
				t.Fatalf("read recovery lost state: %v", err)
			}
		})
	}
}

func (conn inventoryFaultConnection) ExecContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Result, error) {
	return conn.Conn.(driver.ExecerContext).ExecContext(ctx, query, args)
}

func (conn inventoryFaultConnection) BeginTx(ctx context.Context, opts driver.TxOptions) (driver.Tx, error) {
	tx, err := conn.Conn.(driver.ConnBeginTx).BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	return inventoryFaultTransaction{Tx: tx, fault: conn.fault}, nil
}

type inventoryFaultTransaction struct {
	driver.Tx
	fault *inventoryReadFault
}

func (tx inventoryFaultTransaction) Commit() error {
	if tx.fault.armed && tx.fault.commit {
		tx.fault.armed, tx.fault.injected = false, true
		_ = tx.Tx.Rollback()
		return errInventoryReadInterrupted
	}
	return tx.Tx.Commit()
}

type inventoryFaultRows struct {
	driver.Rows
	malformed bool
}

func (rows inventoryFaultRows) Next(values []driver.Value) error {
	if rows.malformed {
		if err := rows.Rows.Next(values); err != nil {
			return err
		}
		// A lost revision value must never turn a decoded record into a valid
		// current target. The underlying stored row remains intact for recovery.
		values[1] = nil
		return nil
	}
	return errInventoryReadInterrupted
}

func installInventoryReadFault(t *testing.T, control *Control, fault *inventoryReadFault) {
	t.Helper()
	connector, err := modernsqlite.NewConnector(controlDatabaseDSN(control.DatabasePath()))
	if err != nil {
		t.Fatal(err)
	}
	if err := control.db.Close(); err != nil {
		t.Fatal(err)
	}
	control.db = sql.OpenDB(inventoryFaultConnector{Connector: connector, fault: fault})
	control.db.SetMaxOpenConns(1)
}

func TestAuthoritativeTargetReadsCannotSucceedAfterAnInterruptedDatabaseRead(t *testing.T) {
	for _, fault := range []inventoryReadFault{
		{query: "FROM targets ORDER BY id"},
		{query: "FROM targets ORDER BY id", cursor: true},
		{query: "FROM targets ORDER BY id", malformed: true},
		{query: "FROM control_cutover WHERE domain = ?"},
		{query: "SELECT DISTINCT record_id FROM control_events"},
		{query: "SELECT DISTINCT record_id FROM control_events", cursor: true},
		{query: "SELECT manifest_digest,transfer_id,health_digest,row_count"},
		{query: "SELECT manifest_digest,transfer_id,health_digest,row_count", skip: 1},
		{query: "SELECT manifest_bytes,manifest_digest,transfer_id,writer_fencing_token"},
		{query: "FROM backup_target_health ORDER BY target_id"},
		{query: "FROM backup_target_health ORDER BY target_id", cursor: true},
		{commit: true},
	} {
		name := fault.query
		if fault.cursor {
			name += " cursor"
		}
		if fault.malformed {
			name += " lost revision"
		}
		if fault.skip > 0 {
			name += " after validation"
		}
		if fault.commit {
			name = "commit"
		}
		t.Run(name, func(t *testing.T) {
			control, checkpoint, dual, imported := importedHealthStore(t, false)
			defer control.Close()
			if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); err != nil {
				t.Fatal(err)
			}
			fault.armed = true
			installInventoryReadFault(t, control, &fault)
			if _, _, err := control.ListAuthoritativeTargets(); err == nil || !fault.injected {
				t.Fatalf("interrupted target read succeeded or did not reach its I/O boundary: injected=%v error=%v", fault.injected, err)
			}
			// Once storage is readable again, the exact original projection returns.
			targets, health, err := control.ListAuthoritativeTargets()
			if err != nil || len(targets) != 1 || len(health) != 2 || health[1].Status != "blocked" {
				t.Fatalf("read failure damaged retained state: targets=%v health=%v error=%v", targets, health, err)
			}
		})
	}
}

func TestHandbackPublicationStopsWhenItsCommittedProofCannotBeRead(t *testing.T) {
	for _, fault := range []inventoryReadFault{{query: "FROM control_inventory_handbacks WHERE domain=?"}, {commit: true}} {
		control, _, _, imported := importedHealthStore(t, false)
		if _, err := control.RollbackPythonInventory("target", imported.TransferID); err != nil {
			t.Fatal(err)
		}
		fault.armed = true
		installInventoryReadFault(t, control, &fault)
		if _, err := control.ReadPythonInventoryHandback("target", imported.TransferID); err == nil || !fault.injected {
			t.Fatalf("unreadable handback was published: injected=%v error=%v", fault.injected, err)
		}
		if _, err := control.ReadPythonInventoryHandback("target", imported.TransferID); err != nil {
			t.Fatalf("durable handback lost after read recovery: %v", err)
		}
		if err := control.Close(); err != nil {
			t.Fatal(err)
		}
	}
}
