"use client";

import localFont from "next/font/local";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { getProxyBaseUrl } from "@/components/networking";
import { useAuth } from "@/contexts/AuthContext";
import { serverRootPath } from "@/lib/serverRootPath";
import { uiHref } from "@/utils/uiHref";
import styles from "./GatewayLanding.module.css";
import OneWordmark from "./OneWordmark";
import { buildRequestExample, requestPath, type ApiProtocol, type ExampleLanguage } from "./requestExamples";
import useLandingMotion from "./useLandingMotion";

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

type CapabilityFilter = "all" | "language" | "multimodal";
type CopyState = "idle" | "copied" | "selected";

function motionDescription(reduced: boolean, paused: boolean) {
  if (reduced) return { label: "Animation reduced by system preference", text: "Motion reduced" };
  if (paused) return { label: "Resume ONE animation", text: "Resume motion" };
  return { label: "Pause ONE animation", text: "Pause motion" };
}

function gatewayBase(authLoading: boolean): string {
  if (authLoading || typeof window === "undefined") return "https://api.example.com";
  const prefix = serverRootPath === "/" ? "" : "/" + serverRootPath.replace(/^\/+|\/+$/g, "");
  return getProxyBaseUrl() || window.location.origin + prefix;
}

function protocolForKey(key: string, current: ApiProtocol): ApiProtocol {
  if (key === "Home") return "openai";
  if (key === "End") return "anthropic";
  return current === "openai" ? "anthropic" : "openai";
}

