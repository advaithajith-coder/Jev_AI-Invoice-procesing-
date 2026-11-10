import React, { useEffect, useRef, useState } from 'react';

const API = '/api/v1/evaluations';
const stages = {
  extraction: ['Extraction', 'Read the invoice correctly'],
  classification: ['Classification', 'Choose the right accounting'],
  tax: ['Tax treatment', 'Verify treatment and amounts'],
  routing: ['Exception routing', 'Send issues to the right team'],
};
const labels = {not_run: 'Not run', passed: 'Checks passed', differences: 'Differences', failed: 'Failed'};
const industryName = key => key === 'legacy_technology' ? 'Original benchmark' : key.split('_').map(word => word[0].toUpperCase() + word.slice(1)).join(' ');
const percent = (n) => n == null ? 'Not measured' : `${(n * 100).toFixed(1)}%`;
const money = (n) => n == null ? 'Not recorded' : `$${Number(n).toFixed(5)}`;
const duration = (n) => n == null ? 'Not recorded' : n < 1000 ? `${Math.round(n)} ms` : n < 60000 ? `${(n / 1000).toFixed(1)} s` : `${(n / 60000).toFixed(1)} min`;
const valueText = (v) => v === null ? 'Not specified' : v === undefined ? 'Missing' : typeof v === 'object' ? JSON.stringify(v, null, 2) : typeof v === 'boolean' ? (v ? 'Yes' : 'No') : String(v);
const fieldName = (path) => path.split('.').map(p => /^\d+$/.test(p) ? `Item ${Number(p) + 1}` : p.replaceAll('_', ' ')).join(' / ');
async function api(path, options = {}) {
  const response = await fetch(API + path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || (body.detail ? 'The saved output has an invalid format. Check the response file and try again.' : 'Unable to load evaluations. Try refreshing.'));
  return body;
}
const jsonPost = (body) => ({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
function Badge({status}) { return <span className={`ev-badge ev-badge--${status}`}>{labels[status] || status}</span>; }

function CaseDetail({run, caseId, onClose, onSaved}) {
  const dialog = useRef(null), file = useRef(null);
  const [data, setData] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [onlyDifferences, setOnlyDifferences] = useState(false), [tab, setTab] = useState('comparison');
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const previousFocus = document.activeElement;
    dialog.current.showModal();
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = previous; previousFocus?.focus(); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    api(`/runs/${run}/cases/${caseId}`, {signal: controller.signal}).then(setData).catch(e => { if (e.name !== 'AbortError') setError(e.message); });
    return () => controller.abort();
  }, [run, caseId, version]);
  async function saveOutput(event) {
    const selected = event.target.files[0];
    event.target.value = '';
    if (!selected) return;
    setError(''); setBusy(true);
    try {
      if (selected.size > 10 * 1024 * 1024) throw new Error('Choose a response file smaller than 10 MB.');
      let parsed;
      try { parsed = JSON.parse(await selected.text()); } catch { throw new Error('This file is not valid JSON. Choose a saved generated response.'); }
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('The response must be a JSON object.');
      const envelope = 'actual' in parsed || parsed.status === 'failed' ? parsed : {actual: parsed};
      await api(`/runs/${run}/cases/${caseId}/output`, jsonPost(envelope));
      setVersion(v => v + 1); onSaved();
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  const generated = data?.generated, score = generated?.score;
  const rows = score?.details || data?.expected?.assertions || [];
  const visible = onlyDifferences ? rows.filter(row => row.passed === false) : rows;
  return <dialog ref={dialog} className="ev-dialog" aria-labelledby="ev-case-title" onCancel={onClose}>
    <div className="ev-dialog-head">
      <div><p>{stages[data?.stage]?.[0] || 'Invoice case'}</p><h2 id="ev-case-title">{data?.invoice_number || caseId}</h2></div>
      <button className="ev-close" type="button" onClick={onClose} aria-label="Close case comparison">×</button>
    </div>
    <div className="ev-dialog-body">
      {error && <div className="ev-error" role="alert">{error}</div>}
      {!data ? <p role="status">Loading the comparison…</p> : <>
        <div className="ev-case-actions"><span>Layout {data.style_id} <span className="ev-separator">/</span> {rows.length} field checks</span>
          <div><a className="button" href={`${API}/runs/${run}/cases/${caseId}/document`} target="_blank" rel="noreferrer">View invoice</a>
            {!generated && <button className="button button--primary" disabled={busy} onClick={() => file.current.click()}>{busy ? 'Saving…' : 'Import saved output'}</button>}
            <input ref={file} className="sr-only" type="file" accept=".json,application/json" aria-label="Saved generated output" onChange={saveOutput}/></div>
        </div>
        {!generated && <div className="ev-note"><strong>Expected answers are ready.</strong> No generated output has been saved. Importing a saved response compares it here; it does not run a model. Include cost and timing in the response file to record them.</div>}
        {generated?.error && <div className="ev-error">Processing failed: {generated.error}</div>}
        <div className="ev-tabs" aria-label="Case views">{['comparison', 'usage', 'responses'].map(key => <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{ {comparison: 'Field comparison', usage: 'Cost & time', responses: 'Saved responses'}[key]}</button>)}</div>
        {tab === 'comparison' && <>
          <div className="ev-comparison-top"><p>{score ? `${score.fields_passed} of ${score.fields_checked} field checks passed` : 'Review the expected values before testing.'}</p>
            {score && <label className="ev-checkbox"><input type="checkbox" checked={onlyDifferences} onChange={e => setOnlyDifferences(e.target.checked)}/> Differences only</label>}</div>
          <div className="ev-table-scroll"><table className="ev-comparison"><thead><tr><th>Invoice field</th><th>Expected</th><th>Generated</th><th>Check</th></tr></thead><tbody>
            {visible.map((row, index) => <tr key={`${row.path}-${index}`} className={row.passed === false ? 'ev-row-difference' : ''}>
              <th scope="row">{fieldName(row.path)}{row.critical && <small>Critical field</small>}</th>
              <td>{row.mode === 'nonempty' ? 'Supporting evidence required' : row.mode === 'one_of' ? row.expected.map(valueText).join(' or ') : valueText(row.expected)}</td>
              <td className={!score ? 'ev-muted' : ''}>{!score ? 'Awaiting output' : row.missing ? 'Missing from output' : valueText(row.actual)}</td>
              <td>{!score ? <Badge status="not_run"/> : <Badge status={row.passed ? 'passed' : 'differences'}/>}</td>
            </tr>)}
            {!visible.length && <tr><td colSpan="4">No differences found.</td></tr>}
          </tbody></table></div>
          <p className="ev-footnote">Evidence presence is checked automatically. A person still needs to confirm that the explanation supports the recommendation.</p>
        </>}
        {tab === 'usage' && <><div className="ev-case-usage"><div><span>Case processing time</span><strong>{duration(generated?.wall_time_ms)}</strong></div><div><span>Recorded model calls</span><strong>{generated ? generated.calls.length : 'Not run'}</strong></div></div>
          {!!generated?.calls.length ? <div className="ev-table-scroll"><table><thead><tr><th>Stage / model</th><th>Model time</th><th>Wait</th><th>Tokens in / out</th><th>Cost ($)</th></tr></thead><tbody>{generated.calls.map((call, index) => <tr key={index}><th scope="row">{call.stage}<small>{call.provider} / {call.model} ({call.status})</small></th><td>{duration(call.latency_ms)}</td><td>{duration(call.wait_ms)}</td><td>{call.input_tokens ?? 'Unknown'} / {call.output_tokens ?? 'Unknown'}</td><td>{money(call.cost_usd)}<small>{call.cost_basis}</small></td></tr>)}</tbody></table></div> : <p className="ev-note">No model measurements have been recorded for this case.</p>}
          <p className="ev-footnote">Missing measurements are unknown, not zero. Local model charges exclude hardware and electricity.</p></>}
        {tab === 'responses' && <div className="ev-responses"><section><h3>Frozen expected output</h3><pre>{JSON.stringify(data.expected, null, 2)}</pre></section><section><h3>Generated output</h3><pre>{generated ? JSON.stringify(generated.actual, null, 2) : 'No generated output saved yet.'}</pre>{generated?.versions && <><h4>Model and prompt versions</h4><pre>{JSON.stringify(generated.versions, null, 2)}</pre></>}</section></div>}
      </>}
    </div>
  </dialog>;
}

export default function Evaluations() {
  const [runs, setRuns] = useState([]), [runId, setRunId] = useState(''), [summary, setSummary] = useState(null), [cases, setCases] = useState([]);
  const [error, setError] = useState(''), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [stage, setStage] = useState('all'), [status, setStatus] = useState('all'), [search, setSearch] = useState(''), [page, setPage] = useState(0), [caseId, setCaseId] = useState(null);
  const [industry, setIndustry] = useState('all'), [split, setSplit] = useState('all');
  const [refresh, setRefresh] = useState(0), [newRun, setNewRun] = useState(false), [runName, setRunName] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    api('/runs', {signal: controller.signal}).then(items => {
      setRuns(items);
      const requested = new URLSearchParams(window.location.search).get('run');
      setRunId(old => items.some(r => r.run_id === old) ? old : items.find(r => r.run_id === requested)?.run_id || items[0]?.run_id || '');
      if (!items.length) setLoading(false);
    }).catch(e => { if (e.name !== 'AbortError') { setError(e.message); setLoading(false); } });
    return () => controller.abort();
  }, [refresh]);
  useEffect(() => {
    if (!runId) return;
    const controller = new AbortController();
    setLoading(true); setError(''); setSummary(null); setCases([]);
    Promise.all([api(`/runs/${runId}`, {signal: controller.signal}), api(`/runs/${runId}/cases`, {signal: controller.signal})]).then(([s, c]) => {setSummary(s); setCases(c); setLoading(false);}).catch(e => {if (e.name !== 'AbortError') {setError(e.message); setLoading(false);}});
    const url = new URL(window.location.href); url.searchParams.set('run', runId); window.history.replaceState(null, '', url);
    return () => controller.abort();
  }, [runId, refresh]);
  useEffect(() => setPage(0), [stage, status, search, industry, split, runId]);
  useEffect(() => {setCaseId(null); setIndustry('all'); setSplit('all');}, [runId]);
  async function prepare(event) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const datasetId = runs.find(r => r.run_id === runId)?.dataset_id || (await api('/datasets/import', {method: 'POST'})).dataset_id;
      const created = await api('/runs', jsonPost({dataset_id: datasetId, name: runName.trim() || 'Invoice evaluation'}));
      setRuns(old => [created, ...old]); setRunId(created.run_id); setNewRun(false); setRunName('');
    } catch(e) {setError(e.message);} finally {setBusy(false);}
  }
  const filtered = cases.filter(c => (stage === 'all' || c.stage === stage) && (status === 'all' || c.status === status) && (industry === 'all' || c.industry === industry) && (split === 'all' || c.split === split) && `${c.invoice_number} ${c.case_id}`.toLowerCase().includes(search.toLowerCase()));
  const shown = filtered.slice(page * 12, (page + 1) * 12);
  const attempted = summary ? summary.completed + summary.failed : 0;
  const percentage = summary?.cases ? attempted / summary.cases * 100 : 0;
  return <div className="ev-workspace">
    <header className="ev-heading"><div><h1>Evaluations</h1><p>See what the models got right, what changed, and what it cost.</p></div><div className="ev-heading-actions"><button className="button" onClick={() => setRefresh(v => v + 1)} disabled={loading}>Refresh</button><button className="button button--primary" onClick={() => setNewRun(v => !v)}>Prepare new run</button></div></header>
    {error && <div className="ev-error" role="alert">{error} <button type="button" onClick={() => setRefresh(v => v + 1)}>Try again</button></div>}
    {newRun && <form className="ev-new-run" onSubmit={prepare}><label>Run name<input autoFocus value={runName} maxLength={200} onChange={e => setRunName(e.target.value)} placeholder="For example, baseline with revised prompts" required/></label><p>Creates an empty comparison run using the frozen dataset. No invoices are processed.</p><div><button className="button button--primary" disabled={busy}>{busy ? 'Preparing…' : 'Prepare run'}</button><button type="button" className="button" onClick={() => setNewRun(false)}>Cancel</button></div></form>}
    {loading && <div className="ev-loading" role="status">Loading evaluation records…</div>}
    {!loading && !runs.length && !error && <div className="ev-empty"><h2>Prepare your first comparison</h2><p>Bring the existing invoice test set into a frozen run, ready for generated outputs.</p><button className="button button--primary" onClick={() => setNewRun(true)}>Prepare baseline</button></div>}
    {summary && <>
      <div className="ev-runbar"><label>Evaluation run<select value={runId} onChange={e => {setRunId(e.target.value); setStage('all'); setStatus('all');}}>{runs.map(r => <option key={r.run_id} value={r.run_id}>{r.name}</option>)}</select></label><span>Prepared {new Date(summary.created_at).toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'})}</span><a className="button" href={`${API}/runs/${runId}/report.csv`}>Download report</a></div>
      <section className="ev-overview" aria-label="Run overview"><div className="ev-progress-block"><div className="ev-progress-title"><h2>{!attempted ? 'Your baseline is ready' : attempted === summary.cases ? 'Every case has a result' : 'Evaluation in progress'}</h2><span className="ev-ready">{!attempted ? 'Not run' : attempted === summary.cases ? 'Recorded' : 'In progress'}</span></div><p>{!attempted ? 'Expected answers are frozen. Choose an invoice below to inspect its checks before testing.' : `${summary.completed} completed, ${summary.failed} failed and ${summary.not_run} awaiting an output.`}</p><div className="ev-completion"><strong>{attempted}<span> / {summary.cases}</span></strong><span>cases recorded</span></div><div className="ev-progress" role="progressbar" aria-label="Cases recorded" aria-valuemin={0} aria-valuemax={summary.cases} aria-valuenow={attempted}><span style={{width: `${percentage}%`}}/></div></div>
        <dl className="ev-totals"><div><dt>Field accuracy</dt><dd>{percent(summary.field_accuracy)}</dd><small>{summary.fields_checked ? `${summary.fields_passed} of ${summary.fields_checked} checks passed` : 'Available after comparison'}</small></div><div><dt>Recorded model cost</dt><dd>{money(summary.known_cost_usd)}</dd><small>{summary.calls_missing_cost || summary.cases_missing_call_telemetry ? 'Partial cost records' : 'Estimated or reported API cost'}</small></div><div><dt>Processing time</dt><dd>{duration(summary.sum_case_wall_time_ms)}</dd><small>Sum of recorded case durations</small></div><div><dt>Critical fields passed</dt><dd>{summary.completed ? `${summary.critical_pass_cases} / ${summary.completed}` : 'Not measured'}</dd><small>Cases with all critical checks passed</small></div></dl>
      </section>
      <section className="ev-segments" aria-label="Filter by evaluation segment">{Object.entries(stages).filter(([key]) => summary.segments[key]).map(([key, [name, description]]) => {
        const group = summary.segments[key], groupCases = cases.filter(c => c.stage === key);
        return <button key={key} className={`ev-segment ${stage === key ? 'is-selected' : ''}`} aria-pressed={stage === key} onClick={() => setStage(stage === key ? 'all' : key)}><span className="ev-segment-title">{name}<span>{group.cases}</span></span><span className="ev-segment-desc">{description}</span><span className="ev-ticks" aria-hidden="true">{groupCases.map(c => <i key={c.case_id} className={`ev-tick--${c.status}`}/>)}</span><span className="ev-segment-result">{group.completed || group.failed ? `${group.completed + group.failed} recorded / ${percent(group.field_accuracy)} accuracy` : `${group.cases} cases ready to test`}</span></button>;
      })}</section>
      {summary.classification_dimensions?.purchase_category?.checked > 0 && <section className="ev-models" aria-label="Classification quality"><h2>Classification quality</h2><div className="ev-table-scroll"><table><thead><tr><th>Check</th><th>Passed</th><th>Accuracy</th></tr></thead><tbody>{Object.entries(summary.classification_dimensions).map(([key, metric]) => <tr key={key}><th scope="row">{{gl_coding: 'GL account', purchase_category: 'Purchase category', supporting_evidence: 'Supporting evidence present'}[key]}</th><td>{metric.passed} / {metric.checked}</td><td>{percent(metric.accuracy)}</td></tr>)}</tbody></table></div><p className="ev-footnote">A correct GL account can still have an incomplete category. Evidence must also be checked for relevance by a reviewer.</p></section>}
      {Object.keys(summary.industries || {}).length > 1 && <section className="ev-models ev-industries" aria-label="Industry comparison"><h2>Accuracy by industry</h2><p>Compare each stage separately. Figures include all recorded cases in this run; missing outputs are not counted as correct.</p><div className="ev-table-scroll"><table><thead><tr><th>Industry</th><th>Recorded</th>{Object.entries(stages).map(([key, [name]]) => <th key={key}>{name}</th>)}<th>Cost ($)</th><th>Time</th></tr></thead><tbody>{Object.entries(summary.industries).map(([key, group]) => <tr key={key}><th scope="row"><button className="ev-clear" onClick={() => {setIndustry(key); setStage('all'); setStatus('all'); setSplit('all'); document.getElementById('ev-case-register')?.scrollIntoView({block: 'start'});}}>{industryName(key)}</button></th><td>{group.completed + group.failed} / {group.cases}</td>{Object.keys(stages).map(stageKey => <td key={stageKey}>{percent(group.segments[stageKey]?.field_accuracy)}<small>{group.segments[stageKey]?.fields_passed || 0} / {group.segments[stageKey]?.fields_checked || 0} checks</small></td>)}<td>{money(group.known_cost_usd)}</td><td>{duration(group.sum_case_wall_time_ms)}</td></tr>)}</tbody></table></div><div className="ev-split-results">{Object.entries(summary.splits || {}).map(([key, group]) => <p key={key}><strong>{key === 'holdout' ? 'Reserved tests' : key === 'development' ? 'Development tests' : 'Original tests'}</strong> {group.completed + group.failed} / {group.cases} recorded · {percent(group.field_accuracy)} accuracy</p>)}</div><p className="ev-footnote">20 cases per industry is an initial diagnostic sample. Reserved cases should be run after improvements are frozen. PO inheritance and model suggestions are separate sources; evidence relevance still requires review.</p></section>}
      <section className="ev-register" id="ev-case-register"><div className="ev-register-heading"><div><h2>Invoice cases</h2><p>Open a case to compare its expected and generated values. Totals above describe the full run.</p></div><span>{filtered.length} {filtered.length === 1 ? 'case' : 'cases'}</span></div>
        <div className="ev-filters"><label><span className="sr-only">Search invoices</span><input type="search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search invoice number…"/></label><label><span className="sr-only">Filter result status</span><select value={status} onChange={e => setStatus(e.target.value)}><option value="all">All results</option>{Object.entries(labels).map(([key, text]) => <option key={key} value={key}>{text}</option>)}</select></label>{stage !== 'all' && <button className="ev-clear" onClick={() => setStage('all')}>Clear {stages[stage][0]} filter ×</button>}</div>
        {Object.keys(summary.industries || {}).length > 1 && <div className="ev-filters ev-industry-filters"><label>Industry<select value={industry} onChange={e => setIndustry(e.target.value)}><option value="all">All industries</option>{Object.keys(summary.industries).map(key => <option key={key} value={key}>{industryName(key)}</option>)}</select></label><label>Test set<select value={split} onChange={e => setSplit(e.target.value)}><option value="all">All cases</option><option value="development">Development tests</option><option value="holdout">Reserved tests</option></select></label></div>}
        <div className="ev-table-scroll"><table className="ev-cases"><thead><tr><th>Invoice</th><th>Segment</th><th>Result</th><th>Field checks</th><th>Time</th><th>Cost ($)</th></tr></thead><tbody>{shown.map(c => <tr key={c.case_id}><th scope="row"><button onClick={() => setCaseId(c.case_id)}>{c.invoice_number}</button><small>Layout {c.style_id}{c.industry !== 'legacy_technology' && ` / ${industryName(c.industry)}`}{c.split === 'holdout' && ' / Reserved'}</small></th><td>{stages[c.stage]?.[0]}</td><td><Badge status={c.status}/>{c.manual_review_required && <small>Evidence needs review</small>}</td><td>{c.fields_checked == null ? `${c.assertions} expected` : `${c.fields_passed} / ${c.fields_checked}`}</td><td>{c.wall_time_ms == null ? '—' : duration(c.wall_time_ms)}</td><td>{c.known_cost_usd == null ? '—' : money(c.known_cost_usd)}{c.cost_incomplete && <small>Incomplete</small>}</td></tr>)}{!shown.length && <tr><td colSpan="6" className="ev-no-matches">No cases match these filters. Try another invoice number or result.</td></tr>}</tbody></table></div>
        <div className="ev-pagination"><span>{filtered.length ? `${page * 12 + 1}–${Math.min((page + 1) * 12, filtered.length)} of ${filtered.length}` : '0 cases'}</span><div><button className="button" disabled={page === 0} onClick={() => setPage(p => p - 1)}>Previous</button><button className="button" disabled={(page + 1) * 12 >= filtered.length} onClick={() => setPage(p => p + 1)}>Next</button></div></div>
      </section>
      <section className="ev-models"><h2>Model cost & time</h2>{Object.keys(summary.models).length ? <div className="ev-table-scroll"><table><thead><tr><th>Provider / model / stage</th><th>Calls</th><th>Failed</th><th>Model time</th><th>Recorded cost</th></tr></thead><tbody>{Object.entries(summary.models).map(([name, m]) => <tr key={name}><th scope="row">{name}</th><td>{m.calls}</td><td>{m.failed}</td><td>{duration(m.latency_ms)}</td><td>{money(m.known_cost_usd)}{m.missing_cost > 0 && <small>{m.missing_cost} calls missing cost</small>}</td></tr>)}</tbody></table></div> : <p>No model calls recorded yet. Cost and timing will appear alongside imported outputs.</p>}<p className="ev-footnote">A passed field check is not invoice approval. Missing measurements remain unknown. Evidence meaning still needs human review.</p></section>
    </>}
    {caseId && <CaseDetail key={`${runId}-${caseId}`} run={runId} caseId={caseId} onClose={() => setCaseId(null)} onSaved={() => setRefresh(v => v + 1)}/>}
  </div>;
}
