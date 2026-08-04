package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type runtimeSnapshot struct {
	config    *Config
	pools     map[string]*runtimePool
	toolIndex map[string]string
	client    *http.Client
}

type workflowServer struct {
	configPath string
	logger     *slog.Logger
	snapshot   atomic.Pointer[runtimeSnapshot]
	reloadMu   sync.Mutex
	requests   atomic.Uint64
	errors     atomic.Uint64
}

func newWorkflowServer(configPath string, logger *slog.Logger) (*workflowServer, error) {
	server := &workflowServer{configPath: configPath, logger: logger}
	if err := server.reload(); err != nil {
		return nil, err
	}
	return server, nil
}

func (s *workflowServer) reload() error {
	s.reloadMu.Lock()
	defer s.reloadMu.Unlock()
	cfg, err := loadConfig(s.configPath)
	if err != nil {
		return err
	}
	s.installConfig(cfg)
	return nil
}

func (s *workflowServer) installConfig(cfg *Config) {
	snapshot := &runtimeSnapshot{
		config:    cfg,
		pools:     map[string]*runtimePool{},
		toolIndex: map[string]string{},
		client: &http.Client{
			Transport: &http.Transport{
				Proxy:                 http.ProxyFromEnvironment,
				DialContext:           (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
				ForceAttemptHTTP2:     true,
				MaxIdleConns:          512,
				MaxIdleConnsPerHost:   128,
				IdleConnTimeout:       90 * time.Second,
				TLSHandshakeTimeout:   10 * time.Second,
				ExpectContinueTimeout: 1 * time.Second,
				ResponseHeaderTimeout: time.Duration(cfg.UpstreamTimeoutMS) * time.Millisecond,
			},
		},
	}
	for id, pool := range cfg.Pools {
		snapshot.pools[id] = newRuntimePool(id, pool)
	}
	for adapterID, adapter := range cfg.Adapters {
		name, _ := adapterToolName(adapter)
		snapshot.toolIndex[name] = adapterID
	}
	previous := s.snapshot.Swap(snapshot)
	if previous != nil {
		// Reloads must not retain an unbounded chain of idle transports. Closing
		// idle connections is safe for requests still using the old snapshot.
		previous.client.CloseIdleConnections()
	}
	s.logger.Info("configuration loaded", "models", len(cfg.enabledModelIDs()), "pools", len(cfg.Pools), "adapters", len(cfg.Adapters))
}

func (s *workflowServer) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /readyz", s.handleReady)
	mux.HandleFunc("GET /admin/config", s.handleAdminConfigGet)
	mux.HandleFunc("PUT /admin/config", s.handleAdminConfigPut)
	mux.HandleFunc("GET /v1/models", s.handleModels)
	mux.HandleFunc("POST /v1/chat/completions", s.handleChatCompletions)
	return s.withRecovery(mux)
}

func (s *workflowServer) withRecovery(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recovered := recover(); recovered != nil {
				s.errors.Add(1)
				s.logger.Error("request panic", "error", recovered)
				writeOpenAIError(w, http.StatusInternalServerError, "internal workflow error", "server_error", "internal_error")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func (s *workflowServer) authorized(r *http.Request, snapshot *runtimeSnapshot) bool {
	expected := []byte(strings.TrimSpace(getenv(snapshot.config.SharedSecretEnv)))
	provided := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	return len(expected) == len(provided) && subtle.ConstantTimeCompare(expected, []byte(provided)) == 1
}

func getenv(key string) string {
	return strings.TrimSpace(strings.ReplaceAll(os.Getenv(key), "\x00", ""))
}

func (s *workflowServer) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "version": buildVersion})
}

