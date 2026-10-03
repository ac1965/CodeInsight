;;; extras.el --- 追加機能

(require 'core)

(defun shared-fn (x)
  "extras 側の同名の再定義。"
  (core-greet x))

(if (display-graphic-p)
    (defun plat-fn () 1)
  (defun plat-fn () 2))

(defun extras-call ()
  (plat-fn)
  (undefined-external-fn)
  (cl-loop for i below 3 collect (core-greet i)))
