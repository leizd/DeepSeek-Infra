package api

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/policy"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

const (
	targetSchemaVersion = 3
	targetMarkerName    = ".deepseek-infra-backup-target.json"
	targetGenesisHash   = "0000000000000000000000000000000000000000000000000000000000000000"
	defaultQoSRate      = 100 * 1024 * 1024
)

var (
	backupTargetTempDir    = os.TempDir
	targetSecretFieldNames = map[string]bool{
		"accessKey":         true,
		"accessKeyId":       true,
		"secretAccessKey":   true,
		"secret":            true,
		"sessionToken":      true,
		"password":          true,
		"secret_access_key": true,
		"access_key_id":     true,
		"session_token":     true,
	}
)

func randomPrefixedHex(prefix string, byteLen int) string {
	raw := make([]byte, byteLen)
	_, _ = rand.Read(raw)
	return prefix + hex.EncodeToString(raw)
}

func assertTargetNoSecrets(value any) bool {
	switch typed := value.(type) {
	case map[string]any:
		for key, item := range typed {
			if targetSecretFieldNames[key] && item != nil && item != "" {
				return false
			}
			if !assertTargetNoSecrets(item) {
				return false
			}
		}
	case []any:
		for _, item := range typed {
			if !assertTargetNoSecrets(item) {
				return false
			}
		}
	}
	return true
}

func jsonInt64(value any) (int64, bool) {
	switch typed := value.(type) {
	case json.Number:
		parsed, err := typed.Int64()
		return parsed, err == nil
	case float64:
		if math.IsNaN(typed) || math.IsInf(typed, 0) || math.Trunc(typed) != typed {
			return 0, false
		}
		return int64(typed), true
	default:
		return 0, false
	}
}

func jsonFloat64(value any) (float64, bool) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, false
	}
	parsed, err := number.Float64()
	return parsed, err == nil && !math.IsNaN(parsed) && !math.IsInf(parsed, 0)
}

func parseTargetCost(value any, field string) (any, error) {
	if value == nil {
		return nil, nil
	}
	parsed, ok := jsonFloat64(value)
	if !ok || parsed < 0 {
		return nil, fmt.Errorf("Backup target %s must be a finite non-negative number", field)
	}
	return parsed, nil
}

func parsePositiveQoS(value any, field string, fallback int64) (int64, error) {
	if value == nil {
		return fallback, nil
	}
	parsed, ok := jsonInt64(value)
	if !ok || parsed <= 0 {
		return 0, fmt.Errorf("Backup target %s must be a positive integer", field)
	}
	return parsed, nil
}

func targetStringOr(value any, fallback string) string {
	text, _ := value.(string)
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return fallback
	}
	return trimmed
}

func isWithinPath(base, candidate string) bool {
	rel, err := filepath.Rel(base, candidate)
	if err != nil {
		return false
	}
	return rel == "." || (rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)))
}

func resolveDirectoryNoSymlink(rawPath string) (string, error) {
	trimmed := strings.TrimSpace(rawPath)
	if trimmed == "" || !filepath.IsAbs(trimmed) {
		return "", errors.New("Backup target path must be absolute")
	}
	cleaned := filepath.Clean(trimmed)
	info, err := os.Lstat(cleaned)
	if err != nil {
		return "", errors.New("Backup target directory does not exist")
	}
	for cur := cleaned; ; {
		curInfo, lErr := os.Lstat(cur)
		if lErr == nil && curInfo.Mode()&(os.ModeSymlink|os.ModeIrregular) != 0 {
			return "", errors.New("Backup target must not sit behind a symlink or reparse point")
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			break
		}
		cur = parent
	}
	evaluated, err := filepath.EvalSymlinks(cleaned)
	if err != nil || filepath.Clean(evaluated) != cleaned {
		return "", errors.New("Backup target must not sit behind a symlink or reparse point")
	}
	if !info.IsDir() {
		return "", errors.New("Backup target directory does not exist")
	}
	return cleaned, nil
}

