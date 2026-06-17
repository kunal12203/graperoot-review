"use client";

import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { Check, X } from "lucide-react";

const COMPARE = [
  { feature: "Graph-proven findings",   gr: "Real import edges", cr: "LLM guess",       ca: "LLM guess",        gp: "Partial"         },
  { feature: "Cross-repo blast radius", gr: true,                cr: false,              ca: false,              gp: false             },
  { feature: "Committable suggestions", gr: true,                cr: true,               ca: true,               gp: false             },
  { feature: "Rate limits",             gr: "None",              cr: "5–10/hr (Pro)",    ca: "None",             gp: "None"            },
  { feature: "Self-hosted at Pro tier", gr: true,                cr: "Enterprise only",  ca: "Enterprise only",  gp: "Enterprise only" },
  { feature: "Free for open source",    gr: "5 reviews/mo",      cr: "Unlimited",        ca: "100% discount",    gp: "Qualified OSS"   },
  { feature: "Price / user / month",    gr: "$15",               cr: "$24",              ca: "$24–30",           gp: "$30"             },
  { feature: "BYOK (your API key)",     gr: "Self-hosted",       cr: false,              ca: false,              gp: false             },
  { feature: "Code leaves your server", gr: "Never",             cr: "Yes",              ca: "Yes",              gp: "Yes"             },
];

const HEADROOM_COMPARE = [
  { feature: "What it does",            gr: "Selects which context to send",     hr: "Compresses context after selection"     },
  { feature: "Works at",                gr: "Upstream (selection)",              hr: "Downstream (compression)"              },
  { feature: "Knows your codebase",     gr: "Yes — graph of every symbol/edge",  hr: "No — stateless per request"            },
  { feature: "Finds the right 8 files", gr: "Yes — graph traversal query",       hr: "No — compresses whatever it receives"  },
  { feature: "Cross-file relationships",gr: "Yes — import edges, call graphs",   hr: "No — AST within a single file only"    },
  { feature: "Lossless",                gr: "Yes — original code untouched",     hr: "No — lossy by design (CCR recovery)"   },
  { feature: "Token savings mechanism", gr: "Read 8 relevant files not 40",      hr: "Shrink those 40 files by 60–92%"       },
  { feature: "Open source",             gr: "No",                                hr: "Yes — Apache 2.0, 30k+ stars"          },
  { feature: "Works together?",         gr: "Yes — complementary",               hr: "Yes — complementary"                   },
];

function Cell({ val }: { val: string | boolean }) {
  if (val === true)  return <Check size={15} className="text-green-400 mx-auto" />;
  if (val === false) return <X     size={15} className="text-zinc-700 mx-auto" />;
  const positive = ["Real import edges", "None", "Never", "Self-hosted", "5 reviews/mo", "$15", "Partial"];
  return (
    <span className={`text-xs ${positive.includes(val as string) ? "text-zinc-200" : "text-zinc-500"}`}>
      {val}
    </span>
  );
}

