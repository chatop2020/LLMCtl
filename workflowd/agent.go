package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

type toolCall struct {
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}

type toolResult struct {
	call    toolCall
	content string
	err     error
}

func (s *workflowServer) runAgent(
	ctx context.Context,
	w http.ResponseWriter,
	snapshot *runtimeSnapshot,
	route Route,
	publicModel string,
	requestID string,
	payload map[string]any,
) error {
	originalStream, _ := payload["stream"].(bool)
	payload["stream"] = false
	payload["model"] = route.BaseModel
	if route.SystemPrompt != "" {
		messages, _ := payload["messages"].([]any)
		payload["messages"] = append([]any{map[string]any{"role": "system", "content": route.SystemPrompt}}, messages...)
	}
	toolNames := map[string]bool{}
	configuredTools := make([]any, 0, len(route.Tools))
	for _, adapterID := range route.Tools {
		adapter := snapshot.config.Adapters[adapterID]
		name, _ := adapterToolName(adapter)
		toolNames[name] = true
	}
	if existing, ok := payload["tools"].([]any); ok {
		for _, definition := range existing {
			// A server-managed tool wins a same-name collision. Sending duplicate
			// function definitions to the model is ambiguous and some OpenAI-
			// compatible backends reject the entire request.
			if name := toolDefinitionName(definition); name == "" || !toolNames[name] {
				configuredTools = append(configuredTools, definition)
			}
		}
	}
	configuredTools = append(configuredTools, serverToolDefinitions(snapshot, route)...)
	payload["tools"] = configuredTools
	payload["tool_choice"] = "auto"
	aggregatedUsage := map[string]any{}

	for round := 0; round < route.MaxToolRounds; round++ {
		response, status, targetID, err := s.callBaseModel(ctx, snapshot, route, requestID, payload)
		if err != nil {
			return err
		}
		if status < 200 || status >= 300 {
			return fmt.Errorf("base model returned HTTP %d", status)
		}
		mergeUsage(aggregatedUsage, response["usage"])
		message, calls, err := extractAssistantMessage(response)
		if err != nil {
			return err
		}
		serverCalls := make([]toolCall, 0)
		for _, call := range calls {
			if toolNames[call.Function.Name] {
				serverCalls = append(serverCalls, call)
			}
		}
		if len(serverCalls) == 0 {
			response["model"] = publicModel
			if len(aggregatedUsage) > 0 {
				response["usage"] = aggregatedUsage
			}
			w.Header().Set("X-LLMCtl-Workflow-Target", targetID)
			w.Header().Set("X-LLMCtl-Workflow-Rounds", fmt.Sprintf("%d", round))
			if originalStream {
				writeSyntheticStream(w, response, publicModel)
			} else {
				writeJSON(w, http.StatusOK, response)
			}
			return nil
		}
		messages, ok := payload["messages"].([]any)
		if !ok {
			return fmt.Errorf("messages must be an array")
		}
		// If a model emitted both client-owned and server-owned tools, append
		// only the server-owned calls for this internal round. Otherwise the
		// next OpenAI request would contain tool_call IDs with no matching tool
		// result. Client-owned calls remain available and may be requested in
		// the next/final response returned to the caller.
		internalMessage := cloneJSON(message)
		internalMessage["tool_calls"] = serverCalls
		messages = append(messages, internalMessage)
		results := s.invokeTools(ctx, snapshot, serverCalls, publicModel, requestID)
		for _, result := range results {
			if result.err != nil {
				result.content = compactToolError(result.err)
			}
			messages = append(messages, map[string]any{
				"role":         "tool",
				"tool_call_id": result.call.ID,
				"name":         result.call.Function.Name,
				"content":      result.content,
			})
		}
		payload["messages"] = messages
	}
	return fmt.Errorf("workflow exceeded max_tool_rounds=%d", route.MaxToolRounds)
}

func serverToolDefinitions(snapshot *runtimeSnapshot, route Route) []any {
	definitions := make([]any, 0, len(route.Tools))
	for _, adapterID := range route.Tools {
		adapter := snapshot.config.Adapters[adapterID]
		var definition any
		if json.Unmarshal(adapter.ToolDefinition, &definition) == nil {
			definitions = append(definitions, definition)
		}
	}
	return definitions
}

func toolDefinitionName(value any) string {
	definition, ok := value.(map[string]any)
	if !ok {
		return ""
	}
	function, ok := definition["function"].(map[string]any)
	if !ok {
		return ""
	}
	name, _ := function["name"].(string)
	return name
}

// mergeUsage recursively adds numeric OpenAI usage fields. This includes the
// standard prompt/completion/total fields and nested cache/reasoning details,
// so the upstream gateway bills every internal model round exactly once.
func mergeUsage(destination map[string]any, raw any) {
	source, ok := raw.(map[string]any)
	if !ok {
		return
	}
	for key, value := range source {
		switch current := value.(type) {
		case float64:
			previous, _ := destination[key].(float64)
			destination[key] = previous + current
		case map[string]any:
			nested, _ := destination[key].(map[string]any)
			if nested == nil {
				nested = map[string]any{}
				destination[key] = nested
			}
			mergeUsage(nested, current)
		}
	}
}

