//go:build !android && !deepseek_android

package sqlitedb

import (
	"database/sql/driver"

	modernsqlite "modernc.org/sqlite"
)

func NewConnector(dsn string) (driver.Connector, error) {
	return modernsqlite.NewConnector(dsn)
}
