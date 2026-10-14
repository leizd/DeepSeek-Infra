//go:build android || deepseek_android

package sqlitedb

import (
	"context"
	"database/sql/driver"
	"errors"
	"fmt"
	"net/url"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"

	"github.com/ncruces/go-sqlite3"
	sqlitedriver "github.com/ncruces/go-sqlite3/driver"
	"github.com/ncruces/go-sqlite3/vfs"
)

// Android app seccomp rejects legacy Linux syscalls used by modernc's amd64
// musl VFS (lstat first). This pinned SQLite implementation uses Go's OS APIs.
// Only the connector changes: the control schema, transactions and unique-writer
// admission remain identical. There is no cgo, secondary writer or fallback.
func NewConnector(dsn string) (driver.Connector, error) {
	if !vfs.SupportsFileLocking || !vfs.SupportsSharedMemory {
		return nil, errors.New("Android SQLite requires file locking and shared WAL")
	}
	var uri *url.URL
	var err error
	if strings.HasPrefix(dsn, "file:") {
		uri, err = url.Parse(dsn)
	} else {
		path := filepath.ToSlash(dsn)
		if runtime.GOOS == "windows" && len(path) >= 2 && path[1] == ':' {
			path = "/" + path
		}
		uri = &url.URL{Scheme: "file", Path: path}
	}
	if err != nil {
		return nil, err
	}
	// This adapter is also exercised on Windows hosts. The portable VFS expects
	// file:C:/... instead of modernc's file:///C:/... drive-letter representation.
	if runtime.GOOS == "windows" && len(uri.Path) >= 3 && uri.Path[0] == '/' && uri.Path[2] == ':' {
		uri.Opaque = uri.EscapedPath()[1:]
		uri.Path, uri.RawPath = "", ""
	}
	query, err := url.ParseQuery(uri.RawQuery)
	if err != nil {
		return nil, err
	}
	// Invalid or ambiguous security settings must fail before opening a file,
	// just as the original connector does. Values.Get silently hides duplicates.
	security := make(map[string]bool, 2)
	for _, name := range []string{"_defensive", "_dqs"} {
		values, present := query[name]
		if !present {
			continue
		}
		if len(values) != 1 {
			return nil, fmt.Errorf("%s must be specified exactly once", name)
		}
		value, parseErr := strconv.ParseBool(values[0])
		if parseErr != nil {
			return nil, fmt.Errorf("invalid %s value: %w", name, parseErr)
		}
		security[name] = value
	}
	// The portable driver uses PRAGMAs for these settings. Preserve order so the
	// busy timeout is active before acquiring the WAL initialization lock.
	pragmas := []string{}
	if timeout := query.Get("_busy_timeout"); timeout != "" {
		pragmas = append(pragmas, "busy_timeout("+timeout+")")
	}
	if len(query["_pragma"]) == 0 && len(pragmas) == 0 {
		pragmas = append(pragmas, "busy_timeout(0)")
	}
	for _, setting := range [][2]string{{"_foreign_keys", "foreign_keys"}, {"_journal_mode", "journal_mode"},
		{"_synchronous", "synchronous"}, {"_query_only", "query_only"}} {
		if value := query.Get(setting[0]); value != "" {
			pragmas = append(pragmas, setting[1]+"("+value+")")
		}
		query.Del(setting[0])
	}
	pragmas = append(pragmas, query["_pragma"]...)
	query.Del("_busy_timeout")
	query.Del("_defensive")
	query.Del("_dqs")
	query["_pragma"] = pragmas
	uri.RawQuery = query.Encode()
	connector, err := (&sqlitedriver.SQLite{}).OpenConnector(uri.String())
	if err != nil {
		return nil, err
	}
	return configuredConnector{Connector: connector, initialize: func(connection *sqlite3.Conn) error {
		if defensive, present := security["_defensive"]; present {
			if _, err := connection.Config(sqlite3.DBCONFIG_DEFENSIVE, defensive); err != nil {
				return err
			}
		}
		if dqs, present := security["_dqs"]; present {
			for _, operation := range []sqlite3.DBConfig{sqlite3.DBCONFIG_DQS_DML, sqlite3.DBCONFIG_DQS_DDL} {
				if _, err := connection.Config(operation, dqs); err != nil {
					return err
				}
			}
		}
		return nil
	}}, nil
}

// Retain the driver's connection interfaces and context cancellation while applying
// the same defensive configuration to every pooled or fault-wrapped connection.
type configuredConnector struct {
	driver.Connector
	initialize func(*sqlite3.Conn) error
}

func (connector configuredConnector) Connect(ctx context.Context) (driver.Conn, error) {
	connection, err := connector.Connector.Connect(ctx)
	if err != nil {
		return nil, err
	}
	if err := connector.initialize(connection.(sqlitedriver.Conn).Raw()); err != nil {
		_ = connection.Close()
		return nil, err
	}
	return connection, nil
}
