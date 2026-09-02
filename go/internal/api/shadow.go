package api

import (
	"encoding/json"
	"net/http"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"github.com/leizd/DeepSeek-Infra/go/internal/shadow"
)

func Register(mux *http.ServeMux) {
	mux.HandleFunc("/internal/shadow/evaluate", evaluate)
	mux.HandleFunc("/internal/action/execute", deny)
}

func Handler() http.Handler {
	mux := http.NewServeMux()
	Register(mux)
	return mux
}

func evaluate(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodPost {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var snapshot map[string]any
	if err := json.NewDecoder(request.Body).Decode(&snapshot); err != nil {
		writer.WriteHeader(http.StatusBadRequest)
		return
	}
	decision, err := shadow.Evaluate(snapshot)
	if err != nil {
		writer.WriteHeader(http.StatusBadRequest)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(decision)
}

func deny(writer http.ResponseWriter, _ *http.Request) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(http.StatusForbidden)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": internalprotocol.ErrMutationDenied.Error()})
}
