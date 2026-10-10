//go:build android || deepseek_android

package sqlitedb

import (
	"context"
	"database/sql/driver"
	"errors"
	"os"
	"path/filepath"
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
