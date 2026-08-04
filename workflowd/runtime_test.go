package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

const testSecret = "workflow-test-secret-at-least-24"

func writeTestConfig(t *testing.T, upstreamURL string, adapterURL string, mode string) string {
	t.Helper()
	t.Setenv("TEST_WORKFLOW_SECRET", testSecret)
	t.Setenv("TEST_BACKEND_KEY", "backend-key")
	adapters := map[string]any{}
	tools := []string{}
	if adapterURL != "" {
		adapters["search"] = map[string]any{
			"kind":             "http-json",
			"endpoint":         adapterURL,
			"timeout_ms":       1000,
			"result_max_bytes": 65536,
			"tool_definition": map[string]any{
				"type": "function",
				"function": map[string]any{
					"name":        "web_search",
					"description": "Search approved sources",
					"parameters": map[string]any{
						"type":       "object",
						"properties": map[string]any{"query": map[string]any{"type": "string"}},
					},
				},
			},
		}
		tools = []string{"search"}
	}
	config := map[string]any{
		"version":                  1,
		"listen":                   "127.0.0.1:18100",
		"shared_secret_env":        "TEST_WORKFLOW_SECRET",
		"request_body_limit_bytes": 1048576,
		"upstream_timeout_ms":      5000,
		"models": map[string]any{
			"gdn-inside": map[string]any{
				"enabled":         true,
				"mode":            mode,
				"base_model":      "ornith-internal",
				"pool":            "text",
				"tools":           tools,
				"max_tool_rounds": 3,
			},
		},
		"pools": map[string]any{
			"text": map[string]any{
				"strategy": "p2c-least-inflight",
				"targets": []any{map[string]any{
					"id":          "remote-worker-0",
					"base_url":    upstreamURL + "/v1",
					"api_key_env": "TEST_BACKEND_KEY",
				}},
			},
		},
		"adapters": adapters,
	}
	raw, err := json.Marshal(config)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "workflow.json")
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func newTestHandler(t *testing.T, configPath string) http.Handler {
	t.Helper()
	server, err := newWorkflowServer(configPath, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	return server.handler()
}

func newTestServer(t *testing.T, configPath string) *workflowServer {
	t.Helper()
	server, err := newWorkflowServer(configPath, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	return server
}

func authorizedRequest(t *testing.T, handler http.Handler, payload string) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(payload))
	request.Header.Set("Authorization", "Bearer "+testSecret)
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	return recorder
}

func TestTransparentStreamPreservesPublicModelID(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			t.Fatalf("unexpected upstream path %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer backend-key" {
			t.Fatal("backend key was not forwarded")
		}
		var request map[string]any
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request["model"] != "ornith-internal" {
			t.Fatalf("model was not rewritten: %#v", request["model"])
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"id\":\"c1\",\"model\":\"ornith-internal\",\"choices\":[]}\n\ndata: [DONE]\n\n")
	}))
	defer upstream.Close()

	handler := newTestHandler(t, writeTestConfig(t, upstream.URL, "", "transparent"))
	response := authorizedRequest(t, handler, `{"model":"gdn-inside","stream":true,"messages":[{"role":"user","content":"hello"}]}`)
	if response.Code != http.StatusOK {
		t.Fatalf("unexpected status %d: %s", response.Code, response.Body.String())
	}
	if strings.Contains(response.Body.String(), "ornith-internal") || !strings.Contains(response.Body.String(), `"model":"gdn-inside"`) {
		t.Fatalf("public model id was not preserved: %s", response.Body.String())
	}
}

