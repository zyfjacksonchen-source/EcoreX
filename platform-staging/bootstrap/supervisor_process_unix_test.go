//go:build !windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestOwnedSupervisorForwardsTerminationSignalsAndWaits(t *testing.T) {
	if os.Getenv("EMATE_SUPERVISOR_SIGNAL_HELPER") == "1" {
		signals := make(chan os.Signal, 1)
		signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
		if err := os.WriteFile(os.Getenv("EMATE_SUPERVISOR_READY"), []byte("ready"), 0o600); err != nil {
			os.Exit(24)
		}
		<-signals
		os.Exit(23)
	}

	for _, value := range []os.Signal{os.Interrupt, syscall.SIGTERM} {
		ready := filepath.Join(t.TempDir(), "ready")
		command := exec.Command(os.Args[0], "-test.run=TestOwnedSupervisorForwardsTerminationSignalsAndWaits")
		command.Env = append(os.Environ(),
			"EMATE_SUPERVISOR_SIGNAL_HELPER=1",
			"EMATE_SUPERVISOR_READY="+ready,
		)
		result := make(chan error, 1)
		go func() { result <- runOwnedSupervisor(command) }()
		deadline := time.Now().Add(5 * time.Second)
		for {
			if _, err := os.Stat(ready); err == nil {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("supervisor signal helper did not become ready")
			}
			time.Sleep(10 * time.Millisecond)
		}
		if err := syscall.Kill(os.Getpid(), value.(syscall.Signal)); err != nil {
			t.Fatal(err)
		}
		select {
		case err := <-result:
			var exit *exec.ExitError
			if !errors.As(err, &exit) || exit.ExitCode() != 23 {
				t.Fatalf("supervisor did not receive %v and exit after it: %v", value, err)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("Bootstrap did not wait for the supervisor after %v", value)
		}
	}
}
