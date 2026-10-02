import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

type CatalogModel = {
	provider?: string;
	id?: string;
	name?: string;
	identity?: { class?: string; family?: string; revision?: string } | null;
	thinking?: {
		efforts?: readonly string[];
		defaultLevel?: string;
		prefixBinding?: boolean;
		effortRouting?: Record<string, string>;
	} | null;
};

type QuotaSnapshot = {
	selected: string;
	applied: boolean;
	description: string;
	claudeSource: string;
	level: "ok" | "warn" | "hot";
	summary: string;
	panel: string[];
	roles: Record<string, string>;
};

type ThemeLike = { fg?: (role: string, text: string) => string };

type Ui = {
	setStatus?: (key: string, text: string | undefined) => void;
	setWidget?: (
		key: string,
		content: string[] | undefined,
		options?: { placement?: "aboveEditor" | "belowEditor" },
	) => void;
	notify?: (message: string, type?: "info" | "warning" | "error") => void;
	theme?: ThemeLike;
};

const STATUS_KEY = "quota";
const WIDGET_KEY = "quota-balancer";

function scriptPath(): string {
	const here = dirname(realpathSync(fileURLToPath(import.meta.url)));
	return join(here, "scripts", "balance.py");
}

function catalogJson(ctx: { models?: { list?: () => CatalogModel[] } }): string {
	const models = ctx.models?.list?.() ?? [];
	const slim = models.map((model) => {
		const thinking = model.thinking;
		return {
			provider: model.provider,
			id: model.id,
			name: model.name,
			identity: model.identity ?? null,
			thinking: thinking
				? {
						efforts: thinking.efforts ?? [],
						defaultLevel: thinking.defaultLevel,
						prefixBinding: thinking.prefixBinding === true,
						effortRouting: thinking.effortRouting ?? null,
					}
				: null,
		};
	});
	return JSON.stringify({ models: slim });
}

function runBalancer(catalog: string): Promise<{ code: number; stdout: string; stderr: string }> {
	const { promise, resolve } = Promise.withResolvers<{ code: number; stdout: string; stderr: string }>();
	const child = spawn("python3", [scriptPath(), "--apply", "--json"], {
		stdio: ["pipe", "pipe", "pipe"],
	});
	let stdout = "";
	let stderr = "";
	child.stdout.on("data", (chunk: Buffer) => {
		stdout += chunk.toString();
	});
	child.stderr.on("data", (chunk: Buffer) => {
		stderr += chunk.toString();
	});
	child.on("error", () => resolve({ code: 1, stdout, stderr }));
	child.on("close", (code) => resolve({ code: code ?? 1, stdout, stderr }));
	try {
		child.stdin.end(catalog);
	} catch {
		// The child may already have exited.
	}
	return promise;
}

function readSnapshot(stdout: string): QuotaSnapshot | undefined {
	const raw = stdout.trim();
	if (!raw) return undefined;
	let data: {
		selected?: string;
		applied?: boolean;
		level?: string;
		summary?: string;
		panel?: string[];
		profile?: {
			name?: string;
			description?: string;
			claude_source?: string;
			modelRoles?: Record<string, string>;
		};
	};
	try {
		data = JSON.parse(raw);
	} catch {
		return undefined;
	}
	const level = data.level === "hot" || data.level === "warn" ? data.level : "ok";
	const roles = data.profile?.modelRoles ?? {};
	const panel = Array.isArray(data.panel) ? data.panel.filter((line) => typeof line === "string" && line.length > 0) : [];
	if (!data.selected && !data.profile?.name && panel.length === 0) return undefined;
	return {
		selected: (data.selected || data.profile?.name || "auto").toUpperCase(),
		applied: data.applied === true,
		description: data.profile?.description ?? "",
		claudeSource: data.profile?.claude_source ?? "",
		level,
		summary: data.summary || data.profile?.description || "",
		panel,
		roles,
	};
}

function colorize(ui: Ui | undefined, role: string, text: string): string {
	try {
		const theme = ui?.theme;
		if (!theme?.fg) return text;
		return theme.fg(role, text);
	} catch {
		return text;
	}
}

