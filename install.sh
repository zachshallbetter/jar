#!/usr/bin/env bash
# Jar Appliance Node - Standalone Installer
set -eo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JAR_HOME="${JAR_HOME:-$HOME/.jar}"
JAR_PORT="${JAR_PORT:-7770}"

echo "================================================================"
echo "           🏺  JAR HOUSEHOLD APPLIANCE NODE INSTALLER  🏺"
echo "================================================================"
echo "Installation Root: $JAR_HOME"
echo "Service Port:      $JAR_PORT"
echo ""

# 1. Platform & OS Check
OS="$(uname -s)"
ARCH="$(uname -m)"
echo "▶ Detecting platform: $OS ($ARCH)"

case "$OS" in
    Darwin)
        PLATFORM="macos"
        ;;
    Linux)
        PLATFORM="linux"
        ;;
    *)
        echo "❌ Unsupported operating system: $OS" >&2
        exit 1
        ;;
esac

# 2. Check Python 3.10+
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ Python 3 is required. Please install Python 3.10 or newer." >&2
    exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$(echo "$PY_VERSION" | cut -d. -f1)"
PY_MINOR="$(echo "$PY_VERSION" | cut -d. -f2)"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "❌ Python 3.10+ is required (found Python $PY_VERSION)." >&2
    exit 1
fi
echo "  ✓ Python $PY_VERSION detected"

# 3. Hardware Profiling
echo ""
echo "▶ Profiling system hardware..."
export PYTHONPATH="$PROJECT_ROOT/src:$PROJECT_ROOT/src/jar/core:$PYTHONPATH"
python3 -m jar.cli profile || true
echo ""

# 4. Prepare Directories
echo "▶ Preparing data and runtime directories in $JAR_HOME..."
mkdir -p "$JAR_HOME/bin" \
         "$JAR_HOME/config/box-setup" \
         "$JAR_HOME/data/world" \
         "$JAR_HOME/logs" \
         "$JAR_HOME/models" \
         "$JAR_HOME/tmp"

# 5. Virtual Environment
VENV_DIR="$JAR_HOME/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "▶ Setting up Python virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

echo "▶ Installing dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
"$VENV_DIR/bin/pip" install -q -r "$PROJECT_ROOT/requirements.txt"
"$VENV_DIR/bin/pip" install -q -e "$PROJECT_ROOT"
echo "  ✓ Dependencies installed"

# 6. Copy CLI and setup links
ln -sf "$PROJECT_ROOT/bin/jar" "$JAR_HOME/bin/jar"
if [ -d "$HOME/.local/bin" ]; then
    ln -sf "$PROJECT_ROOT/bin/jar" "$HOME/.local/bin/jar"
    echo "  ✓ Linked 'jar' command to $HOME/.local/bin/jar"
fi

# 7. Service Setup
if [ "$PLATFORM" = "macos" ]; then
    PLIST="$HOME/Library/LaunchAgents/com.jar.node.plist"
    cat <<EOF > "$PLIST"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jar.node</string>
    <key>ProgramArguments</key>
    <array>
        <string>$JAR_HOME/bin/jar</string>
        <string>run</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>JAR_HOME</key>
        <string>$JAR_HOME</string>
        <key>AGENT_API_PORT</key>
        <string>$JAR_PORT</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$JAR_HOME/logs/launchd.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$JAR_HOME/logs/launchd.stderr.log</string>
</dict>
</plist>
EOF
    echo "  ✓ Configured macOS LaunchAgent: $PLIST"
elif [ "$PLATFORM" = "linux" ] && command -v systemctl >/dev/null 2>&1; then
    USER_SYSTEMD="$HOME/.config/systemd/user"
    mkdir -p "$USER_SYSTEMD"
    SERVICE_FILE="$USER_SYSTEMD/jar.service"
    cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Jar Household Appliance Node
After=network.target

[Service]
Type=simple
Environment=JAR_HOME=$JAR_HOME
Environment=AGENT_API_PORT=$JAR_PORT
ExecStart=$JAR_HOME/bin/jar run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
    echo "  ✓ Configured systemd user service: $SERVICE_FILE"
    systemctl --user daemon-reload 2>/dev/null || true
fi

# 8. Print Complete Status
echo ""
echo "================================================================"
echo "          🎉  JAR NODE INSTALLATION COMPLETE!  🎉"
echo "================================================================"
echo ""
echo "To start the node interactively in the foreground:"
echo "  jar run"
echo ""
echo "To start as a background daemon:"
echo "  jar start"
echo ""
"$JAR_HOME/bin/jar" url || true
echo "================================================================"

