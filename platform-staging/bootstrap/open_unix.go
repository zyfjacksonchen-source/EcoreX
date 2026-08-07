//go:build !windows

package main

import (
	"fmt"
	"os/exec"
	"runtime"
)

func openWebUI(location string) error {
	if runtime.GOOS != "darwin" {
		return fmt.Errorf("WebUI opening is supported on macOS only")
	}
	if err := exec.Command("/usr/bin/open", location).Run(); err != nil {
		return fmt.Errorf("WebUI could not be opened")
	}
	return nil
}

func openPreviewWebUI(location string) error {
	if runtime.GOOS != "darwin" {
		return fmt.Errorf("WebUI opening is supported on macOS only")
	}
	if err := exec.Command("/usr/bin/open", "-n", location).Run(); err != nil {
		return fmt.Errorf("WebUI preview window could not be opened")
	}
	return nil
}
