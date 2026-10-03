;;; core.el --- コアの機能  -*- lexical-binding: t; -*-

(require 'cl-lib)
(require 'util)

(defvar core-counter 0
  "カウンタ（括弧 ( を含む説明）。")

(defcustom core-name "demo" "名前。" :type 'string)

(defun core-open-paren-p (ch)
  "文字が開き括弧か。"
  (or (eq ch ?\() (eq ch ?\))))     ; 文字リテラルの括弧

(defun core-greet (who)
  "WHO に挨拶する。"
  (let ((msg (format "hello %s" who))     ; msg は局所変数
        (n core-counter))
    (setq core-counter (1+ n))
    (util-log msg)
    (when (core-open-paren-p ?\()
      (util-helper who))
    msg))

(defmacro core-with-log (&rest body)
  "BODY を、ログつきで実行する。"
  `(progn (util-log "start") ,@body))

(cl-defstruct (core-item (:constructor core-item-create))
  "項目。" name count)

(defun core-make ()
  (let ((item (core-item-create :name "a" :count 0)))
    (core-item-p item)
    (core-item-name item)))

(defun core-hook-setup ()
  (add-hook 'after-save-hook #'core-greet)
  (mapcar #'core-open-paren-p '(?a ?b))
  (funcall 'util-helper "x")
  '(core-greet (not-a-call)))

(define-minor-mode core-mode
  "コアのマイナーモード。"
  :lighter " core"
  (core-greet "mode"))

(provide 'core)
