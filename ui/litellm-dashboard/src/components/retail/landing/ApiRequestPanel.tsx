"use client";

import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Copy } from "lucide-react";
import styles from "./GatewayLanding.module.css";
import { buildRequestExample, requestPath, type ApiProtocol, type ExampleLanguage } from "./requestExamples";

type CopyResult = { example: string; state: "copied" | "selected" };

function protocolForKey(key: string, current: ApiProtocol): ApiProtocol {
  if (key === "Home") return "openai";
  if (key === "End") return "anthropic";
  return current === "openai" ? "anthropic" : "openai";
}

export default function ApiRequestPanel({ apiBase }: { apiBase: string }) {
  const id = useId();
  const codeRef = useRef<HTMLElement>(null);
  const tabsRef = useRef<Record<ApiProtocol, HTMLButtonElement | null>>({ openai: null, anthropic: null });
  const copyVersion = useRef(0);
  const [protocol, setProtocol] = useState<ApiProtocol>("openai");
  const [language, setLanguage] = useState<ExampleLanguage>("curl");
  const [copyResult, setCopyResult] = useState<CopyResult | null>(null);
  const example = buildRequestExample(protocol, language, apiBase);
  const copyState = copyResult?.example === example ? copyResult.state : "idle";
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

  function handleProtocolKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = protocolForKey(event.key, protocol);
    setProtocol(next);
    tabsRef.current[next]?.focus();
  }

  async function copyExample() {
    const version = ++copyVersion.current;
    try {
      await navigator.clipboard.writeText(example);
      if (version === copyVersion.current) setCopyResult({ example, state: "copied" });
    } catch {
      if (version !== copyVersion.current || !codeRef.current) return;
      const range = document.createRange();
      range.selectNodeContents(codeRef.current);
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
      setCopyResult({ example, state: "selected" });
    }
  }

  return (
    <div className={styles["code-workbench"]}>
      <div className={styles.protocols} role="tablist" aria-label="API format">
        <button
          ref={(node) => {
            tabsRef.current.openai = node;
          }}
          className={styles.protocol}
          role="tab"
          id={`${id}-openai`}
          aria-selected={protocol === "openai"}
          aria-controls={`${id}-example`}
          tabIndex={protocol === "openai" ? 0 : -1}
          onClick={() => setProtocol("openai")}
          onKeyDown={handleProtocolKey}
        >
          OpenAI-compatible
        </button>
        <button
          ref={(node) => {
            tabsRef.current.anthropic = node;
          }}
          className={styles.protocol}
          role="tab"
          id={`${id}-anthropic`}
          aria-selected={protocol === "anthropic"}
          aria-controls={`${id}-example`}
          tabIndex={protocol === "anthropic" ? 0 : -1}
          onClick={() => setProtocol("anthropic")}
          onKeyDown={handleProtocolKey}
        >
          Anthropic Messages
        </button>
      </div>
      <div className={styles["code-toolbar"]}>
        <div className={styles.languages} role="group" aria-label="Example language">
          <button className={styles.language} aria-pressed={language === "curl"} onClick={() => setLanguage("curl")}>
            cURL
          </button>
          <button
            className={styles.language}
            aria-pressed={language === "python"}
            onClick={() => setLanguage("python")}
          >
            Python
          </button>
          <button
            className={styles.language}
            aria-label="JavaScript"
            aria-pressed={language === "javascript"}
            onClick={() => setLanguage("javascript")}
          >
            JS
          </button>
        </div>
        <button
          className={styles.copy}
          type="button"
          aria-label="Copy request example"
          onClick={copyExample}
          data-copied={copyState === "copied"}
        >
          <Copy size={14} aria-hidden="true" />
          {copyLabel}
        </button>
      </div>
      <div id={`${id}-example`} role="tabpanel" aria-labelledby={`${id}-${protocol}`} tabIndex={0}>
        <pre>
          <code ref={codeRef}>{example}</code>
        </pre>
      </div>
      <div className={styles.endpoint}>
        <code>POST {requestPath(protocol)}</code>
        <p>Replace YOUR_API_KEY and YOUR_MODEL_ID. Use a model that supports the selected API format.</p>
      </div>
      <div className={styles["copy-status"]} role="status" aria-live="polite">
        {copyStatus}
      </div>
    </div>
  );
}
