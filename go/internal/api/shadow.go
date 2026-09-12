package api

import (
	"encoding/json"
	"net/http"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"github.com/leizd/DeepSeek-Infra/go/internal/shadow"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func Register(mux *http.ServeMux, control *store.Control) {
	mux.HandleFunc("/internal/shadow/evaluate", func(writer http.ResponseWriter, request *http.Request) {
		evaluate(writer, request, control)
	})
	mux.HandleFunc("/internal/shadow/snapshot", func(writer http.ResponseWriter, request *http.Request) {
		snapshot(writer, request, control)
	})
	mux.HandleFunc("/internal/action/execute", deny)
	mux.HandleFunc("/internal/action/dispatch", func(writer http.ResponseWriter, request *http.Request) {
		dispatch(writer, request, control)
	})
	mux.HandleFunc("/internal/cutover/status", func(writer http.ResponseWriter, request *http.Request) {
		cutoverStatus(writer, request, control)
	})
	mux.HandleFunc("/internal/cutover/transition", func(writer http.ResponseWriter, request *http.Request) {
		cutoverTransition(writer, request, control)
	})
}

func Handler() http.Handler {
	mux := http.NewServeMux()
	Register(mux, nil)
	return mux
}

func evaluate(writer http.ResponseWriter, request *http.Request, control *store.Control) {
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
	if err := shadow.Persist(control, snapshot, decision); err != nil {
		writer.WriteHeader(http.StatusConflict)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(decision)
}

func snapshot(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodGet {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writer.WriteHeader(http.StatusNotFound)
		return
	}
	report, err := control.ExportSnapshot()
	if err != nil {
		writer.WriteHeader(http.StatusConflict)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(report)
}

func dispatch(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodPost {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Kind           string `json:"kind"`
		ActionID       string `json:"actionId"`
		ExecutionEpoch uint64 `json:"executionEpoch"`
	}
	if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
		writer.WriteHeader(http.StatusBadRequest)
		return
	}
	kind, ok := internalprotocol.KindFromName(body.Kind)
	if !ok {
		writer.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": internalprotocol.ErrUnknownEffect.Error()})
		return
	}
	liveEpoch := uint64(0)
	if control != nil && body.ActionID != "" {
		record, exists, lookupErr := control.Get("action", body.ActionID)
		if lookupErr != nil {
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusConflict)
			_ = json.NewEncoder(writer).Encode(map[string]string{"error": internalprotocol.ErrFenceMismatch.Error()})
			return
		}
		if exists {
			liveEpoch = record.ExecutionEpoch
		}
	}
	err := internalprotocol.PlanNative(kind, &internalprotocol.ActionFence{ActionId: body.ActionID, ExecutionEpoch: body.ExecutionEpoch}, liveEpoch)
	writer.Header().Set("Content-Type", "application/json")
	status := http.StatusConflict
	if err == internalprotocol.ErrEmptyActionID || err == internalprotocol.ErrZeroEpoch || err == internalprotocol.ErrUnknownEffect {
		status = http.StatusBadRequest
	}
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": err.Error()})
}

func deny(writer http.ResponseWriter, _ *http.Request) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(http.StatusForbidden)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": internalprotocol.ErrMutationDenied.Error()})
}

func cutoverStatus(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodGet {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writer.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	domain := request.URL.Query().Get("domain")
	if domain == "" {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": "domain required"})
		return
	}
	rec, err := control.GetCutover(domain)
	writer.Header().Set("Content-Type", "application/json")
	if err != nil {
		writer.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": err.Error()})
		return
	}
	_ = json.NewEncoder(writer).Encode(rec)
}

func cutoverTransition(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodPost {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writer.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	var req store.CutoverTransition
	if err := json.NewDecoder(request.Body).Decode(&req); err != nil {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": "invalid json"})
		return
	}
	rec, err := control.TransitionCutover(req)
	writer.Header().Set("Content-Type", "application/json")
	if err != nil {
		writer.WriteHeader(http.StatusConflict)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": err.Error()})
		return
	}
	_ = json.NewEncoder(writer).Encode(rec)
}