func targetContainmentViolation(resolved string, existing []store.Record, runtimeRoot, excludeTargetID string) string {
	forbidden := []struct {
		name string
		path string
	}{}
	if runtimeRoot != "" {
		forbidden = append(forbidden,
			struct{ name, path string }{"runtime root", runtimeRoot},
			struct{ name, path string }{"restore staging", filepath.Join(runtimeRoot, ".restore-staging")},
			struct{ name, path string }{"managed backup directory", filepath.Join(runtimeRoot, ".backups")},
		)
	}
	if tempRoot := strings.TrimSpace(backupTargetTempDir()); tempRoot != "" {
		if evalTemp, err := filepath.EvalSymlinks(tempRoot); err == nil {
			tempRoot = evalTemp
		}
		forbidden = append(forbidden, struct{ name, path string }{"system temporary directory", filepath.Clean(tempRoot)})
	}
	for _, item := range forbidden {
		if item.path != "" && isWithinPath(filepath.Clean(item.path), resolved) {
			return "target is inside the " + item.name
		}
	}
	for _, record := range existing {
		if record.State == store.TombstoneState || (excludeTargetID != "" && record.ID == excludeTargetID) {
			continue
		}
		var doc map[string]any
		if json.Unmarshal(record.Payload, &doc) != nil || doc == nil {
			continue
		}
		if kind, _ := doc["kind"].(string); kind != "" && kind != "filesystem" {
			continue
		}
		otherRaw, _ := doc["path"].(string)
		if otherRaw == "" {
			continue
		}
		other := filepath.Clean(otherRaw)
		if isWithinPath(other, resolved) || isWithinPath(resolved, other) {
			return fmt.Sprintf("target overlaps registered target %s", record.ID)
		}
	}
	return ""
}

func configuredRuntimeRoot(control *store.Control) string {
	if envRoot := strings.TrimSpace(os.Getenv("DEEPSEEK_INFRA_ROOT")); envRoot != "" {
		if eval, err := filepath.EvalSymlinks(envRoot); err == nil {
			return filepath.Clean(eval)
		}
		return filepath.Clean(envRoot)
	}
	dbDir := filepath.Dir(control.DatabasePath())
	if filepath.Base(dbDir) == ".go-control" || filepath.Base(dbDir) == "go-control" {
		parent := filepath.Dir(dbDir)
		if eval, err := filepath.EvalSymlinks(parent); err == nil {
			return filepath.Clean(eval)
		}
		return filepath.Clean(parent)
	}
	return ""
}

func atomicWriteTargetMarker(markerPath string, payload map[string]any) error {
	raw, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return err
	}
	raw = append(raw, '\n')
	tmp := filepath.Join(filepath.Dir(markerPath), fmt.Sprintf(".%s.%d.tmp", filepath.Base(markerPath), os.Getpid()))
	file, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(raw)
	syncErr := file.Sync()
	closeErr := file.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil {
		_ = os.Remove(tmp)
		return errors.New("Unable to write target marker")
	}
	if err := os.Rename(tmp, markerPath); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

func decodeTargetRecord(record store.Record) (map[string]any, error) {
	var doc map[string]any
	decoder := json.NewDecoder(strings.NewReader(string(record.Payload)))
	decoder.UseNumber()
	if err := decoder.Decode(&doc); err != nil || doc == nil {
		return nil, store.ErrCorruptRecord
	}
	return doc, nil
}

func targetRecordState(doc map[string]any) string {
	switch doc["drainState"] {
	case "draining":
		return "DRAINING"
	case "drained":
		return "DRAINED"
	case "disabled":
		return "DISABLED"
	default:
		return "ACTIVE"
	}
}

