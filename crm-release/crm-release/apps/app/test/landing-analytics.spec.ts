import { describe, expect, it } from "bun:test";
import { analyticsAllowed } from "../lib/analytics";

describe("analyticsAllowed", () => {
	it("allows the two hosts the landing page is served from", () => {
		expect(analyticsAllowed("trycrm.ai")).toBe(true);
		expect(analyticsAllowed("www.trycrm.ai")).toBe(true);
	});

	it("ignores case and surrounding whitespace", () => {
		expect(analyticsAllowed(" TryCRM.ai ")).toBe(true);
	});

	it("refuses a self-hosted install serving the same page", () => {
		expect(analyticsAllowed("crm.acme.com")).toBe(false);
		expect(analyticsAllowed("localhost")).toBe(false);
	});

	it("refuses a preview deployment", () => {
		expect(analyticsAllowed("crm-git-lewis-telemetry.vercel.app")).toBe(false);
	});

	it("refuses a host that merely ends in the marketing domain", () => {
		expect(analyticsAllowed("evil-trycrm.ai")).toBe(false);
		expect(analyticsAllowed("trycrm.ai.attacker.com")).toBe(false);
	});
});
