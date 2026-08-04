package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

const controlBodyLimit = int64(2 << 20)

type configEnvelope struct {
	Revision string          `json:"revision"`
	Config   json.RawMessage `json:"config"`
}

func configRevision(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func (s *workflowServer) handleAdminConfigGet(w http.ResponseWriter, r *http.Request) {
	snapshot := s.snapshot.Load()
	if snapshot == nil || !s.authorized(r, snapshot) {
		writeOpenAIError(w, http.StatusUnauthorized, "invalid workflow credential", "authentication_error", "invalid_api_key")
		return
	}
	raw, err := os.ReadFile(s.configPath)
	if err != nil {
		writeOpenAIError(w, http.StatusInternalServerError, "workflow configuration is unavailable", "server_error", "config_read_failed")
		return
	}
	writeJSON(w, http.StatusOK, configEnvelope{Revision: configRevision(raw), Config: raw})
}

func (s *workflowServer) handleAdminConfigPut(w http.ResponseWriter, r *http.Request) {
	snapshot := s.snapshot.Load()
	if snapshot == nil || !s.authorized(r, snapshot) {
		writeOpenAIError(w, http.StatusUnauthorized, "invalid workflow credential", "authentication_error", "invalid_api_key")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, controlBodyLimit+1))
	if err != nil || int64(len(raw)) > controlBodyLimit {
		writeOpenAIError(w, http.StatusRequestEntityTooLarge, "configuration request is too large", "invalid_request_error", "request_too_large")
		return
	}
	var envelope configEnvelope
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&envelope); err != nil || len(envelope.Config) == 0 || strings.TrimSpace(envelope.Revision) == "" {
		writeOpenAIError(w, http.StatusBadRequest, "revision and config are required", "invalid_request_error", "invalid_config_envelope")
		return
	}
	if err := s.replaceConfig(envelope.Revision, envelope.Config); err != nil {
		status := http.StatusBadRequest
		code := "invalid_workflow_config"
		if errors.Is(err, errConfigConflict) {
			status = http.StatusConflict
			code = "config_conflict"
		}
		writeOpenAIError(w, status, err.Error(), "invalid_request_error", code)
		return
	}
	current, _ := os.ReadFile(s.configPath)
	writeJSON(w, http.StatusOK, configEnvelope{Revision: configRevision(current), Config: current})
}

var errConfigConflict = errors.New("workflow configuration changed; reload before saving")

func (s *workflowServer) replaceConfig(expectedRevision string, raw []byte) error {
	s.reloadMu.Lock()
	defer s.reloadMu.Unlock()
	current, err := os.ReadFile(s.configPath)
	if err != nil {
		return fmt.Errorf("read current config: %w", err)
	}
	if configRevision(current) != expectedRevision {
		return errConfigConflict
	}
	cfg, err := decodeConfig(raw)
	if err != nil {
		return err
	}
	active := s.snapshot.Load()
	if active != nil && cfg.Listen != active.config.Listen {
		return errors.New("listen address changes require llmctl workflow disable/enable")
	}
	if err := atomicWriteConfig(s.configPath, raw); err != nil {
		return err
	}
	s.installConfig(cfg)
	return nil
}

func atomicWriteConfig(path string, raw []byte) error {
	directory := filepath.Dir(path)
	temporary, err := os.CreateTemp(directory, ".workflow.json.*")
	if err != nil {
		return fmt.Errorf("create temporary config: %w", err)
	}
	name := temporary.Name()
	defer os.Remove(name)
	if err := temporary.Chmod(0o640); err != nil {
		temporary.Close()
		return err
	}
	if _, err := temporary.Write(raw); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(name, path); err != nil {
		return fmt.Errorf("replace config: %w", err)
	}
	if directoryHandle, err := os.Open(directory); err == nil {
		_ = directoryHandle.Sync()
		_ = directoryHandle.Close()
	}
	return nil
}