func buildFilesystemTargetRecord(payload map[string]any, resolved, targetID, nonce, incarnationID, createdAt string, gen int64, commitHash string) (map[string]any, error) {
	storageCost, err := parseTargetCost(payload["storageCostPerGiBMonth"], "storageCostPerGiBMonth")
	if err != nil {
		return nil, err
	}
	egressCost, err := parseTargetCost(payload["egressCostPerGiB"], "egressCostPerGiB")
	if err != nil {
		return nil, err
	}
	maxRead, err := parsePositiveQoS(payload["maxReadBytesPerSecond"], "maxReadBytesPerSecond", defaultQoSRate)
	if err != nil {
		return nil, err
	}
	maxWrite, err := parsePositiveQoS(payload["maxWriteBytesPerSecond"], "maxWriteBytesPerSecond", defaultQoSRate)
	if err != nil {
		return nil, err
	}
	maxConcurrent, err := parsePositiveQoS(payload["maxConcurrentTransfers"], "maxConcurrentTransfers", 4)
	if err != nil {
		return nil, err
	}
	priority := int64(0)
	if payload["priority"] != nil {
		parsed, ok := jsonInt64(payload["priority"])
		if !ok {
			return nil, errors.New("Backup target priority must be an integer")
		}
		priority = parsed
	}
	now := nowISOMatchesPolicy()
	if createdAt == "" {
		createdAt = now
	}
	if commitHash == "" {
		commitHash = targetGenesisHash
	}
	label, _ := payload["label"].(string)
	return map[string]any{
		"schemaVersion":          targetSchemaVersion,
		"targetId":               targetID,
		"kind":                   "filesystem",
		"path":                   resolved,
		"targetNonce":            nonce,
		"incarnationId":          incarnationID,
		"lastSeenGeneration":     gen,
		"lastSeenCommitHash":     commitHash,
		"label":                  label,
		"region":                 targetStringOr(payload["region"], "local"),
		"failureDomain":          targetStringOr(payload["failureDomain"], "local"),
		"provider":               targetStringOr(payload["provider"], "operator-filesystem"),
		"jurisdiction":           targetStringOr(payload["jurisdiction"], "local"),
		"priority":               priority,
		"costClass":              targetStringOr(payload["costClass"], "standard"),
		"storageCostPerGiBMonth": storageCost,
		"egressCostPerGiB":       egressCost,
		"maxReadBytesPerSecond":  maxRead,
		"maxWriteBytesPerSecond": maxWrite,
		"maxConcurrentTransfers": maxConcurrent,
		"storageTier":            "hot",
		"restoreLatencyClass":    "seconds",
		"minResidenceSeconds":    0,
		"retrievalCostPerGiB":    nil,
		"drainState":             "active",
		"createdAt":              createdAt,
		"registeredAt":           now,
	}, nil
}

func targetStoreWriteRefusal(writer http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrCutoverNotAuthorized), errors.Is(err, store.ErrDomainNotAuthoritative):
		policyWriteError(writer, http.StatusServiceUnavailable, CodeControlNotAuthoritative,
			"Backup targets have not completed native ownership cutover")
	case errors.Is(err, store.ErrTargetHealthNotTransferred):
		policyWriteError(writer, http.StatusServiceUnavailable, "TARGET_HEALTH_NOT_TRANSFERRED",
			"Backup target health has not completed native transfer")
	default:
		writeRefusal(writer, err)
	}
}

func saveTargetMutation(writer http.ResponseWriter, control *store.Control, targetID string, revision int64, state string, doc map[string]any) bool {
	doc["topologyGeneration"] = revision
	encoded, err := json.Marshal(doc)
	if err != nil {
		policyWriteError(writer, http.StatusInternalServerError, "internal", "Unable to encode the target")
		return false
	}
	if _, err := control.ApplyOperatorMutation(store.OperatorMutation{
		OperationID: newOperationID(),
		ActionID:    newActionID(),
		Actor:       "operator:public-api",
		Record: store.Record{
			Domain:   "target",
			ID:       targetID,
			Revision: revision,
			State:    state,
			Payload:  encoded,
		},
	}); err != nil {
		targetStoreWriteRefusal(writer, err)
		return false
	}
	return true
}

func prepareTargetPayload(writer http.ResponseWriter, request *http.Request, control *store.Control) (map[string]any, bool) {
	if !authorizationAndHost(writer, request) {
		return nil, false
	}
	if control == nil {
		policyWriteError(writer, http.StatusServiceUnavailable, "CONTROL_UNAVAILABLE", "Go control store unavailable")
		return nil, false
	}
	payload, ok := readPolicyBody(writer, request)
	if !ok {
		return nil, false
	}
	if !assertTargetNoSecrets(payload) {
		policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, "cloud credentials must not be stored in target registry")
		return nil, false
	}
	return payload, true
}

