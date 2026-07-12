//go:build windows

package main

import (
	"fmt"
	"runtime"
	"syscall"
	"unsafe"
)

const (
	seFileObject             = 1
	ownerSecurityInformation = 0x00000001
	daclSecurityInformation  = 0x00000004
	tokenQuery               = 0x0008
	tokenUserInformation     = 1
	accessAllowedACEType     = 0x00
	accessAllowedObjectACE   = 0x05
	accessAllowedCallbackACE = 0x09
	accessAllowedCallbackObj = 0x0B
	unsafeWriteMask          = 0x10000000 | 0x40000000 | 0x00010000 | 0x00040000 | 0x00080000 | 0x00000002 | 0x00000004 | 0x00000010 | 0x00000040 | 0x00000100
)

type windowsACL struct {
	Revision uint8
	Sbz1     uint8
	Size     uint16
	ACECount uint16
	Sbz2     uint16
}

type windowsACEHeader struct {
	Type  uint8
	Flags uint8
	Size  uint16
}

type windowsSIDAndAttributes struct {
	SID        uintptr
	Attributes uint32
}

type windowsTokenUser struct {
	User windowsSIDAndAttributes
}

var (
	advapi32               = syscall.NewLazyDLL("advapi32.dll")
	localConfigKernel32    = syscall.NewLazyDLL("kernel32.dll")
	getNamedSecurityInfoW  = advapi32.NewProc("GetNamedSecurityInfoW")
	openProcessToken       = advapi32.NewProc("OpenProcessToken")
	getTokenInformation    = advapi32.NewProc("GetTokenInformation")
	equalSID               = advapi32.NewProc("EqualSid")
	isValidSID             = advapi32.NewProc("IsValidSid")
	convertStringSIDToSIDW = advapi32.NewProc("ConvertStringSidToSidW")
	getACE                 = advapi32.NewProc("GetAce")
	getCurrentProcess      = localConfigKernel32.NewProc("GetCurrentProcess")
	closeHandle            = localConfigKernel32.NewProc("CloseHandle")
	localFree              = localConfigKernel32.NewProc("LocalFree")
)

func validateTrustedLocalConfigFile(path string) error {
	pathPointer, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	var owner uintptr
	var dacl uintptr
	var descriptor uintptr
	status, _, _ := getNamedSecurityInfoW.Call(
		uintptr(unsafe.Pointer(pathPointer)),
		seFileObject,
		ownerSecurityInformation|daclSecurityInformation,
		uintptr(unsafe.Pointer(&owner)),
		0,
		uintptr(unsafe.Pointer(&dacl)),
		0,
		uintptr(unsafe.Pointer(&descriptor)),
	)
	if status != 0 || descriptor == 0 || owner == 0 || dacl == 0 {
		return fmt.Errorf("local configuration security descriptor is unavailable")
	}
	defer localFree.Call(descriptor)
	if valid, _, _ := isValidSID.Call(owner); valid == 0 {
		return fmt.Errorf("local configuration owner SID is invalid")
	}
	currentUser, releaseToken, err := currentWindowsUserSID()
	if err != nil {
		return err
	}
	defer releaseToken()
	trusted := []uintptr{currentUser}
	for _, text := range []string{"S-1-5-18", "S-1-5-32-544"} {
		sid, sidErr := windowsSID(text)
		if sidErr != nil {
			return sidErr
		}
		defer localFree.Call(sid)
		trusted = append(trusted, sid)
	}
	if !sidIn(owner, trusted) {
		return fmt.Errorf("local configuration owner is not trusted")
	}
	acl := (*windowsACL)(unsafe.Pointer(dacl))
	if acl.Size < uint16(unsafe.Sizeof(windowsACL{})) || acl.ACECount > 4096 {
		return fmt.Errorf("local configuration DACL is invalid")
	}
	for index := uint16(0); index < acl.ACECount; index++ {
		var ace uintptr
		result, _, _ := getACE.Call(dacl, uintptr(index), uintptr(unsafe.Pointer(&ace)))
		if result == 0 || ace == 0 {
			return fmt.Errorf("local configuration DACL cannot be inspected")
		}
		header := (*windowsACEHeader)(unsafe.Pointer(ace))
		if header.Size < 8 {
			return fmt.Errorf("local configuration DACL contains an invalid ACE")
		}
		mask := *(*uint32)(unsafe.Pointer(ace + 4))
		if mask&unsafeWriteMask == 0 {
			continue
		}
		switch header.Type {
		case accessAllowedACEType:
			sid := ace + 8
			if valid, _, _ := isValidSID.Call(sid); valid == 0 || !sidIn(sid, trusted) {
				return fmt.Errorf("local configuration is writable by an untrusted principal")
			}
		case accessAllowedObjectACE, accessAllowedCallbackACE, accessAllowedCallbackObj:
			return fmt.Errorf("local configuration has a complex writable ACE")
		}
	}
	return nil
}

func currentWindowsUserSID() (uintptr, func(), error) {
	process, _, _ := getCurrentProcess.Call()
	var token uintptr
	opened, _, openErr := openProcessToken.Call(process, tokenQuery, uintptr(unsafe.Pointer(&token)))
	if opened == 0 || token == 0 {
		return 0, func() {}, fmt.Errorf("current process token is unavailable: %w", openErr)
	}
	release := func() { closeHandle.Call(token) }
	var required uint32
	getTokenInformation.Call(token, tokenUserInformation, 0, 0, uintptr(unsafe.Pointer(&required)))
	if required == 0 || required > 64*1024 {
		release()
		return 0, func() {}, fmt.Errorf("current user token identity is invalid")
	}
	buffer := make([]byte, required)
	result, _, infoErr := getTokenInformation.Call(
		token,
		tokenUserInformation,
		uintptr(unsafe.Pointer(&buffer[0])),
		uintptr(required),
		uintptr(unsafe.Pointer(&required)),
	)
	if result == 0 {
		release()
		return 0, func() {}, fmt.Errorf("current user token identity is unavailable: %w", infoErr)
	}
	sid := (*windowsTokenUser)(unsafe.Pointer(&buffer[0])).User.SID
	if valid, _, _ := isValidSID.Call(sid); valid == 0 {
		release()
		return 0, func() {}, fmt.Errorf("current user SID is invalid")
	}
	// Keep both the token and its backing TOKEN_USER buffer alive until the
	// caller finishes every EqualSid operation.
	return sid, func() {
		runtime.KeepAlive(buffer)
		release()
	}, nil
}

func windowsSID(value string) (uintptr, error) {
	pointer, err := syscall.UTF16PtrFromString(value)
	if err != nil {
		return 0, err
	}
	var sid uintptr
	result, _, callErr := convertStringSIDToSIDW.Call(
		uintptr(unsafe.Pointer(pointer)),
		uintptr(unsafe.Pointer(&sid)),
	)
	if result == 0 || sid == 0 {
		return 0, fmt.Errorf("trusted Windows SID is unavailable: %w", callErr)
	}
	return sid, nil
}

func sidIn(candidate uintptr, trusted []uintptr) bool {
	for _, expected := range trusted {
		if equal, _, _ := equalSID.Call(candidate, expected); equal != 0 {
			return true
		}
	}
	return false
}
