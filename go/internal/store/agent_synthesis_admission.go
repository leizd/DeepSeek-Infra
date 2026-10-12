package store

import "database/sql"

// A public task-done projection is insufficient. Admit synthesis only after the
// original task action has verifiable signed settlements, its immutable Go
// checkpoint, and the independently validated terminal lease/resource journal.
// This historical binding also remains necessary during takeover/reconciliation.
func (store *Control) verifiedAgentSynthesisSourceTx(tx *sql.Tx, intent AgentRunExecutionBinding) (Record, error) {
	if intent.Phase != "synthesis" {
		return Record{}, ErrAgentRunExecutionRequired
	}
	rows, err := tx.Query(`SELECT id FROM action_journal WHERE state='SUCCEEDED'
        AND json_extract(payload_json,'$.nativeAgentExecution.runId')=?
        AND json_extract(payload_json,'$.nativeAgentExecution.phase')='tasks'
        AND json_extract(payload_json,'$.nativeAgentTasksCompletion.metadataIndex')=? LIMIT 2`, intent.RunID, intent.MetadataIndex)
	if err != nil {
		return Record{}, err
	}
	ids := make([]string, 0, 2)
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return Record{}, err
		}
		ids = append(ids, id)
	}
	err = rows.Err()
	closeErr := rows.Close()
	if err != nil {
		return Record{}, err
	}
	if closeErr != nil {
		return Record{}, closeErr
	}
	if len(ids) != 1 {
		return Record{}, ErrAgentNodeDependencyUnverified
	}
	source, found, err := readAgentExecutionActionTx(tx, ids[0])
	if err != nil {
		return Record{}, err
	}
	if !found {
		return Record{}, ErrAgentNodeDependencyUnverified
	}
	binding, err := decodeAgentExecution(source)
	if err != nil {
		return Record{}, err
	}
	completion, present, err := readAgentTasksCompletion(source)
	if err != nil {
		return Record{}, err
	}
	if !present || binding.Phase != "tasks" || binding.RunID != intent.RunID || binding.Request != intent.Request ||
		binding.PlanDigest != intent.PlanDigest || binding.MetadataEpoch != intent.MetadataEpoch ||
		completion.MetadataEpoch != intent.MetadataEpoch || completion.MetadataIndex != intent.MetadataIndex {
		return Record{}, ErrAgentNodeDependencyUnverified
	}
	if err := store.validateAgentTasksCompletionTx(tx, source); err != nil {
		return Record{}, err
	}
	return source, nil
}
