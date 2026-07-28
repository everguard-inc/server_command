const {
	pipelineOrder,
	pipelineGroups,
	pipelineStatic,
	statusDisplay,
	urls,
} = window.APP_CONFIG;
const cameraFeedBase = urls.cameraFeed;
const MEMORY_UNIT_BYTES = {
	B: 1,
	KIB: 1024,
	MIB: 1024 ** 2,
	GIB: 1024 ** 3,
	TIB: 1024 ** 4,
};

// --- State ---
let statusFilter = null;
let lastPipelines = null;
let fullCollecting = false;
let pollTimer = null;
let fastPollTimer = null;
let updatePollTimer = null;
let updateFailureNotified = false;
let lastUpdateServerByIp = {};
const POLL_MS = 5000;
const JOB_POLL_MS = 1000;
const COMMAND_PROGRESS_TICK_MS = 250;
const COMMAND_BTN_IDS = ["btn_start", "btn_stop", "btn_restart", "btn_status", "btn_update"];
const SERVER_COMMAND_BTN_IDS = ["btn_server_shutdown", "btn_server_reboot"];
const COMMAND_LABELS = {
	"service:start": "Start",
	"service:stop": "Stop",
	"service:restart": "Restart",
	"service:status": "Status",
	update: "Update",
	"server:shutdown": "Shutdown",
	"server:reboot": "Reboot",
};
const CARD_SECTION_IDS = [
	"section_pipeline_status",
	"section_service_control",
	"section_server_control",
];
const UPDATE_PIPELINE_STEPS = new Set([
	"pipeline_build",
	"pipeline_restart",
	"pipelines_parallel",
	"pipelines_restart",
]);
// Labels for known kinds. Dropdown/filter order follows servers.json
// (first appearance in pipelineOrder), not this array.
const PIPELINE_KIND_LABELS = {
	sys: "SYSTEM",
	rtls: "RTLS",
	camera_drift: "DRIFT",
	eg: "EG",
	plc: "PLC",
	cobble: "COBBLE",
	forklift: "FORKLIFT",
	detseg: "DETSEG",
};
const VIEW_FILTER_META = {
	group: {
		key: "groupFilter",
		allLabel: "All groups",
		options() {
			return pipelineGroups.map(([key]) => ({ value: key, label: key }));
		},
	},
	server: {
		key: "serverFilter",
		allLabel: "All servers",
		options() {
			const ips = new Set();
			for (const name of pipelineOrder) {
				const ip = pipelineInfo(name).server_ip;
				if (ip) ips.add(ip);
			}
			return [...ips]
				.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
				.map((value) => ({ value, label: value }));
		},
	},
	type: {
		key: "typeFilter",
		allLabel: "All types",
		options() {
			return pipelineKindsPresent()
				.map(([value, label]) => ({ value, label }))
				.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: "base" }));
		},
	},
};
const viewState = {
	groupFilter: null,
	serverFilter: null,
	typeFilter: null,
};
let appModalResolver = null;
let commandModalRunning = false;
let servicePollTimer = null;
let commandProgressTickTimer = null;
let activeServiceJobId = null;
let activeServiceCommand = null;
let activeServiceTargets = [];
let activeServiceJob = null;
let activeUpdateTargets = null;
let lastUpdateStatusData = null;
let updateSeenActive = false;
let ipUpdating = new Set();
const pingBusy = new Set();
let cameraLiveSource = null;

// --- DOM helpers ---

function $(id) {
	return document.getElementById(id);
}

function setOverlayVisible(modal, visible) {
	const isVisible = !modal.classList.contains("hidden");
	if (isVisible === visible) return;
	modal.classList.toggle("hidden", !visible);
	modal.setAttribute("aria-hidden", visible ? "false" : "true");
	syncBodyScrollLock();
}

let lockedBodyScrollTop = 0;

function syncBodyScrollLock() {
	const hasOpenOverlay = document.querySelector(".overlay:not(.hidden)");
	if (hasOpenOverlay) {
		if (!document.body.classList.contains("overlay-scroll-lock")) {
			lockedBodyScrollTop = window.scrollY || document.documentElement.scrollTop || 0;
			document.body.classList.add("overlay-scroll-lock");
			document.body.style.top = `-${lockedBodyScrollTop}px`;
		}
		return;
	}
	document.body.classList.remove("overlay-scroll-lock");
	document.body.style.top = "";
	window.scrollTo(0, lockedBodyScrollTop);
}

function parseJsonResponse(text) {
	try {
		return JSON.parse(text || "{}");
	} catch (e) {
		return {};
	}
}

