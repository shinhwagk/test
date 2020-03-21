#!/usr/bin/env bash
commitmsg=$2
cd $1
git add -A
git status -s
[ `git status -s | wc -l` != 0 ] && \
git commit -q -m "$2"
for i in $(seq 1 5); do
  git push && exit 0 || git pull -q --rebase
  sleep 5
  echo "retry ${i}."
done
