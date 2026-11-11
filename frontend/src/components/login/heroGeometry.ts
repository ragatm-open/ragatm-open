// frontend/src/components/login/heroGeometry.ts
// Projection isométrique PURE (aucune dépendance React) de la scène du panneau de
// connexion. Les volumes sont de vrais prismes : trois faces ombrées, dessinées dans
// l'ordre de profondeur, donc des occlusions cohérentes — pas des rectangles en dégradé.
//
// Repère : x vers la droite-bas, y vers la gauche-bas, z vers le haut. L'observateur
// voit la face supérieure, la face « avant-gauche » (normale +y) et la face
// « avant-droite » (normale +x).

/** Taille d'une unité du monde, en unités du viewBox. */
export const UNIT = 30;
/** Origine de la scène dans le viewBox (centre du moteur, au sol). */
export const ORIGIN_X = 300;
export const ORIGIN_Y = 340;
export const VIEWBOX = { width: 620, height: 620 } as const;

const COS = Math.cos(Math.PI / 6);
const SIN = 0.5;

export type Point = readonly [number, number];

export function iso(x: number, y: number, z = 0): Point {
  return [ORIGIN_X + (x - y) * UNIT * COS, ORIGIN_Y + (x + y) * UNIT * SIN - z * UNIT];
}

/** Inverse de `iso` à hauteur `z` donnée : place un objet par sa position à l'écran. */
export function world(screenX: number, screenY: number, z = 0): { x: number; y: number } {
  const a = (screenX - ORIGIN_X) / (UNIT * COS);
  const b = (screenY - ORIGIN_Y + z * UNIT) / (UNIT * SIN);
  return { x: (a + b) / 2, y: (b - a) / 2 };
}

export function points(ps: readonly Point[]): string {
  return ps.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join(" ");
}

export type Prism = { top: string; left: string; right: string };

/** Prisme droit occupant [x, x+w] × [y, y+d] × [z, z+h]. */
export function prism(x: number, y: number, z: number, w: number, d: number, h: number): Prism {
  return {
    top: points([iso(x, y, z + h), iso(x + w, y, z + h), iso(x + w, y + d, z + h), iso(x, y + d, z + h)]),
    left: points([iso(x, y + d, z + h), iso(x + w, y + d, z + h), iso(x + w, y + d, z), iso(x, y + d, z)]),
    right: points([iso(x + w, y, z + h), iso(x + w, y + d, z + h), iso(x + w, y + d, z), iso(x + w, y, z)]),
  };
}

/** Prisme centré sur un point de l'écran (base posée à la hauteur `z`). */
export function prismAt(screenX: number, screenY: number, z: number, w: number, d: number, h: number): Prism {
  const c = world(screenX, screenY, z);
  return prism(c.x - w / 2, c.y - d / 2, z, w, d, h);
}

/** Cercle horizontal (anneau) projeté : chemin fermé. */
export function ring(x: number, y: number, z: number, r: number, steps = 72): string {
  let d = "";
  for (let i = 0; i <= steps; i++) {
    const t = (i / steps) * Math.PI * 2;
    const [px, py] = iso(x + r * Math.cos(t), y + r * Math.sin(t), z);
    d += `${i === 0 ? "M" : "L"}${px.toFixed(1)},${py.toFixed(1)}`;
  }
  return `${d}Z`;
}

/** Demi-ellipse avant d'un cylindre vertical (silhouette visible). */
export function cylinder(x: number, y: number, z0: number, z1: number, r: number) {
  const rx = r * UNIT * Math.SQRT2 * COS;
  const ry = r * UNIT * Math.SQRT2 * SIN;
  const [cx, bottom] = iso(x, y, z0);
  const [, top] = iso(x, y, z1);
  return {
    cx,
    top,
    bottom,
    rx,
    ry,
    body: `M${(cx - rx).toFixed(1)},${top.toFixed(1)} L${(cx - rx).toFixed(1)},${bottom.toFixed(1)} A${rx.toFixed(1)},${ry.toFixed(1)} 0 0 0 ${(cx + rx).toFixed(1)},${bottom.toFixed(1)} L${(cx + rx).toFixed(1)},${top.toFixed(1)} Z`,
  };
}

/**
 * Matrice SVG plaçant un repère local 2D (u vers la droite, v vers le bas, en unités
 * du viewBox) dans un plan VERTICAL du monde, ancré au point (x, y, z).
 *  - `faceLeft`  : plan tourné vers l'avant-gauche (axe u = +x) ;
 *  - `faceRight` : plan tourné vers l'avant-droite (axe u = −y).
 */
export function planeMatrix(anchorX: number, anchorY: number, facing: "faceLeft" | "faceRight"): string {
  const b = facing === "faceLeft" ? SIN : -SIN;
  return `matrix(${COS.toFixed(4)} ${b} 0 1 ${anchorX.toFixed(1)} ${anchorY.toFixed(1)})`;
}

/**
 * Courbe lisse passant par tous les points (Catmull-Rom convertie en Bézier cubiques).
 * Les flux sont ainsi dérivés des positions réelles des modules : déplacer un module
 * déplace son flux, sans coordonnées recopiées à la main.
 */
export function curve(ps: readonly Point[], tension = 0.5): string {
  if (ps.length < 2) return "";
  const f = (n: number) => n.toFixed(1);
  let d = `M${f(ps[0][0])},${f(ps[0][1])}`;
  for (let i = 0; i < ps.length - 1; i++) {
    const p0 = ps[Math.max(0, i - 1)];
    const p1 = ps[i];
    const p2 = ps[i + 1];
    const p3 = ps[Math.min(ps.length - 1, i + 2)];
    const k = tension / 3;
    const c1: Point = [p1[0] + (p2[0] - p0[0]) * k, p1[1] + (p2[1] - p0[1]) * k];
    const c2: Point = [p2[0] - (p3[0] - p1[0]) * k, p2[1] - (p3[1] - p1[1]) * k];
    d += ` C${f(c1[0])},${f(c1[1])} ${f(c2[0])},${f(c2[1])} ${f(p2[0])},${f(p2[1])}`;
  }
  return d;
}

/** Panneau flottant légèrement incliné (lisible, contrairement au plan isométrique pur). */
export function billboardMatrix(x: number, y: number, rise = 0.16): string {
  return `matrix(1 ${(-rise).toFixed(3)} 0 1 ${x.toFixed(1)} ${y.toFixed(1)})`;
}
