package store

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"unicode/utf8"
)

var errInjectedAdmissionFault = errors.New("INJECTED_ADMISSION_FAULT")

type AdmissionPolicy struct {
	MaxConcurrentActions                 int `json:"maxConcurrentActions"`
	MaxActionsPerHour                    int `json:"maxActionsPerHour"`
	MaxConcurrentPerTarget               int `json:"maxConcurrentPerTarget"`
	MaxConcurrentPerPolicy               int `json:"maxConcurrentPerPolicy"`
	MaxSimultaneousFailureDomainsTouched int `json:"maxSimultaneousFailureDomainsTouched"`
}

func defaultAdmissionPolicy() AdmissionPolicy {
	return AdmissionPolicy{
		MaxConcurrentActions:                 3,
		MaxActionsPerHour:                    20,
		MaxConcurrentPerTarget:               2,
		MaxConcurrentPerPolicy:               2,
		MaxSimultaneousFailureDomainsTouched: 1,
	}
}

func effectivePolicy(p AdmissionPolicy) AdmissionPolicy {
	def := defaultAdmissionPolicy()
	if p.MaxConcurrentActions <= 0 {
		p.MaxConcurrentActions = def.MaxConcurrentActions
	}
	if p.MaxActionsPerHour <= 0 {
		p.MaxActionsPerHour = def.MaxActionsPerHour
	}
	if p.MaxConcurrentPerTarget <= 0 {
		p.MaxConcurrentPerTarget = def.MaxConcurrentPerTarget
	}
	if p.MaxConcurrentPerPolicy <= 0 {
		p.MaxConcurrentPerPolicy = def.MaxConcurrentPerPolicy
	}
	if p.MaxSimultaneousFailureDomainsTouched <= 0 {
		p.MaxSimultaneousFailureDomainsTouched = def.MaxSimultaneousFailureDomainsTouched
	}
	return p
}

type AdmissionRequest struct {
	ActionID     string
	Owner        string
	LeaseSeconds int64
	ResourceKeys []string
	Policy       AdmissionPolicy
}

type ActionLease struct {
	ActionID           string `json:"actionId"`
	Owner              string `json:"owner"`
	Epoch              uint64 `json:"epoch"`
	ClaimToken         string `json:"claimToken"`
	LeaseUntil         int64  `json:"leaseUntil"`
	AcquiredAt         int64  `json:"acquiredAt"`
	UpdatedAt          int64  `json:"updatedAt"`
	ClaimRevision      int64  `json:"claimRevision"`
	WriterFencingToken int64  `json:"writerFencingToken"`
}

type ActionResourceLease struct {
	ResourceKey        string `json:"resourceKey"`
	ActionID           string `json:"actionId"`
	Owner              string `json:"owner"`
	Epoch              uint64 `json:"epoch"`
	AcquiredAt         int64  `json:"acquiredAt"`
	LeaseUntil         int64  `json:"leaseUntil"`
	WriterFencingToken int64  `json:"writerFencingToken"`
}

type ActionLeaseRenewal struct {
	ActionID     string
	Epoch        uint64
	ClaimToken   string
	Owner        string
	LeaseSeconds int64
}

type AdmissionResult struct {
	Lease        ActionLease
	Record       Record
	ResourceKeys []string
}

func generateClaimToken() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return hex.EncodeToString(bytes), nil
}

func actionLeaseDeadline(now, seconds, priorDeadline int64) (int64, error) {
	if now < 0 || seconds <= 0 || seconds > math.MaxInt64-now {
		return 0, ErrActionLeaseExpired
	}
	return max(priorDeadline, now+seconds), nil
}

type parsedActionParameters struct {
	PolicyID       string `json:"policyId"`
	BackupID       string `json:"backupId"`
	SourceTargetID string `json:"sourceTargetId"`
	DestTargetID   string `json:"destTargetId"`
	TargetID       string `json:"targetId"`
	Source         string `json:"source"`
	Destination    string `json:"destination"`
	Target         string `json:"target"`
	FailureDomain  string `json:"failureDomain"`
}

type parsedActionPayload struct {
	PolicyID    string                 `json:"policyId"`
	BackupID    string                 `json:"backupId"`
	Source      string                 `json:"source"`
	Destination string                 `json:"destination"`
	Target      string                 `json:"target"`
	Parameters  parsedActionParameters `json:"parameters"`
	RiskSubject struct {
		PolicyID      string `json:"policyId"`
		FailureDomain string `json:"failureDomain"`
	} `json:"riskSubject"`
}

