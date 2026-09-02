package lifecycle

import (
	"context"
	"encoding/json"
	"net"
	"net/http"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
)

type Status struct {
	OK                 bool   `json:"ok"`
	Mode               string `json:"mode"`
	MutationAuthority  string `json:"mutationAuthority"`
	ProductionMutation bool   `json:"productionMutation"`
}

func StatusFrom(cfg config.Config) Status {
	return Status{
		OK:                 true,
		Mode:               cfg.Mode,
		MutationAuthority:  config.MutationAuthority,
		ProductionMutation: false,
	}
}

func Listen(ctx context.Context, cfg config.Config) (string, error) {
	listener, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return "", err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(writer).Encode(StatusFrom(cfg))
	})
	server := &http.Server{Handler: mux}
	go func() {
		<-ctx.Done()
		_ = server.Shutdown(context.Background())
	}()
	go func() {
		_ = server.Serve(listener)
	}()
	return listener.Addr().String(), nil
}
