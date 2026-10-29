#!/usr/bin/env bash
# stage-bios-usb.sh — copy the X99-DELUXE BIOS recovery kit onto a USB mount.
#
# Usage:
#   bash scripts/stage-bios-usb.sh /Volumes/BIOSSTICK
#   bash scripts/stage-bios-usb.sh /Volumes/BIOSSTICK --with-firmware
#
# Stick should be FAT32. --with-firmware copies X99-DELUXE-ASUS-4101.CAP from
# iCloud Tools (for USB BIOS Flashback only — does not apply settings).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KIT="$ROOT/server-setup/bios-x99-deluxe"
TOOLS_CAP_DIR="${TITAN_ASUS_TOOLS:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/Projects/Tools/ASUS Motherboard}"

dest="${1:-}"
with_fw=0
shift || true
for a in "${@:-}"; do
  case "$a" in
    --with-firmware) with_fw=1 ;;
    "") ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

if [[ -z "$dest" || ! -d "$dest" ]]; then
  echo "usage: $0 /Volumes/YOUR_FAT32_STICK [--with-firmware]" >&2
  exit 2
fi
if [[ ! -d "$KIT" ]]; then
  echo "missing kit: $KIT" >&2
  exit 1
fi

mkdir -p "$dest/titan-bios/profiles" "$dest/titan-bios/firmware"
# checklist + readme at stick root and under titan-bios/
cp -f "$KIT/README.md" "$dest/titan-bios/README.md"
cp -f "$KIT/SETTINGS-CHECKLIST.txt" "$dest/titan-bios/SETTINGS-CHECKLIST.txt"
cp -f "$KIT/SETTINGS-CHECKLIST.txt" "$dest/SETTINGS-CHECKLIST.txt"
if [[ -d "$KIT/profiles" ]]; then
  find "$KIT/profiles" -type f ! -name '.gitkeep' -exec cp -f {} "$dest/titan-bios/profiles/" \;
fi

if [[ "$with_fw" -eq 1 ]]; then
  cap=""
  for c in \
    "$TOOLS_CAP_DIR/X99-DELUXE-ASUS-4101.CAP" \
    "$TOOLS_CAP_DIR/X99-DELUXE-ASUS-4101-1.CAP" \
    "$KIT/firmware/X99-DELUXE-ASUS-4101.CAP"
  do
    if [[ -f "$c" ]]; then cap="$c"; break; fi
  done
  if [[ -z "$cap" ]]; then
    echo "warning: no X99-DELUXE .CAP found under $TOOLS_CAP_DIR" >&2
  else
    # Flashback wants the CAP at the stick root with the ASUS name.
    cp -f "$cap" "$dest/$(basename "$cap")"
    cp -f "$cap" "$dest/titan-bios/firmware/$(basename "$cap")"
    echo "firmware: $(basename "$cap")"
  fi
fi

# tiny marker so you know the stick is staged
date -u +"%Y-%m-%dT%H:%M:%SZ staged by stage-bios-usb.sh" > "$dest/titan-bios/STAGED.txt"

echo "staged → $dest/titan-bios/"
echo "open SETTINGS-CHECKLIST.txt on the stick (also at stick root)."
echo "after a good BIOS config: save O.C. Profile to USB, then copy it into profiles/."
