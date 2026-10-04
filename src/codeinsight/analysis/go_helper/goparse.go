// goparse は、CodeInsight の Go アダプターが使う補助プログラム。
//
// 標準ライブラリの go/parser だけで、Go のソースを構文解析し、宣言・インポート・関数内の呼び出しを JSON で返す。
// 対象のコードをコンパイルも実行もしない（構文木を読むだけ）。外部のモジュールにも依存しない。
//
// 入出力: 標準入力から、1行1件の JSON（{"name": "...", "source": "<base64>"}）を読み、1件ごとに1行の JSON を標準出力へ書く。
package main

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strings"
)

type Call struct {
	Line     int    `json:"line"`
	EndLine  int    `json:"end_line"`
	Name     string `json:"name"`
	Selector bool   `json:"selector,omitempty"`  // x.Name(...) の形（x が識別子でない場合 `a.b.Name()` も含む）
	X        string `json:"x,omitempty"`         // pkg.Func / x.Method の x（識別子の場合）
	RecvType string `json:"recv_type,omitempty"` // x の型（同じ関数内の宣言から、静的に分かる場合）。例: Server / pkg.Client
}

type Decl struct {
	Kind     string   `json:"kind"` // func / method / struct / interface / type / var / const
	Name     string   `json:"name"`
	Recv     string   `json:"recv,omitempty"` // メソッドのレシーバーの型名（ポインタ・型引数を除く）
	Start    int      `json:"start"`
	End      int      `json:"end"`
	Doc      string   `json:"doc,omitempty"`
	Embeds   []string `json:"embeds,omitempty"` // struct / interface に埋め込まれた型
	Methods  []string `json:"methods,omitempty"`
	Calls    []Call   `json:"calls,omitempty"`
	Receiver string   `json:"receiver_name,omitempty"`
}

type Import struct {
	Path  string `json:"path"`
	Alias string `json:"alias,omitempty"`
	Line  int    `json:"line"`
}

type Result struct {
	Package string   `json:"package,omitempty"`
	Imports []Import `json:"imports,omitempty"`
	Decls   []Decl   `json:"decls,omitempty"`
	Errors  []string `json:"errors,omitempty"`
}

type request struct {
	Name   string `json:"name"`
	Source string `json:"source"`
}

func firstLine(group *ast.CommentGroup) string {
	if group == nil {
		return ""
	}
	for _, line := range strings.Split(group.Text(), "\n") {
		if t := strings.TrimSpace(line); t != "" {
			if len(t) > 160 {
				t = t[:159] + "…"
			}
			return t
		}
	}
	return ""
}

// typeName は、型の式から「Name」または「pkg.Name」を返す。ポインタ・型引数は外す。配列・マップ・関数型などは空。
func typeName(expr ast.Expr) string {
	switch t := expr.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.StarExpr:
		return typeName(t.X)
	case *ast.ParenExpr:
		return typeName(t.X)
	case *ast.IndexExpr:
		return typeName(t.X)
	case *ast.IndexListExpr:
		return typeName(t.X)
	case *ast.SelectorExpr:
		if x, ok := t.X.(*ast.Ident); ok {
			return x.Name + "." + t.Sel.Name
		}
	}
	return ""
}

func receiverType(list *ast.FieldList) (string, string) {
	if list == nil || len(list.List) == 0 {
		return "", ""
	}
	field := list.List[0]
	name := ""
	if len(field.Names) > 0 {
		name = field.Names[0].Name
	}
	return name, typeName(field.Type)
}

// localTypes は、関数の中で、型が宣言から静的に分かる変数（レシーバー・引数・`var x T`・`x := T{}`）の型を集める。
// 同じ名前を複数回宣言・代入しているものは、型を確定できないため含めない。
func localTypes(decl *ast.FuncDecl) map[string]string {
	types := map[string]string{}
	counts := map[string]int{}
	add := func(name, typ string) {
		counts[name]++
		if typ != "" {
			types[name] = typ
		}
	}
	if name, typ := receiverType(decl.Recv); name != "" {
		add(name, typ)
	}
	if decl.Type.Params != nil {
		for _, field := range decl.Type.Params.List {
			for _, n := range field.Names {
				add(n.Name, typeName(field.Type))
			}
		}
	}
	if decl.Body == nil {
		return types
	}
	ast.Inspect(decl.Body, func(node ast.Node) bool {
		switch n := node.(type) {
		case *ast.FuncLit:
			return true
		case *ast.ValueSpec:
			for _, name := range n.Names {
				add(name.Name, typeName(n.Type))
			}
		case *ast.AssignStmt:
			for i, lhs := range n.Lhs {
				ident, ok := lhs.(*ast.Ident)
				if !ok || ident.Name == "_" {
					continue
				}
				typ := ""
				if n.Tok == token.DEFINE && len(n.Lhs) == len(n.Rhs) {
					switch rhs := n.Rhs[i].(type) {
					case *ast.CompositeLit:
						typ = typeName(rhs.Type)
					case *ast.UnaryExpr:
						if lit, ok := rhs.X.(*ast.CompositeLit); ok && rhs.Op == token.AND {
							typ = typeName(lit.Type)
						}
					}
				}
				add(ident.Name, typ)
			}
		case *ast.RangeStmt:
			for _, e := range []ast.Expr{n.Key, n.Value} {
				if ident, ok := e.(*ast.Ident); ok {
					add(ident.Name, "")
				}
			}
		}
		return true
	})
	for name, n := range counts {
		if n > 1 {
			delete(types, name)
		}
	}
	return types
}