func TestAgentInvokesConfiguredAdapterAndReturnsFinalAnswer(t *testing.T) {
	var upstreamCalls atomic.Int64
	var adapterCalls atomic.Int64
	adapter := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		adapterCalls.Add(1)
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload["tool"] != "web_search" {
			t.Fatalf("unexpected tool payload %#v", payload)
		}
		writeJSON(w, http.StatusOK, map[string]any{"results": []string{"official result"}})
	}))
	defer adapter.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		call := upstreamCalls.Add(1)
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if call == 1 {
			writeJSON(w, http.StatusOK, map[string]any{
				"id": "plan", "created": 1, "model": "ornith-internal",
				"choices": []any{map[string]any{"message": map[string]any{
					"role": "assistant", "content": nil,
					"tool_calls": []any{map[string]any{
						"id": "tool-1", "type": "function",
						"function": map[string]any{"name": "web_search", "arguments": `{"query":"LLMCtl"}`},
					}},
				}}},
				"usage": map[string]any{
					"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6,
					"prompt_tokens_details": map[string]any{"cached_tokens": 1},
				},
			})
			return
		}
		messages, _ := payload["messages"].([]any)
		if len(messages) < 3 {
			t.Fatalf("tool result was not appended: %#v", messages)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"id": "final", "created": 2, "model": "ornith-internal",
			"choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "final answer"}}},
			"usage": map[string]any{
				"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
				"completion_tokens_details": map[string]any{"reasoning_tokens": 1},
			},
		})
	}))
	defer upstream.Close()

	handler := newTestHandler(t, writeTestConfig(t, upstream.URL, adapter.URL, "agent"))
	response := authorizedRequest(t, handler, `{"model":"gdn-inside","stream":false,"messages":[{"role":"user","content":"research this"}]}`)
	if response.Code != http.StatusOK {
		t.Fatalf("unexpected status %d: %s", response.Code, response.Body.String())
	}
	if upstreamCalls.Load() != 2 || adapterCalls.Load() != 1 {
		t.Fatalf("unexpected calls upstream=%d adapter=%d", upstreamCalls.Load(), adapterCalls.Load())
	}
	if !strings.Contains(response.Body.String(), `"model":"gdn-inside"`) || !strings.Contains(response.Body.String(), "final answer") {
		t.Fatalf("unexpected final response: %s", response.Body.String())
	}
	var document map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &document); err != nil {
		t.Fatal(err)
	}
	usage := document["usage"].(map[string]any)
	if usage["prompt_tokens"] != float64(14) || usage["completion_tokens"] != float64(4) || usage["total_tokens"] != float64(18) {
		t.Fatalf("internal model usage was not aggregated: %#v", usage)
	}
	if usage["prompt_tokens_details"].(map[string]any)["cached_tokens"] != float64(1) || usage["completion_tokens_details"].(map[string]any)["reasoning_tokens"] != float64(1) {
		t.Fatalf("nested usage details were not aggregated: %#v", usage)
	}
}

func TestAgentSeparatesClientAndServerToolCalls(t *testing.T) {
	adapter := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"url": "https://assets.example.test/image.png"})
	}))
	defer adapter.Close()
	var calls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		call := calls.Add(1)
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if call == 1 {
			writeJSON(w, http.StatusOK, map[string]any{
				"id": "mixed", "created": 1, "model": "ornith-internal",
				"choices": []any{map[string]any{"message": map[string]any{
					"role": "assistant", "content": nil,
					"tool_calls": []any{
						map[string]any{"id": "server-1", "type": "function", "function": map[string]any{"name": "web_search", "arguments": `{"query":"image"}`}},
						map[string]any{"id": "client-1", "type": "function", "function": map[string]any{"name": "client_read_file", "arguments": `{"path":"notes.txt"}`}},
					},
				}}},
			})
			return
		}
		messages := payload["messages"].([]any)
		assistant := messages[len(messages)-2].(map[string]any)
		internalCalls := assistant["tool_calls"].([]any)
		if len(internalCalls) != 1 || internalCalls[0].(map[string]any)["id"] != "server-1" {
			t.Fatalf("client tool call leaked into internal round: %#v", internalCalls)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"id": "client", "created": 2, "model": "ornith-internal",
			"choices": []any{map[string]any{"message": map[string]any{
				"role": "assistant", "content": nil,
				"tool_calls": []any{map[string]any{
					"id": "client-2", "type": "function",
					"function": map[string]any{"name": "client_read_file", "arguments": `{"path":"notes.txt"}`},
				}},
			}}},
		})
	}))
	defer upstream.Close()

	path := writeTestConfig(t, upstream.URL, adapter.URL, "agent")
	handler := newTestHandler(t, path)
	response := authorizedRequest(t, handler, `{"model":"gdn-inside","stream":false,"messages":[{"role":"user","content":"work"}],"tools":[{"type":"function","function":{"name":"client_read_file","parameters":{"type":"object"}}}]}`)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "client_read_file") {
		t.Fatalf("client tool call was not returned: %d %s", response.Code, response.Body.String())
	}
}

