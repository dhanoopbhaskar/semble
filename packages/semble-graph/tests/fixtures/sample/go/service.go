package service

import "fmt"

type Store struct{}

func (s *Store) Save() {
	fmt.Println("save")
}

func NewStore() *Store {
	return &Store{}
}
