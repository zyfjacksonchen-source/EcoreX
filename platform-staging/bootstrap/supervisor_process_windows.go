//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"syscall"
	"unsafe"
)

const (
	jobObjectExtendedLimitInformation = 9
	jobObjectLimitKillOnJobClose      = 0x00002000
)

type jobObjectBasicLimitInformation struct {
	PerProcessUserTimeLimit int64
	PerJobUserTimeLimit     int64
	LimitFlags              uint32
	MinimumWorkingSetSize   uintptr
	MaximumWorkingSetSize   uintptr
	ActiveProcessLimit      uint32
	Affinity                uintptr
	PriorityClass           uint32
	SchedulingClass         uint32
}

type jobObjectIOCounters struct {
	ReadOperationCount  uint64
	WriteOperationCount uint64
	OtherOperationCount uint64
	ReadTransferCount   uint64
	WriteTransferCount  uint64
	OtherTransferCount  uint64
}

type jobObjectExtendedLimitInformationValue struct {
	BasicLimitInformation jobObjectBasicLimitInformation
	IOInfo                jobObjectIOCounters
	ProcessMemoryLimit    uintptr
	JobMemoryLimit        uintptr
	PeakProcessMemoryUsed uintptr
	PeakJobMemoryUsed     uintptr
}

var (
	runtimeLifecycleKernel32  = syscall.NewLazyDLL("kernel32.dll")
	createJobObjectW          = runtimeLifecycleKernel32.NewProc("CreateJobObjectW")
	setInformationJobObject   = runtimeLifecycleKernel32.NewProc("SetInformationJobObject")
	assignProcessToJobObject  = runtimeLifecycleKernel32.NewProc("AssignProcessToJobObject")
	getCurrentRuntimeProcess  = runtimeLifecycleKernel32.NewProc("GetCurrentProcess")
	closeRuntimeProcessHandle = runtimeLifecycleKernel32.NewProc("CloseHandle")
)

func createOwnedSupervisorJob() (uintptr, error) {
	job, _, createErr := createJobObjectW.Call(0, 0)
	if job == 0 {
		return 0, fmt.Errorf("Runtime lifecycle job could not be created: %w", createErr)
	}
	fail := func(message string, callErr error) (uintptr, error) {
		closeRuntimeProcessHandle.Call(job)
		return 0, fmt.Errorf("%s: %w", message, callErr)
	}
	limits := jobObjectExtendedLimitInformationValue{}
	limits.BasicLimitInformation.LimitFlags = jobObjectLimitKillOnJobClose
	configured, _, configureErr := setInformationJobObject.Call(
		job,
		jobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&limits)),
		unsafe.Sizeof(limits),
	)
	if configured == 0 {
		return fail("Runtime lifecycle job could not be configured", configureErr)
	}
	process, _, processErr := getCurrentRuntimeProcess.Call()
	if process == 0 {
		return fail("Bootstrap process identity is unavailable", processErr)
	}
	assigned, _, assignErr := assignProcessToJobObject.Call(job, process)
	if assigned == 0 {
		return fail("Bootstrap could not own the Runtime process tree", assignErr)
	}
	return job, nil
}

func runOwnedSupervisor(command *exec.Cmd) error {
	if _, err := createOwnedSupervisorJob(); err != nil {
		return err
	}
	// The Bootstrap belongs to this kill-on-close job too. Keep its sole handle
	// open until process exit, when Windows atomically terminates any descendants.
	return command.Run()
}
