package agent

import (
	"bytes"
	"encoding/json"
	"io"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

type recoveryProgressJSON struct {
	MetadataProto []byte `json:"metadataProto"`
}

func (progress RunRecoveryProgress) MarshalJSON() ([]byte, error) {
	if progress.Receipt == nil || recoveryInvalidFields(progress.Receipt.ProtoReflect()) {
		return nil, ErrRunRecoveryContextEncoding
	}
	data, err := (proto.MarshalOptions{Deterministic: true}).Marshal(progress.Receipt)
	if err != nil || len(data) == 0 || len(data) > 1<<20 {
		return nil, ErrRunRecoveryContextEncoding
	}
	return json.Marshal(recoveryProgressJSON{MetadataProto: data})
}

func (progress *RunRecoveryProgress) UnmarshalJSON(data []byte) error {
	var value recoveryProgressJSON
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&value) != nil || decoder.Decode(new(any)) != io.EOF || len(value.MetadataProto) == 0 || len(value.MetadataProto) > 1<<20 {
		return ErrRunRecoveryContextEncoding
	}
	var receipt agentv1.AgentRecoveryProgressReceipt
	if proto.Unmarshal(value.MetadataProto, &receipt) != nil || recoveryInvalidFields(receipt.ProtoReflect()) {
		return ErrRunRecoveryContextEncoding
	}
	canonical, err := (proto.MarshalOptions{Deterministic: true}).Marshal(&receipt)
	if err != nil || !bytes.Equal(canonical, value.MetadataProto) {
		return ErrRunRecoveryContextEncoding
	}
	progress.Receipt = &receipt
	return nil
}
