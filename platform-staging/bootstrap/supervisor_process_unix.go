//go:build !windows

package main

import (
	"os"
	"os/exec"
	"os/signal"
	"syscall"
)

func runOwnedSupervisor(command *exec.Cmd) error {
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	if err := command.Start(); err != nil {
		return err
	}
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			select {
			case value := <-signals:
				if command.Process.Signal(value) != nil {
					return
				}
			case <-stop:
				return
			}
		}
	}()
	err := command.Wait()
	close(stop)
	<-done
	return err
}
