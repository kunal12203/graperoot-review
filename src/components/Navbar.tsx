"use client";

import { useState, useEffect } from "react";
import { Menu, X } from "lucide-react";

const GH_APP = "https://github.com/apps/graperoot-review/installations/new/permissions?target_id=84341876";

const navLinks = [
  { label: "About",     href: "/how"       },
  { label: "Compare",   href: "/compare"   },
  { label: "Pricing",   href: "/pricing"   },
  { label: "Dashboard", href: "/dashboard" },
];

function GhIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className="w-3.5 h-3.5 flex-shrink-0">
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" />
    </svg>
  );
}

export default function Navbar() {
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen]         = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 24);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <nav className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${
      scrolled
        ? "bg-[#09090b]/97 backdrop-blur-2xl border-b border-zinc-800"
        : "bg-[#09090b]/70 backdrop-blur-xl border-b border-zinc-800/40"
    }`}>
      <div className="max-w-7xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
        <a href="/" className="flex items-center gap-2.5 group flex-shrink-0">
          <div className="w-7 h-7 rounded-lg bg-grape-500/15 border border-grape-500/30 flex items-center justify-center group-hover:bg-grape-500/25 transition-all overflow-hidden">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/image.svg" alt="GrapeRoot" className="w-5 h-5" />
          </div>
          <span className="font-semibold text-white tracking-tight">GrapeRoot Review</span>
        </a>

        <div className="hidden md:flex items-center gap-6 lg:gap-7">
          {navLinks.map((l) => (
            <a key={l.label} href={l.href} className="text-sm text-zinc-400 hover:text-white transition-colors">
              {l.label}
            </a>
          ))}
        </div>

        <div className="hidden md:flex items-center gap-3">
          <a
            href={GH_APP}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg border border-zinc-700 hover:border-zinc-600 text-zinc-300 hover:text-white font-medium transition-all"
          >
            <GhIcon />
            GitHub
          </a>
          <a
            href={GH_APP}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm px-4 py-1.5 rounded-lg bg-grape-600 hover:bg-grape-500 text-white font-medium transition-all shadow-lg shadow-grape-900/30"
          >
            <GhIcon />
            Login
          </a>
        </div>

        <button
          className="md:hidden text-zinc-400 hover:text-white transition-colors p-2 -mr-1"
          onClick={() => setOpen(!open)}
          aria-label="Toggle menu"
        >
          {open ? <X size={20} /> : <Menu size={20} />}
        </button>
      </div>

      {open && (
        <div className="md:hidden border-t border-zinc-800 bg-[#09090b]/98 backdrop-blur-2xl px-4 pt-4 pb-6 flex flex-col gap-1">
          {navLinks.map((l) => (
            <a
              key={l.label}
              href={l.href}
              onClick={() => setOpen(false)}
              className="text-sm text-zinc-400 hover:text-white transition-colors py-3 border-b border-zinc-800/60 last:border-0"
            >
              {l.label}
            </a>
          ))}
          <div className="flex flex-col gap-2 mt-4">
            <a
              href={GH_APP}
              target="_blank"
              rel="noopener noreferrer"
              onClick={() => setOpen(false)}
              className="inline-flex items-center justify-center gap-1.5 text-sm px-4 py-2.5 rounded-lg bg-grape-600 text-white font-medium"
            >
              <GhIcon />
              Login
            </a>
          </div>
        </div>
      )}
    </nav>
  );
}
