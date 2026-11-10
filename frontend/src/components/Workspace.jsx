import React from 'react';

const paths = {
  Workflow: 'M4 4h6v6H4zM14 14h6v6h-6zM7 10v7h7M14 4h6v6h-6zM10 7h4',
  Inbox: 'M4 4h16v16H4zM4 14h5l2 3h2l2-3h5',
  'Review queue': 'M8 4h12v16H4V8M8 4v4H4M8 13l3 3 5-6',
  Dashboard: 'M3 6h18v14H3zM3 10h18M15 15h3',
  Exports: 'M12 15V3M8 7l4-4 4 4M4 14v7h16v-7',
  Evaluations: 'M4 20h16M7 16V9M12 16V4M17 16v-5',
  Vendors: 'M16 21v-3a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v3M10 10a4 4 0 1 0 0-8 4 4 0 0 0 0 8M20 21v-3a4 4 0 0 0-3-4',
  'Purchase orders': 'M6 3h12v18H6zM9 7h6M9 11h6M9 15h4',
  'Delivery Management': 'M3 6h11v12H3zM14 10h4l3 4v4h-7M7 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4M17 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4',
  'Chart of accounts': 'M4 3h16v18H4zM8 7h8M8 11h8M8 15h8',
  'Tax rules': 'M6 3h12v18H6zM9 7h6M9 13l6 4M15 13l-6 4',
  'Company policies': 'M12 3v3M12 18v3M3 12h3M18 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8',
};
const text = (node) => node.type === 'text' ? node.data : (node.children || []).map(text).join('');
const links = (node) => node.name === 'a' ? [node] : (node.children || []).flatMap(links);

export function Navigation({ element }) {
  const all = links(element);
  return <nav id="workspace-navigation" className="side__nav" aria-label="Main">
    {[['Workspace', all.slice(0, 5)], ['Records', all.slice(5)]].map(([label, items]) =>
      <div className="navigation-group" key={label}>
        <p className="side__group">{label}</p>
        <ul>{items.map((item) => {
          const name = text(item).trim();
          return <li key={item.attribs.href}><a href={item.attribs.href} aria-current={item.attribs['aria-current']}>
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.Workflow}/></svg>
            <span>{name}</span>
          </a></li>;
        })}</ul>
      </div>)}
  </nav>;
}

export function LandingHeader({ children }) {
  return <header className="landing-header">
    {children}
    <div className="landing-header__actions">
      <a className="button button--primary" href="/inbox">Upload Invoice</a>
      <a className="button" href="/purchase-orders#new-title">Create purchase order</a>
    </div>
  </header>;
}