export default function ComparePage() {
  return (
    <main className="min-h-screen text-white">
      <Navbar />

      <section className="relative px-4 pt-36 pb-24 overflow-hidden">
        <div className="max-w-4xl mx-auto">
          <p className="text-center text-[11px] font-bold text-grape-500 uppercase tracking-widest mb-3">
            Comparison
          </p>
          <h1 className="font-sans text-center text-4xl sm:text-5xl font-bold text-zinc-100 mb-4 tracking-tight">
            Built different from<br />CodeRabbit and CodeAnt
          </h1>
          <p className="text-center text-zinc-400 text-base mb-14 max-w-lg mx-auto leading-relaxed">
            Not just cheaper — fundamentally more trustworthy. Every finding has a source.
          </p>

          {/* Table */}
          <div className="rounded-xl border border-white/[0.07] overflow-hidden mb-6">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/[0.07] bg-[#0e0e11]">
                  <th className="text-left px-4 py-3 text-[11px] text-zinc-500 uppercase tracking-wider w-[32%]">Feature</th>
                  <th className="px-3 py-3 text-[11px] text-grape-400 uppercase tracking-wider text-center">GrapeRoot</th>
                  <th className="px-3 py-3 text-[11px] text-zinc-500 uppercase tracking-wider text-center">CodeRabbit</th>
                  <th className="px-3 py-3 text-[11px] text-zinc-500 uppercase tracking-wider text-center">CodeAnt</th>
                  <th className="px-3 py-3 text-[11px] text-zinc-500 uppercase tracking-wider text-center">Greptile</th>
                </tr>
              </thead>
              <tbody>
                {COMPARE.map((row, i) => (
                  <tr key={row.feature} className={`border-b border-white/[0.04] last:border-0 ${i % 2 ? "bg-zinc-900/20" : ""}`}>
                    <td className="px-4 py-3 text-zinc-400 text-xs">{row.feature}</td>
                    <td className="px-3 py-3 text-center bg-grape-500/5"><Cell val={row.gr} /></td>
                    <td className="px-3 py-3 text-center"><Cell val={row.cr} /></td>
                    <td className="px-3 py-3 text-center"><Cell val={row.ca} /></td>
                    <td className="px-3 py-3 text-center"><Cell val={row.gp} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Callout */}
          <div className="rounded-xl border border-grape-500/20 bg-grape-500/5 p-5 mb-6">
            <p className="text-sm text-zinc-200 italic mb-2">
              &ldquo;Every finding is graph-proven — we cite the exact import chain, not a guess.&rdquo;
            </p>
            <p className="text-xs text-zinc-500 leading-relaxed">
              The one claim no competitor can match. CodeAnt&apos;s headline stat is 87.6% F1.
              GrapeRoot&apos;s is 0 graph-fabricated findings — because the graph either has
              the edge or it doesn&apos;t.
            </p>
          </div>

          {/* Graph citation visual */}
          <div className="rounded-xl border border-white/[0.07] bg-[#111113] overflow-hidden">
            <div className="px-4 py-2.5 bg-[#0e0e11] border-b border-white/[0.05] flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-grape-500/70" />
              <span className="text-xs text-zinc-300 font-semibold">What a graph citation looks like</span>
            </div>
            <div className="px-4 py-4">
              <div className="pl-3 border-l-2 border-grape-500/50 bg-grape-500/5 py-2 rounded-r">
                <p className="text-[10px] text-zinc-500 mb-1.5 uppercase tracking-wider font-semibold">Graph-proven blast radius</p>
                <p className="text-[12px] text-grape-300 font-mono">
                  index_set.go → compactor.go → shipper.go →{" "}
                  <span className="text-white font-semibold">store/chunk_store.go</span>
                </p>
              </div>
            </div>
          </div>

          {/* Headroom comparison */}
          <div className="mt-16">
            <p className="text-center text-[11px] font-bold text-grape-500 uppercase tracking-widest mb-3">
              vs token compression
            </p>
            <h2 className="font-sans text-center text-3xl sm:text-4xl font-bold text-zinc-100 mb-4 tracking-tight">
              GrapeRoot vs Headroom
            </h2>
            <p className="text-center text-zinc-400 text-sm mb-8 max-w-lg mx-auto leading-relaxed">
              Headroom (30k+ stars) compresses context after you select it.
              GrapeRoot selects the right context before you ever read a file.
              They solve different problems — and work best together.
            </p>

            <div className="rounded-xl border border-white/[0.07] overflow-hidden mb-6">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-white/[0.07] bg-[#0e0e11]">
                    <th className="text-left px-4 py-3 text-[11px] text-zinc-500 uppercase tracking-wider w-[36%]">Dimension</th>
                    <th className="px-3 py-3 text-[11px] text-grape-400 uppercase tracking-wider text-center">GrapeRoot</th>
                    <th className="px-3 py-3 text-[11px] text-zinc-500 uppercase tracking-wider text-center">Headroom</th>
                  </tr>
                </thead>
                <tbody>
                  {HEADROOM_COMPARE.map((row, i) => (
                    <tr key={row.feature} className={`border-b border-white/[0.04] last:border-0 ${i % 2 ? "bg-zinc-900/20" : ""}`}>
                      <td className="px-4 py-3 text-zinc-400 text-xs">{row.feature}</td>
                      <td className="px-3 py-3 text-center bg-grape-500/5 text-xs text-zinc-200">{row.gr}</td>
                      <td className="px-3 py-3 text-center text-xs text-zinc-500">{row.hr}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="rounded-xl border border-grape-500/20 bg-grape-500/5 p-5">
              <p className="text-sm text-zinc-200 italic mb-2">
                &ldquo;Headroom shrinks 40 files down. GrapeRoot tells you it&apos;s only 8 files you need.&rdquo;
              </p>
              <p className="text-xs text-zinc-500 leading-relaxed">
                Token compression and context selection are complementary — not competitive.
                Run GrapeRoot to pick the right files, then Headroom to compress them.
                Together they give you the lowest possible token spend with the highest signal.
              </p>
            </div>
          </div>
        </div>
      </section>

      <Footer />
    </main>
  );
}
