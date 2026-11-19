#!/bin/bash
set -e

# Ensure state, artifact, and log directories exist
mkdir -p /opt/trillionaire/logs \
         /opt/trillionaire/research_lab/artifacts \
         /opt/trillionaire/research_lab/data_store \
         /opt/trillionaire/sentinel/artifacts

# Fix permissions on host-mounted volumes so unprivileged user has full read/write
chown -R trillionaire:trillionaire /opt/trillionaire/logs \
                                   /opt/trillionaire/research_lab/artifacts \
                                   /opt/trillionaire/research_lab/data_store \
                                   /opt/trillionaire/sentinel/artifacts 2>/dev/null || true

chmod -R 775 /opt/trillionaire/logs \
             /opt/trillionaire/research_lab/artifacts \
             /opt/trillionaire/research_lab/data_store \
             /opt/trillionaire/sentinel/artifacts 2>/dev/null || true

# Drop privileges and execute CMD as user trillionaire
if command -v gosu >/dev/null 2>&1; then
    exec gosu trillionaire "$@"
else
    exec su -s /bin/bash trillionaire -c 'exec "$@"' -- "$@"
fi
