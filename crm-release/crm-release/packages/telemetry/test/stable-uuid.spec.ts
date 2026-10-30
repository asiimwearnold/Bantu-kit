import { describe, expect, it } from "bun:test";
import { stableUuid, utcDay } from "../src/install";

const INSTALL = "cdf04e64-7722-4921-8b28-be936ce36353";

const OTHER = "c4b8d582-ec43-44f1-9a73-7b331eec90c7";

const UUID_SHAPE =
	/^[0-9a-f]{8}-[0-9a-f]{4}-8[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe("utcDay", () => {
	it("is the same string across a whole utc day", () => {
		expect(utcDay(new Date("2026-08-05T00:00:00.000Z"))).toBe("2026-08-05");
		expect(utcDay(new Date("2026-08-05T23:59:59.999Z"))).toBe("2026-08-05");
	});

	it("turns over at midnight utc", () => {
		expect(utcDay(new Date("2026-08-06T00:00:00.000Z"))).toBe("2026-08-06");
	});
});

describe("stableUuid", () => {
	it("is a well formed v8 uuid", () => {
		expect(stableUuid(INSTALL, "install_daily", "2026-08-05")).toMatch(
			UUID_SHAPE,
		);
	});

	it("repeats for the same parts, so a retry dedupes", () => {
		const first = stableUuid(INSTALL, "install_daily", "2026-08-05");
		const retry = stableUuid(INSTALL, "install_daily", "2026-08-05");

		expect(first).toBe(retry);
	});

	it("changes with the day", () => {
		expect(stableUuid(INSTALL, "install_daily", "2026-08-05")).not.toBe(
			stableUuid(INSTALL, "install_daily", "2026-08-06"),
		);
	});

	it("changes with the install", () => {
		expect(stableUuid(INSTALL, "install_daily", "2026-08-05")).not.toBe(
			stableUuid(OTHER, "install_daily", "2026-08-05"),
		);
	});

	it("separates a milestone from a rollup on the same install", () => {
		expect(stableUuid(INSTALL, "first_fact_applied")).not.toBe(
			stableUuid(INSTALL, "install_daily", "2026-08-05"),
		);
	});

	it("gives a milestone one id for the life of the install", () => {
		expect(stableUuid(INSTALL, "first_fact_applied")).toBe(
			stableUuid(INSTALL, "first_fact_applied"),
		);
	});
});
