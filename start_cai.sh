#!/usr/bin/env bash
cd /home/l4k3q/cai
# Safety timeout: if no event is emitted for 5 minutes, cancel the run.
export CAI_API_IDLE_TIMEOUT=300
# Emit waiting heartbeat every 2 seconds so the user sees progress.
export CAI_API_HEARTBEAT_SECONDS=2
exec uv run cai --api --api-port 8000 >> /tmp/cai_api.log 2>&1
