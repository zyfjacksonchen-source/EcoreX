//go:build windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"testing"
)

func hardenTestLocalConfig(path string) error {
	current, err := user.Current()
	if err != nil || current.Username == "" {
		return fmt.Errorf("test user identity is unavailable")
	}
	systemRoot := os.Getenv("SystemRoot")
	if systemRoot == "" {
		return fmt.Errorf("Windows system root is unavailable")
	}
	icacls := filepath.Join(systemRoot, "System32", "icacls.exe")
	command := exec.Command(
		icacls,
		path,
		"/inheritance:r",
		"/grant:r",
		current.Username+":(F)",
		"*S-1-5-18:(F)",
		"*S-1-5-32-544:(F)",
	)
	if output, err := command.CombinedOutput(); err != nil {
		return fmt.Errorf("test ACL hardening failed: %v: %s", err, output)
	}
	return nil
}

func TestTrustedLocalConfigRejectsBroadWriteACL(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bootstrap-local.json")
	if err := os.WriteFile(path, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	icacls := filepath.Join(os.Getenv("SystemRoot"), "System32", "icacls.exe")
	command := exec.Command(icacls, path, "/grant", "*S-1-5-11:(M)")
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("could not prepare broad-write ACL: %v: %s", err, output)
	}
	if err := validateTrustedLocalConfigFile(path); err == nil {
		t.Fatal("a local config writable by Authenticated Users was accepted")
	}
}
