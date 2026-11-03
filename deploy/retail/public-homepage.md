# Public homepage

The retail gateway has a public homepage at `/ui/welcome/`. It links to the model catalog, login and registration, and includes API request examples with the current gateway base URL and placeholder credentials

Desktop and mobile navigation open actual pages: Models at `/ui/models/`, Developers at `/ui/developers/`, Pricing at `/ui/pricing/` and Sign in at `/ui/login/`. The developer guide includes account setup, copyable protocol examples and authenticated account links. Pricing reuses the public catalog API and reference rates, defaults to a table, and opens each model on its pricing tab. Explicit view and detail-tab URL choices remain supported; account prices and invocation access still require authentication

The retail Compose override sets `DOCS_URL=/docs` and `ROOT_REDIRECT_URL=/ui/welcome/` by default. These use the proxy's existing root redirect; `/ui/` remains the dashboard entry and authentication behavior is unchanged. Override the environment values when needed. A deployment under a non-root prefix must include that prefix in `ROOT_REDIRECT_URL`, for example `/gateway/ui/welcome/`

Run `npm ci` when dependencies are missing, then `npm run build` from `ui/litellm-dashboard`. The static export includes `welcome/index.html` and the bundled fonts. The local retail Compose override mounts that `out` directory into the gateway's UI directory. The first change to the root redirect environment requires recreating only the gateway service; subsequent frontend builds are served from the mounted directory

If a Windows checkout uses a `node_modules` junction outside the dashboard directory, Turbopack can reject its filesystem root. Use `npm run build -- --webpack` for that checkout; this builds the same static export without changing the application configuration

The homepage styles are scoped to its component. It keeps the approved light ONE identity, connected terminal paths, pause control, offscreen/background suspension, and reduced-motion fallback. It does not send inference requests, create accounts or modify billing

Check the gateway root, `/ui/welcome/`, `/ui/models/`, `/ui/login/` and `/ui/signup/` after a build. Confirm the catalog logo, login home link and registration logo return to the homepage, and that the normal dashboard still requires sign-in