func saveAndRespondFilesystemTarget(
	writer http.ResponseWriter,
	control *store.Control,
	payload map[string]any,
	resolved, targetID, nonce, incarnationID, createdAt string,
	revision, markerGen int64,
	commitHash string,
) {
	doc, err := buildFilesystemTargetRecord(payload, resolved, targetID, nonce, incarnationID, createdAt, markerGen, commitHash)
	if err != nil {
		policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, err.Error())
		return
	}
	if !saveTargetMutation(writer, control, targetID, revision, "ACTIVE", doc) {
		return
	}
	writeTargetJSON(writer, doc)
}

func writeFreshMarkerAndSave(
	writer http.ResponseWriter,
	control *store.Control,
	payload map[string]any,
	resolved, markerPath string,
) {
	targetID := randomPrefixedHex("target_", 6)
	nonce := randomPrefixedHex("", 16)
	incarnationID := randomPrefixedHex("inc_", 8)
	createdAt := nowISOMatchesPolicy()
	markerPayload := map[string]any{
		"schemaVersion":    targetSchemaVersion,
		"targetId":         targetID,
		"targetNonce":      nonce,
		"incarnationId":    incarnationID,
		"targetGeneration": 0,
		"latestCommitHash": targetGenesisHash,
		"createdAt":        createdAt,
	}
	if writeErr := atomicWriteTargetMarker(markerPath, markerPayload); writeErr != nil {
		policyWriteError(writer, http.StatusInternalServerError, "internal", writeErr.Error())
		return
	}
	saveAndRespondFilesystemTarget(writer, control, payload, resolved, targetID, nonce, incarnationID, createdAt, 1, 0, targetGenesisHash)
}

// backupTargetsCreate serves `POST /api/workspace/backup-targets`.
func backupTargetsCreate(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	payload, ok := prepareTargetPayload(writer, request, control)
	if !ok {
		return
	}
	kind := strings.ToLower(strings.TrimSpace(targetStringOr(payload["kind"], "filesystem")))
	if kind == "webdav" {
		policyWriteError(writer, http.StatusNotImplemented, "invalid_request", "WebDAV targets are reserved but not GA in 4.4.6")
		return
	}
	if kind == "s3" || kind == "s3-compatible" {
		bucket := targetStringOr(payload["bucket"], "")
		if bucket == "" {
			policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, "S3 bucket is required")
			return
		}
		policyWriteError(writer, http.StatusServiceUnavailable, "NATIVE_BACKUP_TARGET_S3_UNMIGRATED",
			"s3TargetAvailable=false: native S3 target initialization requires storage plane integration")
		return
	}
	if kind != "filesystem" {
		policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, "unsupported target kind: "+kind)
		return
	}
	existingTargets, _, err := control.ListAuthoritativeTargets()
	if err != nil {
		targetStoreWriteRefusal(writer, err)
		return
	}
	rawPath, _ := payload["path"].(string)
	resolved, err := resolveDirectoryNoSymlink(rawPath)
	if err != nil {
		policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, err.Error())
		return
	}
	runtimeRoot := configuredRuntimeRoot(control)
	markerPath := filepath.Join(resolved, targetMarkerName)

	if markerBytes, readErr := os.ReadFile(markerPath); readErr == nil {
		var marker map[string]any
		if json.Unmarshal(markerBytes, &marker) != nil || marker == nil {
			policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, "Backup target marker is unreadable")
			return
		}
		targetID, _ := marker["targetId"].(string)
		if strings.TrimSpace(targetID) == "" {
			policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, "Backup target marker is invalid")
			return
		}
		if violation := targetContainmentViolation(resolved, existingTargets, runtimeRoot, targetID); violation != "" {
			policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, "Unsafe backup target: "+violation)
			return
		}
		var revision int64 = 1
		if existingRec, found, getErr := control.Get("target", targetID); getErr == nil && found && existingRec.State != store.TombstoneState {
			if existingDoc, decErr := decodeTargetRecord(existingRec); decErr == nil {
				oldPath, _ := existingDoc["path"].(string)
				if oldPath != "" && filepath.Clean(oldPath) != resolved {
					if _, statErr := os.Stat(oldPath); statErr == nil {
						policyWriteError(writer, http.StatusConflict, "invalid_request",
							"target-clone-detected: target id is registered at another location that still exists; adopt one branch or register it as new")
						return
					}
				}
			}
			revision = existingRec.Revision + 1
		}
		nonce, _ := marker["targetNonce"].(string)
		incarnationID, _ := marker["incarnationId"].(string)
		createdAt, _ := marker["createdAt"].(string)
		commitHash, _ := marker["latestCommitHash"].(string)
		markerGen, _ := jsonInt64(marker["targetGeneration"])
		saveAndRespondFilesystemTarget(writer, control, payload, resolved, targetID, nonce, incarnationID, createdAt, revision, markerGen, commitHash)
		return
	}
	if violation := targetContainmentViolation(resolved, existingTargets, runtimeRoot, ""); violation != "" {
		policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, "Unsafe backup target: "+violation)
		return
	}
	writeFreshMarkerAndSave(writer, control, payload, resolved, markerPath)
}

