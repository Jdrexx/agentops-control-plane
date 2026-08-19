const $ = id => document.getElementById(id);
const state = {tools: [], templates: [], projects: [], steps: [], selectedStep: null, selectedRun: null, runs: [], lastEventId: 0, liveSocket: null, liveReconnectTimer: null, liveEnabled: false, authEnabled: false, datasets: [], evaluations: [], selectedEvals: new Set(), evalPollTimer: null};
const requireLogin = () => {
  disconnectLive();
  $('login-error').textContent = '';
  if (!$('login-dialog').open) $('login-dialog').showModal();
  $('login-token').focus();
};
const api = async (path, options = {}) => {
  const token = sessionStorage.getItem('agentops-token');
  const headers = {'Content-Type': 'application/json', ...(token ? {Authorization: `Bearer ${token}`} : {})};
  const response = await fetch(path, {headers, ...options});
  if (response.status === 401) { requireLogin(); throw new Error('Sign in required'); }
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.detail || `Request failed: ${response.status}`); }
  return response.json();
};
const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const pretty = value => JSON.stringify(value, null, 2);
const toast = message => { $('toast').textContent = message; $('toast').classList.add('show'); setTimeout(() => $('toast').classList.remove('show'), 2400); };
const showJSON = (title, value) => { $('dialog-title').textContent = title; $('dialog-json').textContent = pretty(value); $('json-dialog').showModal(); };

