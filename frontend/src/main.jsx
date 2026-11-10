import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
import parse, { attributesToProps, domToReact } from 'html-react-parser';
import { Navigation, LandingHeader } from './components/Workspace.jsx';
import './design.css';
import WorkflowDashboard from './components/WorkflowDashboard.jsx';
import './workflow.css';
import Evaluations from './components/Evaluations.jsx';
import './evaluations.css';

// Existing FastAPI view responses remain the compatibility contract during migration.
// All browser nodes are rendered by React; forms retain their server validation.
function MobileMenu({ attributes, children }) {
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    document.getElementById('workspace-navigation')?.classList.toggle('is-open', expanded);
  }, [expanded]);
  useEffect(() => {
    const escape = (event) => {
      if (event.key === 'Escape') setExpanded(false);
    };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, []);
  return <button {...attributesToProps(attributes)} aria-expanded={expanded}
    onClick={() => setExpanded((value) => !value)}>{children}</button>;
}

function fixed4(value) {
  const match = String(value ?? '').trim().match(/^(\d+)(?:\.(\d{0,4}))?$/);
  return match ? BigInt(match[1]) * 10000n + BigInt((match[2] || '').padEnd(4, '0')) : null;
}
function centsText(value) {
  return value === null ? 'Not calculated' : `${value / 100n}.${String(value % 100n).padStart(2, '0')}`;
}
function readPOTotals(element) {
  let subtotal = 0n, known = true, netKnown = true;
  const groups = new Map();
  const lines = [...element.querySelectorAll('[data-po-items] [data-po-item]')].map((row) => {
    const quantity = fixed4(row.querySelector('[name="item_quantity"]')?.value);
    const price = fixed4(row.querySelector('[name="item_price"]')?.value);
    const rate = fixed4(row.querySelector('[name="item_tax_rate_percent"]')?.value);
    const net = quantity !== null && price !== null ? (quantity * price + 500000n) / 1000000n : null;
    if (net === null) netKnown = false; else subtotal += net;
    if (rate === null || rate > 1000000n || net === null) known = false;
    else groups.set(rate, (groups.get(rate) || 0n) + net);
    return net;
  });
  let tax = 0n;
  for (const [rate, net] of groups) tax += (rate * net + 500000n) / 1000000n;
  return { lines, subtotal: netKnown ? subtotal : null, tax: known ? tax : null, total: known && netKnown ? subtotal + tax : null };
}
function PurchaseOrderItems({ element }) {
  const initial = element.children.find((node) => node.attribs?.['data-po-items'] !== undefined);
  const template = element.children.find((node) => node.name === 'template');
  const rows = initial?.children.filter((node) => node.attribs?.['data-po-item'] !== undefined) || [];
  const [items, setItems] = useState(() => rows.map((node, index) => ({ id: index + 1, node })));
  const [message, setMessage] = useState('');
  const [nextId, setNextId] = useState(rows.length + 1);
  const [totals, setTotals] = useState({lines:[], subtotal:null, tax:null, total:null});
  useEffect(() => {
    const editor = document.querySelector('[data-po-editor]');
    if (editor) setTotals(readPOTotals(editor));
  }, [items]);
  const blank = template?.children.find((node) => node.attribs?.['data-po-item'] !== undefined);
  const add = () => {
    if (!blank || items.length >= 200) return;
    setItems([...items, { id: nextId, node: blank }]);
    setNextId(nextId + 1);
    setMessage(`Item ${items.length + 1} added.`);
  };
  const remove = (id) => {
    if (items.length <= 1) return;
    setItems(items.filter((item) => item.id !== id));
    setMessage(`Item removed. ${items.length - 1} items remaining.`);
  };
  const renderRow = (item, index) => {
    const rowOptions = { replace(node) {
      if (!node.attribs) return;
      if (node.attribs['data-item-number'] !== undefined) return <span>{index + 1}</span>;
      if (node.attribs['data-item-net'] !== undefined) {
        const outputProps = attributesToProps(node.attribs);
        if (outputProps.id?.endsWith('-new')) outputProps.id = outputProps.id.replace(/-new$/, `-react-${item.id}`);
        return <output {...outputProps}>{centsText(totals.lines[index] ?? null)}</output>;
      }
      if (node.attribs['data-remove-item'] !== undefined) return <button type="button"
        className={node.attribs.class} disabled={items.length === 1} onClick={() => remove(item.id)}>
        Remove<span className="sr-only"> item {index + 1}</span></button>;
      const props = attributesToProps(node.attribs);
      if (props.id?.endsWith('-new')) props.id = props.id.replace(/-new$/, `-react-${item.id}`);
      if (props.htmlFor?.endsWith('-new')) props.htmlFor = props.htmlFor.replace(/-new$/, `-react-${item.id}`);
      if (node.name === 'input') {
        if ('value' in props) { props.defaultValue = props.value; delete props.value; }
        if ('checked' in props) { props.defaultChecked = props.checked; delete props.checked; }
        return <input {...props} />;
      }
      if (node.name === 'label') return <label {...props}>{domToReact(node.children, rowOptions)}</label>;
      if (node.name === 'select') return <select {...props}>{domToReact(node.children, rowOptions)}</select>;
    }};
    return <div key={item.id} {...attributesToProps(item.node.attribs)}>
      {domToReact(item.node.children, rowOptions)}</div>;
  };
  return <div {...attributesToProps(element.attribs)} onChange={(event) => setTotals(readPOTotals(event.currentTarget))}>
    <h3 id="po-items-title">Items</h3>
    <p className="muted small">Add each item separately. Leave the account on the vendor's default unless you want to choose another.</p>
    <div data-po-items="">{items.map(renderRow)}</div>
    <button type="button" className="button" data-add-item="" disabled={items.length >= 200} onClick={add}>Add item</button>
    <p className="small muted" data-item-status="" aria-live="polite">{message}</p>
    <dl className="figures po-tax-totals" aria-live="polite"><div><dt>Subtotal before tax ($)</dt><dd>{centsText(totals.subtotal)}</dd></div><div><dt>Tax ($)</dt><dd>{centsText(totals.tax)}</dd></div><div><dt>Total including tax ($)</dt><dd>{centsText(totals.total)}</dd></div></dl>
    <p className="small muted">Enter a tax rate for every line to calculate the total. Final amounts are validated when saved.</p>
  </div>;
}