function paint(
	ctx: { ui?: Ui },
	snapshot: QuotaSnapshot | undefined,
	error: string | undefined,
	showPanel: boolean,
): void {
	const ui = ctx.ui;
	if (!ui?.setStatus) return;

	if (!snapshot) {
		const dot = colorize(ui, "error", "●");
		ui.setStatus(STATUS_KEY, `${dot} ⚖ ${colorize(ui, "muted", "quota: ")}${colorize(ui, "text", "error")}`);
		if (!showPanel) {
			ui.setWidget?.(WIDGET_KEY, undefined);
			return;
		}
		ui.setWidget?.(WIDGET_KEY, error ? [`⚖ quota-balancer`, error, "/rebalance to retry"] : undefined, {
			placement: "aboveEditor",
		});
		return;
	}

	const dotRole = snapshot.level === "hot" ? "error" : snapshot.level === "warn" ? "warning" : "accent";
	const dot = colorize(ui, dotRole, "●");
	const label = colorize(ui, "muted", "quota: ");
	const name = colorize(ui, "text", snapshot.selected);
	const summary = snapshot.summary ? ` · ${colorize(ui, "muted", snapshot.summary)}` : "";
	ui.setStatus(STATUS_KEY, `${dot} ⚖ ${label}${name}${summary}`);

	if (!showPanel) {
		ui.setWidget?.(WIDGET_KEY, undefined);
		return;
	}
	const lines = snapshot.panel.length > 0 ? snapshot.panel : [
		`⚖ quota-balancer  [${snapshot.selected}]`,
		snapshot.description,
		`default ${snapshot.roles.default || "—"}`,
		snapshot.claudeSource ? `Claude: ${snapshot.claudeSource}` : "",
	].filter((line) => line.length > 0);
	ui.setWidget?.(WIDGET_KEY, lines, { placement: "aboveEditor" });
}

export default function (pi: ExtensionAPI) {
	let snapshot: QuotaSnapshot | undefined;
	let lastError: string | undefined;
	let showPanel = false;
	let running = false;

	async function refresh(ctx: { ui?: Ui; models?: { list?: () => CatalogModel[] } }, announce: boolean): Promise<void> {
		if (running) return;
		running = true;
		const ui = ctx.ui;
		ui?.setStatus?.(
			STATUS_KEY,
			`${colorize(ui, "dim", "○")} ⚖ ${colorize(ui, "muted", "quota: ")}${colorize(ui, "text", "…")}`,
		);
		try {
			const { code, stdout, stderr } = await runBalancer(catalogJson(ctx));
			const next = code === 0 ? readSnapshot(stdout) : undefined;
			if (!next) {
				snapshot = undefined;
				lastError = (stderr || stdout || "quota balancer returned no quotas").trim().split("\n").at(-1);
				paint(ctx, undefined, lastError, showPanel);
				if (announce) ui?.notify?.(lastError || "Failed to run the quota balancer.", "error");
				return;
			}
			snapshot = next;
			lastError = undefined;
			paint(ctx, snapshot, undefined, showPanel);
			if (announce) {
				ui?.notify?.(`Quota ${snapshot.selected} · ${snapshot.summary}`, levelNotify(snapshot));
			}
		} catch (err) {
			snapshot = undefined;
			lastError = String(err);
			paint(ctx, undefined, lastError, showPanel);
			if (announce) ui?.notify?.(lastError, "error");
		} finally {
			running = false;
		}
	}

	function levelNotify(current: QuotaSnapshot): "info" | "warning" | "error" {
		if (current.level === "hot") return "error";
		if (current.level === "warn") return "warning";
		return "info";
	}

	// The first frame wipes notify(). The footer badge is set before that paint
	// and replaced when the quota read finishes, same channel caveman/ponytail use.
	pi.on("session_start", (_event, ctx) => {
		void refresh(ctx, false);
	});

	pi.on("session_switch", (_event, ctx) => {
		paint(ctx, snapshot, lastError, showPanel);
	});

	pi.on("session_shutdown", (_event, ctx) => {
		ctx.ui?.setStatus?.(STATUS_KEY, undefined);
		ctx.ui?.setWidget?.(WIDGET_KEY, undefined);
	});

	pi.registerCommand("rebalance", {
		description: "Re-read provider quotas and rewrite the OMP model assignment",
		handler: async (_args, ctx) => {
			await refresh(ctx, true);
		},
	});

	pi.registerCommand("quota", {
		description: "Show, hide, or refresh the quota-balancer panel",
		handler: async (args, ctx) => {
			const command = args.trim().toLowerCase();
			if (command === "hide" || command === "off" || (command === "" && showPanel)) {
				showPanel = false;
				paint(ctx, snapshot, lastError, showPanel);
				ctx.ui?.notify?.("Quota panel hidden. The footer badge stays. /quota on brings it back.", "info");
				return;
			}
			if (command === "show" || command === "on" || command === "") {
				showPanel = true;
				paint(ctx, snapshot, lastError, showPanel);
				return;
			}
			if (command === "status") {
				if (!snapshot) {
					ctx.ui?.notify?.(lastError || "Quota balancer has no reading yet.", "warning");
					return;
				}
				ctx.ui?.notify?.(`Quota ${snapshot.selected} · ${snapshot.summary}`, levelNotify(snapshot));
				return;
			}
			if (command === "refresh") {
				await refresh(ctx, true);
				return;
			}
			ctx.ui?.notify?.("Usage: /quota [on|off|status|refresh]", "warning");
		},
	});
}
