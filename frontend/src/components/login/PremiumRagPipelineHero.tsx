"use client";

// frontend/src/components/login/PremiumRagPipelineHero.tsx
// Panneau droit de /login : scène isométrique du moteur RAGatm.
//
// Purement ILLUSTRATIF : aucun appel réseau, aucune donnée réelle, aucun timer ni
// écouteur. Toute l'animation est en CSS (opacité, transform, dashoffset) et s'arrête
// sous `prefers-reduced-motion` ; l'état de repos est la composition complète, lisible
// sans animation. La scène est masquée aux technologies d'assistance, qui reçoivent à
// la place une description textuelle du pipeline.
import { useTranslations } from "next-intl";

import {
  UNIT,
  VIEWBOX,
  billboardMatrix,
  curve,
  cylinder,
  iso,
  planeMatrix,
  points,
  prism,
  prismAt,
  ring,
  type Point,
  type Prism,
} from "./heroGeometry";
import s from "./PremiumRagPipelineHero.module.css";

type Glyph = "ingest" | "vision" | "embed" | "index" | "search" | "rerank" | "llm";
type ChipSpec = { key: Glyph; x: number; y: number; z: number; side: "left" | "right" };

const CHIP = { w: 1.1, d: 1.1, h: 0.42 };

// --- Disposition (coordonnées du viewBox 620 × 620) ------------------------------------
// Chaîne de PRÉPARATION à gauche (documents → noyau), chaîne de RÉPONSE à droite
// (noyau → réponse) : deux familles visuellement distinctes autour du moteur.
const PREP: ChipSpec[] = [
  { key: "ingest", x: 104, y: 372, z: 0, side: "left" },
  { key: "vision", x: 138, y: 318, z: 0.7, side: "left" },
  { key: "embed", x: 174, y: 266, z: 1.4, side: "left" },
  { key: "index", x: 212, y: 216, z: 2.1, side: "left" },
];
const QUERY: ChipSpec[] = [
  { key: "search", x: 424, y: 334, z: 0.3, side: "right" },
  { key: "rerank", x: 470, y: 286, z: 0.9, side: "right" },
  { key: "llm", x: 516, y: 238, z: 1.5, side: "right" },
];
const QUESTION = { x: 178, y: 112, w: 226, h: 40 };
const ANSWER = { x: 414, y: 56, w: 192, h: 134 };
const DOCS = [
  { fmt: "PDF", band: "#B5532F", x: 44, y: 470 },
  { fmt: "DOCX", band: "#8C5A36", x: 74, y: 484 },
  { fmt: "PPTX", band: "#B9783E", x: 104, y: 498 },
  { fmt: "IMG", band: "#2E7A57", x: 134, y: 512 },
  { fmt: "FOLDER", band: "#A88A62", x: 164, y: 526 },
] as const;

const chipTop = (c: ChipSpec): Point => [c.x, c.y - CHIP.h * UNIT];

// --- Géométrie statique (calculée une fois au chargement du module) -------------------
const PLINTH = prism(-2.5, -2.5, 0, 5, 5, 0.5);
const TIER = prism(-1.9, -1.9, 0.5, 3.8, 3.8, 0.32);
const CORE = cylinder(0, 0, 0.82, 3.3, 0.82);
const PLATES = [prism(-1.55, -1.55, 1.72, 3.1, 3.1, 0.07), prism(-1.2, -1.2, 2.5, 2.4, 2.4, 0.07)];
const RING_INNER = ring(0, 0, 2.12, 2.25);
const RING_OUTER = ring(0, 0, 1.22, 2.95);
const BOUNDARY = points([iso(-4.7, -4.7), iso(4.7, -4.7), iso(4.7, 4.7), iso(-4.7, 4.7)]);
const [LOCK_X, LOCK_Y] = iso(4.7, 4.7);
const [PLATE_X, PLATE_Y] = iso(0, 0, 5.0);

const INDEX_BLOCKS = [
  [-1.45, -1.45], [0, -1.5], [1.45, -1.45], [-1.5, 0], [1.5, 0], [-1.45, 1.45], [0, 1.5], [1.45, 1.45],
]
  .sort((a, b) => a[0] + a[1] - (b[0] + b[1]))
  .map(([x, y]) => ({ behind: x + y < 0, p: prism(x - 0.16, y - 0.16, 0.82, 0.32, 0.32, 0.3) }));

