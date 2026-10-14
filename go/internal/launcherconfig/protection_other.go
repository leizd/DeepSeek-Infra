//go:build !windows

package launcherconfig

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"errors"
	"os"
)

const protectionName = "aes256gcm-private-key-v1"
const settingsAAD = "deepseek-native-launcher-settings/v2"

func key(root *os.Root, create bool) ([]byte, error) {
	if _, err := root.Lstat(keyName); create && errors.Is(err, os.ErrNotExist) {
		value := make([]byte, 32)
		if _, err := rand.Read(value); err != nil {
			return nil, err
		}
		defer clear(value)
		name, err := pending(root, value)
		if err != nil {
			return nil, err
		}
		defer root.Remove(name)
		// Exclusive publication exposes only complete, synced key material.
		if err := root.Link(name, keyName); err != nil && !errors.Is(err, os.ErrExist) {
			return nil, err
		}
	}
	if err := regular(root, keyName, false); err != nil {
		return nil, err
	}
	file, err := root.Open(keyName)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	value := make([]byte, 33)
	n, err := file.Read(value)
	if err != nil || n != 32 {
		clear(value)
		return nil, errors.New("invalid launcher encryption key")
	}
	return value[:32], nil
}

func aead(root *os.Root, create bool) (cipher.AEAD, error) {
	value, err := key(root, create)
	if err != nil {
		return nil, err
	}
	defer clear(value)
	block, err := aes.NewCipher(value)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

func seal(plain []byte, root *os.Root) ([]byte, error) {
	crypto, err := aead(root, true)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, crypto.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	return crypto.Seal(nonce, nonce, plain, []byte(settingsAAD)), nil
}

func unseal(data []byte, root *os.Root) ([]byte, error) {
	crypto, err := aead(root, false)
	if err != nil {
		return nil, err
	}
	if len(data) < crypto.NonceSize()+crypto.Overhead() {
		return nil, errors.New("invalid launcher ciphertext")
	}
	return crypto.Open(nil, data[:crypto.NonceSize()], data[crypto.NonceSize():], []byte(settingsAAD))
}
