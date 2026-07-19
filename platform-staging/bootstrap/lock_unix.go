//go:build !windows

package main

import (
	"fmt"
	"os"
	"syscall"
)

type productLock struct{ file *os.File }

func acquireProductLock(path string) (*productLock, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		return nil, fmt.Errorf("%w", errProductLocked)
	}
	return &productLock{file: file}, nil
}

func (lock *productLock) close() {
	if lock == nil || lock.file == nil {
		return
	}
	_ = syscall.Flock(int(lock.file.Fd()), syscall.LOCK_UN)
	_ = lock.file.Close()
}
