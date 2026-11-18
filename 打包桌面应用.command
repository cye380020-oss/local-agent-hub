#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
/usr/bin/python3 -B macos-app/build.py
echo '桌面应用已构建完成：macos-app/本地Agent工作台.app'
echo '按回车关闭窗口。'
read -r answer