const CORE_LEFT: Point = [CORE.cx - CORE.rx + 4, CORE.top + 34];
const CORE_RIGHT: Point = [CORE.cx + CORE.rx - 2, CORE.top + 46];
const FLOWS = {
  ingest: curve([[150, 474], [128, 432], [106, 384]]),
  prep: curve([...PREP.map(chipTop), [CORE_LEFT[0] - 18, CORE_LEFT[1] - 22], CORE_LEFT]),
  index: curve([CORE_RIGHT, [370, 300], chipTop(QUERY[0])]),
  question: curve([[QUESTION.x + QUESTION.w - 30, QUESTION.y + QUESTION.h - 2], [410, 230], chipTop(QUERY[0])]),
  answer: curve([...QUERY.map(chipTop), [ANSWER.x + 40, ANSWER.y + ANSWER.h - 6]]),
};

export default function PremiumRagPipelineHero() {
  const t = useTranslations("login");
  const labels: Record<Glyph, string> = {
    ingest: t("heroIngestion"),
    vision: t("heroVision"),
    embed: t("heroEmbeddings"),
    index: t("heroIndexing"),
    search: t("heroRetrieval"),
    rerank: t("heroReranking"),
    llm: t("heroLlm"),
  };

  return (
    <figure className={s.hero} aria-labelledby="rag-hero-title">
      <figcaption className={s.header}>
        <h2 id="rag-hero-title" className={s.title}>
          {t("heroTitle")}
        </h2>
      </figcaption>

      <p className={s.srOnly}>{t("heroA11y")}</p>

      <div className={s.stage}>
        <svg
          className={s.scene}
          viewBox={`0 0 ${VIEWBOX.width} ${VIEWBOX.height}`}
          preserveAspectRatio="xMidYMid meet"
          aria-hidden="true"
          focusable="false"
          data-testid="rag-hero-scene"
        >
          <SceneDefs />

          {/* Sol : halo et frontière de l'espace documentaire */}
          <ellipse cx="300" cy="342" rx="262" ry="150" fill="url(#rgh-floor)" />
          <polygon points={BOUNDARY} className={s.boundary} />

          {/* Flux : tracé fixe sous les volumes */}
          <g>
            {Object.values(FLOWS).map((d) => (
              <path key={d} d={d} className={s.flowBase} />
            ))}
          </g>

          {/* Moteur : socle, second étage, blocs d'index arrière */}
          <Solid p={PLINTH} tone="bronze" />
          <Solid p={TIER} tone="glass" />
          {INDEX_BLOCKS.filter((b) => b.behind).map((b, i) => (
            <Solid key={`ib${i}`} p={b.p} tone="block" className={s.indexBlock} />
          ))}

          {/* Noyau */}
          <ellipse cx={CORE.cx} cy={CORE.top + 10} rx="120" ry="78" fill="url(#rgh-core-glow)" className={s.coreGlow} />
          <path d={CORE.body} fill="url(#rgh-core)" className={s.coreBody} />
          <ellipse cx={CORE.cx} cy={CORE.top} rx={CORE.rx} ry={CORE.ry} className={s.coreTop} />
          <ellipse cx={CORE.cx} cy={CORE.top} rx={CORE.rx * 0.55} ry={CORE.ry * 0.55} className={s.coreEye} />
          {INDEX_BLOCKS.filter((b) => !b.behind).map((b, i) => (
            <Solid key={`if${i}`} p={b.p} tone="block" className={s.indexBlock} />
          ))}

          {/* Plaques de verre et anneaux */}
          {PLATES.map((p, i) => (
            <Solid key={`pl${i}`} p={p} tone="pane" />
          ))}
          <path d={RING_OUTER} className={`${s.ring} ${s.ringSlow}`} />
          <path d={RING_INNER} className={s.ring} />

          {/* Faisceau du noyau vers la plaque RAGatm */}
          <line x1={CORE.cx} y1={CORE.top - 4} x2={PLATE_X} y2={PLATE_Y + 14} className={s.beam} />
          <g className={s.brandPlate}>
            <rect x={PLATE_X - 56} y={PLATE_Y - 18} width="112" height="34" rx="17" className={s.brandPill} />
            <text x={PLATE_X} y={PLATE_Y + 5} textAnchor="middle" className={s.brandText}>
              RAG<tspan className={s.brandAccent}>atm</tspan>
            </text>
          </g>

          {/* Documents entrants */}
          <ellipse cx="128" cy="540" rx="110" ry="62" fill="url(#rgh-chip-glow)" className={s.docsGlow} />
          <g>
            {DOCS.map((d, i) => (
              <DocCard key={d.fmt} index={i} {...d} folderLabel={t("heroFolder")} />
            ))}
          </g>
          <text x="118" y="606" className={s.zoneLabel} textAnchor="middle">
            {t("heroDocuments")}
          </text>

          {/* Préparation : ingestion → vision → embeddings → indexation */}
          {PREP.map((c) => (
            <ChipNode key={c.key} chip={c} label={labels[c.key]} family="prep" />
          ))}

          {/* Question puis chaîne de réponse : recherche → reranking → LLM */}
          <QuestionCard text={t("heroQuestion")} />
          {QUERY.map((c) => (
            <ChipNode key={c.key} chip={c} label={labels[c.key]} family="query" />
          ))}

          {/* Impulsions (au-dessus des volumes) */}
          <g>
            <path d={FLOWS.ingest} pathLength={100} className={`${s.pulse} ${s.pIngest}`} />
            <path d={FLOWS.prep} pathLength={100} className={`${s.pulse} ${s.pPrep}`} />
            <path d={FLOWS.index} pathLength={100} className={`${s.pulse} ${s.pIndex}`} />
            <path d={FLOWS.question} pathLength={100} className={`${s.pulse} ${s.pQuestion}`} />
            <path d={FLOWS.answer} pathLength={100} className={`${s.pulse} ${s.pAnswer}`} />
          </g>

          <AnswerCard
            title={t("heroAnswer")}
            text={t("heroAnswerText")}
            source1={t("heroSource1")}
            source2={t("heroSource2")}
            verified={t("heroVerifiable")}
          />

          {/* Accès contrôlés */}
          <g>
            <circle cx={LOCK_X} cy={LOCK_Y} r="14" className={s.lockBadge} />
            <rect x={LOCK_X - 5.5} y={LOCK_Y - 1.5} width="11" height="8.5" rx="1.5" className={s.lockBody} />
            <path d={`M${LOCK_X - 3.2},${LOCK_Y - 1.5} v-3 a3.2,3.2 0 0 1 6.4,0 v3`} className={s.lockShackle} />
            <text x={LOCK_X} y={LOCK_Y + 32} textAnchor="middle" className={s.zoneLabel}>
              {t("heroAccess")}
            </text>
          </g>
        </svg>
      </div>

      <p className={s.subtitle}>{t("heroSubtitle")}</p>
    </figure>
  );
}

