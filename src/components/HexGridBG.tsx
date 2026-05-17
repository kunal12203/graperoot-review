"use client";

import { useEffect, useMemo, useRef } from "react";

const HEX_SIZE = 58;
const HEX_R = HEX_SIZE - 2;
const VIEWBOX_W = 1800;
const VIEWBOX_H = 1100;

const HORIZ_SPACING = Math.sqrt(3) * HEX_SIZE;
const VERT_SPACING = 1.5 * HEX_SIZE;

const FALLOFF_RADIUS = HEX_SIZE * 4.5;
const MAX_FILL = 0.13;
const MAX_STROKE = 0.3;
const FALLOFF_POWER = 1.6;

function hexPath(cx: number, cy: number, r: number): string {
  const points: string[] = [];
  for (let i = 0; i < 6; i++) {
    const angle = (Math.PI / 3) * i + Math.PI / 6;
    const x = cx + r * Math.cos(angle);
    const y = cy + r * Math.sin(angle);
    points.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  }
  return points.join(" ");
}

interface Hex {
  cx: number;
  cy: number;
}

function generateHexes(): Hex[] {
  const hexes: Hex[] = [];
  const cols = Math.ceil(VIEWBOX_W / HORIZ_SPACING) + 2;
  const rows = Math.ceil(VIEWBOX_H / VERT_SPACING) + 2;
  for (let row = -1; row < rows; row++) {
    for (let col = -1; col < cols; col++) {
      const isOddRow = row % 2 !== 0;
      const cx = col * HORIZ_SPACING + (isOddRow ? HORIZ_SPACING / 2 : 0);
      const cy = row * VERT_SPACING;
      hexes.push({ cx, cy });
    }
  }
  return hexes;
}

function getVertices(hexes: Hex[], r: number): Array<{ x: number; y: number }> {
  const seen = new Set<string>();
  const out: Array<{ x: number; y: number }> = [];
  for (const h of hexes) {
    for (let i = 0; i < 6; i++) {
      const angle = (Math.PI / 3) * i + Math.PI / 6;
      const x = h.cx + r * Math.cos(angle);
      const y = h.cy + r * Math.sin(angle);
      const key = `${Math.round(x)},${Math.round(y)}`;
      if (!seen.has(key)) {
        seen.add(key);
        out.push({ x, y });
      }
    }
  }
  return out;
}

export default function HexGridBG() {
  const hexes = useMemo(generateHexes, []);
  const vertices = useMemo(() => getVertices(hexes, HEX_SIZE), [hexes]);
  const svgRef = useRef<SVGSVGElement>(null);
  const hoverRefs = useRef<Array<SVGPolygonElement | null>>([]);

  useEffect(() => {
    let raf: number | null = null;

    const updateHeatmap = (cx: number, cy: number) => {
      for (let i = 0; i < hexes.length; i++) {
        const h = hexes[i];
        const d = Math.hypot(h.cx - cx, h.cy - cy);
        const ratio = Math.max(0, 1 - d / FALLOFF_RADIUS);
        const strength = Math.pow(ratio, FALLOFF_POWER);
        const el = hoverRefs.current[i];
        if (!el) continue;
        if (strength <= 0.001) {
          el.setAttribute("fill-opacity", "0");
          el.setAttribute("stroke-opacity", "0");
        } else {
          el.setAttribute("fill-opacity", (strength * MAX_FILL).toFixed(3));
          el.setAttribute("stroke-opacity", (strength * MAX_STROKE).toFixed(3));
        }
      }
    };

    const clearHeatmap = () => {
      for (const el of hoverRefs.current) {
        if (el) {
          el.setAttribute("fill-opacity", "0");
          el.setAttribute("stroke-opacity", "0");
        }
      }
    };

    const onMove = (e: MouseEvent) => {
      if (raf !== null) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const rect = svgRef.current?.getBoundingClientRect();
        if (!rect) return;
        const scale = Math.max(rect.width / VIEWBOX_W, rect.height / VIEWBOX_H);
        const offsetX = (rect.width - VIEWBOX_W * scale) / 2;
        const offsetY = (rect.height - VIEWBOX_H * scale) / 2;
        const x = (e.clientX - rect.left - offsetX) / scale;
        const y = (e.clientY - rect.top - offsetY) / scale;
        updateHeatmap(x, y);
      });
    };

    const onLeave = () => clearHeatmap();

    window.addEventListener("mousemove", onMove, { passive: true });
    document.addEventListener("mouseleave", onLeave);
    window.addEventListener("blur", onLeave);
    return () => {
      window.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseleave", onLeave);
      window.removeEventListener("blur", onLeave);
      if (raf !== null) cancelAnimationFrame(raf);
    };
  }, [hexes]);

  return (
    <div className="absolute inset-0 pointer-events-none overflow-hidden">
      <svg
        ref={svgRef}
        className="absolute inset-0 w-full h-full"
        viewBox={`0 0 ${VIEWBOX_W} ${VIEWBOX_H}`}
        preserveAspectRatio="xMidYMid slice"
        aria-hidden
      >
        {/* Layer 1 — static hex outlines (very subtle, no animation) */}
        {hexes.map((h, i) => (
          <polygon
            key={`s-${i}`}
            points={hexPath(h.cx, h.cy, HEX_R)}
            stroke="rgb(168, 85, 247)"
            strokeWidth="0.5"
            strokeOpacity="0.05"
            fill="none"
          />
        ))}

        {/* Layer 2 — single circle at each merging point (vertex) */}
        {vertices.map((v, i) => (
          <circle
            key={`v-${i}`}
            cx={v.x}
            cy={v.y}
            r="2.4"
            fill="none"
            stroke="rgb(216, 180, 254)"
            strokeWidth="0.7"
            strokeOpacity="0.32"
          />
        ))}

        {/* Layer 3 — cursor heatmap (radial falloff, no animation) */}
        {hexes.map((h, i) => (
          <polygon
            key={`h-${i}`}
            ref={(el) => {
              hoverRefs.current[i] = el;
            }}
            points={hexPath(h.cx, h.cy, HEX_R)}
            stroke="rgb(216, 180, 254)"
            strokeWidth="1"
            fill="rgb(192, 132, 252)"
            fillOpacity="0"
            strokeOpacity="0"
          />
        ))}
      </svg>
    </div>
  );
}