// backupTargetsRegisterNew serves `POST /api/workspace/backup-targets/register-new`.
func backupTargetsRegisterNew(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	payload, ok := prepareTargetPayload(writer, request, control)
	if !ok {
		return
	}
	existingTargets, _, err := control.ListAuthoritativeTargets()
	if err != nil {
		targetStoreWriteRefusal(writer, err)
		return
	}
	rawPath, _ := payload["path"].(string)
	resolved, err := resolveDirectoryNoSymlink(rawPath)
	if err != nil {
		policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, err.Error())
		return
	}
	markerPath := filepath.Join(resolved, targetMarkerName)
	previousID := ""
	if raw, readErr := os.ReadFile(markerPath); readErr == nil {
		var prev map[string]any
		if json.Unmarshal(raw, &prev) == nil && prev != nil {
			previousID, _ = prev["targetId"].(string)
		}
	}
	if violation := targetContainmentViolation(resolved, existingTargets, configuredRuntimeRoot(control), previousID); violation != "" {
		policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, "Unsafe backup target: "+violation)
		return
	}
	writeFreshMarkerAndSave(writer, control, payload, resolved, markerPath)
}

func loadAuthoritativeTarget(
	writer http.ResponseWriter,
	request *http.Request,
	control *store.Control,
	targetID string,
	allowMissing bool,
) (store.Record, map[string]any, []store.Record, bool, bool) {
	if !authorizationAndHost(writer, request) {
		return store.Record{}, nil, nil, false, false
	}
	if control == nil {
		policyWriteError(writer, http.StatusServiceUnavailable, "CONTROL_UNAVAILABLE", "Go control store unavailable")
		return store.Record{}, nil, nil, false, false
	}
	records, _, err := control.ListAuthoritativeTargets()
	if err != nil {
		targetStoreWriteRefusal(writer, err)
		return store.Record{}, nil, nil, false, false
	}
	for _, rec := range records {
		if rec.ID != targetID || rec.State == store.TombstoneState {
			continue
		}
		doc, decErr := decodeTargetRecord(rec)
		if decErr != nil {
			policyWriteError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED", "Backup target payload invalid")
			return store.Record{}, nil, nil, false, false
		}
		return rec, doc, records, true, true
	}
	if allowMissing {
		return store.Record{}, nil, records, false, true
	}
	policyWriteError(writer, http.StatusNotFound, "not_found", "Backup target not found")
	return store.Record{}, nil, nil, false, false
}

func readFilesystemTargetMarker(rawPath string) (string, string, map[string]any, error) {
	resolved, err := resolveDirectoryNoSymlink(rawPath)
	if err != nil {
		return "", "", nil, err
	}
	markerPath := filepath.Join(resolved, targetMarkerName)
	raw, err := os.ReadFile(markerPath)
	if err != nil {
		if os.IsNotExist(err) {
			return resolved, markerPath, nil, errors.New("Backup target marker is missing")
		}
		return resolved, markerPath, nil, errors.New("Backup target marker is unreadable")
	}
	var marker map[string]any
	if json.Unmarshal(raw, &marker) != nil || marker == nil {
		return resolved, markerPath, nil, errors.New("Backup target marker is unreadable")
	}
	return resolved, markerPath, marker, nil
}

func writeTargetJSON(writer http.ResponseWriter, payload map[string]any) {
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(payload)
}

