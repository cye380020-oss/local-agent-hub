#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
/usr/bin/python3 -B -m unittest discover -s tests -v
echo '自检已完成。按回车关闭窗口。'
read -r answer
