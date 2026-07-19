//go:build windows

package main

import (
	"fmt"
	"syscall"
	"unsafe"
)

var (
	openShell32           = syscall.NewLazyDLL("shell32.dll")
	openProcShellExecuteW = openShell32.NewProc("ShellExecuteW")
)

func openWebUI(location string) error {
	verb, err := syscall.UTF16PtrFromString("open")
	if err != nil {
		return fmt.Errorf("WebUI action is invalid")
	}
	target, err := syscall.UTF16PtrFromString(location)
	if err != nil {
		return fmt.Errorf("WebUI location is invalid")
	}
	result, _, _ := openProcShellExecuteW.Call(
		0,
		uintptr(unsafe.Pointer(verb)),
		uintptr(unsafe.Pointer(target)),
		0,
		0,
		1,
	)
	if result <= 32 {
		return fmt.Errorf("WebUI could not be opened")
	}
	return nil
}
