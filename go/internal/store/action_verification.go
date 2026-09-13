package store

import "encoding/json"

// MarkActionVerifying records an observed effect, preserving the original
// action scope and live reservations. It does not certify outcome or risk.
func (store *Control) MarkActionVerifying(actionID string, epoch uint64, claimToken string, observation json.RawMessage) (Record, error) {
	return store.transitionLeasedAction(actionID, epoch, claimToken, "VERIFYING", observation)
}

// MarkActionAssessingEffect records outcome verification before risk assessment.
// The caller must provide qualified evidence; this is only a journal primitive.
func (store *Control) MarkActionAssessingEffect(actionID string, epoch uint64, claimToken string, observation json.RawMessage) (Record, error) {
	return store.transitionLeasedAction(actionID, epoch, claimToken, "ASSESSING_EFFECT", observation)
}

func actionVerificationPayload(original json.RawMessage, state string, observation json.RawMessage) (json.RawMessage, error) {
	// Canonicalization enforces bounded, secret-free input before decoding it.
	canonical, err := canonicalControlPayload(observation)
	if err != nil {
		return nil, err
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(canonical, &object); err != nil || object == nil {
		return nil, ErrInvalidPayload
	}
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(original, &payload); err != nil || payload == nil {
		return nil, ErrInvalidPayload
	}
	key := "nativeEffectObservation"
	if state == "ASSESSING_EFFECT" {
		key = "nativeOutcomeVerification"
	}
	payload[key] = canonical
	// prepareControlRecordWrite also bounds and validates the combined payload.
	return json.Marshal(payload)
}
