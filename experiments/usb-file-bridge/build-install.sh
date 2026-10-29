#!/bin/bash
set -euo pipefail
: "${DEVICE_UDID:?Set the USB device UDID}"
: "${TEAM_ID:?Set your Xcode development team}"
: "${BUNDLE_ID:?Set your own unique bundle identifier}"
cd "$(dirname "$0")"
xcodegen generate --spec ios/project.yml
xcodebuild -project ios/USBFileBridge.xcodeproj -scheme USBFileBridge \
  -configuration Debug -destination "id=$DEVICE_UDID" -derivedDataPath build \
  IPU_BRIDGE_TEAM="$TEAM_ID" IPU_BRIDGE_BUNDLE_ID="$BUNDLE_ID" \
  -allowProvisioningUpdates build
xcrun devicectl device install app --device "$DEVICE_UDID" \
  build/Build/Products/Debug-iphoneos/USBFileBridge.app