async function loadOperations() {
  const [stats, runs, approvals, trends] = await Promise.all([api('/api/stats'), api('/api/runs?limit=20'), api('/api/approvals?status=pending'), api('/api/stats/trends')]);
  state.runs = runs; $('total').textContent = stats.total_runs; $('success').textContent = `${Math.round(stats.success_rate * 100)}%`; $('failed').textContent = stats.failed_runs; $('pending').textContent = stats.pending_approvals; $('tokens').textContent = (stats.input_tokens + stats.output_tokens).toLocaleString(); $('cost').textContent = `$${stats.total_cost_usd.toFixed(4)}`;
  $('runs').className = runs.length ? '' : 'empty';
  $('runs').innerHTML = runs.length ? runs.map(run => `<button class="run" data-run="${run.id}"><span class="status ${esc(run.status)}">${esc(run.status.replace('_',' '))}</span><span><b>Run #${run.id}</b><small>Workflow ${run.workflow_id}${run.parent_run_id ? ` · replay of #${run.parent_run_id}` : ''}</small></span><time>${new Date(run.started_at).toLocaleString()}</time><span class="view-link">→</span></button>`).join('') : 'No runs yet. Load the demo or build a workflow.';
  document.querySelectorAll('[data-run]').forEach(el => el.onclick = () => showTrace(Number(el.dataset.run)));
  $('approvals').className = approvals.length ? '' : 'empty';
  $('approvals').innerHTML = approvals.length ? approvals.map(item => `<div class="approval"><small>RUN #${item.run_id}</small><p>${esc(item.prompt)}</p><div class="approval-actions"><button data-approval="${item.id}" data-decision="approved">Approve & resume</button><button class="reject" data-approval="${item.id}" data-decision="rejected">Reject</button></div></div>`).join('') : 'No actions need approval.';
  document.querySelectorAll('[data-approval]').forEach(el => el.onclick = () => decide(el.dataset.approval, el.dataset.decision));
  const maxDuration = Math.max(...trends.map(item => item.duration_ms), 1);
  $('trends').innerHTML = trends.length ? trends.map(item => `<button class="trend-bar ${esc(item.status)} h${Math.max(1, Math.ceil(item.duration_ms / maxDuration * 10))}" title="Run #${item.id}: ${item.duration_ms.toFixed(2)} ms · $${item.cost_usd.toFixed(4)}"></button>`).join('') : '<span class="empty">Run workflows to build a trend.</span>';
}

async function showTrace(id) {
  const run = await api(`/api/runs/${id}`); state.selectedRun = run; $('trace-title').textContent = `Run #${run.id} · ${run.status}`; $('replay').disabled = false; $('compare').disabled = state.runs.length < 2;
  const totalMs = run.spans.reduce((sum, span) => sum + span.duration_ms, 0);
  $('run-summary').innerHTML = `<div class="summary"><button data-json="input"><small>INPUT</small><b>Inspect JSON</b></button><button data-json="output"><small>OUTPUT</small><b>Inspect JSON</b></button><span><small>DURATION</small><b>${totalMs.toFixed(2)} ms</b></span><span><small>STEPS</small><b>${run.spans.length}</b></span></div>`;
  document.querySelectorAll('[data-json]').forEach(el => el.onclick = () => showJSON(`Run #${run.id} ${el.dataset.json}`, run[el.dataset.json]));
  $('trace').className = ''; $('trace').innerHTML = run.spans.map((span, index) => `<button class="span" data-span="${index}"><span class="span-index">${index + 1}</span><span><b>${esc(span.step_name)}</b><small class="status ${esc(span.status)}">${esc(span.tool)} · ${esc(span.status)}</small></span><code class="${span.error ? 'error' : ''}">${esc(span.error || pretty(span.output))}</code><time>${span.duration_ms.toFixed(2)} ms</time></button>`).join('') || '<div class="empty">This run has no spans.</div>';
  document.querySelectorAll('[data-span]').forEach(el => el.onclick = () => showJSON(`Step ${Number(el.dataset.span) + 1}`, run.spans[el.dataset.span]));
}

async function loadBuilder() {
  [state.tools, state.templates, state.projects] = await Promise.all([api('/api/tools'), api('/api/templates'), api('/api/projects')]);
  $('templates').innerHTML = state.templates.map(template => `<button class="tool" data-template="${esc(template.id)}"><b>${esc(template.name)}</b><small>${esc(template.description)}</small><span>＋</span></button>`).join('');
  $('toolbox').innerHTML = state.tools.map(tool => `<button class="tool" data-tool="${tool.name}"><b>${esc(tool.name)}</b><small>${esc(tool.description)}</small><span>＋</span></button>`).join('');
  $('project-select').innerHTML = state.projects.length ? state.projects.map(project => `<option value="${project.id}">${esc(project.name)}</option>`).join('') : '<option value="">Create a project via API or load demo</option>';
  document.querySelectorAll('[data-tool]').forEach(el => el.onclick = () => addStep(el.dataset.tool));
  document.querySelectorAll('[data-template]').forEach(el => el.onclick = () => applyTemplate(el.dataset.template));
}

async function applyTemplate(templateId) {
  if (!state.projects.length) throw new Error('Create a project or load the demo first');
  const projectId = Number($('project-select').value);
  const workflow = await api(`/api/templates/${templateId}?project_id=${projectId}`, {method:'POST'});
  state.steps = workflow.steps; $('workflow-name').value = workflow.name; state.selectedStep = null;
  renderSteps(); toast(`Created ${workflow.name} v${workflow.version}`);
}

function addStep(toolName) { const tool = state.tools.find(item => item.name === toolName); state.steps.push({name: toolName.replace('_', ' '), tool: toolName, config: structuredClone(tool.config)}); state.selectedStep = state.steps.length - 1; renderSteps(); }
function renderSteps() {
  $('steps').className = state.steps.length ? 'steps' : 'empty'; $('steps').innerHTML = state.steps.length ? state.steps.map((step, index) => `<div class="builder-step ${index === state.selectedStep ? 'selected' : ''}" data-step="${index}"><span>${index + 1}</span><b>${esc(step.name)}</b><small>${esc(step.tool)}</small><div><button class="mini" data-up="${index}" ${index === 0 ? 'disabled' : ''}>↑</button><button class="mini" data-down="${index}" ${index === state.steps.length - 1 ? 'disabled' : ''}>↓</button><button class="mini danger" data-remove="${index}">×</button></div></div>`).join('') : 'Add a tool to begin.';
  document.querySelectorAll('[data-step]').forEach(el => el.onclick = event => { if (!event.target.dataset.up && !event.target.dataset.down && !event.target.dataset.remove) { state.selectedStep = Number(el.dataset.step); renderSteps(); }});
  document.querySelectorAll('[data-up]').forEach(el => el.onclick = () => moveStep(Number(el.dataset.up), -1)); document.querySelectorAll('[data-down]').forEach(el => el.onclick = () => moveStep(Number(el.dataset.down), 1)); document.querySelectorAll('[data-remove]').forEach(el => el.onclick = () => { state.steps.splice(Number(el.dataset.remove), 1); state.selectedStep = null; renderSteps(); }); renderConfig();
}
function moveStep(index, delta) { [state.steps[index], state.steps[index + delta]] = [state.steps[index + delta], state.steps[index]]; state.selectedStep = index + delta; renderSteps(); }
function renderConfig() { const step = state.steps[state.selectedStep]; if (!step) { $('step-config').className = 'empty'; $('step-config').innerHTML = 'Select a workflow step.'; return; } $('step-config').className = 'config'; $('step-config').innerHTML = `<label>Step name<input id="step-name" value="${esc(step.name)}"></label><label>Tool<input value="${esc(step.tool)}" disabled></label><label>Configuration (JSON)<textarea id="step-json" rows="10">${esc(pretty(step.config))}</textarea></label><button id="apply-config">Apply configuration</button>`; $('apply-config').onclick = () => { try { step.name = $('step-name').value.trim(); step.config = JSON.parse($('step-json').value); if (!step.name) throw new Error('Step name is required'); toast('Step updated'); renderSteps(); } catch (error) { toast(error.message); }}; }

async function saveWorkflow(runAfter = false) { if (!state.projects.length) throw new Error('Create a project or load the demo first'); if (!state.steps.length) throw new Error('Add at least one step'); const workflow = await api('/api/workflows', {method:'POST', body:pretty({project_id:Number($('project-select').value), name:$('workflow-name').value, steps:state.steps})}); toast(`Saved ${workflow.name} v${workflow.version}`); if (runAfter) { const raw = prompt('Run input as JSON', '"Hello agent"'); if (raw !== null) { const run = await api(`/api/workflows/${workflow.id}/runs`, {method:'POST', body:pretty({input:JSON.parse(raw)})}); switchView('operations'); await loadOperations(); await showTrace(run.id); }} return workflow; }

async function loadQuality() {
  const [datasets, evaluations, workflows, projects] = await Promise.all([
    api('/api/datasets'), api('/api/evaluations'), api('/api/workflows'), api('/api/projects'),
  ]);
  state.datasets = datasets; state.evaluations = evaluations; state.workflows = workflows;
  renderDatasets(datasets, projects);
  renderEvaluations(evaluations);
  populateQualityForms(projects, workflows, datasets);
  const busy = evaluations.some(item => item.status === 'queued' || item.status === 'running');
  $('compare-evals').disabled = state.selectedEvals.size < 2;
  if (busy) { clearTimeout(state.evalPollTimer); state.evalPollTimer = setTimeout(loadQuality, 1500); }
}

function renderDatasets(datasets, projects) {
  const projectName = id => (projects.find(item => item.id === id) || {}).name || `Project ${id}`;
  $('datasets').className = datasets.length ? '' : 'empty';
  $('datasets').innerHTML = datasets.length ? datasets.map(item => `<div class="quality-row"><span><b>${esc(item.name)}</b><small>${item.cases.length} cases · ${esc(projectName(item.project_id))}</small></span><div class="row-actions"><button class="mini" data-dataset-json="${item.id}">JSON</button><button class="mini danger" data-dataset-del="${item.id}">Delete</button></div></div>`).join('') : 'No datasets yet. Create one below.';
  document.querySelectorAll('[data-dataset-json]').forEach(el => el.onclick = () => showJSON('Dataset', datasets.find(item => item.id === Number(el.dataset.datasetJson))));
  document.querySelectorAll('[data-dataset-del]').forEach(el => el.onclick = async () => {
    const item = datasets.find(entry => entry.id === Number(el.dataset.datasetDel));
    if (!confirm(`Delete dataset "${item.name}" and its evaluations?`)) return;
    await api(`/api/datasets/${item.id}`, {method:'DELETE'});
    toast('Dataset deleted');
    await loadQuality();
  });
}

function renderEvaluations(evaluations) {
  $('evaluations').className = evaluations.length ? '' : 'empty';
  $('evaluations').innerHTML = evaluations.length ? evaluations.map(item => {
    const gate = item.gate ? `<b class="gate ${esc(item.gate)}">${item.gate.toUpperCase()}</b>` : '';
    const status = item.status === 'completed' ? '' : `<b class="status ${esc(item.status)}">${esc(item.status)} ${item.completed_cases}/${item.total}</b>`;
    const cancel = item.status === 'queued' || item.status === 'running' ? `<button class="mini danger" data-eval-cancel="${item.id}">Cancel</button>` : '';
    const checked = state.selectedEvals.has(item.id) ? 'checked' : '';
    return `<div class="eval-row"><label><input type="checkbox" data-eval-check="${item.id}" ${checked}></label><button class="eval-main" data-eval-json="${item.id}"><span><b>Workflow ${item.workflow_id}</b><small>${new Date(item.created_at).toLocaleString()} · ${item.total} cases</small></span></button>${status}<b class="${item.pass_rate === 1 ? 'good' : ''}">${item.status === 'completed' ? Math.round(item.pass_rate * 100) + '%' : '…'}</b>${gate}${cancel}</div>`;
  }).join('') : 'No evaluations yet. Run one below.';
  document.querySelectorAll('[data-eval-check]').forEach(el => el.onchange = () => {
    const id = Number(el.dataset.evalCheck);
    if (el.checked) state.selectedEvals.add(id); else state.selectedEvals.delete(id);
    $('compare-evals').disabled = state.selectedEvals.size < 2;
  });
  document.querySelectorAll('[data-eval-json]').forEach(el => el.onclick = () => showJSON('Evaluation', evaluations.find(item => item.id === Number(el.dataset.evalJson))));
  document.querySelectorAll('[data-eval-cancel]').forEach(el => el.onclick = async () => {
    await api(`/api/evaluations/${el.dataset.evalCancel}/cancel`, {method:'POST'});
    toast('Evaluation cancelled');
    await loadQuality();
  });
}

function populateQualityForms(projects, workflows, datasets) {
  const projectOptions = projects.length ? projects.map(item => `<option value="${item.id}">${esc(item.name)}</option>`).join('') : '<option value="">No projects</option>';
  const keepDataset = $('dataset-project').value;
  $('dataset-project').innerHTML = projectOptions;
  if (projects.some(item => String(item.id) === keepDataset)) $('dataset-project').value = keepDataset;
  const keepEval = $('eval-project').value;
  $('eval-project').innerHTML = projectOptions;
  if (projects.some(item => String(item.id) === keepEval)) $('eval-project').value = keepEval;
  const projectId = Number($('eval-project').value) || 0;
  const projectWorkflows = workflows.filter(item => item.project_id === projectId);
  const projectDatasets = datasets.filter(item => item.project_id === projectId);
  $('eval-workflow').innerHTML = projectWorkflows.length ? projectWorkflows.map(item => `<option value="${item.id}">${esc(item.name)} v${item.version}</option>`).join('') : '<option value="">No workflows</option>';
  $('eval-dataset').innerHTML = projectDatasets.length ? projectDatasets.map(item => `<option value="${item.id}">${esc(item.name)}</option>`).join('') : '<option value="">No datasets</option>';
}

async function createDataset() {
  const projectId = Number($('dataset-project').value);
  const name = $('dataset-name').value.trim();
  let cases;
  try { cases = JSON.parse($('dataset-cases').value); } catch (_) { throw new Error('Cases must be valid JSON'); }
  if (!projectId) throw new Error('Choose a project');
  if (!name) throw new Error('Dataset name is required');
  if (!Array.isArray(cases) || !cases.length) throw new Error('Cases must be a non-empty array');
  await api('/api/datasets', {method:'POST', body: pretty({project_id: projectId, name, cases})});
  toast('Dataset created');
  $('dataset-name').value = ''; $('dataset-cases').value = '';
  await loadQuality();
}

async function runEvaluation() {
  const workflowId = Number($('eval-workflow').value);
  const datasetId = Number($('eval-dataset').value);
  if (!workflowId || !datasetId) throw new Error('Choose a workflow and dataset');
  const body = {workflow_id: workflowId, dataset_id: datasetId, execution: $('eval-execution').value};
  const passMin = parseFloat($('eval-pass-min').value);
  const maxCost = parseFloat($('eval-max-cost').value);
  if (!isNaN(passMin)) body.pass_rate_min = passMin;
  if (!isNaN(maxCost)) body.max_cost_usd = maxCost;
  const evaluation = await api('/api/evaluations', {method:'POST', body: pretty(body)});
  toast(evaluation.status === 'queued' ? 'Evaluation queued — watching progress' : `Evaluation complete: ${Math.round(evaluation.pass_rate * 100)}%`);
  await loadQuality();
}

async function compareEvaluations() {
  const [left, right] = [...state.selectedEvals];
  if (!left || !right) return;
  const comparison = await api(`/api/evaluations/${left}/compare/${right}`);
  $('eval-diff-panel').hidden = false;
  const delta = value => (value >= 0 ? '+' : '') + value.toFixed(1);
  const summary = `<div class="diff-summary"><span>Pass rate <b>${delta(comparison.pass_rate_delta * 100)}%</b></span><span>Cost Δ <b>${comparison.cost_delta_usd >= 0 ? '+' : ''}$${comparison.cost_delta_usd.toFixed(4)}</b></span><span>p95 Δ <b>${delta(comparison.p95_latency_delta_ms)}ms</b></span><span>Regressed <b class="red">${comparison.regressed}</b></span><span>Fixed <b class="good">${comparison.fixed}</b></span></div>`;
  const rows = comparison.cases.map(item => `<div class="diff-row ${esc(item.transition)}"><b>${esc(item.case_id)}</b><span class="badge">${esc(item.transition.replace('_', ' '))}</span><code>${esc(pretty(item.expected))}</code><small>${item.left ? (item.left.passed ? 'PASS' : 'FAIL') : '—'} → ${item.right ? (item.right.passed ? 'PASS' : 'FAIL') : '—'}</small></div>`).join('');
  $('eval-diff').className = '';
  $('eval-diff').innerHTML = summary + (rows || '<div class="empty">No case transitions.</div>');
}
async function decide(id, decision) { await api(`/api/approvals/${id}/decision`, {method:'POST', body:pretty({decision, note: decision === 'rejected' ? 'Rejected from dashboard' : ''})}); toast(decision === 'approved' ? 'Run approved and resumed' : 'Run rejected'); await loadOperations(); }
async function loadDemo(scenario, reset) {
  const result = await api('/api/demo/seed', {method:'POST', body: pretty({scenario, reset})});
  toast(result.blurb || 'Demo loaded');
  if (result.focus === 'quality') {
    switchView('quality');
    await loadQuality();
    if (result.pass_rates) toast(`v1 ${Math.round(result.pass_rates[0] * 100)}% pass -> v2 ${Math.round(result.pass_rates[1] * 100)}% pass`);
  } else {
    switchView('operations');
    await loadOperations();
    if (result.focus_run_id) await showTrace(result.focus_run_id);
  }
}
function switchView(id) { document.querySelectorAll('.view').forEach(el => el.classList.toggle('active', el.id === id)); document.querySelectorAll('.nav').forEach(el => el.classList.toggle('active', el.dataset.view === id)); if (id === 'builder') loadBuilder().catch(error => toast(error.message)); if (id === 'quality') loadQuality().catch(error => toast(error.message)); }

document.querySelectorAll('.nav').forEach(el => el.onclick = () => switchView(el.dataset.view)); $('demo-tour').onclick = () => loadDemo('tour', $('demo-reset').checked).catch(error => toast(error.message)); $('demo-quality').onclick = () => loadDemo('quality', $('demo-reset').checked).catch(error => toast(error.message)); $('demo-incident').onclick = () => loadDemo('incident', $('demo-reset').checked).catch(error => toast(error.message)); $('refresh').onclick = () => loadOperations().catch(error => toast(error.message)); $('create-dataset').onclick = () => createDataset().catch(error => toast(error.message)); $('run-eval').onclick = () => runEvaluation().catch(error => toast(error.message)); $('compare-evals').onclick = () => compareEvaluations().catch(error => toast(error.message)); $('close-diff').onclick = () => { $('eval-diff-panel').hidden = true; }; $('eval-project').onchange = () => { const workflows = state.workflows || []; const datasets = state.datasets || []; populateQualityForms(state.projects, workflows, datasets); }; $('dataset-project').onchange = () => { $('dataset-cases').focus(); }; $('clear-steps').onclick = () => { state.steps = []; state.selectedStep = null; renderSteps(); }; $('save-workflow').onclick = () => saveWorkflow(false).catch(error => toast(error.message)); $('save-run').onclick = () => saveWorkflow(true).catch(error => toast(error.message)); $('replay').onclick = async () => { const run = await api(`/api/runs/${state.selectedRun.id}/replay`, {method:'POST'}); await loadOperations(); await showTrace(run.id); toast('Replay completed'); };
$('compare').onclick = () => {
  const others = state.runs.filter(item => item.id !== state.selectedRun.id);
  if (!others.length) return toast('No other runs to compare');
  $('compare-select').innerHTML = others.map(item => `<option value="${item.id}">Run #${item.id} · ${esc(item.status.replace('_', ' '))} · workflow ${item.workflow_id}</option>`).join('');
  $('compare-dialog').showModal();
};
$('compare-confirm').onclick = async () => {
  const other = Number($('compare-select').value);
  $('compare-dialog').close();
  showJSON('Run comparison', await api(`/api/runs/${state.selectedRun.id}/compare/${other}`));
};
$('compare-cancel').onclick = () => $('compare-dialog').close();
$('login-form').onsubmit = async event => {
  event.preventDefault();
  sessionStorage.setItem('agentops-token', $('login-token').value);
  try {
    const actor = await api('/api/session');
    $('login-dialog').close(); $('login-token').value = ''; $('logout').hidden = false;
    toast(`Signed in as ${actor.name} (${actor.role})`); await loadOperations(); connectLive();
  } catch (error) { sessionStorage.removeItem('agentops-token'); $('login-error').textContent = 'That token was not accepted.'; }
};
$('logout').onclick = () => { sessionStorage.removeItem('agentops-token'); $('logout').hidden = true; requireLogin(); };
$('clear-stream').onclick = () => { $('live-output').textContent = 'Waiting for a streamed LLM run.'; };

async function bootstrap() {
  const health = await fetch('/api/ready');
  $('health-label').textContent = health.ok ? 'System ready' : 'System unavailable';
  const auth = await fetch('/api/auth/status').then(response => response.json());
  state.authEnabled = auth.enabled;
  if (auth.enabled) {
    if (!sessionStorage.getItem('agentops-token')) return requireLogin();
    try { await api('/api/session'); $('logout').hidden = false; } catch (_) { return; }
  }
  await loadOperations(); connectLive();
}
bootstrap().catch(error => toast(error.message));

function disconnectLive() {
  state.liveEnabled = false;
  if (state.liveReconnectTimer !== null) clearTimeout(state.liveReconnectTimer);
  state.liveReconnectTimer = null;
  const socket = state.liveSocket;
  state.liveSocket = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'session ended');
}

