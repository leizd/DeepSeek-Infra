package sqlitedb

import "database/sql"

// Open uses the same connector as migration readers and transport fault fixtures.
func Open(dsn string) (*sql.DB, error) {
	connector, err := NewConnector(dsn)
	if err != nil {
		return nil, err
	}
	return sql.OpenDB(connector), nil
}
