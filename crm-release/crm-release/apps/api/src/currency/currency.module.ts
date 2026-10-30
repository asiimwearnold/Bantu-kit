import { Module } from "@nestjs/common";
import { TrpcModule } from "../trpc/trpc.module";
import { ConversionService } from "./conversion.service";
import { CurrencyRouter } from "./currency.router";
import { CurrencyService } from "./currency.service";
import { RatesController } from "./rates.controller";
import { RatesService } from "./rates.service";

@Module({
	imports: [TrpcModule],
	controllers: [RatesController],
	providers: [ConversionService, RatesService, CurrencyService, CurrencyRouter],
	exports: [ConversionService],
})
export class CurrencyModule {}