export default function GatewayLanding() {
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLButtonElement>(null);
  const codeRef = useRef<HTMLElement>(null);
  const copyVersion = useRef(0);
  const { authLoading } = useAuth();
  const motion = useLandingMotion(rootRef);
  const [menuOpen, setMenuOpen] = useState(false);
  const [filter, setFilter] = useState<CapabilityFilter>("all");
  const [protocol, setProtocol] = useState<ApiProtocol>("openai");
  const [language, setLanguage] = useState<ExampleLanguage>("curl");
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const apiBase = gatewayBase(authLoading);
  const modelsHref = uiHref("models/");
  const developersHref = uiHref("developers/");
  const pricingHref = uiHref("pricing/");
  const loginHref = uiHref("login/");
  const signupHref = uiHref("signup/");
  const homeHref = uiHref("welcome/");
  const example = buildRequestExample(protocol, language, apiBase);
  const visibleCount = { all: 4, language: 1, multimodal: 3 }[filter];
  const description = motionDescription(motion.reduced, motion.paused);
  const motionLabel = description.label;
  const motionText = description.text;
  const copyLabel = { idle: "Copy", copied: "Copied", selected: "Selected" }[copyState];
  const copyStatus = {
    idle: "",
    copied: "Request example copied.",
    selected: "Example selected. Press Ctrl+C or Command+C to copy.",
  }[copyState];

  useEffect(
    () => () => {
      copyVersion.current += 1;
    },
    [],
  );

  useEffect(() => {
    if (!menuOpen) return;
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setMenuOpen(false);
      menuRef.current?.focus();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [menuOpen]);

  function closeMenu() {
    setMenuOpen(false);
  }
  function changeProtocol(value: ApiProtocol) {
    setProtocol(value);
    setCopyState("idle");
    copyVersion.current += 1;
  }
  function changeLanguage(value: ExampleLanguage) {
    setLanguage(value);
    setCopyState("idle");
    copyVersion.current += 1;
  }
  function handleProtocolKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = protocolForKey(event.key, protocol);
    changeProtocol(next);
    rootRef.current?.querySelector<HTMLButtonElement>('[data-protocol="' + next + '"]')?.focus();
  }
  async function copyExample() {
    const version = ++copyVersion.current;
    try {
      await navigator.clipboard.writeText(example);
      if (version === copyVersion.current) setCopyState("copied");
    } catch {
      if (version !== copyVersion.current || !codeRef.current) return;
      const range = document.createRange();
      range.selectNodeContents(codeRef.current);
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
      setCopyState("selected");
    }
  }

  return (
    <div
      ref={rootRef}
      className={[styles.root, geist.variable, mono.variable].join(" ")}
      data-auth-loading={authLoading}
    >
      <a className={styles["skip"]} href="#main">
        Skip to content
      </a>
      <header>
        <div className={[styles["nav"], styles["wrap"]].join(" ")}>
          <a className={styles["brand"]} href={homeHref} aria-label="AI Gateway home">
            AI Gateway
          </a>
          <nav className={styles["navlinks"]} aria-label="Main navigation">
            <a href={modelsHref}>Models</a>
            <a href={developersHref}>Developers</a>
            <a href={pricingHref}>Pricing</a>
          </nav>
          <a className={styles["signin"]} href={loginHref}>
            Sign in
          </a>
          <button
            ref={menuRef}
            className={styles["menu-toggle"]}
            type="button"
            onClick={() => setMenuOpen(!menuOpen)}
            aria-expanded={menuOpen}
            aria-controls="mobile-navigation"
          >
            Menu
          </button>
        </div>
        <nav
          id="mobile-navigation"
          className={styles["mobile-nav"]}
          aria-label="Mobile navigation"
          hidden={!menuOpen}
          onClick={closeMenu}
        >
          <a href={modelsHref}>Models</a>
          <a href={developersHref}>Developers</a>
          <a href={pricingHref}>Pricing</a>
        </nav>
      </header>
      <main id="main">
        <section
          className={[
            styles.hero,
            styles.wrap,
            motion.ready && styles["motion-enabled"],
            motion.status === "running" && styles["motion-running"],
          ]
            .filter(Boolean)
            .join(" ")}
          data-motion={motion.status}
          aria-labelledby="hero-title"
        >
          <h1 id="hero-title">
            <span>One connection.</span>
            <span>More possibilities.</span>
          </h1>
          <div className={styles["hero-art"]} id="one-brand-motion">
            <OneWordmark />
          </div>
          <div className={styles["hero-bottom"]}>
            <div className={styles["hero-actions"]}>
              <a className={styles["button"]} href={modelsHref}>
                Explore models{" "}
                <span className={styles["arrow"]} aria-hidden="true">
                  →
                </span>
              </a>
              <button
                className={styles["motion-toggle"]}
                type="button"
                aria-label={motionLabel}
                aria-pressed={motion.paused || motion.reduced}
                aria-controls="one-brand-motion"
                hidden={!motion.ready}
                disabled={motion.reduced}
                onClick={motion.toggle}
              >
                <svg
                  className={styles["motion-pause"]}
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  aria-hidden="true"
                >
                  <path d="M7 4v12M13 4v12" />
                </svg>
                <svg
                  className={styles["motion-play"]}
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  aria-hidden="true"
                >
                  <path d="m7 4 9 6-9 6Z" />
                </svg>
                <span className={styles["motion-label"]}>{motionText}</span>
              </button>
            </div>
            <p className={styles["hero-note"]}>Access models through familiar APIs.</p>
          </div>
        </section>
        <section id="models" className={[styles["section"], styles["wrap"]].join(" ")} aria-labelledby="catalog-title">
          <div className={styles["section-head"]}>
            <div>
              <span className={styles["section-label"]}>The catalog</span>
              <h2 id="catalog-title">
                Choose the model
                <br />
                that fits.
              </h2>
            </div>
            <p>
              From everyday conversations to multimodal workflows. Browse capabilities, supported API formats, and
              reference prices in the model catalog.
            </p>
          </div>
          <div className={styles["catalog-controls"]}>
            <div className={styles["filters"]} role="group" aria-label="Filter capabilities">
              <button
                className={styles["filter"]}
                aria-pressed={filter === "all"}
                onClick={() => setFilter("all")}
                data-filter="all"
              >
                All capabilities
              </button>
              <button
                className={styles["filter"]}
                aria-pressed={filter === "language"}
                onClick={() => setFilter("language")}
                data-filter="language"
              >
                Language
              </button>
              <button
                className={styles["filter"]}
                aria-pressed={filter === "multimodal"}
                onClick={() => setFilter("multimodal")}
                data-filter="multimodal"
              >
                Multimodal
              </button>
            </div>
            <a className={styles["text-link"]} href={modelsHref}>
              Explore models{" "}
              <span className={styles["arrow"]} aria-hidden="true">
                ↗
              </span>
            </a>
          </div>
          <div className={styles["catalog-list"]}>
            <a
              className={styles["catalog-row"]}
              data-category="language"
              hidden={filter !== "all" && filter !== "language"}
              href={modelsHref}
            >
              <div className={styles["row-name"]}>
                <span className={styles["catalog-mark"]} aria-hidden="true"></span>
                <h3>Language</h3>
              </div>
              <p>Write, reason, and build with text. Find a model for your next conversation or coding workflow.</p>
              <span className={styles["row-open"]} aria-hidden="true">
                ↗
              </span>
            </a>
            <a
              className={styles["catalog-row"]}
              data-category="multimodal"
              hidden={filter !== "all" && filter !== "multimodal"}
              href={modelsHref}
            >
              <div className={styles["row-name"]}>
                <span className={[styles["catalog-mark"], styles["vision"]].join(" ")} aria-hidden="true"></span>
                <h3>Vision</h3>
              </div>
              <p>Bring images into the conversation. Explore models that can understand visual input.</p>
              <span className={styles["row-open"]} aria-hidden="true">
                ↗
              </span>
            </a>
            <a
              className={styles["catalog-row"]}
              data-category="multimodal"
              hidden={filter !== "all" && filter !== "multimodal"}
              href={modelsHref}
            >
              <div className={styles["row-name"]}>
                <span className={[styles["catalog-mark"], styles["image"]].join(" ")} aria-hidden="true"></span>
                <h3>Images</h3>
              </div>
              <p>Turn a brief into an image. Check the catalog for supported generation models and formats.</p>
              <span className={styles["row-open"]} aria-hidden="true">
                ↗
              </span>
            </a>
            <a
              className={styles["catalog-row"]}
              data-category="multimodal"
              hidden={filter !== "all" && filter !== "multimodal"}
              href={modelsHref}
            >
              <div className={styles["row-name"]}>
                <span className={[styles["catalog-mark"], styles["audio"]].join(" ")} aria-hidden="true"></span>
                <h3>Audio</h3>
              </div>
              <p>Work with speech and sound. Compare the input and output capabilities of available models.</p>
              <span className={styles["row-open"]} aria-hidden="true">
                ↗
              </span>
            </a>
          </div>
          <div className={styles["catalog-foot"]}>
            <p>Capabilities vary by model. Sign in to confirm the resources and rates available to your account.</p>
            <span className={styles["filter-status"]} role="status">
              {visibleCount} capability {visibleCount === 1 ? "group" : "groups"}
            </span>
          </div>
        </section>
        <section
          id="developers"
          className={[styles["section"], styles["developers"]].join(" ")}
          aria-labelledby="developers-title"
        >
          <div className={[styles["wrap"], styles["dev-layout"]].join(" ")}>
            <div className={styles["dev-copy"]}>
              <h2 id="developers-title">
                Keep the tools
                <br />
                you know.
              </h2>
              <p>
                Use OpenAI-compatible APIs or Anthropic Messages with supported models. Start with the format your
                application already understands.
              </p>
              <ol className={styles["setup-list"]}>
                <li>Set your endpoint.</li>
                <li>Add your API key.</li>
                <li>Choose a model.</li>
              </ol>
              <a className={styles["text-link"]} href={modelsHref}>
                View model examples{" "}
                <span className={styles["arrow"]} aria-hidden="true">
                  ↗
                </span>
              </a>
            </div>
            <div className={styles["code-workbench"]} id="api-format">
              <div className={styles["protocols"]} role="tablist" aria-label="API format">
                <button
                  className={styles["protocol"]}
                  role="tab"
                  id="tab-openai"
                  aria-selected={protocol === "openai"}
                  aria-controls="request-example"
                  data-protocol="openai"
                  tabIndex={protocol === "openai" ? 0 : -1}
                  onClick={() => changeProtocol("openai")}
                  onKeyDown={handleProtocolKey}
                >
                  OpenAI-compatible
                </button>
                <button
                  className={styles["protocol"]}
                  role="tab"
                  id="tab-anthropic"
                  aria-selected={protocol === "anthropic"}
                  aria-controls="request-example"
                  data-protocol="anthropic"
                  tabIndex={protocol === "anthropic" ? 0 : -1}
                  onClick={() => changeProtocol("anthropic")}
                  onKeyDown={handleProtocolKey}
                >
                  Anthropic Messages
                </button>
              </div>
              <div className={styles["code-toolbar"]}>
                <div className={styles["languages"]} role="group" aria-label="Example language">
                  <button
                    className={styles["language"]}
                    aria-pressed={language === "curl"}
                    data-language="curl"
                    onClick={() => changeLanguage("curl")}
                  >
                    cURL
                  </button>
                  <button
                    className={styles["language"]}
                    aria-pressed={language === "python"}
                    data-language="python"
                    onClick={() => changeLanguage("python")}
                  >
                    Python
                  </button>
                  <button
                    className={styles["language"]}
                    aria-pressed={language === "javascript"}
                    data-language="javascript"
                    onClick={() => changeLanguage("javascript")}
                    aria-label="JavaScript"
                  >
                    JS
                  </button>
                </div>
                <button
                  className={styles["copy"]}
                  type="button"
                  aria-label="Copy request example"
                  onClick={copyExample}
                  data-copied={copyState === "copied"}
                >
                  <span aria-hidden="true">⧉</span>
                  <span className={styles["copy-label"]}>{copyLabel}</span>
                </button>
              </div>
              <div id="request-example" role="tabpanel" aria-labelledby={"tab-" + protocol} tabIndex={0}>
                <pre>
                  <code ref={codeRef} id="example-code">
                    {example}
                  </code>
                </pre>
              </div>
              <div className={styles["endpoint"]}>
                <code id="endpoint-path">POST {requestPath(protocol)}</code>
                <p>
                  Example only. Replace the endpoint, API key, and model ID with your own. Check your model’s supported
                  API format.
                </p>
              </div>
              <div className={styles["copy-status"]} role="status" aria-live="polite">
                {copyStatus}
              </div>
            </div>
          </div>
        </section>
        <section id="pricing" className={[styles["section"], styles["wrap"]].join(" ")} aria-labelledby="pricing-title">
          <div className={styles["billing-layout"]}>
            <div>
              <span className={styles["section-label"]}>Usage & pricing</span>
              <h2 id="pricing-title">
                Every request.
                <br />
                In view.
              </h2>
              <p className={styles["billing-description"]}>
                Follow your request history, token usage, and recorded charges from your account.
              </p>
              <a className={styles["text-link"]} href={loginHref}>
                Open your account{" "}
                <span className={styles["arrow"]} aria-hidden="true">
                  ↗
                </span>
              </a>
            </div>
            <div>
              <div className={styles["usage-ledger"]}>
                <div className={styles["usage-row"]}>
                  <h3>Input & output</h3>
                  <p>See the tokens used by your prompts and model responses.</p>
                </div>
                <div className={styles["usage-row"]}>
                  <h3>Cache usage</h3>
                  <p>Check cache reads and writes where the model and API support them.</p>
                </div>
                <div className={styles["usage-row"]}>
                  <h3>Recorded charges</h3>
                  <p>Review the charge saved for each request, including applicable pricing rules.</p>
                </div>
              </div>
              <p className={styles["pricing-note"]}>
                Catalog prices are a reference. Your account rates, actual usage, and applicable rules determine the
                final charge.
              </p>
            </div>
          </div>
        </section>
        <section className={[styles["section"], styles["wrap"]].join(" ")} aria-labelledby="faq-title">
          <div className={styles["faq-layout"]}>
            <div>
              <h2 id="faq-title">
                A few things
                <br /> to know.
              </h2>
            </div>
            <div className={styles["questions"]}>
              <details>
                <summary>Can I browse without an account?</summary>
                <p>Yes. The model catalog, reference prices, and model API examples are public.</p>
              </details>
              <details>
                <summary>Can I use every model in the catalog?</summary>
                <p>
                  Access depends on the resources enabled for your account. Sign in to check availability, or contact an
                  administrator to arrange access.
                </p>
              </details>
              <details>
                <summary>How do I get an API key?</summary>
                <p>
                  Create an account, sign in, and create a key from API key management. Use that key with the endpoint
                  and request format shown for your selected model.
                </p>
              </details>
              <details>
                <summary>How do I add funds?</summary>
                <p>Contact an administrator to arrange funding. New accounts start without credit.</p>
              </details>
            </div>
          </div>
        </section>
        <section className={styles["closing"]} aria-labelledby="closing-title">
          <div className={styles["wrap"]}>
            <h2 id="closing-title">
              Your next request
              <br />
              starts here.
            </h2>
            <div className={styles["closing-bottom"]}>
              <p>
                Find a model. Create a key.
                <br />
                Start building.
              </p>
              <div className={styles["closing-actions"]}>
                <a className={styles["button"]} href={signupHref}>
                  Create an account{" "}
                  <span className={styles["arrow"]} aria-hidden="true">
                    ↗
                  </span>
                </a>
                <a className={styles["text-link"]} href={modelsHref}>
                  Explore models{" "}
                  <span className={styles["arrow"]} aria-hidden="true">
                    →
                  </span>
                </a>
              </div>
            </div>
            <footer className={styles["site-footer"]} role="contentinfo">
              <a className={styles["footer-brand"]} href={homeHref}>
                AI Gateway
              </a>
              <p className={styles["footer-note"]}>One connection. More possibilities.</p>
              <nav className={styles["footer-links"]} aria-label="Footer navigation">
                <a href={developersHref}>Developers</a>
                <a href={loginHref}>Sign in</a>
              </nav>
            </footer>
          </div>
        </section>
      </main>
    </div>
  );
}