func (s *workflowServer) handleReady(w http.ResponseWriter, _ *http.Request) {
	snapshot := s.snapshot.Load()
	if snapshot == nil || len(snapshot.config.enabledModelIDs()) == 0 {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "not_ready"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ready", "models": len(snapshot.config.enabledModelIDs())})
}

func (s *workflowServer) handleModels(w http.ResponseWriter, r *http.Request) {
	snapshot := s.snapshot.Load()
	if !s.authorized(r, snapshot) {
		writeOpenAIError(w, http.StatusUnauthorized, "invalid workflow credential", "authentication_error", "invalid_api_key")
		return
	}
	models := make([]map[string]any, 0)
	for _, id := range snapshot.config.enabledModelIDs() {
		models = append(models, map[string]any{"id": id, "object": "model", "owned_by": "llmctl"})
	}
	writeJSON(w, http.StatusOK, map[string]any{"object": "list", "data": models})
}

func (s *workflowServer) handleChatCompletions(w http.ResponseWriter, r *http.Request) {
	snapshot := s.snapshot.Load()
	if !s.authorized(r, snapshot) {
		writeOpenAIError(w, http.StatusUnauthorized, "invalid workflow credential", "authentication_error", "invalid_api_key")
		return
	}
	s.requests.Add(1)
	body, err := readLimitedBody(r.Body, snapshot.config.RequestBodyLimitBytes)
	if err != nil {
		writeOpenAIError(w, http.StatusRequestEntityTooLarge, err.Error(), "invalid_request_error", "request_too_large")
		return
	}
	var request map[string]any
	if err := json.Unmarshal(body, &request); err != nil {
		writeOpenAIError(w, http.StatusBadRequest, "request body must be valid JSON", "invalid_request_error", "invalid_json")
		return
	}
	publicModel, _ := request["model"].(string)
	route, ok := snapshot.config.Models[publicModel]
	if !ok || !route.Enabled {
		writeOpenAIError(w, http.StatusNotFound, fmt.Sprintf("model %q is not available", publicModel), "invalid_request_error", "model_not_found")
		return
	}
	requestID := requestIDFrom(r)
	w.Header().Set("X-LLMCtl-Request-ID", requestID)
	if route.Mode == "transparent" || len(route.Tools) == 0 {
		s.forwardTransparent(r.Context(), w, snapshot, route, publicModel, requestID, request)
		return
	}
	if err := s.runAgent(r.Context(), w, snapshot, route, publicModel, requestID, request); err != nil {
		s.errors.Add(1)
		s.logger.Warn("agent request failed", "request_id", requestID, "model", publicModel, "error", err)
		writeOpenAIError(w, http.StatusBadGateway, err.Error(), "workflow_error", "workflow_failed")
	}
}

func readLimitedBody(body io.ReadCloser, limit int64) ([]byte, error) {
	defer body.Close()
	reader := io.LimitReader(body, limit+1)
	raw, err := io.ReadAll(reader)
	if err != nil {
		return nil, fmt.Errorf("read request body: %w", err)
	}
	if int64(len(raw)) > limit {
		return nil, fmt.Errorf("request exceeds %d bytes", limit)
	}
	return raw, nil
}

func requestIDFrom(r *http.Request) string {
	for _, header := range []string{"X-Request-ID", "X-Omniroute-Request-ID"} {
		if value := strings.TrimSpace(r.Header.Get(header)); value != "" && len(value) <= 128 {
			return value
		}
	}
	return fmt.Sprintf("wf-%d", time.Now().UnixNano())
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeOpenAIError(w http.ResponseWriter, status int, message, kind, code string) {
	writeJSON(w, status, map[string]any{"error": map[string]any{"message": message, "type": kind, "code": code}})
}

func cloneJSON(input map[string]any) map[string]any {
	raw, _ := json.Marshal(input)
	var output map[string]any
	_ = json.Unmarshal(raw, &output)
	return output
}

func buildUpstreamRequest(ctx context.Context, target *targetState, payload map[string]any, requestID string) (*http.Request, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	base, err := url.Parse(strings.TrimRight(target.config.BaseURL, "/") + "/chat/completions")
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base.String(), bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	req.Header.Set("Authorization", "Bearer "+target.apiKey())
	req.Header.Set("X-LLMCtl-Request-ID", requestID)
	return req, nil
}

// openBaseResponse retries only transport failures that happen before an HTTP
// response exists. Once an upstream has returned headers (and especially once
// an SSE body has begun), retrying could duplicate a model/tool invocation and
// is therefore deliberately forbidden.
func (s *workflowServer) openBaseResponse(
	ctx context.Context,
	snapshot *runtimeSnapshot,
	pool *runtimePool,
	payload map[string]any,
	requestID string,
) (*targetState, *http.Response, error) {
	if pool == nil {
		return nil, nil, fmt.Errorf("configured upstream pool is unavailable")
	}
	attempted := make(map[string]bool, pool.size())
	var lastErr error
	for len(attempted) < pool.size() {
		if err := ctx.Err(); err != nil {
			return nil, nil, err
		}
		target, err := pool.acquireExcluding(attempted)
		if err != nil {
			lastErr = err
			break
		}
		request, err := buildUpstreamRequest(ctx, target, payload, requestID)
		if err != nil {
			target.release(false)
			return nil, nil, err
		}
		response, err := snapshot.client.Do(request)
		if err == nil {
			return target, response, nil
		}
		target.release(false)
		attempted[target.config.ID] = true
		lastErr = err
		s.logger.Warn(
			"upstream connection failed before response; trying another target",
			"request_id", requestID,
			"pool", pool.id,
			"target", target.config.ID,
			"attempt", len(attempted),
			"targets", pool.size(),
			"error", err,
		)
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("pool %s has no available targets", pool.id)
	}
	return nil, nil, fmt.Errorf("all upstream targets failed before response: %w", lastErr)
}

func upstreamSuccess(status int) bool { return status >= 200 && status < 500 }

func copySafeHeaders(destination, source http.Header) {
	for _, name := range []string{"Content-Type", "Cache-Control", "X-Request-ID", "X-LLMCtl-Request-ID"} {
		if value := source.Get(name); value != "" {
			destination.Set(name, value)
		}
	}
}

func decodeResponse(raw []byte) (map[string]any, error) {
	var response map[string]any
	if err := json.Unmarshal(raw, &response); err != nil {
		return nil, err
	}
	if _, ok := response["error"]; ok {
		return response, errors.New("upstream returned an error response")
	}
	return response, nil
}
