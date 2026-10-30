import {
	flushTelemetry,
	onTelemetryProblem,
	shutdownTelemetry,
	syncVersion,
	telemetryDisabled,
} from "@crm/telemetry";
import {
	Injectable,
	Logger,
	type OnApplicationShutdown,
	type OnModuleInit,
} from "@nestjs/common";
import { RollupService } from "./rollup.service";

const ROLLUP_INTERVAL_MS = 60 * 60 * 1000;

@Injectable()
export class TelemetryService implements OnModuleInit, OnApplicationShutdown {
	private readonly logger = new Logger(TelemetryService.name);
	private timer: NodeJS.Timeout | null = null;

	constructor(private readonly rollup: RollupService) {}

	async onModuleInit(): Promise<void> {
		onTelemetryProblem((message) => this.logger.debug({ message }));

		if (telemetryDisabled()) {
			this.logger.log({
				message: "Anonymous usage telemetry is off for this install.",
			});
			return;
		}

		const install = await syncVersion();

		this.logger.log({
			message: "Anonymous usage telemetry is on. See docs/telemetry.md.",
			crmVersion: install?.version,
		});

		void this.rollup.run().catch(() => {});

		this.timer = setInterval(() => {
			void this.rollup.run().catch(() => {});
		}, ROLLUP_INTERVAL_MS);

		this.timer.unref?.();
	}

	async onApplicationShutdown(): Promise<void> {
		if (this.timer) clearInterval(this.timer);
		this.timer = null;

		await flushTelemetry();
		await shutdownTelemetry();
	}
}