// --- Éléments ----------------------------------------------------------------------

function SceneDefs() {
  return (
    <defs>
      <radialGradient id="rgh-floor" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stopColor="#C58B62" stopOpacity="0.32" />
        <stop offset="100%" stopColor="#C58B62" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="rgh-core-glow" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stopColor="#F3D6B0" stopOpacity="0.75" />
        <stop offset="45%" stopColor="#C58B62" stopOpacity="0.28" />
        <stop offset="100%" stopColor="#C58B62" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="rgh-chip-glow" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stopColor="#F3D6B0" stopOpacity="0.85" />
        <stop offset="100%" stopColor="#F3D6B0" stopOpacity="0" />
      </radialGradient>
      <linearGradient id="rgh-core" x1="0" x2="1" y1="0" y2="0">
        <stop offset="0%" stopColor="#E3C39D" />
        <stop offset="38%" stopColor="#C58B62" />
        <stop offset="100%" stopColor="#5E3A24" />
      </linearGradient>
      <linearGradient id="rgh-card" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0%" stopColor="#443126" />
        <stop offset="100%" stopColor="#2B2019" />
      </linearGradient>
    </defs>
  );
}

function Solid({ p, tone, className }: { p: Prism; tone: "bronze" | "glass" | "pane" | "block" | "chip" | "chipQuery"; className?: string }) {
  return (
    <g className={`${s[tone]} ${className ?? ""}`}>
      <polygon points={p.left} className={s.faceLeft} />
      <polygon points={p.right} className={s.faceRight} />
      <polygon points={p.top} className={s.faceTop} />
    </g>
  );
}

