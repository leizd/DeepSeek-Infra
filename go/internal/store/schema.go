package store

import (
	"errors"
	"path/filepath"
	"strings"
)

var (
	ErrEmptyStorePath      = errors.New("EMPTY_STORE_PATH")
	ErrPythonStorePath     = errors.New("PYTHON_STORE_PATH")
	ErrWriterFenceHeld     = errors.New("WRITER_FENCE_HELD")
	ErrUnknownDomain       = errors.New("UNKNOWN_DOMAIN")
	ErrRevisionConflict    = errors.New("REVISION_CONFLICT")
	ErrIllegalTransition   = errors.New("ILLEGAL_TRANSITION")
	ErrForeignRuntimeStore = errors.New("FOREIGN_RUNTIME_STORE")
	ErrSchemaInactive      = errors.New("SCHEMA_INACTIVE")
	ErrEmptyRecordID       = errors.New("EMPTY_RECORD_ID")
)

const (
	RuntimeGo  = "go"
	ModeShadow = "shadow"
	SchemaV1   = 1
)

var TableNames = []string{
	"policies",
	"targets",
	"scheduler_runs",
	"action_journal",
	"risk_observations",
	"wave_schedules",
	"federation_peers",
	"federation_grants",
	"federation_sessions",
	"federation_transfers",
	"forecasts",
	"agent_runs",
}

var DomainTable = map[string]string{
	"policy":        "policies",
	"target":        "targets",
	"scheduler_run": "scheduler_runs",
	"action":        "action_journal",
	"risk":          "risk_observations",
	"wave":          "wave_schedules",
	"peer":          "federation_peers",
	"grant":         "federation_grants",
	"session":       "federation_sessions",
	"transfer":      "federation_transfers",
	"forecast":      "forecasts",
	"agent_run":     "agent_runs",
}

var fencedDomains = map[string]bool{
	"action":        true,
	"scheduler_run": true,
	"wave":          true,
	"transfer":      true,
}

var pythonPathParts = map[string]bool{
	".backup-control":       true,
	".backup-scheduler":     true,
	".backup-policies":      true,
	".backup-targets":       true,
	".resilience-journal":   true,
	".resilience-risk":      true,
	".resilience-waves":     true,
	".resilience-scheduler": true,
	".resilience-capacity":  true,
	".resilience-slo":       true,
	".resilience-cost":      true,
	".resilience-optimizer": true,
	".resilience-policy":    true,
	".federation":           true,
	".agent-runs":           true,
	".a2a":                  true,
	".backup-drains":        true,
	".backup-retirements":   true,
	".backup-dr":            true,
}

var pythonStoreFiles = map[string]bool{
	"control.sqlite3":    true,
	"scheduler.db":       true,
	"journal.sqlite3":    true,
	"risk.sqlite3":       true,
	"waves.sqlite3":      true,
	"service.sqlite3":    true,
	"capacity.sqlite3":   true,
	"peer-trust.sqlite3": true,
	"transfers.sqlite3":  true,
}

var transitions = map[string]map[string][]string{
	"policy": {
		"":         {"ACTIVE"},
		"ACTIVE":   {"DISABLED"},
		"DISABLED": {"ACTIVE"},
	},
	"target": {
		"":         {"ACTIVE"},
		"ACTIVE":   {"DRAINING", "DISABLED"},
		"DRAINING": {"DRAINED"},
	},
	"scheduler_run": {
		"":       {"queued"},
		"queued": {"leased", "failed"},
		"leased": {"complete", "failed"},
	},
	"action": {
		"":          {"PENDING"},
		"PENDING":   {"CLAIMED", "FAILED_BEFORE_EFFECT"},
		"CLAIMED":   {"EXECUTING", "EFFECT_UNKNOWN"},
		"EXECUTING": {"SUCCEEDED", "EFFECT_UNKNOWN"},
	},
	"risk": {
		"":         {"OPEN"},
		"OPEN":     {"CLEARED", "SUPERSEDED", "RETIRED", "REOPENED"},
		"REOPENED": {"CLEARED", "SUPERSEDED", "RETIRED"},
	},
	"wave": {
		"":        {"PLANNED"},
		"PLANNED": {"RUNNING", "SUPERSEDED"},
		"RUNNING": {"COMPLETED", "FAILED", "SUPERSEDED"},
	},
	"peer": {
		"":          {"PENDING"},
		"PENDING":   {"VERIFIED", "REVOKED"},
		"VERIFIED":  {"ACTIVE", "REVOKED"},
		"ACTIVE":    {"SUSPENDED", "REVOKED"},
		"SUSPENDED": {"REVOKED"},
	},
	"grant": {
		"":       {"ACTIVE"},
		"ACTIVE": {"REVOKED"},
	},
	"session": {
		"":          {"PENDING"},
		"PENDING":   {"RESPONDED"},
		"RESPONDED": {"CONSUMED"},
	},
	"transfer": {
		"":                 {"PROPOSED"},
		"PROPOSED":         {"GRANT_REQUESTED"},
		"GRANT_REQUESTED":  {"GRANT_VERIFIED"},
		"GRANT_VERIFIED":   {"TRANSFERRING"},
		"TRANSFERRING":     {"REMOTE_VERIFYING"},
		"REMOTE_VERIFYING": {"REMOTE_COMMITTED"},
		"REMOTE_COMMITTED": {"LOCAL_RECORDED"},
		"LOCAL_RECORDED":   {"SUCCEEDED"},
	},
	"forecast": {
		"":       {"ACTIVE"},
		"ACTIVE": {"DUE"},
		"DUE":    {"BACKTESTED"},
	},
	"agent_run": {
		"":         {"created"},
		"created":  {"planning", "cancelled"},
		"planning": {"running", "cancelled"},
		"running":  {"done", "failed", "cancelled"},
	},
}

func RejectPythonPath(path string) error {
	if strings.TrimSpace(path) == "" {
		return ErrEmptyStorePath
	}
	cleaned := filepath.Clean(path)
	base := strings.ToLower(filepath.Base(cleaned))
	if pythonStoreFiles[base] {
		return ErrPythonStorePath
	}
	normalized := strings.ReplaceAll(cleaned, "\\", "/")
	for _, part := range strings.Split(normalized, "/") {
		if pythonPathParts[strings.ToLower(part)] {
			return ErrPythonStorePath
		}
	}
	return nil
}

func ValidRecordID(id string) bool {
	if id == "" || id == "." || id == ".." {
		return false
	}
	if filepath.Base(id) != id {
		return false
	}
	for _, mark := range id {
		if mark == '/' || mark == '\\' || mark == 0 {
			return false
		}
	}
	return true
}

func LegalTransition(domain, from, to string) bool {
	allowed, ok := transitions[domain]
	if !ok {
		return false
	}
	next, ok := allowed[from]
	if !ok {
		return false
	}
	for _, state := range next {
		if state == to {
			return true
		}
	}
	return false
}