func (s *workflowServer) callBaseModel(
	ctx context.Context,
	snapshot *runtimeSnapshot,
	route Route,
	requestID string,
	payload map[string]any,
) (map[string]any, int, string, error) {
	pool := snapshot.pools[route.Pool]
	target, response, err := s.openBaseResponse(ctx, snapshot, pool, payload, requestID)
	if err != nil {
		return nil, 0, "", err
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, snapshot.config.RequestBodyLimitBytes+1))
	if err != nil {
		target.release(false)
		return nil, response.StatusCode, target.config.ID, err
	}
	if int64(len(raw)) > snapshot.config.RequestBodyLimitBytes {
		target.release(false)
		return nil, response.StatusCode, target.config.ID, fmt.Errorf("base model response exceeded configured limit")
	}
	target.release(upstreamSuccess(response.StatusCode))
	document, decodeErr := decodeResponse(raw)
	if decodeErr != nil && response.StatusCode >= 200 && response.StatusCode < 300 {
		return nil, response.StatusCode, target.config.ID, decodeErr
	}
	return document, response.StatusCode, target.config.ID, nil
}

func extractAssistantMessage(response map[string]any) (map[string]any, []toolCall, error) {
	choices, ok := response["choices"].([]any)
	if !ok || len(choices) == 0 {
		return nil, nil, fmt.Errorf("base model response has no choices")
	}
	choice, ok := choices[0].(map[string]any)
	if !ok {
		return nil, nil, fmt.Errorf("base model choice is invalid")
	}
	message, ok := choice["message"].(map[string]any)
	if !ok {
		return nil, nil, fmt.Errorf("base model response has no assistant message")
	}
	rawCalls, _ := json.Marshal(message["tool_calls"])
	var calls []toolCall
	if string(rawCalls) != "null" {
		if err := json.Unmarshal(rawCalls, &calls); err != nil {
			return nil, nil, fmt.Errorf("decode tool calls: %w", err)
		}
	}
	return message, calls, nil
}

func (s *workflowServer) invokeTools(
	ctx context.Context,
	snapshot *runtimeSnapshot,
	calls []toolCall,
	publicModel string,
	requestID string,
) []toolResult {
	results := make([]toolResult, len(calls))
	var wait sync.WaitGroup
	for index, call := range calls {
		wait.Add(1)
		go func(index int, call toolCall) {
			defer wait.Done()
			adapterID := snapshot.toolIndex[call.Function.Name]
			adapter := snapshot.config.Adapters[adapterID]
			content, err := invokeHTTPAdapter(ctx, snapshot.client, adapter, call, publicModel, requestID)
			results[index] = toolResult{call: call, content: content, err: err}
		}(index, call)
	}
	wait.Wait()
	return results
}

func invokeHTTPAdapter(ctx context.Context, client *http.Client, adapter Adapter, call toolCall, publicModel, requestID string) (string, error) {
	var arguments any
	if err := json.Unmarshal([]byte(call.Function.Arguments), &arguments); err != nil {
		arguments = call.Function.Arguments
	}
	if len(adapter.AllowedPurposes) > 0 {
		argumentMap, ok := arguments.(map[string]any)
		purpose, purposeOK := argumentMap["purpose"].(string)
		if !ok || !purposeOK || !purposeAllowed(adapter, purpose) {
			return "", fmt.Errorf(
				"adapter purpose %q is not allowed; expected one of %s",
				strings.TrimSpace(purpose), strings.Join(adapter.AllowedPurposes, ","),
			)
		}
	}
	payload := map[string]any{
		"tool":      call.Function.Name,
		"arguments": arguments,
		"context": map[string]any{
			"request_id": requestID,
			"model":      publicModel,
		},
	}
	raw, _ := json.Marshal(payload)
	timeout := time.Duration(adapter.TimeoutMS) * time.Millisecond
	callContext, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	request, err := http.NewRequestWithContext(callContext, http.MethodPost, adapter.Endpoint, bytes.NewReader(raw))
	if err != nil {
		return "", err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	request.Header.Set("X-LLMCtl-Request-ID", requestID)
	if adapter.SecretEnv != "" {
		request.Header.Set("Authorization", "Bearer "+os.Getenv(adapter.SecretEnv))
	}
	response, err := client.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	result, err := io.ReadAll(io.LimitReader(response.Body, adapter.ResultMaxBytes+1))
	if err != nil {
		return "", err
	}
	if int64(len(result)) > adapter.ResultMaxBytes {
		return "", fmt.Errorf("adapter response exceeded %d bytes", adapter.ResultMaxBytes)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return "", fmt.Errorf("adapter returned HTTP %d", response.StatusCode)
	}
	if !json.Valid(result) {
		return string(result), nil
	}
	var compact bytes.Buffer
	if err := json.Compact(&compact, result); err != nil {
		return string(result), nil
	}
	return compact.String(), nil
}

func compactToolError(err error) string {
	raw, _ := json.Marshal(map[string]any{"ok": false, "error": err.Error()})
	return string(raw)
}

func writeSyntheticStream(w http.ResponseWriter, response map[string]any, publicModel string) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
	w.Header().Set("X-Accel-Buffering", "no")
	w.Header().Set("X-LLMCtl-Workflow-Buffered", "true")
	message, _, err := extractAssistantMessage(response)
	if err != nil {
		writeOpenAIError(w, http.StatusBadGateway, err.Error(), "workflow_error", "invalid_final_response")
		return
	}
	chunk := map[string]any{
		"id":      response["id"],
		"object":  "chat.completion.chunk",
		"created": response["created"],
		"model":   publicModel,
		"choices": []any{map[string]any{
			"index":         0,
			"delta":         message,
			"finish_reason": "stop",
		}},
	}
	if usage, ok := response["usage"]; ok {
		chunk["usage"] = usage
	}
	raw, _ := json.Marshal(chunk)
	_, _ = fmt.Fprintf(w, "data: %s\n\ndata: [DONE]\n\n", raw)
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
}

func purposeAllowed(adapter Adapter, purpose string) bool {
	if len(adapter.AllowedPurposes) == 0 {
		return true
	}
	for _, candidate := range adapter.AllowedPurposes {
		if strings.EqualFold(candidate, purpose) {
			return true
		}
	}
	return false
}
