package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

func (s *workflowServer) forwardTransparent(
	ctx context.Context,
	w http.ResponseWriter,
	snapshot *runtimeSnapshot,
	route Route,
	publicModel string,
	requestID string,
	payload map[string]any,
) {
	pool := snapshot.pools[route.Pool]
	payload["model"] = route.BaseModel
	target, response, err := s.openBaseResponse(ctx, snapshot, pool, payload, requestID)
	if err != nil {
		writeOpenAIError(w, http.StatusBadGateway, "upstream connection failed", "server_error", "upstream_unavailable")
		return
	}
	defer response.Body.Close()
	defer target.release(upstreamSuccess(response.StatusCode))
	copySafeHeaders(w.Header(), response.Header)
	w.Header().Set("X-LLMCtl-Workflow-Target", target.config.ID)
	contentType := strings.ToLower(response.Header.Get("Content-Type"))
	if strings.Contains(contentType, "text/event-stream") {
		s.copyEventStream(w, response, publicModel)
		return
	}
	raw, readErr := io.ReadAll(io.LimitReader(response.Body, snapshot.config.RequestBodyLimitBytes+1))
	if readErr != nil {
		writeOpenAIError(w, http.StatusBadGateway, "failed to read upstream response", "server_error", "upstream_read_failed")
		return
	}
	if int64(len(raw)) > snapshot.config.RequestBodyLimitBytes {
		writeOpenAIError(w, http.StatusBadGateway, "upstream response exceeded configured limit", "server_error", "upstream_response_too_large")
		return
	}
	if response.StatusCode >= 200 && response.StatusCode < 300 {
		var document map[string]any
		if json.Unmarshal(raw, &document) == nil {
			document["model"] = publicModel
			raw, _ = json.Marshal(document)
		}
	}
	w.WriteHeader(response.StatusCode)
	_, _ = w.Write(raw)
}

func (s *workflowServer) copyEventStream(w http.ResponseWriter, response *http.Response, publicModel string) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(response.StatusCode)
	flusher, _ := w.(http.Flusher)
	scanner := bufio.NewScanner(response.Body)
	buffer := make([]byte, 64*1024)
	scanner.Buffer(buffer, 8<<20)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "data: ") && line != "data: [DONE]" {
			var event map[string]any
			if json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &event) == nil {
				event["model"] = publicModel
				if rewritten, err := json.Marshal(event); err == nil {
					line = "data: " + string(rewritten)
				}
			}
		}
		_, _ = fmt.Fprintln(w, line)
		if flusher != nil {
			flusher.Flush()
		}
	}
	if err := scanner.Err(); err != nil {
		s.errors.Add(1)
		s.logger.Warn("upstream event stream ended with an error", "error", err, "model", publicModel)
	}
}
