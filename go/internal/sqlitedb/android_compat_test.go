//go:build android || deepseek_android

package sqlitedb

import (
	"context"
	"database/sql/driver"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/ncruces/go-sqlite3"
	sqlitedriver "github.com/ncruces/go-sqlite3/driver"
)

type observedConnector struct {
	driver.Connector
	connection *observedConnection
}

type observedConnection struct {
	sqlitedriver.Conn
	closed bool
}

func (connector *observedConnector) Connect(ctx context.Context) (driver.Conn, error) {
	connection, err := connector.Connector.Connect(ctx)
	if err != nil {
		return nil, err
	}
	connector.connection = &observedConnection{Conn: connection.(sqlitedriver.Conn)}
	return connector.connection, nil
}

func (connection *observedConnection) Close() error {
	connection.closed = true
	return connection.Conn.Close()
}

func TestAndroidInitializationFailureClosesRealConnectionBeforeAdmission(t *testing.T) {
	path := filepath.Join(t.TempDir(), "denied.sqlite3")
	base, err := NewConnector(databaseURI(path, "_pragma=busy_timeout(0)"))
	if err != nil {
		t.Fatal(err)
	}
	observed := &observedConnector{Connector: base}
	denied := errors.New("security configuration rejected")
	called := false
	connector := configuredConnector{Connector: observed, initialize: func(raw *sqlite3.Conn) error {
		called = raw != nil
		return denied
	}}
	connection, err := connector.Connect(context.Background())
	if connection != nil || !errors.Is(err, denied) || !called || observed.connection == nil || !observed.connection.closed {
		t.Fatal("partially configured connection escaped admission or remained open", err)
	}
	// Reopen the actual file through the ordinary configured adapter. The failed
	// admission must not leave unusable state or an outstanding native connection.
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec("CREATE TABLE retained (value TEXT)"); err != nil {
		t.Fatal("failed configuration left unusable database state", err)
	}
}

func TestAndroidMalformedFileAuthorityRejectedBeforeOpen(t *testing.T) {
	for _, dsn := range []string{"file://[broken", "file:/tmp/unopened.sqlite3?bad=%GG"} {
		if connection, err := NewConnector(dsn); err == nil || connection != nil {
			t.Fatal("malformed file URI reached SQLite")
		}
	}
}

func TestAndroidUnavailableDatabaseDirectoryDoesNotCreateOrAdmitConnection(t *testing.T) {
	parent := filepath.Join(t.TempDir(), "missing-parent")
	connector, err := NewConnector(filepath.Join(parent, "control.sqlite3"))
	if err != nil {
		t.Fatal(err)
	}
	connection, err := connector.Connect(context.Background())
	if connection != nil || err == nil {
		t.Fatal("unavailable control directory admitted a connection")
	}
	if _, err := os.Stat(parent); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("failed connection unexpectedly created the directory", err)
	}
}

func TestAndroidConnectionFailureRetainsOriginalSQLiteCauseAndNumericDiagnostic(t *testing.T) {
	parent := filepath.Join(t.TempDir(), "unavailable-parent")
	connector, err := NewConnector(filepath.Join(parent, "control.sqlite3"))
	if err != nil {
		t.Fatal(err)
	}
	connection, err := connector.Connect(context.Background())
	var failure *sqlite3.Error
	var errno syscall.Errno
	if connection != nil || !errors.Is(err, sqlite3.CANTOPEN) || !errors.As(err, &failure) || !errors.As(err, &errno) {
		t.Fatal("connection diagnostic lost the original failure identity", err)
	}
	// A platform may return extended CANTOPEN (for example 782), while the
	// primary classification remains CANTOPEN. Preserve the actual numeric code.
	if !strings.Contains(err.Error(), fmt.Sprintf("Android SQLite connection (code=%d errno=%d)", failure.ExtendedCode(), errno)) {
		t.Fatal("connection diagnostic did not match its retained native cause", err)
	}
	if _, err := os.Stat(parent); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("connection diagnostics created unavailable control state", err)
	}
}

type rejectedDiagnosticConnector struct {
	cause error
}

func (connector rejectedDiagnosticConnector) Connect(context.Context) (driver.Conn, error) {
	return nil, connector.cause
}

func (rejectedDiagnosticConnector) Driver() driver.Driver {
	return &sqlitedriver.SQLite{}
}

func TestAndroidConnectionDiagnosticsAcceptTypedErrorCodeValuesWithoutInventingErrno(t *testing.T) {
	for _, test := range []struct {
		name  string
		cause error
		code  sqlite3.ExtendedErrorCode
		errno string
	}{
		{"primary-value", fmt.Errorf("invalid _pragma: %w", sqlite3.IOERR), sqlite3.IOERR.ExtendedCode(), "UNAVAILABLE"},
		{"extended-value", fmt.Errorf("invalid _pragma: %w", sqlite3.IOERR_LOCK), sqlite3.IOERR_LOCK, "UNAVAILABLE"},
		{"known-os-cause", errors.Join(fmt.Errorf("invalid _pragma: %w", sqlite3.IOERR_LOCK), syscall.EINVAL), sqlite3.IOERR_LOCK, fmt.Sprintf("%d", syscall.EINVAL)},
	} {
		t.Run(test.name, func(t *testing.T) {
			initialized := false
			connector := configuredConnector{Connector: rejectedDiagnosticConnector{test.cause}, initialize: func(*sqlite3.Conn) error {
				initialized = true
				return nil
			}}
			connection, err := connector.Connect(context.Background())
			if connection != nil || initialized || !errors.Is(err, test.cause) || !errors.Is(err, sqlite3.IOERR) {
				t.Fatal("diagnostic changed failure identity or admitted a failed connection", err)
			}
			want := fmt.Sprintf("Android SQLite connection (code=%d errno=%s)", test.code, test.errno)
			if !strings.Contains(err.Error(), want) {
				t.Fatalf("typed SQLite cause missing numeric diagnostic; want %s, got %v", want, err)
			}
		})
	}

	// Similar text is insufficient evidence for an SQLite code or an OS errno.
	unknown := errors.New("sqlite3: disk I/O error")
	connector := configuredConnector{Connector: rejectedDiagnosticConnector{unknown}}
	if connection, err := connector.Connect(context.Background()); connection != nil || err != unknown {
		t.Fatal("untyped error text was reclassified as a measured SQLite cause", err)
	}
}
