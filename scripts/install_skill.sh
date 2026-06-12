#!/usr/bin/env bash
# Installa la skill wiki-import su Hermes (WSL): symlink in ~/.hermes/skills.
set -e
SRC="/mnt/c/Projects/wikibridge/skill"
DST="$HOME/.hermes/skills/wiki-import"
mkdir -p "$HOME/.hermes/skills"
if [ -L "$DST" ] || [ -e "$DST" ]; then
  echo "già presente: $DST"
else
  ln -s "$SRC" "$DST"
  echo "creato symlink: $DST -> $SRC"
fi
