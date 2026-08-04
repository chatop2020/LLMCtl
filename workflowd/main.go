package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

var buildVersion = "dev"

func main() {
	configPath := flag.String("config", "/var/lib/llm-cluster/workflow/workflow.json", "configuration file")
	checkConfig := flag.Bool("check-config", false, "validate configuration and exit")
	showVersion := flag.Bool("version", false, "print version and exit")
	flag.Parse()
	if *showVersion {
		fmt.Printf("llm-workflowd %s\n", buildVersion)
		return
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	server, err := newWorkflowServer(*configPath, logger)
	if err != nil {
		logger.Error("configuration rejected", "error", err)
		os.Exit(2)
	}
	if *checkConfig {
		fmt.Println("configuration valid")
		return
	}
	snapshot := server.snapshot.Load()
	httpServer := &http.Server{
		Addr:              snapshot.config.Listen,
		Handler:           server.handler(),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}
	stop := make(chan os.Signal, 2)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	go func() {
		for event := range stop {
			if event == syscall.SIGHUP {
				if err := server.reload(); err != nil {
					logger.Error("configuration reload rejected; keeping previous snapshot", "error", err)
				} else {
					logger.Info("configuration reloaded")
				}
				continue
			}
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			_ = httpServer.Shutdown(ctx)
			cancel()
			return
		}
	}()
	logger.Info("workflow data plane listening", "address", snapshot.config.Listen, "version", buildVersion)
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Error("server stopped unexpectedly", "error", err)
		os.Exit(1)
	}
}