function ChipNode({ chip, label, family }: { chip: ChipSpec; label: string; family: "prep" | "query" }) {
  // (x, y) = centre de la base du module à sa hauteur z.
  const p = prismAt(chip.x, chip.y, chip.z, CHIP.w, CHIP.d, CHIP.h);
  const [, topY] = chipTop(chip);
  const groundY = chip.y + chip.z * UNIT;
  const left = chip.side === "left";
  return (
    <g data-module={chip.key}>
      {chip.z > 0 && <line x1={chip.x} y1={chip.y + 10} x2={chip.x} y2={groundY} className={s.stem} />}
      <ellipse cx={chip.x} cy={topY + 2} rx="38" ry="24" fill="url(#rgh-chip-glow)" className={`${s.chipGlow} ${s[`g_${chip.key}`]}`} />
      <Solid p={p} tone={family === "prep" ? "chip" : "chipQuery"} />
      <polygon points={p.top} className={`${s.chipFlash} ${s[`g_${chip.key}`]}`} />
      <g transform={`translate(${chip.x.toFixed(1)} ${topY.toFixed(1)}) matrix(1 0 0 0.58 0 0)`}>
        <GlyphIcon name={chip.key} />
      </g>
      <text x={left ? chip.x - 32 : chip.x + 32} y={topY + 5} textAnchor={left ? "end" : "start"} className={s.label}>
        {label}
      </text>
    </g>
  );
}

function GlyphIcon({ name }: { name: Glyph }) {
  // Pictogrammes 12 × 12 tracés sur la face supérieure du module.
  switch (name) {
    case "ingest":
      return <path d="M0,-5 v6 M-3,-2 l3,3 3,-3 M-5,3 h10" className={s.glyph} />;
    case "vision":
      return (
        <g className={s.glyph}>
          <path d="M-6,0 C-3,-4 3,-4 6,0 C3,4 -3,4 -6,0 Z" />
          <circle cx="0" cy="0" r="1.6" />
        </g>
      );
    case "embed":
      return (
        <g className={s.glyphFill}>
          {[-4, 0, 4].flatMap((x) => [-3, 1].map((y) => <circle key={`${x}${y}`} cx={x} cy={y} r="1.2" />))}
        </g>
      );
    case "index":
      return <path d="M-5,-3 h10 M-5,0 h10 M-5,3 h7" className={s.glyph} />;
    case "search":
      return (
        <g className={s.glyph}>
          <circle cx="-1" cy="-1" r="3.6" />
          <path d="M1.8,1.8 l3.2,3.2" />
        </g>
      );
    case "rerank":
      return <path d="M-3,4 v-8 M-5.5,-1.5 l2.5,-2.5 2.5,2.5 M3,-4 v8 M0.5,1.5 l2.5,2.5 2.5,-2.5" className={s.glyph} />;
    case "llm":
      return <path d="M0,-5 v10 M-5,0 h10 M-3.5,-3.5 l7,7 M3.5,-3.5 l-7,7" className={s.glyph} />;
  }
}

