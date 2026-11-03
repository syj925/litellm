"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { uiHref } from "@/utils/uiHref";
import { publicSiteFontClassName } from "./publicSiteFonts";
import styles from "./PublicSiteShell.module.css";

export type PublicSitePage = "home" | "models" | "developers" | "pricing" | "login" | "signup";
export const publicSiteThemeClassName = `${styles.theme} ${publicSiteFontClassName}`;

const sections = [
  { page: "models", label: "Models" },
  { page: "developers", label: "Developers" },
  { page: "pricing", label: "Pricing" },
] as const;

export function PublicSiteHeader({ activePage }: { activePage: PublicSitePage }) {
  const { authLoading } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setMenuOpen(false);
      menuRef.current?.focus();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [menuOpen]);

  return (
    <header className={`${styles.header} z-chrome`} data-public-header data-auth-loading={authLoading}>
      <div className={styles.navigation}>
        <a className={styles.brand} href={uiHref("welcome/")} aria-label="AI Gateway home">
          AI Gateway
        </a>
        <nav className={styles.links} aria-label="Main navigation">
          {sections.map(({ page, label }) => (
            <a key={page} href={uiHref(`${page}/`)} aria-current={activePage === page ? "page" : undefined}>
              {label}
            </a>
          ))}
        </nav>
        <a className={styles.signIn} href={uiHref("login/")} aria-current={activePage === "login" ? "page" : undefined}>
          Sign in
        </a>
        <button
          ref={menuRef}
          type="button"
          className={styles.menuButton}
          aria-expanded={menuOpen}
          aria-controls="public-mobile-navigation"
          onClick={() => setMenuOpen(!menuOpen)}
        >
          Menu
        </button>
      </div>
      <nav
        id="public-mobile-navigation"
        className={styles.mobileLinks}
        aria-label="Mobile navigation"
        hidden={!menuOpen}
        onClick={() => setMenuOpen(false)}
      >
        {sections.map(({ page, label }) => (
          <a key={page} href={uiHref(`${page}/`)} aria-current={activePage === page ? "page" : undefined}>
            {label}
          </a>
        ))}
      </nav>
    </header>
  );
}

export function PublicSiteFooter({ tone = "surface" }: { tone?: "surface" | "signal" }) {
  const { authLoading } = useAuth();
  return (
    <footer
      className={[styles.footer, tone === "signal" ? styles.signalFooter : ""].join(" ")}
      data-public-footer
      data-auth-loading={authLoading}
    >
      <div className={styles.footerInner}>
        <a className={styles.footerBrand} href={uiHref("welcome/")}>
          AI Gateway
        </a>
        <p>One connection. More possibilities.</p>
        <nav className={styles.footerLinks} aria-label="Footer navigation">
          <a href={uiHref("developers/")}>Developers</a>
          <a href={uiHref("pricing/")}>Pricing</a>
          <a href={uiHref("login/")}>Sign in</a>
        </nav>
      </div>
    </footer>
  );
}

export default function PublicSiteShell({
  activePage,
  children,
  footer = true,
}: {
  activePage: PublicSitePage;
  children: ReactNode;
  footer?: boolean;
}) {
  return (
    <div className={`${publicSiteThemeClassName} ${styles.site}`} data-public-site>
      <a className={`${styles.skip} z-overlay`} href="#public-site-content">
        Skip to content
      </a>
      <PublicSiteHeader activePage={activePage} />
      <div id="public-site-content" className={styles.content} tabIndex={-1}>
        {children}
      </div>
      {footer && <PublicSiteFooter />}
    </div>
  );
}
