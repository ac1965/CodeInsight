package main

import (
	"fmt"

	"example.com/demo/store"
	"example.com/demo/svc"
)

func main() {
	repo := store.NewMemory()
	service := &svc.Service{Repo: repo}
	if err := service.Place("book"); err != nil {
		fmt.Println(err)
	}
	total := count(repo)
	fmt.Println(total, len("x"))
}

func count(r store.Repository) int {
	items, _ := r.List()
	return len(items)
}
