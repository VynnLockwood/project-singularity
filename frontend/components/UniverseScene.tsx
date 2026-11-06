"use client";

import { useEffect, useRef } from "react";
import type { Universe } from "@/lib/universe";

export function webglAvailable(): boolean {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch {
    return false;
  }
}

interface Props {
  universe: Universe;
  selected: string | null;
  onSelect: (name: string | null) => void;
  /** Called when the scene cannot start (no WebGL, driver error) so the parent can show the 2D view. */
  onUnavailable?: () => void;
}

const BODY = 0xffa646;
const PICKED = 0xfff1c2;
const LINK = 0x9a4f1c;

/** The 3D "schema universe": tables are bodies, foreign keys are links. Drag to orbit, wheel to zoom, click to select. */
export function UniverseScene({ universe, selected, onSelect, onUnavailable }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const paint = useRef<(name: string | null) => void>(() => {});
  const pick = useRef(onSelect);
  pick.current = onSelect;

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let stop = () => {};
    let dead = false;

    (async () => {
      try {
        const THREE = await import("three");
        const { OrbitControls } = await import("three/examples/jsm/controls/OrbitControls.js");
        if (dead) return;

        const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        el.appendChild(renderer.domElement);
        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 5000);
        camera.position.set(0, 60, 260);
        scene.add(new THREE.AmbientLight(0xffffff, 0.7));
        const sun = new THREE.DirectionalLight(0xffffff, 1.1);
        sun.position.set(120, 200, 160);
        scene.add(sun);
        const controls = new OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true;

        const meshes = new Map<string, import("three").Mesh>();
        const pos = new Map(universe.bodies.map((b) => [b.name, b.pos]));
        for (const b of universe.bodies) {
          const m = new THREE.Mesh(
            new THREE.SphereGeometry(b.size, 24, 16),
            new THREE.MeshStandardMaterial({ color: BODY, roughness: 0.5, metalness: 0.1, emissive: 0x000000 }),
          );
          m.position.set(...b.pos);
          m.userData.name = b.name;
          scene.add(m);
          meshes.set(b.name, m);

          const cv = document.createElement("canvas");
          cv.width = 256; cv.height = 64;
          const g = cv.getContext("2d");
          if (g) {
            g.font = "28px sans-serif"; g.fillStyle = "#fbe9d0"; g.textAlign = "center";
            g.fillText(b.name, 128, 40);
          }
          const label = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(cv), transparent: true, depthTest: false }));
          label.scale.set(40, 10, 1);
          label.position.set(b.pos[0], b.pos[1] + b.size + 6, b.pos[2]);
          scene.add(label);
        }

        // the singularity itself: a black sphere with a glowing ring, at the centre the tables orbit
        const hole = new THREE.Mesh(new THREE.SphereGeometry(11, 32, 20), new THREE.MeshBasicMaterial({ color: 0x000000 }));
        scene.add(hole);
        const ring = new THREE.Mesh(
          new THREE.TorusGeometry(19, 1.6, 12, 96),
          new THREE.MeshBasicMaterial({ color: 0xff8a1f, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending }),
        );
        ring.rotation.x = Math.PI / 2.25;
        scene.add(ring);
        const ring2 = new THREE.Mesh(
          new THREE.TorusGeometry(26, 0.7, 8, 96),
          new THREE.MeshBasicMaterial({ color: 0xffc46b, transparent: true, opacity: 0.45, blending: THREE.AdditiveBlending }),
        );
        ring2.rotation.x = Math.PI / 2.25;
        scene.add(ring2);

        const pts: number[] = [];
        for (const l of universe.links) {
          const a = pos.get(l.from), b = pos.get(l.to);
          if (a && b) pts.push(...a, ...b);
        }
        const lineGeo = new THREE.BufferGeometry();
        lineGeo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
        scene.add(new THREE.LineSegments(lineGeo, new THREE.LineBasicMaterial({ color: LINK })));

        paint.current = (name) => {
          meshes.forEach((m, n) => {
            const mat = m.material as import("three").MeshStandardMaterial;
            mat.color.setHex(n === name ? PICKED : BODY);
            mat.emissive.setHex(n === name ? 0x884400 : 0x000000);
          });
        };

        const resize = () => {
          const w = el.clientWidth || 800, h = el.clientHeight || 520;
          renderer.setSize(w, h);
          camera.aspect = w / h;
          camera.updateProjectionMatrix();
        };
        resize();
        const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(resize) : null;
        ro?.observe(el);

        // click = pointer down and up without moving (so orbiting does not select)
        const ray = new THREE.Raycaster();
        let down: [number, number] | null = null;
        const dom = renderer.domElement;
        const onDown = (e: PointerEvent) => { down = [e.clientX, e.clientY]; };
        const onUp = (e: PointerEvent) => {
          if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
          const r = dom.getBoundingClientRect();
          ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
          const hit = ray.intersectObjects([...meshes.values()])[0];
          pick.current(hit ? (hit.object.userData.name as string) : null);
        };
        dom.addEventListener("pointerdown", onDown);
        dom.addEventListener("pointerup", onUp);

        let raf = 0;
        const tick = () => {
          ring.rotation.z += 0.003;
          ring2.rotation.z -= 0.002;
          controls.update();
          renderer.render(scene, camera);
          raf = requestAnimationFrame(tick);
        };
        tick();

        stop = () => {
          cancelAnimationFrame(raf);
          ro?.disconnect();
          dom.removeEventListener("pointerdown", onDown);
          dom.removeEventListener("pointerup", onUp);
          controls.dispose();
          scene.traverse((o) => {
            const mesh = o as import("three").Mesh;
            mesh.geometry?.dispose();
            const mat = mesh.material as import("three").Material | undefined;
            mat?.dispose();
          });
          renderer.dispose();
          dom.remove();
          paint.current = () => {};
        };
        paint.current(selected);
      } catch {
        if (!dead) onUnavailable?.();
      }
    })();

    return () => { dead = true; stop(); };
    // the scene is rebuilt only when the universe itself changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [universe]);

  useEffect(() => { paint.current(selected); }, [selected]);

  return <div ref={host} className="universe" data-testid="universe" aria-label="3D schema universe" />;
}
