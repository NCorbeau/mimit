// Correct only the observed Railway late-success/auto-inactive sequence.
const ENVIRONMENT = "mimit / production";
const RAILWAY_BOT = "railway-app[bot]";
const LOG_URL = "https://railway.com/project/e6cd1cc9-9bae-4a13-9e13-3b471e9f5504?environmentId=da04f1a7-cde1-453d-b65d-b91efd05fdfd";
const READY_URL = "https://mimit-production.up.railway.app/readyz";

function isRailway(record) {
  return record.creator?.login === RAILWAY_BOT;
}

function raceSuccess(statuses, olderStatuses) {
  const [inactive, success] = statuses;
  if (!inactive || !success || inactive.state !== "inactive" || success.state !== "success") return null;
  if (!isRailway(inactive) || !isRailway(success)) return null;
  // GitHub's automatic inactive status drops environment_url. Explicit Railway
  // removal statuses retain it. Never repair an explicit removal/failure.
  if (inactive.environment_url !== "" || inactive.description !== "") return null;
  if (success.environment_url !== LOG_URL || inactive.log_url !== LOG_URL) return null;
  if (inactive.environment !== ENVIRONMENT || success.environment !== ENVIRONMENT) return null;
  const inactiveAt = Date.parse(inactive.created_at);
  const successAt = Date.parse(success.created_at);
  if (!Number.isFinite(inactiveAt) || !Number.isFinite(successAt) || inactiveAt <= successAt) return null;
  const lateSuccess = olderStatuses.some(status => {
    const at = Date.parse(status.created_at);
    return isRailway(status) && status.state === "success" && status.environment === ENVIRONMENT
      && status.environment_url === LOG_URL && at > successAt
      && inactiveAt >= at && inactiveAt - at <= 5000;
  });
  return lateSuccess ? success : null;
}

async function repair({ github, context, core, fetchImpl = fetch }) {
  const repo = context.repo;
  const deployments = async () => {
    const { data } = await github.rest.repos.listDeployments({ ...repo, environment: ENVIRONMENT, per_page: 20 });
    return data.sort((a, b) => b.id - a.id);
  };
  const statuses = async id => {
    const { data } = await github.rest.repos.listDeploymentStatuses({ ...repo, deployment_id: id, per_page: 10 });
    return data.sort((a, b) => b.id - a.id);
  };
  const mainSha = async () => (await github.rest.repos.getBranch({ ...repo, branch: "main" })).data.commit.sha;
  const recent = await deployments();
  const latest = recent[0];
  if (!latest || !isRailway(latest) || latest.sha !== await mainSha()) return false;
  const current = await statuses(latest.id);
  if (current[0]?.state !== "inactive") return false;
  const older = [];
  for (const deployment of recent.slice(1)) {
    if (isRailway(deployment)) older.push(...await statuses(deployment.id));
  }
  const success = raceSuccess(current, older);
  if (!success) return false;

  const response = await fetchImpl(READY_URL, { signal: AbortSignal.timeout(10000), redirect: "error" });
  if (!response.ok || (await response.json()).status !== "ok") {
    throw new Error("API readiness failed; deployment metadata was not changed.");
  }
  // Re-read after all external checks; a newer deployment or changed status
  // invalidates the repair. GitHub does not expose a compare-and-set status API.
  if ((await deployments())[0]?.id !== latest.id || await mainSha() !== latest.sha
      || (await statuses(latest.id))[0]?.id !== current[0].id) return false;
  await github.rest.repos.createDeploymentStatus({
    ...repo,
    deployment_id: latest.id,
    state: "success",
    environment: ENVIRONMENT,
    environment_url: success.environment_url,
    log_url: success.log_url,
    description: "Restored successful release after Railway late-status race (MAC-154).",
    auto_inactive: false,
  });
  core.info(`Corrected GitHub deployment ${latest.id}; no Railway release was changed.`);
  return true;
}

module.exports = { repair, raceSuccess, ENVIRONMENT, LOG_URL };
