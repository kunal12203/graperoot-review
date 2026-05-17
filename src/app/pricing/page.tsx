"use client";

import { useState } from "react";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { Check, X, Server, Sparkles, ArrowRight, ChevronDown } from "lucide-react";

const PLANS = [
  {
    name: "Free",
    price: "$0",
    period: "forever",
    desc: "Try it on any public repo",
    cta: "Install free",
    href: "https://github.com/apps/graperoot-review/installations/new",
    highlight: false,
    features: [
      { label: "5 reviews / month",            ok: true  },
      { label: "Public repos",                 ok: true  },
      { label: "Graph-proven findings",        ok: true  },
      { label: "Committable suggestions",      ok: true  },
      { label: "Private repos",                ok: false },
      { label: "Custom rules",                 ok: false },
      { label: "@graperoot PR chat",           ok: false },
    ],
  },
  {
    name: "Pro",
    price: "$15",
    period: "/ user / mo",
    desc: "Unlimited reviews · cancel anytime",
    note: "No credit card required",
    cta: "Start free trial",
    href: "https://github.com/apps/graperoot-review/installations/new",
    highlight: true,
    badge: "Most popular",
    features: [
      { label: "Unlimited reviews",            ok: true },
      { label: "Private repos",                ok: true },
      { label: "Graph-proven findings",        ok: true },
      { label: "Cross-repo blast radius",      ok: true },
      { label: "Custom rules (graperoot.yml)", ok: true },
      { label: "@graperoot PR chat",           ok: true },
      { label: "Review analytics dashboard",  ok: true },
    ],
  },
];

const FAQ = [
  {
    q: "Why is self-hosted cheaper to run?",
    a: "You bring your own Anthropic API key (BYOK). You pay Claude directly — ~$0.05/review. We charge a flat platform fee. No per-review markup.",
  },
  {
    q: "Does the free tier require a credit card?",
    a: "No. Install the GitHub App and you get 5 reviews/month immediately.",
  },
  {
    q: "What counts as a review?",
    a: "One PR = one review. Re-reviews on new commits (synchronize events) count as separate reviews.",
  },
  {
    q: "Can I switch between hosted and self-hosted?",
    a: "Yes. Same GitHub App, same config file. Just point the webhook URL at your self-hosted server and set your own API key.",
  },
];

function FaqItem({ q, a }: { q: string; a: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b border-zinc-800/60 last:border-0">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between py-4 text-left text-sm font-medium text-zinc-200 hover:text-white transition-colors gap-4"
      >
        {q}
        <ChevronDown size={16} className={`flex-shrink-0 text-zinc-500 transition-transform duration-200 ${open ? "rotate-180" : ""}`} />
      </button>
      {open && <p className="pb-4 text-sm text-zinc-500 leading-relaxed">{a}</p>}
    </div>
  );
}

