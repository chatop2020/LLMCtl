package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	defaultBodyLimit = int64(32 << 20)
	defaultTimeout   = 7200 * time.Second
)

var (
	toolNamePattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)
	envNamePattern  = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,127}$`)
)

type Config struct {
	Version               int                `json:"version"`
	Listen                string             `json:"listen"`
	GatewayBaseURL        string             `json:"gateway_base_url"`
	SharedSecretEnv       string             `json:"shared_secret_env"`
	RequestBodyLimitBytes int64              `json:"request_body_limit_bytes"`
	UpstreamTimeoutMS     int                `json:"upstream_timeout_ms"`
	Models                map[string]Route   `json:"models"`
	Pools                 map[string]Pool    `json:"pools"`
	Adapters              map[string]Adapter `json:"adapters"`
}

type Route struct {
	Enabled       bool     `json:"enabled"`
	Mode          string   `json:"mode"`
	BaseModel     string   `json:"base_model"`
	Pool          string   `json:"pool"`
	Tools         []string `json:"tools"`
	MaxToolRounds int      `json:"max_tool_rounds"`
	SystemPrompt  string   `json:"system_prompt"`
}

type Pool struct {
	Strategy string   `json:"strategy"`
	Targets  []Target `json:"targets"`
}

type Target struct {
	ID        string `json:"id"`
	BaseURL   string `json:"base_url"`
	APIKeyEnv string `json:"api_key_env"`
}

type Adapter struct {
	Kind            string          `json:"kind"`
	Endpoint        string          `json:"endpoint"`
	SecretEnv       string          `json:"secret_env"`
	TimeoutMS       int             `json:"timeout_ms"`
	ResultMaxBytes  int64           `json:"result_max_bytes"`
	ToolDefinition  json.RawMessage `json:"tool_definition"`
	AllowedPurposes []string        `json:"allowed_purposes"`
}

func loadConfig(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}
	return decodeConfig(raw)
}

func decodeConfig(raw []byte) (*Config, error) {
	var cfg Config
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&cfg); err != nil {
		return nil, fmt.Errorf("decode config: %w", err)
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		if err == nil {
			return nil, errors.New("decode config: trailing JSON value")
		}
		return nil, fmt.Errorf("decode config: %w", err)
	}
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	return &cfg, nil
}

func (c *Config) validate() error {
	if c.Version != 1 {
		return fmt.Errorf("unsupported config version %d", c.Version)
	}
	if override := strings.TrimSpace(os.Getenv("LLM_WORKFLOW_LISTEN")); override != "" {
		c.Listen = override
	}
	if strings.TrimSpace(c.Listen) == "" {
		return errors.New("listen is required")
	}
	listenHost, listenPort, err := net.SplitHostPort(strings.TrimSpace(c.Listen))
	if err != nil {
		return errors.New("listen must use host:port syntax")
	}
	portNumber, err := strconv.Atoi(listenPort)
	if err != nil || portNumber < 1 || portNumber > 65535 {
		return errors.New("listen port must be between 1 and 65535")
	}
	c.Listen = net.JoinHostPort(listenHost, strconv.Itoa(portNumber))
	if strings.TrimSpace(c.GatewayBaseURL) == "" {
		c.GatewayBaseURL = defaultGatewayBaseURL(c.Listen)
	}
	gatewayURL, err := url.Parse(c.GatewayBaseURL)
	if err != nil || !validHTTPURL(gatewayURL) {
		return errors.New("gateway_base_url must be an http(s) URL without credentials")
	}
	c.GatewayBaseURL = strings.TrimRight(c.GatewayBaseURL, "/")
	if c.SharedSecretEnv == "" {
		c.SharedSecretEnv = "LLM_WORKFLOW_SHARED_SECRET"
	}
	if !envNamePattern.MatchString(c.SharedSecretEnv) {
		return errors.New("shared_secret_env must be an uppercase environment variable name")
	}
	if len(os.Getenv(c.SharedSecretEnv)) < 24 {
		return fmt.Errorf("environment %s must contain at least 24 characters", c.SharedSecretEnv)
	}
	if c.RequestBodyLimitBytes == 0 {
		c.RequestBodyLimitBytes = defaultBodyLimit
	}
	if c.RequestBodyLimitBytes < 1<<20 || c.RequestBodyLimitBytes > 256<<20 {
		return errors.New("request_body_limit_bytes must be between 1 MiB and 256 MiB")
	}
	if c.UpstreamTimeoutMS == 0 {
		c.UpstreamTimeoutMS = int(defaultTimeout / time.Millisecond)
	}
	if c.UpstreamTimeoutMS < 1000 || c.UpstreamTimeoutMS > int((24*time.Hour)/time.Millisecond) {
		return errors.New("upstream_timeout_ms must be between 1000 and 86400000")
	}
	if len(c.Models) == 0 {
		return errors.New("at least one model route is required")
	}
	for poolID, pool := range c.Pools {
		if strings.TrimSpace(poolID) == "" || len(pool.Targets) == 0 {
			return fmt.Errorf("pool %q must contain targets", poolID)
		}
		if pool.Strategy == "" {
			pool.Strategy = "p2c-least-inflight"
			c.Pools[poolID] = pool
		}
		if pool.Strategy != "p2c-least-inflight" && pool.Strategy != "round-robin" {
			return fmt.Errorf("pool %q has unsupported strategy %q", poolID, pool.Strategy)
		}
		seen := map[string]bool{}
		for index, target := range pool.Targets {
			if target.ID == "" {
				target.ID = fmt.Sprintf("target-%d", index)
				pool.Targets[index] = target
				c.Pools[poolID] = pool
			}
			if seen[target.ID] {
				return fmt.Errorf("pool %q contains duplicate target id %q", poolID, target.ID)
			}
			seen[target.ID] = true
			parsed, err := url.Parse(target.BaseURL)
			if err != nil || !validHTTPURL(parsed) {
				return fmt.Errorf("pool %q target %q has invalid base_url", poolID, target.ID)
			}
			if !envNamePattern.MatchString(target.APIKeyEnv) {
				return fmt.Errorf("pool %q target %q has invalid api_key_env", poolID, target.ID)
			}
			if os.Getenv(target.APIKeyEnv) == "" {
				return fmt.Errorf("pool %q target %q requires populated api_key_env", poolID, target.ID)
			}
		}
	}
	for publicID, route := range c.Models {
		if strings.TrimSpace(publicID) == "" || strings.TrimSpace(route.BaseModel) == "" {
			return fmt.Errorf("model route %q requires public id and base_model", publicID)
		}
		if route.Mode == "" {
			route.Mode = "transparent"
		}
		if route.Mode != "transparent" && route.Mode != "agent" {
			return fmt.Errorf("model route %q has unsupported mode %q", publicID, route.Mode)
		}
		if _, ok := c.Pools[route.Pool]; !ok {
			return fmt.Errorf("model route %q references missing pool %q", publicID, route.Pool)
		}
		if route.MaxToolRounds == 0 {
			route.MaxToolRounds = 4
		}
		if route.MaxToolRounds < 1 || route.MaxToolRounds > 12 {
			return fmt.Errorf("model route %q max_tool_rounds must be 1-12", publicID)
		}
		for _, adapterID := range route.Tools {
			if _, ok := c.Adapters[adapterID]; !ok {
				return fmt.Errorf("model route %q references missing adapter %q", publicID, adapterID)
			}
		}
		c.Models[publicID] = route
	}
	toolNames := map[string]string{}
	for adapterID, adapter := range c.Adapters {
		if adapter.Kind == "" {
			adapter.Kind = "http-json"
		}
		if adapter.Kind != "http-json" {
			return fmt.Errorf("adapter %q has unsupported kind %q", adapterID, adapter.Kind)
		}
		parsed, err := url.Parse(adapter.Endpoint)
		if err != nil || !validHTTPURL(parsed) {
			return fmt.Errorf("adapter %q has invalid endpoint", adapterID)
		}
		if adapter.SecretEnv != "" {
			if !envNamePattern.MatchString(adapter.SecretEnv) {
				return fmt.Errorf("adapter %q has invalid secret_env", adapterID)
			}
			if os.Getenv(adapter.SecretEnv) == "" {
				return fmt.Errorf("adapter %q requires populated secret_env", adapterID)
			}
		}
		if adapter.TimeoutMS == 0 {
			adapter.TimeoutMS = 60000
		}
		if adapter.TimeoutMS < 100 || adapter.TimeoutMS > int((2*time.Hour)/time.Millisecond) {
			return fmt.Errorf("adapter %q timeout_ms is outside 100-7200000", adapterID)
		}
		if adapter.ResultMaxBytes == 0 {
			adapter.ResultMaxBytes = 4 << 20
		}
		if adapter.ResultMaxBytes < 1024 || adapter.ResultMaxBytes > 64<<20 {
			return fmt.Errorf("adapter %q result_max_bytes is outside 1 KiB-64 MiB", adapterID)
		}
		purposes := make([]string, 0, len(adapter.AllowedPurposes))
		seenPurposes := map[string]bool{}
		for _, rawPurpose := range adapter.AllowedPurposes {
			purpose := strings.ToLower(strings.TrimSpace(rawPurpose))
			if purpose == "" || len(purpose) > 64 || !toolNamePattern.MatchString(purpose) {
				return fmt.Errorf("adapter %q contains invalid allowed_purposes value %q", adapterID, rawPurpose)
			}
			if !seenPurposes[purpose] {
				purposes = append(purposes, purpose)
				seenPurposes[purpose] = true
			}
		}
		adapter.AllowedPurposes = purposes
		name, err := adapterToolName(adapter)
		if err != nil {
			return fmt.Errorf("adapter %q: %w", adapterID, err)
		}
		if previous := toolNames[name]; previous != "" {
			return fmt.Errorf("adapters %q and %q expose duplicate tool name %q", previous, adapterID, name)
		}
		toolNames[name] = adapterID
		c.Adapters[adapterID] = adapter
	}
	return nil
}

func validHTTPURL(parsed *url.URL) bool {
	return parsed != nil &&
		(parsed.Scheme == "http" || parsed.Scheme == "https") &&
		parsed.Host != "" && parsed.Hostname() != "" && parsed.User == nil &&
		parsed.RawQuery == "" && parsed.Fragment == ""
}

func defaultGatewayBaseURL(listen string) string {
	host, port, err := net.SplitHostPort(strings.TrimSpace(listen))
	if err != nil {
		return "http://127.0.0.1:18100/v1"
	}
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	return "http://" + net.JoinHostPort(host, port) + "/v1"
}

func adapterToolName(adapter Adapter) (string, error) {
	var definition struct {
		Type     string `json:"type"`
		Function struct {
			Name string `json:"name"`
		} `json:"function"`
	}
	if err := json.Unmarshal(adapter.ToolDefinition, &definition); err != nil {
		return "", fmt.Errorf("invalid tool_definition: %w", err)
	}
	if definition.Type != "function" || !toolNamePattern.MatchString(definition.Function.Name) {
		return "", errors.New("tool_definition must contain a valid function name")
	}
	return definition.Function.Name, nil
}

func (c *Config) enabledModelIDs() []string {
	ids := make([]string, 0, len(c.Models))
	for id, route := range c.Models {
		if route.Enabled {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	return ids
}
