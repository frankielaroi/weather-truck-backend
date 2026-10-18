#!/usr/bin/env bash
# Build the image, push it to Lightsail and deploy it. Run from backend/ with the AWS CLI signed in.
#
# Needs: Docker running, the AWS CLI, and the Lightsail plugin (lightsailctl) on PATH.
# Secrets come from your shell, never from the repo:
#   ORS_API_KEY            OpenRouteService key
#   CORS_ALLOWED_ORIGINS   the frontend's https:// origin(s), comma-separated
# Optional: AWS_REGION (default eu-north-1), SERVICE (default weather-truck-api), DB (default weather-truck-db).
set -o errexit -o nounset -o pipefail

REGION="${AWS_REGION:-eu-north-1}"
SERVICE="${SERVICE:-weather-truck-api}"
DB="${DB:-weather-truck-db}"
: "${ORS_API_KEY:?set ORS_API_KEY}"
: "${CORS_ALLOWED_ORIGINS:?set CORS_ALLOWED_ORIGINS to the https origin of the frontend}"

lightsail() { aws lightsail --region "$REGION" --no-cli-pager "$@"; }

# --- Django secret key: kept from the running deployment, generated on the first deploy --------------
SECRET_KEY="$(lightsail get-container-services --service-name "$SERVICE"   --query 'containerServices[0].currentDeployment.containers.api.environment.DJANGO_SECRET_KEY' --output text 2>/dev/null || true)"
if [ -z "$SECRET_KEY" ] || [ "$SECRET_KEY" = "None" ]; then
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')"
fi

# --- Database connection details, read from the Lightsail database (the password is never printed) ----
read -r DB_HOST DB_PORT DB_USER DB_NAME < <(lightsail get-relational-database --relational-database-name "$DB" \
  --query 'relationalDatabase.[masterEndpoint.address,masterEndpoint.port,masterUsername,masterDatabaseName]' --output text)
DB_PASS="$(lightsail get-relational-database-master-user-password --relational-database-name "$DB" \
  --query masterUserPassword --output text)"
export DB_USER DB_PASS DB_HOST DB_PORT DB_NAME SECRET_KEY

# --- Build and push -------------------------------------------------------------------------------------
docker build -t "$SERVICE:latest" .
# The plugin prints progress and then: Refer to this image as ":service.label.N" in deployments.
PUSH_LOG="$(mktemp)"
lightsail push-container-image --service-name "$SERVICE" --label api --image "$SERVICE:latest" | tee "$PUSH_LOG"
IMAGE="$(grep -o ":${SERVICE}\.api\.[0-9]*" "$PUSH_LOG" | tail -n 1)"
rm -f "$PUSH_LOG"
[ -n "$IMAGE" ] || { echo "Could not read the pushed image name" >&2; exit 1; }
echo "Pushed $IMAGE"

# --- Deploy ------------------------------------------------------------------------------------------------
URL="$(lightsail get-container-services --service-name "$SERVICE" --query 'containerServices[0].url' --output text)"
HOST="${URL#https://}"; HOST="${HOST%/}"

CONTAINERS="$(python deploy/lightsail/containers.py "$IMAGE" "$HOST")"
ENDPOINT='{"containerName":"api","containerPort":8000,"healthCheck":{"path":"/api/health/","successCodes":"200","intervalSeconds":10,"timeoutSeconds":5,"healthyThreshold":2,"unhealthyThreshold":3}}'

lightsail create-container-service-deployment --service-name "$SERVICE" \
  --containers "$CONTAINERS" --public-endpoint "$ENDPOINT" --query 'containerService.state' --output text
echo "Deploying. Health check: ${URL}api/health/"