// backupTargetsDelete serves `DELETE /api/workspace/backup-targets/{target_id}`.
func backupTargetsDelete(writer http.ResponseWriter, request *http.Request, targetID string, control *store.Control) {
	rec, doc, _, _, ok := loadAuthoritativeTarget(writer, request, control, targetID, false)
	if !ok {
		return
	}
	if !saveTargetMutation(writer, control, targetID, rec.Revision+1, store.TombstoneState, doc) {
		return
	}
	writeTargetJSON(writer, map[string]any{"deleted": true, "targetId": targetID})
}

// backupTargetsAdopt serves `POST /api/workspace/backup-targets/{target_id}/adopt`.
func backupTargetsAdopt(writer http.ResponseWriter, request *http.Request, targetID string, control *store.Control) {
	rec, doc, _, _, ok := loadAuthoritativeTarget(writer, request, control, targetID, false)
	if !ok {
		return
	}
	kind := strings.ToLower(strings.TrimSpace(targetStringOr(doc["kind"], "filesystem")))
	if kind != "filesystem" {
		policyWriteError(writer, http.StatusBadRequest, "invalid_request", "Only filesystem targets support incarnation adoption")
		return
	}
	rawPath, _ := doc["path"].(string)
	_, markerPath, marker, err := readFilesystemTargetMarker(rawPath)
	if err != nil {
		policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload, err.Error())
		return
	}
	if marker["targetId"] != targetID || marker["targetNonce"] != doc["targetNonce"] {
		policyWriteError(writer, http.StatusBadRequest, policy.CodeInvalidPayload,
			"blocked-target-unavailable: marker identity does not match registered target")
		return
	}
	newIncarnation := randomPrefixedHex("inc_", 8)
	markerGen, _ := jsonInt64(marker["targetGeneration"])
	markerCommit := targetStringOr(marker["latestCommitHash"], targetGenesisHash)
	marker["schemaVersion"] = targetSchemaVersion
	marker["incarnationId"] = newIncarnation
	if _, hasGen := jsonInt64(marker["targetGeneration"]); !hasGen {
		marker["targetGeneration"] = int64(0)
	}
	marker["latestCommitHash"] = markerCommit
	if writeErr := atomicWriteTargetMarker(markerPath, marker); writeErr != nil {
		policyWriteError(writer, http.StatusInternalServerError, "internal", writeErr.Error())
		return
	}
	doc["incarnationId"] = newIncarnation
	doc["lastSeenGeneration"] = markerGen
	doc["lastSeenCommitHash"] = markerCommit
	doc["adoptedAt"] = nowISOMatchesPolicy()
	if !saveTargetMutation(writer, control, targetID, rec.Revision+1, targetRecordState(doc), doc) {
		return
	}
	writeTargetJSON(writer, map[string]any{
		"targetId":         targetID,
		"adopted":          true,
		"incarnationId":    newIncarnation,
		"targetGeneration": markerGen,
		"latestCommitHash": markerCommit,
	})
}

// backupTargetsActivate serves `POST /api/workspace/backup-targets/{target_id}/activate`.
func backupTargetsActivate(writer http.ResponseWriter, request *http.Request, targetID string, control *store.Control) {
	rec, doc, _, _, ok := loadAuthoritativeTarget(writer, request, control, targetID, false)
	if !ok {
		return
	}
	doc["drainState"] = "active"
	delete(doc, "drainReason")
	delete(doc, "drainingAt")
	delete(doc, "drainedAt")
	doc["activatedAt"] = nowISOMatchesPolicy()
	if !saveTargetMutation(writer, control, targetID, rec.Revision+1, "ACTIVE", doc) {
		return
	}
	writeTargetJSON(writer, doc)
}

