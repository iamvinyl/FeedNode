#!/bin/bash
set -u

TTY=/dev/tty1
MODE_FILE=/run/feednode-splash-mode

# FeedNode splash. Keep this deliberately simple so it can appear very early
# in boot and during firmware updates without X11, pygame, or network access.
exec >"$TTY" 2>&1

printf '\033[?25l\033[2J\033[H'

cleanup() {
  printf '\033[?25l\033[2J\033[H'
}
trap cleanup EXIT TERM INT

frames=("·  " "·· " "···")
i=0

while true; do
  frame="${frames[$i]}"
  i=$(( (i + 1) % ${#frames[@]} ))

  mode="LOADING"
  if [ -r "$MODE_FILE" ]; then
    requested="$(tr -dc '[:alnum:] _-' < "$MODE_FILE" | head -c 24)"
    [ -n "$requested" ] && mode="$requested"
  fi

  # Linux virtual consoles are normally 80x25 before the DRM renderer takes
  # over. Position the splash around the center without depending on tput.
  printf '\033[2J\033[H'
  printf '\033[9;1H\033[2K'
  printf '\033[10;1H\033[2K\033[1;36m%*s\033[0m' 43 'FEEDNODE'
  printf '\033[11;1H\033[2K\033[90m%*s\033[0m' 47 'UNIFIED CHAT FEED'
  printf '\033[14;1H\033[2K\033[37m%*s\033[0m' 44 "$mode $frame"
  sleep 0.5
done
