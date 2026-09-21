#!/bin/sh
# Stand-in for the depot's 305-byte launcher: the real one chmods the shipping
# binary and then runs it with "ConanSandbox" as the first argument, without
# exec, so /bin/sh stays PID 1 and returns the game's exit code.
cd "$(dirname "$0")" || exit 1
chmod +x ConanSandbox/Binaries/Linux/ConanSandboxServer-Linux-Shipping
ConanSandbox/Binaries/Linux/ConanSandboxServer-Linux-Shipping ConanSandbox "$@"
