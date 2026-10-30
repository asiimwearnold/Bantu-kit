const TRUTHY = new Set(["1", "true", "yes", "on"]);

export const DISABLE_VARIABLES = ["CRM_TELEMETRY_DISABLED", "DO_NOT_TRACK"];

export function telemetryDisabled(
	env: Record<string, string | undefined> = process.env,
): boolean {
	if (env.NODE_ENV === "test") return true;

	return DISABLE_VARIABLES.some((name) => isTruthy(env[name]));
}

function isTruthy(value: string | undefined): boolean {
	return TRUTHY.has((value ?? "").trim().toLowerCase());
}
