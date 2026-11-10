"use client";

import { useEffect, useRef, useState } from "react";
import { ACTIVITY_EVENT, FLARE_EVENT, ease } from "@/lib/activity";
import { INTENSITY_EVENT, LEVELS, loadCosmos, readIntensity, type Cosmos as Scene, type Intensity, type Particle } from "@/lib/cosmos";

const TILT = -0.42; // the disk is seen at an angle, rotated like in the reference picture
const FLAT = 0.34;
const SPIN = 0.05; // radians per second at the innermost speed 1
const FLARE_SECONDS = 0.6;

/**
 * Full-screen background: stars, a black hole and its glowing accretion disk. The particles come from the Rust engine.
 * The disk also shows the state of the review queue: it spins up and brightens while changes wait
 * (`activity`, 0 to 1, or the `cosmos:activity` event) and flares once when a change is applied.
 */
export function Cosmos({ intensity, activity }: { intensity?: Intensity; activity?: number } = {}) {
  const target = useRef(activity ?? 0);
  useEffect(() => { if (activity !== undefined) target.current = Math.max(0, Math.min(1, activity)); }, [activity]);
  const ref = useRef<HTMLCanvasElement>(null);
  const [level, setLevel] = useState<Intensity>(intensity ?? readIntensity);

  useEffect(() => { if (intensity) setLevel(intensity); }, [intensity]);
  useEffect(() => {
    const sync = () => setLevel(intensity ?? readIntensity());
    addEventListener(INTENSITY_EVENT, sync);
    addEventListener("storage", sync);
    return () => { removeEventListener(INTENSITY_EVENT, sync); removeEventListener("storage", sync); };
  }, [intensity]);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return; // no canvas (old browser, test): the plain gradient stays
    let scene: Scene | null = null;
    let raf = 0;
    let dead = false;
    let w = 0, h = 0;
    // accessibility: the OS always wins over the pick
    const want = typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches ? "still" : level;
    const cfg = LEVELS[want];
    const still = want === "still";

    const size = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth; h = window.innerHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      canvas.style.width = `${w}px`; canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    // eased queue activity, the disk's accumulated rotation, and when the last flare began
    let a = still ? target.current : 0;
    let phase = 0, last = -1, flareAt = -Infinity;

    const draw = (t: number) => {
      if (!scene) return;
      const dt = last < 0 ? 0 : Math.min(0.1, t - last);
      last = t;
      a = still ? target.current : ease(a, target.current, dt);
      phase += dt * (1 + 0.6 * a);
      const lit = 1 + 0.3 * a;
      const burst = Math.max(0, 1 - (t - flareAt) / FLARE_SECONDS);
      ctx.clearRect(0, 0, w, h);
      // inside the app the hole sits low and to the right, out from under the text
      const inApp = document.documentElement.dataset.app === "true";
      const cx = inApp ? w * 0.82 : w * (w > 900 ? 0.72 : 0.5), cy = inApp ? h * 0.78 : h * 0.42;
      const hr = Math.min(w, h) * 0.11, R = hr * 5.4;
      const cos = Math.cos(TILT), sin = Math.sin(TILT);
      const at = (x: number, y: number): [number, number] => [cx + x * cos - y * sin, cy + x * sin + y * cos];

      ctx.globalCompositeOperation = "source-over";
      for (const s of scene.stars) {
        const twinkle = still ? 1 : 0.75 + 0.25 * Math.sin(t * 0.8 + s.x * 40 + s.y * 17);
        ctx.fillStyle = `hsla(${s.warm > 0.6 ? 32 : 220}, ${s.warm > 0.6 ? 90 : 30}%, ${85 - s.warm * 15}%, ${s.alpha * 0.8 * twinkle})`;
        ctx.fillRect(s.x * w, s.y * h, s.size, s.size);
      }

      // soft halo behind everything
      const halo = ctx.createRadialGradient(cx, cy, hr * 0.9, cx, cy, R * 1.25);
      halo.addColorStop(0, `rgba(255,140,40,${Math.min(0.5, 0.3 * lit)})`);
      halo.addColorStop(0.5, "rgba(190,70,10,0.10)");
      halo.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = halo;
      ctx.fillRect(0, 0, w, h);

      ctx.globalCompositeOperation = "lighter";
      const pos = scene.disk.map((p) => {
        const ang = p.angle + p.speed * SPIN * phase;
        return { p, a: ang, back: Math.sin(ang) < 0 };
      });
      // each particle is a short streak along its orbit, so the disk reads as flowing light
      ctx.lineCap = "round";
      const streak = (p: Particle, x1: number, y1: number, x2: number, y2: number, k: number) => {
        const [sx, sy] = at(x1, y1);
        const [tx, ty] = at(x2, y2);
        ctx.strokeStyle = `hsla(${p.hue}, 100%, ${52 + p.alpha * 20}%, ${Math.min(1, p.alpha * k * 1.25 * lit)})`;
        ctx.lineWidth = p.size * 1.1;
        ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(tx, ty); ctx.stroke();
      };
      const along = (q: { p: Particle; a: number }) => {
        const r = R * q.p.radius, da = (0.05 + q.p.size * 0.03) * cfg.streak;
        return { r, a2: q.a - da };
      };
      // the far half of the disk, plus its image bent over the top of the hole
      for (const q of pos) if (q.back) {
        const { r, a2 } = along(q);
        const x1 = r * Math.cos(q.a), y1 = r * Math.sin(q.a) * FLAT, x2 = r * Math.cos(a2), y2 = r * Math.sin(a2) * FLAT;
        streak(q.p, x1, y1, x2, y2, 0.9);
        const arc = hr * (1.06 + 0.55 * (q.p.radius - 0.16));
        if (Math.abs(x1) < arc && Math.abs(x2) < arc) streak(q.p, x1, -Math.sqrt(arc * arc - x1 * x1), x2, -Math.sqrt(arc * arc - x2 * x2), 1.1);
      }

      ctx.globalCompositeOperation = "source-over";
      const [hx, hy] = [cx, cy];
      const body = ctx.createRadialGradient(hx, hy, hr * 0.2, hx, hy, hr * 1.02);
      body.addColorStop(0, "#000");
      body.addColorStop(0.85, "#080200");
      body.addColorStop(1, "#3a1204");
      ctx.fillStyle = body;
      ctx.beginPath(); ctx.arc(hx, hy, hr, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = `rgba(255,190,110,${Math.min(1, 0.55 * lit + 0.45 * burst)})`; ctx.lineWidth = 1.5 + burst;
      ctx.shadowColor = "rgba(255,120,30,0.9)"; ctx.shadowBlur = 18 * cfg.glow * lit + 30 * burst;
      ctx.beginPath(); ctx.arc(hx, hy, hr * 1.01, 0, Math.PI * 2); ctx.stroke();
      ctx.shadowBlur = 0;

      ctx.globalCompositeOperation = "lighter";
      for (const q of pos) if (!q.back) {
        const { r, a2 } = along(q);
        streak(q.p, r * Math.cos(q.a), r * Math.sin(q.a) * FLAT, r * Math.cos(a2), r * Math.sin(a2) * FLAT, 1);
      }
      ctx.globalCompositeOperation = "source-over";
    };

    const loop = (ms: number) => { draw(ms / 1000); raf = requestAnimationFrame(loop); };

    size();
    loadCosmos(1, cfg.stars, cfg.particles).then(({ scene: s }) => {
      if (dead) return;
      scene = s;
      if (still) draw(0); else raf = requestAnimationFrame(loop);
    });
    const onResize = () => { size(); if (still) draw(0); };
    // a still scene has no loop: redraw its one frame when the queue or the placement changes
    const onActivity = (e: Event) => {
      const level = (e as CustomEvent<number>).detail;
      if (typeof level === "number") target.current = level;
      if (still) draw(0);
    };
    const onFlare = () => { if (!still) flareAt = last < 0 ? 0 : last; };
    window.addEventListener("resize", onResize);
    window.addEventListener(ACTIVITY_EVENT, onActivity);
    window.addEventListener(FLARE_EVENT, onFlare);
    return () => {
      dead = true; cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      window.removeEventListener(ACTIVITY_EVENT, onActivity);
      window.removeEventListener(FLARE_EVENT, onFlare);
    };
  }, [level]);

  return <canvas ref={ref} className="cosmos" aria-hidden="true" />;
}
