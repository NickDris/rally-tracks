// Runs on Node 18/20 (ubuntu-latest). No extra deps required.
// Uses GitHub REST API via fetch + GITHUB_TOKEN.

const TOKEN = process.env.GITHUB_TOKEN;
if (!TOKEN) {
  console.error("Missing GITHUB_TOKEN");
  process.exit(1);
}

const LABEL_NAME = process.env.LABEL_NAME || "backport-pending";
const TARGET_BRANCH = process.env.TARGET_BRANCH || "master";
const AFTER_DAYS = parseInt(process.env.REMIND_AFTER_DAYS || "7", 10);
const EVERY_DAYS = parseInt(process.env.REMIND_EVERY_DAYS || "7", 10);
const MARKER = process.env.MARKER || "[backport-pending-reminder]";

const { GITHUB_REPOSITORY } = process.env; // "owner/repo"
const [OWNER, REPO] = (GITHUB_REPOSITORY || "").split("/");

if (!OWNER || !REPO) {
  console.error("Cannot parse OWNER/REPO from GITHUB_REPOSITORY");
  process.exit(1);
}

const api = (path, opts = {}) => {
  const url = `https://api.github.com${path}`;
  const headers = {
    "Authorization": `Bearer ${TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "backport-pending-reminder-script",
  };
  return fetch(url, { ...opts, headers });
};

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const daysBetween = (a, b) => Math.floor((a - b) / (1000 * 60 * 5)); // 5 minutes as "1 day" for testing

async function paginate(path, params = {}) {
  // params -> { per_page, page } appended to the query string
  const qp = new URLSearchParams({ per_page: "100", ...Object.fromEntries(Object.entries(params).map(([k,v]) => [k, String(v)])) });
  let page = 1, results = [];
  while (true) {
    qp.set("page", String(page));
    const res = await api(`${path}?${qp.toString()}`);
    if (res.status === 403 && res.headers.get("x-ratelimit-remaining") === "0") {
      const reset = Number(res.headers.get("x-ratelimit-reset") || "0") * 1000;
      const waitMs = Math.max(0, reset - Date.now()) + 1000;
      console.log(`Rate-limited. Sleeping ${waitMs}ms...`);
      await sleep(waitMs);
      continue;
    }
    if (!res.ok) {
      const t = await res.text();
      throw new Error(`GET ${path} failed: ${res.status} ${t}`);
    }
    const data = await res.json();
    if (!Array.isArray(data) || data.length === 0) break;
    results = results.concat(data);
    if (data.length < 100) break;
    page += 1;
  }
  return results;
}

async function listOpenIssuesWithLabel(label) {
  // Issues API returns PRs too (those have .pull_request)
  return paginate(`/repos/${OWNER}/${REPO}/issues`, { state: "open", labels: label });
}

async function getPull(pull_number) {
  const res = await api(`/repos/${OWNER}/${REPO}/pulls/${pull_number}`);
  if (!res.ok) throw new Error(`GET pull ${pull_number} failed: ${res.status} ${await res.text()}`);
  return res.json();
}

async function listIssueEvents(number) {
  return paginate(`/repos/${OWNER}/${REPO}/issues/${number}/events`);
}

async function listComments(number) {
  return paginate(`/repos/${OWNER}/${REPO}/issues/${number}/comments`);
}

async function createComment(number, body) {
  const res = await api(`/repos/${OWNER}/${REPO}/issues/${number}/comments`, {
    method: "POST",
    body: JSON.stringify({ body }),
  });
  if (!res.ok) throw new Error(`POST comment on #${number} failed: ${res.status} ${await res.text()}`);
  return res.json();
}

async function run() {
  const now = new Date();
  console.log(`Repo: ${OWNER}/${REPO}`);
  console.log(`Label: ${LABEL_NAME} | Target branch: ${TARGET_BRANCH}`);
  console.log(`Threshold: ${AFTER_DAYS}d | Re-reminder: ${EVERY_DAYS}d`);
  const issues = await listOpenIssuesWithLabel(LABEL_NAME);

  for (const issue of issues) {
    if (!issue.pull_request) continue; // Only PRs
    const number = issue.number;

    // Check PR base branch
    const pr = await getPull(number);
    const baseRef = pr.base?.ref;
    if (baseRef !== TARGET_BRANCH) {
      console.log(`#${number}: base is ${baseRef}, skipping (want ${TARGET_BRANCH}).`);
      continue;
    }

    // Find last time the label was (re)applied
    const events = await listIssueEvents(number);
    const labeledEvents = events
      .filter(e => e.event === "labeled" && e.label?.name === LABEL_NAME)
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

    if (labeledEvents.length === 0) {
      console.log(`#${number}: no labeled event found (label may be pre-existing), skipping.`);
      continue;
    }

    const labeledAt = new Date(labeledEvents[0].created_at);
    const ageDays = daysBetween(now, labeledAt);
    if (ageDays < AFTER_DAYS) {
      console.log(`#${number}: label age ${ageDays}d < ${AFTER_DAYS}d, skipping.`);
      continue;
    }

    // Avoid reposting within the interval
    const comments = await listComments(number);
    const ourRecent = comments
      .filter(c =>
        (c.user?.type === "Bot" || c.user?.login?.includes("github-actions")) &&
        c.body?.includes(MARKER)
      )
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

    if (ourRecent.length) {
      const lastAt = new Date(ourRecent[0].created_at);
      const since = daysBetween(now, lastAt);
      if (since < EVERY_DAYS) {
        console.log(`#${number}: reminded ${since}d ago (< ${EVERY_DAYS}d), skipping.`);
        continue;
      }
    }

    // Mentions: author + currently requested reviewers (users & teams)
    const author = issue.user?.login ? `@${issue.user.login}` : "";
    const requestedUsers = pr.requested_reviewers?.map(u => `@${u.login}`) || [];
    const requestedTeams = pr.requested_teams?.map(t => `@${pr.base.repo.owner.login}/${t.slug}`) || [];
    const mentions = [author, ...requestedUsers, ...requestedTeams].filter(Boolean).join(" ");

    const body = `${MARKER}
${mentions}

This pull request targets \`${TARGET_BRANCH}\` and has the \`${LABEL_NAME}\` label for **${ageDays} days**.
Please review next steps for backporting (or remove the label if no longer needed).

- Threshold: \`${AFTER_DAYS}d\`
- Re-reminder interval: \`${EVERY_DAYS}d\`
`;

    await createComment(number, body);
    console.log(`#${number}: posted reminder (age ${ageDays}d).`);
    // Tiny delay to be nice to the API
    await sleep(200);
  }

  console.log("Done.");
}

run().catch(err => {
  console.error(err);
  process.exit(1);
});
