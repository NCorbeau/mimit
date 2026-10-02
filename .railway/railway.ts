import { defineRailway, github, postgres, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  // Preserve the existing project's region and database volume.
  const db = postgres("Postgres", { region: "sfo" });
  db.networking = { privateNetworkEndpoint: "postgres" };
  const data = volume("postgres-volume", {
    alerts: { usage: { "80": {}, "95": {}, "100": {} } },
    allowOnlineResize: true,
    region: "sfo",
    sizeMB: 5000,
  });

  const api = service("mimit", {
    source: github("NCorbeau/mimit", { branch: "dev/mac-59-railway-deployment", checkSuites: true }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    start: "sh -c 'exec uvicorn mimit.api:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}'",
    preDeploy: "alembic upgrade head",
    healthcheck: "/readyz",
    healthcheckTimeout: 120,
    replicas: { sfo: 1 },
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 10 },
    env: { DATABASE_URL: db.env.DATABASE_URL, HOUSEHOLD_TIMEZONE: "Europe/Warsaw", PORT: "8000" },
  });
  const worker = service("worker", {
    // Connect the repository after the complete Telegram configuration is set.
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    start: "python -m mimit.telegram.sender",
    replicas: { sfo: 1 },
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 10 },
    env: { DATABASE_URL: db.env.DATABASE_URL, HOUSEHOLD_TIMEZONE: "Europe/Warsaw" },
  });

  return project("mimit", { resources: [api, worker, db, data] });
});
