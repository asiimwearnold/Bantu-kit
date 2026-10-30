import { readFileSync } from "node:fs";
import { join } from "node:path";
import { findWorkspaceRoot } from "@crm/env";

export function crmVersion(from = process.cwd()): string | null {
	const root = findWorkspaceRoot(from);
	if (!root) return null;

	try {
		const parsed: unknown = JSON.parse(
			readFileSync(join(root, "package.json"), "utf8"),
		);

		if (typeof parsed !== "object" || parsed === null) return null;
		if (!("version" in parsed)) return null;

		const version = parsed.version;
		return typeof version === "string" && version.trim()
			? version.trim()
			: null;
	} catch {
		return null;
	}
}

export function commitSha(): string | null {
	const candidates = [
		process.env.VERCEL_GIT_COMMIT_SHA,
		process.env.GIT_COMMIT_SHA,
		process.env.GITHUB_SHA,
	];

	for (const candidate of candidates) {
		const sha = candidate?.trim();
		if (sha && /^[0-9a-f]{7,40}$/i.test(sha)) return sha;
	}

	return null;
}
