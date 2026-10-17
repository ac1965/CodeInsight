// cifast は、CodeInsight の重い処理（ファイル走査・内容ハッシュ・グラフ探索）を速くするための補助プログラム。
//
// 標準ライブラリだけに依存し、対象のコードを読み取る以外のことをしない（コンパイルも実行も、書き込みもしない。1.1「非侵襲的な解析」）。
// Python 側に同じ結果を返す実装があり、このプログラムが無い・使えない場合は Python 側で処理する（結果は同じでなければならない）。
//
// 入出力: 標準入力から JSON を1件読み、結果を JSON 1件で標準出力へ書く。{"op": "scan"|"hash"|"paths", ...}
package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
)

func main() {
	var request map[string]json.RawMessage
	if err := json.NewDecoder(bufio.NewReader(os.Stdin)).Decode(&request); err != nil {
		fail("入力を読めません: " + err.Error())
	}
	var op string
	_ = json.Unmarshal(request["op"], &op)
	var result any
	switch op {
	case "scan":
		var req scanRequest
		decode(request, &req)
		result = doScan(req)
	case "hash":
		var req hashRequest
		decode(request, &req)
		result = doHash(req)
	case "paths":
		var req pathsRequest
		decode(request, &req)
		result = doPaths(req)
	default:
		fail("未対応の操作です: " + op)
	}
	out := bufio.NewWriter(os.Stdout)
	_ = json.NewEncoder(out).Encode(result)
	_ = out.Flush()
}

func decode(raw map[string]json.RawMessage, into any) {
	buffer, _ := json.Marshal(raw)
	if err := json.Unmarshal(buffer, into); err != nil {
		fail("入力の形式が不正です: " + err.Error())
	}
}

func fail(message string) {
	_ = json.NewEncoder(os.Stdout).Encode(map[string]string{"error": message})
	os.Exit(0)
}

// ---- scan ---------------------------------------------------------------

type scanRequest struct {
	Root           string   `json:"root"`
	GitignoreLines []string `json:"gitignore_lines"` // nil のときは .gitignore を使わない
	UseGitignore   bool     `json:"use_gitignore"`
	ExcludeDirs    []string `json:"exclude_dirs"`
	ExcludeFiles   []string `json:"exclude_files"`
}

type scanResult struct {
	Files       []string `json:"files,omitempty"`
	Unsupported string   `json:"unsupported,omitempty"` // 対応しない .gitignore の書式があった（Python 側で処理する）
}

type ignoreRule struct {
	re      *regexp.Regexp
	include bool // true: 除外する / false: 否定（! で戻す）
}

// compileGitignore は、pathspec の "gitignore" 書式のうち、よく使う範囲（リテラル・*・?・**・先頭/末尾の /・!）を扱う。
// 範囲外（[...]・バックスラッシュ・セグメント内の **）が含まれる場合は ok=false を返し、呼び出し側が Python 側へ任せる。
func compileGitignore(lines []string) ([]ignoreRule, string) {
	var rules []ignoreRule
	for _, line := range lines {
		pattern := strings.TrimRight(line, " \t\r\n\f\v")
		if pattern == "" || strings.HasPrefix(pattern, "#") {
			continue
		}
		if strings.ContainsAny(pattern, "[\\") {
			return nil, "対応しない書式: " + line
		}
		include := true
		if strings.HasPrefix(pattern, "!") {
			include = false
			pattern = pattern[1:]
		}
		segs := strings.Split(pattern, "/")
		isDir := segs[len(segs)-1] == ""
		if pattern == "/" {
			return nil, "対応しない書式: " + line
		}
		if segs[0] == "" {
			segs = segs[1:]
		} else if len(segs) == 1 || (len(segs) == 2 && segs[1] == "") {
			if segs[0] != "**" {
				segs = append([]string{"**"}, segs...)
			}
		}
		if len(segs) == 0 {
			return nil, "対応しない書式: " + line
		}
		if segs[len(segs)-1] == "" {
			segs[len(segs)-1] = "**"
		}
		collapsed := []string{segs[0]}
		for _, s := range segs[1:] {
			if s == "**" && collapsed[len(collapsed)-1] == "**" {
				continue
			}
			collapsed = append(collapsed, s)
		}
		segs = collapsed
		var expr string
		switch {
		case len(segs) == 1 && segs[0] == "**":
			if isDir {
				expr = "/"
			} else {
				expr = "."
			}
		case len(segs) == 2 && segs[0] == "**" && segs[1] == "*":
			expr = "."
		case len(segs) == 3 && segs[0] == "**" && segs[1] == "*" && segs[2] == "**":
			expr = "/"
		default:
			built, reason := translate(segs)
			if reason != "" {
				return nil, reason + ": " + line
			}
			expr = built
		}
		re, err := regexp.Compile(expr)
		if err != nil {
			return nil, "正規表現にできません: " + line
		}
		rules = append(rules, ignoreRule{re: re, include: include})
	}
	return rules, ""
}

func translate(segs []string) (string, string) {
	var out strings.Builder
	needSlash := false
	end := len(segs) - 1
	for i, seg := range segs {
		if seg == "**" {
			switch {
			case i == 0:
				out.WriteString("^(?:.+/)?")
			case i < end:
				out.WriteString("(?:/.+)?")
				needSlash = true
			default:
				out.WriteString("/")
			}
			continue
		}
		if strings.Contains(seg, "**") {
			return "", "セグメント内の **"
		}
		if i == 0 {
			out.WriteString("^")
		}
		if needSlash {
			out.WriteString("/")
		}
		if seg == "*" {
			out.WriteString("[^/]+")
		} else {
			for _, r := range seg {
				switch r {
				case '*':
					out.WriteString("[^/]*")
				case '?':
					out.WriteString("[^/]")
				default:
					out.WriteString(regexp.QuoteMeta(string(r)))
				}
			}
		}
		if i == end {
			if seg == "*" {
				out.WriteString("/?$")
			} else {
				out.WriteString("(?:/|$)")
			}
		}
		needSlash = true
	}
	return out.String(), ""
}