func collectCalls(fset *token.FileSet, decl *ast.FuncDecl) []Call {
	if decl.Body == nil {
		return nil
	}
	types := localTypes(decl)
	var calls []Call
	ast.Inspect(decl.Body, func(node ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		entry := Call{Line: fset.Position(call.Pos()).Line, EndLine: fset.Position(call.End()).Line}
		switch fun := call.Fun.(type) {
		case *ast.Ident:
			entry.Name = fun.Name
		case *ast.SelectorExpr:
			entry.Name = fun.Sel.Name
			entry.Selector = true
			if x, ok := fun.X.(*ast.Ident); ok {
				entry.X = x.Name
				entry.RecvType = types[x.Name]
			}
		case *ast.IndexExpr:
			if ident, ok := fun.X.(*ast.Ident); ok {
				entry.Name = ident.Name // ジェネリック関数の呼び出し f[T](x)
			}
		}
		if entry.Name != "" {
			calls = append(calls, entry)
		}
		return true
	})
	return calls
}

func analyze(name string, source []byte) Result {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, name, source, parser.ParseComments|parser.SkipObjectResolution)
	result := Result{}
	if err != nil {
		result.Errors = append(result.Errors, err.Error())
		if file == nil {
			return result
		}
	}
	result.Package = file.Name.Name
	for _, spec := range file.Imports {
		path := strings.Trim(spec.Path.Value, "\"`")
		alias := ""
		if spec.Name != nil {
			alias = spec.Name.Name
		}
		result.Imports = append(result.Imports, Import{Path: path, Alias: alias, Line: fset.Position(spec.Pos()).Line})
	}
	for _, d := range file.Decls {
		switch decl := d.(type) {
		case *ast.FuncDecl:
			entry := Decl{Kind: "func", Name: decl.Name.Name, Start: fset.Position(decl.Pos()).Line, End: fset.Position(decl.End()).Line, Doc: firstLine(decl.Doc)}
			if decl.Recv != nil {
				entry.Kind = "method"
				entry.Receiver, entry.Recv = receiverType(decl.Recv)
			}
			entry.Calls = collectCalls(fset, decl)
			result.Decls = append(result.Decls, entry)
		case *ast.GenDecl:
			for _, spec := range decl.Specs {
				switch s := spec.(type) {
				case *ast.TypeSpec:
					entry := Decl{Kind: "type", Name: s.Name.Name, Start: fset.Position(s.Pos()).Line, End: fset.Position(s.End()).Line, Doc: firstLine(decl.Doc)}
					if s.Doc != nil {
						entry.Doc = firstLine(s.Doc)
					}
					switch t := s.Type.(type) {
					case *ast.StructType:
						entry.Kind = "struct"
						for _, f := range t.Fields.List {
							if len(f.Names) == 0 {
								if n := typeName(f.Type); n != "" {
									entry.Embeds = append(entry.Embeds, n)
								}
							}
						}
					case *ast.InterfaceType:
						entry.Kind = "interface"
						for _, f := range t.Methods.List {
							if len(f.Names) == 0 {
								if n := typeName(f.Type); n != "" {
									entry.Embeds = append(entry.Embeds, n)
								}
							}
							for _, n := range f.Names {
								entry.Methods = append(entry.Methods, n.Name)
							}
						}
					}
					result.Decls = append(result.Decls, entry)
				case *ast.ValueSpec:
					kind := "var"
					if decl.Tok == token.CONST {
						kind = "const"
					}
					for _, n := range s.Names {
						if n.Name == "_" {
							continue
						}
						result.Decls = append(result.Decls, Decl{Kind: kind, Name: n.Name, Start: fset.Position(s.Pos()).Line, End: fset.Position(s.End()).Line, Doc: firstLine(decl.Doc)})
					}
				}
			}
		}
	}
	return result
}

func main() {
	reader := bufio.NewReaderSize(os.Stdin, 1<<20)
	writer := bufio.NewWriter(os.Stdout)
	defer writer.Flush()
	for {
		line, err := reader.ReadBytes('\n')
		if len(line) > 0 {
			var req request
			var result Result
			if jerr := json.Unmarshal(line, &req); jerr != nil {
				result = Result{Errors: []string{"不正な要求: " + jerr.Error()}}
			} else if data, derr := base64.StdEncoding.DecodeString(req.Source); derr != nil {
				result = Result{Errors: []string{"不正なソース: " + derr.Error()}}
			} else {
				result = analyze(req.Name, data)
			}
			out, _ := json.Marshal(result)
			fmt.Fprintln(writer, string(out))
			writer.Flush()
		}
		if err != nil {
			return
		}
	}
}
