package main

import (
	"fmt"
	"os"
	"sync/atomic"
	"time"
)

type targetState struct {
	config       Target
	inflight     atomic.Int64
	failures     atomic.Int64
	requests     atomic.Uint64
	blockedUntil atomic.Int64
}

type runtimePool struct {
	id       string
	strategy string
	targets  []*targetState
	cursor   atomic.Uint64
}

func newRuntimePool(id string, config Pool) *runtimePool {
	pool := &runtimePool{id: id, strategy: config.Strategy}
	for _, target := range config.Targets {
		pool.targets = append(pool.targets, &targetState{config: target})
	}
	return pool
}

func (p *runtimePool) acquire() (*targetState, error) {
	return p.acquireExcluding(nil)
}

// acquireExcluding is used only for a same-request connection failover. A
// target is excluded after the transport failed before any HTTP response was
// received, so the next attempt cannot immediately select the same endpoint.
func (p *runtimePool) acquireExcluding(excluded map[string]bool) (*targetState, error) {
	if len(p.targets) == 0 {
		return nil, fmt.Errorf("pool %s has no targets", p.id)
	}
	now := time.Now().UnixNano()
	eligible := make([]*targetState, 0, len(p.targets))
	for _, target := range p.targets {
		if !excluded[target.config.ID] && target.blockedUntil.Load() <= now {
			eligible = append(eligible, target)
		}
	}
	// A pool whose remaining targets are all in backoff still gets one chance:
	// backoff is a load-balancing hint, not a permanent availability decision.
	if len(eligible) == 0 {
		for _, target := range p.targets {
			if !excluded[target.config.ID] {
				eligible = append(eligible, target)
			}
		}
	}
	if len(eligible) == 0 {
		return nil, fmt.Errorf("pool %s has no untried targets", p.id)
	}
	var selected *targetState
	sequence := p.cursor.Add(1) - 1
	if p.strategy == "round-robin" || len(eligible) == 1 {
		selected = eligible[sequence%uint64(len(eligible))]
	} else {
		firstIndex := sequence % uint64(len(eligible))
		secondIndex := (sequence*7 + 3) % uint64(len(eligible))
		if secondIndex == firstIndex {
			secondIndex = (firstIndex + 1) % uint64(len(eligible))
		}
		first := eligible[firstIndex]
		second := eligible[secondIndex]
		selected = first
		if targetScore(second) < targetScore(first) {
			selected = second
		}
	}
	selected.inflight.Add(1)
	selected.requests.Add(1)
	return selected, nil
}

func (p *runtimePool) size() int {
	return len(p.targets)
}

func targetScore(target *targetState) int64 {
	return target.inflight.Load()*100 + target.failures.Load()*10
}

func (target *targetState) release(success bool) {
	target.inflight.Add(-1)
	if success {
		target.failures.Store(0)
		target.blockedUntil.Store(0)
		return
	}
	failures := target.failures.Add(1)
	if failures >= 2 {
		backoff := time.Duration(failures)
		if backoff > 30 {
			backoff = 30
		}
		target.blockedUntil.Store(time.Now().Add(backoff * time.Second).UnixNano())
	}
}

func (target *targetState) apiKey() string {
	return os.Getenv(target.config.APIKeyEnv)
}
