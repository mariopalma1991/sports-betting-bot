#!/usr/bin/env bash
# Corre el programa y, si se cierra por un error, lo vuelve a arrancar a los 30 s.
while true; do
  "$@" || true
  echo
  echo "$(date '+%d/%m %H:%M:%S') El programa se cerró; se reinicia en 30 s."
  sleep 30
done
