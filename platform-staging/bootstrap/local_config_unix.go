//go:build !windows

package main

import (
	"fmt"
	"os"
	"syscall"
)

func validateTrustedLocalConfigFile(path string) error {
	metadata, err := os.Lstat(path)
	if err != nil || !metadata.Mode().IsRegular() || metadata.Mode().Perm()&0o022 != 0 {
		return fmt.Errorf("local configuration permissions are unsafe")
	}
	status, ok := metadata.Sys().(*syscall.Stat_t)
	if !ok || status == nil || status.Uid != uint32(os.Geteuid()) && status.Uid != 0 {
		return fmt.Errorf("local configuration owner is unsafe")
	}
	return nil
}
