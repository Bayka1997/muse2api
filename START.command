#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Can cai Python 3.10+ truoc khi chay Muse2API."
  read -r -p "Nhan Enter de dong... "
  exit 1
fi
python3 launch.py
result=$?
if [ "$result" -ne 0 ]; then
  echo "Khoi dong that bai. Kiem tra Python 3.10+ va Google Chrome/Microsoft Edge."
  read -r -p "Nhan Enter de dong... "
fi
exit "$result"