function escapeAttr(value) {
	return String(value)
		.replace(/&/g, "&amp;")
		.replace(/"/g, "&quot;")
		.replace(/</g, "&lt;");
}

function escapeHtml(value) {
	return String(value)
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;");
}

function httpRequest(method, url, options) {
	const opts = options || {};
	if (method === "GET" && document.hidden && !opts.allowHidden) return;
	const request = new XMLHttpRequest();
	request.open(method, url);
	if (method === "POST") {
		request.setRequestHeader("Content-Type", "application/json;charset=UTF-8");
	}
	if (opts.timeout) request.timeout = opts.timeout;
	request.onload = function () {
		opts.onload(parseJsonResponse(request.response), request.status, request);
	};
	if (opts.onerror) request.onerror = opts.onerror;
	if (opts.ontimeout) request.ontimeout = opts.ontimeout;
	const body = opts.body;
	request.send(body == null ? null : (typeof body === "string" ? body : JSON.stringify(body)));
}

function postJson(url, body, timeoutMs, handlers) {
	httpRequest("POST", url, {
		body,
		timeout: timeoutMs,
		onload: (data, status) => handlers.onload(status, data),
		onerror: handlers.onerror,
		ontimeout: handlers.ontimeout,
	});
}

function fetchJsonGet(url, onLoad, options) {
	const opts = options || {};
	httpRequest("GET", url, {
		allowHidden: opts.allowHidden,
		timeout: opts.timeout,
		onload: onLoad,
		onerror: opts.onerror,
		ontimeout: opts.ontimeout,
	});
}

// --- Pipeline metadata & grouping ---

function pipelineKindLabel(kind) {
	return PIPELINE_KIND_LABELS[kind] || String(kind || "").toUpperCase();
}

function pipelineKindMeta(name) {
	const info = pipelineInfo(name);
	const rawKind = info.pipeline_kind || "";
	// Type badge/filter: Kafka + CV collapse to a single PLC kind.
	if (
		rawKind === "plc_cv"
		|| rawKind === "plc_kafka"
		|| rawKind === "plc"
		|| info.is_plc_cv
		|| info.is_kafka
	) {
		return { kind: "plc", label: "PLC" };
	}
	if (pipelineRuntimeFlags(name).isCameraDrift) {
		return { kind: "camera_drift", label: pipelineKindLabel("camera_drift") };
	}
	if (rawKind) {
		return {
			kind: rawKind,
			label: info.pipeline_kind_label || pipelineKindLabel(rawKind),
		};
	}
	if (info.is_sys_monitor) return { kind: "sys", label: "SYSTEM" };
	if (info.is_rtls) return { kind: "rtls", label: "RTLS" };
	return { kind: "eg", label: "EG" };
}

function pipelineKindKey(name) {
	return pipelineKindMeta(name).kind;
}

function pipelineKindsPresent() {
	// Order = first appearance in servers.json / pipelineOrder.
	const ordered = [];
	const seen = new Set();
	for (const name of pipelineOrder) {
		const meta = pipelineKindMeta(name);
		if (seen.has(meta.kind)) continue;
		seen.add(meta.kind);
		ordered.push([meta.kind, meta.label]);
	}
	return ordered;
}

function pipelineInGroup(name, groupKey) {
	return pipelineGroups.some(([key, names]) => key === groupKey && names.includes(name));
}

function pipelineInAnyGroup(name, groupKeys) {
	return groupKeys.some((groupKey) => pipelineInGroup(name, groupKey));
}

function viewFilterIsAll(filter) {
	return filter === null;
}

function viewFilterIsNone(filter) {
	return Array.isArray(filter) && filter.length === 0;
}

function viewFilterApplies(filter) {
	return Array.isArray(filter) && filter.length > 0;
}

function filteredPipelineNames(state) {
	return pipelineOrder.filter((name) => {
		const info = pipelineInfo(name);
		if (viewFilterIsNone(state.groupFilter)) {
			return false;
		}
		if (viewFilterApplies(state.groupFilter) && !pipelineInAnyGroup(name, state.groupFilter)) {
			return false;
		}
		if (viewFilterIsNone(state.serverFilter)) {
			return false;
		}
		if (viewFilterApplies(state.serverFilter) && !state.serverFilter.includes(info.server_ip || "")) {
			return false;
		}
		if (viewFilterIsNone(state.typeFilter)) {
			return false;
		}
		if (viewFilterApplies(state.typeFilter) && !state.typeFilter.includes(pipelineKindKey(name))) {
			return false;
		}
		return true;
	});
}

function bucketNamesByServer(names) {
	// Preserve first-seen IP order from names (already pipelineOrder-filtered).
	const buckets = new Map();
	for (const name of names) {
		const ip = pipelineInfo(name).server_ip || "no IP";
		if (!buckets.has(ip)) {
			buckets.set(ip, []);
		}
		buckets.get(ip).push(name);
	}
	return [...buckets.entries()];
}

function currentGroups() {
	const state = viewState;
	const names = filteredPipelineNames(state);
	if (!names.length) {
		return [];
	}
	if (viewFilterApplies(state.serverFilter) && viewFilterIsAll(state.groupFilter)) {
		return bucketNamesByServer(names);
	}
	const buckets = [];
	for (const [groupKey, groupNames] of pipelineGroups) {
		if (viewFilterApplies(state.groupFilter) && !state.groupFilter.includes(groupKey)) {
			continue;
		}
		const visible = groupNames.filter((name) => names.includes(name));
		if (visible.length) {
			buckets.push([groupKey, visible]);
		}
	}
	return buckets;
}

function pipelineInfo(name) {
	return pipelineStatic[name] || {};
}

function commandJobBase(name) {
	const info = pipelineInfo(name);
	return {
		pipeline: name,
		server_ip: info.server_ip || "",
		service: info.service_name || "",
	};
}

function uniqueHostIps(names, requireIp = false) {
	const hosts = [];
	const seen = new Set();
	for (const name of names) {
		const ip = pipelineInfo(name).server_ip || "";
		if (requireIp && !ip) continue;
		const key = requireIp ? ip : (ip || name);
		if (seen.has(key)) continue;
		seen.add(key);
		hosts.push(key);
	}
	return hosts;
}

function pipelineRuntimeFlags(name, entry) {
	const info = pipelineInfo(name);
	const rawKind = info.pipeline_kind || "";
	return {
		isRtls: Boolean(entry?.is_rtls ?? info.is_rtls),
		isSys: Boolean(entry?.is_sys_monitor ?? info.is_sys_monitor),
		isKafka: Boolean(
			entry?.is_kafka
			?? info.is_kafka
			?? rawKind === "plc_kafka",
		),
		isPlcCv: Boolean(
			entry?.is_plc_cv
			?? info.is_plc_cv
			?? rawKind === "plc_cv",
		),
		isCameraDrift: Boolean(
			entry?.is_camera_drift
			?? info.is_camera_drift
			?? rawKind === "camera_drift",
		),
	};
}

function streamUrlFor(name, entry) {
	const info = pipelineInfo(name);
	const { isKafka, isCameraDrift } = pipelineRuntimeFlags(name, entry);
	const preferred = isKafka
		? "plc_status_url"
		: (isCameraDrift ? "drift_service_url" : null);
	if (preferred) {
		return (
			(entry && entry[preferred])
			|| (entry && entry.url)
			|| info[preferred]
			|| info.url
			|| ""
		);
	}
	return (entry && entry.url) || info.url || "";
}

// --- Modals ---

function openCommandModal() {
	setOverlayVisible($("command_modal"), true);
}

function closeCommandModal() {
	if (commandModalRunning) return;
	setOverlayVisible($("command_modal"), false);
}

function setCommandModalActions(complete) {
	commandModalRunning = !complete;
	$("command_modal_ok").hidden = !complete;
	$("command_modal_close").hidden = !complete;
}

function commandResponseTone(entry, command) {
	if (command === "service:status") {
		if (entry.ok) return "ok";
		if (entry.info) return "warn";
		return "err";
	}
	if (entry.info || entry.response === "Already running" || entry.response === "Already stopped") {
		return "warn";
	}
	return entry.ok ? "ok" : "err";
}

function hideCommandModalOverall() {
	const panel = $("command_modal_overall");
	panel.classList.add("hidden");
	panel.innerHTML = "";
}

function setCommandModalFinishedTitle(command, allOk) {
	$("command_modal_title").textContent = allOk
		? `${commandLabel(command)} complete`
		: `${commandLabel(command)} finished with errors`;
	$("command_modal_hint").textContent = "Service Status refreshes when done";
}

function resultsByPipeline(results) {
	const byPipeline = {};
	for (const entry of results) {
		byPipeline[entry.pipeline] = entry;
	}
	return byPipeline;
}

function renderCommandListHtml(items) {
	return items.map((item) => {
		const detailHtml = item.detail
			? `<div class="command-result-detail command-result-detail--${item.tone}">${item.detail}</div>`
			: (item.pending
				? `<div class="command-result-detail command-result-detail--pending">Running…</div>`
				: "");
		return `
			<div class="command-result-line">
				<div class="command-result-host">${item.host}</div>
				${detailHtml}
			</div>
		`;
	}).join("");
}

function closeAppModal(result) {
	setOverlayVisible($("app_modal"), false);
	if (appModalResolver) {
		const resolve = appModalResolver;
		appModalResolver = null;
		resolve(result);
	}
}

function openAppModal(options) {
	const {
		title = "Notice",
		message = "",
		bodyHtml = "",
		mode = "alert",
		tone = "info",
		okLabel = "OK",
		cancelLabel = "Cancel",
	} = options || {};

	return new Promise((resolve) => {
		appModalResolver = resolve;
		const modal = $("app_modal");
		const dialog = modal.querySelector(".overlay__dialog");
		const cancelBtn = $("app_modal_cancel");
		const okBtn = $("app_modal_ok");
		const messageEl = $("app_modal_message");
		const isHtml = Boolean(bodyHtml);

		$("app_modal_title").textContent = title;
		messageEl.className = `overlay__message overlay__message--${tone}${isHtml ? " overlay__message--html" : ""}`;
		if (isHtml) {
			messageEl.innerHTML = bodyHtml;
		} else {
			messageEl.textContent = message;
		}

		modal.dataset.mode = mode;
		dialog.classList.toggle("overlay__dialog--form", isHtml);
		cancelBtn.hidden = mode !== "confirm";
		okBtn.textContent = okLabel;
		cancelBtn.textContent = cancelLabel;

		setOverlayVisible(modal, true);
		okBtn.focus();
	});
}

function showAlertModal(message, options) {
	return openAppModal({
		title: "Notice",
		message,
		mode: "alert",
		tone: "info",
		...(options || {}),
	}).then(() => {});
}

function showConfirmModal(message, options) {
	return openAppModal({
		title: "Confirm",
		message,
		mode: "confirm",
		tone: "info",
		okLabel: "Confirm",
		cancelLabel: "Cancel",
		...(options || {}),
	}).then((result) => result === true);
}

function repoLabelForPipeline(name) {
	const path = (pipelineInfo(name).eg_pipeline_path || "").replace(/\\/g, "/");
	if (!path) return "";
	const parts = path.split("/").filter(Boolean);
	return parts.length ? parts[parts.length - 1] : "";
}

function updateIncludesSysMonitor(targets) {
	for (const name of targets) {
		const info = pipelineInfo(name);
		if (info.is_sys_monitor || info.sys_monitor_path) return true;
	}
	const ips = new Set(
		targets.map((name) => pipelineInfo(name).server_ip).filter(Boolean),
	);
	if (ips.size !== 1) return false;
	const ip = [...ips][0];
	return pipelineOrder.some((name) => {
		const info = pipelineInfo(name);
		return info.is_sys_monitor && info.server_ip === ip;
	});
}

function updateReposForTargets(targets) {
	const repos = new Set();
	for (const name of targets) {
		const info = pipelineInfo(name);
		if (info.is_sys_monitor) continue;
		const repo = repoLabelForPipeline(name);
		if (repo) repos.add(repo);
	}
	if (updateIncludesSysMonitor(targets)) repos.add("system_monitor");
	return [...repos];
}

function collectUpdateGitRefs() {
	const refs = {};
	document.querySelectorAll("#app_modal_message [data-git-ref-repo]").forEach((input) => {
		const repo = input.getAttribute("data-git-ref-repo");
		const value = (input.value || "").trim();
		if (repo && value) refs[repo] = value;
	});
	return refs;
}

function showUpdateConfirmModal(targets) {
	const rows = updateReposForTargets(targets).map((repo) => `
		<label class="update-ref-row">
			<span class="update-ref-repo">${escapeAttr(repo)}</span>
			<input type="text" class="update-ref-input" data-git-ref-repo="${escapeAttr(repo)}"
				placeholder="latest (empty)" autocomplete="off" spellcheck="false" />
		</label>
	`).join("");
	return openAppModal({
		title: "Update",
		bodyHtml: `
			<p class="update-ref-lead">Update ${targets.length} service(s)?</p>
			<p class="update-ref-hint">Optional: set a branch, tag, or commit per repo. Leave empty for latest.</p>
			<div class="update-ref-list">${rows}</div>
		`,
		mode: "confirm",
		okLabel: "Update",
		cancelLabel: "Cancel",
	}).then((result) => (result === true ? collectUpdateGitRefs() : null));
}

// --- Card sections ---

function toggleCardSection(sectionId) {
	const section = $(sectionId);
	if (!section) return;
	const expanded = section.classList.toggle("is-expanded");
	const btn = section.querySelector(".card-collapse-btn");
	if (btn) {
		btn.setAttribute("aria-expanded", expanded ? "true" : "false");
	}
	try {
		localStorage.setItem(`section:${sectionId}`, expanded ? "1" : "0");
	} catch (e) {
		/* ignore */
	}
}

function restoreCardSections() {
	for (const sectionId of CARD_SECTION_IDS) {
		const section = $(sectionId);
		if (!section) continue;
		let stored = null;
		try {
			stored = localStorage.getItem(`section:${sectionId}`);
		} catch (e) {
			stored = null;
		}
		if (stored !== "0") continue;
		section.classList.remove("is-expanded");
		const btn = section.querySelector(".card-collapse-btn");
		if (btn) btn.setAttribute("aria-expanded", "false");
	}
}

function startFreshCollect() {
	stopFastPoll();
	setTotalProgress(0, pipelineOrder.length, true);
	updateLastUpdateDisplay({ ready: false, background_refreshing: false, updated_at: 0 }, true);
	showLoadingTable();
	fetchStatus(true);
}

// --- Polling ---

function clearPollTimer(getTimer, setTimer) {
	const timer = getTimer();
	if (timer) {
		clearInterval(timer);
		setTimer(null);
	}
}

function stopAllPolling() {
	clearPollTimer(() => pollTimer, (value) => { pollTimer = value; });
	stopFastPoll();
	stopUpdatePolling();
	stopServicePolling();
}

function pauseAllPolling() {
	clearPollTimer(() => pollTimer, (value) => { pollTimer = value; });
	stopFastPoll();
	pauseUpdatePolling();
	clearPollTimer(() => servicePollTimer, (value) => { servicePollTimer = value; });
	stopCommandProgressTick();
}

function stopServicePolling() {
	clearPollTimer(() => servicePollTimer, (value) => { servicePollTimer = value; });
	stopCommandProgressTick();
	activeServiceJobId = null;
	activeServiceCommand = null;
	activeServiceTargets = [];
	activeServiceJob = null;
}

function stopCommandProgressTick() {
	if (commandProgressTickTimer) {
		clearInterval(commandProgressTickTimer);
		commandProgressTickTimer = null;
	}
}

function startCommandProgressTick() {
	stopCommandProgressTick();
	commandProgressTickTimer = setInterval(() => {
		if (activeUpdateTargets) {
			if (lastUpdateStatusData) {
				renderUpdateModalProgress(lastUpdateStatusData, Date.now());
			}
			return;
		}
		if (!activeServiceCommand || !activeServiceTargets.length) return;
		if (!activeServiceJob || activeServiceJob.done) return;
		renderCommandJobProgress(
			activeServiceCommand,
			activeServiceJob,
			activeServiceTargets,
			Date.now(),
		);
	}, COMMAND_PROGRESS_TICK_MS);
}

function pauseUpdatePolling() {
	if (updatePollTimer) {
		clearInterval(updatePollTimer);
		updatePollTimer = null;
	}
}

function stopUpdatePolling() {
	pauseUpdatePolling();
	const hadUpdate = Boolean(activeUpdateTargets);
	activeUpdateTargets = null;
	lastUpdateStatusData = null;
	lastUpdateServerByIp = {};
	updateSeenActive = false;
	if (hadUpdate && !activeServiceJobId) {
		stopCommandProgressTick();
	}
}

function startUpdateModalPolling(targets) {
	stopUpdatePolling();
	activeUpdateTargets = targets.slice();
	updateFailureNotified = false;
	updateSeenActive = false;
	fetchUpdateStatus();
	updatePollTimer = setInterval(fetchUpdateStatus, JOB_POLL_MS);
	startCommandProgressTick();
}

// --- Update polling ---

function mergeUpdateServers(servers) {
	const incoming = servers || {};
	for (const [ip, status] of Object.entries(incoming)) {
		if (status) lastUpdateServerByIp[ip] = status;
	}
	return { ...lastUpdateServerByIp };
}

function parseServiceStep(serviceStep) {
	if (!serviceStep) return { step: "", label: "" };
	if (typeof serviceStep === "string") return { step: "", label: serviceStep };
	return {
		step: serviceStep.step || "",
		label: serviceStep.label || "",
	};
}

function finalizeUpdateEntry(base, { ok, response, error = null }) {
	const message = error || response || (ok ? "Update complete" : "Update failed");
	return {
		...base,
		status: "done",
		phase: "done",
		step: ok ? "done" : "failed",
		ok,
		response: message,
		...(error != null ? { error } : {}),
	};
}

function pipelineUpdateResponse(name, serverStatus) {
	const staticInfo = pipelineInfo(name);
	const service = staticInfo.service_name || "";
	const step = serverStatus.step || "";
	const serviceSteps = serverStatus.service_steps || {};
	const { step: serviceStepState, label: serviceLabel } = parseServiceStep(serviceSteps[service]);

	if (service && serviceSteps[service]) {
		if (serviceStepState === "failed") {
			return serviceLabel || "Update failed";
		}
		if (serviceLabel) return serviceLabel;
	}

	if (staticInfo.is_sys_monitor && step.startsWith("sys_monitor")) {
		return serverStatus.step_label || "Working";
	}

	if (UPDATE_PIPELINE_STEPS.has(step)) {
		if (serviceStepState === "failed") {
			return serviceLabel || "Update failed";
		}
		return service ? `${service}: waiting` : "Waiting…";
	}
	if (!staticInfo.is_sys_monitor && step.startsWith("sys_monitor")) {
		return serverStatus.step_label || "system_monitor: working";
	}
	return serverStatus.step_label || step || "Working";
}

function updateStepDetail(text) {
	const raw = (text || "").trim();
	if (!raw) return "";
	return raw
		.replace(/^system_monitor:\s*/i, "")
		.replace(/^eg_rtls:\s*/i, "")
		.replace(/^eg_[0-9a-f-]+:\s*/i, "")
		.trim();
}

function updateOverallActiveLabel(serverStatus, order) {
	if (!serverStatus) return "SYS · preparing";

	const step = serverStatus.step || "";
	const stepLabel = serverStatus.step_label || "";
	const serviceSteps = serverStatus.service_steps || {};
	const hasRtls = order.some((name) => pipelineInfo(name).is_rtls);
	const rtlsService = order
		.map((name) => pipelineInfo(name))
		.find((info) => info.is_rtls)?.service_name;

	if (step === "starting") return "SYS · preparing";
	if (step.startsWith("sys_monitor") && step !== "sys_monitor_restart") {
		return `SYS · ${updateStepDetail(stepLabel) || "working"}`;
	}
	if (step === "sys_monitor_restart") return "SYS · restarting";
	if (step === "prune_images") return "Cleanup · pruning images";
	if (step === "done") return "Complete";
	if (step === "failed") return "Failed";

	if (hasRtls && rtlsService) {
		const rtlsStep = serviceSteps[rtlsService];
		const rtlsPending = !rtlsStep || rtlsStep.step !== "done";
		if (rtlsPending) {
			const detail = updateStepDetail(
				typeof rtlsStep === "string" ? rtlsStep : rtlsStep?.label,
			) || (step.includes("restart") ? "restarting" : "building");
			return `RTLS · ${detail}`;
		}
	}

	if (step.includes("restart")) return "Services · restarting";
	if (step === "pipeline_build" || step === "pipelines_parallel") {
		return "Services · building";
	}
	return "Update";
}

function hostUpdateErrorForService(err, service, pipelineName) {
	if (!err) return null;
	const parts = err.split(/;\s*/).filter(Boolean);
	const repoLabel = repoLabelForPipeline(pipelineName);
	const matchesService = (part) => {
		if (service && part.startsWith(`${service}:`)) return true;
		if (repoLabel && (part.startsWith(`${repoLabel}:`) || part.includes(`${repoLabel}:`))) {
			return true;
		}
		if (pipelineName && part.includes(pipelineName)) return true;
		return false;
	};
	if (parts.length <= 1) {
		return matchesService(err) ? err : null;
	}
	return parts.find(matchesService) || null;
}

function pipelineServiceStep(serverStatus, serviceName) {
	if (!serverStatus || !serviceName) return null;
	const steps = serverStatus.service_steps || {};
	return steps[serviceName] || null;
}

function sysMonitorUpdateComplete(name, serverStatus) {
	if (!pipelineInfo(name).is_sys_monitor || !serverStatus) return false;
	const step = serverStatus.step || "";
	if (step === "done" || step === "failed") return true;
	const service = pipelineInfo(name).service_name || "";
	const serviceStep = pipelineServiceStep(serverStatus, service);
	if (!serviceStep || typeof serviceStep === "string") return false;
	if (serviceStep.step === "failed") return true;
	if (serviceStep.step !== "done") return false;
	const label = (serviceStep.label || "").toLowerCase();
	return label.includes("restarted");
}

function pipelineUpdateTerminal(name, serverStatus) {
	if (!serverStatus) return false;
	const step = serverStatus.step || "";
	if (step === "done" || step === "failed") return true;
	if (pipelineInfo(name).is_sys_monitor) {
		return sysMonitorUpdateComplete(name, serverStatus);
	}
	const service = pipelineInfo(name).service_name || "";
	const serviceStep = pipelineServiceStep(serverStatus, service);
	if (!serviceStep || typeof serviceStep === "string") return false;
	return serviceStep.step === "done" || serviceStep.step === "failed";
}

function pipelineUpdateEntry(name, serverStatus) {
	const base = commandJobBase(name);
	const staticInfo = pipelineInfo(name);
	if (!serverStatus) {
		const waiting = Boolean(activeUpdateTargets);
		return {
			...base,
			status: "pending",
			phase: waiting ? "active" : "queued",
			started_at: waiting ? Date.now() / 1000 : null,
			response: waiting ? "Connecting…" : "",
		};
	}

	const step = serverStatus.step || "";
	const service = staticInfo.service_name || "";
	const serviceStep = pipelineServiceStep(serverStatus, service);
	const { step: serviceStepState, label: serviceLabel } = parseServiceStep(serviceStep);

	if (serviceStepState === "failed") {
		return finalizeUpdateEntry(base, {
			ok: false,
			response: serviceLabel,
			error: serviceLabel || "Update failed",
		});
	}

	if (serviceStepState === "done" && step !== "done" && step !== "failed") {
		const earlyDone = !staticInfo.is_sys_monitor || sysMonitorUpdateComplete(name, serverStatus);
		if (earlyDone) {
			return finalizeUpdateEntry(base, { ok: true, response: serviceLabel });
		}
	}

	if (step === "done") {
		return finalizeUpdateEntry(base, {
			ok: true,
			response: serviceLabel || serverStatus.step_label || "Update complete",
		});
	}

	if (step === "failed") {
		if (serviceStepState === "done") {
			return finalizeUpdateEntry(base, { ok: true, response: serviceLabel });
		}
		const err = serverStatus.error || serverStatus.step_label || "Update failed";
		const serviceErr = hostUpdateErrorForService(err, service, name)
			|| (serviceStepState === "failed" ? (serviceLabel || "Update failed") : null);
		if (!serviceErr) {
			return finalizeUpdateEntry(base, {
				ok: true,
				response: serviceLabel || "Update complete",
			});
		}
		return finalizeUpdateEntry(base, {
			ok: false,
			response: serviceErr,
			error: serviceErr,
		});
	}

	const elapsed = Math.max(0, Number(serverStatus.elapsed_sec) || 0);
	return {
		...base,
		status: "pending",
		phase: serverStatus.running || step ? "active" : "queued",
		started_at: Date.now() / 1000 - elapsed,
		response: pipelineUpdateResponse(name, serverStatus),
	};
}

function buildUpdateByPipeline(targets, servers, postErrors) {
	const merged = mergeUpdateServers(servers);
	const byPipeline = {};
	for (const name of targets) {
		const ip = pipelineInfo(name).server_ip || "";
		if (postErrors && postErrors[ip]) {
			byPipeline[name] = pipelineUpdateEntry(name, {
				step: "failed",
				error: postErrors[ip],
			});
		} else {
			byPipeline[name] = pipelineUpdateEntry(name, merged[ip] || null);
		}
	}
	return byPipeline;
}

function isUpdateModalComplete(data) {
	if (!activeUpdateTargets || !activeUpdateTargets.length) return false;
	const servers = mergeUpdateServers((data && data.servers) || {});
	if (Object.keys(servers).length) {
		updateSeenActive = true;
	}
	if (!updateSeenActive) return false;

	const byPipeline = buildUpdateByPipeline(activeUpdateTargets, servers);
	const allTargetsDone = activeUpdateTargets.every((name) => {
		const entry = byPipeline[name];
		if (!entry || entry.status !== "done") return false;
		if (pipelineInfo(name).is_sys_monitor) {
			const ip = pipelineInfo(name).server_ip || "";
			return sysMonitorUpdateComplete(name, servers[ip]);
		}
		return true;
	});
	if (allTargetsDone) return true;

	const ips = uniqueHostIps(activeUpdateTargets, true);
	if (!ips.length) return true;
	for (const ip of ips) {
		const status = servers[ip];
		if (!status) return false;
		if (status.step === "done" || status.step === "failed") continue;
		const hostTargets = activeUpdateTargets.filter(
			(name) => (pipelineInfo(name).server_ip || "") === ip,
		);
		if (!hostTargets.every((name) => pipelineUpdateTerminal(name, status))) {
			return false;
		}
	}
	return true;
}

function renderUpdateModalProgress(data, nowMs) {
	if (!activeUpdateTargets || !activeUpdateTargets.length) return;
	const servers = mergeUpdateServers(data.servers || {});
	const byPipeline = buildUpdateByPipeline(activeUpdateTargets, servers);
	const results = activeUpdateTargets.map((name) => byPipeline[name]);
	const allDone = isUpdateModalComplete(data);
	renderCommandJobProgress(
		"update",
		{ done: allDone, results },
		activeUpdateTargets,
		nowMs || Date.now(),
		servers,
	);
}

function notifyUpdateFailures(servers) {
	if (updateFailureNotified || activeUpdateTargets) return;
	const failed = Object.values(servers || {}).filter((status) => status.step === "failed" && status.error);
	if (!failed.length) return;
	updateFailureNotified = true;
	const lines = failed.map((status) => `${status.server_ip || "unknown"}: ${status.error}`);
	showAlertModal(`Update failed:\n${lines.join("\n")}`, { title: "Update", tone: "err" });
}

function fetchUpdateStatus() {
	fetchJsonGet(urls.getUpdateStatus + "?_=" + Date.now(), (data) => {
		lastUpdateStatusData = data;
		if (activeUpdateTargets) {
			renderUpdateModalProgress(data);
		}
		if (activeUpdateTargets && isUpdateModalComplete(data)) {
			stopUpdatePolling();
			setCommandButtonsDisabled(false);
			startFreshCollect();
			return;
		}
		if (!activeUpdateTargets) {
			notifyUpdateFailures(data.servers);
		}
	});
}

function schedulePoll() {
	if (document.hidden) return;
	clearPollTimer(() => pollTimer, (value) => { pollTimer = value; });
	pollTimer = setInterval(function () {
		fetchStatus(false);
	}, POLL_MS);
}

function scheduleFastPoll() {
	if (document.hidden) return;
	if (fastPollTimer) return;
	fastPollTimer = setInterval(function () {
		fetchStatus(false);
	}, JOB_POLL_MS);
}

function stopFastPoll() {
	clearPollTimer(() => fastPollTimer, (value) => { fastPollTimer = value; });
}

function statusBadge(status) {
	if (status === "PENDING") {
		return `<span class="status-badge status-loading">loading</span>`;
	}
	const value = status || "ERR";
	return `<span class="status-badge status-${value.toLowerCase()}">${value}</span>`;
}

// --- Status rendering ---

function pipelineTypeTag(name) {
	const { kind, label } = pipelineKindMeta(name);
	return `<span class="type-tag type-${escapeAttr(kind)}">${escapeAttr(label)}</span>`;
}

function deviceHost(link) {
	if (!link || !link.url) return link?.label || "";
	try {
		const u = new URL(link.url);
		const port = u.port || (u.protocol === "https:" ? "443" : "80");
		if (port !== "80" && port !== "443") {
			return `${u.hostname}:${port}`;
		}
		return u.hostname;
	} catch (e) {
		return link.label || "";
	}
}

function deviceOpenUrl(link, host) {
	if (link && link.url) return link.url;
	if (host) return `http://${host}`;
	return "";
}

function pingDevice(host, event, openUrl) {
	if (event) {
		event.preventDefault();
		event.stopPropagation();
	}
	const key = host;
	if (pingBusy.has(key)) return;
	pingBusy.add(key);
	const btn = event && event.currentTarget;
	const targetUrl = openUrl || (btn && btn.dataset.url) || deviceOpenUrl(null, host);
	if (btn) btn.classList.add("is-pinging");
	showPingModal(host, "pending", "Sending ICMP echo request…");

	postJson(urls.pingDevice, { host }, 20000, {
		onload: function (status, data) {
			pingBusy.delete(key);
			if (btn) btn.classList.remove("is-pinging");
			if (status !== 200) {
				showPingModal(host, "err", data.error || "Ping request failed");
				return;
			}
			if (data.ok) {
				const detail = data.detail
					|| (data.rtt_ms != null ? `Round-trip time: ${data.rtt_ms} ms` : "Host is reachable");
				showPingModal(host, "ok", detail, targetUrl);
			} else {
				showPingModal(host, "err", data.detail || data.error || "Host unreachable");
			}
		},
		onerror: function () {
			pingBusy.delete(key);
			if (btn) btn.classList.remove("is-pinging");
			showPingModal(host, "err", "Ping request failed");
		},
		ontimeout: function () {
			pingBusy.delete(key);
			if (btn) btn.classList.remove("is-pinging");
			showPingModal(host, "err", "Ping timed out");
		},
	});
}

function closePingModal() {
	setOverlayVisible($("ping_modal"), false);
}

function showPingModal(host, state, detail, openUrl) {
	const badge = $("ping_modal_badge");
	const openBtn = $("ping_modal_open");
	setOverlayVisible($("ping_modal"), true);
	$("ping_modal_host").textContent = host;
	badge.className = `ping-badge ping-badge--${state}`;
	badge.textContent = state === "pending" ? "Testing…" : (state === "ok" ? "OK" : "Failed");
	$("ping_modal_detail").textContent = detail || "";
	if (state === "ok" && openUrl) {
		openBtn.classList.remove("hidden");
		openBtn.dataset.url = openUrl;
	} else {
		openBtn.classList.add("hidden");
		delete openBtn.dataset.url;
	}
}

function setCameraLiveImageVisible(visible) {
	$("camera_live_img").classList.toggle("hidden", !visible);
}

function clearCameraLiveLamps() {
	const el = $("camera_live_lamps");
	if (!el) return;
	el.innerHTML = "";
	el.hidden = true;
}

const LAMP_WARN_COLORS = new Set(["YELLOW", "ORANGE", "AMBER"]);

function lampChipTone(lamp) {
	if (!lamp || !lamp.on) return "idle";
	const color = String(lamp.color || "").toUpperCase();
	if (color === "RED") return "err";
	if (LAMP_WARN_COLORS.has(color)) return "warn";
	return "ok";
}

function lampChipLabel(lamp) {
	const name = String(lamp?.name || "").trim();
	if (name) return name;
	const state = String(lamp?.state || "").trim();
	const color = String(lamp?.color || "").trim().toUpperCase();
	if (/^OFF$/i.test(state) && color) return `${color} OFF`;
	if (state) return state;
	if (color) return lamp?.on ? `${color} ON` : `${color} OFF`;
	return lamp?.on ? "ON" : "OFF";
}

function renderCameraLiveLamps(lamps) {
	const el = $("camera_live_lamps");
	if (!el) return;
	if (!Array.isArray(lamps) || !lamps.length) {
		clearCameraLiveLamps();
		return;
	}
	el.innerHTML = lamps.map((lamp) => {
		const label = lampChipLabel(lamp);
		const state = String(lamp?.state || "").trim();
		const title = state && label !== state ? `${label}: ${state}` : label;
		return (
			`<span class="device-chip device-chip--tag chip-${lampChipTone(lamp)}" `
			+ `title="${escapeAttr(title)}">${escapeHtml(label)}</span>`
		);
	}).join("");
	el.hidden = false;
}

function stopCameraLiveStream() {
	if (cameraLiveSource) {
		cameraLiveSource.close();
		cameraLiveSource = null;
	}
	const img = $("camera_live_img");
	img.removeAttribute("src");
	setCameraLiveImageVisible(false);
	clearCameraLiveLamps();
}

function closeCameraLive() {
	stopCameraLiveStream();
	setOverlayVisible($("camera_live_modal"), false);
}

function closePlcTagModal() {
	setOverlayVisible($("plc_tag_modal"), false);
}

let driftImagesState = null;

function closeDriftImagesModal() {
	driftImagesState = null;
	setDriftAnchorEditMode(false);
	updateDriftAnchorBar();
	const beforeImg = $("drift_images_before");
	const afterImg = $("drift_images_after");
	const overlayImg = $("drift_images_overlay");
	if (beforeImg) {
		beforeImg.onload = null;
		beforeImg.onerror = null;
		beforeImg.removeAttribute("src");
		beforeImg.dataset.driftUrl = "";
	}
	if (afterImg) {
		afterImg.onload = null;
		afterImg.onerror = null;
		afterImg.removeAttribute("src");
		afterImg.dataset.driftUrl = "";
	}
	if (overlayImg) overlayImg.removeAttribute("src");
	clearDriftShiftArrow();
	renderDriftHint(null);
	renderDriftOverlay(null);
	renderDriftRawMetrics(null);
	setDriftResetVisible(false);
	setOverlayVisible($("drift_images_modal"), false);
}

const DRIFT_I18N = {
	en: {
		overlayCaption: "Explanation",
		before: "Before",
		after: "After",
		beforeEmpty: "No before image",
		afterEmpty: "No after image",
		threshold: "threshold",
		matches: "match points",
		fpHigh: "High false-positive risk",
		fpMed: "Possible false positive",
		fpNote: "Note",
		loading: "Loading…",
		unavailable: "Camera drift images unavailable",
		notFound: "No drift images found",
		noReference: "No reference image",
		failed: "Failed to load drift images",
		timeout: "Timed out loading drift images",
		title: "Drift Images",
		reset: "DRIFT Reset",
		pinpointAdd: "Add pinpoint",
		pinpointUndo: "Undo last",
		pinpointClearAll: "Clear all",
		pinpointHelp: "Click a fixed spot on Before.",
		pinpointCount: (total, manual, active) => (
			`Pinpoints ${total} (manual ${manual}, active ${active})`
		),
		pinpointAddFailed: "Failed to add pinpoint",
		pinpointAddTimeout: "Timed out adding pinpoint",
		pinpointUndoFailed: "Failed to undo last pinpoint",
		pinpointUndoTimeout: "Timed out undoing last pinpoint",
		pinpointClearFailed: "Failed to clear manual pinpoints",
		pinpointClearTimeout: "Timed out clearing manual pinpoints",
		referenceOnly: "Camera OK · reference image (pinpoint edit)",
	},
	ko: {
		fpHigh: "오탐 가능성 높음",
		fpMed: "오탐 가능성 있음",
		fpNote: "참고",
	},
};

function uiLang() {
	const raw = String(
		(typeof navigator !== "undefined" && (navigator.language || navigator.userLanguage)) || "en",
	).toLowerCase();
	return raw.startsWith("ko") ? "ko" : "en";
}

function driftT(key) {
	// Chrome labels stay English; hint banner follows browser language.
	return DRIFT_I18N.en[key] || key;
}

function driftHintT(key) {
	const lang = uiLang();
	return (DRIFT_I18N[lang] && DRIFT_I18N[lang][key])
		|| DRIFT_I18N.en[key]
		|| key;
}

function applyDriftImagesStaticI18n() {
	const overlayCap = $("drift_images_overlay_caption");
	if (overlayCap) overlayCap.textContent = driftT("overlayCaption");
	const beforeEmpty = $("drift_images_before_empty");
	if (beforeEmpty) beforeEmpty.textContent = driftT("beforeEmpty");
	const afterEmpty = $("drift_images_after_empty");
	if (afterEmpty) afterEmpty.textContent = driftT("afterEmpty");
	const beforeImg = $("drift_images_before");
	if (beforeImg) beforeImg.alt = driftT("before");
	const afterImg = $("drift_images_after");
	if (afterImg) afterImg.alt = driftT("after");
	const pinpointToggle = $("drift_anchor_toggle");
	if (pinpointToggle) pinpointToggle.textContent = driftT("pinpointAdd");
	const pinpointUndo = $("drift_anchor_clear");
	if (pinpointUndo) pinpointUndo.textContent = driftT("pinpointUndo");
	const pinpointClearAll = $("drift_anchor_clear_all");
	if (pinpointClearAll) pinpointClearAll.textContent = driftT("pinpointClearAll");
	const pinpointHelp = $("drift_anchor_help");
	if (pinpointHelp) pinpointHelp.textContent = driftT("pinpointHelp");
}

function driftImagesProxyUrl(side, camUid, pipeline, cacheBust) {
	const base = urls.cameraDriftImages;
	if (!base) return "";
	const params = new URLSearchParams({
		pipeline: pipeline || "",
		cam_uid: camUid || "",
		lang: uiLang(),
	});
	if (cacheBust != null && cacheBust !== "") {
		params.set("_", String(cacheBust));
	}
	return `${base}/${encodeURIComponent(side)}?${params.toString()}`;
}

function setDriftSideImage(side, url) {
	const img = $(`drift_images_${side}`);
	const empty = $(`drift_images_${side}_empty`);
	if (!img || !empty) return;
	if (!url) {
		img.onload = null;
		img.onerror = null;
		img.removeAttribute("src");
		img.dataset.driftUrl = "";
		img.classList.add("hidden");
		empty.classList.remove("hidden");
		return;
	}
	// Skip restarting a download already in flight / completed for the same URL.
	if (img.dataset.driftUrl === url && (img.complete || img.getAttribute("src"))) {
		empty.classList.add("hidden");
		img.classList.remove("hidden");
		return;
	}
	empty.classList.add("hidden");
	img.classList.remove("hidden");
	img.dataset.driftUrl = url;
	img.src = url;
}

function clearDriftPairImages() {
	setDriftSideImage("before", "");
	setDriftSideImage("after", "");
	const overlayImg = $("drift_images_overlay");
	if (overlayImg) overlayImg.removeAttribute("src");
	const grid = $("drift_images_grid");
	if (grid) {
		grid.hidden = true;
		grid.classList.remove("is-reference-only");
	}
	clearDriftShiftArrow();
	renderDriftOverlay(null);
}

function isDriftCameraTag(tag) {
	const value = String((tag && tag.value) || "").trim().toLowerCase();
	return value === "drift" || (tag && tag.health === "err" && value !== "ok");
}

function isOfflineCameraTag(tag) {
	const value = String((tag && tag.value) || "").trim().toLowerCase();
	return value === "off" || value === "offline";
}

function isDriftImagesCompareMode() {
	return Boolean(driftImagesState && driftImagesState.isDrifted);
}

function setDriftImagesReferenceOnly(referenceOnly) {
	const grid = $("drift_images_grid");
	if (grid) grid.classList.toggle("is-reference-only", Boolean(referenceOnly));
}

function resolveDriftCameraIsDrifted(camUid, explicit) {
	if (explicit === true || explicit === false) return explicit;
	if (explicit != null && explicit !== "") {
		const v = String(explicit).trim().toLowerCase();
		if (v === "1" || v === "true" || v === "drift") return true;
		if (v === "0" || v === "false" || v === "ok") return false;
	}
	const uid = String(camUid || "").trim();
	const tags = plcTagModalState && plcTagModalState.tags;
	if (uid && Array.isArray(tags)) {
		const tag = tags.find((t) => String(t.id || "").trim() === uid);
		if (tag) return isDriftCameraTag(tag);
	}
	// Unknown entry point: keep compare view.
	return true;
}

function prefetchDriftPairImages(camUid, pipeline, cacheBust, compareMode) {
	const beforeUrl = driftImagesProxyUrl("before", camUid, pipeline, cacheBust);
	setDriftPanelCaption("before", null, driftT("before"));
	const grid = $("drift_images_grid");
	if (grid) grid.hidden = false;
	setDriftImagesReferenceOnly(!compareMode);
	setDriftSideImage("before", beforeUrl);
	if (compareMode) {
		const afterUrl = driftImagesProxyUrl("after", camUid, pipeline, cacheBust);
		setDriftPanelCaption("after", null, driftT("after"));
		setDriftSideImage("after", afterUrl);
	} else {
		setDriftSideImage("after", "");
		setDriftPanelCaption("after", null, driftT("after"));
	}
}

/** Browser local TZ label: KST / UTC / UTC±N */
function browserTzLabel() {
	const offsetMin = -new Date().getTimezoneOffset();
	if (offsetMin === 9 * 60) return "KST";
	if (offsetMin === 0) return "UTC";
	const sign = offsetMin >= 0 ? "+" : "-";
	const abs = Math.abs(offsetMin);
	const h = Math.floor(abs / 60);
	const m = abs % 60;
	return m ? `UTC${sign}${h}:${String(m).padStart(2, "0")}` : `UTC${sign}${h}`;
}

function pad2(v) {
	return String(v).padStart(2, "0");
}

/** Format a Date in the browser local timezone with a UTC/KST label. */
function formatDateTimeLocal(date, { withSeconds = false } = {}) {
	if (!(date instanceof Date) || Number.isNaN(date.getTime())) return "";
	const datePart = [
		date.getFullYear(),
		pad2(date.getMonth() + 1),
		pad2(date.getDate()),
	].join("-");
	let timePart = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
	if (withSeconds) timePart += `:${pad2(date.getSeconds())}`;
	return `${datePart} ${timePart} (${browserTzLabel()})`;
}

function formatEpochSecondsLocal(sec, opts) {
	const n = Number(sec);
	if (!Number.isFinite(n) || n <= 0) return "";
	return formatDateTimeLocal(new Date(n * 1000), opts);
}

function formatEpochMsLocal(ms, opts) {
	const n = Number(ms);
	if (!Number.isFinite(n) || n <= 0) return "";
	return formatDateTimeLocal(new Date(n), opts);
}

function formatDriftBatchLabel(batchName) {
	const raw = String(batchName || "").trim();
	if (!raw) return "";
	// Edge batch names are UTC wall clock: 2026-07-21__17-48-58
	const match = raw.match(/^(\d{4}-\d{2}-\d{2})__(\d{2})-(\d{2})(?:-(\d{2}))?/);
	if (match) {
		const sec = match[4] || "00";
		const d = new Date(`${match[1]}T${match[2]}:${match[3]}:${sec}Z`);
		if (!Number.isNaN(d.getTime())) return formatDateTimeLocal(d);
	}
	return raw.replace(/__/g, " ");
}

function setDriftPanelCaption(side, sideInfo, fallbackLabel) {
	const el = $(`drift_images_${side}_caption`);
	if (!el) return;
	const when = formatDriftBatchLabel(sideInfo && sideInfo.batch_name);
	el.textContent = when ? `${fallbackLabel}  ·  ${when}` : fallbackLabel;
}

function formatDriftMetricValue(value) {
	if (value == null || value === "") return "—";
	const n = Number(value);
	if (!Number.isFinite(n)) return String(value);
	return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function splitDriftHintSentences(text) {
	return String(text || "")
		.split(/(?<=[.。!?])\s+/)
		.map((part) => part.trim())
		.filter(Boolean);
}

function highlightDriftHintSentence(text) {
	return escapeHtml(text).replace(
		/(\d+(?:\.\d+)?\s*px)/gi,
		"<strong>$1</strong>",
	);
}

function renderDriftHint(meta) {
	const box = $("drift_images_hint");
	const titleEl = $("drift_images_hint_title");
	const listEl = $("drift_images_hint_list");
	if (!box || !titleEl || !listEl) return;
	const hint = meta && meta.false_positive_hint;
	if (!hint) {
		box.hidden = true;
		titleEl.textContent = "";
		listEl.innerHTML = "";
		box.classList.remove("drift-images-hint--risk", "drift-images-hint--ok");
		return;
	}
	const risk = Boolean(meta.false_positive_risk);
	const conf = String(meta.confidence || "").toLowerCase();
	const label = risk
		? (conf === "low" ? driftHintT("fpHigh") : driftHintT("fpMed"))
		: driftHintT("fpNote");
	const sentences = splitDriftHintSentences(hint);
	box.classList.toggle("drift-images-hint--risk", risk);
	box.classList.toggle("drift-images-hint--ok", !risk);
	titleEl.textContent = label;
	listEl.innerHTML = sentences
		.map((sentence) => `<li>${highlightDriftHintSentence(sentence)}</li>`)
		.join("");
	box.hidden = false;
}

let driftArrowResizeObserver = null;

function clearDriftShiftArrow() {
	["drift_images_before_points", "drift_images_after_arrow"].forEach((id) => {
		const canvas = $(id);
		if (!canvas) return;
		const ctx = canvas.getContext && canvas.getContext("2d");
		if (ctx) ctx.clearRect(0, 0, canvas.width || 0, canvas.height || 0);
		canvas.classList.add("is-hidden");
	});
	if (driftArrowResizeObserver) {
		driftArrowResizeObserver.disconnect();
		driftArrowResizeObserver = null;
	}
}

function scheduleDriftShiftArrow(meta, attempt = 0) {
	requestAnimationFrame(() => {
		requestAnimationFrame(() => {
			const ok = drawDriftMatchOverlays(meta);
			if (!ok && attempt < 8) {
				setTimeout(() => scheduleDriftShiftArrow(meta, attempt + 1), 50 * (attempt + 1));
			}
		});
	});
}

function prepareDriftOverlayCanvas(img, canvas) {
	if (!img || !canvas || img.classList.contains("hidden")) return null;
	if (!img.complete || !img.naturalWidth || !img.naturalHeight) return null;
	const media = img.closest ? img.closest(".drift-images-media") : null;
	const mediaRect = (media || img).getBoundingClientRect();
	const imgRect = img.getBoundingClientRect();
	const w = Math.max(1, Math.round(imgRect.width));
	const h = Math.max(1, Math.round(imgRect.height));
	if (w < 8 || h < 8) return null;
	const left = Math.round(imgRect.left - mediaRect.left);
	const top = Math.round(imgRect.top - mediaRect.top);
	canvas.style.left = `${left}px`;
	canvas.style.top = `${top}px`;
	canvas.style.width = `${w}px`;
	canvas.style.height = `${h}px`;
	const dpr = window.devicePixelRatio || 1;
	canvas.width = Math.round(w * dpr);
	canvas.height = Math.round(h * dpr);
	const ctx = canvas.getContext("2d");
	if (!ctx) return null;
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
	ctx.clearRect(0, 0, w, h);
	const nw = img.naturalWidth;
	const nh = img.naturalHeight;
	const scale = Math.min(w / nw, h / nh);
	const drawW = nw * scale;
	const drawH = nh * scale;
	const ox = (w - drawW) / 2;
	const oy = (h - drawH) / 2;
	return { ctx, w, h, scale, ox, oy, drawW, drawH, media };
}

function mapDriftPoint(layout, x, y) {
	return {
		x: layout.ox + Number(x) * layout.scale,
		y: layout.oy + Number(y) * layout.scale,
	};
}

function driftImageCoordsFromEvent(img, clientX, clientY) {
	if (!img || !img.naturalWidth || !img.naturalHeight) return null;
	const rect = img.getBoundingClientRect();
	const w = rect.width;
	const h = rect.height;
	if (w < 8 || h < 8) return null;
	const scale = Math.min(w / img.naturalWidth, h / img.naturalHeight);
	const drawW = img.naturalWidth * scale;
	const drawH = img.naturalHeight * scale;
	const ox = (w - drawW) / 2;
	const oy = (h - drawH) / 2;
	const x = clientX - rect.left;
	const y = clientY - rect.top;
	if (x < ox || y < oy || x > ox + drawW || y > oy + drawH) return null;
	return {
		x: (x - ox) / scale,
		y: (y - oy) / scale,
	};
}

function setDriftAnchorEditMode(enabled) {
	const on = Boolean(enabled);
	if (driftImagesState) driftImagesState.anchorEdit = on;
	const toggle = $("drift_anchor_toggle");
	const help = $("drift_anchor_help");
	const beforeMedia = $("drift_images_before")
		&& $("drift_images_before").closest(".drift-images-media");
	if (toggle) toggle.setAttribute("aria-pressed", on ? "true" : "false");
	if (help) help.hidden = !on;
	if (beforeMedia) beforeMedia.classList.toggle("is-anchor-edit", on);
	updateDriftAnchorBar();
}

function manualPersistentPoints(points) {
	return (points || []).filter((p) => String(p?.source || "") === "manual");
}

function updateDriftAnchorBar() {
	const countEl = $("drift_anchor_count");
	const undoBtn = $("drift_anchor_clear");
	const clearAllBtn = $("drift_anchor_clear_all");
	const points = (driftImagesState && driftImagesState.persistentPoints) || [];
	const manual = manualPersistentPoints(points).length;
	const active = points.filter((p) => p.active).length;
	const editing = Boolean(driftImagesState && driftImagesState.anchorEdit);
	const showManualActions = editing && manual > 0;
	if (countEl) {
		const format = DRIFT_I18N.en.pinpointCount;
		countEl.textContent = points.length
			? format(points.length, manual, active)
			: "";
	}
	if (undoBtn) {
		// Beside Add pinpoint while edit mode is on (and there is something to undo).
		undoBtn.hidden = !showManualActions;
		undoBtn.disabled = false;
	}
	if (clearAllBtn) {
		clearAllBtn.hidden = !showManualActions;
		clearAllBtn.disabled = false;
	}
}

function applyPersistentPointsResponse(data, loadId) {
	if (!driftImagesState || driftImagesState.loadId !== loadId) return false;
	if (!data || data.ok === false || data.success === false) return false;
	driftImagesState.persistentPoints = Array.isArray(data.points) ? data.points : [];
	updateDriftAnchorBar();
	if (driftImagesState.meta) scheduleDriftShiftArrow(driftImagesState.meta);
	return true;
}

function drawPersistentAnchors(beforeLayout, beforeCanvas) {
	const anchors = (driftImagesState && driftImagesState.persistentPoints) || [];
	if (!beforeLayout || !beforeCanvas || !anchors.length) return;
	for (const p of anchors) {
		const x = Number(p.x);
		const y = Number(p.y);
		if (![x, y].every(Number.isFinite)) continue;
		const pt = mapDriftPoint(beforeLayout, x, y);
		const manual = String(p.source || "") === "manual";
		beforeLayout.ctx.beginPath();
		beforeLayout.ctx.arc(pt.x, pt.y, manual ? 5.5 : 4.2, 0, Math.PI * 2);
		beforeLayout.ctx.fillStyle = manual
			? "rgba(250, 204, 21, 0.95)"
			: "rgba(52, 211, 153, 0.9)";
		beforeLayout.ctx.fill();
		beforeLayout.ctx.lineWidth = 1.5;
		beforeLayout.ctx.strokeStyle = "rgba(0, 0, 0, 0.55)";
		beforeLayout.ctx.stroke();
	}
	beforeCanvas.classList.remove("is-hidden");
}

function loadDriftPersistentPoints() {
	if (!driftImagesState || !urls.cameraDriftPersistentPoints) {
		updateDriftAnchorBar();
		return;
	}
	const { camUid, pipeline, loadId } = driftImagesState;
	const params = new URLSearchParams({
		pipeline: pipeline || "",
		cam_uid: camUid,
	});
	fetchJsonGet(`${urls.cameraDriftPersistentPoints}?${params.toString()}`, (data) => {
		if (!driftImagesState || driftImagesState.loadId !== loadId) return;
		if (!data || data.ok === false || data.success === false) {
			driftImagesState.persistentPoints = [];
			updateDriftAnchorBar();
			return;
		}
		driftImagesState.persistentPoints = Array.isArray(data.points) ? data.points : [];
		updateDriftAnchorBar();
		if (driftImagesState.meta) scheduleDriftShiftArrow(driftImagesState.meta);
	}, {
		timeout: 15000,
		onerror() {
			if (!driftImagesState || driftImagesState.loadId !== loadId) return;
			driftImagesState.persistentPoints = [];
			updateDriftAnchorBar();
		},
	});
}

function addDriftManualAnchor(x, y) {
	if (!driftImagesState || !urls.cameraDriftPersistentPoints) return;
	const { camUid, pipeline, loadId } = driftImagesState;
	postJson(urls.cameraDriftPersistentPoints, {
		pipeline: pipeline || "",
		cam_uid: camUid,
		points: [{ x, y }],
	}, 15000, {
		onload(_status, data) {
			if (!applyPersistentPointsResponse(data, loadId)) {
				showAlertModal(
					(data && (data.error || data.message)) || driftT("pinpointAddFailed"),
				);
			}
		},
		onerror() {
			showAlertModal(driftT("pinpointAddFailed"));
		},
		ontimeout() {
			showAlertModal(driftT("pinpointAddTimeout"));
		},
	});
}

function restoreManualPinpoints(keepPoints, loadId, camUid, pipeline) {
	if (!keepPoints.length) {
		updateDriftAnchorBar();
		if (driftImagesState && driftImagesState.meta) {
			scheduleDriftShiftArrow(driftImagesState.meta);
		}
		return;
	}
	postJson(urls.cameraDriftPersistentPoints, {
		pipeline: pipeline || "",
		cam_uid: camUid,
		points: keepPoints.map((p) => ({ x: Number(p.x), y: Number(p.y) })),
	}, 15000, {
		onload(_status, data) {
			if (!applyPersistentPointsResponse(data, loadId)) {
				showAlertModal(
					(data && (data.error || data.message)) || driftT("pinpointUndoFailed"),
				);
			}
		},
		onerror() {
			showAlertModal(driftT("pinpointUndoFailed"));
		},
		ontimeout() {
			showAlertModal(driftT("pinpointUndoTimeout"));
		},
	});
}

function clearAllManualPinpoints() {
	if (!driftImagesState || !urls.cameraDriftPersistentPoints) return;
	const { camUid, pipeline, loadId, persistentPoints } = driftImagesState;
	if (!manualPersistentPoints(persistentPoints).length) return;
	postJson(urls.cameraDriftPersistentPoints, {
		pipeline: pipeline || "",
		cam_uid: camUid,
		clear_manual: true,
	}, 15000, {
		onload(_status, data) {
			if (!applyPersistentPointsResponse(data, loadId)) {
				showAlertModal(
					(data && (data.error || data.message)) || driftT("pinpointClearFailed"),
				);
			}
		},
		onerror() {
			showAlertModal(driftT("pinpointClearFailed"));
		},
		ontimeout() {
			showAlertModal(driftT("pinpointClearTimeout"));
		},
	});
}

function undoLastManualPinpoint() {
	if (!driftImagesState || !urls.cameraDriftPersistentPoints) return;
	const { camUid, pipeline, loadId, persistentPoints } = driftImagesState;
	const manuals = manualPersistentPoints(persistentPoints);
	if (!manuals.length) return;
	const keep = manuals.slice(0, -1);

	postJson(urls.cameraDriftPersistentPoints, {
		pipeline: pipeline || "",
		cam_uid: camUid,
		remove_last_manual: true,
	}, 15000, {
		onload(_status, data) {
			if (applyPersistentPointsResponse(data, loadId)) return;
			// Fallback when edge only supports clear_manual: clear all, then re-add keep.
			postJson(urls.cameraDriftPersistentPoints, {
				pipeline: pipeline || "",
				cam_uid: camUid,
				clear_manual: true,
			}, 15000, {
				onload(_clearStatus, clearData) {
					if (!driftImagesState || driftImagesState.loadId !== loadId) return;
					if (!clearData || clearData.ok === false || clearData.success === false) {
						showAlertModal(
							(clearData && (clearData.error || clearData.message))
							|| (data && (data.error || data.message))
							|| driftT("pinpointUndoFailed"),
						);
						return;
					}
					driftImagesState.persistentPoints = Array.isArray(clearData.points)
						? clearData.points
						: [];
					restoreManualPinpoints(keep, loadId, camUid, pipeline);
				},
				onerror() {
					showAlertModal(driftT("pinpointUndoFailed"));
				},
				ontimeout() {
					showAlertModal(driftT("pinpointUndoTimeout"));
				},
			});
		},
		onerror() {
			showAlertModal(driftT("pinpointUndoFailed"));
		},
		ontimeout() {
			showAlertModal(driftT("pinpointUndoTimeout"));
		},
	});
}

function drawDriftMatchOverlays(meta) {
	const beforeImg = $("drift_images_before");
	const afterImg = $("drift_images_after");
	const beforeCanvas = $("drift_images_before_points");
	const afterCanvas = $("drift_images_after_arrow");
	const compare = isDriftImagesCompareMode();

	if (!meta) {
		clearDriftShiftArrow();
		return false;
	}

	// OK cameras: show pinpoints on Before only; skip stale After/match overlays.
	if (!compare) {
		if (afterCanvas) {
			const ctx = afterCanvas.getContext && afterCanvas.getContext("2d");
			if (ctx) ctx.clearRect(0, 0, afterCanvas.width || 0, afterCanvas.height || 0);
			afterCanvas.classList.add("is-hidden");
		}
		const beforeLayout = (beforeImg && beforeCanvas && !beforeImg.classList.contains("hidden"))
			? prepareDriftOverlayCanvas(beforeImg, beforeCanvas)
			: null;
		if (!beforeLayout) return false;
		drawPersistentAnchors(beforeLayout, beforeCanvas);
		const observeTarget = beforeLayout.media || beforeImg;
		if (!driftArrowResizeObserver && typeof ResizeObserver !== "undefined" && observeTarget) {
			driftArrowResizeObserver = new ResizeObserver(() => {
				if (!driftImagesState || !driftImagesState.meta) return;
				drawDriftMatchOverlays(driftImagesState.meta);
			});
			driftArrowResizeObserver.observe(observeTarget);
		}
		return true;
	}

	if (!afterImg || !afterCanvas) {
		clearDriftShiftArrow();
		return false;
	}

	const points = Array.isArray(meta.match_points) ? meta.match_points : [];
	const afterLayout = prepareDriftOverlayCanvas(afterImg, afterCanvas);
	if (!afterLayout) return false;

	const beforeLayout = (beforeImg && beforeCanvas && !beforeImg.classList.contains("hidden"))
		? prepareDriftOverlayCanvas(beforeImg, beforeCanvas)
		: null;

	const dx = Number(meta.delta_x != null ? meta.delta_x : meta.delta_x_display);
	const dy = Number(meta.delta_y != null ? meta.delta_y : meta.delta_y_display);

	// Draw automatic match points (Before↔After correspondences).
	if (points.length) {
		for (const p of points) {
			const x0 = Number(p.x0);
			const y0 = Number(p.y0);
			const x1 = Number(p.x1);
			const y1 = Number(p.y1);
			if (![x0, y0, x1, y1].every(Number.isFinite)) continue;

			if (beforeLayout) {
				const a = mapDriftPoint(beforeLayout, x0, y0);
				beforeLayout.ctx.fillStyle = "rgba(80, 220, 255, 0.95)";
				beforeLayout.ctx.beginPath();
				beforeLayout.ctx.arc(a.x, a.y, 3.2, 0, Math.PI * 2);
				beforeLayout.ctx.fill();
			}

			const b = mapDriftPoint(afterLayout, x1, y1);
			const aOnAfter = mapDriftPoint(afterLayout, x0, y0);
			afterLayout.ctx.strokeStyle = "rgba(0, 255, 120, 0.55)";
			afterLayout.ctx.lineWidth = 1.2;
			afterLayout.ctx.beginPath();
			afterLayout.ctx.moveTo(aOnAfter.x, aOnAfter.y);
			afterLayout.ctx.lineTo(b.x, b.y);
			afterLayout.ctx.stroke();
			afterLayout.ctx.fillStyle = "rgba(255, 80, 80, 0.95)";
			afterLayout.ctx.beginPath();
			afterLayout.ctx.arc(b.x, b.y, 3.2, 0, Math.PI * 2);
			afterLayout.ctx.fill();
		}
		if (beforeLayout) {
			beforeCanvas.classList.remove("is-hidden");
		}
	}

	if (beforeLayout) {
		drawPersistentAnchors(beforeLayout, beforeCanvas);
	}

	// Median summary arrow: direction from Δx/Δy; keep subtle vs match overlays.
	if (Number.isFinite(dx) && Number.isFinite(dy) && (Math.abs(dx) > 1e-6 || Math.abs(dy) > 1e-6)) {
		const ctx = afterLayout.ctx;
		const mag = Math.hypot(dx, dy) || 1;
		const ux = dx / mag;
		const uy = dy / mag;
		const maxLen = Math.min(afterLayout.drawW, afterLayout.drawH) * 0.2;
		const len = Math.max(56, Math.min(96, maxLen));
		const headLen = Math.max(12, len * 0.26);
		const headHalf = headLen * 0.48;
		const pad = headLen + 10;

		let cx = afterLayout.ox + afterLayout.drawW / 2;
		let cy = afterLayout.oy + afterLayout.drawH / 2;
		let ex = cx + ux * len;
		let ey = cy + uy * len;

		const minX = afterLayout.ox + pad;
		const maxX = afterLayout.ox + afterLayout.drawW - pad;
		const minY = afterLayout.oy + pad;
		const maxY = afterLayout.oy + afterLayout.drawH - pad;
		if (ex < minX) { cx += minX - ex; ex = minX; }
		if (ex > maxX) { cx -= ex - maxX; ex = maxX; }
		if (ey < minY) { cy += minY - ey; ey = minY; }
		if (ey > maxY) { cy -= ey - maxY; ey = maxY; }

		const angle = Math.atan2(uy, ux);
		const shaftEx = ex - ux * (headLen * 0.8);
		const shaftEy = ey - uy * (headLen * 0.8);
		const leftX = ex - headLen * Math.cos(angle) + headHalf * Math.cos(angle + Math.PI / 2);
		const leftY = ey - headLen * Math.sin(angle) + headHalf * Math.sin(angle + Math.PI / 2);
		const rightX = ex - headLen * Math.cos(angle) + headHalf * Math.cos(angle - Math.PI / 2);
		const rightY = ey - headLen * Math.sin(angle) + headHalf * Math.sin(angle - Math.PI / 2);

		ctx.save();
		ctx.globalAlpha = 0.82;
		ctx.strokeStyle = "rgba(255, 120, 90, 0.9)";
		ctx.fillStyle = "rgba(255, 120, 90, 0.9)";
		ctx.lineWidth = 2.4;
		ctx.lineCap = "round";
		ctx.lineJoin = "round";
		ctx.beginPath();
		ctx.moveTo(cx, cy);
		ctx.lineTo(shaftEx, shaftEy);
		ctx.stroke();
		ctx.beginPath();
		ctx.moveTo(ex, ey);
		ctx.lineTo(leftX, leftY);
		ctx.lineTo(rightX, rightY);
		ctx.closePath();
		ctx.fill();

		ctx.beginPath();
		ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
		ctx.fillStyle = "rgba(255, 220, 120, 0.95)";
		ctx.fill();
		ctx.lineWidth = 1;
		ctx.strokeStyle = "rgba(40, 40, 40, 0.55)";
		ctx.stroke();

		const dist = Number(
			meta.distance_display != null ? meta.distance_display : meta.distance,
		);
		const distTxt = Number.isFinite(dist) ? `${Math.round(dist)}px` : "";
		const label = [
			distTxt,
			`dx ${Math.round(dx)}`,
			`dy ${Math.round(dy)}`,
		].filter(Boolean).join("  ");
		if (label) {
			ctx.globalAlpha = 0.9;
			ctx.font = "600 11px sans-serif";
			ctx.textBaseline = "bottom";
			const tx = Math.min(
				afterLayout.ox + afterLayout.drawW - 8,
				Math.max(afterLayout.ox + 8, cx + 8),
			);
			const ty = Math.max(afterLayout.oy + 14, cy - 8);
			ctx.lineWidth = 2.5;
			ctx.strokeStyle = "rgba(0,0,0,0.45)";
			ctx.strokeText(label, tx, ty);
			ctx.fillStyle = "rgba(255, 230, 170, 0.95)";
			ctx.fillText(label, tx, ty);
		}
		ctx.restore();
	}

	afterCanvas.classList.remove("is-hidden");
	afterCanvas.removeAttribute("hidden");

	const observeTarget = afterLayout.media
		|| (beforeLayout && beforeLayout.media)
		|| afterImg;
	if (!driftArrowResizeObserver && typeof ResizeObserver !== "undefined" && observeTarget) {
		driftArrowResizeObserver = new ResizeObserver(() => {
			if (!driftImagesState || !driftImagesState.meta) return;
			drawDriftMatchOverlays(driftImagesState.meta);
		});
		driftArrowResizeObserver.observe(observeTarget);
	}
	return true;
}

function renderDriftOverlay(meta) {
	const wrap = $("drift_images_overlay_wrap");
	const img = $("drift_images_overlay");
	if (!wrap || !img) return;
	// Prefer the before/after pair; only show baked overlay as fallback.
	const hasPair = Boolean(
		meta && ((meta.before && meta.before.available) || (meta.after && meta.after.available)),
	);
	if (hasPair || !meta || !meta.overlay || !meta.overlay.available) {
		img.removeAttribute("src");
		wrap.hidden = true;
		return;
	}
	const url = driftImagesProxyUrl(
		"overlay",
		driftImagesState && driftImagesState.camUid,
		driftImagesState && driftImagesState.pipeline,
		driftImagesState && driftImagesState.cacheBust,
	);
	if (!url) {
		img.removeAttribute("src");
		wrap.hidden = true;
		return;
	}
	img.src = url;
	wrap.hidden = false;
}

function renderDriftRawMetrics(meta) {
	const box = $("drift_images_metrics");
	if (!box) return;
	if (!meta) {
		box.hidden = true;
		box.innerHTML = "";
		return;
	}
	const dist = formatDriftMetricValue(
		meta.distance_display != null ? meta.distance_display : meta.distance,
	);
	const dx = formatDriftMetricValue(
		meta.delta_x_display != null ? meta.delta_x_display : meta.delta_x,
	);
	const dy = formatDriftMetricValue(
		meta.delta_y_display != null ? meta.delta_y_display : meta.delta_y,
	);
	const thr = formatDriftMetricValue(meta.threshold);
	const matchCount = Number(meta.match_points_count || (meta.match_points && meta.match_points.length) || 0);
	const bits = [
		`<span>distance <strong>${escapeHtml(dist)}px</strong></span>`,
		`<span>${escapeHtml(driftT("threshold"))} <strong>${escapeHtml(thr)}px</strong></span>`,
		`<span>Δx <strong>${escapeHtml(dx)}px</strong></span>`,
		`<span>Δy <strong>${escapeHtml(dy)}px</strong></span>`,
	];
	if (matchCount > 0) {
		bits.push(`<span>${escapeHtml(driftT("matches"))} <strong>${escapeHtml(String(matchCount))}</strong></span>`);
	}
	box.innerHTML = bits.join("");
	box.hidden = false;
}

function setDriftResetVisible(visible) {
	const footer = $("drift_images_footer");
	const btn = $("drift_images_reset");
	if (footer) footer.hidden = !visible;
	if (btn) {
		btn.disabled = false;
		btn.textContent = driftT("reset");
	}
}

function renderDriftImagesPair() {
	if (!driftImagesState || !driftImagesState.meta) return;
	const { pipeline, camUid, meta, cacheBust } = driftImagesState;
	const compare = isDriftImagesCompareMode();
	const before = meta.before;
	const after = meta.after;

	// Stale last-event meta still arrives for OK cameras; hide compare chrome.
	if (compare) {
		renderDriftHint(meta);
		renderDriftRawMetrics(meta);
		renderDriftOverlay(meta);
	} else {
		renderDriftHint(null);
		renderDriftRawMetrics(null);
		renderDriftOverlay(null);
	}
	setDriftPanelCaption("before", before, driftT("before"));
	setDriftPanelCaption("after", compare ? after : null, driftT("after"));
	setDriftResetVisible(Boolean(camUid));
	setDriftImagesReferenceOnly(!compare);

	const beforeImg = $("drift_images_before");
	const afterImg = $("drift_images_after");
	const onOverlayReady = () => {
		if (!driftImagesState || driftImagesState.camUid !== camUid) return;
		scheduleDriftShiftArrow(meta);
	};
	if (beforeImg) beforeImg.onload = onOverlayReady;
	if (afterImg) afterImg.onload = compare ? onOverlayReady : null;

	const beforeUrl = before && before.available
		? driftImagesProxyUrl("before", camUid, pipeline, cacheBust)
		: "";
	const afterUrl = compare && after && after.available
		? driftImagesProxyUrl("after", camUid, pipeline, cacheBust)
		: "";

	if (before && before.available) {
		setDriftSideImage("before", beforeUrl);
	} else {
		setDriftSideImage("before", "");
	}
	if (afterUrl) {
		setDriftSideImage("after", afterUrl);
	} else {
		setDriftSideImage("after", "");
	}

	const grid = $("drift_images_grid");
	if (grid) {
		grid.hidden = !(beforeUrl || afterUrl);
	}
	if (beforeUrl || afterUrl) {
		scheduleDriftShiftArrow(meta);
	} else {
		clearDriftShiftArrow();
	}
}

function openDriftImagesModal({ camUid, pipeline, title, isDrifted }) {
	const uid = String(camUid || "").trim();
	if (!uid || !urls.cameraDriftImages) {
		showAlertModal(driftT("unavailable"));
		return;
	}
	applyDriftImagesStaticI18n();
	const cacheBust = Date.now();
	const drifted = resolveDriftCameraIsDrifted(uid, isDrifted);
	driftImagesState = {
		camUid: uid,
		pipeline: pipeline || "",
		title: title || driftT("title"),
		meta: null,
		persistentPoints: [],
		anchorEdit: false,
		isDrifted: drifted,
		cacheBust,
		loadId: (driftImagesState && driftImagesState.loadId || 0) + 1,
	};
	const loadId = driftImagesState.loadId;
	$("drift_images_title").textContent = driftImagesState.title;
	$("drift_images_status").hidden = false;
	$("drift_images_status").textContent = drifted
		? driftT("loading")
		: driftT("referenceOnly");
	renderDriftHint(null);
	renderDriftRawMetrics(null);
	clearDriftPairImages();
	setDriftResetVisible(false);
	setDriftAnchorEditMode(false);
	updateDriftAnchorBar();
	setOverlayVisible($("drift_images_modal"), true);
	loadDriftPersistentPoints();

	// Prefetch while meta computes; cache-bust avoids post-reset stale PNGs.
	prefetchDriftPairImages(uid, driftImagesState.pipeline, cacheBust, drifted);

	const params = new URLSearchParams({
		pipeline: driftImagesState.pipeline,
		cam_uid: uid,
		lang: uiLang(),
	});
	fetchJsonGet(`${urls.cameraDriftImages}?${params.toString()}`, (data) => {
		if (!driftImagesState || driftImagesState.loadId !== loadId) return;
		const emptyMsg = isDriftImagesCompareMode()
			? driftT("notFound")
			: driftT("noReference");
		if (!data || data.ok === false) {
			// Edge may return localized errors (e.g. Korean); UI status stays English.
			$("drift_images_status").textContent = emptyMsg;
			clearDriftPairImages();
			setDriftResetVisible(Boolean(uid));
			return;
		}
		driftImagesState.meta = data;
		const hasImage = Boolean(
			(data.before && data.before.available)
			|| (isDriftImagesCompareMode() && data.after && data.after.available)
			|| (isDriftImagesCompareMode() && data.overlay && data.overlay.available),
		);
		if (!hasImage) {
			$("drift_images_status").textContent = emptyMsg;
			clearDriftPairImages();
			if (isDriftImagesCompareMode()) {
				renderDriftHint(data);
				renderDriftRawMetrics(data);
			} else {
				renderDriftHint(null);
				renderDriftRawMetrics(null);
			}
			setDriftResetVisible(true);
			return;
		}
		$("drift_images_status").hidden = true;
		renderDriftImagesPair();
	}, {
		timeout: 20000,
		onerror() {
			if (!driftImagesState || driftImagesState.loadId !== loadId) return;
			$("drift_images_status").textContent = driftT("failed");
			clearDriftPairImages();
			setDriftResetVisible(Boolean(uid));
		},
		ontimeout() {
			if (!driftImagesState || driftImagesState.loadId !== loadId) return;
			$("drift_images_status").textContent = driftT("timeout");
			clearDriftPairImages();
			setDriftResetVisible(Boolean(uid));
		},
	});
}

const PLC_TAG_META_KEYS = new Set([
	"timestamp",
	"everguard_srvtime",
	"stale",
	"stale_after_ms",
	"updated_at_ms",
]);

function formatPlcUpdatedAt(ms) {
	const formatted = formatEpochMsLocal(ms, { withSeconds: true });
	if (!formatted) return "—";
	// Keep previous PLC meta style: dots between date parts.
	return formatted.replace(/^(\d{4})-(\d{2})-(\d{2}) /, "$1.$2.$3 ");
}

function plcTagStatusFromMeta(meta) {
	if (!meta || typeof meta !== "object") return null;
	const raw = String(meta.status || "").trim().toUpperCase();
	if (raw === "OK" || raw === "WARN" || raw === "ERROR" || raw === "ERR") {
		return raw === "ERR" ? "ERROR" : raw;
	}
	// Legacy edge payloads used boolean `stale`.
	if (meta.stale != null) return meta.stale ? "WARN" : "OK";
	return null;
}

function normalizePlcTagModalPayload(tags, meta) {
	const nextMeta = meta && typeof meta === "object" ? { ...meta } : {};
	const filtered = [];
	for (const tag of Array.isArray(tags) ? tags : []) {
		const name = String(tag?.name || "");
		const key = name.toLowerCase();
		if (key === "stale") {
			if (plcTagStatusFromMeta(nextMeta) == null) {
				nextMeta.status = String(tag.value).toLowerCase() === "true" ? "WARN" : "OK";
			}
			delete nextMeta.stale;
			continue;
		}
		if (key === "status" && plcTagStatusFromMeta(nextMeta) == null) {
			const v = String(tag.value || "").trim().toUpperCase();
			if (v === "OK" || v === "WARN" || v === "ERROR" || v === "ERR") {
				nextMeta.status = v === "ERR" ? "ERROR" : v;
			}
			continue;
		}
		if (key === "updated_at_ms") {
			if (nextMeta.updated_at_ms == null) {
				const n = Number(tag.value);
				if (Number.isFinite(n) && n > 0) nextMeta.updated_at_ms = n;
			}
			continue;
		}
		if (PLC_TAG_META_KEYS.has(key) || /(_nifitime|_srctime|_srvtime)$/i.test(name)) {
			continue;
		}
		filtered.push(tag);
	}
	const status = plcTagStatusFromMeta(nextMeta);
	if (status) nextMeta.status = status;
	delete nextMeta.stale;
	return { tags: filtered, meta: nextMeta };
}

function renderPlcTagMeta(meta) {
	const box = $("plc_tag_modal_meta");
	if (!box) return;
	const status = plcTagStatusFromMeta(meta);
	const hasDriftCounts = meta
		&& meta.total != null
		&& meta.drifted != null;
	const hasUpdated = meta && meta.updated_at_ms != null;
	if (!meta || (status == null && !hasDriftCounts && !hasUpdated)) {
		box.hidden = true;
		box.innerHTML = "";
		return;
	}
	const statusClass =
		status === "ERROR" ? "is-error" : status === "WARN" ? "is-warn" : "is-ok";
	const rows = [];
	if (meta.group) {
		rows.push(`
		<div class="plc-tag-meta-row">
			<span class="plc-tag-meta-label">Group</span>
			<span class="plc-tag-meta-value">${escapeHtml(String(meta.group))}</span>
		</div>`);
	}
	if (status) {
		rows.push(`
		<div class="plc-tag-meta-row">
			<span class="plc-tag-meta-label">Status</span>
			<span class="plc-tag-meta-value plc-tag-meta-status ${statusClass}">${escapeHtml(status)}</span>
		</div>`);
	}
	if (hasDriftCounts) {
		rows.push(`
		<div class="plc-tag-meta-row">
			<span class="plc-tag-meta-label">Drifted</span>
			<span class="plc-tag-meta-value">${escapeHtml(String(meta.drifted))} / ${escapeHtml(String(meta.total))}</span>
		</div>`);
	}
	if (meta.online != null && meta.total != null) {
		rows.push(`
		<div class="plc-tag-meta-row">
			<span class="plc-tag-meta-label">Online</span>
			<span class="plc-tag-meta-value">${escapeHtml(String(meta.online))} / ${escapeHtml(String(meta.total))}</span>
		</div>`);
	}
	if (hasUpdated) {
		rows.push(`
		<div class="plc-tag-meta-row">
			<span class="plc-tag-meta-label">Last update</span>
			<span class="plc-tag-meta-value">${escapeHtml(formatPlcUpdatedAt(meta.updated_at_ms))}</span>
		</div>`);
	}
	box.hidden = false;
	box.innerHTML = rows.join("");
}

let plcTagModalState = null;

function tagValueLower(tag) {
	return String(tag?.value != null ? tag.value : "").toLowerCase();
}

function renderPlcTagValue(tag) {
	const lower = tagValueLower(tag);
	let display = tag.value != null ? String(tag.value) : "";
	let tone = "unknown";
	if (lower === "true" || lower === "false") {
		display = lower === "true" ? "TRUE" : "FALSE";
		tone = lower === "true" ? "ok" : "idle";
	} else if (tag.health === "err" || lower === "error" || lower === "drift") {
		tone = "err";
		if (lower === "drift") display = "DRIFT";
	} else if (lower === "off" || lower === "offline") {
		tone = "idle";
		display = "OFF";
	} else if (lower === "ok") {
		tone = "ok";
		display = "OK";
	}
	return `<span class="device-chip device-chip--tag chip-${tone}">${escapeHtml(display)}</span>`;
}

function ipv4SortKeyFromText(text) {
	const match = String(text || "").match(/\b(\d{1,3}(?:\.\d{1,3}){3})\b/);
	if (!match) return [1, 999, 999, 999, 999, String(text || "")];
	const parts = match[1].split(".").map((part) => Number(part));
	if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
		return [1, 999, 999, 999, 999, String(text || "")];
	}
	return [0, ...parts, String(text || "")];
}

function compareDriftCameraTags(a, b) {
	const aDrift = tagValueLower(a) === "drift" ? 0 : 1;
	const bDrift = tagValueLower(b) === "drift" ? 0 : 1;
	if (aDrift !== bDrift) return aDrift - bDrift;
	const aKey = ipv4SortKeyFromText(a?.name);
	const bKey = ipv4SortKeyFromText(b?.name);
	for (let i = 0; i < aKey.length; i += 1) {
		if (aKey[i] < bKey[i]) return -1;
		if (aKey[i] > bKey[i]) return 1;
	}
	return 0;
}

function countTagsByValue(list, predicate) {
	return list.filter((tag) => predicate(tagValueLower(tag))).length;
}

function formatScopedModalTitle(pipeline, title, fallback) {
	const group = String(title || "").trim();
	const pipe = String(pipeline || "").trim();
	if (pipe && group) {
		if (
			group === pipe
			|| group.startsWith(`${pipe} > `)
			|| group.startsWith(`${pipe}/`)
		) {
			return group;
		}
		return `${pipe} > ${group}`;
	}
	return group || fallback || "";
}

function openPlcTagModal({ title, url, tags, meta, pipeline }) {
	const normalized = normalizePlcTagModalPayload(tags, meta);
	let list = normalized.tags;
	const trueN = countTagsByValue(list, (v) => v === "true");
	const falseN = countTagsByValue(list, (v) => v === "false");
	const driftN = countTagsByValue(list, (v) => v === "drift");
	const offN = countTagsByValue(list, (v) => v === "off" || v === "offline");
	const okN = countTagsByValue(list, (v) => v === "ok");
	const errN = list.filter((t) => t.health === "err" || tagValueLower(t) === "error").length;
	const otherN = list.length - trueN - falseN - driftN - offN - okN - errN;

	const isDriftModal = driftN > 0 || offN > 0 || okN > 0;
	if (isDriftModal) {
		list = [...list].sort(compareDriftCameraTags);
	}
	const fallback = isDriftModal ? "Drift Cameras" : "PLC Tags";
	plcTagModalState = {
		title: formatScopedModalTitle(pipeline, title, fallback),
		url: url || "",
		tags: list,
		meta: normalized.meta,
		pipeline: pipeline || "",
		isDriftModal,
	};
	$("plc_tag_modal_title").textContent = plcTagModalState.title;
	const parts = isDriftModal
		? [`${list.length} cameras`, `${driftN} DRIFT`, `${offN} OFF`, `${okN} OK`]
		: [`${list.length} tags`, `${trueN} true`, `${falseN} false`];
	if (otherN > 0) parts.push(`${otherN} other`);
	if (!isDriftModal && errN > 0) parts.push(`${errN} error`);
	$("plc_tag_modal_summary").textContent = parts.join(" · ");

	// Drift modal: group/status/counts already appear in title + summary.
	if (isDriftModal) {
		const box = $("plc_tag_modal_meta");
		if (box) {
			box.hidden = true;
			box.innerHTML = "";
		}
	} else {
		renderPlcTagMeta(normalized.meta);
	}

	const link = $("plc_tag_modal_link");
	if (!isDriftModal && url) {
		link.href = url;
		link.classList.remove("hidden");
	} else {
		link.removeAttribute("href");
		link.classList.add("hidden");
	}

	$("plc_tag_modal_list").innerHTML = list.length
		? list.map((tag) => {
			const camUid = String(tag.id || "").trim();
			const name = tag.name || "";
			// Drift/OK cameras open images modal; OFF cameras stay plain text.
			const nameHtml = isDriftModal && camUid && !isOfflineCameraTag(tag)
				? `<button type="button" class="plc-tag-name plc-tag-name-btn drift-images-btn" title="${escapeAttr(name)}" data-cam-uid="${escapeAttr(camUid)}" data-pipeline="${escapeAttr(plcTagModalState.pipeline || "")}" data-cam-name="${escapeAttr(name)}" data-cam-drifted="${isDriftCameraTag(tag) ? "1" : "0"}">${escapeHtml(name)}</button>`
				: `<span class="plc-tag-name" title="${escapeAttr(name)}">${escapeHtml(name)}</span>`;
			return `<div class="plc-tag-row">
				${nameHtml}
				${renderPlcTagValue(tag)}
			</div>`;
		}).join("")
		: `<div class="plc-tag-empty">${isDriftModal ? "No cameras" : "No tags"}</div>`;

	setOverlayVisible($("plc_tag_modal"), true);
}

function resetCameraDrift(camUid, pipeline, btn) {
	const uid = String(camUid || "").trim();
	if (!uid || !urls.cameraDriftReset) return;
	if (btn) {
		btn.disabled = true;
		btn.textContent = "…";
	}
	postJson(urls.cameraDriftReset, { cam_uid: uid, pipeline: pipeline || "" }, 25000, {
		onload(_status, data) {
			if (!data || !data.ok) {
				if (btn) {
					btn.disabled = false;
					btn.textContent = driftT("reset");
				}
				showAlertModal((data && data.error) || "Camera drift reset failed");
				return;
			}
			closeDriftImagesModal();
			if (plcTagModalState && Array.isArray(plcTagModalState.tags)) {
				// Only drop drifted rows after reset; OK cameras stay in the list.
				const wasDrift = plcTagModalState.tags.some(
					(tag) => String(tag.id || "") === uid && isDriftCameraTag(tag),
				);
				if (wasDrift) {
					plcTagModalState.tags = plcTagModalState.tags.filter(
						(tag) => String(tag.id || "") !== uid,
					);
				}
				if (plcTagModalState.tags.length) {
					openPlcTagModal(plcTagModalState);
				} else {
					closePlcTagModal();
				}
			}
			fetchStatus(true);
		},
		onerror() {
			if (btn) {
				btn.disabled = false;
				btn.textContent = driftT("reset");
			}
			showAlertModal("Camera drift reset failed");
		},
		ontimeout() {
			if (btn) {
				btn.disabled = false;
				btn.textContent = driftT("reset");
			}
			showAlertModal("Camera drift reset timed out");
		},
	});
}

function openCameraLive(pipeline, camIndex, title) {
	stopCameraLiveStream();
	const img = $("camera_live_img");
	const status = $("camera_live_status");
	const fallback = `Camera ${camIndex + 1}`;
	$("camera_live_title").textContent = formatScopedModalTitle(
		pipeline, title, fallback,
	);
	status.textContent = "Connecting…";
	clearCameraLiveLamps();
	setCameraLiveImageVisible(false);
	setOverlayVisible($("camera_live_modal"), true);

	const feedUrl = `${cameraFeedBase}?pipeline=${encodeURIComponent(pipeline)}&cam=${camIndex}`;
	let receivedFrame = false;
	let lastFps = null;
	cameraLiveSource = new EventSource(feedUrl);
	cameraLiveSource.onmessage = function (event) {
		let data = {};
		try {
			data = JSON.parse(event.data || "{}");
		} catch (e) {
			return;
		}
		if (data.error) {
			status.textContent = data.error;
			setCameraLiveImageVisible(false);
			clearCameraLiveLamps();
			return;
		}
		if (data.jpeg) {
			receivedFrame = true;
			img.src = `data:image/jpeg;base64,${data.jpeg}`;
			setCameraLiveImageVisible(true);
			// PLC /stream often sends fps as "" on most frames — keep last value.
			const fps = data.fps;
			if (fps != null && String(fps).trim() !== "") {
				lastFps = fps;
			}
			status.textContent = lastFps != null ? `FPS: ${lastFps}` : "Live";
			if (Object.prototype.hasOwnProperty.call(data, "lamps")) {
				renderCameraLiveLamps(data.lamps);
			}
		} else if (!receivedFrame) {
			status.textContent = "Waiting for frame…";
		}
	};
	cameraLiveSource.onerror = function () {
		if (!receivedFrame) {
			status.textContent = "Stream disconnected";
		}
	};
}

function parseMemoryBytes(value) {
	const match = String(value).trim().match(/^([\d.]+)\s*(B|KiB|MiB|GiB|TiB)?$/i);
	if (!match) return null;
	const num = parseFloat(match[1]);
	if (!Number.isFinite(num)) return null;
	const unit = (match[2] || "B").toUpperCase();
	const factor = MEMORY_UNIT_BYTES[unit];
	return factor ? num * factor : null;
}

function memoryUsageDisplay(mem) {
	const parts = String(mem).split("/").map((part) => part.trim());
	if (parts.length !== 2) {
		return { text: mem, percent: null };
	}
	const used = parseMemoryBytes(parts[0]);
	const total = parseMemoryBytes(parts[1]);
	if (!used || !total) {
		return { text: mem, percent: null };
	}
	return {
		text: `${parts[0]} / ${parts[1]}`,
		percent: Math.min(100, (used / total) * 100),
	};
}

function formatMemoryPercent(percent) {
	if (percent == null) return "";
	if (percent < 10) return `${percent.toFixed(1)}%`;
	return `${Math.round(percent)}%`;
}

function memoryPercentForEntry(entry, mem) {
	if (entry?.mem_usage_percent != null && Number.isFinite(entry.mem_usage_percent)) {
		return Math.min(100, Number(entry.mem_usage_percent));
	}
	return memoryUsageDisplay(mem).percent;
}

function memoryWarnThreshold() {
	return Number(statusDisplay.mem_warn_percent) || 80;
}

function memoryExceedsWarn(entry) {
	const percent = memoryPercentForEntry(entry, entry && entry.mem_usage);
	if (percent == null) return false;
	return percent > memoryWarnThreshold();
}

function memoryToneClass(percent) {
	if (percent == null) return "";
	if (percent > memoryWarnThreshold()) return " svc-mem--warn";
	return "";
}

function formatStreamLink(name, entry) {
	const { isRtls, isSys, isKafka, isCameraDrift } = pipelineRuntimeFlags(name, entry);
	const url = streamUrlFor(name, entry);
	if (isRtls || isSys || !url) return "";
	let href = url;
	if (isKafka || isCameraDrift) {
		href = (/^https?:\/\//.test(url) ? url : `http://${url}`).replace(/\/$/, "");
	}
	const safeUrl = escapeAttr(href);
	const label = streamEndpointLabel(url);
	const rowLabel = isKafka ? "PLC:" : (isCameraDrift ? "Drift:" : "Stream:");
	return `<div class="pipeline-stream-row"><span class="pipeline-stream-label">${rowLabel}</span><a class="svc-stream" href="${safeUrl}" target="_blank" rel="noopener" title="${safeUrl}">${escapeHtml(label)}</a></div>`;
}

function formatServerIpRow(name) {
	const serverIp = pipelineInfo(name).server_ip;
	const busy = ipUpdating.has(name);
	const ipHtml = serverIp
		? `<span class="server-ip">${serverIp}</span>`
		: `<span class="server-ip server-ip-missing">no IP</span>`;
	return `<div class="server-ip-row">${ipHtml}<button type="button" class="status-badge ip-update-btn${busy ? " is-busy" : ""}" title="Update IP from S3" aria-label="Update IP" data-pipeline="${escapeAttr(name)}"${busy ? " disabled" : ""}>Update IP</button></div>`;
}

function formatServerIdCell(name, entry) {
	const serverId = pipelineInfo(name).server_id;
	let html = "";
	if (serverId) {
		html += `<span class="server-id" title="${serverId}">${serverId}</span>`;
	} else {
		html += `<span class="muted-dash">—</span>`;
	}
	html += formatVersionRow(entry);
	return html;
}

function finishIpUpdateUi(name) {
	ipUpdating.delete(name);
	if (lastPipelines) renderTable(lastPipelines);
}

function updateServerIp(name, event) {
	if (event) {
		event.preventDefault();
		event.stopPropagation();
	}
	if (ipUpdating.has(name)) return;
	ipUpdating.add(name);
	if (lastPipelines) {
		renderTable(lastPipelines);
	}

	postJson(urls.updateServerIp, { pipeline: name }, 60000, {
		onload: function (status, data) {
			finishIpUpdateUi(name);
			if (status !== 200 || !data.ok) {
				showAlertModal(data.error || "IP update failed", { title: "IP Update", tone: "err" });
				return;
			}
			if (!pipelineStatic[name]) pipelineStatic[name] = {};
			pipelineStatic[name].server_ip = data.server_ip;
			refreshViewSelects();
			renderTargetGroups(new Set(getSelectedTargets()));
			renderServerGroups(new Set(getSelectedServers()));
			if (lastPipelines) renderTable(lastPipelines);
			fetchStatus(true);
		},
		onerror: function () {
			finishIpUpdateUi(name);
			showAlertModal("IP update failed", { title: "IP Update", tone: "err" });
		},
		ontimeout: function () {
			finishIpUpdateUi(name);
			showAlertModal("IP update timed out", { title: "IP Update", tone: "err" });
		},
	});
}

function formatVersionRow(entry) {
	if (!entry) return "";
	const current = entry.version_current;
	const latest = entry.version_latest;
	const currentDate = entry.version_current_date || "";
	const latestDate = entry.version_latest_date || "";
	if (!current && !latest && !currentDate && !latestDate) {
		return `<div class="version-row"><span class="version-muted">—</span></div>`;
	}
	const upToDate = Boolean(current && latest && current === latest);
	const curClass = upToDate ? "version-tag--ok" : (current && latest ? "version-tag--warn" : "");
	return `<div class="version-row" title="Git revision: current on edge / latest from central">
		<span class="version-label">Current:</span>
		<span class="version-tag ${curClass}">${escapeHtml(current || "?")}</span>
		<span class="version-date">${escapeHtml(currentDate)}</span>
		<span class="version-label">Latest:</span>
		<span class="version-tag">${escapeHtml(latest || "?")}</span>
		<span class="version-date">${escapeHtml(latestDate)}</span>
	</div>`;
}

function formatServerNameCell(name) {
	return `<div class="pipeline-cell"><div class="pipeline-name-row"><span class="pipeline-name">${name}</span>${pipelineTypeTag(name)}</div>${formatServerIpRow(name)}</div>`;
}

function formatMemoryUsage(entry) {
	const mem = entry && entry.mem_usage;
	if (!mem) {
		return `<div class="svc-mem-row"><span class="svc-mem-label">Memory:</span><span class="svc-mem-value muted-dash">—</span></div>`;
	}
	const { text } = memoryUsageDisplay(mem);
	const percent = memoryPercentForEntry(entry, mem);
	const toneClass = memoryToneClass(percent);
	const pctHtml = percent != null
		? `<span class="svc-mem-pct">${formatMemoryPercent(percent)}</span>`
		: "";
	return `<div class="svc-mem-row${toneClass}"><span class="svc-mem-label">Memory:</span><span class="svc-mem-value" title="Container memory">${escapeHtml(text)}</span>${pctHtml}</div>`;
}

function metricTone(now, set) {
	if (now == null || set == null || set <= 0) return "idle";
	if (now === set) return "ok";
	if (now > 0) return "warn";
	return "err";
}

function metricBar(now, set) {
	if (now == null || set == null || set <= 0) return 0;
	return Math.min(100, Math.round((now / set) * 100));
}

function chipStatusesFromBools(statuses, running) {
	return (statuses || []).map((ok) => {
		if (!running) return "idle";
		return ok ? "ok" : "err";
	});
}

function chipStatusesFromCount(now, set, running) {
	const total = set || 0;
	const statuses = [];
	for (let i = 0; i < total; i++) {
		if (!running) statuses.push("idle");
		else if (now == null) statuses.push("unknown");
		else if (i < now) statuses.push("ok");
		else statuses.push("err");
	}
	return statuses;
}

function isLiveCameraChipPrefix(prefix) {
	return prefix === "C" || prefix === "Sign";
}

function usesMetricChipOptions(prefix) {
	return isLiveCameraChipPrefix(prefix) || prefix === "D" || prefix === "T";
}

function renderMetricChip({ prefix, index, status, link, liveOptions }) {
	const fallback = `${prefix}${index + 1}`;
	// PLC tags: chip text is the state value (true/false/number/error).
	const label = (prefix === "T" && link && link.value != null && String(link.value) !== "")
		? String(link.value)
		: (prefix === "D" && link && link.label)
			? String(link.label)
		: (prefix === "P" && link && link.value != null && String(link.value) !== "")
			? String(link.value)
		: fallback;
	const title = link ? (link.title || link.label || label) : label;
	const safeTitle = escapeAttr(title);
	const chipClass = `device-chip chip-${status}${prefix === "T" ? " device-chip--tag" : ""}`;
	const streamUrl = link && link.url ? link.url : "";

	if (
		prefix === "P" && link && link.pipeline
	) {
		return `<button type="button" class="device-chip-btn" title="Go to ${safeTitle}" aria-label="Go to ${safeTitle}" data-pipeline="${escapeAttr(link.pipeline)}"><span class="${chipClass}">${escapeHtml(label)}</span></button>`;
	}
	if (
		isLiveCameraChipPrefix(prefix)
		&& status === "ok" && liveOptions
		&& liveOptions.pipeline && liveOptions.streamUrl
	) {
		return `<button type="button" class="device-chip-btn device-chip-live" title="View live: ${safeTitle}" aria-label="View live ${escapeAttr(fallback)}" data-pipeline="${escapeAttr(liveOptions.pipeline)}" data-cam="${index}" data-title="${safeTitle}"><span class="${chipClass}">${escapeHtml(label)}</span></button>`;
	}
	if (prefix === "T" || (prefix === "D" && link && Array.isArray(link.tags) && link.tags.length)) {
		const tagsJson = escapeAttr(JSON.stringify(link && link.tags ? link.tags : []));
		// PLC tags: location/sub (SBC/LineA). Drift areas: just the area (RND), not Drift/RND.
		const groupTitle = escapeAttr(
			prefix === "D"
				? ((link && (link.value || link.label)) || label)
				: ((link && (link.location
					? `${link.location}/${link.value || link.label}`
					: (link.value || link.label))) || label),
		);
		const metaJson = escapeAttr(JSON.stringify(link && link.meta ? link.meta : {}));
		const chipBtnClass = prefix === "D" ? "device-chip-drift-tag" : "device-chip-plc-tag";
		const pipelineAttr = liveOptions && liveOptions.pipeline
			? ` data-pipeline="${escapeAttr(liveOptions.pipeline)}"`
			: "";
		return `<button type="button" class="device-chip-btn ${chipBtnClass}" title="${safeTitle}" aria-label="${safeTitle}" data-plc-title="${groupTitle}" data-plc-url="${escapeAttr(streamUrl)}" data-plc-tags="${tagsJson}" data-plc-meta="${metaJson}"${pipelineAttr}><span class="${chipClass}">${escapeHtml(label)}</span></button>`;
	}
	if (status === "ok" && streamUrl) {
		return `<a class="device-chip-link" href="${escapeAttr(streamUrl)}" target="_blank" rel="noopener" title="${safeTitle}"><span class="${chipClass}">${escapeHtml(label)}</span></a>`;
	}
	const host = deviceHost(link);
	if (status !== "ok" && host && prefix !== "P") {
		const openUrl = deviceOpenUrl(link, host);
		return `<button type="button" class="device-chip-btn" title="Ping ${escapeAttr(host)}" aria-label="Ping ${escapeAttr(host)}" data-host="${escapeAttr(host)}" data-url="${escapeAttr(openUrl)}"><span class="${chipClass}">${label}</span></button>`;
	}
	return `<span class="${chipClass}" title="${safeTitle}">${escapeHtml(label)}</span>`;
}

function deviceChips(prefix, statuses, links, liveOptions) {
	if (!statuses || !statuses.length) return "";
	const opts = usesMetricChipOptions(prefix) ? liveOptions : null;
	const chips = statuses.map((status, idx) => renderMetricChip({
		prefix,
		index: idx,
		status,
		link: links && links[idx],
		liveOptions: opts,
	})).join("");
	return `<div class="device-chips">${chips}</div>`;
}

function metricBlock(label, now, set, running, chipPrefix, chipStatuses, links, liveOptions, toneOverride) {
	const displayNow = now != null ? now : (running ? "?" : "-");
	const displaySet = set != null ? set : "?";
	const tone = toneOverride || metricTone(now, set);
	const width = metricBar(now, set);
	const chips = chipPrefix && chipStatuses && chipStatuses.length
		? deviceChips(chipPrefix, chipStatuses, links, liveOptions)
		: "";
	return `<div class="metric-block">
		<div class="metric-head">
			<span class="metric-label">${label}</span>
			<span class="metric-nums"><strong>${displayNow}</strong><span class="metric-sep">/</span>${displaySet}</span>
		</div>
		<div class="metric-bar" aria-hidden="true"><div class="metric-fill ${tone}" style="width:${width}%"></div></div>
		${chips}
	</div>`;
}

function rtlsDeviceMetric(entry, label, prefix, nowKey, setKey, statusKey, linksKey) {
	const set = entry[setKey];
	if (set == null) return "";
	const running = !!entry.running;
	const statuses = entry[statusKey] && entry[statusKey].length
		? chipStatusesFromBools(entry[statusKey], running)
		: chipStatusesFromCount(entry[nowKey], set, running);
	return metricBlock(label, entry[nowKey], set, running, prefix, statuses, entry[linksKey]);
}

function formatRtlsMetricCell(entry) {
	if (entry.rtls_config_missing) {
		return `<div class="metric-grid"><div class="metric-block"><span class="metric-label">Config</span><span class="metric-nums"><strong class="metric-fill warn">missing</strong></span></div></div>`;
	}
	if (entry.rtls_devices_missing) {
		return `<div class="metric-grid"><div class="metric-block"><span class="metric-label">Devices</span><span class="metric-nums"><strong>-</strong></span></div></div>`;
	}
	const qlBlock = rtlsDeviceMetric(entry, "Tower Lamp", "L", "qlight_now", "qlight_set", "qlight_status", "qlight_links");
	const spBlock = rtlsDeviceMetric(entry, "IP Speaker", "S", "speaker_now", "speaker_set", "speaker_status", "speaker_links");
	return `<div class="metric-grid">${qlBlock}${spBlock}</div>`;
}

function formatCameraDriftMetricCell(entry, name) {
	// Head: TOTAL: {total}     {drifted}/{online}
	const running = !!entry.running;
	const liveOptions = { pipeline: name || entry.pipeline || "" };
	const renderDriftBlock = (now, total, online, statuses, links) => {
		const tone = !running ? "idle" : (now > 0 ? "warn" : "ok");
		const right = online != null ? online : total;
		return metricBlock(
			total != null ? `TOTAL: ${total}` : "TOTAL",
			now,
			right,
			running,
			"D",
			statuses,
			links || [],
			liveOptions,
			tone,
		);
	};

	const groups = entry.drift_camera_groups;
	if (Array.isArray(groups) && groups.length) {
		const blocks = groups.map((group) => {
			const statuses = (group.chip_status || []).map((status) => (
				running ? status : "idle"
			));
			const online = group.online != null
				? group.online
				: entry.drift_cameras_online;
			return renderDriftBlock(
				group.now, group.set, online, statuses, group.links,
			);
		}).join("");
		return `<div class="metric-grid metric-grid--plc">${blocks}</div>`;
	}

	const now = entry.drift_cameras_now;
	const total = entry.drift_cameras_set;
	if (total == null || now == null) {
		return `<div class="metric-grid"><div class="metric-block">
			<span class="metric-label">TOTAL</span>
			<span class="metric-nums"><strong class="metric-fill warn">?</strong></span>
		</div></div>`;
	}
	const statuses = entry.drift_camera_status && entry.drift_camera_status.length
		? (running
			? entry.drift_camera_status
			: entry.drift_camera_status.map(() => "idle"))
		: [];
	return renderDriftBlock(
		now,
		total,
		entry.drift_cameras_online,
		statuses,
		entry.drift_camera_links,
	);
}

function formatEgMetricCell(entry, name) {
	const running = !!entry.running;
	const { isPlcCv } = pipelineRuntimeFlags(name, entry);
	const camSet = entry.cameras_set ?? entry.camera_links?.length ?? 0;
	const camStatuses = entry.camera_status && entry.camera_status.length
		? entry.camera_status
		: chipStatusesFromCount(entry.cameras_now, camSet, running);
	const metricLabel = isPlcCv ? "Signs" : "Cameras";
	const chipPrefix = isPlcCv ? "Sign" : "C";
	return metricBlock(
		metricLabel,
		entry.cameras_now,
		entry.cameras_set,
		running,
		chipPrefix,
		camStatuses,
		entry.camera_links,
		{
			pipeline: name,
			streamUrl: streamUrlFor(name, entry),
			streamHealth: !!entry.stream_health,
		},
	);
}

function formatKafkaMetricCell(entry, name) {
	const running = !!entry.running;
	const liveOptions = { pipeline: name || entry.pipeline || "" };
	const groups = entry.plc_tag_groups;
	if (Array.isArray(groups) && groups.length) {
		const blocks = groups.map((group) => {
			const statuses = (group.chip_status || []).map((status) => (
				running ? status : "idle"
			));
			return metricBlock(
				group.label || "PLC",
				group.now,
				group.set,
				running,
				"T",
				statuses,
				group.links || [],
				liveOptions,
			);
		}).join("");
		return `<div class="metric-grid metric-grid--plc">${blocks}</div>`;
	}
	const set = entry.plc_tags_set;
	if (set == null) {
		return `<div class="metric-grid"><div class="metric-block">
			<span class="metric-label">PLC Tags</span>
			<span class="metric-nums"><strong class="metric-fill warn">?</strong></span>
		</div></div>`;
	}
	const statuses = entry.plc_tag_status && entry.plc_tag_status.length
		? (running
			? entry.plc_tag_status
			: entry.plc_tag_status.map(() => "idle"))
		: chipStatusesFromCount(entry.plc_tags_now, set, running);
	return metricBlock(
		"PLC Tags",
		entry.plc_tags_now,
		set,
		running,
		"T",
		statuses,
		entry.plc_tag_links || [],
		liveOptions,
	);
}

function formatMetricCell(entry, name) {
	const { isRtls, isSys, isKafka, isCameraDrift } = pipelineRuntimeFlags(name, entry);
	if (isSys) return formatSysMetricCell(name, entry);
	if (isRtls) return formatRtlsMetricCell(entry);
	if (isKafka) return formatKafkaMetricCell(entry, name);
	if (isCameraDrift) return formatCameraDriftMetricCell(entry, name);
	return formatEgMetricCell(entry, name);
}

// --- SYS monitor metrics ---

function pipelineMonitorHost(name) {
	// Keep in sync with servers_cfg.pipeline_monitor_host.
	const info = pipelineInfo(name);
	if (info.monitor_host_ip) return info.monitor_host_ip;
	const { isRtls, isSys } = pipelineRuntimeFlags(name);
	if (isSys || isRtls) return info.server_ip || "";
	return "";
}

function isMonitoredEgPipeline(name, monitorHostIp) {
	// Keep in sync with servers_cfg.is_sys_monitored_peer (excludes SYS).
	const { isSys } = pipelineRuntimeFlags(name);
	if (isSys) return false;
	return pipelineMonitorHost(name) === monitorHostIp;
}

function monitoredPipelineNames(monitorHostIp) {
	if (!monitorHostIp) return [];
	return pipelineOrder.filter((name) => isMonitoredEgPipeline(name, monitorHostIp));
}

function monitorKindBucket(name) {
	// SYS MONITOR chips keep Kafka/CV split even though type badge is unified PLC.
	const info = pipelineInfo(name);
	const rawKind = info.pipeline_kind || "";
	const lower = String(name || "").toLowerCase();
	if (rawKind === "rtls" || info.is_rtls) return "rtls";
	if (
		rawKind === "plc_cv"
		|| info.is_plc_cv
		|| /plc-cv|cv-plc|plc_cv/.test(lower)
	) {
		return "plc_cv";
	}
	if (
		rawKind === "plc_kafka"
		|| info.is_kafka
		|| /plc-kafka|kafka/.test(lower)
	) {
		return "plc_kafka";
	}
	if (pipelineRuntimeFlags(name).isCameraDrift) return "camera_drift";
	return "peer";
}

function monitorPlcCvChipLabel(name) {
	// RND-PLC-CV-RND → PLC-CV-RND (also accepts legacy …-CV-PLC-…).
	const s = String(name || "");
	const match = s.match(/PLC-CV-(.+)$/i) || s.match(/CV-PLC-(.+)$/i);
	if (match) return `PLC-CV-${match[1]}`;
	return "PLC-CV";
}

function sysMonitorStatusFromCounts(runningCount, totalCount, monitorRunning) {
	if (!monitorRunning) return "ERR";
	if (!totalCount) return "OK";
	if (runningCount === totalCount) return "OK";
	return "WARN";
}

function monitorMetricTone(runningCount, totalCount, monitorRunning) {
	if (!monitorRunning || totalCount <= 0) return "idle";
	if (runningCount === totalCount) return "ok";
	if (runningCount > 0) return "warn";
	return "idle";
}

function monitorGroupChipStatus(members, monitorRunning) {
	if (!monitorRunning) return "idle";
	let running = 0;
	let pending = false;
	for (const name of members) {
		const entry = lastPipelines && lastPipelines[name];
		if (!entry || entry.status === "PENDING") {
			pending = true;
			continue;
		}
		if (entry.running) running += 1;
	}
	if (pending && running <= 0) return "unknown";
	if (running >= members.length) return "ok";
	if (running > 0) return "warn";
	return "idle";
}

function monitorGroupNavigateTarget(members) {
	for (const name of members) {
		const entry = lastPipelines && lastPipelines[name];
		if (!entry || entry.status === "PENDING" || !entry.running) return name;
	}
	return members[0] || "";
}

function monitoredPipelineView(monitorHostIp, monitorRunning) {
	// Chip order follows servers.json / pipelineOrder (first appearance).
	// PLC-CV is one chip per pipeline (PLC-CV-RND / PC2 / LBC, …).
	const names = monitoredPipelineNames(monitorHostIp);
	let running = 0;
	let pending = false;
	const groups = {
		rtls: [],
		plc_kafka: [],
		camera_drift: [],
	};
	const groupLabels = {
		rtls: "RTLS",
		plc_kafka: "PLC-KAFKA",
		camera_drift: "CAM-DRIFT",
	};

	for (const name of names) {
		const entry = lastPipelines && lastPipelines[name];
		if (!entry || entry.status === "PENDING") pending = true;
		else if (entry.running) running += 1;

		const bucket = monitorKindBucket(name);
		if (bucket !== "peer" && bucket !== "plc_cv") groups[bucket].push(name);
	}

	const chipStatuses = [];
	const links = [];
	const seenGroup = new Set();
	let peerIndex = 0;
	const pushChip = (label, members) => {
		if (!members.length) return;
		const target = monitorGroupNavigateTarget(members);
		const status = monitorGroupChipStatus(members, monitorRunning);
		chipStatuses.push(status);
		links.push({
			title: `${label}: ${members.join(", ")}`,
			label,
			value: label,
			url: "",
			pipeline: target,
		});
	};

	for (const name of names) {
		const bucket = monitorKindBucket(name);
		if (bucket === "peer") {
			peerIndex += 1;
			pushChip(`P${peerIndex}`, [name]);
			continue;
		}
		if (bucket === "plc_cv") {
			pushChip(monitorPlcCvChipLabel(name), [name]);
			continue;
		}
		if (seenGroup.has(bucket)) continue;
		seenGroup.add(bucket);
		pushChip(groupLabels[bucket] || bucket.toUpperCase(), groups[bucket]);
	}

	const counts = { running, total: names.length };
	let status;
	if (pending && names.length) {
		status = "WARN";
	} else {
		status = sysMonitorStatusFromCounts(running, names.length, monitorRunning);
	}
	return {
		counts,
		chipStatuses,
		links,
		status,
		pending,
	};
}

function sysMonitorHostIp(name) {
	return pipelineInfo(name).server_ip || "";
}

function effectivePipelineStatus(name, entry) {
	const { isSys } = pipelineRuntimeFlags(name, entry);
	if (isSys) {
		return monitoredPipelineView(sysMonitorHostIp(name), !!entry.running).status;
	}
	const status = entry.status || "ERR";
	if (status === "OK" && memoryExceedsWarn(entry)) {
		return "WARN";
	}
	return status;
}

function formatSysMetricCell(name, entry) {
	const monitorRunning = !!entry.running;
	const monitorHostIp = sysMonitorHostIp(name);
	const view = monitoredPipelineView(monitorHostIp, monitorRunning);
	const tone = view.pending
		? "warn"
		: monitorMetricTone(view.counts.running, view.counts.total, monitorRunning);
	return `<div class="metric-grid">${metricBlock(
		"Monitor",
		view.counts.running,
		view.counts.total,
		monitorRunning,
		"P",
		view.chipStatuses,
		view.links,
		null,
		tone,
	)}</div>`;
}

function streamEndpointLabel(url) {
	if (!url) return "";
	try {
		const parsed = new URL(url);
		const port = parsed.port || (parsed.protocol === "https:" ? "443" : "80");
		return `${parsed.hostname}:${port}`;
	} catch (e) {
		return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
	}
}

function formatServiceCell(entry, pipelineName) {
	const name = pipelineName || entry.pipeline || "";
	const stateHtml = entry.running
		? `<span class="svc-state running"><span class="svc-dot"></span>Running</span>`
		: `<span class="svc-state stopped"><span class="svc-dot"></span>Stopped</span>`;
	const streamHtml = formatStreamLink(name, entry);
	return `<div class="service-stack">${stateHtml}${streamHtml}${formatMemoryUsage(entry)}</div>`;
}

function isPipelineRowVisible(pipelineName) {
	return !!document.querySelector(
		`.status-item[data-pipeline="${CSS.escape(pipelineName)}"]`,
	);
}

function resetViewFilters() {
	viewState.groupFilter = null;
	viewState.serverFilter = null;
	viewState.typeFilter = null;
	for (const kind of Object.keys(VIEW_FILTER_META)) {
		renderViewFilter(kind);
	}
}

function revealPipelineInStatusView() {
	statusFilter = null;
	resetViewFilters();
	updateFilterPills();
	if (lastPipelines) {
		renderTable(lastPipelines);
	} else {
		showLoadingTable();
	}
}

function scrollToPipeline(pipelineName) {
	if (!pipelineName) return false;
	const row = document.querySelector(
		`.status-item[data-pipeline="${CSS.escape(pipelineName)}"]`,
	);
	if (!row) return false;
	row.scrollIntoView({ behavior: "smooth", block: "center" });
	row.classList.add("status-item--focus");
	window.setTimeout(() => row.classList.remove("status-item--focus"), 1600);
	return true;
}

function navigateToPipeline(pipelineName) {
	if (!pipelineName) return;
	if (isPipelineRowVisible(pipelineName)) {
		scrollToPipeline(pipelineName);
		return;
	}
	if (!pipelineOrder.includes(pipelineName)) return;
	showConfirmModal(
		`"${pipelineName}" is hidden by the current filter.\nClear filters and jump to it?`,
		{ title: "Hidden service", tone: "warn", okLabel: "Show", cancelLabel: "Cancel" },
	).then((ok) => {
		if (!ok) return;
		revealPipelineInStatusView();
		if (!scrollToPipeline(pipelineName)) {
			showAlertModal(
				`Could not show "${pipelineName}" in the status list.`,
				{ title: "Hidden service", tone: "warn" },
			);
		}
	});
}

function statusItem(pipelineName, entry) {
	const status = effectivePipelineStatus(pipelineName, entry);
	return `<div class="status-item ${rowStatusClass(status)}" data-pipeline="${escapeAttr(pipelineName)}">
		<div class="status-col">${formatServerNameCell(pipelineName)}</div>
		<div class="status-col server-id-cell">${formatServerIdCell(pipelineName, entry)}</div>
		<div class="status-col service-cell">${formatServiceCell(entry, pipelineName)}</div>
		<div class="status-col metric-cell">${formatMetricCell(entry, pipelineName)}</div>
		<div class="status-col status-col-badge">${statusBadge(status)}</div>
	</div>`;
}

function statusGroupPanel(group, count, itemsHtml) {
	const safeGroup = escapeAttr(group);
	return `<div class="group-panel status-group-panel" data-group="${safeGroup}">
		<div class="group-panel-header">
			<span class="group-panel-title">${escapeHtml(group)}</span>
			<span class="group-panel-count">${count} services</span>
		</div>
		<div class="group-panel-body status-panel-body">${itemsHtml}</div>
	</div>`;
}

function statusLoadingItem(name) {
	return `<div class="status-item row-pending" data-pipeline="${escapeAttr(name)}">
		<div class="status-col">${formatServerNameCell(name)}</div>
		<div class="status-col server-id-cell">${formatServerIdCell(name, null)}</div>
		<div class="status-col service-cell"><span class="svc-state idle"><span class="svc-dot"></span>—</span></div>
		<div class="status-col metric-cell"><span class="muted-dash">—</span></div>
		<div class="status-col status-col-badge">${statusBadge("PENDING")}</div>
	</div>`;
}

function showLoadingTable() {
	let html = "";
	for (const [group, names] of currentGroups()) {
		const items = names.map(statusLoadingItem).join("");
		html += statusGroupPanel(group, names.length, items);
	}
	$("status_body").innerHTML = html;
}

function matchesFilter(entry, name) {
	if (!statusFilter) return true;
	const status = effectivePipelineStatus(name, entry);
	if (statusFilter === "ERR") {
		return status === "ERR";
	}
	return status === statusFilter;
}

function setStatusFilter(filter) {
	statusFilter = filter;
	updateFilterPills();
	if (lastPipelines) {
		renderTable(lastPipelines);
	}
	renderTargetGroups();
	renderServerGroups();
}

function updateFilterPills() {
	for (const pill of document.querySelectorAll(".filter-pill")) {
		const filter = pill.dataset.filter;
		const active = filter === "all" ? !statusFilter : statusFilter === filter;
		pill.classList.toggle("active", active);
	}
}

function parseStatusResponse(body) {
	if (body && body.pipelines) {
		return body;
	}
	return {
		pipelines: body || {},
		refreshing: true,
		background_refreshing: false,
		ready: false,
		updated_at: 0,
		total: pipelineOrder.length,
	};
}

function rowStatusClass(status) {
	const value = (status || "ERR").toLowerCase();
	if (value === "pending") return "row-pending";
	return `row-${value}`;
}

function updateSummary(resp) {
	const names = filteredPipelineNames(viewState);
	let ok = 0, warn = 0, err = 0;
	for (const name of names) {
		const entry = resp[name];
		if (!entry) continue;
		const status = effectivePipelineStatus(name, entry);
		if (status === "OK") ok++;
		else if (status === "WARN") warn++;
		else if (status === "ERR") err++;
	}
	$("sum_ok").textContent = ok;
	$("sum_warn").textContent = warn;
	$("sum_err").textContent = err;
	if (!fullCollecting) {
		$("sum_total").textContent = names.length;
	}
}

function setTotalProgress(done, total, collecting) {
	fullCollecting = collecting;
	const pill = $("total_pill");
	const el = $("sum_total");
	if (collecting) {
		el.textContent = done + " / " + total;
		pill.classList.add("collecting");
	} else {
		el.textContent = total;
		pill.classList.remove("collecting");
	}
}

function renderTable(resp) {
	let html = "";
	for (const [group, names] of currentGroups()) {
		const visible = names.filter((name) => {
			const entry = resp[name];
			return entry && matchesFilter(entry, name);
		});
		if (!visible.length) continue;
		const items = visible.map((name) => statusItem(name, resp[name])).join("");
		html += statusGroupPanel(group, visible.length, items);
	}
	$("status_body").innerHTML = html;
}

function isStatusCollecting(meta, force) {
	if (meta.ready) {
		return false;
	}
	if (force) {
		return true;
	}
	if (meta.refreshing && !meta.background_refreshing) {
		return true;
	}
	const total = meta.total || pipelineOrder.length;
	const done = meta.done_count != null ? meta.done_count : 0;
	return total > 0 && done < total;
}

function updateLastUpdateDisplay(meta, force) {
	const el = $("lst_update");
	if (isStatusCollecting(meta, force)) {
		el.textContent = "updating...";
		el.classList.add("updating");
		return;
	}
	el.classList.remove("updating");
	if (meta.updated_at > 0) {
		el.textContent = formatEpochSecondsLocal(meta.updated_at, { withSeconds: true }) || "-";
	} else {
		el.textContent = "-";
	}
}

function refreshServerStatus(resp) {
	lastPipelines = resp;
	renderTable(resp);
	updateSummary(resp);
}

// --- Status API ---

function fetchStatus(force) {
	if (document.hidden && !force) return;
	let url = urls.getStatus + "?_=" + Date.now();
	if (force) {
		url = urls.getStatus + "?force=1&_=" + Date.now();
	}
	fetchJsonGet(url, (data, status) => {
		if (status !== 200) return;
		try {
			const meta = parseStatusResponse(data);
			const done = meta.done_count != null ? meta.done_count : 0;
			const total = meta.total || pipelineOrder.length;
			const collecting = isStatusCollecting(meta, force);

			if (collecting) {
				updateLastUpdateDisplay(meta, force);
				setTotalProgress(done, total, true);
				refreshServerStatus(meta.pipelines);
				scheduleFastPoll();
				return;
			}
			stopFastPoll();
			setTotalProgress(total, total, false);
			updateLastUpdateDisplay(meta, false);
			refreshServerStatus(meta.pipelines);
			if (!pollTimer) {
				schedulePoll();
			}
		} catch (e) {
			console.error("status parse failed", e);
		}
	}, {
		allowHidden: force,
		timeout: 15000,
		onerror: () => {
			console.warn("status request failed");
			scheduleFastPoll();
		},
		ontimeout: () => {
			console.warn("status request timed out");
			scheduleFastPoll();
		},
	});
}

// --- View filter UI ---

function viewFilterRoot(kind) {
	return $(`status_${kind}_filter`);
}

function arraysEqual(a, b) {
	if (a === null && b === null) return true;
	if (a === null || b === null) return false;
	return a.length === b.length && a.every((value, index) => value === b[index]);
}

function viewFilterSummary(selected, allLabel, options) {
	if (selected === null) return allLabel;
	if (!selected.length) return "None";
	if (selected.length === 1) {
		const match = options.find((option) => option.value === selected[0]);
		return match ? match.label : selected[0];
	}
	return `${selected.length} selected`;
}

function viewFilterOptionValues(options) {
	return options.map((option) => option.value);
}

function normalizeViewFilterSelection(values, options) {
	if (values === null) return null;
	const optionValues = viewFilterOptionValues(options);
	if (!values.length) return [];
	if (values.length >= optionValues.length) return null;
	const allowed = new Set(optionValues);
	return values.filter((value) => allowed.has(value));
}

function sanitizeViewFilters() {
	for (const meta of Object.values(VIEW_FILTER_META)) {
		const options = meta.options();
		viewState[meta.key] = normalizeViewFilterSelection(viewState[meta.key], options);
	}
}

function renderViewFilter(kind) {
	const meta = VIEW_FILTER_META[kind];
	const root = viewFilterRoot(kind);
	if (!root || !meta) return;

	const options = meta.options();
	const selected = viewState[meta.key];
	const selectedSet = new Set(Array.isArray(selected) ? selected : []);
	const allMode = selected === null;

	root.querySelector(".view-ms__options").innerHTML = `
		<label class="view-ms__option view-ms__option--all check-label">
			<input type="checkbox" class="view-ms__cb-all">
			<span>${escapeAttr(meta.allLabel)}</span>
		</label>
		<div class="view-ms__divider" aria-hidden="true"></div>
		${options.map((option) => {
			const checked = allMode || selectedSet.has(option.value) ? " checked" : "";
			return `<label class="view-ms__option check-label">
				<input type="checkbox" class="view-ms__cb" value="${escapeAttr(option.value)}"${checked}>
				<span>${escapeAttr(option.label)}</span>
			</label>`;
		}).join("")}
	`;

	const selectAllCb = root.querySelector(".view-ms__cb-all");
	if (selectAllCb) {
		selectAllCb.checked = allMode;
		selectAllCb.indeterminate = viewFilterApplies(selected);
	}

	const valueEl = root.querySelector(".view-ms__value");
	if (valueEl) {
		valueEl.textContent = viewFilterSummary(selected, meta.allLabel, options);
	}
}

function refreshViewSelects() {
	sanitizeViewFilters();
	for (const kind of Object.keys(VIEW_FILTER_META)) {
		renderViewFilter(kind);
	}
}

function setViewMenuOpen(root, open) {
	const menu = root.querySelector(".view-ms__menu");
	const trigger = root.querySelector(".view-ms__trigger");
	if (!menu || !trigger) return;
	menu.hidden = !open;
	trigger.setAttribute("aria-expanded", open ? "true" : "false");
}

function closeAllViewMenus() {
	for (const menu of document.querySelectorAll(".view-ms__menu:not([hidden])")) {
		const root = menu.closest(".view-ms");
		if (root) setViewMenuOpen(root, false);
	}
}

function toggleViewMenu(root) {
	const menu = root.querySelector(".view-ms__menu");
	if (!menu) return;
	const willOpen = menu.hidden;
	closeAllViewMenus();
	setViewMenuOpen(root, willOpen);
}

function refreshViewSection() {
	if (lastPipelines) {
		renderTable(lastPipelines);
		updateSummary(lastPipelines);
	} else {
		showLoadingTable();
	}
	renderTargetGroups(new Set(getSelectedTargets()));
	renderServerGroups(new Set(getSelectedServers()));
}

function setViewFilterValues(kind, values) {
	const meta = VIEW_FILTER_META[kind];
	if (!meta) return;
	const options = meta.options();
	const next = normalizeViewFilterSelection(values, options);
	const current = viewState[meta.key];
	if (arraysEqual(current, next)) return;
	viewState[meta.key] = next;
	renderViewFilter(kind);
	refreshViewSection();
}

function bindViewControls() {
	document.addEventListener("click", (event) => {
		const trigger = event.target.closest(".view-ms__trigger");
		if (trigger) {
			event.preventDefault();
			event.stopPropagation();
			toggleViewMenu(trigger.closest(".view-ms"));
			return;
		}
		if (!event.target.closest(".view-ms")) {
			closeAllViewMenus();
		}
	});

	document.addEventListener("change", (event) => {
		const selectAllCb = event.target.closest(".view-ms__cb-all");
		const checkbox = event.target.closest(".view-ms__cb");
		if (!selectAllCb && !checkbox) return;
		const root = event.target.closest(".view-ms");
		if (!root) return;

		const kind = root.dataset.filter;
		const meta = VIEW_FILTER_META[kind];
		if (!meta) return;

		if (selectAllCb) {
			setViewFilterValues(kind, selectAllCb.checked ? null : []);
			return;
		}

		const options = meta.options();
		const optionValues = viewFilterOptionValues(options);
		let selected = viewState[meta.key];
		if (selected === null) {
			if (!checkbox.checked) {
				selected = optionValues.filter((value) => value !== checkbox.value);
			} else {
				return;
			}
		} else {
			selected = Array.isArray(selected) ? [...selected] : [];
			if (checkbox.checked) {
				if (!selected.includes(checkbox.value)) {
					selected.push(checkbox.value);
				}
			} else {
				selected = selected.filter((value) => value !== checkbox.value);
			}
		}
		setViewFilterValues(kind, selected);
	});
}

function bindPageActions() {
	document.addEventListener("click", (event) => {
		const collapseBtn = event.target.closest(".card-collapse-btn");
		if (collapseBtn) {
			const section = collapseBtn.closest(".card--collapsible");
			if (section) toggleCardSection(section.id);
			return;
		}

		const filterPill = event.target.closest("#status_summary .filter-pill");
		if (filterPill) {
			const filter = filterPill.dataset.filter;
			setStatusFilter(filter === "all" ? null : filter);
			return;
		}

		const serverCommandBtn = event.target.closest("[data-server-command]");
		if (serverCommandBtn) {
			sendServerCommand(serverCommandBtn.dataset.serverCommand);
			return;
		}

		const commandBtn = event.target.closest("[data-command]");
		if (commandBtn) {
			sendCommand(commandBtn.dataset.command);
		}
	});

	const toolbarBindings = [
		["#section_service_control .command-toolbar", "select_all", toggleAll],
		["#section_server_control .command-toolbar", "select_all_servers", toggleAllServers],
	];
	for (const [selector, selectAllId, handler] of toolbarBindings) {
		const toolbar = document.querySelector(selector);
		if (!toolbar) continue;
		toolbar.addEventListener("change", (event) => {
			if (event.target.id === selectAllId) {
				handler(event.target);
			}
		});
	}

	$("target_groups").addEventListener("change", (event) => {
		if (event.target.classList.contains("group-toggle")) {
			toggleGroup(event.target);
			return;
		}
		if (event.target.classList.contains("target-cb")) {
			updateSelectionHint();
		}
	});

	$("server_groups").addEventListener("change", (event) => {
		if (event.target.classList.contains("server-group-toggle")) {
			toggleServerGroup(event.target);
			return;
		}
		if (event.target.classList.contains("server-cb")) {
			syncServerCheckbox(event.target);
		}
	});
}

function handleEscapeKey(event) {
	if (event.key !== "Escape") return;
	closeAllViewMenus();
	closeCameraLive();
	closePlcTagModal();
	closePingModal();
	closeCommandModal();
	const appModal = $("app_modal");
	if (!appModal.classList.contains("hidden")) {
		closeAppModal(appModal.dataset.mode === "alert");
	}
}

// --- Target selection ---

function targetGroupPanelHtml(groupKey, names, selected) {
	const safeGroup = escapeAttr(groupKey);
	const items = names.map((name) => {
		const checked = selected.has(name) ? " checked" : "";
		return `<label class="check-label target-item">
			<input type="checkbox" class="target-cb" data-group="${safeGroup}" id="${escapeAttr(name)}"${checked}>
			${escapeHtml(name)}
		</label>`;
	}).join("");
	return `<div class="group-panel" data-group="${safeGroup}">
		<div class="group-panel-header">
			<label class="check-label group-panel-title">
				<input type="checkbox" class="group-toggle" data-group="${safeGroup}">
				${escapeHtml(groupKey)}
			</label>
			<span class="group-panel-count">${names.length} services</span>
		</div>
		<div class="group-panel-body">${items}</div>
	</div>`;
}

function statusFilteredNames(names) {
	if (!statusFilter || !lastPipelines) return names;
	return names.filter((name) => {
		const entry = lastPipelines[name];
		return entry && matchesFilter(entry, name);
	});
}

function renderTargetGroups(preserveSelected) {
	const selected = preserveSelected || new Set(getSelectedTargets());
	const html = currentGroups()
		.map(([groupKey, names]) => [groupKey, statusFilteredNames(names)])
		.filter(([, names]) => names.length)
		.map(([groupKey, names]) => targetGroupPanelHtml(groupKey, names, selected))
		.join("");
	$("target_groups").innerHTML = html;
	updateSelectionHint();
}

function getSelectedTargets() {
	return [...document.querySelectorAll(".target-cb:checked")].map((cb) => cb.id);
}

function syncGroupToggles(toggleSelector, itemSelector) {
	for (const toggle of document.querySelectorAll(toggleSelector)) {
		const group = toggle.dataset.group;
		const items = [...document.querySelectorAll(`${itemSelector}[data-group="${group}"]`)];
		const checked = items.filter((cb) => cb.checked).length;
		toggle.checked = checked > 0 && checked === items.length;
		toggle.indeterminate = checked > 0 && checked < items.length;
	}
}

function updateSelectionHint() {
	const count = getSelectedTargets().length;
	$("selection_hint").textContent = `${count} selected`;
	$("select_all").checked =
		count > 0 && count === document.querySelectorAll(".target-cb").length;
	syncGroupToggles(".group-toggle", ".target-cb");
}

function toggleAll(source) {
	for (const cb of document.querySelectorAll(".target-cb")) {
		cb.checked = source.checked;
	}
	updateSelectionHint();
}

function toggleGroup(source) {
	const group = source.dataset.group;
	for (const cb of document.querySelectorAll(`.target-cb[data-group="${group}"]`)) {
		cb.checked = source.checked;
	}
	updateSelectionHint();
}

// --- Server selection ---

function currentServerGroups() {
	const groups = [];
	for (const [groupKey, names] of currentGroups()) {
		const byIp = new Map();
		for (const name of statusFilteredNames(names)) {
			const ip = (pipelineInfo(name).server_ip || "").trim();
			if (!ip) continue;
			if (!byIp.has(ip)) {
				byIp.set(ip, []);
			}
			byIp.get(ip).push(name);
		}
		if (!byIp.size) continue;
		// IP order follows first appearance in the filtered pipeline list.
		const servers = [...byIp.entries()].map(([ip, pipelines]) => ({ ip, pipelines }));
		groups.push([groupKey, servers]);
	}
	return groups;
}

function serverGroupPanelHtml(groupKey, servers, selected) {
	const safeGroup = escapeAttr(groupKey);
	const items = servers.map(({ ip, pipelines }) => {
		const checked = selected.has(ip) ? " checked" : "";
		const count = pipelines.length;
		const title = escapeAttr(pipelines.join(", "));
		return `<label class="check-label target-item" title="${title}">
			<input type="checkbox" class="server-cb" data-group="${safeGroup}" value="${escapeAttr(ip)}"${checked}>
			<span class="server-item-ip">${escapeHtml(ip)}</span>
			<span class="server-item-meta">${count} service${count === 1 ? "" : "s"}</span>
		</label>`;
	}).join("");
	return `<div class="group-panel" data-group="${safeGroup}">
		<div class="group-panel-header">
			<label class="check-label group-panel-title">
				<input type="checkbox" class="server-group-toggle" data-group="${safeGroup}">
				${escapeHtml(groupKey)}
			</label>
			<span class="group-panel-count">${servers.length} server${servers.length === 1 ? "" : "s"}</span>
		</div>
		<div class="group-panel-body">${items}</div>
	</div>`;
}

function renderServerGroups(preserveSelected) {
	const selected = preserveSelected || new Set(getSelectedServers());
	const html = currentServerGroups()
		.map(([groupKey, servers]) => serverGroupPanelHtml(groupKey, servers, selected))
		.join("");
	$("server_groups").innerHTML = html;
	updateServerSelectionHint();
}

function getSelectedServers() {
	return [...new Set(
		[...document.querySelectorAll(".server-cb:checked")].map((cb) => cb.value),
	)];
}

function uniqueServerIps() {
	return [...new Set(
		[...document.querySelectorAll(".server-cb")].map((cb) => cb.value),
	)];
}

function setServerIpChecked(ip, checked) {
	for (const cb of document.querySelectorAll(".server-cb")) {
		if (cb.value === ip) {
			cb.checked = checked;
		}
	}
}

function updateServerSelectionHint() {
	const selected = getSelectedServers();
	const total = uniqueServerIps().length;
	const count = selected.length;
	$("server_selection_hint").textContent = `${count} selected`;
	$("select_all_servers").checked = count > 0 && count === total;
	$("select_all_servers").indeterminate = count > 0 && count < total;
	syncGroupToggles(".server-group-toggle", ".server-cb");
}

function toggleAllServers(source) {
	const checked = source.checked;
	for (const ip of uniqueServerIps()) {
		setServerIpChecked(ip, checked);
	}
	updateServerSelectionHint();
}

function toggleServerGroup(source) {
	const group = source.dataset.group;
	const checked = source.checked;
	const ips = new Set(
		[...document.querySelectorAll(`.server-cb[data-group="${group}"]`)].map((cb) => cb.value),
	);
	for (const ip of ips) {
		setServerIpChecked(ip, checked);
	}
	updateServerSelectionHint();
}

function syncServerCheckbox(source) {
	setServerIpChecked(source.value, source.checked);
	updateServerSelectionHint();
}

function renderServerCommandResults(command, results) {
	const label = commandLabel(command);
	$("command_modal_title").textContent = "Server Control";
	$("command_modal_hint").textContent = label;
	$("command_modal_overall").classList.add("hidden");
	$("command_modal_overall").innerHTML = "";
	const lines = (results || []).map((entry) => {
		const tone = entry.ok ? "ok" : "err";
		const response = entry.response || (entry.ok ? "Scheduled" : "Failed");
		return `<div class="command-result-line command-result-line--${tone}">
			<span class="command-result-host">${escapeHtml(entry.server_ip || "")}</span>
			<span class="command-result-response">${escapeHtml(response)}</span>
		</div>`;
	}).join("");
	$("command_modal_list").innerHTML = lines || `<div class="command-result-line">No results</div>`;
	setCommandModalActions(true);
	openCommandModal();
}

async function sendServerCommand(command) {
	const servers = getSelectedServers();
	if (!servers.length) {
		showAlertModal("Select at least one server.", { title: "Server Control" });
		return;
	}
	const label = commandLabel(command);
	const confirmed = await showConfirmModal(
		`${label} ${servers.length} server(s)? This affects the whole host, not just services.`,
		{ title: "Server Control", okLabel: label, tone: "err" },
	);
	if (!confirmed) {
		return;
	}

	setCommandButtonsDisabled(true);
	$("command_modal_title").textContent = "Server Control";
	$("command_modal_hint").textContent = label;
	$("command_modal_overall").classList.add("hidden");
	$("command_modal_list").innerHTML = servers.map((ip) => `
		<div class="command-result-line">
			<span class="command-result-host">${escapeHtml(ip)}</span>
			<span class="command-result-response">Sending…</span>
		</div>
	`).join("");
	setCommandModalActions(false);
	openCommandModal();

	postJson(
		urls.manageServers,
		{ command, servers },
		30000,
		{
			onload: (status, data) => {
				setCommandButtonsDisabled(false);
				renderServerCommandResults(command, (data && data.results) || []);
				if (status !== 200 && status !== 409) {
					showAlertModal("Server command failed.", { title: "Server Control", tone: "err" });
				}
			},
			onerror: () => {
				setCommandButtonsDisabled(false);
				renderServerCommandResults(command, servers.map((ip) => ({
					server_ip: ip,
					ok: false,
					response: "request failed",
				})));
				showAlertModal("Server command failed.", { title: "Server Control", tone: "err" });
			},
			ontimeout: () => {
				setCommandButtonsDisabled(false);
				renderServerCommandResults(command, servers.map((ip) => ({
					server_ip: ip,
					ok: false,
					response: "timed out",
				})));
				showAlertModal("Server command timed out.", { title: "Server Control", tone: "err" });
			},
		},
	);
}

// --- Service control helpers ---

function setCommandButtonsDisabled(disabled) {
	for (const id of COMMAND_BTN_IDS) {
		$(id).disabled = disabled;
	}
	for (const id of SERVER_COMMAND_BTN_IDS) {
		$(id).disabled = disabled;
	}
}

function commandLabel(command) {
	if (COMMAND_LABELS[command]) return COMMAND_LABELS[command];
	const name = String(command || "").replace(/^(service|server):/, "");
	return name ? name.charAt(0).toUpperCase() + name.slice(1) : "";
}

function commandHostLabel(pipelineName, entry) {
	const serverIp = (entry && entry.server_ip)
		|| pipelineInfo(pipelineName).server_ip
		|| "";
	if (serverIp) {
		return `${escapeAttr(pipelineName)} @ ${escapeAttr(serverIp)}`;
	}
	return escapeAttr(pipelineName);
}

function commandExpectedSec(command) {
	if (command === "service:status") return 20;
	if (command === "service:stop") return 120;
	if (command === "service:restart") return 90;
	if (command === "update") return 600;
	return 60;
}

function commandPipelinePhase(entry) {
	if (entry && entry.status === "done") return "done";
	return (entry && entry.phase) || "queued";
}

function commandPipelineElapsed(entry, nowMs) {
	if (!entry || entry.phase !== "active" || !entry.started_at) return 0;
	return Math.max(0, Math.floor(nowMs / 1000 - entry.started_at));
}

function commandPipelinePercent(command, phase, elapsedSec) {
	if (phase === "queued") return 0;
	if (phase === "done") return 100;
	const expected = commandExpectedSec(command);
	return Math.min(95, Math.round((elapsedSec / expected) * 100));
}

function commandProgressLabel(command, phase) {
	const action = commandLabel(command);
	if (phase === "queued") return "Waiting";
	if (phase === "active") return action;
	return "Done";
}

function pipelineCountsAsDoneForUpdate(name, entry, updateContext) {
	if (commandPipelinePhase(entry) !== "done") return false;
	if (!pipelineInfo(name).is_sys_monitor) return true;
	const ip = pipelineInfo(name).server_ip || "";
	const serverStatus = (updateContext?.servers || {})[ip];
	return sysMonitorUpdateComplete(name, serverStatus);
}

function computeOverallCommandProgress(command, order, byPipeline, nowMs, updateContext) {
	const progressOrder = order.length ? order : ["_"];
	const total = Math.max(1, progressOrder.length);
	let sum = 0;
	let doneCount = 0;
	let activeLabel = "";
	for (const name of progressOrder) {
		const entry = byPipeline[name] || { phase: "queued" };
		const phase = commandPipelinePhase(entry);
		const countsDone = command === "update"
			? pipelineCountsAsDoneForUpdate(name, entry, updateContext)
			: phase === "done";
		if (countsDone) {
			sum += 100;
			doneCount += 1;
			continue;
		}
		if (phase === "active" || (phase === "done" && command === "update" && pipelineInfo(name).is_sys_monitor)) {
			const elapsed = commandPipelineElapsed(entry, nowMs);
			const progressPhase = phase === "done" ? "active" : phase;
			sum += commandPipelinePercent(command, progressPhase, elapsed);
			if (command === "update" && updateContext) {
				const ip = pipelineInfo(name).server_ip || "";
				const serverStatus = (updateContext.servers || {})[ip];
				activeLabel = updateOverallActiveLabel(serverStatus, order);
			} else if (command === "update" && entry.response) {
				activeLabel = entry.response;
			} else {
				activeLabel = commandProgressLabel(command, progressPhase);
			}
		}
	}
	const allDone = doneCount === total;
	return {
		pct: allDone ? 100 : Math.min(99, Math.round(sum / total)),
		doneCount,
		total,
		activeLabel,
		unitLabel: "services",
	};
}

function renderOverallCommandProgress(command, order, byPipeline, nowMs, finished, updateContext) {
	const panel = $("command_modal_overall");
	if (finished || !order.length) {
		panel.classList.add("hidden");
		panel.innerHTML = "";
		return;
	}
	const { pct, doneCount, total, activeLabel, unitLabel } = computeOverallCommandProgress(
		command, order, byPipeline, nowMs, updateContext,
	);
	const label = activeLabel || commandLabel(command);
	const unit = unitLabel || "services";
	panel.classList.remove("hidden");
	panel.innerHTML = `
		<div class="command-result-progress-row">
			<span>${escapeAttr(label)}… ${doneCount}/${total} ${unit}</span>
			<span>${pct}%</span>
		</div>
		<div class="command-result-progress-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
			<div class="command-result-progress-fill" style="width:${pct}%"></div>
		</div>
	`;
}

function commandEntryLine(entry, command) {
	const host = commandHostLabel(entry.pipeline, entry);
	const phase = commandPipelinePhase(entry);
	const hasResult = entry.response || entry.error;
	if (phase === "done") {
		if (command === "update") {
			const tone = entry.step === "done" || entry.ok ? "ok" : "err";
			const detail = escapeAttr(entry.error || entry.response || "unknown");
			return { tone, host, pending: false, detail };
		}
		if (command === "service:status") {
			const tone = commandResponseTone(entry, command);
			const detail = escapeAttr(entry.response || entry.error || "unknown");
			return { tone, host, pending: false, detail };
		}
		const tone = commandResponseTone(entry, command);
		const detail = hasResult
			? `${escapeAttr(entry.service || "")}: ${escapeAttr(entry.response || "unknown")}`
			: "Done";
		return { tone, host, pending: false, detail };
	}
	if (command === "update") {
		const detail = entry.error
			? escapeAttr(entry.error)
			: (entry.response
				? escapeAttr(entry.response)
				: (phase === "queued" ? "Waiting…" : "Starting…"));
		return {
			tone: entry.error ? "err" : "pending",
			host,
			pending: !entry.response && !entry.error,
			detail,
		};
	}
	return {
		tone: "pending",
		host,
		pending: true,
		detail: "",
	};
}

function showCommandProgress(command, targets) {
	const nowSec = Date.now() / 1000;
	activeServiceJob = {
		active: true,
		done: false,
		results: targets.map((name, idx) => ({
			...commandJobBase(name),
			status: "pending",
			phase: idx === 0 ? "active" : "queued",
			started_at: idx === 0 ? nowSec : null,
			response: command === "update"
				? (idx === 0 ? "Sending update request…" : "Waiting…")
				: "",
		})),
	};
	renderCommandJobProgress(command, activeServiceJob, targets, Date.now());
	startCommandProgressTick();
}

function renderCommandJobProgress(command, job, targets, nowMs, updateServers) {
	activeServiceJob = job;
	const results = Array.isArray(job.results) ? job.results : [];
	const byPipeline = resultsByPipeline(results);
	const order = targets.length ? targets : (job.pipelines || []);
	const tick = nowMs || Date.now();
	const items = order.map((name) => commandEntryLine(
		byPipeline[name] || { pipeline: name, status: "pending", phase: "queued" },
		command,
	));
	$("command_modal_list").innerHTML = renderCommandListHtml(items);

	const allDone = Boolean(job.done);
	const updateContext = command === "update"
		? { servers: updateServers || mergeUpdateServers(lastUpdateStatusData?.servers || {}) }
		: null;
	renderOverallCommandProgress(command, order, byPipeline, tick, allDone, updateContext);
	if (allDone) {
		stopCommandProgressTick();
		const allOk = results.length > 0 && results.every((entry) => entry.ok);
		setCommandModalFinishedTitle(command, allOk);
		setCommandModalActions(true);
		return;
	}

	const failedDone = results.filter((entry) => entry.status === "done" && entry.ok === false).length;
	const title = failedDone > 0
		? `${commandLabel(command)} in progress (${failedDone} failed)`
		: `${commandLabel(command)} in progress`;
	$("command_modal_title").textContent = title;
	$("command_modal_hint").textContent = "";
	setCommandModalActions(false);
	openCommandModal();
	startCommandProgressTick();
}

function fetchServiceCommandStatus() {
	if (!activeServiceJobId) return;
	fetchJsonGet(
		`${urls.serviceCommandStatus}?job_id=${encodeURIComponent(activeServiceJobId)}&_=${Date.now()}`,
		(job, status) => {
			if (status === 404) {
				stopServicePolling();
				setCommandButtonsDisabled(false);
				renderCommandResults(activeServiceCommand, [{
					pipeline: activeServiceTargets.join(", "),
					server_ip: "",
					service: "",
					ok: false,
					response: "job not found",
				}]);
				return;
			}
			if (status !== 200) return;
			const command = activeServiceCommand || job.command;
			renderCommandJobProgress(command, job, activeServiceTargets);
			if (job.done) {
				stopServicePolling();
				setCommandButtonsDisabled(false);
				if (command !== "service:status") {
					startFreshCollect();
				}
			}
		},
	);
}

function startServiceCommandPolling(jobId, command, targets) {
	stopServicePolling();
	activeServiceJobId = jobId;
	activeServiceCommand = command;
	activeServiceTargets = targets.slice();
	resumeServiceCommandPolling();
}

function resumeServiceCommandPolling() {
	if (!activeServiceJobId || document.hidden) return;
	if (servicePollTimer) return;
	fetchServiceCommandStatus();
	servicePollTimer = setInterval(fetchServiceCommandStatus, JOB_POLL_MS);
}

function renderCommandResults(command, results) {
	stopCommandProgressTick();
	const entries = Array.isArray(results) ? results : [];
	const allOk = entries.length > 0 && entries.every((entry) => entry.ok);
	setCommandModalFinishedTitle(command, allOk);
	hideCommandModalOverall();
	if (!entries.length) {
		$("command_modal_list").innerHTML = renderCommandListHtml([{
			tone: "err",
			host: "Service control",
			pending: false,
			detail: "No response from server.",
		}]);
	} else {
		$("command_modal_list").innerHTML = renderCommandListHtml(
			entries.map((entry) => commandEntryLine({ ...entry, status: "done" }, command)),
		);
	}
	setCommandModalActions(true);
	openCommandModal();
}

// --- Service control ---

function failUpdateCommand(targets, message) {
	stopUpdatePolling();
	setCommandButtonsDisabled(false);
	const results = targets.map((name) => pipelineUpdateEntry(name, {
		step: "failed",
		error: message,
	}));
	renderCommandJobProgress("update", { done: true, results }, targets, Date.now());
}

function failServiceCommand(command, targets, message, alertMessage) {
	stopServicePolling();
	setCommandButtonsDisabled(false);
	renderCommandResults(command, [{
		pipeline: targets.join(", "),
		server_ip: "",
		service: "",
		ok: false,
		response: message,
	}]);
	if (alertMessage) {
		showAlertModal(alertMessage, { title: "Service Control", tone: "err" });
	}
	startFreshCollect();
}

function handleManageDockerResponse(command, targets, status, data) {
	if (command === "update") {
		if (status === 409) {
			if (data.busy && data.busy.length) {
				startUpdateModalPolling(targets);
				return;
			}
			if (data.errors && Object.keys(data.errors).length) {
				setCommandButtonsDisabled(false);
				const byPipeline = buildUpdateByPipeline(targets, {}, data.errors);
				renderCommandJobProgress(
					"update",
					{ done: true, results: Object.values(byPipeline) },
					targets,
					Date.now(),
				);
				return;
			}
			setCommandButtonsDisabled(false);
			showAlertModal("Update could not be started.", { title: "Update", tone: "err" });
			return;
		}
		if (status !== 200) {
			setCommandButtonsDisabled(false);
			showAlertModal("Command failed.", { title: "Update", tone: "err" });
			return;
		}
		startUpdateModalPolling(targets);
		return;
	}
	if (status === 202 && data.job_id) {
		startServiceCommandPolling(data.job_id, command, targets);
		return;
	}
	setCommandButtonsDisabled(false);
	const results = data.results || [];
	renderCommandResults(command, results);
	if (command !== "service:status") {
		startFreshCollect();
	}
	if ((status !== 200 && status !== 409) || !results.length) {
		showAlertModal("Command failed.", { title: "Service Control", tone: "err" });
	}
}

async function sendCommand(command) {
	const targets = getSelectedTargets();
	if (!targets.length) {
		showAlertModal("Select at least one service.", { title: "Service Control" });
		return;
	}
	const label = commandLabel(command);
	let gitRefs = {};
	if (command === "update") {
		const confirmed = await showUpdateConfirmModal(targets);
		if (confirmed === null) {
			return;
		}
		gitRefs = confirmed;
	} else if (command !== "service:status") {
		const confirmed = await showConfirmModal(
			`${label} ${targets.length} service(s)?`,
			{ title: "Service Control", okLabel: label },
		);
		if (!confirmed) {
			return;
		}
	}

	setCommandButtonsDisabled(true);
	stopAllPolling();
	showCommandProgress(command, targets);
	const body = { command: command, lst_images: targets };
	if (command === "update" && Object.keys(gitRefs).length) {
		body.git_refs = gitRefs;
	}
	postJson(
		urls.manageDocker,
		body,
		command === "update" ? 900000 : 30000,
		{
			onload: (status, data) => handleManageDockerResponse(command, targets, status, data),
			onerror: () => {
				if (command === "update") {
					failUpdateCommand(targets, "request failed");
					return;
				}
				failServiceCommand(command, targets, "request failed", "Command failed.");
			},
			ontimeout: () => {
				if (command === "update") {
					failUpdateCommand(targets, "timed out");
					return;
				}
				failServiceCommand(command, targets, "timed out", "Command timed out.");
			},
		},
	);
}

function onVisibilityChange() {
	if (document.hidden) {
		pauseAllPolling();
		closeCameraLive();
		return;
	}
	fetchStatus(false);
	if (activeUpdateTargets) {
		if (lastUpdateStatusData) {
			renderUpdateModalProgress(lastUpdateStatusData, Date.now());
		}
		fetchUpdateStatus();
		if (!updatePollTimer) {
			updatePollTimer = setInterval(fetchUpdateStatus, JOB_POLL_MS);
		}
		startCommandProgressTick();
	}
	if (activeServiceJobId) {
		resumeServiceCommandPolling();
		if (!activeUpdateTargets) {
			startCommandProgressTick();
		}
	}
	if (!pollTimer) {
		schedulePoll();
	}
}

// --- Init ---

function bindOverlayDismiss(modalId, closeBtnId, onClose) {
	const modal = $(modalId);
	$(closeBtnId).addEventListener("click", onClose);
	modal.addEventListener("click", (event) => {
		if (event.target.classList.contains("overlay__backdrop")) {
			onClose();
		}
	});
}

bindOverlayDismiss("camera_live_modal", "camera_live_close", closeCameraLive);
bindOverlayDismiss("plc_tag_modal", "plc_tag_modal_close", closePlcTagModal);
bindOverlayDismiss("drift_images_modal", "drift_images_close", closeDriftImagesModal);
$("plc_tag_modal_list").addEventListener("click", (event) => {
	const driftImagesBtn = event.target.closest(".drift-images-btn");
	if (!driftImagesBtn) return;
	event.preventDefault();
	openDriftImagesModal({
		camUid: driftImagesBtn.dataset.camUid,
		pipeline: driftImagesBtn.dataset.pipeline
			|| (plcTagModalState && plcTagModalState.pipeline)
			|| "",
		title: driftImagesBtn.dataset.camName || "Drift Images",
		isDrifted: driftImagesBtn.dataset.camDrifted,
	});
});
$("drift_images_reset").addEventListener("click", () => {
	if (!driftImagesState) return;
	resetCameraDrift(
		driftImagesState.camUid,
		driftImagesState.pipeline || "",
		$("drift_images_reset"),
	);
});
$("drift_anchor_toggle").addEventListener("click", () => {
	if (!driftImagesState) return;
	setDriftAnchorEditMode(!driftImagesState.anchorEdit);
});
$("drift_anchor_clear").addEventListener("click", () => {
	if (!driftImagesState) return;
	undoLastManualPinpoint();
});
$("drift_anchor_clear_all").addEventListener("click", () => {
	if (!driftImagesState) return;
	clearAllManualPinpoints();
});
(() => {
	const beforeImg = $("drift_images_before");
	const media = beforeImg && beforeImg.closest(".drift-images-media");
	if (!media) return;
	media.addEventListener("click", (event) => {
		if (!driftImagesState || !driftImagesState.anchorEdit) return;
		const coords = driftImageCoordsFromEvent(beforeImg, event.clientX, event.clientY);
		if (!coords) return;
		event.preventDefault();
		event.stopPropagation();
		addDriftManualAnchor(coords.x, coords.y);
	});
})();
bindOverlayDismiss("ping_modal", "ping_modal_close", closePingModal);
$("ping_modal_open").addEventListener("click", () => {
	const url = $("ping_modal_open").dataset.url;
	if (url) {
		window.open(url, "_blank", "noopener,noreferrer");
	}
});
bindOverlayDismiss("command_modal", "command_modal_close", closeCommandModal);
$("command_modal_ok").addEventListener("click", closeCommandModal);
bindOverlayDismiss("app_modal", "app_modal_close", () => {
	closeAppModal($("app_modal").dataset.mode === "alert");
});
$("app_modal_ok").addEventListener("click", () => closeAppModal(true));
$("app_modal_cancel").addEventListener("click", () => closeAppModal(false));

$("status_body").addEventListener("click", (event) => {
	const liveBtn = event.target.closest(".device-chip-live");
	if (liveBtn) {
		event.preventDefault();
		openCameraLive(
			liveBtn.dataset.pipeline,
			parseInt(liveBtn.dataset.cam, 10),
			liveBtn.dataset.title || "",
		);
		return;
	}
	const plcTagBtn = event.target.closest(".device-chip-plc-tag, .device-chip-drift-tag");
	if (plcTagBtn) {
		event.preventDefault();
		let tags = [];
		let meta = {};
		try {
			tags = JSON.parse(plcTagBtn.dataset.plcTags || "[]");
		} catch (_e) {
			tags = [];
		}
		try {
			meta = JSON.parse(plcTagBtn.dataset.plcMeta || "{}");
		} catch (_e) {
			meta = {};
		}
		const isDrift = plcTagBtn.classList.contains("device-chip-drift-tag");
		const pipeline = plcTagBtn.dataset.pipeline
			|| plcTagBtn.closest(".status-item")?.dataset?.pipeline
			|| "";
		openPlcTagModal({
			title: plcTagBtn.dataset.plcTitle
				|| plcTagBtn.getAttribute("title")
				|| (isDrift ? "Drift Cameras" : "PLC Tags"),
			url: plcTagBtn.dataset.plcUrl || "",
			tags,
			meta,
			pipeline,
		});
		return;
	}
	const scrollBtn = event.target.closest(".device-chip-btn[data-pipeline]:not([data-cam])");
	if (scrollBtn) {
		event.preventDefault();
		navigateToPipeline(scrollBtn.dataset.pipeline);
		return;
	}
	const ipBtn = event.target.closest(".ip-update-btn");
	if (ipBtn) {
		event.preventDefault();
		updateServerIp(ipBtn.dataset.pipeline, event);
		return;
	}
	const btn = event.target.closest(".device-chip-btn");
	if (!btn) return;
	const host = btn.dataset.host;
	if (host) {
		pingDevice(host, event);
	}
});

function syncStickyOffset() {
	const toolbar = document.querySelector(".sticky-toolbar");
	const height = toolbar ? Math.ceil(toolbar.getBoundingClientRect().height) : 0;
	document.documentElement.style.setProperty("--sticky-offset", `${height}px`);
	updateStickySectionHeads();
}

function stickyOffsetPx() {
	const raw = getComputedStyle(document.documentElement).getPropertyValue("--sticky-offset");
	const n = parseFloat(raw);
	return Number.isFinite(n) ? n : 0;
}

function updateStickySectionHeads() {
	const offset = stickyOffsetPx();
	document.querySelectorAll(".card--collapsible > .card-section-head").forEach((head) => {
		const sentinel = head.previousElementSibling;
		if (!sentinel || !sentinel.classList.contains("card-sticky-sentinel")) return;
		const stuck = sentinel.getBoundingClientRect().top < offset + 0.5;
		head.classList.toggle("is-stuck", stuck);
	});
}

function bindStickyOffset() {
	document.querySelectorAll(".card--collapsible > .card-section-head").forEach((head) => {
		if (head.previousElementSibling?.classList.contains("card-sticky-sentinel")) return;
		const sentinel = document.createElement("div");
		sentinel.className = "card-sticky-sentinel";
		sentinel.setAttribute("aria-hidden", "true");
		head.parentElement.insertBefore(sentinel, head);
	});
	syncStickyOffset();
	window.addEventListener("scroll", updateStickySectionHeads, { passive: true });
	window.addEventListener("resize", syncStickyOffset);
	const toolbar = document.querySelector(".sticky-toolbar");
	if (toolbar && typeof ResizeObserver !== "undefined") {
		const observer = new ResizeObserver(() => syncStickyOffset());
		observer.observe(toolbar);
	}
}

document.addEventListener("keydown", handleEscapeKey);
document.addEventListener("visibilitychange", onVisibilityChange);
bindPageActions();
bindViewControls();
bindStickyOffset();
restoreCardSections();
refreshViewSelects();
renderTargetGroups();
renderServerGroups();
fetchUpdateStatus();
startFreshCollect();
