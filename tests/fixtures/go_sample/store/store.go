// Package store は、注文の保存先。
package store

// Repository は、注文の保存先の抽象。
type Repository interface {
	Save(item string) error
	List() ([]string, error)
}

// Memory は、メモリ上の保存先。
type Memory struct {
	items []string
}

// NewMemory は、空の保存先を作る。
func NewMemory() *Memory { return &Memory{} }

// Save は、注文を保存する。
func (m *Memory) Save(item string) error {
	m.items = append(m.items, item)
	return nil
}

// List は、保存した注文を返す。
func (m *Memory) List() ([]string, error) {
	return m.copyItems(), nil
}

func (m *Memory) copyItems() []string {
	out := make([]string, len(m.items))
	copy(out, m.items)
	return out
}

const Limit = 100
