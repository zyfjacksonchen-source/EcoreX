//go:build windows

package main

import (
	"fmt"
	"os"
	"syscall"
	"unsafe"
)

const (
	lockfileExclusiveLock   = 0x00000002
	lockfileFailImmediately = 0x00000001
)

var (
	kernel32       = syscall.NewLazyDLL("kernel32.dll")
	procLockFileEx = kernel32.NewProc("LockFileEx")
	procUnlockFile = kernel32.NewProc("UnlockFileEx")
)

type productLock struct {
	file       *os.File
	overlapped syscall.Overlapped
}

func acquireProductLock(path string) (*productLock, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	lock := &productLock{file: file}
	result, _, _ := procLockFileEx.Call(
		file.Fd(),
		lockfileExclusiveLock|lockfileFailImmediately,
		0,
		1,
		0,
		uintptr(unsafe.Pointer(&lock.overlapped)),
	)
	if result == 0 {
		file.Close()
		return nil, fmt.Errorf("%w", errProductLocked)
	}
	return lock, nil
}

func (lock *productLock) close() {
	if lock == nil || lock.file == nil {
		return
	}
	_, _, _ = procUnlockFile.Call(
		lock.file.Fd(),
		0,
		1,
		0,
		uintptr(unsafe.Pointer(&lock.overlapped)),
	)
	_ = lock.file.Close()
}
