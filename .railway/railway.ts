import { defineRailway, github, postgres, preserve, project, service, volume } from "railway/iac";

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
    source: github("NCorbeau/mimit", { branch: "main", checkSuites: true }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    start: "sh -c 'exec uvicorn mimit.api:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}'",
    preDeploy: "alembic upgrade head",
    healthcheck: "/readyz",
    healthcheckTimeout: 120,
    replicas: { sfo: 1 },
    deploy: { drainingSeconds: 30 },
    env: {
      DATABASE_URL: db.env.DATABASE_URL,
      HOUSEHOLD_TIMEZONE: "Europe/Warsaw",
      PORT: "8000",
      RECOMMENDATION_HISTORY_DAYS: "30",
      RECOMMENDATION_DISCOUNT_FRACTION: "0.10",
      RECOMMENDATION_MIN_PRIOR_OBSERVATIONS: "3",
      RECOMMENDATION_PRICE_MAX_AGE_HOURS: "48",
      TELEGRAM_BOT_TOKEN: preserve(),
      TELEGRAM_WEBHOOK_SECRET: preserve(),
      PUBLIC_BASE_URL: "https://mimit-production.up.railway.app",
      TELEGRAM_ALLOWED_USER_ID: preserve(),
      TELEGRAM_ALLOWED_CHAT_ID: preserve(),
    },
  });
  const worker = service("worker", {
    source: github("NCorbeau/mimit", { branch: "main", checkSuites: true }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    start: "python -m mimit.worker",
    replicas: { sfo: 1 },
    deploy: { drainingSeconds: 30, overlapSeconds: 0 },
    env: {
      DATABASE_URL: db.env.DATABASE_URL,
      HOUSEHOLD_TIMEZONE: "Europe/Warsaw",
      WORKER_POLL_SECONDS: "1",
      WORKER_SHUTDOWN_GRACE_SECONDS: "30",
      WORKER_PRICE_CONCURRENCY: "2",
      RECOMMENDATION_HISTORY_DAYS: api.env.RECOMMENDATION_HISTORY_DAYS,
      RECOMMENDATION_DISCOUNT_FRACTION: api.env.RECOMMENDATION_DISCOUNT_FRACTION,
      RECOMMENDATION_MIN_PRIOR_OBSERVATIONS: api.env.RECOMMENDATION_MIN_PRIOR_OBSERVATIONS,
      RECOMMENDATION_PRICE_MAX_AGE_HOURS: api.env.RECOMMENDATION_PRICE_MAX_AGE_HOURS,
      TELEGRAM_BOT_TOKEN: api.env.TELEGRAM_BOT_TOKEN,
      TELEGRAM_WEBHOOK_SECRET: api.env.TELEGRAM_WEBHOOK_SECRET,
      PUBLIC_BASE_URL: api.env.PUBLIC_BASE_URL,
      TELEGRAM_ALLOWED_USER_ID: api.env.TELEGRAM_ALLOWED_USER_ID,
      TELEGRAM_ALLOWED_CHAT_ID: api.env.TELEGRAM_ALLOWED_CHAT_ID,
    },
  });

  return project("mimit", { resources: [api, worker, db, data] });
});
