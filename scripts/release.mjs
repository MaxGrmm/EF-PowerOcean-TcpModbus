/** Prepares versioned release changes and GitHub release notes. */
import { execFileSync } from "node:child_process";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";

const CHANGELOG_PATH = "CHANGELOG.md";
const MANIFEST_PATH = "custom_components/ef_powerocean_tcpmodbus/manifest.json";

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function assertVersion(version) {
  if (
    !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(version)
  ) {
    throw new Error(
      "Version must be valid semantic versioning without a v prefix.",
    );
  }
}

function changelogSection(changelog, version) {
  const heading = new RegExp(
    `^## \\[${escapeRegExp(version)}\\] - .+\\n`,
    "m",
  ).exec(changelog);
  if (!heading) {
    throw new Error(`CHANGELOG.md has no section for ${version}.`);
  }

  const sectionStart = heading.index + heading[0].length;
  const sectionEnd = changelog.indexOf("\n## ", sectionStart);
  return changelog.slice(
    sectionStart,
    sectionEnd === -1 ? undefined : sectionEnd,
  );
}

function prepareRelease(version) {
  assertVersion(version);

  const manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf8"));
  if (manifest.version === version) {
    throw new Error(`manifest.json already declares version ${version}.`);
  }

  const changelog = readFileSync(CHANGELOG_PATH, "utf8");
  const unreleasedHeading = "## Unreleased\n";
  const unreleasedStart = changelog.indexOf(unreleasedHeading);
  if (unreleasedStart === -1) {
    throw new Error("CHANGELOG.md must contain an Unreleased section.");
  }

  const unreleasedEnd = changelog.indexOf(
    "\n## ",
    unreleasedStart + unreleasedHeading.length,
  );
  const unreleased = changelog.slice(
    unreleasedStart + unreleasedHeading.length,
    unreleasedEnd === -1 ? undefined : unreleasedEnd,
  );
  if (!unreleased.trim()) {
    throw new Error(
      "CHANGELOG.md must contain a non-empty Unreleased section.",
    );
  }
  if (new RegExp(`^## \\[${escapeRegExp(version)}\\]`, "m").test(changelog)) {
    throw new Error(`CHANGELOG.md already contains version ${version}.`);
  }

  const date = new Date().toISOString().slice(0, 10);
  writeFileSync(
    CHANGELOG_PATH,
    changelog.replace(
      /^## Unreleased\n/m,
      `## Unreleased\n\n## [${version}] - ${date}\n`,
    ),
  );
  manifest.version = version;
  writeFileSync(MANIFEST_PATH, `${JSON.stringify(manifest, null, 2)}\n`);
}

function setOutput(name, value) {
  const outputPath = process.env.GITHUB_OUTPUT;
  if (!outputPath) {
    throw new Error("GITHUB_OUTPUT must be set when publishing a release.");
  }
  appendFileSync(outputPath, `${name}=${value}\n`);
}

function publishRelease() {
  const manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf8"));
  const version = manifest.version;
  assertVersion(version);

  const tag = `v${version}`;
  try {
    execFileSync("git", [
      "show-ref",
      "--verify",
      "--quiet",
      `refs/tags/${tag}`,
    ]);
    console.log(`${tag} already exists; skipping release.`);
    return;
  } catch (error) {
    if (error.status !== 1) {
      throw error;
    }
  }

  const changelog = readFileSync(CHANGELOG_PATH, "utf8");
  writeFileSync(
    "RELEASE_NOTES.md",
    `${changelogSection(changelog, version).trimEnd()}\n`,
  );
  setOutput("tag", tag);
  setOutput("prerelease", version.includes("-"));
}

function publishPrerelease(version) {
  assertVersion(version);
  if (!version.includes("-")) {
    throw new Error(
      `${version} is not a pre-release version. Use a suffix such as ${version}-beta.1; real releases go through Prepare Release.`,
    );
  }

  const tag = `v${version}`;
  try {
    execFileSync("git", [
      "show-ref",
      "--verify",
      "--quiet",
      `refs/tags/${tag}`,
    ]);
    throw new Error(`${tag} already exists.`);
  } catch (error) {
    if (error.status !== 1) {
      throw error;
    }
  }

  // Only the packaged copy carries the pre-release version; nothing is committed.
  const manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf8"));
  manifest.version = version;
  writeFileSync(MANIFEST_PATH, `${JSON.stringify(manifest, null, 2)}\n`);

  const changelog = readFileSync(CHANGELOG_PATH, "utf8");
  const start = changelog.indexOf("## Unreleased\n");
  let notes = "";
  if (start !== -1) {
    const from = start + "## Unreleased\n".length;
    const end = changelog.indexOf("\n## ", from);
    notes = changelog.slice(from, end === -1 ? undefined : end).trim();
  }
  writeFileSync("RELEASE_NOTES.md", `${notes || "Pre-release build."}\n`);
  setOutput("tag", tag);
}

const [command, version] = process.argv.slice(2);
if (command === "prepare") {
  prepareRelease(version);
} else if (command === "publish") {
  publishRelease();
} else if (command === "prerelease") {
  publishPrerelease(version);
} else {
  throw new Error(
    "Usage: node scripts/release.mjs <prepare VERSION|publish|prerelease VERSION>",
  );
}