function connectLive() {
  disconnectLive();
  state.liveEnabled = true;
  const token = sessionStorage.getItem('agentops-token') || '';
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${protocol}://${location.host}/api/live`);
  state.liveSocket = socket;
  socket.onopen = () => {
    if (state.authEnabled) socket.send(JSON.stringify({type: 'authenticate', token}));
  };
  socket.onmessage = event => {
    const message = JSON.parse(event.data); const stats = message.stats;
    $('total').textContent = stats.total_runs;
    $('success').textContent = `${Math.round(stats.success_rate * 100)}%`;
    $('failed').textContent = stats.failed_runs;
    $('pending').textContent = stats.pending_approvals;
    $('tokens').textContent = (stats.input_tokens + stats.output_tokens).toLocaleString();
    $('cost').textContent = `$${stats.total_cost_usd.toFixed(4)}`;
    const fresh = message.events.filter(item => item.id > state.lastEventId);
    if (message.events.length) state.lastEventId = Math.max(...message.events.map(item => item.id));
    fresh.filter(item => item.event_type === 'llm.started').forEach(() => { $('live-output').textContent = ''; });
    fresh.filter(item => item.event_type === 'llm.chunk').forEach(item => { $('live-output').textContent += item.payload.text; });
  };
  socket.onclose = () => {
    if (state.liveSocket !== socket) return;
    state.liveSocket = null;
    if (state.liveEnabled) state.liveReconnectTimer = setTimeout(connectLive, 3000);
  };
}
