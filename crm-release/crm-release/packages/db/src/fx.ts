import type { Db } from "./client";
import { minorUnitsOf, normalizeCurrency } from "./currency";
import { Prisma } from "./generated/prisma/client";
import { RateSource } from "./generated/prisma/enums";

export type RateOrigin = "IDENTITY" | "MANUAL" | "FETCHED";

export interface ResolvedRate {
	rate: Prisma.Decimal;
	asOf: Date;
	origin: RateOrigin;
	provider: string | null;
}

export interface Conversion {
	baseAmount: Prisma.Decimal;
	baseCurrency: string;
	fxRate: Prisma.Decimal;
	fxRateAt: Date;
	origin: RateOrigin;
}

export const ONE = new Prisma.Decimal(1);

export async function resolveRate(
	db: Db,
	base: string,
	quote: string,
	now: Date = new Date(),
): Promise<ResolvedRate | null> {
	const baseCurrency = normalizeCurrency(base);
	const quoteCurrency = normalizeCurrency(quote);

	if (!baseCurrency || !quoteCurrency) return null;

	if (baseCurrency === quoteCurrency) {
		return { rate: ONE, asOf: now, origin: "IDENTITY", provider: null };
	}

	const rows = await db.exchangeRate.findMany({
		where: { baseCurrency, quoteCurrency },
		select: { rate: true, asOf: true, source: true, provider: true },
	});

	const manual = rows.find((row) => row.source === RateSource.MANUAL);
	const chosen =
		manual ?? rows.find((row) => row.source === RateSource.FETCHED);

	if (!chosen || chosen.rate.lessThanOrEqualTo(0)) return null;

	return {
		rate: chosen.rate,
		asOf: chosen.asOf,
		origin: chosen.source === RateSource.MANUAL ? "MANUAL" : "FETCHED",
		provider: chosen.provider,
	};
}

export function applyRate(
	amount: Prisma.Decimal,
	rate: ResolvedRate,
	base: string,
): Conversion {
	const baseCurrency = normalizeCurrency(base);

	return {
		baseAmount: amount
			.times(rate.rate)
			.toDecimalPlaces(minorUnitsOf(baseCurrency)),
		baseCurrency,
		fxRate: rate.rate,
		fxRateAt: rate.asOf,
		origin: rate.origin,
	};
}

export async function convertToBase(
	db: Db,
	amount: Prisma.Decimal | null,
	from: string,
	base: string,
	now: Date = new Date(),
): Promise<Conversion | null> {
	if (amount === null) return null;

	const rate = await resolveRate(db, base, from, now);
	if (!rate) return null;

	return applyRate(amount, rate, base);
}
