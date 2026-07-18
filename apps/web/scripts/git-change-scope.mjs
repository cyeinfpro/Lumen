import { execFileSync } from "node:child_process";
import path from "node:path";

function normalizePath(value) {
  return value.replaceAll("\\", "/");
}

function commandError(args, error) {
  const detail =
    typeof error?.stderr === "string"
      ? error.stderr.trim()
      : Buffer.isBuffer(error?.stderr)
        ? error.stderr.toString("utf8").trim()
        : "";
  return new Error(
    `git ${args.join(" ")} failed${detail ? `: ${detail}` : ""}`,
    { cause: error },
  );
}

export function gitOutput(repoRoot, args, { allowFailure = false } = {}) {
  try {
    return execFileSync("git", args, {
      cwd: repoRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    if (allowFailure) return null;
    throw commandError(args, error);
  }
}

function outputLines(value) {
  return value
    .split(/\r?\n/)
    .filter(Boolean)
    .map(normalizePath);
}

function verifiedCommit(repoRoot, candidate) {
  if (!candidate || /^0+$/.test(candidate)) return null;
  return gitOutput(
    repoRoot,
    ["rev-parse", "--verify", `${candidate}^{commit}`],
    { allowFailure: true },
  )?.trim() ?? null;
}

function mergeBase(repoRoot, candidate) {
  const commit = verifiedCommit(repoRoot, candidate);
  if (!commit) return null;
  return gitOutput(repoRoot, ["merge-base", "HEAD", commit], {
    allowFailure: true,
  })?.trim() ?? null;
}

export function resolveComparisonBase(repoRoot, env = process.env) {
  const explicit = env.WEB_GATE_BASE_REF?.trim();
  if (explicit && !/^0+$/.test(explicit)) {
    const commit = verifiedCommit(repoRoot, explicit);
    if (!commit) {
      throw new Error(
        `WEB_GATE_BASE_REF does not resolve to a commit: ${explicit}`,
      );
    }
    return commit;
  }

  const githubBase = env.GITHUB_BASE_REF?.trim();
  if (githubBase) {
    for (const candidate of [`origin/${githubBase}`, githubBase]) {
      const base = mergeBase(repoRoot, candidate);
      if (base) return base;
    }
  }

  for (const candidate of [
    env.GITHUB_EVENT_BEFORE?.trim(),
    env.GITHUB_BEFORE?.trim(),
  ]) {
    const commit = verifiedCommit(repoRoot, candidate);
    if (commit) return commit;
  }

  const parent = verifiedCommit(repoRoot, "HEAD^");
  if (parent) return parent;

  throw new Error(
    "Unable to determine a Git comparison base. Fetch repository history or set WEB_GATE_BASE_REF; refusing to skip touched-debt checks.",
  );
}

function workingTreeFiles(repoRoot) {
  const tracked = outputLines(
    gitOutput(repoRoot, [
      "diff",
      "--name-only",
      "--diff-filter=ACMR",
      "HEAD",
      "--",
    ]),
  );
  const untracked = outputLines(
    gitOutput(repoRoot, ["ls-files", "--others", "--exclude-standard"]),
  );
  return new Set([...tracked, ...untracked]);
}

export function getGitChangeScope({
  startDir,
  env = process.env,
} = {}) {
  if (!startDir) throw new Error("startDir is required");
  const repoRoot = gitOutput(startDir, ["rev-parse", "--show-toplevel"]).trim();
  const working = workingTreeFiles(repoRoot);
  if (working.size > 0) {
    return {
      repoRoot,
      baseRef: "HEAD",
      files: working,
      source: "working-tree",
    };
  }

  const baseRef = resolveComparisonBase(repoRoot, env);
  const files = new Set(
    outputLines(
      gitOutput(repoRoot, [
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        baseRef,
        "HEAD",
        "--",
      ]),
    ),
  );
  return { repoRoot, baseRef, files, source: "commit-range" };
}

export function repoRelativePath(repoRoot, absolutePath) {
  return normalizePath(path.relative(repoRoot, absolutePath));
}

export function readFileAtRef(repoRoot, ref, repoPath) {
  return gitOutput(repoRoot, ["show", `${ref}:${normalizePath(repoPath)}`]);
}

export function changedLinesForPath(repoRoot, baseRef, repoPath) {
  const diff = gitOutput(repoRoot, [
    "diff",
    "--unified=0",
    "--no-color",
    baseRef,
    "--",
    normalizePath(repoPath),
  ]);
  const lines = new Set();
  for (const rawLine of diff.split(/\r?\n/)) {
    const match = rawLine.match(
      /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/,
    );
    if (!match) continue;
    const start = Number.parseInt(match[1], 10);
    const count =
      match[2] === undefined ? 1 : Number.parseInt(match[2], 10);
    if (count === 0) {
      lines.add(Math.max(1, start));
      lines.add(Math.max(1, start + 1));
      continue;
    }
    for (let line = start; line < start + count; line += 1) {
      lines.add(line);
    }
  }
  return lines;
}
