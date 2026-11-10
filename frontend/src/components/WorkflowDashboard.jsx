import React, { useEffect, useState, useRef } from 'react';
import { domToReact } from 'html-react-parser';

const walk = (node, test) => [ ...(test(node) ? [node] : []), ...(node.children || []).flatMap(child => walk(child, test)) ];
const text = node => node?.type === 'text' ? node.data : (node?.children || []).map(text).join('');
const classText = (node, name) => text(walk(node, n => n.attribs?.class?.split(' ').includes(name))[0]).trim();
const countKeys = { review:'pending_review', approved:'approved', export:'exported', rejected:'rejected', failed:'failed' };
const phases = [
  { name:'Capture & extract', detail:'Turn documents into structured data', keys:['arrive','read','vision','extract'] },
  { name:'Validate & understand', detail:'Match records. Apply intelligence.', keys:['doc','md','jev','coding'] },
  { name:'Check & decide', detail:'Your controls, consistently applied', keys:['finance','tax','policy'] },
];
const duration = ms => ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
function Icon({ kind = 'file' }) {
  const paths = { file:'M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h4', spark:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z', check:'m5 12 4 4L19 6', route:'M5 4v10a4 4 0 0 0 4 4h10M5 8h10a4 4 0 0 0 4-4M15 14l4 4-4 4', shield:'m12 3 8 3v5c0 5-8 10-8 10S4 16 4 11V6zM8 11l3 3 5-5' };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[kind]}/></svg>;
}
export default function WorkflowDashboard({ element, options }) {
  const initial = walk(element, n => n.attribs?.id === 'workflow-data')[0];
  const [data, setData] = useState(() => JSON.parse(text(initial)));
  const [connection, setConnection] = useState('Live updates');
  const [paused, setPaused] = useState(false);
  const graph = useRef(null);
  const [paths, setPaths] = useState([]);
  useEffect(() => {
    const draw = () => {
      const host = graph.current;
      if (!host) return;
      const bounds = host.getBoundingClientRect();
      const routes = [['arrive','read'],['read','vision'],['vision','extract'],['extract','doc'],['doc','md'],['md','jev'],['jev','coding'],['coding','finance'],['finance','tax'],['tax','policy'],['policy','approved','success'],['approved','export','success'],['policy','review','warning'],['review','person','warning']];
      setPaths(routes.flatMap(([from,to,tone]) => {
        const a = host.querySelector('[data-flow-node="'+from+'"]')?.getBoundingClientRect();
        const b = host.querySelector('[data-flow-node="'+to+'"]')?.getBoundingClientRect();
        if (!a || !b) return [];
        const vertical = Math.abs(a.left - b.left) < 20;
        const x1 = (vertical ? a.left+a.width/2 : a.right)-bounds.left;
        const y1 = (vertical ? a.bottom : a.top+a.height/2)-bounds.top;
        const x2 = (vertical ? b.left+b.width/2 : b.left)-bounds.left;
        const y2 = (vertical ? b.top : b.top+b.height/2)-bounds.top;
        const middle = (x1+x2)/2;
        return [{id:from+to,tone: tone || '',d:vertical ? 'M'+x1+','+y1+' L'+x2+','+y2 : 'M'+x1+','+y1+' C'+middle+','+y1+' '+middle+','+y2+' '+x2+','+y2}];
      }));
    };
    const observer = new ResizeObserver(draw);
    observer.observe(graph.current);
    draw();
    return () => observer.disconnect();
  }, [data]);
  const nodes = walk(element, n => n.attribs?.['data-node'] !== undefined).map(n => ({
    key:n.attribs['data-node'], step:n.attribs['data-step'], title:classText(n,'wf-title'), actor:classText(n,'wf-actor'), number:classText(n,'wf-num'), info:walk(n, c => c.attribs?.['data-info'] !== undefined).length > 0,
  }));
  const dialogs = walk(element, n => n.name === 'dialog');
  useEffect(() => {
    if (paused) return;
    const controller = new AbortController();
    let timer;
    const refresh = async () => {
      try {
        const response = await fetch('/workflow/live', {signal:controller.signal, headers:{Accept:'application/json'}});
        if (!response.ok) throw new Error('unavailable');
        const next = await response.json();
        if (!controller.signal.aborted) { setData(next); setConnection('Live updates'); }
      } catch { if (!controller.signal.aborted) setConnection('Reconnecting · showing last update'); }
      if (!controller.signal.aborted) timer = setTimeout(refresh, document.hidden ? 10000 : 3000);
    };
    refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [paused]);
  const card = key => {
    const n = nodes.find(node => node.key === key);
    if (!n) return null;
    const live = data.in_flight.filter(item => item.desk === n.step || (key === 'policy' && item.desk === 'outcome'));
    const tone = ['approved','export'].includes(key) ? 'success' : ['review','person'].includes(key) ? 'warning' : ['failed','rejected'].includes(key) ? 'danger' : '';
    const ai = ['vision','extract','jev'].includes(key);
    return <article data-flow-node={key} className={`flow-card ${tone} ${ai ? 'is-ai' : ''} ${live.length ? 'is-active' : ''}`} key={key}>
      <div className="flow-card__top"><span className="flow-icon"><Icon kind={tone === 'success' ? 'check' : ai ? 'spark' : key === 'policy' ? 'route' : ['doc','finance','tax'].includes(key) ? 'shield' : 'file'}/></span><span className="flow-step">{n.number ? `Step ${n.number.padStart(2,'0')}` : 'Outcome'}</span>{ai && <span className="flow-ai">AI</span>}</div>
      <h3>{key === 'vision' ? 'Vision extraction' : key === 'policy' ? 'Decision routing' : n.title}</h3>
      <p className="flow-actor">{n.actor}</p>
      {key === 'vision' && <p className="flow-note">Scans & photos only · text files skip this step</p>}
      <div className="flow-card__bottom"><span className="flow-stat">{countKeys[key] ? <><b>{data.counts[countKeys[key]] ?? 0}</b> now</> : live.length ? <><b>{live.length}</b> processing</> : data.avg_ms[n.step] != null ? <>avg <b>{duration(data.avg_ms[n.step])}</b></> : 'No timing yet'}</span>{n.info && <button className="flow-info" onClick={() => document.getElementById(`info-${key}`)?.showModal()} aria-label={`How ${n.title} works`}>↗</button>}</div>
      {live.map(item => <a className="flow-invoice" key={item.id || item.url} href={item.url}>{item.number || item.label}</a>)}
    </article>;
  };
  return <div className="workflow-dashboard" data-workflow-dashboard>
    <header className="flow-heading"><div><div className="flow-breadcrumb">Workspace <span>/</span> Workflow</div><h1>Where every invoice goes<span className="flow-version">11 steps</span></h1><p>One clear path from document to decision.</p>{data.speed && <p className="flow-speed"><span><i/>Fastest invoice</span><b>{duration(data.speed.fastest_ms)}</b><a href={data.speed.url}>{data.speed.label}{data.speed.number ? ` ${data.speed.number}` : ''}</a><span className="flow-speed__typical">Typical {duration(data.speed.typical_ms)} across {data.speed.count} invoices</span></p>}</div><a className="button button--primary" href="/inbox"><span aria-hidden="true">＋</span> Upload invoice</a></header>
    <section className="flow-metrics" aria-label="Invoice overview">
      {[['In processing',data.counts.in_flight,'cyan','/inbox'],['Ready to export',data.counts.approved,'success','/exports'],['Needs review',data.counts.pending_review,'warning','/review'],['Exported',data.counts.exported,'neutral','/exports']].map(([label,value,tone,href]) => <a href={href} className={`flow-metric ${tone}`} key={label}><span><i/>{label}</span><strong>{value ?? 0}</strong><span className="metric-link" aria-hidden="true">↗</span></a>)}
    </section>
    <section className="flow-canvas" aria-labelledby="pipeline-heading">
      <div className="flow-toolbar"><div><h2 id="pipeline-heading">Processing pipeline</h2><span className={`flow-connection ${connection !== 'Live updates' ? 'warning' : ''}`} role="status"><i/>{paused ? 'Updates paused' : connection}</span></div><button className="button flow-pause" aria-pressed={paused} onClick={() => setPaused(value => !value)}>{paused ? 'Resume updates' : 'Pause updates'}</button></div>
      <div className="decision-tree" ref={graph}>
        <div className="tree-trunk">
          {card('arrive')}
          <details className="tree-phase"><summary><Icon kind="file"/><span>Read & extract<small>Document reading, vision and structured fields</small></span><span>⌄</span></summary><div className="tree-details">{['read','vision','extract'].map(card)}</div></details>
          <details className="tree-phase"><summary><Icon kind="shield"/><span>Validate & match<small>Document checks, vendor and purchase order</small></span><span>⌄</span></summary><div className="tree-details">{['doc','md'].map(card)}</div></details>
          {card('jev')}
          <details className="tree-phase"><summary><Icon kind="shield"/><span>Apply company controls<small>Classification, company policy and tax</small></span><span>⌄</span></summary><div className="tree-details">{['coding','finance','tax'].map(card)}</div></details>
          {card('policy')}
        </div>
        <div className="tree-branches"><section className="tree-destination success"><h3>Checks passed</h3>{card('approved')}{card('export')}<a href="/exports">View exports ↗</a></section><section className="tree-destination warning"><h3>Review required</h3>{card('review')}<details><summary>Resolution path</summary>{card('person')}</details><a href="/review">Open review queue ↗</a></section><section className="tree-destination danger"><h3>Stopped</h3>{card('failed')}{card('rejected')}<a href="/inbox">View invoices ↗</a></section></div>
      </div>
      <footer className="flow-canvas-footer"><span>Vision runs only for scans and photos. Unreadable files stop after extraction.</span><span>Approval prepares a bill for export, not payment.</span></footer>
    </section>
    <section className="flow-start"><div><h2>Start with the order</h2><p>Give every invoice the right records to match against.</p></div><a href="/purchase-orders"><span>01</span>Create purchase order ↗</a><a href="/receiving"><span>02</span>Record delivery ↗</a><a href="/inbox"><span>03</span>Upload invoice ↗</a></section>
    {domToReact(dialogs, options)}
  </div>;
}
