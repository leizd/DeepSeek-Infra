package worker

import (
	"context"
	"strings"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// A missing public pin is permitted only for discovery through an already
// authenticated worker TLS peer. The launcher must retain and enforce that pin
// when reopening a managed profile; this method never authorizes key rotation.
type ControlSignerBindingQuery struct {
	FleetID, Environment, PublicKey string
	FencingToken                    int64
}

func (client *Client) ReadControlSignerBinding(ctx context.Context, query ControlSignerBindingQuery) (ControlSignerBinding, error) {
	if ctx == nil || !validBindingName(query.FleetID) || !validBindingName(query.Environment) || query.FencingToken <= 0 {
		return ControlSignerBinding{}, ErrControlSignerResponse
	}
	if query.PublicKey != "" {
		if _, err := store.SignerKeyIDForPublicKey(query.PublicKey); err != nil {
			return ControlSignerBinding{}, ErrControlSignerResponse
		}
	}
	if client == nil || !client.tlsSecured || !client.bearerAttached {
		return ControlSignerBinding{}, ErrWorkerTLSRequired
	}
	if client.bootstrap == nil {
		return ControlSignerBinding{}, ErrControlSignerResponse
	}
	outbound, err := client.outgoingContext(ctx, "")
	if err != nil {
		return ControlSignerBinding{}, err
	}
	response, err := client.bootstrap.GetBinding(outbound, &actionv1.ControlSignerBindingRequest{
		SchemaVersion: 1, FleetId: query.FleetID, Environment: query.Environment,
		FencingToken: uint64(query.FencingToken), ExpectedSignerPublicKey: query.PublicKey,
	})
	if err != nil {
		return ControlSignerBinding{}, err
	}
	if response == nil || response.SchemaVersion != 1 || len(response.ProtoReflect().GetUnknown()) != 0 ||
		response.FleetId != query.FleetID || response.Environment != query.Environment ||
		response.FencingToken != uint64(query.FencingToken) ||
		(query.PublicKey != "" && query.PublicKey != response.SignerPublicKey) {
		return ControlSignerBinding{}, ErrControlSignerResponse
	}
	keyID, err := store.SignerKeyIDForPublicKey(response.SignerPublicKey)
	if err != nil || keyID != response.SignerKeyId {
		return ControlSignerBinding{}, ErrControlSignerResponse
	}
	return ControlSignerBinding{PublicKey: response.SignerPublicKey, FleetID: response.FleetId, Environment: response.Environment}, nil
}

func validBindingName(value string) bool {
	if len(value) == 0 || len(value) > 128 || strings.TrimSpace(value) != value {
		return false
	}
	for index, char := range []byte(value) {
		alphanumeric := char >= 'a' && char <= 'z' || char >= '0' && char <= '9'
		if !alphanumeric && (index == 0 || char != '.' && char != '_' && char != '-') {
			return false
		}
	}
	return true
}