// backupTargetsProbe serves `POST /api/workspace/backup-targets/{target_id}/probe`.
func backupTargetsProbe(writer http.ResponseWriter, request *http.Request, targetID string, control *store.Control) {
	rec, doc, allRecords, found, ok := loadAuthoritativeTarget(writer, request, control, targetID, true)
	if !ok {
		return
	}
	if !found {
		writeTargetJSON(writer, map[string]any{
			"targetId": targetID,
			"ready":    false,
			"status":   "blocked-target-unavailable",
			"detail":   "Backup target not found",
		})
		return
	}
	kind := strings.ToLower(strings.TrimSpace(targetStringOr(doc["kind"], "filesystem")))
	blocked := func(status, detail string) {
		writeTargetJSON(writer, map[string]any{
			"targetId":             targetID,
			"ready":                false,
			"status":               status,
			"detail":               detail,
			"kind":                 kind,
			"scheduledBackupReady": false,
		})
	}
	if kind != "filesystem" {
		blocked("blocked-s3-unmigrated", "s3TargetAvailable=false: native S3 target probe requires storage plane")
		return
	}
	rawPath, _ := doc["path"].(string)
	resolved, _, marker, err := readFilesystemTargetMarker(rawPath)
	if err != nil {
		blocked("blocked-target-unavailable", err.Error())
		return
	}
	if violation := targetContainmentViolation(resolved, allRecords, configuredRuntimeRoot(control), targetID); violation != "" {
		blocked("blocked-unsafe-target", "Unsafe backup target: "+violation)
		return
	}
	if marker["targetId"] != targetID || marker["targetNonce"] != doc["targetNonce"] {
		blocked("blocked-target-unavailable", "blocked-target-unavailable: marker identity does not match registered target")
		return
	}
	docInc, _ := doc["incarnationId"].(string)
	markerInc, _ := marker["incarnationId"].(string)
	docGen, _ := jsonInt64(doc["lastSeenGeneration"])
	markerGen, hasGen := jsonInt64(marker["targetGeneration"])
	docCommit, _ := doc["lastSeenCommitHash"].(string)
	markerCommit, _ := marker["latestCommitHash"].(string)
	if docGen > 0 && hasGen && markerGen < docGen {
		blocked("target-rollback-detected",
			fmt.Sprintf("target-rollback-detected: marker generation %d is behind last seen generation %d", markerGen, docGen))
		return
	}
	if docInc != "" && markerInc != "" && markerInc != docInc {
		blocked("target-fork-detected",
			fmt.Sprintf("target-fork-detected: marker incarnation %s does not match %s", markerInc, docInc))
		return
	}
	if hasGen && markerGen == docGen && docCommit != "" && markerCommit != "" && markerCommit != docCommit {
		blocked("target-fork-detected", "target-fork-detected: marker commit head diverged at same generation")
		return
	}
	if (hasGen && markerGen > docGen) || (markerCommit != "" && markerCommit != docCommit) || (docInc == "" && markerInc != "") {
		if hasGen {
			doc["lastSeenGeneration"] = markerGen
		}
		if markerCommit != "" {
			doc["lastSeenCommitHash"] = markerCommit
		}
		if docInc == "" && markerInc != "" {
			doc["incarnationId"] = markerInc
		}
		if !saveTargetMutation(writer, control, targetID, rec.Revision+1, targetRecordState(doc), doc) {
			return
		}
	}
	writeTargetJSON(writer, map[string]any{
		"targetId":             targetID,
		"ready":                true,
		"status":               "ok",
		"detail":               "ok",
		"path":                 resolved,
		"kind":                 "filesystem",
		"scheduledBackupReady": true,
	})
}

func backupTargetsDispatch(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	rest := strings.TrimPrefix(request.URL.Path, "/api/workspace/backup-targets/")
	parts := strings.Split(rest, "/")
	switch {
	case rest == "register-new":
		switch request.Method {
		case http.MethodPost:
			backupTargetsRegisterNew(writer, request, control)
		case http.MethodDelete:
			backupTargetsDelete(writer, request, "register-new", control)
		default:
			writer.WriteHeader(http.StatusMethodNotAllowed)
		}
	case rest != "" && len(parts) == 1:
		if request.Method != http.MethodDelete {
			writer.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		backupTargetsDelete(writer, request, parts[0], control)
	case len(parts) == 2 && parts[0] != "" && (parts[1] == "adopt" || parts[1] == "activate" || parts[1] == "probe"):
		if request.Method != http.MethodPost {
			writer.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		switch parts[1] {
		case "adopt":
			backupTargetsAdopt(writer, request, parts[0], control)
		case "activate":
			backupTargetsActivate(writer, request, parts[0], control)
		case "probe":
			backupTargetsProbe(writer, request, parts[0], control)
		}
	default:
		notImplemented(writer, request)
	}
}
