#!/bin/bash
set -u

TTY=/dev/tty1

# FeedNode boot splash. Keep this deliberately simple so it can appear very
# early in boot without needing X11, a compositor, pygame, or network access.
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

  # Linux virtual consoles are normally 80x25 before the DRM renderer takes
  # over. Position the splash around the center without depending on tput.
  printf '\033[2J\033[H'
  printf '\033[9;1H\033[2K'
  printf '\033[10;1H\033[2K\033[1;36m%*s\033[0m' 43 'FEEDNODE'
  printf '\033[11;1H\033[2K\033[90m%*s\033[0m' 47 'UNIFIED CHAT FEED'
  printf '\033[14;1H\033[2K\033[37m%*s\033[0m' 44 "LOADING $frame"
  sleep 0.5
done
