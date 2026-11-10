# Parser.ai React frontend

The browser UI is rendered by React 19 and built with Vite. FastAPI serves the production assets at `/static/react/` on the same origin as the existing application.

## Build

From `frontend/`:

```sh
pnpm install --frozen-lockfile
pnpm build
```

`pnpm dev` watches the sources and rebuilds the assets used by FastAPI. Refresh the browser after a rebuild. Run the FastAPI application normally; a separate frontend server is not required.

## Migration boundary

React owns the browser rendering on all application pages, the mobile menu, purchase-order item editor and invoice upload feedback. Existing form actions continue posting to FastAPI and retain server validation, redirects and error handling. Normal links currently use browser navigation.

The existing Jinja templates supply initial page content through a compatibility layer using `html-react-parser`. This is a React browser migration, **not a removal of Jinja or an API-only SPA rewrite**. The workflow dashboard is rendered by WorkflowDashboard.jsx, with responsive node connectors, live polling, and accessible step-detail dialogs. Its JSON data endpoint and all business operations remain unchanged.

Keep frontend changes in `src/main.jsx`. Run `pnpm build` after edits, include generated assets in releases, and test item addition/removal, upload feedback, menus and delivery expansion in a browser as well as the backend tests.

