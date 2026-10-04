package svc

import "example.com/demo/store"

// Base は、共通の処理を持つ。
type Base struct{}

// Log は、記録する。
func (b *Base) Log(message string) {}

// Service は、注文の処理。
type Service struct {
	Base
	Repo store.Repository
}

// Place は、注文を受け付ける。
func (s *Service) Place(item string) error {
	s.Log("place " + item)
	return s.Repo.Save(item)
}

func helper() {}