const options = { replace(node) {
  if (node.type === 'script' && node.attribs?.src) return <></>;
  if (!node.attribs) return;
  if (node.attribs.class === 'react-pipeline') return <WorkflowDashboard element={node} options={options} />;
  if (node.attribs['data-evaluations'] !== undefined) return <Evaluations />;
  if (node.name === 'nav' && node.attribs.id === 'workspace-navigation') return <Navigation element={node} />;
  if (node.name === 'header' && node.attribs.class === 'parser-hero') return <LandingHeader>{domToReact(node.children, options)}</LandingHeader>;
  if (node.attribs.class?.split(' ').includes('mobile-menu')) {
    return <MobileMenu attributes={node.attribs}>{domToReact(node.children, options)}</MobileMenu>;
  }
  if (node.attribs['data-po-editor'] !== undefined) return <PurchaseOrderItems element={node} />;
  if (node.name === 'form' && node.attribs['data-upload'] !== undefined) return <InvoiceUpload element={node} />;
}};

function InvoiceUpload({ element }) {
  const [fileName, setFileName] = useState('');
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const uploadOptions = { replace(node) {
    if (!node.attribs) return;
    if (node.attribs['data-file-name'] !== undefined) return <span className="dropzone__file" data-file-name="" aria-live="polite">{fileName ? `Selected: ${fileName}` : ''}</span>;
    if (node.attribs['data-progress'] !== undefined) return <div className="progress" hidden={!uploading} role="status"><div className="progress__bar"/><p>Uploading. Next you will see each step as it happens.</p></div>;
    if (node.name === 'button' && node.attribs.type === 'submit') return <button className="button button--primary" type="submit" disabled={uploading}>{uploading ? 'Processing…' : 'Process invoice'}</button>;
  }};
  return <form {...attributesToProps(element.attribs)} className={`dropzone${dragging ? ' is-dragging' : ''}`}
    onChange={(event) => { if (event.target.type === 'file') setFileName(event.target.files[0]?.name || ''); }}
    onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
    onDragLeave={() => setDragging(false)}
    onDrop={(event) => {
      event.preventDefault(); setDragging(false);
      const input = event.currentTarget.querySelector('input[type="file"]');
      if (event.dataTransfer.files.length && input) {
        input.files = event.dataTransfer.files;
        setFileName(input.files[0]?.name || '');
      }
    }} onSubmit={() => setUploading(true)}>
    {domToReact(element.children, uploadOptions)}
  </form>;
}

function App({ markup }) {
  useEffect(() => {
    if (document.querySelector('[data-workflow]')) import('../../src/jev_invoice/web/static/workflow.js');
    const timer = document.querySelector('[data-processing]') ? setInterval(() => {
      const input = document.querySelector('[data-upload] input[type="file"]');
      if (!input?.files?.length) window.location.reload();
    }, 3000) : null;
    const copy = async (event) => {
      const button = event.target.closest('[data-copy]');
      if (!button) return;
      const box = document.getElementById(button.dataset.copy);
      const status = button.parentElement.querySelector('[data-copy-status]');
      try { await navigator.clipboard.writeText(box.value); if (status) status.textContent = 'Copied. Paste it into your email to the vendor.'; }
      catch { box.select(); if (status) status.textContent = 'Select all and copy (Ctrl+C).'; }
    };
    const discount = (event) => {
      const box = event.target;
      if (!box.matches('[data-payment] input[name=take_discount]')) return;
      box.form.querySelector('input[name=amount]').value = (Number(box.dataset.full) - (box.checked ? Number(box.dataset.discount) : 0)).toFixed(2);
    };
    document.addEventListener('click', copy);
    document.addEventListener('change', discount);
    return () => {
      if (timer) clearInterval(timer);
      document.removeEventListener('click', copy);
      document.removeEventListener('change', discount);
    };
  }, []);
  return <>{parse(markup, options)}</>;
}

const container = document.getElementById('react-root');
const markup = container.innerHTML;
flushSync(() => createRoot(container).render(<App markup={markup} />));

