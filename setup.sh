#!/bin/bash
# First-time setup: creates secrets/ and data/ with placeholder values.
# Edit secrets/ files with real credentials, then run start.sh.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SECRETS="$SCRIPT_DIR/secrets"
DATA="$SCRIPT_DIR/data"

mkdir -p "$SECRETS" "$DATA"

# ── config ───────────────────────────────────────────────────────────────────
CREDS="$SECRETS/config"
if [ -f "$CREDS" ]; then
    echo "  skipped  $CREDS (already exists)"
else
    cat > "$CREDS" <<'EOF'
STASH_URL="https://your-bitbucket-server"
JIRA_URL="https://your-jira-server"
SONAR_URL="https://your-sonarqube-server"
JENKINS_URL="https://your-jenkins-server"
JIRA_PROJECTS="PROJ1,PROJ2"
EOF
    echo "  created  $CREDS"
fi

# ── bitbucket-token ───────────────────────────────────────────────────────────
BB_TOKEN="$SECRETS/bitbucket-token"
if [ -f "$BB_TOKEN" ]; then
    echo "  skipped  $BB_TOKEN (already exists)"
else
    echo "your-bitbucket-pat-here" > "$BB_TOKEN"
    echo "  created  $BB_TOKEN"
fi

# ── jira-token ────────────────────────────────────────────────────────────────
JIRA_TOKEN="$SECRETS/jira-token"
if [ -f "$JIRA_TOKEN" ]; then
    echo "  skipped  $JIRA_TOKEN (already exists)"
else
    echo "your-jira-pat-here" > "$JIRA_TOKEN"
    echo "  created  $JIRA_TOKEN"
fi

# ── sonar-token ───────────────────────────────────────────────────────────────
SONAR_TOKEN="$SECRETS/sonar-token"
if [ -f "$SONAR_TOKEN" ]; then
    echo "  skipped  $SONAR_TOKEN (already exists)"
else
    echo "your-sonarqube-user-token-here" > "$SONAR_TOKEN"
    echo "  created  $SONAR_TOKEN"
fi

echo ""
echo "Fill in secrets/ with real values, then run ./start.sh"