func TestAdapterPurposeAllowlistIsEnforced(t *testing.T) {
	adapter := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("disallowed adapter invocation reached the upstream adapter")
	}))
	defer adapter.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"id": "plan", "created": 1, "model": "ornith-internal",
			"choices": []any{map[string]any{"message": map[string]any{
				"role": "assistant", "content": nil,
				"tool_calls": []any{map[string]any{
					"id": "tool-1", "type": "function",
					"function": map[string]any{"name": "web_search", "arguments": `{"purpose":"image-edit","query":"test"}`},
				}},
			}}},
		})
	}))
	defer upstream.Close()
	path := writeTestConfig(t, upstream.URL, adapter.URL, "agent")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var config map[string]any
	if err := json.Unmarshal(raw, &config); err != nil {
		t.Fatal(err)
	}
	config["adapters"].(map[string]any)["search"].(map[string]any)["allowed_purposes"] = []string{"web-search"}
	raw, _ = json.Marshal(config)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}

	handler := newTestHandler(t, path)
	response := authorizedRequest(t, handler, `{"model":"gdn-inside","stream":false,"messages":[{"role":"user","content":"test"}]}`)
	if response.Code != http.StatusBadGateway || !strings.Contains(response.Body.String(), "max_tool_rounds") {
		t.Fatalf("expected bounded workflow failure, got %d: %s", response.Code, response.Body.String())
	}
}

