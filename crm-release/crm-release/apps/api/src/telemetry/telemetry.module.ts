import { Module } from "@nestjs/common";
import { FunnelService } from "./funnel.service";
import { RollupService } from "./rollup.service";
import { TelemetryController } from "./telemetry.controller";
import { TelemetryService } from "./telemetry.service";

@Module({
	controllers: [TelemetryController],
	providers: [FunnelService, RollupService, TelemetryService],
	exports: [TelemetryService],
})
export class TelemetryModule {}
