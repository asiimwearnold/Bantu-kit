import { Module } from "@nestjs/common";
import { CurrencyModule } from "../currency/currency.module";
import { TrpcModule } from "../trpc/trpc.module";
import { DashboardRouter } from "./dashboard.router";
import { DashboardService } from "./dashboard.service";

@Module({
	imports: [TrpcModule, CurrencyModule],
	providers: [DashboardService, DashboardRouter],
})
export class DashboardModule {}