func ignored(rules []ignoreRule, path string) bool {
	result := false
	for _, rule := range rules {
		if rule.re.MatchString(path) {
			result = rule.include
		}
	}
	return result
}

type walker struct {
	root         string
	rules        []ignoreRule
	excludeDirs  map[string]bool
	excludeFiles map[string]bool
	visited      map[string]bool
	files        []string
}

func within(path, root string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func doScan(req scanRequest) scanResult {
	root, err := filepath.EvalSymlinks(req.Root)
	if err != nil {
		return scanResult{}
	}
	root, _ = filepath.Abs(root)
	w := &walker{root: root, excludeDirs: toSet(req.ExcludeDirs), excludeFiles: toSet(req.ExcludeFiles), visited: map[string]bool{}}
	if req.UseGitignore {
		rules, reason := compileGitignore(req.GitignoreLines)
		if reason != "" {
			return scanResult{Unsupported: reason}
		}
		w.rules = rules
	}
	w.walk(root)
	return scanResult{Files: w.files}
}

func toSet(values []string) map[string]bool {
	set := make(map[string]bool, len(values))
	for _, v := range values {
		set[v] = true
	}
	return set
}

func (w *walker) walk(current string) {
	real, err := filepath.EvalSymlinks(current)
	if err != nil {
		return
	}
	if info, err := os.Lstat(current); err == nil && info.Mode()&os.ModeSymlink != 0 && !within(real, w.root) {
		return
	}
	if w.visited[real] {
		return
	}
	w.visited[real] = true
	entries, err := os.ReadDir(current)
	if err != nil {
		return
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	sort.Strings(names)
	for _, name := range names {
		entry := filepath.Join(current, name)
		rel, _ := filepath.Rel(w.root, entry)
		rel = filepath.ToSlash(rel)
		info, err := os.Stat(entry) // リンクを辿った先の種類
		if err != nil {
			continue
		}
		if info.IsDir() {
			if w.excludeDirs[name] || ignored(w.rules, rel+"/") {
				continue
			}
			w.walk(entry)
		} else if info.Mode().IsRegular() {
			if w.excludeFiles[name] {
				continue
			}
			if l, err := os.Lstat(entry); err == nil && l.Mode()&os.ModeSymlink != 0 {
				resolved, err := filepath.EvalSymlinks(entry)
				if err != nil || !within(resolved, w.root) {
					continue
				}
			}
			if ignored(w.rules, rel) {
				continue
			}
			w.files = append(w.files, rel)
		}
	}
}

// ---- hash ---------------------------------------------------------------

type hashRequest struct {
	Paths []string `json:"paths"`
}

type hashResult struct {
	Hashes map[string]*string `json:"hashes"` // 読めなかったファイルは null
}

func doHash(req hashRequest) hashResult {
	result := hashResult{Hashes: make(map[string]*string, len(req.Paths))}
	var mutex sync.Mutex
	jobs := make(chan string)
	var group sync.WaitGroup
	for i := 0; i < runtime.NumCPU(); i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			for path := range jobs {
				digest := sha256File(path)
				mutex.Lock()
				result.Hashes[path] = digest
				mutex.Unlock()
			}
		}()
	}
	for _, p := range req.Paths {
		jobs <- p
	}
	close(jobs)
	group.Wait()
	return result
}

func sha256File(path string) *string {
	file, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer file.Close()
	hasher := sha256.New()
	if _, err := io.Copy(hasher, file); err != nil {
		return nil
	}
	sum := hex.EncodeToString(hasher.Sum(nil))
	return &sum
}

// ---- paths --------------------------------------------------------------

type edge struct {
	ID     int    `json:"id"`
	Target string `json:"target"`
}

type pathsRequest struct {
	Edges    map[string][]edge `json:"edges"` // 起点ごとの、解決済みの呼び出しだけ。順序は Python 側の calls_from と同じ
	Source   string            `json:"source"`
	Target   string            `json:"target"`
	MaxDepth int               `json:"max_depth"`
	Limit    int               `json:"limit"`
}

type pathsResult struct {
	Paths [][]int `json:"paths"` // 辺の id の並び
}

// doPaths は、source から target への単純経路を、Python 側の find_paths と同じ順序・同じ打ち切りで返す
// （Python 側は、上限に達した直後の兄弟の辺でも1件追加しうる。その挙動も同じにする）。
func doPaths(req pathsRequest) pathsResult {
	paths := [][]int{}
	visited := map[string]bool{req.Source: true}
	var trail []int
	var walk func(current string)
	walk = func(current string) {
		if len(paths) >= req.Limit || len(trail) >= req.MaxDepth {
			return
		}
		for _, e := range req.Edges[current] {
			if visited[e.Target] {
				continue
			}
			if e.Target == req.Target {
				paths = append(paths, append(append([]int(nil), trail...), e.ID))
				if len(paths) >= req.Limit {
					return
				}
				continue
			}
			visited[e.Target] = true
			trail = append(trail, e.ID)
			walk(e.Target)
			trail = trail[:len(trail)-1]
			delete(visited, e.Target)
		}
	}
	walk(req.Source)
	return pathsResult{Paths: paths}
}
