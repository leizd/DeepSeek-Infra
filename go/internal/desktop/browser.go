package desktop

import (
	"context"
	"errors"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
)

// OpenBrowser invokes the OS handler for this native instance's public URL.
// Failure leaves the console's copyable URL available, as in the old launcher.
func OpenBrowser(ctx context.Context, raw string) error {
	if err := validateBrowserURL(raw); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if os.Getenv("TERMUX_VERSION") != "" {
		if err := openTermuxBrowser(ctx, raw); err == nil {
			return nil
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return openPlatformBrowser(raw)
}

func validateBrowserURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || strings.ContainsAny(raw, "\r\n\x00") {
		return errors.New("invalid native browser URL")
	}
	if u.Hostname() != "localhost" && net.ParseIP(u.Hostname()) == nil {
		return errors.New("invalid native browser host")
	}
	if _, err := url.ParseQuery(u.RawQuery); err != nil {
		return errors.New("invalid native browser query")
	}
	if port := u.Port(); port != "" {
		number, err := strconv.Atoi(port)
		if err != nil || number < 1 || number > 65535 {
			return errors.New("invalid native browser port")
		}
	}
	return nil
}
