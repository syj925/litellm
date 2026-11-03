import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import styles from "./GatewayLanding.module.css";

type MotionStatus = "static" | "running" | "reduced" | "paused" | "hidden" | "offscreen";
type Entrance = "row" | "panel" | "quiet" | "heading";
type MotionSnapshot = { ready: boolean; reduced: boolean; paused: boolean; status: MotionStatus };

const initialSnapshot: MotionSnapshot = { ready: false, reduced: false, paused: false, status: "static" };

function sameSnapshot(current: MotionSnapshot, next: MotionSnapshot): boolean {
  const preferencesMatch =
    current.ready === next.ready && current.reduced === next.reduced && current.paused === next.paused;
  return preferencesMatch && current.status === next.status;
}

function statusFor(paused: boolean, reduced: boolean, hidden: boolean, visible: boolean): MotionStatus {
  if (reduced) return "reduced";
  if (paused) return "paused";
  if (hidden) return "hidden";
  return visible ? "running" : "offscreen";
}

function selector(value: string): string {
  return value.replace(/\.([a-zA-Z][\w-]*)/g, (_, name: string) => `.${styles[name]}`);
}

function entranceStart(kind: Entrance): Keyframe {
  if (kind === "row") return { opacity: 0.45, transform: "translateX(18px)" };
  if (kind === "panel") return { opacity: 0.5, transform: "translateY(18px) scale(.99)" };
  if (kind === "quiet") return { opacity: 0.5, transform: "none" };
  return { opacity: 0.65, transform: "translateY(22px)" };
}

export default function useLandingMotion(rootRef: RefObject<HTMLDivElement | null>) {
  const [snapshot, setSnapshot] = useState(initialSnapshot);
  const toggleRef = useRef<(() => void) | null>(null);
  const toggle = useCallback(() => toggleRef.current?.(), []);

  useEffect(() => {
    const root = rootRef.current;
    const hero = root?.querySelector<HTMLElement>(selector(".hero"));
    const art = root?.querySelector<HTMLElement>(selector(".hero-art"));
    if (!root || !hero || !art) return;
    if (!window.matchMedia || !window.IntersectionObserver) return;
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const finite = new Set<Animation>();
    const heroFinite = new Set<Animation>();
    let userPaused = false;
    let artVisible = false;
    let pageSuspended = false;
    let disposed = false;

    function finish(collection = finite) {
      for (const animation of [...collection]) animation.cancel();
      collection.clear();
    }
    function sync() {
      if (disposed) return;
      const next: MotionSnapshot = {
        ready: true,
        reduced: preference.matches,
        paused: userPaused,
        status: statusFor(userPaused, preference.matches, document.hidden || pageSuspended, artVisible),
      };
      setSnapshot((current) => (sameSnapshot(current, next) ? current : next));
      if (preference.matches || userPaused || document.hidden) finish();
      else if (!artVisible) finish(heroFinite);
    }
    function arrive(element: Element, kind: Entrance, delay = 0, duration = 680) {
      if (preference.matches || userPaused || document.hidden) return;
      if (!element.animate) return;
      const options: KeyframeAnimationOptions = {
        duration,
        delay,
        easing: "cubic-bezier(.16,1,.3,1)",
        fill: "backwards",
      };
      const animation = element.animate([entranceStart(kind), { opacity: 1, transform: "none" }], options);
      finite.add(animation);
      if (hero?.contains(element)) heroFinite.add(animation);
      const clear = () => {
        finite.delete(animation);
        heroFinite.delete(animation);
      };
      animation.addEventListener("finish", clear, { once: true });
      animation.addEventListener("cancel", clear, { once: true });
    }
    toggleRef.current = () => {
      userPaused = !userPaused;
      sync();
    };
    const heroObserver = new IntersectionObserver(
      (entries) => {
        artVisible = entries[0]?.isIntersecting ?? false;
        sync();
      },
      { threshold: 0, rootMargin: "-2% 0px -2% 0px" },
    );
    heroObserver.observe(art);
    const onPageHide = () => {
      pageSuspended = true;
      sync();
      finish();
    };
    const onPageShow = () => {
      pageSuspended = false;
      sync();
    };
    document.addEventListener("visibilitychange", sync);
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
    preference.addEventListener("change", sync);
    sync();

    hero.querySelectorAll("h1 span").forEach((line, index) => arrive(line, "heading", index * 85, 720));
    arrive(art, "panel", 70, 800);
    const reveals = new Map<Element, { kind: Entrance; delay: number }>();
    function register(value: string, kind: Entrance, stagger = 0) {
      root?.querySelectorAll(selector(value)).forEach((element, index) => {
        reveals.set(element, { kind, delay: Math.min(index * stagger, 210) });
      });
    }
    register(".section-head h2, .dev-copy h2, .billing-layout h2, .faq-layout h2", "heading");
    register(".section-head > p, .dev-copy > p, .billing-description", "quiet");
    register(".catalog-row", "row", 70);
    register(".code-workbench", "panel");
    register(".usage-row", "row", 65);
    register(".questions details", "quiet", 60);
    register(".closing h2", "heading");
    register(".closing-bottom", "quiet");
    const revealObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const config = reveals.get(entry.target);
          if (!entry.isIntersecting || !config) continue;
          if (entry.boundingClientRect.top > 12) arrive(entry.target, config.kind, config.delay);
          revealObserver.unobserve(entry.target);
          reveals.delete(entry.target);
        }
      },
      { threshold: 0, rootMargin: "0px 0px -7% 0px" },
    );
    reveals.forEach((_, element) => revealObserver.observe(element));

    const mobile = root.querySelector<HTMLElement>(selector(".mobile-nav"));
    const menuObserver = new MutationObserver(() => {
      if (mobile && !mobile.hidden) arrive(mobile, "panel", 0, 220);
    });
    if (mobile) menuObserver.observe(mobile, { attributes: true, attributeFilter: ["hidden"] });
    const copy = root.querySelector<HTMLElement>(selector(".copy"));
    const copyObserver = new MutationObserver(() => {
      if (copy?.dataset.copied === "true") arrive(copy, "quiet", 0, 200);
    });
    if (copy) copyObserver.observe(copy, { attributes: true, attributeFilter: ["data-copied"] });

    return () => {
      disposed = true;
      toggleRef.current = null;
      heroObserver.disconnect();
      revealObserver.disconnect();
      menuObserver.disconnect();
      copyObserver.disconnect();
      preference.removeEventListener("change", sync);
      document.removeEventListener("visibilitychange", sync);
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("pageshow", onPageShow);
      finish();
    };
  }, [rootRef]);

  return { ...snapshot, toggle };
}