export default function PricingPage() {
  return (
    <main className="min-h-screen text-white">
      <Navbar />

      <section className="relative px-4 pt-36 pb-24 overflow-hidden">
        <div className="absolute inset-0 pointer-events-none">
          <div className="absolute left-1/2 top-1/3 -translate-x-1/2 w-[700px] h-[400px] bg-grape-500/10 rounded-[100%] blur-[120px]" />
        </div>
        <div className="relative z-10 max-w-5xl mx-auto">
          <p className="text-center text-[11px] font-bold text-grape-400 uppercase tracking-widest mb-3">
            Pricing
          </p>
          <h1 className="font-sans text-center text-4xl sm:text-5xl font-bold tracking-tight text-white mb-4">
            Simple pricing.{" "}
            <span className="bg-gradient-to-r from-grape-400 to-purple-400 bg-clip-text text-transparent">
              No surprises.
            </span>
          </h1>
          <p className="text-center text-zinc-400 text-base max-w-xl mx-auto mb-14">
            ~$0.05 API cost per review. We pass most of the savings to you.
          </p>

          {/* Plans */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5 md:gap-6 items-stretch max-w-2xl mx-auto w-full">
            {PLANS.map((plan) => (
              <div key={plan.name} className="group relative h-full">
                {plan.highlight && (
                  <div className="absolute -inset-px rounded-2xl bg-[linear-gradient(135deg,rgb(168_85_247),rgb(236_72_153),rgb(168_85_247))] bg-[length:200%_100%] animate-flow-border opacity-90" />
                )}
                <div className={`relative h-full rounded-2xl p-7 sm:p-8 flex flex-col ${
                  plan.highlight ? "bg-[#0e0e11]" : "bg-[#111113]/80 border border-white/[0.07] hover:border-grape-500/30 transition-colors"
                }`}>
                  {plan.badge && (
                    <div className="absolute -top-3 left-1/2 -translate-x-1/2 inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-grape-600 text-white text-[10px] font-semibold uppercase tracking-widest shadow-lg shadow-grape-900/40">
                      <Sparkles size={10} />
                      {plan.badge}
                    </div>
                  )}
                  <h3 className="font-sans text-white font-bold text-xl tracking-tight mb-2">{plan.name}</h3>
                  <p className="text-zinc-400 text-sm leading-relaxed mb-6 min-h-[2.5rem]">{plan.desc}</p>
                  <div className="mb-6">
                    <span className={`font-sans text-4xl font-bold tracking-tight ${
                      plan.highlight ? "bg-gradient-to-r from-grape-300 to-purple-300 bg-clip-text text-transparent" : "text-white"
                    }`}>{plan.price}</span>
                    <span className="text-zinc-500 text-sm ml-2">{plan.period}</span>
                  </div>
                  <a
                    href={plan.href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={`inline-flex items-center justify-center gap-2 px-5 py-3 rounded-xl font-semibold text-sm transition-all mb-2 ${
                      plan.highlight
                        ? "bg-grape-600 hover:bg-grape-500 text-white shadow-xl shadow-grape-900/40"
                        : "bg-white/[0.04] hover:bg-white/[0.08] text-white border border-white/[0.08] hover:border-white/[0.16]"
                    }`}
                  >
                    {plan.cta}
                  </a>
                  {"note" in plan && plan.note && (
                    <p className="text-[11px] text-zinc-600 text-center mb-5">{plan.note}</p>
                  )}
                  {!("note" in plan && plan.note) && <div className="mb-5" />}
                  <ul className="space-y-3 mt-auto">
                    {plan.features.map((f) => (
                      <li key={f.label} className="flex items-start gap-2.5 text-sm">
                        {f.ok
                          ? <Check size={16} className={`mt-0.5 flex-shrink-0 ${plan.highlight ? "text-grape-400" : "text-emerald-400"}`} />
                          : <X     size={16} className="mt-0.5 flex-shrink-0 text-zinc-700" />}
                        <span className={f.ok ? "text-zinc-300" : "text-zinc-600 line-through"}>{f.label}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ))}
          </div>

          {/* Enterprise */}
          <div className="mt-8 rounded-2xl border border-zinc-700/60 bg-zinc-900/60 p-7 sm:p-8">
            <div className="flex flex-col sm:flex-row items-start sm:items-center gap-6">
              <div className="w-12 h-12 rounded-xl bg-zinc-800 border border-zinc-700/60 flex items-center justify-center flex-shrink-0">
                <Server size={22} className="text-zinc-300" />
              </div>
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-1">
                  <h3 className="font-sans text-base font-bold text-zinc-100">Enterprise</h3>
                  <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-zinc-700/60 text-zinc-400 uppercase tracking-widest">Self-hosted</span>
                </div>
                <p className="text-sm text-zinc-400 leading-relaxed">
                  Your Claude API key. Your network. Code never leaves your servers.
                  Air-gapped mode for defense &amp; healthcare. Docker or Kubernetes.
                  Set <code className="text-grape-300 bg-grape-500/10 px-1 rounded text-xs">ENABLE_GRAPH_CLONE=1</code> for full AST blast-radius analysis.
                </p>
              </div>
              <a
                href="mailto:hello@graperoot.dev"
                className="flex-shrink-0 inline-flex items-center gap-2 text-sm px-5 py-2.5 rounded-xl bg-zinc-800 hover:bg-zinc-700 border border-zinc-700/60 hover:border-zinc-600 text-zinc-200 hover:text-white font-semibold transition-all whitespace-nowrap"
              >
                Book a demo <ArrowRight size={14} />
              </a>
            </div>
          </div>

          {/* FAQ */}
          <div className="mt-12 max-w-2xl mx-auto">
            <p className="text-center text-[11px] font-bold text-zinc-500 uppercase tracking-widest mb-6">FAQ</p>
            {FAQ.map((item) => (
              <FaqItem key={item.q} q={item.q} a={item.a} />
            ))}
          </div>
        </div>
      </section>

      <Footer />
    </main>
  );
}
