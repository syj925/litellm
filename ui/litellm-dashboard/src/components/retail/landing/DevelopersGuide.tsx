"use client";

import localFont from "next/font/local";
import { getProxyBaseUrl } from "@/components/networking";
import { useAuth } from "@/contexts/AuthContext";
import { serverRootPath } from "@/lib/serverRootPath";
import { uiHref } from "@/utils/uiHref";
import landingStyles from "./GatewayLanding.module.css";
import styles from "./DevelopersGuide.module.css";
import ApiRequestPanel from "./ApiRequestPanel";

const geist = localFont({
  src: "../../../../public/retail-landing/fonts/geist-latin-wght-normal.woff2",
  weight: "100 900",
  variable: "--font-gateway-geist",
});
const mono = localFont({
  src: "../../../../public/retail-landing/fonts/roboto-mono-latin-wght-normal.woff2",
  weight: "100 700",
  variable: "--font-gateway-mono",
});

function gatewayBase(authLoading: boolean): string {
  if (authLoading || typeof window === "undefined") return "https://api.example.com";
  const prefix = serverRootPath === "/" ? "" : "/" + serverRootPath.replace(/^\/+|\/+$/g, "");
  return getProxyBaseUrl() || window.location.origin + prefix;
}

export default function DevelopersGuide() {
  const { authLoading } = useAuth();
  const apiBase = gatewayBase(authLoading);
  const homeHref = uiHref("welcome/");
  const modelsHref = uiHref("models/");
  const developersHref = uiHref("developers/");
  const pricingHref = uiHref("pricing/");
  const loginHref = uiHref("login/");

  return (
    <div className={[landingStyles.root, styles.guide, geist.variable, mono.variable].join(" ")}>
      <a className={landingStyles.skip} href="#guide-main">
        Skip to content
      </a>
      <header className={[styles.frame, styles.header].join(" ")}>
        <a className={styles.brand} href={homeHref} aria-label="AI Gateway home">
          AI Gateway
        </a>
        <nav className={styles.navigation} aria-label="Main navigation">
          <a href={modelsHref}>Models</a>
          <a href={developersHref} aria-current="page">
            Developers
          </a>
          <a href={pricingHref}>Pricing</a>
        </nav>
        <a className={styles.signIn} href={loginHref}>
          Sign in
        </a>
      </header>
      <main id="guide-main">
        <section className={[styles.frame, styles.hero].join(" ")} aria-labelledby="developers-heading">
          <h1 className={styles.title} id="developers-heading">
            Connect. <br />
            Start building.
          </h1>
          <div className={styles.heroCopy}>
            <p>
              Send your first model request through the gateway using familiar API formats and the tools you already
              use.
            </p>
            <div className={styles.heroActions}>
              <a className={styles.primary} href="#first-request">
                Try a request example
              </a>
              <a className={styles.inlineLink} href={modelsHref}>
                Explore models
              </a>
            </div>
          </div>
        </section>
        <section className={[styles.frame, styles.section].join(" ")} aria-labelledby="access-heading">
          <div className={styles.intro}>
            <h2 id="access-heading">Get ready.</h2>
            <p>
              Your account, API key and selected model determine what you can call. You can browse models and reference
              prices before signing in.
            </p>
          </div>
          <ol className={styles.steps}>
            <li>
              <h3>Create your account</h3>
              <p>New accounts start without credit. Contact an administrator to arrange funding and resource access.</p>
              <a className={styles.inlineLink} href={uiHref("signup/")}>
                Create an account
              </a>
            </li>
            <li>
              <h3>Create an API key</h3>
              <p>
                Sign in and create a key in API key management. Keep the key on your server and out of public browser
                code.
              </p>
              <a className={styles.inlineLink} href={uiHref("api-keys/")}>
                Manage API keys
              </a>
            </li>
            <li>
              <h3>Choose a model</h3>
              <p>
                Copy its model ID and check the supported API format. Published models may require resources to be
                enabled for your account.
              </p>
              <a className={styles.inlineLink} href={modelsHref}>
                Browse model details
              </a>
            </li>
          </ol>
        </section>
        <section className={styles.requestField} id="first-request" aria-labelledby="request-heading">
          <div className={[styles.frame, styles.section].join(" ")}>
            <h2 id="request-heading">Make your first call.</h2>
            <div className={styles.requestGrid}>
              <div className={styles.requestAside}>
                <p className={styles.endpointLabel}>Your gateway endpoint</p>
                <code className={styles.endpointValue}>{apiBase}</code>
                <p>
                  Select the API format supported by your model, copy an example, and replace the API key and model ID.
                </p>
                <p>The examples use normal JSON responses. No request is sent from this page.</p>
                <a className={styles.inlineLink} href={modelsHref}>
                  Check model API formats
                </a>
              </div>
              <ApiRequestPanel apiBase={apiBase} />
            </div>
          </div>
        </section>
        <section
          className={[styles.frame, styles.section, styles.readingGrid].join(" ")}
          aria-labelledby="formats-heading"
        >
          <h2 id="formats-heading">Choose the right format.</h2>
          <div className={styles.articles}>
            <article>
              <h3>OpenAI-compatible APIs</h3>
              <p>
                Use <code>/v1/chat/completions</code> with the <code>Authorization: Bearer</code> header for a supported
                model. Keep the gateway endpoint and model ID explicit in your application configuration.
              </p>
            </article>
            <article>
              <h3>Anthropic Messages</h3>
              <p>
                Use <code>/v1/messages</code> with the <code>x-api-key</code> and <code>anthropic-version</code>{" "}
                headers. The Messages format has its own request structure, including <code>max_tokens</code>.
              </p>
            </article>
            <article>
              <h3>Streaming responses</h3>
              <p>
                For a model that supports streaming, add <code>{'"stream": true'}</code> to the request body and consume
                the event stream with a streaming client. Check the model’s API example before changing formats.
              </p>
            </article>
          </div>
        </section>
        <section
          className={[styles.frame, styles.section, styles.readingGrid].join(" ")}
          aria-labelledby="errors-heading"
        >
          <h2 id="errors-heading">Resolve a failed call.</h2>
          <div>
            <table className={styles.errorTable}>
              <thead>
                <tr>
                  <th scope="col">Status</th>
                  <th scope="col">What to check</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>401</td>
                  <td>Check the API key and authentication header.</td>
                </tr>
                <tr>
                  <td>402</td>
                  <td>Check your wallet balance and arrange funding with an administrator.</td>
                </tr>
                <tr>
                  <td>403</td>
                  <td>Check the permissions and resources enabled for your account and key.</td>
                </tr>
                <tr>
                  <td>429</td>
                  <td>Reduce the request rate or concurrency, then retry with a bounded delay.</td>
                </tr>
                <tr>
                  <td>5xx</td>
                  <td>Check the returned error and request history. Retry transient failures with a limit.</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
        <section
          className={[styles.frame, styles.section, styles.readingGrid].join(" ")}
          aria-labelledby="usage-heading"
        >
          <h2 id="usage-heading">Follow your usage.</h2>
          <div className={styles.articles}>
            <p>
              Sign in to review your own request status, token usage and recorded charges. Catalog prices are a
              reference; your account rates and applicable rules determine the final charge.
            </p>
            <div className={styles.accountActions}>
              <a className={styles.inlineLink} href={uiHref("logs/")}>
                View request history
              </a>
              <a className={styles.inlineLink} href={uiHref("billing/")}>
                Open billing
              </a>
              <a className={styles.inlineLink} href={pricingHref}>
                View reference prices
              </a>
            </div>
          </div>
        </section>
      </main>
      <footer className={[styles.frame, styles.footer].join(" ")}>
        <a className={styles.brand} href={homeHref}>
          AI Gateway
        </a>
        <nav className={styles.footerLinks} aria-label="Footer navigation">
          <a href={homeHref}>Home</a>
          <a href={modelsHref}>Models</a>
          <a href={pricingHref}>Pricing</a>
          <a href={loginHref}>Sign in</a>
        </nav>
      </footer>
    </div>
  );
}