func parsePayloadMetadata(raw []byte) (parsedActionPayload, error) {
	if len(raw) == 0 {
		return parsedActionPayload{}, nil
	}
	var p parsedActionPayload
	if err := json.Unmarshal(raw, &p); err != nil {
		return parsedActionPayload{}, err
	}
	return p, nil
}

func deriveResourceKeys(payload parsedActionPayload) []string {
	params := payload.Parameters
	pid := params.PolicyID
	if pid == "" {
		pid = payload.PolicyID
	}
	if pid == "" {
		pid = payload.RiskSubject.PolicyID
	}

	bid := params.BackupID
	if bid == "" {
		bid = payload.BackupID
	}

	var keys []string
	if pid != "" && bid != "" {
		keys = append(keys, fmt.Sprintf("backup:%s:%s", pid, bid))
	}
	for _, target := range admissionTargets(payload) {
		keys = append(keys, "target:"+target)
	}
	if pid != "" && bid == "" {
		keys = append(keys, fmt.Sprintf("policy:%s", pid))
	}

	seen := make(map[string]bool, len(keys))
	var unique []string
	for _, k := range keys {
		if !seen[k] {
			seen[k] = true
			unique = append(unique, k)
		}
	}
	sort.Strings(unique)
	return unique
}

func (store *Control) AdmitAndClaimAction(req AdmissionRequest) (AdmissionResult, error) {
	store.mu.Lock()
	defer store.mu.Unlock()

	if store.closed {
		return AdmissionResult{}, ErrWriterFenceHeld
	}
	if !ValidRecordID(req.ActionID) {
		return AdmissionResult{}, ErrEmptyRecordID
	}
	owner := strings.TrimSpace(req.Owner)
	if owner == "" {
		owner = store.owner
	}
	leaseSeconds := req.LeaseSeconds
	if leaseSeconds <= 0 {
		leaseSeconds = 60
	}

	tx, err := store.db.Begin()
	if err != nil {
		return AdmissionResult{}, err
	}
	defer tx.Rollback()

	now := store.now()
	writerLeaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		return AdmissionResult{}, err
	}
	if store.schema != CurrentSchema {
		return AdmissionResult{}, ErrSchemaInactive
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return AdmissionResult{}, err
	}

	// 1. Fetch action record from action_journal
	existing, exists, err := readControlRecord(tx.QueryRow(
		"SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM action_journal WHERE id = ?",
		req.ActionID,
	), "action")
	if err != nil {
		return AdmissionResult{}, err
	}
	if !exists {
		return AdmissionResult{}, ErrActionNotClaimable
	}
	if err := validateControlHistory(tx, existing); err != nil {
		return AdmissionResult{}, err
	}

	// 2. Determine whether fresh claim or takeover
	isFreshClaim := existing.State == "PENDING"
	activeStates := map[string]bool{
		"CLAIMED":        true,
		"EXECUTING":      true,
		"EFFECT_UNKNOWN": true,
	}
	isTakeover := activeStates[existing.State]

	if !isFreshClaim && !isTakeover {
		return AdmissionResult{}, ErrActionNotClaimable
	}

	var nextEpoch uint64
	var nextRevision = existing.Revision + 1
	var targetState string

	existingLease, err := readActiveActionLeaseTx(tx, req.ActionID)
	hasLeaseRow := err == nil
	if err != nil && !errors.Is(err, ErrActionLeaseNotFound) {
		return AdmissionResult{}, err
	}
	var priorManifest string
	var priorKeys []string
	if isTakeover {
		if !hasLeaseRow {
			return AdmissionResult{}, ErrActionLeaseNotFound
		}
		if existingLease.Epoch != existing.ExecutionEpoch {
			return AdmissionResult{}, ErrActionLeaseStale
		}
		priorManifest, _, err = validateActionLeaseResourcesTx(tx, existingLease)
		if err != nil {
			return AdmissionResult{}, err
		}
		if err := json.Unmarshal([]byte(priorManifest), &priorKeys); err != nil {
			return AdmissionResult{}, ErrActionLeaseStale
		}
		if existingLease.LeaseUntil > now {
			return AdmissionResult{}, ErrActionLeaseActive
		}
		if existing.ExecutionEpoch >= math.MaxInt64 {
			return AdmissionResult{}, ErrEpochOutOfRange
		}
		nextEpoch = existing.ExecutionEpoch + 1
		targetState = "EFFECT_UNKNOWN"
	} else {
		if hasLeaseRow {
			return AdmissionResult{}, ErrActionLeaseStale
		}
		var retained int
		if err := tx.QueryRow(`SELECT (SELECT COUNT(*) FROM action_lease_events WHERE action_id=?) +
			(SELECT COUNT(*) FROM action_resource_leases WHERE action_id=?)`, req.ActionID, req.ActionID).Scan(&retained); err != nil {
			return AdmissionResult{}, err
		}
		if retained != 0 {
			return AdmissionResult{}, ErrActionLeaseStale
		}
		// Fresh claim: PENDING -> CLAIMED
		targetState = "CLAIMED"
		nextEpoch = existing.ExecutionEpoch
		if nextEpoch == 0 {
			nextEpoch = 1
		}
	}

	// Parse payload
	payloadMeta, err := parsePayloadMetadata(existing.Payload)
	if err != nil {
		return AdmissionResult{}, ErrInvalidPayload
	}

	// A zero-valued request selects defaults; it never disables admission.
	if err := store.evaluateAdmissionBudgetsTx(tx, req.ActionID, payloadMeta, effectivePolicy(req.Policy), now); err != nil {
		return AdmissionResult{}, err
	}

	if store.admissionFaultStage == "after_budget_check" {
		return AdmissionResult{}, errInjectedAdmissionFault
	}

	// 4. Determine resource keys
	// Explicit resources may add scope, never replace reservations derived from
	// the persisted action. Otherwise a caller could omit a conflicting target.
	keys := append(deriveResourceKeys(payloadMeta), req.ResourceKeys...)
	keys = append(keys, priorKeys...)
	for _, k := range keys {
		if len(k) == 0 || len(k) > 1024 || strings.ContainsRune(k, 0) || !utf8.ValidString(k) {
			return AdmissionResult{}, ErrInvalidPayload
		}
	}
	seenKeys := make(map[string]bool, len(keys))
	uniqueKeys := make([]string, 0, len(keys))
	for _, k := range keys {
		if !seenKeys[k] {
			seenKeys[k] = true
			uniqueKeys = append(uniqueKeys, k)
		}
	}
	sort.Strings(uniqueKeys)
	resourceManifest, err := json.Marshal(uniqueKeys)
	if err != nil || len(resourceManifest) > maximumPayloadBytes {
		return AdmissionResult{}, ErrInvalidPayload
	}
	if isTakeover && string(resourceManifest) != priorManifest {
		return AdmissionResult{}, ErrActionLeaseStale
	}

	// 5. Evaluate and acquire resource reservations
	leaseUntil, err := actionLeaseDeadline(now, leaseSeconds, 0)
	if err != nil {
		return AdmissionResult{}, err
	}
	for idx, key := range uniqueKeys {
		var heldActionID string
		var heldEpoch uint64
		var heldLeaseUntil int64
		rowErr := tx.QueryRow("SELECT action_id, epoch, lease_until FROM action_resource_leases WHERE resource_key = ?", key).
			Scan(&heldActionID, &heldEpoch, &heldLeaseUntil)
		if rowErr == nil {
			if heldActionID != req.ActionID {
				return AdmissionResult{}, fmt.Errorf("%w: resource %q held by %s until %d", ErrResourceConflict, key, heldActionID, heldLeaseUntil)
			}
			// Held by same action -> update
			if _, updateErr := tx.Exec(`UPDATE action_resource_leases
				SET owner = ?, epoch = ?, lease_until = ?, writer_fencing_token = ?, acquired_at = ?
				WHERE resource_key = ?`, owner, int64(nextEpoch), leaseUntil, store.token, now, key); updateErr != nil {
				return AdmissionResult{}, updateErr
			}
		} else if errors.Is(rowErr, sql.ErrNoRows) {
			// Free -> insert
			if _, insertErr := tx.Exec(`INSERT INTO action_resource_leases(
				resource_key, action_id, owner, epoch, acquired_at, lease_until, writer_fencing_token
			) VALUES(?, ?, ?, ?, ?, ?, ?)`, key, req.ActionID, owner, int64(nextEpoch), now, leaseUntil, store.token); insertErr != nil {
				return AdmissionResult{}, insertErr
			}
		} else {
			return AdmissionResult{}, rowErr
		}

		if store.admissionFaultStage == "after_first_resource" && idx == 0 {
			return AdmissionResult{}, errInjectedAdmissionFault
		}
		if store.admissionFaultStage == "mid_resources" && idx == 1 {
			return AdmissionResult{}, errInjectedAdmissionFault
		}
	}

	// 6. Generate claim token
	claimToken, err := generateClaimToken()
	if err != nil {
		return AdmissionResult{}, err
	}

	// 7. Insert or update action_leases
	if isFreshClaim && !hasLeaseRow {
		if _, err := tx.Exec(`INSERT INTO action_leases(
			action_id, owner, epoch, claim_token, lease_until, acquired_at, updated_at,
			claim_revision, writer_fencing_token, terminal_state
		) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)`,
			req.ActionID, owner, int64(nextEpoch), claimToken, leaseUntil, now, now, nextRevision, store.token); err != nil {
			return AdmissionResult{}, err
		}
	} else {
		if _, err := tx.Exec(`INSERT INTO action_leases(
			action_id, owner, epoch, claim_token, lease_until, acquired_at, updated_at,
			claim_revision, writer_fencing_token, terminal_state
		) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
		ON CONFLICT(action_id) DO UPDATE SET
			owner = excluded.owner,
			epoch = excluded.epoch,
			claim_token = excluded.claim_token,
			lease_until = excluded.lease_until,
			acquired_at = excluded.acquired_at,
			updated_at = excluded.updated_at,
			claim_revision = excluded.claim_revision,
			writer_fencing_token = excluded.writer_fencing_token,
			terminal_state = NULL`,
			req.ActionID, owner, int64(nextEpoch), claimToken, leaseUntil, now, now, nextRevision, store.token); err != nil {
			return AdmissionResult{}, err
		}
	}

	// Append to action_lease_events
	eventType := "ADMITTED"
	if isTakeover {
		eventType = "TAKEOVER"
	}
	if _, err := tx.Exec(`INSERT INTO action_lease_events(
		action_id, event_type, owner, epoch, claim_token, lease_until, claim_revision, writer_fencing_token, recorded_at, resource_keys_json
	) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		req.ActionID, eventType, owner, int64(nextEpoch), claimToken, leaseUntil, nextRevision, store.token, now, string(resourceManifest)); err != nil {
		return AdmissionResult{}, err
	}

	if store.admissionFaultStage == "after_action_lease_insert" {
		return AdmissionResult{}, errInjectedAdmissionFault
	}

	// 8. Update action_journal and insert into control_events
	record := Record{
		Domain:         "action",
		ID:             req.ActionID,
		Revision:       nextRevision,
		ExecutionEpoch: nextEpoch,
		State:          targetState,
		Payload:        existing.Payload,
	}
	write := controlRecordWrite{
		record:          record,
		table:           "action_journal",
		allowBoundLease: true,
	}
	if err := store.putControlRecordTx(tx, write, now); err != nil {
		return AdmissionResult{}, err
	}

	if store.admissionFaultStage == "after_control_event_write" {
		return AdmissionResult{}, errInjectedAdmissionFault
	}

	if store.admissionFaultStage == "before_final_writer_fence" {
		return AdmissionResult{}, errInjectedAdmissionFault
	}

	// 9. Pre-commit clock check
	commitNow := store.now()
	if commitNow < 0 || commitNow >= writerLeaseUntil {
		return AdmissionResult{}, ErrWriterFenceHeld
	}
	if commitNow < now || commitNow >= leaseUntil {
		return AdmissionResult{}, ErrActionLeaseExpired
	}

	if store.admissionFaultStage == "at_commit" {
		return AdmissionResult{}, errInjectedAdmissionFault
	}

	// 10. Commit transaction
	if err := tx.Commit(); err != nil {
		return AdmissionResult{}, err
	}
	store.leaseUntil = writerLeaseUntil

	lease := ActionLease{
		ActionID:           req.ActionID,
		Owner:              owner,
		Epoch:              nextEpoch,
		ClaimToken:         claimToken,
		LeaseUntil:         leaseUntil,
		AcquiredAt:         now,
		UpdatedAt:          now,
		ClaimRevision:      nextRevision,
		WriterFencingToken: store.token,
	}

	return AdmissionResult{
		Lease:        lease,
		Record:       record,
		ResourceKeys: uniqueKeys,
	}, nil
}

func (store *Control) evaluateAdmissionBudgetsTx(
	tx *sql.Tx,
	actionID string,
	currentPayload parsedActionPayload,
	policy AdmissionPolicy,
	now int64,
) error {
	// 1. MaxConcurrentActions
	var activeCount int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM action_journal
		WHERE state IN ('CLAIMED', 'EXECUTING', 'EFFECT_UNKNOWN')
		  AND id != ?`, actionID).Scan(&activeCount); err != nil {
		return err
	}
	if activeCount >= policy.MaxConcurrentActions {
		return fmt.Errorf("%w: max concurrent actions %d exceeded (current: %d)", ErrBudgetExceeded, policy.MaxConcurrentActions, activeCount)
	}

	// 2. MaxActionsPerHour
	oneHourAgo := now - 3600
	var hourlyCount int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM control_events
		WHERE domain = 'action'
		  AND state = 'CLAIMED'
		  AND recorded_at >= ?
		  AND record_id != ?`, oneHourAgo, actionID).Scan(&hourlyCount); err != nil {
		return err
	}
	if hourlyCount >= policy.MaxActionsPerHour {
		return fmt.Errorf("%w: max actions per hour %d exceeded (current: %d)", ErrBudgetExceeded, policy.MaxActionsPerHour, hourlyCount)
	}

	// 3. Targets, policy, and failure domain limits
	params := currentPayload.Parameters
	nonBlankTargets := admissionTargets(currentPayload)

	currPolicyID := params.PolicyID
	if currPolicyID == "" {
		currPolicyID = currentPayload.PolicyID
	}
	if currPolicyID == "" {
		currPolicyID = currentPayload.RiskSubject.PolicyID
	}

	currFailureDomain := params.FailureDomain
	if currFailureDomain == "" {
		currFailureDomain = currentPayload.RiskSubject.FailureDomain
	}

	rows, err := tx.Query(`SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM action_journal
		WHERE state IN ('CLAIMED', 'EXECUTING', 'EFFECT_UNKNOWN')
		  AND id != ?`, actionID)
	if err != nil {
		return err
	}
	var peers []Record
	for rows.Next() {
		peer, _, err := readControlRecord(rows, "action")
		if err != nil {
			_ = rows.Close()
			return err
		}
		peers = append(peers, peer)
	}
	rowsErr := rows.Err()
	closeErr := rows.Close()
	if rowsErr != nil {
		return rowsErr
	}
	if closeErr != nil {
		return closeErr
	}

	activeTargetCounts := make(map[string]int)
	activePolicyCount := 0
	activeDomains := make(map[string]bool)

	for _, peer := range peers {
		if err := validateControlHistory(tx, peer); err != nil {
			return err
		}
		lease, err := readActiveActionLeaseTx(tx, peer.ID)
		if err != nil {
			return err
		}
		if lease.Epoch != peer.ExecutionEpoch {
			return ErrActionLeaseStale
		}
		if _, _, err := validateActionLeaseResourcesTx(tx, lease); err != nil {
			return err
		}
		other, err := parsePayloadMetadata(peer.Payload)
		if err != nil {
			return ErrInvalidPayload
		}
		oParams := other.Parameters
		for _, target := range admissionTargets(other) {
			activeTargetCounts[target]++
		}

		oPolicyID := oParams.PolicyID
		if oPolicyID == "" {
			oPolicyID = other.PolicyID
		}
		if oPolicyID == "" {
			oPolicyID = other.RiskSubject.PolicyID
		}
		if currPolicyID != "" && oPolicyID == currPolicyID {
			activePolicyCount++
		}

		oDomain := oParams.FailureDomain
		if oDomain == "" {
			oDomain = other.RiskSubject.FailureDomain
		}
		if oDomain != "" {
			activeDomains[oDomain] = true
		}
	}

	for _, t := range nonBlankTargets {
		if activeTargetCounts[t] >= policy.MaxConcurrentPerTarget {
			return fmt.Errorf("%w: max concurrent actions per target %s reached (%d >= %d)",
				ErrBudgetExceeded, t, activeTargetCounts[t], policy.MaxConcurrentPerTarget)
		}
	}

	if currPolicyID != "" && activePolicyCount >= policy.MaxConcurrentPerPolicy {
		return fmt.Errorf("%w: max concurrent actions per policy %s reached (%d >= %d)",
			ErrBudgetExceeded, currPolicyID, activePolicyCount, policy.MaxConcurrentPerPolicy)
	}

	if currFailureDomain != "" {
		if !activeDomains[currFailureDomain] && len(activeDomains) >= policy.MaxSimultaneousFailureDomainsTouched {
			return fmt.Errorf("%w: max simultaneous failure domains touched reached (%d >= %d)",
				ErrBudgetExceeded, len(activeDomains), policy.MaxSimultaneousFailureDomainsTouched)
		}
	}

	return nil
}

func admissionTargets(payload parsedActionPayload) []string {
	params := payload.Parameters
	seen := make(map[string]bool)
	var targets []string
	for _, value := range []string{params.SourceTargetID, params.DestTargetID, params.TargetID,
		params.Source, params.Destination, params.Target, payload.Source, payload.Destination, payload.Target} {
		value = strings.TrimSpace(value)
		if value != "" && !seen[value] {
			seen[value] = true
			targets = append(targets, value)
		}
	}
	sort.Strings(targets)
	return targets
}

func (store *Control) RenewActionLease(renewal ActionLeaseRenewal) (ActionLease, error) {
	store.mu.Lock()
	defer store.mu.Unlock()

	if store.closed {
		return ActionLease{}, ErrWriterFenceHeld
	}
	if !ValidRecordID(renewal.ActionID) {
		return ActionLease{}, ErrEmptyRecordID
	}
	if renewal.Epoch == 0 || renewal.Epoch > math.MaxInt64 {
		return ActionLease{}, ErrEpochOutOfRange
	}
	if strings.TrimSpace(renewal.ClaimToken) == "" {
		return ActionLease{}, ErrInvalidClaimToken
	}
	leaseSeconds := renewal.LeaseSeconds
	if leaseSeconds <= 0 {
		leaseSeconds = 60
	}

	tx, err := store.db.Begin()
	if err != nil {
		return ActionLease{}, err
	}
	defer tx.Rollback()

	now := store.now()
	writerLeaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		return ActionLease{}, err
	}
	if store.schema != CurrentSchema {
		return ActionLease{}, ErrSchemaInactive
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return ActionLease{}, err
	}

	// 1. Fetch current lease
	var (
		owner         string
		epoch         uint64
		claimToken    string
		leaseUntil    int64
		acquiredAt    int64
		updatedAt     int64
		claimRevision int64
		writerToken   int64
		terminalState sql.NullString
	)
	err = tx.QueryRow(`SELECT owner, epoch, claim_token, lease_until, acquired_at, updated_at,
		claim_revision, writer_fencing_token, terminal_state
		FROM action_leases WHERE action_id = ?`, renewal.ActionID).
		Scan(&owner, &epoch, &claimToken, &leaseUntil, &acquiredAt, &updatedAt, &claimRevision, &writerToken, &terminalState)
	if errors.Is(err, sql.ErrNoRows) {
		return ActionLease{}, ErrActionLeaseNotFound
	}
	if err != nil {
		return ActionLease{}, err
	}

	if terminalState.Valid {
		return ActionLease{}, ErrActionLeaseStale
	}
	if writerToken != store.token {
		return ActionLease{}, ErrActionLeaseStale
	}
	if epoch != renewal.Epoch {
		return ActionLease{}, ErrActionLeaseStale
	}
	if claimToken != renewal.ClaimToken {
		return ActionLease{}, ErrInvalidClaimToken
	}
	if renewal.Owner != "" && owner != renewal.Owner {
		return ActionLease{}, ErrActionLeaseStale
	}
	if leaseUntil <= now || now < updatedAt {
		return ActionLease{}, ErrActionLeaseExpired
	}

	// 2. Verify action record exists and is in active state
	action, exists, err := readControlRecord(tx.QueryRow(`SELECT id, revision, execution_epoch, state,
		payload_json, record_digest, writer_fencing_token, updated_at FROM action_journal WHERE id=?`, renewal.ActionID), "action")
	if err != nil {
		return ActionLease{}, err
	}
	if !exists {
		return ActionLease{}, ErrActionNotClaimable
	}
	if err := validateControlHistory(tx, action); err != nil {
		return ActionLease{}, err
	}
	activeStates := map[string]bool{"CLAIMED": true, "EXECUTING": true, "EFFECT_UNKNOWN": true}
	if !activeStates[action.State] || action.ExecutionEpoch != renewal.Epoch {
		return ActionLease{}, ErrActionLeaseStale
	}
	lease := ActionLease{ActionID: renewal.ActionID, Owner: owner, Epoch: epoch, ClaimToken: claimToken,
		LeaseUntil: leaseUntil, AcquiredAt: acquiredAt, UpdatedAt: updatedAt, ClaimRevision: claimRevision, WriterFencingToken: writerToken}
	resourceManifest, expectedLocks, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return ActionLease{}, err
	}

	// 3. Renew action_leases
	newLeaseUntil, err := actionLeaseDeadline(now, leaseSeconds, leaseUntil)
	if err != nil {
		return ActionLease{}, err
	}
	if _, err := tx.Exec(`UPDATE action_leases
		SET lease_until = ?, updated_at = ?, writer_fencing_token = ?
		WHERE action_id = ? AND epoch = ? AND claim_token = ?`,
		newLeaseUntil, now, store.token, renewal.ActionID, int64(renewal.Epoch), renewal.ClaimToken); err != nil {
		return ActionLease{}, err
	}

	// 4. Renew all resource leases belonging to this action
	res, err := tx.Exec(`UPDATE action_resource_leases
		SET lease_until = ?, writer_fencing_token = ?
		WHERE action_id = ? AND epoch = ?`,
		newLeaseUntil, store.token, renewal.ActionID, int64(renewal.Epoch))
	if err != nil {
		return ActionLease{}, err
	}
	rowsAffected, err := res.RowsAffected()
	if err != nil {
		return ActionLease{}, err
	}
	if int(rowsAffected) != expectedLocks {
		return ActionLease{}, ErrActionLeaseStale
	}

	// 5. Append event
	if _, err := tx.Exec(`INSERT INTO action_lease_events(
		action_id, event_type, owner, epoch, claim_token, lease_until, claim_revision, writer_fencing_token, recorded_at, resource_keys_json
	) VALUES(?, 'RENEWED', ?, ?, ?, ?, ?, ?, ?, ?)`,
		renewal.ActionID, owner, int64(renewal.Epoch), renewal.ClaimToken, newLeaseUntil, claimRevision, store.token, now, resourceManifest); err != nil {
		return ActionLease{}, err
	}

	// 6. Clock check before commit
	commitNow := store.now()
	if commitNow < 0 || commitNow >= writerLeaseUntil {
		return ActionLease{}, ErrWriterFenceHeld
	}
	if commitNow < now || commitNow >= leaseUntil {
		return ActionLease{}, ErrActionLeaseExpired
	}

	if err := tx.Commit(); err != nil {
		return ActionLease{}, err
	}
	store.leaseUntil = writerLeaseUntil

	return ActionLease{
		ActionID:           renewal.ActionID,
		Owner:              owner,
		Epoch:              renewal.Epoch,
		ClaimToken:         renewal.ClaimToken,
		LeaseUntil:         newLeaseUntil,
		AcquiredAt:         acquiredAt,
		UpdatedAt:          now,
		ClaimRevision:      claimRevision,
		WriterFencingToken: store.token,
	}, nil
}

func (store *Control) settleActionTerminalTx(
	actionID string,
	epoch uint64,
	claimToken string,
	targetState string,
	payload json.RawMessage,
) (Record, error) {
	store.mu.Lock()
	defer store.mu.Unlock()

	if store.closed {
		return Record{}, ErrWriterFenceHeld
	}
	if !ValidRecordID(actionID) {
		return Record{}, ErrEmptyRecordID
	}
	if epoch == 0 || epoch > math.MaxInt64 {
		return Record{}, ErrEpochOutOfRange
	}
	if strings.TrimSpace(claimToken) == "" {
		return Record{}, ErrInvalidClaimToken
	}

	tx, err := store.db.Begin()
	if err != nil {
		return Record{}, err
	}
	defer tx.Rollback()

	now := store.now()
	writerLeaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		return Record{}, err
	}
	if store.schema != CurrentSchema {
		return Record{}, ErrSchemaInactive
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return Record{}, err
	}

	// 1. Verify action lease
	var (
		heldOwner     string
		heldEpoch     uint64
		heldToken     string
		leaseUntil    int64
		heldWriter    int64
		acquiredAt    int64
		updatedAt     int64
		claimRevision int64
		terminalState sql.NullString
	)
	err = tx.QueryRow(`SELECT owner, epoch, claim_token, lease_until, writer_fencing_token, terminal_state, acquired_at, updated_at, claim_revision
		FROM action_leases WHERE action_id = ?`, actionID).
		Scan(&heldOwner, &heldEpoch, &heldToken, &leaseUntil, &heldWriter, &terminalState, &acquiredAt, &updatedAt, &claimRevision)
	if errors.Is(err, sql.ErrNoRows) {
		return Record{}, ErrActionLeaseNotFound
	}
	if err != nil {
		return Record{}, err
	}
	if terminalState.Valid {
		return Record{}, ErrActionLeaseStale
	}
	if heldWriter != store.token {
		return Record{}, ErrActionLeaseStale
	}
	if heldEpoch != epoch {
		return Record{}, ErrActionLeaseStale
	}
	if heldToken != claimToken {
		return Record{}, ErrInvalidClaimToken
	}
	if leaseUntil <= now || now < updatedAt {
		return Record{}, ErrActionLeaseExpired
	}
	lease := ActionLease{ActionID: actionID, Owner: heldOwner, Epoch: heldEpoch, ClaimToken: heldToken,
		LeaseUntil: leaseUntil, AcquiredAt: acquiredAt, UpdatedAt: updatedAt, ClaimRevision: claimRevision, WriterFencingToken: heldWriter}
	resourceManifest, _, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return Record{}, err
	}

	// 2. Fetch action record
	existing, exists, err := readControlRecord(tx.QueryRow(
		"SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM action_journal WHERE id = ?",
		actionID,
	), "action")
	if err != nil {
		return Record{}, err
	}
	if !exists {
		return Record{}, ErrActionNotClaimable
	}
	if err := validateControlHistory(tx, existing); err != nil {
		return Record{}, err
	}
	if existing.ExecutionEpoch != epoch {
		return Record{}, ErrActionLeaseStale
	}
	if !LegalTransition("action", existing.State, targetState) {
		return Record{}, ErrIllegalTransition
	}

	recordPayload := payload
	if len(recordPayload) == 0 {
		recordPayload = existing.Payload
	}

	record := Record{
		Domain:         "action",
		ID:             actionID,
		Revision:       existing.Revision + 1,
		ExecutionEpoch: epoch,
		State:          targetState,
		Payload:        recordPayload,
	}
	write, err := prepareControlRecordWrite(record, nil)
	if err != nil {
		return Record{}, err
	}
	write.allowBoundLease = true
	record = write.record
	if err := store.putControlRecordTx(tx, write, now); err != nil {
		return Record{}, err
	}

	// 3. Mark action lease as terminal
	if _, err := tx.Exec(`UPDATE action_leases
		SET terminal_state = ?, updated_at = ?, writer_fencing_token = ?
		WHERE action_id = ? AND epoch = ? AND claim_token = ?`,
		targetState, now, store.token, actionID, int64(epoch), claimToken); err != nil {
		return Record{}, err
	}

	// 4. Release all resource reservations
	if _, err := tx.Exec("DELETE FROM action_resource_leases WHERE action_id = ?", actionID); err != nil {
		return Record{}, err
	}

	// 5. Append event
	if _, err := tx.Exec(`INSERT INTO action_lease_events(
		action_id, event_type, owner, epoch, claim_token, lease_until, claim_revision, writer_fencing_token, recorded_at, resource_keys_json
	) VALUES(?, 'TERMINATED', ?, ?, ?, ?, ?, ?, ?, ?)`,
		actionID, heldOwner, int64(epoch), claimToken, leaseUntil, record.Revision, store.token, now, resourceManifest); err != nil {
		return Record{}, err
	}

	// 6. Commit check
	commitNow := store.now()
	if commitNow < 0 || commitNow >= writerLeaseUntil {
		return Record{}, ErrWriterFenceHeld
	}
	if commitNow < now || commitNow >= leaseUntil {
		return Record{}, ErrActionLeaseExpired
	}

	if err := tx.Commit(); err != nil {
		return Record{}, err
	}
	store.leaseUntil = writerLeaseUntil

	return record, nil
}

func (store *Control) CompleteAction(actionID string, epoch uint64, claimToken string, payload json.RawMessage) (Record, error) {
	return store.settleActionTerminalTx(actionID, epoch, claimToken, "SUCCEEDED", payload)
}

func (store *Control) FailAction(actionID string, epoch uint64, claimToken string, payload json.RawMessage) (Record, error) {
	return store.settleActionTerminalTx(actionID, epoch, claimToken, "FAILED_BEFORE_EFFECT", payload)
}

func (store *Control) GetActionLease(actionID string) (ActionLease, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()

	if store.closed {
		return ActionLease{}, false, ErrWriterFenceHeld
	}
	if store.schema != CurrentSchema {
		return ActionLease{}, false, ErrSchemaInactive
	}
	if !ValidRecordID(actionID) {
		return ActionLease{}, false, ErrEmptyRecordID
	}

	var (
		lease ActionLease
		term  sql.NullString
	)
	err := store.db.QueryRow(`SELECT action_id, owner, epoch, claim_token, lease_until, acquired_at, updated_at,
		claim_revision, writer_fencing_token, terminal_state
		FROM action_leases WHERE action_id = ?`, actionID).
		Scan(&lease.ActionID, &lease.Owner, &lease.Epoch, &lease.ClaimToken, &lease.LeaseUntil,
			&lease.AcquiredAt, &lease.UpdatedAt, &lease.ClaimRevision, &lease.WriterFencingToken, &term)
	if errors.Is(err, sql.ErrNoRows) {
		return ActionLease{}, false, nil
	}
	if err != nil {
		return ActionLease{}, false, err
	}
	if term.Valid {
		return ActionLease{}, false, nil
	}
	return lease, true, nil
}

func (store *Control) GetResourceLeases(actionID string) ([]ActionResourceLease, error) {
	store.mu.Lock()
	defer store.mu.Unlock()

	if store.closed {
		return nil, ErrWriterFenceHeld
	}
	if store.schema != CurrentSchema {
		return nil, ErrSchemaInactive
	}
	if !ValidRecordID(actionID) {
		return nil, ErrEmptyRecordID
	}

	rows, err := store.db.Query(`SELECT resource_key, action_id, owner, epoch, acquired_at, lease_until, writer_fencing_token
		FROM action_resource_leases WHERE action_id = ? ORDER BY resource_key ASC`, actionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var result []ActionResourceLease
	for rows.Next() {
		var item ActionResourceLease
		if err := rows.Scan(&item.ResourceKey, &item.ActionID, &item.Owner, &item.Epoch,
			&item.AcquiredAt, &item.LeaseUntil, &item.WriterFencingToken); err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	return result, rows.Err()
}
