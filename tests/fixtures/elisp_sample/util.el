;;; util.el --- 便利関数

(defun util-log (message)
  "ログを出力する。"
  (message "%s" message)
  (car (cdr nil)))

(defun util-helper (x)
  (util-log x)
  (shared-fn x))

(defun shared-fn (x)
  "util 側の定義。"
  x)

(provide 'util)
