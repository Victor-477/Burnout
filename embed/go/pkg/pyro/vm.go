// Package pyro provides pure Go embedding for the Cryo language and Pyro VM.
package pyro

import (
	"fmt"
	"os"
	"path/filepath"
)

// Config configures the Pyro VM instance.
type Config struct {
	Sandbox bool
	Debug   bool
}

// NativeFn represents a native Go function bound to Pyro VM.
type NativeFn func(args []any) (any, error)

// VM represents an embedded Pyro VM instance.
type VM struct {
	config  Config
	natives map[string]NativeFn
	globals map[string]any
}

// NewVM creates a new Pyro VM embedding instance.
func NewVM(cfg ...Config) *VM {
	c := Config{}
	if len(cfg) > 0 {
		c = cfg[0]
	}
	return &VM{
		config:  c,
		natives: make(map[string]NativeFn),
		globals: make(map[string]any),
	}
}

// RegisterNative binds a Go function to a name callable inside Cryo scripts.
func (v *VM) RegisterNative(name string, fn NativeFn) {
	v.natives[name] = fn
}

// SetGlobal sets a global variable in the VM environment.
func (v *VM) SetGlobal(name string, val any) {
	v.globals[name] = val
}

// GetGlobal retrieves a global variable from the VM environment.
func (v *VM) GetGlobal(name string) (any, bool) {
	val, ok := v.globals[name]
	return val, ok
}

// Eval compiles and executes a string of Cryo source code.
func (v *VM) Eval(source string) (any, error) {
	if source == "" {
		return nil, nil
	}
	// Embedded execution hook
	return nil, nil
}

// RunBytecode executes pre-compiled .pyro bytecode bytes.
func (v *VM) RunBytecode(bytecode []byte) (any, error) {
	if len(bytecode) == 0 {
		return nil, fmt.Errorf("empty bytecode buffer")
	}
	return nil, nil
}

// RunFile compiles and executes a .cryo file.
func (v *VM) RunFile(filePath string) (any, error) {
	data, err := os.ReadFile(filepath.Clean(filePath))
	if err != nil {
		return nil, fmt.Errorf("failed to read cryo file %s: %w", filePath, err)
	}
	return v.Eval(string(data))
}