function DocCard({ fmt, band, x, y, index, folderLabel }: { fmt: string; band: string; x: number; y: number; index: number; folderLabel: string }) {
  const w = 50;
  const h = 64;
  const isFolder = fmt === "FOLDER";
  return (
    <g className={s.doc} style={{ animationDelay: `${-index * 0.9}s` }} data-doc={fmt}>
      <g transform={planeMatrix(x, y, "faceRight")}>
        <polygon points={`${w},0 ${w + 4},3 ${w + 4},${h + 3} ${w},${h}`} className={s.docEdge} />
        {isFolder ? (
          <>
            <path d={`M0,8 h17 l5,-7 h${w - 22} v${h - 1} h-${w} Z`} className={s.folder} />
            <text x={w / 2} y={h - 11} textAnchor="middle" className={`${s.docFmt} ${s.folderText}`}>
              {folderLabel}
            </text>
          </>
        ) : (
          <>
            <rect x="0" y="0" width={w} height={h} rx="3" className={s.paper} />
            <path d={`M0,3 a3,3 0 0 1 3,-3 h${w - 6} a3,3 0 0 1 3,3 v13 h-${w} Z`} fill={band} />
            <text x={w / 2} y="12" textAnchor="middle" className={s.docFmt}>
              {fmt}
            </text>
            {fmt === "IMG" ? (
              <path d="M8,52 l11,-15 7,9 5,-6 11,12 Z" className={s.docArt} />
            ) : (
              [26, 33, 40, 47].map((ly, i) => <rect key={ly} x="8" y={ly} width={i === 3 ? 22 : 34} height="2.8" rx="1.4" className={s.docLine} />)
            )}
          </>
        )}
      </g>
    </g>
  );
}

function QuestionCard({ text }: { text: string }) {
  const { x, y, w, h } = QUESTION;
  return (
    <g className={s.question}>
      <g transform={billboardMatrix(x, y, 0.1)}>
        <rect x="3" y="4" width={w} height={h} rx="11" className={s.cardShadow} />
        <rect x="0" y="0" width={w} height={h} rx="11" className={s.questionCard} />
        <circle cx="20" cy={h / 2} r="9" className={s.questionDot} />
        <text x="20" y={h / 2 + 4} textAnchor="middle" className={s.questionMark}>
          ?
        </text>
        <text x="37" y={h / 2 + 4.5} className={s.questionText}>
          {text}
        </text>
      </g>
    </g>
  );
}

function AnswerCard({ title, text, source1, source2, verified }: { title: string; text: string; source1: string; source2: string; verified: string }) {
  const { x, y, w, h } = ANSWER;
  return (
    <g className={s.answer} data-testid="rag-hero-answer">
      <ellipse cx={x + w / 2} cy={y + h / 2 + 6} rx="112" ry="88" fill="url(#rgh-chip-glow)" className={s.answerGlow} />
      <g transform={billboardMatrix(x, y, 0.16)}>
        {/* épaisseur du panneau */}
        <path d={`M${w},10 l5,4 v${h - 4} l-5,-4 Z`} className={s.cardEdge} />
        <rect x="4" y="6" width={w} height={h} rx="11" className={s.cardShadow} />
        <rect x="0" y="0" width={w} height={h} rx="11" fill="url(#rgh-card)" className={s.answerCard} />
        <rect x="0" y="0" width={w} height={h} rx="11" className={s.answerFlash} />
        <text x="13" y="25" className={s.answerQuote}>
          “
        </text>
        <text x="30" y="22" className={s.answerTitle}>
          {title}
        </text>
        <text x="13" y="40" className={s.answerText}>
          {text}
        </text>
        {[48, 55].map((ly, i) => (
          <rect key={ly} x="13" y={ly} width={i ? 100 : 144} height="2.8" rx="1.4" className={s.answerLine} />
        ))}
        <line x1="13" y1="66" x2={w - 13} y2="66" className={s.answerRule} />
        {[source1, source2].map((label, i) => (
          <g key={label} className={`${s.source} ${i ? s.source2 : s.source1}`}>
            <rect x="13" y={74 + i * 18} width="10" height="13" rx="2" className={s.sourceIcon} />
            <path d={`M15.5,${78 + i * 18} h5 M15.5,${81 + i * 18} h5`} className={s.sourceIconLine} />
            <text x="30" y={84 + i * 18} className={s.sourceText}>
              {label}
            </text>
          </g>
        ))}
        <g>
          <rect x="13" y="112" width="122" height="16" rx="8" className={s.verifiedPill} />
          <path d="M21,120 l2.5,2.5 4.5,-5" className={s.verifiedTick} />
          <text x="33" y="123.5" className={s.verifiedText}>
            {verified}
          </text>
        </g>
      </g>
    </g>
  );
}
