package launch

import (
	"errors"
	"flag"
	"io"
	"net"
	"strconv"
	"strings"
)

// Options describes one launch. Secrets apply only to the backend environment,
// never the desktop environment or a diagnostic representation.
type Options struct {
	Mode, Host           string
	Port                 int
	APIKey, TavilyAPIKey string
	OCR, AuthDisabled    bool
	NoOpen, NoPrompt     bool
}

func ParseOptions(args []string, lookup func(string) string) (Options, error) {
	options := Options{Host: strings.TrimSpace(lookup("HOST"))}
	if options.Host == "" {
		options.Host = "127.0.0.1"
	}
	port := strings.TrimSpace(lookup("PORT"))
	if port == "" {
		port = "8000"
	}
	if bind := strings.TrimSpace(lookup("GATEWAY_BIND_ADDR")); bind != "" {
		var err error
		options.Host, port, err = net.SplitHostPort(bind)
		if err != nil {
			return Options{}, errors.New("invalid configured gateway address")
		}
	}
	var server, mobile, gui, app, lan bool
	flags := flag.NewFlagSet("deepseek-launch", flag.ContinueOnError)
	// flag's default parse diagnostics may contain a supplied secret value.
	flags.SetOutput(io.Discard)
	flags.BoolVar(&server, "server", false, "run without a desktop window")
	flags.BoolVar(&mobile, "mobile", false, "run the mobile console launcher")
	flags.BoolVar(&gui, "gui", false, "open the launcher window")
	flags.BoolVar(&app, "app", false, "open the desktop application")
	flags.StringVar(&options.Host, "host", options.Host, "public listen host")
	flags.StringVar(&port, "port", port, "public listen port")
	flags.BoolVar(&lan, "lan", false, "listen on all IPv4 interfaces")
	flags.StringVar(&options.APIKey, "api-key", lookup("DEEPSEEK_API_KEY"), "DeepSeek key for this run")
	flags.StringVar(&options.TavilyAPIKey, "tavily-api-key", lookup("TAVILY_API_KEY"), "search key for this run")
	flags.BoolVar(&options.AuthDisabled, "auth-disabled", truthy(lookup("AUTH_DISABLED")), "disable local token authentication")
	flags.BoolVar(&options.OCR, "ocr", truthy(lookup("OCR_ENABLED")), "enable OCR")
	flags.BoolVar(&options.NoOpen, "no-open", false, "print mobile URL without opening a browser")
	flags.BoolVar(&options.NoPrompt, "no-prompt", false, "do not prompt for a missing key")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return Options{}, flag.ErrHelp
		}
		return Options{}, errors.New("invalid launch arguments; use --help")
	}
	if flags.NArg() != 0 {
		return Options{}, errors.New("unexpected launch argument; use --help")
	}
	var err error
	options.Port, err = strconv.Atoi(strings.TrimSpace(port))
	if err != nil || options.Port < 1 || options.Port > 65535 {
		return Options{}, errors.New("port must be between 1 and 65535")
	}
	options.Host = strings.TrimSpace(options.Host)
	if options.Host == "" {
		options.Host = "127.0.0.1"
	}
	if lan {
		options.Host = "0.0.0.0"
	} else if options.Host == "localhost" {
		options.Host = "127.0.0.1"
	}
	if net.ParseIP(options.Host) == nil {
		return Options{}, errors.New("host must be an IP address or localhost")
	}
	options.APIKey = strings.TrimSpace(options.APIKey)
	options.TavilyAPIKey = strings.TrimSpace(options.TavilyAPIKey)
	if strings.ContainsAny(options.APIKey+options.TavilyAPIKey, "\r\n\x00") {
		return Options{}, errors.New("invalid launch credential")
	}
	options.Mode = "app"
	if gui {
		options.Mode = "gui"
	}
	if mobile || (!app && !gui && mobileEnvironment(lookup)) {
		options.Mode = "mobile"
	}
	if server {
		options.Mode = "server"
	}
	return options, nil
}

func mobileEnvironment(lookup func(string) string) bool {
	for _, key := range []string{"ANDROID_ARGUMENT", "ANDROID_DATA", "ANDROID_ROOT", "PYDROID_PACKAGE", "TERMUX_VERSION"} {
		if lookup(key) != "" {
			return true
		}
	}
	return false
}

func truthy(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func (options Options) BindAddress() string {
	return net.JoinHostPort(options.Host, strconv.Itoa(options.Port))
}

func (options Options) Apply(plan Plan) Plan {
	values := map[string]string{
		"HOST": options.Host, "PORT": strconv.Itoa(options.Port),
		"DEEPSEEK_API_KEY": options.APIKey, "TAVILY_API_KEY": options.TavilyAPIKey,
		"OCR_ENABLED": "0", "AUTH_DISABLED": "0",
	}
	if options.OCR {
		values["OCR_ENABLED"] = "1"
	}
	if options.AuthDisabled {
		values["AUTH_DISABLED"] = "1"
	}
	env := make([]string, 0, len(plan.Env)+len(values))
	for _, value := range plan.Env {
		key, _, _ := strings.Cut(value, "=")
		if _, replaced := values[key]; !replaced {
			env = append(env, value)
		}
	}
	for _, key := range []string{"HOST", "PORT", "DEEPSEEK_API_KEY", "TAVILY_API_KEY", "OCR_ENABLED", "AUTH_DISABLED"} {
		env = append(env, key+"="+values[key])
	}
	plan.Env = env
	return plan
}

// PromptAPIKey retains the mobile console's optional setup. An empty answer
// lets the user configure a key later in the web settings.
func (options Options) PromptAPIKey(interactive bool, read func() ([]byte, error)) (Options, error) {
	if options.Mode != "mobile" || options.NoPrompt || options.APIKey != "" || !interactive {
		return options, nil
	}
	value, err := read()
	if err != nil {
		return Options{}, errors.New("cannot read launch credential")
	}
	options.APIKey = strings.TrimSpace(string(value))
	for i := range value {
		value[i] = 0
	}
	if len(options.APIKey) > 8192 || strings.ContainsAny(options.APIKey, "\r\n\x00") {
		return Options{}, errors.New("invalid launch credential")
	}
	return options, nil
}