func TestRejectsUnknownModelAndInvalidCredential(t *testing.T) {
	upstream := httptest.NewServer(http.NotFoundHandler())
	defer upstream.Close()
	handler := newTestHandler(t, writeTestConfig(t, upstream.URL, "", "transparent"))

	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"gdn-inside"}`))
	request.Header.Set("Authorization", "Bearer wrong")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("expected unauthorized, got %d", recorder.Code)
	}

	response := authorizedRequest(t, handler, `{"model":"missing","messages":[]}`)
	if response.Code != http.StatusNotFound {
		t.Fatalf("expected model not found, got %d", response.Code)
	}
}

func TestAdminConfigUsesRevisionAndReloadsAtomically(t *testing.T) {
	upstream := httptest.NewServer(http.NotFoundHandler())
	defer upstream.Close()
	path := writeTestConfig(t, upstream.URL, "", "transparent")
	server := newTestServer(t, path)
	handler := server.handler()

	get := httptest.NewRequest(http.MethodGet, "/admin/config", nil)
	get.Header.Set("Authorization", "Bearer "+testSecret)
	getResponse := httptest.NewRecorder()
	handler.ServeHTTP(getResponse, get)
	if getResponse.Code != http.StatusOK {
		t.Fatalf("config get failed: %d %s", getResponse.Code, getResponse.Body.String())
	}
	var envelope configEnvelope
	if err := json.Unmarshal(getResponse.Body.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	var config map[string]any
	if err := json.Unmarshal(envelope.Config, &config); err != nil {
		t.Fatal(err)
	}
	config["models"].(map[string]any)["gdn-inside"].(map[string]any)["enabled"] = false
	updated, _ := json.Marshal(config)
	payload, _ := json.Marshal(configEnvelope{Revision: envelope.Revision, Config: updated})
	put := httptest.NewRequest(http.MethodPut, "/admin/config", strings.NewReader(string(payload)))
	put.Header.Set("Authorization", "Bearer "+testSecret)
	putResponse := httptest.NewRecorder()
	handler.ServeHTTP(putResponse, put)
	if putResponse.Code != http.StatusOK {
		t.Fatalf("config put failed: %d %s", putResponse.Code, putResponse.Body.String())
	}
	if server.snapshot.Load().config.Models["gdn-inside"].Enabled {
		t.Fatal("runtime snapshot was not reloaded")
	}

	stale := httptest.NewRequest(http.MethodPut, "/admin/config", strings.NewReader(string(payload)))
	stale.Header.Set("Authorization", "Bearer "+testSecret)
	staleResponse := httptest.NewRecorder()
	handler.ServeHTTP(staleResponse, stale)
	if staleResponse.Code != http.StatusConflict {
		t.Fatalf("expected revision conflict, got %d: %s", staleResponse.Code, staleResponse.Body.String())
	}
}

func TestAdminConfigIsAuthenticated(t *testing.T) {
	upstream := httptest.NewServer(http.NotFoundHandler())
	defer upstream.Close()
	server := newTestServer(t, writeTestConfig(t, upstream.URL, "", "transparent"))
	request := httptest.NewRequest(http.MethodGet, "/admin/config", nil)
	request.Header.Set("Authorization", "Bearer wrong")
	response := httptest.NewRecorder()
	server.handler().ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("expected unauthorized, got %d", response.Code)
	}
}

func TestConfigRejectsInvalidListenAndEmbeddedURLCredentials(t *testing.T) {
	t.Setenv("TEST_WORKFLOW_SECRET", testSecret)
	t.Setenv("TEST_BACKEND_KEY", "backend-key")
	base := `{
        "version":1,
        "listen":"%s",
        "shared_secret_env":"TEST_WORKFLOW_SECRET",
        "models":{"gdn":{"enabled":true,"mode":"transparent","base_model":"base","pool":"p"}},
        "pools":{"p":{"strategy":"round-robin","targets":[{"id":"one","base_url":"%s","api_key_env":"TEST_BACKEND_KEY"}]}},
        "adapters":{}
    }`
	if _, err := decodeConfig([]byte(fmt.Sprintf(base, "127.0.0.1:0", "http://127.0.0.1:8100/v1"))); err == nil {
		t.Fatal("zero listen port was accepted")
	}
	if _, err := decodeConfig([]byte(fmt.Sprintf(base, "127.0.0.1:18100", "https://user:secret@example.test/v1"))); err == nil {
		t.Fatal("embedded upstream credentials were accepted")
	}
	if _, err := decodeConfig([]byte(fmt.Sprintf(base, "127.0.0.1:18100", "https://example.test/v1?token=secret"))); err == nil {
		t.Fatal("upstream URL query credentials were accepted")
	}
	invalidEnv := strings.Replace(
		fmt.Sprintf(base, "127.0.0.1:18100", "http://127.0.0.1:8100/v1"),
		"TEST_BACKEND_KEY", "lowercase-key", 1,
	)
	if _, err := decodeConfig([]byte(invalidEnv)); err == nil {
		t.Fatal("invalid environment variable name was accepted")
	}
}

func TestRoundRobinStartsAtFirstTarget(t *testing.T) {
	pool := newRuntimePool("test", Pool{Strategy: "round-robin", Targets: []Target{{ID: "first"}, {ID: "second"}}})
	first, err := pool.acquire()
	if err != nil {
		t.Fatal(err)
	}
	if first.config.ID != "first" {
		t.Fatalf("first round-robin choice was %q", first.config.ID)
	}
	first.release(true)
	second, err := pool.acquire()
	if err != nil {
		t.Fatal(err)
	}
	if second.config.ID != "second" {
		t.Fatalf("second round-robin choice was %q", second.config.ID)
	}
	second.release(true)
}

func TestTransparentRequestFailsOverBeforeResponse(t *testing.T) {
	second := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"id": "failover", "model": "ornith-internal",
			"choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "ok"}}},
		})
	}))
	defer second.Close()

	path := writeTestConfig(t, second.URL, "", "transparent")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var config map[string]any
	if err := json.Unmarshal(raw, &config); err != nil {
		t.Fatal(err)
	}
	pool := config["pools"].(map[string]any)["text"].(map[string]any)
	pool["strategy"] = "round-robin"
	pool["targets"] = []any{
		map[string]any{"id": "unreachable", "base_url": "http://127.0.0.1:1/v1", "api_key_env": "TEST_BACKEND_KEY"},
		map[string]any{"id": "healthy", "base_url": second.URL + "/v1", "api_key_env": "TEST_BACKEND_KEY"},
	}
	raw, _ = json.Marshal(config)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}

	response := authorizedRequest(t, newTestHandler(t, path), `{"model":"gdn-inside","stream":false,"messages":[{"role":"user","content":"hello"}]}`)
	if response.Code != http.StatusOK {
		t.Fatalf("failover request failed: %d %s", response.Code, response.Body.String())
	}
	if response.Header().Get("X-LLMCtl-Workflow-Target") != "healthy" {
		t.Fatalf("unexpected failover target %q", response.Header().Get("X-LLMCtl-Workflow-Target"))
	}
}

func TestPoolFailoverNeverReselectsAnAttemptedTarget(t *testing.T) {
	pool := newRuntimePool("test", Pool{Strategy: "round-robin", Targets: []Target{{ID: "first"}, {ID: "second"}}})
	first, err := pool.acquireExcluding(nil)
	if err != nil {
		t.Fatal(err)
	}
	first.release(false)
	second, err := pool.acquireExcluding(map[string]bool{"first": true})
	if err != nil {
		t.Fatal(err)
	}
	defer second.release(true)
	if second.config.ID != "second" {
		t.Fatalf("failed target was selected again: %q", second.config.ID)
	}
}

func TestP2CComparesDistinctTargets(t *testing.T) {
	pool := newRuntimePool("test", Pool{Strategy: "p2c-least-inflight", Targets: []Target{{ID: "busy"}, {ID: "idle"}, {ID: "other"}}})
	pool.targets[0].inflight.Store(100)
	selected, err := pool.acquire()
	if err != nil {
		t.Fatal(err)
	}
	defer selected.release(true)
	if selected.config.ID == "busy" {
		t.Fatal("P2C compared the first candidate with itself and selected a saturated target")
	}
}
